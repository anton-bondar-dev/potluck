"""The agent's tool surface — and the trust boundary.

Every model action enters here and passes the same sequence:

    validate args -> guards.check -> real PayPal call -> compact digest back to the model

Two properties this file is built around:

  * THE DISPATCHER NEVER RAISES. A guard refusal, a bad argument or an upstream error all
    come back as a dict the model can read and retry against. An exception escaping here
    would end an episode mid-flight and strand a pool.

  * NO MONEY-OUT TOOL TAKES AN AMOUNT OR AN EMAIL. Amounts come from the roster frozen at
    plan_pool; destinations come from the pool's allowlist. The model supplies decisions
    and words, never numbers or addresses — so a hallucinated payout is not a risk we
    mitigate, it is unrepresentable in these signatures.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import clock
import guards
import store
from config import settings
from guards import GuardError
from paypal import paid_cents_from_invoice, str_to_cents

SLEEP_SENTINEL = "__sleep__"   # tools return this so the episode loop knows to end


def mask_email(email: str) -> str:
    try:
        name, dom = email.split("@", 1)
    except ValueError:
        return "***"
    return f"{name[0]}***{name[-1] if len(name) > 1 else ''}@{dom}"


# --- tool schemas (OpenAI function-calling) ---------------------------------
def _schema(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


SCHEMAS = {
    "plan_pool": _schema(
        "plan_pool",
        "Turn the organizer's goal into the pool's contract: who contributes how much, "
        "who receives the money, and by when. Call this once. You must also restate the "
        "organizer's success condition in your own words — that restatement is shown to "
        "everyone and you will be asked to honour it at the deadline.",
        {
            "restatement": {"type": "string", "description": "One sentence: what this pool is doing."},
            "success_condition_restated": {
                "type": "string",
                "description": "The organizer's success condition, in your own words. Be precise: "
                               "this is what you will be held to when you decide."},
            "recipient_cid": {"type": "string", "description": "Who receives the money if it goes ahead."},
            "target_cents": {"type": "integer", "description": "The goal amount, in integer cents."},
            "deadline_label": {"type": "string", "description": "Human label, e.g. 'Friday 9am'."},
            "horizon_seconds": {"type": "integer", "description": "Seconds from now until the deadline."},
            "contributors": {
                "type": "array",
                "description": "One entry per contributor, by cid, with their share in cents.",
                "items": {"type": "object", "properties": {
                    "cid": {"type": "string"},
                    "share_cents": {"type": "integer"}}, "required": ["cid", "share_cents"]}},
            "overage": {"type": "string", "enum": ["refund_prorata", "keep_to_target"],
                        "description": "What happens to money collected above the target."},
        },
        ["restatement", "success_condition_restated", "recipient_cid", "target_cents",
         "deadline_label", "horizon_seconds", "contributors"]),

    "send_invoices": _schema(
        "send_invoices",
        "Create and send a real PayPal invoice to each named contributor. Amounts come "
        "from the roster you set in plan_pool — you cannot re-price here.",
        {"cids": {"type": "array", "items": {"type": "string"},
                  "description": "Contributor cids, or ['all']."},
         "item_label": {"type": "string", "description": "What the invoice line says, e.g. 'Gift share'."},
         "notes": {"type": "array", "description": "Optional per-person note.",
                   "items": {"type": "object", "properties": {
                       "cid": {"type": "string"}, "note": {"type": "string"}},
                       "required": ["cid", "note"]}}},
        ["cids", "item_label"]),

    "check_payments": _schema(
        "check_payments",
        "Read the live payment state of every invoice straight from PayPal. This is your "
        "only source of truth about money; never assume a payment.",
        {"reason": {"type": "string", "description": "Why you are checking now."}},
        ["reason"]),

    "remind_contributors": _schema(
        "remind_contributors",
        "Send a reminder to specific unpaid contributors, each with your own wording. "
        "Choose who actually needs nudging — reminding everyone every time is noise.",
        {"reminders": {"type": "array", "items": {"type": "object", "properties": {
            "cid": {"type": "string"}, "note": {"type": "string"}},
            "required": ["cid", "note"]}}},
        ["reminders"]),

    "drop_contributor": _schema(
        "drop_contributor",
        "Cancel one contributor's invoice — they are out of the pool. Their share stops "
        "being expected. Cannot be used on someone who already paid.",
        {"cid": {"type": "string"}, "reason": {"type": "string"}},
        ["cid", "reason"]),

    "sleep_until": _schema(
        "sleep_until",
        "Stop working and wait. This ENDS your turn; you will be woken when something "
        "worth your attention happens and told what changed. Waiting costs nothing, so "
        "prefer it over polling in a loop.",
        {"sleep_s": {"type": "integer", "description": "Real seconds to wait, at most until the deadline."},
         "reason": {"type": "string", "description": "What you are waiting for."},
         "wake_on": {"type": "array", "items": {"type": "string", "enum": ["payment", "deadline"]}}},
        ["sleep_s", "reason"]),

    "resolve_pool": _schema(
        "resolve_pool",
        "Make the call and move the money. RELEASE pays the pool to the recipient; UNWIND "
        "refunds every contributor exactly what they paid and cancels the unpaid invoices. "
        "Quote the organizer's own words that justify your choice, and attest the numbers "
        "you are deciding on — they are checked against PayPal before anything moves. You "
        "do not specify amounts or destinations; those are computed from the ledger.",
        {"decision": {"type": "string", "enum": ["RELEASE", "UNWIND"]},
         "reason": {"type": "string", "description": "Your reasoning, for the humans involved."},
         "policy_quote": {"type": "string",
                          "description": "The words from the organizer's condition that justify this."},
         "attest": {"type": "object", "description": "The numbers you are deciding on.",
                    "properties": {"collected_cents": {"type": "integer"},
                                   "paid_count": {"type": "integer"},
                                   "ledger_version": {"type": "integer"}},
                    "required": ["collected_cents", "paid_count", "ledger_version"]}},
        ["decision", "reason", "policy_quote", "attest"]),

    "check_payout": _schema(
        "check_payout", "Check whether the payout batch has settled.",
        {"reason": {"type": "string"}}, ["reason"]),

    "close_pool": _schema(
        "close_pool",
        "Close the pool with a short summary for the humans involved. The decision is "
        "already made and the money has already moved — describe what happened, in the "
        "terms of the decision you took. Only call this once PayPal reports the payout "
        "terminal.",
        {"summary": {"type": "string",
                     "description": "Two or three sentences: what was decided, what "
                                    "moved, and what each person should expect to see."}},
        ["summary"]),
}


def menu_for_phase(phase: str, *, deadline_reached: bool = False,
                   target_met: bool = False) -> list[dict]:
    """Hand the model 1-4 schemas, never 9.

    This is the biggest single saving on tokens AND a real guardrail: resolve_pool is not
    merely discouraged during collection, it is absent from the menu and refused by
    check_phase if asked for anyway.
    """
    if phase == "DRAFT":
        names = ["plan_pool"]
    elif phase == "COLLECTING":
        names = ["check_payments", "send_invoices", "remind_contributors",
                 "drop_contributor", "sleep_until"]
        if deadline_reached or target_met:
            names.append("resolve_pool")
    elif phase == "RESOLVING":
        names = ["check_payments", "resolve_pool"]
    elif phase == "SETTLING":
        # sleep_until belongs here. Without it a model watching a PENDING batch has only
        # two legal moves — poll again (a no-op) or close (refused until PayPal is
        # terminal) — so it spins through its whole turn budget. A real batch takes
        # 25-30 seconds; waiting has to be something it can actually choose.
        names = ["check_payout", "sleep_until", "close_pool"]
    else:
        names = []
    return [SCHEMAS[n] for n in names]


# --- context ----------------------------------------------------------------
class ToolContext:
    def __init__(self, pool_id: str, pp, bus, episode_id: str | None = None):
        self.pool_id = pool_id
        self.pp = pp
        self.bus = bus
        self.episode_id = episode_id
        self._sem = asyncio.Semaphore(settings.paypal_concurrency)

    async def emit(self, etype: str, data: dict) -> None:
        await self.bus.publish(self.pool_id, etype, data)


# --- ledger helpers ---------------------------------------------------------
async def refresh_from_paypal(ctx: ToolContext, *, origin: str = "agent",
                             include_terminal: bool = False) -> dict:
    """Re-derive the ledger from PayPal's own numbers. The single definition of 'paid'.

    Public because the ticker owns the routine sweep: it polls, writes the ledger and
    streams the rows with no LLM in the loop, and only then decides whether anything that
    changed is worth waking the model for. The agent's own check_payments and the settle
    path call the same function, so there is one definition of 'paid' and it is PayPal's.
    """
    invoices = await store.get_invoices(ctx.pool_id)
    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}

    async def one(inv):
        # A paid or cancelled invoice cannot change again, so polling it every tick is
        # pure load: one 69-second rehearsal made 125 real PayPal calls, most of them
        # re-reading invoices that were already settled. The settle path passes
        # include_terminal=True, because there the whole point is a fresh read of
        # everything before money moves.
        if not inv.get("invoice_id"):
            return None
        if not include_terminal and inv["status"] in store.TERMINAL_INVOICE_STATUSES:
            return None
        async with ctx._sem:
            try:
                return inv["cid"], await ctx.pp.get_invoice(inv["invoice_id"], origin=origin)
            except Exception:  # noqa: BLE001 — one bad invoice must not fail the sweep
                return None

    for res in await asyncio.gather(*[one(i) for i in invoices]):
        if not res:
            continue
        cid, body = res
        status = body.get("status") or "SENT"
        paid = paid_cents_from_invoice(body)
        source = "merchant_recorded" if status == "MARKED_AS_PAID" else (
            "paypal_payment" if paid else None)
        if await store.set_invoice_state(ctx.pool_id, cid, status=status,
                                         paid_cents=paid, source=source):
            p = people.get(cid, {})
            await ctx.emit("ledger_row", {
                "cid": cid, "name": p.get("name"), "email_masked": mask_email(p.get("email", "")),
                "share_cents": p.get("share_cents"), "status": status, "paid_cents": paid,
                "source": source})
    return await ledger_snapshot(ctx)


async def ledger_snapshot(ctx: ToolContext) -> dict:
    """The pool's live numbers, from the rows PayPal last confirmed.

    Public because the episode header, the wake digest and the ticker all quote it: one
    definition means a judge comparing the banner, the agent's attestation and the
    model's own tool result finds no drift between them.
    """
    pool = await store.get_pool(ctx.pool_id)
    invoices = await store.get_invoices(ctx.pool_id)
    collected = sum(int(i["paid_cents"]) for i in invoices)
    paid_count = sum(1 for i in invoices if int(i["paid_cents"]) > 0)
    left = clock.time_left_s(pool["deadline_at"])
    return {"collected_cents": collected, "target_cents": int(pool["target_cents"]),
            "paid_count": paid_count, "n": len(invoices),
            "time_left_s": left,
            "time_left_label": clock.display_label(left, int(pool["clock_scale"])),
            "deadline_reached": clock.reached(pool["deadline_at"]),
            "ledger_version": int(pool["ledger_version"])}


# --- the tools --------------------------------------------------------------
async def _plan_pool(ctx, args, pool):
    cast = await store.get_contributors(ctx.pool_id)
    if not cast:
        raise GuardError("BAD_ARGS", "this pool has no cast to draw contributors from")
    by_cid = {c["cid"]: c for c in cast}
    contributors = args.get("contributors") or []
    recipient_cid = args.get("recipient_cid")
    target = args.get("target_cents")
    horizon = args.get("horizon_seconds")
    guards.check_plan(contributors, target, horizon, set(by_cid), recipient_cid)
    overage = args.get("overage") or "refund_prorata"
    if overage not in ("refund_prorata", "keep_to_target"):
        raise GuardError("BAD_ARGS", "overage must be refund_prorata or keep_to_target")

    shares = {c["cid"]: int(c["share_cents"]) for c in contributors}

    # ONE CLOCK, and this is the only place it is set.
    #
    # The model reads "by Friday" and asks for three days, which is the right answer to
    # the question it was asked — and useless for a demo nobody will watch for three
    # days. So a demo pool's REAL horizon is compressed to demo_horizon_s and the scale
    # is derived from the ratio, which means the countdown on screen renders exactly the
    # span the model asked for. Nothing is hidden: the scale is published in
    # pool_created, shown as a badge, and the model is told in this tool's own result.
    #
    # Deriving the scale rather than fixing it at 2880 also keeps it honest for any
    # horizon — a model that asks for two minutes gets a scale of 1 and no compression.
    asked_horizon = int(horizon)
    real_horizon = (min(asked_horizon, settings.demo_horizon_s) if pool["demo"]
                    else asked_horizon)
    scale = max(1, round(asked_horizon / real_horizon)) if real_horizon else 1
    deadline_at = clock.now() + real_horizon
    await store.apply_plan(ctx.pool_id, shares=shares, recipient_cid=recipient_cid)
    await store.set_plan(ctx.pool_id,
                         success_condition_restated=args["success_condition_restated"],
                         recipient_cid=recipient_cid, target_cents=int(target),
                         deadline_at=deadline_at, clock_scale=scale,
                         deadline_label=args.get("deadline_label", ""), overage=overage)
    await store.advance(ctx.pool_id, "DRAFT", "COLLECTING")

    await ctx.emit("ledger_init", {
        "rows": [{"cid": cid, "name": by_cid[cid]["name"],
                  "email_masked": mask_email(by_cid[cid]["email"]),
                  "share_cents": amt, "status": "pending"} for cid, amt in shares.items()],
        "target_cents": int(target), "recipient_name": by_cid[recipient_cid]["name"],
        "policy_text": pool["policy_text"], "clock_scale": scale,
        "deadline_at": deadline_at, "deadline_label": args.get("deadline_label", ""),
        "success_condition_restated": args["success_condition_restated"]})
    await ctx.emit("phase", {"from": "DRAFT", "to": "COLLECTING", "reason": "planned"})

    left = clock.time_left_s(deadline_at)
    out = {"pool_id": ctx.pool_id, "n": len(shares), "sum_shares_cents": sum(shares.values()),
           "target_cents": int(target), "slack_cents": sum(shares.values()) - int(target),
           "time_left_s": left, "time_left_label": clock.display_label(left, scale),
           "clock_scale": scale,
           "checks_passed": ["cids_in_cast", "split_covers_target", "caps_ok"]}
    if scale > 1:
        # Told plainly, because the model must keep sleeping in REAL seconds while
        # talking to humans in the span they asked for.
        out["note"] = (f"This is a demo pool, so the {asked_horizon}s you asked for runs in "
                       f"{real_horizon}s of real time and the countdown on screen renders it "
                       f"as {clock.display_label(left, scale)}. Everything you are told about "
                       f"time, and every sleep you request, is in REAL seconds — you have "
                       f"{left} of them. Quote the long span to people; reason in the short one.")
    return out


async def _send_invoices(ctx, args, pool):
    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}
    invs = {i["cid"]: i for i in await store.get_invoices(ctx.pool_id)}
    want = args.get("cids") or []
    if want in ("all", ["all"]):
        want = list(invs)
    notes = {n["cid"]: n.get("note", "") for n in (args.get("notes") or [])}
    label = args.get("item_label") or "Contribution"
    sent, skipped, failed = [], [], []

    async def one(cid, idx):
        inv = invs.get(cid)
        if inv is None:
            skipped.append({"cid": cid, "reason": "UNKNOWN_CID"})
            return
        if inv.get("invoice_id"):
            skipped.append({"cid": cid, "reason": "ALREADY_INVOICED"})
            return
        person = people[cid]
        await asyncio.sleep(0.18 * idx)      # stagger: the UI reads as a cascade, not a blink
        async with ctx._sem:
            try:
                iid = await ctx.pp.create_invoice(person["email"], int(person["share_cents"]),
                                                  note=notes.get(cid, ""), item_name=label)
                url = await ctx.pp.send_invoice(iid)
            except Exception as e:           # one failure must not sink the batch
                failed.append({"cid": cid, "error": str(e)[:120]})
                return
        await store.set_invoice_sent(ctx.pool_id, cid, iid, url)
        sent.append({"cid": cid, "name": person["name"]})
        await ctx.emit("ledger_row", {"cid": cid, "name": person["name"],
                                      "email_masked": mask_email(person["email"]),
                                      "share_cents": int(person["share_cents"]),
                                      "status": "SENT", "paid_cents": 0,
                                      "invoice_id": iid, "payer_url": url})

    await asyncio.gather(*[one(cid, i) for i, cid in enumerate(want)])
    await store.bump_ledger_version(ctx.pool_id)
    rows = await store.get_invoices(ctx.pool_id)
    return {"sent": sent, "skipped": skipped, "failed": failed,
            "open_count": sum(1 for i in rows
                              if i["status"] != "CANCELLED" and int(i["paid_cents"]) == 0)}


async def _check_payments(ctx, args, pool):
    totals = await refresh_from_paypal(ctx)
    await ctx.emit("pool_totals", totals)
    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}
    rows = await store.get_invoices(ctx.pool_id)
    paid, open_ = [], []
    for i in rows:
        who = people.get(i["cid"], {})
        if int(i["paid_cents"]) > 0:
            paid.append({"cid": i["cid"], "name": who.get("name"),
                         "paid_cents": int(i["paid_cents"]), "status": i["status"],
                         "source": i["source"]})
        elif i["status"] != "CANCELLED":
            open_.append({"cid": i["cid"], "name": who.get("name"),
                          "share_cents": int(who.get("share_cents") or 0),
                          "status": i["status"], "reminders": int(i["reminders"])})
    pct = int(100 * totals["collected_cents"] / max(1, totals["target_cents"]))
    return {**totals, "pct": pct, "paid": paid, "open": open_}


async def _remind_contributors(ctx, args, pool):
    invs = {i["cid"]: i for i in await store.get_invoices(ctx.pool_id)}
    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}
    reminded, blocked = [], []
    for r in (args.get("reminders") or []):
        cid, note = r.get("cid"), (r.get("note") or "").strip()
        inv = invs.get(cid)
        if inv is None:
            blocked.append({"cid": cid, "reason": "UNKNOWN_CID"})
            continue
        try:
            guards.check_remindable({**inv, "cid": cid}, clock.now(), int(pool["clock_scale"]))
        except GuardError as e:
            blocked.append({"cid": cid, "reason": e.code})
            await ctx.emit("guard_block", {"tool": "remind_contributors", "rule": e.code,
                                           "detail": e.detail, "cid": cid})
            continue
        try:
            await ctx.pp.remind_invoice(inv["invoice_id"], note=note)
        except Exception as e:
            blocked.append({"cid": cid, "reason": "SEND_FAILED"})
            continue
        await store.bump_reminder(ctx.pool_id, cid)
        reminded.append(cid)
        # The count travels with the event so a client can SET it. Incrementing on the
        # client double-counts the moment a replay re-delivers the frame, which is every
        # time anyone refreshes.
        await ctx.emit("reminder_sent", {
            "cid": cid, "name": people.get(cid, {}).get("name"), "note": note,
            "reminders": int(invs[cid]["reminders"]) + 1,
            "max_reminders": settings.max_reminders})
    return {"reminded": reminded, "blocked": blocked}


async def _drop_contributor(ctx, args, pool):
    cid = args.get("cid")
    invs = {i["cid"]: i for i in await store.get_invoices(ctx.pool_id)}
    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}
    inv = invs.get(cid)
    if inv is None:
        raise GuardError("UNKNOWN_CID", f"{cid!r} is not in this pool")
    guards.check_cancellable({**inv, "cid": cid})
    if inv.get("invoice_id"):
        await ctx.pp.cancel_invoice(inv["invoice_id"],
                                    note=(args.get("reason") or "")[:200] or
                                    "You have been removed from this pool — nothing is owed.")
    await store.set_invoice_state(ctx.pool_id, cid, status="CANCELLED",
                                  paid_cents=int(inv["paid_cents"]), source=inv["source"])
    await store.bump_ledger_version(ctx.pool_id)
    person = people.get(cid, {})
    await ctx.emit("ledger_row", {"cid": cid, "name": person.get("name"),
                                  "email_masked": mask_email(person.get("email", "")),
                                  "status": "CANCELLED", "paid_cents": int(inv["paid_cents"])})
    totals = await ledger_snapshot(ctx)
    still_possible = sum(int(p["share_cents"]) for p in people.values()
                         if not p["is_recipient"] and invs.get(p["cid"], {}).get("status") != "CANCELLED"
                         and p["cid"] != cid)
    return {"cancelled": cid, "name": person.get("name"),
            "shortfall_cents": max(0, totals["target_cents"] - still_possible),
            "still_feasible": still_possible >= totals["target_cents"], **totals}


async def _sleep_until(ctx, args, pool):
    left = clock.time_left_s(pool["deadline_at"])
    want = int(args.get("sleep_s") or 0)
    sleep_s = max(settings.min_sleep_s, min(want, max(settings.min_sleep_s, left)))
    wake_on = args.get("wake_on") or ["payment", "deadline"]
    if "deadline" not in wake_on:
        wake_on = list(wake_on) + ["deadline"]     # always armed: a pool must never miss its own deadline
    await ctx.emit("agent_sleep", {"episode_id": ctx.episode_id, "sleep_s": sleep_s,
                                   "reason": args.get("reason", ""), "wake_on": wake_on,
                                   "until_label": clock.display_label(sleep_s, int(pool["clock_scale"]))})
    return {SLEEP_SENTINEL: True, "sleep_s": sleep_s, "wake_on": wake_on,
            "requested_s": want, "clamped": sleep_s != want}


async def execute_settlement(ctx, pool, *, decision: str, reason: str, policy_quote: str,
                             decided_by: str, attest: dict | None = None) -> dict:
    """THE ONE MONEY-OUT PATH. Both the agent's `resolve_pool` and a human's approved
    refund come through here, so there is exactly one function to audit.

    Ordering matters more than anything else in this file: EVERY check runs before ANY
    state changes. An earlier version took the phase lock first, so a settlement refused
    for a bad attestation still left the pool stranded in RESOLVING — unable to collect,
    with no settlement to show. A refused settlement must be a no-op.

    Note what is NOT a parameter: an amount, or a destination. Those are computed from
    PayPal-verified payments and the frozen roster. A caller — model or human — supplies a
    branch and words, and that is the whole action space.
    """
    if decision not in ("RELEASE", "UNWIND"):
        raise GuardError("BAD_ARGS", "decision must be RELEASE or UNWIND")

    # 1. Freeze the truth from PayPal itself, never from the ledger cache.
    truth = await refresh_from_paypal(ctx, origin="settle", include_terminal=True)

    # 2. Validate everything. Any failure below leaves the pool exactly as it was.
    if attest is not None:
        guards.check_attestation(attest, truth)

    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}
    invoices = await store.get_invoices(ctx.pool_id)
    payers = [{"cid": i["cid"], "name": people[i["cid"]]["name"],
               "email": people[i["cid"]]["email"], "paid_cents": int(i["paid_cents"])}
              for i in invoices if int(i["paid_cents"]) > 0]
    recipient = next((c for c in people.values() if c["is_recipient"]), None)
    if recipient is None:
        raise GuardError("BAD_ARGS", "this pool has no recipient")

    frozen = truth["collected_cents"]
    items = guards.build_settlement_items(
        decision=decision, payers=payers, recipient=dict(recipient),
        frozen_collected_cents=frozen, target_cents=truth["target_cents"],
        overage=pool["overage"])
    guards.assert_settlement_valid(
        items=items, frozen_collected_cents=frozen,
        allowed_emails=guards.allowed_payout_emails(dict(recipient), payers),
        payers=payers, decision=decision,
        payouts_today_cents=await store.payouts_today_cents())

    # 3. Only now take the settle lock. A concurrent caller loses here, cleanly.
    phase = pool["phase"]
    if phase == "COLLECTING":
        if not await store.advance(ctx.pool_id, "COLLECTING", "RESOLVING"):
            existing = await store.get_settlement(ctx.pool_id)
            raise GuardError("ALREADY_SETTLED", "this pool is already being settled",
                             batch_id=(existing or {}).get("batch_id"))
        # Announce it. Only the later move to SETTLING used to be published, so a viewer
        # watching live saw the stepper sitting on "Collecting" while the decision card
        # said UNWIND — the one moment the phase display most needs to be right.
        await ctx.emit("phase", {"from": "COLLECTING", "to": "RESOLVING",
                                 "reason": "deciding"})
    lock_phase = "RESOLVING" if phase in ("COLLECTING", "RESOLVING") else phase

    # 4. Write-ahead the settlement BEFORE any HTTP money call, so a crash mid-flight
    #    cannot turn into a second payout on retry.
    sbid = guards.sender_batch_id(ctx.pool_id, decision)
    if not await store.insert_settlement(ctx.pool_id, decision=decision, reason=reason,
                                         policy_quote=policy_quote,
                                         frozen_collected_cents=frozen, sender_batch_id=sbid,
                                         items=items, decided_by=decided_by):
        existing = await store.get_settlement(ctx.pool_id)
        raise GuardError("ALREADY_SETTLED", "a settlement already exists for this pool",
                         batch_id=(existing or {}).get("batch_id"))

    # 5. The decision is committed — it lands on screen alone, before any money moves.
    await ctx.emit("decision", {
        "decision": decision, "reason": reason, "policy_quote": policy_quote,
        "success_condition_restated": pool["success_condition_restated"],
        "collected_cents": frozen, "target_cents": truth["target_cents"],
        "paid_count": truth["paid_count"], "n": truth["n"],
        "attested": attest is not None, "by": decided_by})
    if attest is not None:
        await ctx.emit("fact_check", {"matched": True, "model_said": attest,
                                      "paypal_says": {k: truth[k] for k in
                                                      ("collected_cents", "paid_count",
                                                       "ledger_version")}})

    # 6. Freeze then pay: close the unpaid invoices BEFORE money leaves, so a late payer
    #    cannot pay into a pool that is already being refunded.
    for i in invoices:
        if int(i["paid_cents"]) == 0 and i["status"] != "CANCELLED" and i.get("invoice_id"):
            try:
                await ctx.pp.cancel_invoice(i["invoice_id"], origin="settle")
                await store.set_invoice_state(ctx.pool_id, i["cid"], status="CANCELLED",
                                              paid_cents=0, source=None)
                await ctx.emit("invoice_cancelled", {"cid": i["cid"],
                                                     "name": people[i["cid"]]["name"],
                                                     "invoice_id": i["invoice_id"]})
            except Exception:  # a stubborn invoice must not block the refunds
                pass

    # 7. Record what each payer had paid at the moment of the freeze. Anything that
    #    arrives after this is a LATE payment, and the ticker returns it with no model
    #    involved — handing back money that should never have arrived is arithmetic.
    await store.freeze_paid_snapshot(ctx.pool_id)

    await store.advance(ctx.pool_id, lock_phase, "SETTLING")
    await ctx.emit("phase", {"from": lock_phase, "to": "SETTLING", "reason": decision})

    batch = await ctx.pp.create_payout(
        sbid, [{"email": i["email"], "amount_cents": i["amount_cents"],
                "note": f"Potluck {decision.lower()}", "sender_item_id": i["cid"]} for i in items],
        subject="Potluck", origin="settle")
    batch_id = batch.get("payout_batch_id", "")
    await store.set_settlement_batch(ctx.pool_id, batch_id, batch.get("batch_status", "PENDING"))
    await ctx.emit("payout_submitted", {
        "batch_id": batch_id, "sender_batch_id": sbid, "kind": decision.lower(),
        "item_count": len(items), "total_cents": sum(i["amount_cents"] for i in items),
        "items": [{"cid": i["cid"], "name": i["name"], "email_masked": mask_email(i["email"]),
                   "amount_cents": i["amount_cents"]} for i in items],
        "batch_status": batch.get("batch_status", "PENDING"), "by": decided_by})
    return {"batch_id": batch_id, "sender_batch_id": sbid, "kind": decision.lower(),
            "item_count": len(items), "total_cents": sum(i["amount_cents"] for i in items),
            "batch_status": batch.get("batch_status", "PENDING"),
            "invariants_checked": ["conservation", "exact_refunds", "allowlist", "settle_once"]}


async def _resolve_pool(ctx, args, pool):
    """The model's branch. It supplies a decision, its reasoning, the organizer's words
    that justify it, and an attestation of the numbers it decided on — and nothing else.
    Read the signature of execute_settlement for what it cannot supply."""
    return await execute_settlement(
        ctx, pool, decision=args.get("decision"), reason=args.get("reason", ""),
        policy_quote=args.get("policy_quote", ""), decided_by="agent",
        attest=args.get("attest") or {})




async def poll_payout(ctx, args, pool, *, origin: str = "settle",
                      only_changes: bool = False):
    """Read the batch back from PayPal and stream the per-item cascade.

    Public and shared: the model reaches it as `check_payout`, and the ticker calls it
    directly to reconcile a PENDING batch after a restart — by GET, never by re-POST.
    One function means the numbers a judge sees are the same either way, and a crash
    mid-payout can never become a second payout.
    """
    s = await store.get_settlement(ctx.pool_id)
    if not s or not s.get("batch_id"):
        return {"error": "NO_BATCH", "detail": "nothing has been paid out yet"}
    body = await ctx.pp.get_payout(s["batch_id"], origin=origin)
    header = body.get("batch_header") or {}
    items = body.get("items") or []
    status = header.get("batch_status", "PENDING")
    # Join back to the write-ahead item list: PayPal answers by receiver address, the
    # ledger grid needs a cid to flash the right row.
    planned = {i["email"]: i for i in (s.get("items") or [])}
    was = s.get("item_statuses") or {}
    counts = {"SUCCESS": 0, "PENDING": 0, "UNCLAIMED": 0, "FAILED": 0}
    now: dict[str, str] = {}
    changed = []
    for it in items:
        st = it.get("transaction_status", "PENDING")
        counts[st] = counts.get(st, 0) + 1
        pi = it.get("payout_item") or {}
        who = planned.get(pi.get("receiver", ""), {})
        key = who.get("cid") or pi.get("receiver", "")
        now[key] = st
        if was.get(key) != st:
            changed.append(key)
        # The ticker polls this every couple of seconds for half a minute. Re-sending an
        # identical PENDING frame ten times is noise on the wire and in the log, and the
        # cascade is supposed to be legible: one frame per item per actual transition.
        if not only_changes or key in changed:
            await ctx.emit("payout_item", {
                "batch_id": s["batch_id"], "cid": who.get("cid"), "name": who.get("name"),
                "email_masked": mask_email(pi.get("receiver", "")),
                "amount_cents": who.get("amount_cents") or str_to_cents(
                    (pi.get("amount") or {}).get("value", 0)),
                "item_status": st, "transaction_id": it.get("transaction_id")})
    await store.set_settlement_batch(ctx.pool_id, s["batch_id"], status, now)
    done = guards.batch_is_done({**s, "item_statuses": now, "batch_status": status})
    if changed or not only_changes:
        await ctx.emit("payout_settled", {
            "batch_id": s["batch_id"], "batch_status": status, "items_done": done,
            "succeeded": counts.get("SUCCESS", 0), "pending": counts.get("PENDING", 0),
            "unclaimed": counts.get("UNCLAIMED", 0), "failed": counts.get("FAILED", 0),
            "total_cents": s["frozen_collected_cents"]})
    return {"batch_status": status, "items_done": done, **counts}


async def build_receipt(ctx: ToolContext, pool: dict) -> dict:
    """The downloadable proof that no money was created, destroyed, or sent off-list.

    inflow is what PayPal confirmed arrived; outflow is the sum of the items that were
    written ahead of the batch. delta must be zero, or be a retained overage the
    organizer explicitly asked for — and it says which, rather than leaving a reader to
    work it out. The hash covers the ledger rows, so two runs of the same pool are
    comparable and a doctored screenshot is not.
    """
    s = await store.get_settlement(ctx.pool_id) or {}
    invoices = await store.get_invoices(ctx.pool_id)
    people = {c["cid"]: c for c in await store.get_contributors(ctx.pool_id)}
    inflow = int(s.get("frozen_collected_cents") or
                 sum(int(i["paid_cents"]) for i in invoices))
    items = s.get("items") or []
    outflow = sum(int(i["amount_cents"]) for i in items)
    rows = sorted((i["cid"], i["status"], int(i["paid_cents"]), i.get("invoice_id") or "")
                  for i in invoices)
    ledger_hash = hashlib.sha256(
        json.dumps([ctx.pool_id, inflow, outflow, rows], sort_keys=True).encode()).hexdigest()
    delta = inflow - outflow
    return {
        "inflow_cents": inflow, "outflow_cents": outflow, "delta_cents": delta,
        "delta_explained": ("balanced to the cent" if delta == 0 else
                            f"{delta}c retained: the organizer chose "
                            f"{pool['overage']} for money above the target"),
        "ledger_hash": ledger_hash,
        "invariants_checked": ["conservation", "exact_refunds", "settle_once",
                               "idempotent_batch_id", "receiver_allowlist",
                               "freeze_before_pay"],
        "decision": s.get("decision"), "decided_by": s.get("decided_by"),
        "item_statuses": s.get("item_statuses") or {},
        "provenance": {
            "invoice_ids": [i["invoice_id"] for i in invoices if i.get("invoice_id")],
            "payer_urls": [i["payer_url"] for i in invoices if i.get("payer_url")],
            "payout_batch_id": s.get("batch_id"), "sender_batch_id": s.get("sender_batch_id"),
            "paypal_env": "sandbox", "mock": settings.use_mock_paypal,
            **await store.pool_provenance(ctx.pool_id)},
        "people": len(people),
    }


async def _close_pool(ctx, args, pool):
    s = await store.get_settlement(ctx.pool_id)
    guards.check_closable(s)
    if not await store.advance(ctx.pool_id, "SETTLING", "CLOSED"):
        return {"error": "WRONG_PHASE", "detail": "this pool is no longer settling"}
    receipt = await build_receipt(ctx, pool)
    await ctx.emit("receipt", receipt)
    await ctx.emit("pool_closed", {"phase": "CLOSED", "summary": (args.get("summary") or "")[:600],
                                   "decision": (s or {}).get("decision"),
                                   "batch_id": (s or {}).get("batch_id"), "by": "agent"})
    return {"phase": "CLOSED", "delta_cents": receipt["delta_cents"],
            "ledger_hash": receipt["ledger_hash"][:16]}


_HANDLERS = {
    "plan_pool": _plan_pool,
    "send_invoices": _send_invoices,
    "check_payments": _check_payments,
    "remind_contributors": _remind_contributors,
    "drop_contributor": _drop_contributor,
    "sleep_until": _sleep_until,
    "resolve_pool": _resolve_pool,
    "check_payout": poll_payout,
    "close_pool": _close_pool,
}


async def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """The one entry point. Returns a dict — always. Never raises."""
    try:
        pool = await store.get_pool(ctx.pool_id)
        if not pool:
            return {"error": "UNKNOWN_POOL", "detail": ctx.pool_id}
        guards.check_phase(name, pool["phase"])
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "UNKNOWN_TOOL", "detail": name}
        return await handler(ctx, args, pool)
    except GuardError as e:
        await ctx.emit("guard_block", {"tool": name, "rule": e.code, "detail": e.detail,
                                       "episode_id": ctx.episode_id, **e.extra})
        return e.as_dict()
    except Exception as e:  # noqa: BLE001 — an upstream failure is data, not a crash
        await ctx.emit("error", {"where": name, "message": str(e)[:200], "recoverable": True})
        return {"error": "TOOL_FAILED", "detail": f"{type(e).__name__}: {str(e)[:160]}"}

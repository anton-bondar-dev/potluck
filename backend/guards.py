"""Guards — pure predicates that constrain the MONEY, never the judgement.

The design rule this file exists to enforce: inside the action space the agent is given,
every choice it can make is harmless. It supplies a decision; code supplies every amount
and every destination. So "the model hallucinated a $4,500 payout to attacker@evil.com"
is not a scenario we check for — it is unrepresentable in the tool signatures.

No I/O lives here. Everything takes plain dicts so the whole table can be tested
adversarially with no network, no database and no model.

Invariants:
  G1 conservation   — payout items sum to exactly the frozen collected amount
  G2 exact refunds  — an UNWIND item equals that payer's PayPal-verified paid amount
  G3 settle-once    — enforced by store (UNIQUE + rowcount-guarded phase) and re-asserted
  G5 reminder limit — <= N per contributor, cooldown, only on a remindable status
  G6 allowlist      — destinations may only be the frozen recipient or a paid contributor
  G8 phase gating   — every tool declares the phases it is legal in
  G9 caps           — roster size, share size, pool size, item count, daily ceiling
  G10 one pre-flight — assert_settlement_valid() is the single audit point for both branches
"""
from __future__ import annotations

from config import settings

REMINDABLE_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID"}
CANCELLABLE_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "pending"}
PAID_STATUSES = {"PAID", "MARKED_AS_PAID"}

# G8: the phases each tool may legally run in. The menu already hides illegal tools;
# this is the second, independent assertion (defence in depth).
TOOL_PHASES: dict[str, set[str]] = {
    "plan_pool": {"DRAFT"},
    "send_invoices": {"COLLECTING"},
    "check_payments": {"COLLECTING", "RESOLVING"},
    "remind_contributors": {"COLLECTING"},
    "drop_contributor": {"COLLECTING"},
    "sleep_until": {"COLLECTING"},
    "resolve_pool": {"COLLECTING", "RESOLVING"},
    "check_payout": {"SETTLING"},
    "close_pool": {"SETTLING"},
}


class GuardError(Exception):
    """Raised by a guard; the dispatcher turns it into a structured tool result the
    model can read and retry against. It never escapes to the client as a 500."""

    def __init__(self, code: str, detail: str, **extra):
        self.code = code
        self.detail = detail
        self.extra = extra
        super().__init__(f"{code}: {detail}")

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail, **self.extra}


# --- G8 ---------------------------------------------------------------------
def check_phase(tool: str, phase: str) -> None:
    legal = TOOL_PHASES.get(tool)
    if legal is None:
        raise GuardError("UNKNOWN_TOOL", f"{tool} is not a tool")
    if phase not in legal:
        raise GuardError("WRONG_PHASE",
                         f"{tool} is only legal in {sorted(legal)}, pool is {phase}",
                         phase=phase)


# --- G9 + plan validation ---------------------------------------------------
def check_plan(contributors: list[dict], target_cents: int, horizon_s: int,
               cast_cids: set[str], recipient_cid: str) -> None:
    n = len(contributors)
    if n == 0:
        raise GuardError("BAD_ARGS", "a pool needs at least one contributor")
    if n > settings.max_contributors:
        raise GuardError("CAP_EXCEEDED",
                         f"{n} contributors exceeds the cap of {settings.max_contributors}")
    seen = set()
    total = 0
    for c in contributors:
        cid = c.get("cid")
        if cid not in cast_cids:
            raise GuardError("UNKNOWN_CID", f"{cid!r} is not in this pool's cast",
                             known=sorted(cast_cids))
        if cid in seen:
            raise GuardError("DUPLICATE_CID", f"{cid!r} appears twice")
        seen.add(cid)
        share = c.get("share_cents")
        if not isinstance(share, int) or isinstance(share, bool):
            raise GuardError("SHARE_OUT_OF_RANGE", f"share for {cid} must be integer cents")
        if share < 100 or share > settings.max_share_cents:
            raise GuardError("SHARE_OUT_OF_RANGE",
                             f"share for {cid} is {share}c; allowed 100..{settings.max_share_cents}")
        total += share
    if recipient_cid not in cast_cids:
        raise GuardError("UNKNOWN_CID", f"recipient {recipient_cid!r} is not in the cast")
    if recipient_cid in seen:
        raise GuardError("DUPLICATE_CID", "the recipient cannot also be a contributor")
    if not isinstance(target_cents, int) or target_cents <= 0:
        raise GuardError("TARGET_OUT_OF_RANGE", "target must be positive integer cents")
    if target_cents > settings.max_pool_cents:
        raise GuardError("TARGET_OUT_OF_RANGE",
                         f"target {target_cents}c exceeds the cap of {settings.max_pool_cents}c")
    if total < target_cents:
        raise GuardError("SPLIT_MISMATCH",
                         f"shares total {total}c but the target is {target_cents}c "
                         f"(short by {target_cents - total}c)",
                         shares_total_cents=total, target_cents=target_cents,
                         delta_cents=target_cents - total)
    if not isinstance(horizon_s, int) or horizon_s < 20 or horizon_s > 30 * 86400:
        raise GuardError("BAD_HORIZON", "horizon must be 20s..30d, in seconds")


# --- G5 ---------------------------------------------------------------------
def check_remindable(invoice: dict, now: float, clock_scale: int = 1) -> None:
    status = invoice.get("status")
    if status in PAID_STATUSES:
        raise GuardError("ALREADY_PAID", f"{invoice.get('cid')} has already paid")
    if status not in REMINDABLE_STATUSES:
        raise GuardError("NOT_REMINDABLE", f"invoice status {status} cannot be reminded")
    if int(invoice.get("reminders") or 0) >= settings.max_reminders:
        raise GuardError("LIMIT_3",
                         f"{invoice.get('cid')} has had {settings.max_reminders} reminders")
    last = invoice.get("last_reminder_at")
    if last:
        cooldown = settings.remind_cooldown_s / max(1, clock_scale)
        if now - float(last) < cooldown:
            raise GuardError("COOLDOWN",
                             f"last reminder was {int(now - float(last))}s ago; "
                             f"cooldown is {int(cooldown)}s")


def check_cancellable(invoice: dict) -> None:
    status = invoice.get("status")
    if status in PAID_STATUSES:
        raise GuardError("ALREADY_PAID", "a paid invoice cannot be cancelled here")
    if status not in CANCELLABLE_STATUSES:
        raise GuardError("NOT_CANCELLABLE", f"invoice status {status} cannot be cancelled")


# --- attestation ------------------------------------------------------------
def check_attestation(attest: dict, truth: dict) -> None:
    """The model must prove it read real tool results before money moves. Code stays the
    source of truth; this only refuses to act on a stale or invented view."""
    for key in ("collected_cents", "paid_count", "ledger_version"):
        if key not in attest:
            raise GuardError("BAD_ARGS", f"attest.{key} is required")
        if int(attest[key]) != int(truth[key]):
            raise GuardError("LEDGER_MISMATCH",
                             f"attested {key}={attest[key]} but the ledger says {truth[key]}",
                             truth=truth)


# --- the money: code builds every item ---------------------------------------
def _prorata(amount_cents: int, weights: list[tuple[str, int]]) -> dict[str, int]:
    """Split amount_cents across weights so the parts sum EXACTLY to the whole.

    Largest-remainder: floor each share, then hand the leftover cents out one at a time
    to the largest remainders. A naive round() here loses or invents cents, which is the
    exact failure the product's "to the cent" promise cannot survive.
    """
    total_w = sum(w for _, w in weights)
    if total_w <= 0 or amount_cents <= 0:
        return {cid: 0 for cid, _ in weights}
    base, rema = {}, []
    for cid, w in weights:
        exact = amount_cents * w
        share = exact // total_w
        base[cid] = share
        rema.append((exact - share * total_w, cid))
    leftover = amount_cents - sum(base.values())
    for _, cid in sorted(rema, reverse=True)[:leftover]:
        base[cid] += 1
    return base


def build_settlement_items(*, decision: str, payers: list[dict], recipient: dict,
                           frozen_collected_cents: int, target_cents: int,
                           overage: str = "refund_prorata") -> list[dict]:
    """Code — not the model — decides every amount and every destination.

    payers: [{cid, name, email, paid_cents}] with PayPal-verified amounts.
    Returns [{cid, name, email, amount_cents, kind}] summing to exactly the frozen total.
    """
    if decision not in ("RELEASE", "UNWIND"):
        raise GuardError("BAD_ARGS", f"decision must be RELEASE or UNWIND, got {decision!r}")
    if frozen_collected_cents < 0:
        raise GuardError("BAD_AMOUNT", f"collected cannot be negative: {frozen_collected_cents}")
    if frozen_collected_cents == 0:
        # Nothing arrived, so there is nothing to move — and that is an ENDING, not an
        # error. Refusing here used to leave the agent with no legal way to finish: it
        # decided correctly, was blocked, ran out of turns and the pool escalated. The
        # single most likely path through the public demo (create a pool and just watch)
        # dead-ended in an amber banner, and a judge never saw a decision at all.
        return []

    if decision == "UNWIND":
        # G2: everyone gets back exactly what PayPal says they paid. Not a judgement call.
        items = [{"cid": p["cid"], "name": p["name"], "email": p["email"],
                  "amount_cents": int(p["paid_cents"]), "kind": "refund"}
                 for p in payers if int(p["paid_cents"]) > 0]
    elif overage == "keep_to_target" or frozen_collected_cents <= target_cents:
        items = [{"cid": recipient["cid"], "name": recipient["name"], "email": recipient["email"],
                  "amount_cents": frozen_collected_cents, "kind": "release"}]
    else:
        # Overfunded and the organizer asked for the excess back: recipient gets the
        # target, the surplus returns pro-rata to payers by what each actually paid.
        surplus = frozen_collected_cents - target_cents
        weights = [(p["cid"], int(p["paid_cents"])) for p in payers if int(p["paid_cents"]) > 0]
        back = _prorata(surplus, weights)
        items = [{"cid": recipient["cid"], "name": recipient["name"], "email": recipient["email"],
                  "amount_cents": target_cents, "kind": "release"}]
        by_cid = {p["cid"]: p for p in payers}
        for cid, amt in back.items():
            if amt > 0:
                p = by_cid[cid]
                items.append({"cid": cid, "name": p["name"], "email": p["email"],
                              "amount_cents": amt, "kind": "overage_refund"})
    return items


def assert_settlement_valid(*, items: list[dict], frozen_collected_cents: int,
                            allowed_emails: set[str], payers: list[dict],
                            decision: str, payouts_today_cents: int = 0) -> None:
    """G10 — the single pre-flight both branches pass through. One function to audit."""
    if not items:
        # No items is legitimate only when nothing was collected. Then there is nothing
        # to check, because there is nothing to move.
        if frozen_collected_cents == 0:
            return
        raise GuardError("NOTHING_COLLECTED",
                         f"settlement produced no items for {frozen_collected_cents}c")
    if len(items) > settings.max_payout_items:
        raise GuardError("CAP_EXCEEDED",
                         f"{len(items)} payout items exceeds the cap of {settings.max_payout_items}")
    total = 0
    seen = set()
    paid_by_cid = {p["cid"]: int(p["paid_cents"]) for p in payers}
    for it in items:
        amt = it.get("amount_cents")
        if not isinstance(amt, int) or isinstance(amt, bool):
            raise GuardError("BAD_ARGS", f"amount for {it.get('cid')} must be integer cents")
        if amt <= 0:
            raise GuardError("BAD_ARGS", f"amount for {it.get('cid')} must be positive, got {amt}")
        # G6: destinations are structurally restricted to the frozen allowlist.
        if it.get("email") not in allowed_emails:
            raise GuardError("RECEIVER_NOT_ALLOWLISTED",
                             f"{it.get('email')!r} is not a payable destination for this pool")
        key = (it.get("cid"), it.get("kind"))
        if key in seen:
            raise GuardError("BAD_ARGS", f"duplicate payout item for {it.get('cid')}")
        seen.add(key)
        # G2: a refund is exactly what that person paid — never more.
        if decision == "UNWIND" and it.get("kind") == "refund":
            if amt != paid_by_cid.get(it.get("cid")):
                raise GuardError("BAD_ARGS",
                                 f"refund for {it.get('cid')} is {amt}c but they paid "
                                 f"{paid_by_cid.get(it.get('cid'))}c")
        total += amt
    # G1: nothing is created and nothing is destroyed.
    if total != frozen_collected_cents:
        raise GuardError("BAD_ARGS",
                         f"items total {total}c but {frozen_collected_cents}c was collected",
                         total_cents=total, frozen_collected_cents=frozen_collected_cents)
    # G9: daily ceiling across all pools.
    if payouts_today_cents + total > settings.daily_payout_cents:
        raise GuardError("CAP_EXCEEDED",
                         f"this settlement would exceed today's payout ceiling "
                         f"({settings.daily_payout_cents}c)")


# PayPal's own terminal batch states. UNCLAIMED/FAILED items can sit INSIDE a SUCCESS
# batch; that is reported per item on the receipt rather than hidden.
TERMINAL_BATCH_STATUSES = {"SUCCESS", "DENIED", "CANCELED", "CANCELLED"}

# Per-item states PayPal will not move off on its own. UNCLAIMED is in here on purpose:
# an item sent to an address with no PayPal account sits unclaimed for thirty days, and
# waiting that out is not an option — so it is terminal for the purpose of closing, and
# reported as its own count so nothing ever claims the refund completed.
TERMINAL_ITEM_STATUSES = {"SUCCESS", "FAILED", "BLOCKED", "RETURNED", "REFUNDED",
                          "REVERSED", "DENIED", "UNCLAIMED"}


def settlement_moves_money(settlement: dict | None) -> bool:
    """Whether this settlement has anything to pay out.

    An ABSENT item list means unknown, not empty — only an explicitly empty one means
    nobody is owed anything. Collapsing the two would make a batch look finished merely
    because a caller did not pass its items.
    """
    items = (settlement or {}).get("items")
    return items is None or bool(items)


def batch_is_done(settlement: dict | None) -> bool:
    """Has PayPal finished with this batch?

    Judged on the ITEMS, because the batch header lags behind them — a live batch
    reported PROCESSING long after both of its items had reported SUCCESS.
    """
    if not settlement:
        return False
    if not settlement_moves_money(settlement):
        return True          # a settlement with no items has nothing to wait for
    items = settlement.get("item_statuses") or {}
    if items:
        return all(v in TERMINAL_ITEM_STATUSES for v in items.values())
    return (settlement.get("batch_status") or "PENDING") in TERMINAL_BATCH_STATUSES


def check_closable(settlement: dict | None) -> None:
    """A pool may not be closed while the payout is still in flight.

    The live rehearsal closed a pool with its batch at PENDING, because the model was
    offered close_pool and took it. On screen that reads as "done" before PayPal has said
    so, which is the one claim this project cannot afford to get wrong. So the order is
    enforced rather than suggested: poll until terminal, then close.
    """
    if not settlement:
        raise GuardError("NOT_SETTLED", "nothing has been settled, so there is nothing to close")
    status = settlement.get("batch_status") or "PENDING"
    if not batch_is_done(settlement):
        raise GuardError("PAYOUT_NOT_TERMINAL",
                         f"the payout batch is still {status}, which takes PayPal around "
                         f"30 seconds. Call sleep_until to wait — you will be woken when the "
                         f"batch reaches a terminal state — then check_payout and close. Do "
                         f"not tell anyone the money has arrived before it has.",
                         batch_status=status)


def check_late_refund(*, amount_cents: int, to_email: str, payer_email: str,
                      payouts_today_cents: int) -> None:
    """A payment that arrived after the pool was frozen goes straight back.

    There is no judgement in this and so no agency: the amount is the difference between
    what PayPal says they paid and what was frozen for them, and the destination is their
    own address. Both are facts. The guard exists so that the one code path that moves
    money without a decision still cannot move the wrong amount or move it anywhere else.
    """
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise GuardError("BAD_AMOUNT", f"a late refund must be a positive integer number of "
                                       f"cents, got {amount_cents!r}")
    if amount_cents > settings.max_share_cents:
        raise GuardError("CAP_EXCEEDED",
                         f"late refund of {amount_cents}c exceeds the per-share ceiling of "
                         f"{settings.max_share_cents}c")
    if to_email != payer_email:
        raise GuardError("RECEIVER_NOT_ALLOWLISTED",
                         "a late payment can only be returned to the person who made it")
    if payouts_today_cents + amount_cents > settings.daily_payout_cents:
        raise GuardError("CAP_EXCEEDED", "this would cross the daily payout ceiling")


def allowed_payout_emails(recipient: dict, payers: list[dict]) -> set[str]:
    """G6 layer two: the only payable destinations are the frozen recipient and the
    contributors PayPal confirms actually paid into THIS pool."""
    out = {recipient["email"]}
    out |= {p["email"] for p in payers if int(p.get("paid_cents") or 0) > 0}
    return out


def sender_batch_id(pool_id: str, decision: str) -> str:
    """G4: deterministic, so a retried settle is deduped by PayPal for 30 days instead
    of paying twice."""
    return f"potluck-{pool_id}-{decision}"

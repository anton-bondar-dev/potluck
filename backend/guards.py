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
    if frozen_collected_cents <= 0:
        raise GuardError("NOTHING_COLLECTED", "no money was collected, so there is none to move")

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
        raise GuardError("NOTHING_COLLECTED", "settlement produced no items")
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

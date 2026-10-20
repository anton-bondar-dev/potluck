"""Adversarial guard table.

Every row is a hostile or malformed payload that MUST be refused, plus the happy paths
that must still pass (a guard layer that only ever says no is useless). No network, no
database, no model — pure predicates, so this runs in milliseconds.

Run: ./.venv/bin/python tests/test_guards.py
"""
import os, random, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import guards  # noqa: E402
from guards import GuardError  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def blocked(name, code, fn):
    """Assert fn() raises GuardError with exactly `code`."""
    try:
        fn()
        check(name, False, f"NOT blocked (expected {code})")
    except GuardError as e:
        check(name, e.code == code, f"got {e.code}, wanted {code}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception {type(e).__name__}: {e}")


def allowed(name, fn):
    try:
        fn()
        check(name, True)
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")


CAST = {"c0", "c1", "c2", "c3", "rec"}
ROSTER = [{"cid": f"c{i}", "share_cents": 4500} for i in range(4)]
REC = {"cid": "rec", "name": "Dana", "email": "dana@ok.com"}
PAYERS = [{"cid": f"c{i}", "name": f"P{i}", "email": f"p{i}@ok.com", "paid_cents": 4500}
          for i in range(4)]
ALLOWED = guards.allowed_payout_emails(REC, PAYERS)

print("--- plan validation ---")
allowed("a sane plan passes",
        lambda: guards.check_plan(ROSTER, 18000, 3600, CAST, "rec"))
blocked("500 contributors", "CAP_EXCEEDED",
        lambda: guards.check_plan([{"cid": "c0", "share_cents": 100}] * 500, 100, 3600, CAST, "rec"))
blocked("a cid that is not in the cast", "UNKNOWN_CID",
        lambda: guards.check_plan([{"cid": "ghost", "share_cents": 4500}], 4500, 3600, CAST, "rec"))
blocked("the same contributor twice", "DUPLICATE_CID",
        lambda: guards.check_plan([{"cid": "c0", "share_cents": 4500}] * 2, 9000, 3600, CAST, "rec"))
blocked("negative share", "SHARE_OUT_OF_RANGE",
        lambda: guards.check_plan([{"cid": "c0", "share_cents": -4500}], 100, 3600, CAST, "rec"))
blocked("zero share", "SHARE_OUT_OF_RANGE",
        lambda: guards.check_plan([{"cid": "c0", "share_cents": 0}], 100, 3600, CAST, "rec"))
blocked("share above the per-person cap", "SHARE_OUT_OF_RANGE",
        lambda: guards.check_plan([{"cid": "c0", "share_cents": 99_999_999}], 100, 3600, CAST, "rec"))
blocked("share as a string, not cents", "SHARE_OUT_OF_RANGE",
        lambda: guards.check_plan([{"cid": "c0", "share_cents": "4,500"}], 4500, 3600, CAST, "rec"))
blocked("shares do not cover the target", "SPLIT_MISMATCH",
        lambda: guards.check_plan(ROSTER, 36000, 3600, CAST, "rec"))
blocked("target above the pool cap", "TARGET_OUT_OF_RANGE",
        lambda: guards.check_plan(ROSTER, 99_999_999, 3600, CAST, "rec"))
blocked("zero target", "TARGET_OUT_OF_RANGE",
        lambda: guards.check_plan(ROSTER, 0, 3600, CAST, "rec"))
blocked("recipient is also a contributor", "DUPLICATE_CID",
        lambda: guards.check_plan(ROSTER + [{"cid": "rec", "share_cents": 4500}], 18000, 3600, CAST, "rec"))
blocked("deadline 2 seconds away", "BAD_HORIZON",
        lambda: guards.check_plan(ROSTER, 18000, 2, CAST, "rec"))
blocked("deadline 10 years away", "BAD_HORIZON",
        lambda: guards.check_plan(ROSTER, 18000, 10 * 365 * 86400, CAST, "rec"))

print("--- reminders and cancellation ---")
now = time.time()
allowed("reminding an unpaid invoice",
        lambda: guards.check_remindable({"cid": "c0", "status": "SENT", "reminders": 0}, now))
blocked("reminding someone who already paid", "ALREADY_PAID",
        lambda: guards.check_remindable({"cid": "c0", "status": "MARKED_AS_PAID", "reminders": 0}, now))
blocked("reminding a cancelled invoice", "NOT_REMINDABLE",
        lambda: guards.check_remindable({"cid": "c0", "status": "CANCELLED", "reminders": 0}, now))
blocked("a 4th reminder", "LIMIT_3",
        lambda: guards.check_remindable({"cid": "c0", "status": "SENT", "reminders": 3}, now))
blocked("reminding again inside the cooldown", "COOLDOWN",
        lambda: guards.check_remindable({"cid": "c0", "status": "SENT", "reminders": 1,
                                         "last_reminder_at": now - 5}, now))
allowed("reminding after the cooldown",
        lambda: guards.check_remindable({"cid": "c0", "status": "SENT", "reminders": 1,
                                         "last_reminder_at": now - 99999}, now))
blocked("cancelling a paid invoice", "ALREADY_PAID",
        lambda: guards.check_cancellable({"cid": "c0", "status": "PAID"}))

print("--- phase gating ---")
allowed("resolve_pool once collecting", lambda: guards.check_phase("resolve_pool", "COLLECTING"))
blocked("resolve_pool while still a draft", "WRONG_PHASE",
        lambda: guards.check_phase("resolve_pool", "DRAFT"))
blocked("send_invoices after settlement", "WRONG_PHASE",
        lambda: guards.check_phase("send_invoices", "CLOSED"))
blocked("a tool that does not exist", "UNKNOWN_TOOL",
        lambda: guards.check_phase("transfer_everything", "COLLECTING"))

print("--- attestation ---")
truth = {"collected_cents": 18000, "paid_count": 4, "ledger_version": 7}
allowed("an honest attestation", lambda: guards.check_attestation(dict(truth), truth))
blocked("attesting a collected amount that never happened", "LEDGER_MISMATCH",
        lambda: guards.check_attestation({**truth, "collected_cents": 99999}, truth))
blocked("attesting a stale ledger version", "LEDGER_MISMATCH",
        lambda: guards.check_attestation({**truth, "ledger_version": 3}, truth))
blocked("attestation missing a field", "BAD_ARGS",
        lambda: guards.check_attestation({"collected_cents": 18000}, truth))

print("--- the money ---")
unwind = guards.build_settlement_items(decision="UNWIND", payers=PAYERS, recipient=REC,
                                       frozen_collected_cents=18000, target_cents=18000)
allowed("a clean UNWIND passes pre-flight",
        lambda: guards.assert_settlement_valid(items=unwind, frozen_collected_cents=18000,
                                               allowed_emails=ALLOWED, payers=PAYERS,
                                               decision="UNWIND"))
check("UNWIND refunds each payer exactly what they paid",
      all(i["amount_cents"] == 4500 for i in unwind) and len(unwind) == 4,
      [i["amount_cents"] for i in unwind])

release = guards.build_settlement_items(decision="RELEASE", payers=PAYERS, recipient=REC,
                                        frozen_collected_cents=18000, target_cents=18000)
check("RELEASE sends one item to the recipient only",
      len(release) == 1 and release[0]["email"] == "dana@ok.com", release)

blocked("paying a destination nobody in this pool owns", "RECEIVER_NOT_ALLOWLISTED",
        lambda: guards.assert_settlement_valid(
            items=[{"cid": "x", "name": "E", "email": "evil@example.com",
                    "amount_cents": 18000, "kind": "release"}],
            frozen_collected_cents=18000, allowed_emails=ALLOWED, payers=PAYERS, decision="RELEASE"))
blocked("paying out more than was collected", "BAD_ARGS",
        lambda: guards.assert_settlement_valid(
            items=[{"cid": "rec", "name": "Dana", "email": "dana@ok.com",
                    "amount_cents": 99_999_999, "kind": "release"}],
            frozen_collected_cents=18000, allowed_emails=ALLOWED, payers=PAYERS, decision="RELEASE"))
blocked("paying out less than was collected (cents vanish)", "BAD_ARGS",
        lambda: guards.assert_settlement_valid(
            items=[{"cid": "rec", "name": "Dana", "email": "dana@ok.com",
                    "amount_cents": 17999, "kind": "release"}],
            frozen_collected_cents=18000, allowed_emails=ALLOWED, payers=PAYERS, decision="RELEASE"))
blocked("a refund larger than that person actually paid", "BAD_ARGS",
        lambda: guards.assert_settlement_valid(
            items=[{**unwind[0], "amount_cents": 9000}] + unwind[1:],
            frozen_collected_cents=18000, allowed_emails=ALLOWED, payers=PAYERS, decision="UNWIND"))
blocked("the same person paid twice in one batch", "BAD_ARGS",
        lambda: guards.assert_settlement_valid(
            items=[unwind[0], unwind[0], unwind[1], unwind[2]],
            frozen_collected_cents=18000, allowed_emails=ALLOWED, payers=PAYERS, decision="UNWIND"))
blocked("a negative amount", "BAD_ARGS",
        lambda: guards.assert_settlement_valid(
            items=[{"cid": "rec", "name": "D", "email": "dana@ok.com",
                    "amount_cents": -18000, "kind": "release"}],
            frozen_collected_cents=-18000, allowed_emails=ALLOWED, payers=PAYERS, decision="RELEASE"))
blocked("an amount that is a string", "BAD_ARGS",
        lambda: guards.assert_settlement_valid(
            items=[{"cid": "rec", "name": "D", "email": "dana@ok.com",
                    "amount_cents": "18000", "kind": "release"}],
            frozen_collected_cents=18000, allowed_emails=ALLOWED, payers=PAYERS, decision="RELEASE"))
# Nothing collected is an ENDING, not an error. The agent still decides, there is simply
# nothing to move — and refusing here used to leave it with no legal way to finish, so the
# most likely path through the public demo dead-ended in an escalation.
check("a pool nobody paid into settles with no items",
      guards.build_settlement_items(decision="UNWIND", payers=[], recipient=REC,
                                    frozen_collected_cents=0, target_cents=18000) == [])
allowed("and the pre-flight accepts that, at zero only",
        lambda: guards.assert_settlement_valid(
            items=[], frozen_collected_cents=0, allowed_emails=ALLOWED, payers=[],
            decision="UNWIND"))
check("a settlement with no items is trivially finished",
      guards.batch_is_done({"items": [], "batch_status": "NO_PAYOUT"}))
check("an absent item list is unknown, not empty",
      not guards.batch_is_done({"batch_status": "PENDING"}))
blocked("but an empty settlement for money that DID arrive is refused", "NOTHING_COLLECTED",
        lambda: guards.assert_settlement_valid(
            items=[], frozen_collected_cents=13500, allowed_emails=ALLOWED,
            payers=PAYERS, decision="UNWIND"))
blocked("a negative collection is refused outright", "BAD_AMOUNT",
        lambda: guards.build_settlement_items(decision="UNWIND", payers=[], recipient=REC,
                                              frozen_collected_cents=-1, target_cents=18000))
blocked("a decision that is neither branch", "BAD_ARGS",
        lambda: guards.build_settlement_items(decision="STEAL", payers=PAYERS, recipient=REC,
                                              frozen_collected_cents=18000, target_cents=18000))
blocked("blowing through today's payout ceiling", "CAP_EXCEEDED",
        lambda: guards.assert_settlement_valid(items=release, frozen_collected_cents=18000,
                                               allowed_emails=ALLOWED, payers=PAYERS,
                                               decision="RELEASE",
                                               payouts_today_cents=10_000_000))

print("--- overfunded pools split to the exact cent ---")
over_payers = [{"cid": f"c{i}", "name": f"P{i}", "email": f"p{i}@ok.com", "paid_cents": p}
               for i, p in enumerate([4500, 4500, 4500, 7000])]
over_allowed = guards.allowed_payout_emails(REC, over_payers)
over = guards.build_settlement_items(decision="RELEASE", payers=over_payers, recipient=REC,
                                     frozen_collected_cents=20500, target_cents=18000,
                                     overage="refund_prorata")
check("overfunded: items still sum to exactly what was collected",
      sum(i["amount_cents"] for i in over) == 20500, sum(i["amount_cents"] for i in over))
allowed("overfunded settlement passes pre-flight",
        lambda: guards.assert_settlement_valid(items=over, frozen_collected_cents=20500,
                                               allowed_emails=over_allowed, payers=over_payers,
                                               decision="RELEASE"))
check("overfunded: the recipient gets exactly the target",
      next(i["amount_cents"] for i in over if i["kind"] == "release") == 18000)

rnd = random.Random(7)
exact = all(
    sum(guards._prorata(amt, [(f"c{i}", rnd.randint(1, 9999)) for i in range(n)]).values()) == amt
    for amt, n in ((rnd.randint(1, 500000), rnd.randint(1, 11)) for _ in range(400))
)
check("pro-rata never loses or invents a cent (400 random splits)", exact)

print("--- idempotency ---")
check("the payout batch id is deterministic for a settlement",
      guards.sender_batch_id("pool_abc", "UNWIND") == guards.sender_batch_id("pool_abc", "UNWIND")
      != guards.sender_batch_id("pool_abc", "RELEASE"))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    sys.exit(1)

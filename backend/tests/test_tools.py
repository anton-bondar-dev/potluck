"""End-to-end lifecycle through the tool layer, on the in-memory PayPal.

Run: USE_MOCK_PAYPAL=1 ./.venv/bin/python tests/test_tools.py
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clock, store, tools  # noqa: E402
from bus import EventBus  # noqa: E402
from paypal import get_paypal_client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


CAST = [{"cid": f"c{i}", "name": n, "email": f"{n.lower()}@sandbox.test"}
        for i, n in enumerate(["Ana", "Ben", "Cal", "Dee", "Eli"])]
CAST.append({"cid": "rec", "name": "Dana", "email": "dana@sandbox.test"})


async def main():
    tmp = tempfile.mkdtemp()
    await store.init_db(os.path.join(tmp, "t.db"))
    b = EventBus()

    pid = await store.create_pool(
        goal_text="Collect $45 each from the five of us for Dana's $180 leaving gift by Friday.",
        policy_text="Only buy it if we reach $180. If we fall short, give everyone their money back.")
    await store.seed_cast(pid, CAST)

    async with get_paypal_client() as pp:
        ctx = tools.ToolContext(pid, pp, b, episode_id="ep_test")

        # --- the model plans the pool ---
        r = await tools.dispatch(ctx, "plan_pool", {
            "restatement": "Five people chip in $45 each for Dana's gift.",
            "success_condition_restated": "Release only if we collect at least $180; otherwise refund everyone.",
            "recipient_cid": "rec", "target_cents": 18000, "deadline_label": "Friday 9am",
            "horizon_seconds": 3600,
            "contributors": [{"cid": f"c{i}", "share_cents": 4500} for i in range(5)]})
        check("plan_pool accepted a sane plan", r.get("n") == 5, r)
        check("plan_pool moved the pool to COLLECTING",
              (await store.get_pool(pid))["phase"] == "COLLECTING")
        check("shares cover the target", r.get("slack_cents") == 4500, r.get("slack_cents"))

        # --- a wrong-phase call is refused even if the model asks ---
        bad = await tools.dispatch(ctx, "plan_pool", {"restatement": "again"})
        check("re-planning a live pool is refused", bad.get("error") == "WRONG_PHASE", bad)

        # --- invoices go out ---
        r = await tools.dispatch(ctx, "send_invoices", {"cids": ["all"], "item_label": "Gift share"})
        check("all five invoices sent", len(r.get("sent", [])) == 5, r)
        r2 = await tools.dispatch(ctx, "send_invoices", {"cids": ["all"], "item_label": "Gift share"})
        check("re-sending is idempotent (all skipped, nobody invoiced twice)",
              len(r2.get("skipped", [])) == 5 and not r2.get("sent"), r2)

        # --- three of five pay ---
        invs = {i["cid"]: i for i in await store.get_invoices(pid)}
        for cid in ("c0", "c1", "c2"):
            await pp.record_payment(invs[cid]["invoice_id"], 4500)
        r = await tools.dispatch(ctx, "check_payments", {"reason": "halfway check"})
        check("check_payments reads the real payment state",
              r.get("collected_cents") == 13500 and r.get("paid_count") == 3, r)
        check("it reports who is still open", len(r.get("open", [])) == 2, r.get("open"))

        # --- reminders: the model picks who, code vetoes the rest ---
        r = await tools.dispatch(ctx, "remind_contributors", {"reminders": [
            {"cid": "c3", "note": "Hey Dee, just you and Eli left!"},
            {"cid": "c0", "note": "nudge"}]})
        check("the unpaid contributor is reminded", r.get("reminded") == ["c3"], r)
        check("reminding someone who already paid is vetoed",
              r.get("blocked") and r["blocked"][0]["reason"] == "ALREADY_PAID", r.get("blocked"))

        # --- attestation: a decision on invented numbers is refused ---
        r = await tools.dispatch(ctx, "resolve_pool", {
            "decision": "RELEASE", "reason": "we made it", "policy_quote": "reach $180",
            "attest": {"collected_cents": 18000, "paid_count": 5, "ledger_version": 99}})
        check("a settlement attested on numbers that never happened is blocked",
              r.get("error") == "LEDGER_MISMATCH", r)
        check("the blocked settlement did NOT move the pool",
              (await store.get_pool(pid))["phase"] == "COLLECTING",
              (await store.get_pool(pid))["phase"])

        # --- the real decision: short of target -> UNWIND ---
        truth = await tools.dispatch(ctx, "check_payments", {"reason": "deadline"})
        r = await tools.dispatch(ctx, "resolve_pool", {
            "decision": "UNWIND",
            "reason": "We collected $135 of the $180 we needed, so by the organizer's rule everyone gets refunded.",
            "policy_quote": "If we fall short, give everyone their money back.",
            "attest": {k: truth[k] for k in ("collected_cents", "paid_count", "ledger_version")}})
        check("UNWIND settles", r.get("kind") == "unwind", r)
        check("exactly the three payers are refunded", r.get("item_count") == 3, r)
        check("refunds total exactly what was collected", r.get("total_cents") == 13500, r)
        check("the pool moved to SETTLING", (await store.get_pool(pid))["phase"] == "SETTLING")

        s = await store.get_settlement(pid)
        check("each refund equals what that person paid",
              all(i["amount_cents"] == 4500 for i in s["items"]), s["items"])
        check("the batch id is deterministic",
              s["sender_batch_id"] == f"potluck-{pid}-UNWIND", s["sender_batch_id"])

        # --- a second settlement cannot happen ---
        r = await tools.dispatch(ctx, "resolve_pool", {
            "decision": "RELEASE", "reason": "second try", "policy_quote": "x",
            "attest": {k: truth[k] for k in ("collected_cents", "paid_count", "ledger_version")}})
        check("a second settlement is refused", r.get("error") in ("ALREADY_SETTLED", "WRONG_PHASE"), r)

        # --- unpaid invoices were closed before money moved ---
        after = {i["cid"]: i for i in await store.get_invoices(pid)}
        check("unpaid invoices were cancelled before the payout",
              after["c3"]["status"] == "CANCELLED" and after["c4"]["status"] == "CANCELLED",
              {k: after[k]["status"] for k in ("c3", "c4")})

        # --- settle and close ---
        await asyncio.sleep(3.2)   # the mock settles its batch after ~3s
        r = await tools.dispatch(ctx, "check_payout", {"reason": "confirm"})
        check("the payout batch reaches SUCCESS", r.get("batch_status") == "SUCCESS", r)
        r = await tools.dispatch(ctx, "close_pool", {"summary": "Refunded everyone, nobody is out of pocket."})
        check("the pool closes", (await store.get_pool(pid))["phase"] == "CLOSED")

    # --- the whole story is on the event log ---
    evs = await store.read_events(pid, 0, 5000)
    types = [e["type"] for e in evs]
    for needed in ("ledger_init", "ledger_row", "pool_totals", "reminder_sent", "guard_block",
                   "decision", "fact_check", "payout_submitted", "payout_item", "pool_closed"):
        check(f"event '{needed}' was streamed", needed in types, types[:40])
    dec = next(e for e in evs if e["type"] == "decision")
    check("the decision event carries the model's own words and a policy quote",
          dec["policy_quote"].startswith("If we fall short") and dec["attested"] is True, dec)

    await demo_clock(pp, b)

    await store.close_db()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL)); sys.exit(1)


async def demo_clock(pp, b):
    """ONE CLOCK, and the only place it is set.

    The model reads "by Friday" and asks for three days, which is the right answer to the
    question it was asked and useless for a demo nobody will watch for three days. A demo
    pool compresses the REAL horizon and derives the display scale from the ratio, so the
    countdown renders exactly the span the model asked for. Without this the pool's
    climax is three days away and the film cannot exist.
    """
    print("\n-- the demo clock --")
    for demo, asked, want_real, want_scale in ((True, 259200, 90, 2880),
                                               (False, 259200, 259200, 1),
                                               (True, 60, 60, 1)):
        pid = await store.create_pool(goal_text="Three days of chipping in for a gift.",
                                      policy_text="Reach the target or refund.", demo=demo)
        await store.seed_cast(pid, CAST)
        ctx = tools.ToolContext(pid, pp, b)
        r = await tools.dispatch(ctx, "plan_pool", {
            "restatement": "x", "success_condition_restated": "y", "recipient_cid": "rec",
            "target_cents": 18000, "deadline_label": "Friday 9am", "horizon_seconds": asked,
            "contributors": [{"cid": f"c{i}", "share_cents": 4500} for i in range(5)]})
        tag = f"demo={demo} asked={asked}s"
        check(f"{tag}: the real deadline is {want_real}s away",
              abs(r["time_left_s"] - want_real) <= 2, r.get("time_left_s") or r)
        check(f"{tag}: the scale is {want_scale}", r["clock_scale"] == want_scale, r)
        label = clock.display_label(r["time_left_s"], r["clock_scale"])
        check(f"{tag}: the countdown still renders what was asked for",
              label == clock.human_label(asked) or abs(
                  clock.display_left_s(r["time_left_s"], r["clock_scale"]) - asked) < asked * 0.05,
              (label, clock.human_label(asked)))
        check(f"{tag}: compression is disclosed to the model" if want_scale > 1
              else f"{tag}: no compression, nothing to disclose",
              bool(r.get("note")) == (want_scale > 1), r.get("note"))


if __name__ == "__main__":
    asyncio.run(main())

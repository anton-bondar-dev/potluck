"""The ticker: what costs a token and what does not.

The claim this file exists to prove is the load-bearing one in the whole design — that a
routine payment is recorded, written and streamed with NO model involvement, and that the
model is woken only for transitions that need a judgement. Everything else here is the
failure behaviour: the row lock, the deadline that must not re-fire every two seconds, the
escalation that must not move money, and a restart that reconciles an in-flight payout by
GET rather than paying it again.

Run: USE_MOCK_PAYPAL=1 ./.venv/bin/python tests/test_ticker.py
"""
import asyncio, json, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_core, bus as bus_mod, store, ticker, tools  # noqa: E402
from bus import EventBus  # noqa: E402
from config import settings  # noqa: E402
from paypal import get_paypal_client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


class CountingLLM:
    """Counts how many times the ticker decided a model turn was worth paying for.

    With no script it parks the agent immediately — one sleep_until and done — which is
    what we want for every test whose subject is the ticker rather than the agent.
    """
    model = "counting/stub"

    def __init__(self, script=None, park_s=300):
        self.n = 0
        self.script = list(script or [])
        self.park_s = park_s
        self.triggers = []

    async def generate(self, messages, tools=None, **_kw):
        self.n += 1
        names = [t["function"]["name"] for t in (tools or [])]
        turn = self.script.pop(0) if self.script else None
        if turn is None:
            turn = ([("sleep_until", {"sleep_s": self.park_s, "reason": "parked"})]
                    if "sleep_until" in names else [])
        if not turn:
            return {"choices": [{"message": {"role": "assistant", "content": "nothing to do"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520}}
        calls = [{"id": f"c{self.n}_{i}", "type": "function",
                  "function": {"name": nm, "arguments": json.dumps(a)}}
                 for i, (nm, a) in enumerate(turn)]
        return {"choices": [{"message": {"role": "assistant", "content": "",
                                         "tool_calls": calls},
                             "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 540}}


CAST = [{"cid": f"c{i}", "name": n, "email": f"{n.lower()}@sandbox.test"}
        for i, n in enumerate(["Ana", "Ben", "Cal", "Dee", "Eli"])]
CAST.append({"cid": "rec", "name": "Dana", "email": "dana@sandbox.test"})

PLAN = {"restatement": "Five chip in $45 each.",
        "success_condition_restated": "Release at $180 or more, otherwise refund everyone.",
        "recipient_cid": "rec", "target_cents": 18000, "deadline_label": "Friday",
        "horizon_seconds": 3600,
        "contributors": [{"cid": f"c{i}", "share_cents": 4500} for i in range(5)]}


async def fresh():
    """A new database per scenario, so one test's pools cannot wake another's model."""
    await store.close_db()
    await store.init_db(os.path.join(tempfile.mkdtemp(), "tick.db"))


async def collecting_pool(pp, b, *, horizon=3600):
    """A pool already invoiced and collecting, set up WITHOUT spending an episode."""
    pid = await store.create_pool(goal_text="$45 each for Dana's $180 gift.",
                                  policy_text="Only buy it if we reach $180, else refund.")
    await store.seed_cast(pid, CAST)
    ctx = tools.ToolContext(pid, pp, b)
    await tools.dispatch(ctx, "plan_pool", {**PLAN, "horizon_seconds": horizon})
    await tools.dispatch(ctx, "send_invoices", {"cids": ["all"], "item_label": "Gift share"})
    return pid, {i["cid"]: i for i in await store.get_invoices(pid)}


def new_ticker(pp, b, llm=None):
    return ticker.Ticker(pp=pp, llm=llm or CountingLLM(),
                         meter=agent_core.TokenMeter(budget=500_000), bus=b, interval_s=0.05)


async def events(pid, etype=None):
    evs = await store.read_events(pid)
    return [e for e in evs if etype is None or e["type"] == etype]


# =============================================================================
async def zero_token_payments(pp, b):
    print("\n-- what costs a token --")
    await fresh()
    pid, invs = await collecting_pool(pp, b)
    llm = CountingLLM()
    t = new_ticker(pp, b, llm)

    await t.tick_once()
    check("a quiet pool costs nothing", llm.n == 0, llm.n)
    check("but the clock still runs", len(await events(pid, "clock_tick")) >= 1)

    await pp.record_payment(invs["c0"]["invoice_id"], 4500)
    await t.tick_once()
    check("the FIRST payment is worth waking the agent for", llm.n == 1, llm.n)
    check("the payment is streamed either way", len(await events(pid, "payment_detected")) == 1)

    await pp.record_payment(invs["c1"]["invoice_id"], 4500)
    await t.tick_once()
    await pp.record_payment(invs["c2"]["invoice_id"], 4500)
    await t.tick_once()
    check("ROUTINE PAYMENTS COST ZERO TOKENS", llm.n == 1, llm.n)
    check("and yet the ledger is live: three rows, three detections",
          len(await events(pid, "payment_detected")) == 3)
    check("the running total is published without a model turn",
          any(e["collected_cents"] == 13500 for e in await events(pid, "pool_totals")))

    await pp.record_payment(invs["c3"]["invoice_id"], 4500)
    await t.tick_once()
    check("crossing the target is worth waking the agent for", llm.n == 2, llm.n)

    await pp.record_payment(invs["c4"]["invoice_id"], 4500)
    await t.tick_once()
    check("a payment past the target is routine again", llm.n == 2, llm.n)
    check("the sweep is labelled as the ticker's own work on the wire",
          any(e["origin"] == "ticker" for e in await events(pid, "paypal_call")),
          [e.get("origin") for e in await events(pid, "paypal_call")][:4])


async def interesting_transitions(pp, b):
    print("\n-- the other interesting transitions --")
    await fresh()
    pid, invs = await collecting_pool(pp, b)
    llm = CountingLLM()
    t = new_ticker(pp, b, llm)
    await t.tick_once()
    await pp.record_payment(invs["c0"]["invoice_id"], 4500)
    await t.tick_once()                                   # first payment: n=1
    base = llm.n

    await store.bump_reminder(pid, "c3")
    await t.tick_once()                                   # the nudge is noticed, not acted on
    check("a reminder on its own wakes nobody", llm.n == base, llm.n)
    await pp.record_payment(invs["c3"]["invoice_id"], 4500)
    await t.tick_once()
    check("but the person the agent nudged paying does", llm.n == base + 1, llm.n)

    await fresh()
    pid, invs = await collecting_pool(pp, b)
    llm = CountingLLM()
    t = new_ticker(pp, b, llm)
    await t.tick_once()
    await pp.record_payment(invs["c0"]["invoice_id"], 4500)
    await t.tick_once()
    await pp.record_payment(invs["c1"]["invoice_id"], 2000)
    await t.tick_once()
    check("a partial payment is never routine", llm.n == 2, llm.n)
    await pp.record_payment(invs["c2"]["invoice_id"], 9000)
    did = await t.tick_once()
    check("an overpayment is never routine either",
          any(d.get("why") == "an overpayment" for d in did), did)


async def draft_and_sleep(pp, b):
    print("\n-- planning once, and sleeping properly --")
    await fresh()
    pid = await store.create_pool(goal_text="$45 each for Dana.", policy_text="Reach $180 or refund.")
    await store.seed_cast(pid, CAST)
    llm = CountingLLM(script=[[("plan_pool", PLAN)],
                              [("send_invoices", {"cids": ["all"], "item_label": "Gift share"})],
                              [("sleep_until", {"sleep_s": 300, "reason": "waiting"})]])
    t = new_ticker(pp, b, llm)

    did = await t.tick_once()
    check("a DRAFT pool is planned on the first pass",
          did and did[0]["trigger"] == "pool_created", did)
    check("and it reaches COLLECTING", (await store.get_pool(pid))["phase"] == "COLLECTING")
    check("the agent parked itself", t.wake_at.get(pid, 0) > time.time() + 200, t.wake_at)

    before = llm.n
    for _ in range(3):
        await t.tick_once()
    check("a parked agent is not woken again on every tick", llm.n == before, llm.n)
    check("and the pool is not re-planned", len(await events(pid, "ledger_init")) == 1)

    t.wake_at[pid] = time.time() - 1
    did = await t.tick_once()
    check("when the window it asked for elapses, it is woken",
          any(d.get("trigger") == "tick" for d in did), did)


async def deadline_and_escalation(pp, b):
    print("\n-- the deadline, and refusing to decide for it --")
    await fresh()
    pid, invs = await collecting_pool(pp, b, horizon=3600)
    await pp.record_payment(invs["c0"]["invoice_id"], 4500)
    # The model is unreachable for the rest of this scenario.
    llm = CountingLLM(script=[[], [], [], [], [], [], [], [], []])
    t = new_ticker(pp, b, llm)
    await store.db().execute("UPDATE pools SET deadline_at=? WHERE pool_id=?",
                             (time.time() - 1, pid))
    await store.db().commit()

    did = await t.tick_once()
    check("reaching the deadline wakes the agent",
          any(d.get("trigger") == "deadline" for d in did), did)
    n_after_first = llm.n
    for _ in range(5):
        await t.tick_once()
    check("a reached deadline does NOT re-fire on every tick", llm.n == n_after_first, llm.n)
    check("the countdown went red", len(await events(pid, "deadline_imminent")) == 1)

    # now push past the grace window
    await store.db().execute("UPDATE pools SET deadline_at=? WHERE pool_id=?",
                             (time.time() - settings.deadline_grace_s - 5, pid))
    await store.db().commit()
    did = await t.tick_once()
    check("past the grace window the pool escalates",
          any(d.get("action") == "escalated" for d in did), did)
    check("to a human, not to a payout", (await store.get_pool(pid))["phase"] == "ESCALATED")
    esc = await events(pid, "escalated")
    check("the escalation proposes a refund and waits for approval",
          esc and esc[0]["proposed_action"] == "refund_everyone", esc)
    check("NO MONEY MOVED", not await events(pid, "payout_submitted")
          and not await events(pid, "decision"))
    check("the unpaid invoices were frozen first",
          len(await events(pid, "invoice_cancelled")) == 4,
          len(await events(pid, "invoice_cancelled")))
    paid = [i for i in await store.get_invoices(pid) if int(i["paid_cents"]) > 0]
    check("and the money that did arrive is left exactly where it is",
          len(paid) == 1 and paid[0]["paid_cents"] == 4500, paid)
    check("an escalated pool drops off the ticker's list",
          pid not in {p["pool_id"] for p in await store.live_pools()})

    # out of episodes, past the deadline: escalate without waiting out the grace
    await fresh()
    pid, _ = await collecting_pool(pp, b)
    await store.db().execute(
        "UPDATE pools SET deadline_at=?, episodes_used=? WHERE pool_id=?",
        (time.time() - 1, settings.max_episodes, pid))
    await store.db().commit()
    llm2 = CountingLLM()
    t2 = new_ticker(pp, b, llm2)
    await t2.tick_once()
    check("a pool with no episodes left escalates at the deadline, not after the grace",
          (await store.get_pool(pid))["phase"] == "ESCALATED")
    check("and spends nothing trying", llm2.n == 0, llm2.n)


async def human_message(pp, b):
    print("\n-- a human interrupting --")
    await fresh()
    pid, invs = await collecting_pool(pp, b)
    llm = CountingLLM()
    t = new_ticker(pp, b, llm)
    t.wake_at[pid] = time.time() + 9999                   # the agent is deep asleep
    await t.tick_once()
    check("a sleeping agent stays asleep", llm.n == 0, llm.n)

    t.message(pid, "Sam says he'll pay tonight — can you give him until then?")
    did = await t.tick_once()
    check("a human message wakes it straight through the sleep",
          any(d.get("trigger") == "human" for d in did), did)
    brief = llm.n
    await t.tick_once()
    check("and is delivered exactly once", llm.n == brief, llm.n)


async def locks_and_recovery(pp, b):
    print("\n-- the row lock and the restart --")
    await fresh()
    pid, invs = await collecting_pool(pp, b)
    llm = CountingLLM()
    t = new_ticker(pp, b, llm)

    claimed = await store.claim_pool(pid, 60)
    did = await t.tick_once()
    check("a pool claimed by someone else is left alone", claimed and did == [], did)
    await store.release_pool(pid)

    t1, t2 = new_ticker(pp, b, llm), new_ticker(pp, b, llm)
    await pp.record_payment(invs["c0"]["invoice_id"], 4500)
    both = await asyncio.gather(t1.tick_once(), t2.tick_once())
    served = [d for pass_ in both for d in pass_]
    check("two tickers racing one pool: exactly one acts", len(served) == 1, served)

    # a restart, mid-sleep, with an in-flight payout batch
    await fresh()
    pid, invs = await collecting_pool(pp, b)
    for cid in ("c0", "c1", "c2", "c3", "c4"):
        await pp.record_payment(invs[cid]["invoice_id"], 4500)
    ctx = tools.ToolContext(pid, pp, b)
    truth = await tools.refresh_from_paypal(ctx)
    r = await tools.dispatch(ctx, "resolve_pool", {
        "decision": "RELEASE", "reason": "everyone paid", "policy_quote": "reach $180",
        "attest": {k: truth[k] for k in ("collected_cents", "paid_count", "ledger_version")}})
    check("the scenario is set up with a real batch in flight",
          r.get("batch_status") == "PENDING", r)
    posts = [c for c in pp.calls if c["method"] == "POST" and c["path"].endswith("/payouts")]

    ep = await store.start_episode(pid, "tick")
    await store.end_episode(ep, turns=1, prompt_tokens=10, completion_tokens=5, reason="sleep",
                            messages=[{"role": "system", "content": "s"},
                                      {"role": "user", "content": "u"},
                                      {"role": agent_core.PENDING_SLEEP,
                                       "tool_call_id": "zz", "sleep_s": 40}])
    await store.claim_pool(pid, 3600)                     # a claim the dead process still holds

    t3 = new_ticker(pp, b, llm)
    summary = await t3.recover()
    check("recovery clears a claim left by a process that is gone",
          summary["reclaimed"] == 1, summary)
    check("it gives the sleeping agent its wake time back",
          summary["sleeps_restored"] == 1 and pid in t3.wake_at, summary)
    check("it reconciles the in-flight batch", summary["batches_reconciled"] == 1, summary)
    posts_after = [c for c in pp.calls if c["method"] == "POST" and c["path"].endswith("/payouts")]
    check("BY GET, NEVER BY RE-POST — the batch is read back, not re-sent",
          len(posts_after) == len(posts), (len(posts), len(posts_after)))
    check("the per-item cascade is replayed onto the stream",
          len(await events(pid, "payout_item")) >= 1)
    check("and the pool is claimable again", await store.claim_pool(pid, 5))
    await store.release_pool(pid)


async def resilience(pp, b):
    print("\n-- one pool must not take the others down --")
    await fresh()
    good, _ = await collecting_pool(pp, b)
    bad, _ = await collecting_pool(pp, b)
    llm = CountingLLM()
    t = new_ticker(pp, b, llm)
    original = t._work

    async def explode(pool):
        if pool["pool_id"] == bad:
            raise RuntimeError("boom")
        return await original(pool)

    t._work = explode
    did = await t.tick_once()
    check("the failing pool is reported, not raised",
          any(d.get("pool_id") == bad and "boom" in d.get("error", "") for d in did), did)
    check("the healthy pool was still served the same pass",
          len(await events(good, "clock_tick")) >= 1)
    check("and the failing pool's claim was released", await store.claim_pool(bad, 5))
    await store.release_pool(bad)


async def main():
    await store.init_db(os.path.join(tempfile.mkdtemp(), "boot.db"))
    b = EventBus()
    async with get_paypal_client(on_call=bus_mod.stream_paypal_call) as pp:
        await zero_token_payments(pp, b)
        await interesting_transitions(pp, b)
        await draft_and_sleep(pp, b)
        await deadline_and_escalation(pp, b)
        await human_message(pp, b)
        await locks_and_recovery(pp, b)
        await resilience(pp, b)
    await store.close_db()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())

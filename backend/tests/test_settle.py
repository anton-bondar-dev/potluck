"""The money-out paths that have no model in them.

Three things are tested here and each one is a claim the README makes:

  * THERE IS EXACTLY ONE SETTLEMENT PATH. The agent's decision and a human's approved
    refund both go through execute_settlement, so there is one function to audit and one
    place where amounts and destinations are computed.
  * A LATE PAYMENT IS RETURNED BY CODE. Money that lands after the pool was frozen goes
    back to the person who sent it, with no LLM involved, and cannot be returned twice.
  * A SCHEMA CHANGE REACHES AN EXISTING DATABASE. CREATE TABLE IF NOT EXISTS does not add
    a column, which is a bug that only ever appears on the deployed box.

Run: USE_MOCK_PAYPAL=1 ./.venv/bin/python tests/test_settle.py
"""
import asyncio, json, os, re, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aiosqlite  # noqa: E402
import httpx  # noqa: E402

import agent_core, bus as bus_mod, guards, main, store, tools  # noqa: E402
from config import settings  # noqa: E402
from paypal import get_paypal_client  # noqa: E402
from ticker import Ticker  # noqa: E402
from test_ticker import CountingLLM  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


CAST = [{"cid": "c1", "name": "Maya", "email": "maya@sandbox.test"},
        {"cid": "c2", "name": "Sam", "email": "sam@sandbox.test"},
        {"cid": "c3", "name": "Priya", "email": "priya@sandbox.test"},
        {"cid": "c4", "name": "Theo", "email": "theo@sandbox.test"},
        {"cid": "c5", "name": "Noor", "email": "noor@sandbox.test"},
        {"cid": "r1", "name": "Dana", "email": "dana@sandbox.test"}]

PLAN = {"restatement": "Five chip in $45 each.",
        "success_condition_restated": "Release at $180 or more, otherwise refund everyone.",
        "recipient_cid": "r1", "target_cents": 18000, "deadline_label": "Friday",
        "horizon_seconds": 3600,
        "contributors": [{"cid": f"c{i}", "share_cents": 4500} for i in range(1, 6)]}


async def fresh():
    """A new database per scenario.

    tick_once() serves every live pool, so a pool left settling by an earlier scenario
    would be woken inside a later one — correct behaviour for the product, and a
    cross-contaminated assertion in a test.
    """
    await store.close_db()
    await store.init_db(os.path.join(tempfile.mkdtemp(), "settle.db"))


async def events(pid, etype=None):
    evs = await store.read_events(pid)
    return [e for e in evs if etype is None or e["type"] == etype]


async def collecting(pp, b, payers=("c1", "c2", "c3")):
    """A pool already invoiced and partly paid.

    Everything runs inside bus.pool_scope, the way the ticker and the API run it in
    production — that is what makes every REST call land on the pool's stream as a
    paypal_call, which the receipt then counts.
    """
    pid = await store.create_pool(goal_text="$45 each for Dana's $180 gift by Friday.",
                                  policy_text="Only buy it if we reach $180, else refund.")
    await store.seed_cast(pid, CAST)
    ctx = tools.ToolContext(pid, pp, b)
    with bus_mod.pool_scope(pid):
        await tools.dispatch(ctx, "plan_pool", PLAN)
        await tools.dispatch(ctx, "send_invoices", {"cids": ["all"], "item_label": "Gift share"})
        invs = {i["cid"]: i for i in await store.get_invoices(pid)}
        for cid in payers:
            await pp.record_payment(invs[cid]["invoice_id"], 4500)
        await tools.refresh_from_paypal(ctx)
    return pid, ctx, invs


# =============================================================================
async def one_path():
    print("\n-- one settlement path --")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools.py")).read()
    tick_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "ticker.py")).read()
    check("tools.py posts a payout in exactly one place",
          len(re.findall(r"create_payout\(", src)) == 1,
          re.findall(r".*create_payout\(.*", src))
    check("the ticker's only payout is the late refund",
          len(re.findall(r"create_payout\(", tick_src)) == 1,
          re.findall(r".*create_payout\(.*", tick_src))
    check("no comparison of collected against target initiates money anywhere",
          not re.search(r"if .*collected\w*\s*>=\s*.*target", src + tick_src),
          re.findall(r".*collected.*>=.*target.*", src + tick_src))
    check("the model's resolve_pool is a thin call into the shared path",
          "return await execute_settlement(" in src)

    # Mock mode is supposed to need no credentials and no network at all. That claim is
    # in the README and it is how a judge runs this repo, so it is worth an assertion
    # rather than a promise: every outbound call is severed and the lifecycle still runs.
    from paypal import PayPalClient
    pp = PayPalClient()

    async def refuse(*_a, **_kw):
        raise AssertionError("mock mode made a network call")

    pp._client.request = refuse
    pp._client.post = refuse
    iid = await pp.create_invoice("a@b.c", 4500, note="n", item_name="i")
    await pp.send_invoice(iid)
    await pp.record_payment(iid, 4500)
    body = await pp.get_invoice(iid)
    batch = await pp.create_payout("sb-test", [{"email": "a@b.c", "amount_cents": 4500}])
    await pp.get_payout(batch["payout_batch_id"])
    await pp.aclose()
    check("MOCK MODE TOUCHES NO NETWORK — the whole lifecycle runs with every call severed",
          body.get("status") == "MARKED_AS_PAID", body.get("status"))


async def late_payments(pp, b):
    await fresh()
    print("\n-- a payment that arrives too late --")
    pid, ctx, invs = await collecting(pp, b)
    truth = await tools.refresh_from_paypal(ctx)
    out = await tools.execute_settlement(
        ctx, await store.get_pool(pid), decision="UNWIND",
        reason="only three of five paid", policy_quote="give everyone their money back",
        decided_by="agent",
        attest={k: truth[k] for k in ("collected_cents", "paid_count", "ledger_version")})
    check("the pool unwinds to the three payers", out["item_count"] == 3, out)
    rows = await store.get_invoices(pid)
    check("every paid invoice is stamped with what was frozen for it",
          all(int(r["settled_cents"]) == int(r["paid_cents"]) for r in rows),
          [(r["cid"], r["paid_cents"], r["settled_cents"]) for r in rows])
    check("and nothing is late yet", not await store.late_arrivals(pid))

    # Theo pays in the window between the freeze and the cancel. In that case PayPal
    # refuses the cancel (you cannot cancel a paid invoice) so the row stays SENT and
    # keeps being polled — which is exactly the state reproduced here.
    await store.db().execute("UPDATE invoices SET status='SENT' WHERE pool_id=? AND cid='c4'",
                             (pid,))
    await store.db().commit()
    await pp.record_payment(invs["c4"]["invoice_id"], 4500)

    llm = CountingLLM()
    t = Ticker(pp=pp, llm=llm, meter=agent_core.TokenMeter(budget=500_000), bus=b,
               interval_s=0.05)
    done = await t._late_sweep(ctx, await store.get_pool(pid))
    check("the late payment is detected and returned",
          done == [{"cid": "c4", "amount_cents": 4500}], done)
    check("WITH NO MODEL INVOLVED", llm.n == 0, llm.n)
    ev = await events(pid, "late_payment")
    check("it is announced as a refund, not as a decision",
          len(ev) == 1 and ev[0]["action"] == "REFUNDED" and ev[0]["cid"] == "c4", ev)
    check("the refund is exactly what arrived", ev[0]["paid_cents"] == 4500, ev[0])
    check("the batch id is derived from the pool and the amount, so a retry is the same id",
          ev[0]["sender_batch_id"] == guards.sender_batch_id(pid, "late-c4-4500"), ev[0])

    again = await t._late_sweep(ctx, await store.get_pool(pid))
    check("SWEEPING AGAIN REFUNDS NOTHING", again == [], again)
    check("and emits nothing new", len(await events(pid, "late_payment")) == 1)

    # the guard still governs the one path that moves money without a decision
    await store.db().execute(
        "UPDATE invoices SET paid_cents=?, settled_cents=0 WHERE pool_id=? AND cid='c5'",
        (settings.max_share_cents + 1, pid))
    await store.db().commit()
    blocked_before = len(await events(pid, "guard_block"))
    await t._late_sweep(ctx, await store.get_pool(pid))
    check("an implausible late amount is refused by the guard, not paid",
          len(await events(pid, "guard_block")) > blocked_before
          or not [e for e in await events(pid, "late_payment") if e["cid"] == "c5"])


async def closing_too_early(pp, b):
    await fresh()
    print("\n-- a pool cannot say 'done' before PayPal has --")
    pid, ctx, invs = await collecting(pp, b)
    with bus_mod.pool_scope(pid):
        truth = await tools.refresh_from_paypal(ctx, include_terminal=True)
        await tools.execute_settlement(
            ctx, await store.get_pool(pid), decision="UNWIND", reason="short",
            policy_quote="give everyone their money back", decided_by="agent",
            attest={k: truth[k] for k in ("collected_cents", "paid_count", "ledger_version")})
    s = await store.get_settlement(pid)
    check("the batch starts in flight", s["batch_status"] == "PENDING", s["batch_status"])

    out = await tools.dispatch(ctx, "close_pool", {"summary": "all done, money returned"})
    check("CLOSING WHILE THE PAYOUT IS PENDING IS REFUSED",
          out.get("error") == "PAYOUT_NOT_TERMINAL", out)
    check("and the model is told to poll first", "check_payout" in out.get("detail", ""), out)
    check("the pool is still settling", (await store.get_pool(pid))["phase"] == "SETTLING")
    check("and nothing claimed completion on the stream", not await events(pid, "pool_closed"))

    await asyncio.sleep(3.2)
    await tools.dispatch(ctx, "check_payout", {"reason": "has it landed?"})
    out = await tools.dispatch(ctx, "close_pool", {"summary": "refunds confirmed by PayPal"})
    check("once PayPal reports terminal, closing works", out.get("phase") == "CLOSED", out)
    check("and the pool is CLOSED", (await store.get_pool(pid))["phase"] == "CLOSED")

    receipts = await events(pid, "receipt")
    check("closing publishes a receipt", len(receipts) == 1, len(receipts))
    r = receipts[0]
    check("CONSERVATION: every cent that came in went back out",
          r["inflow_cents"] == 13500 and r["outflow_cents"] == 13500
          and r["delta_cents"] == 0, r)
    check("and it says so in words, not just a number",
          r["delta_explained"] == "balanced to the cent", r["delta_explained"])
    check("the ledger is hashed, so two runs are comparable",
          len(r["ledger_hash"]) == 64, r["ledger_hash"])
    prov = r["provenance"]
    check("it carries the real PayPal ids",
          prov["payout_batch_id"] and len(prov["invoice_ids"]) == 5, prov)
    check("and what the run actually cost, counted from the log",
          prov["paypal_calls"] >= 10 and prov["guards_fired"] >= 1,
          {k: prov[k] for k in ("paypal_calls", "guards_fired", "llm_turns")})
    check("the invariants it claims are named, not implied",
          "conservation" in r["invariants_checked"]
          and "exact_refunds" in r["invariants_checked"], r["invariants_checked"])

    for bad, why in (({}, "no settlement"), (None, "nothing settled")):
        try:
            guards.check_closable(bad or None)
            check(f"closing with {why} is refused", False, "not raised")
        except guards.GuardError as exc:
            check(f"closing with {why} is refused", exc.code in ("NOT_SETTLED",), exc.code)


async def human_refund(pp, b):
    await fresh()
    print("\n-- the human-approved refund on an escalated pool --")
    pid, ctx, invs = await collecting(pp, b)
    llm = CountingLLM()
    t = Ticker(pp=pp, llm=llm, meter=agent_core.TokenMeter(budget=500_000), bus=b,
               interval_s=0.05)
    await store.db().execute(
        "UPDATE pools SET deadline_at=?, episodes_used=? WHERE pool_id=?",
        (time.time() - settings.deadline_grace_s - 5, settings.max_episodes, pid))
    await store.db().commit()
    await t.tick_once()
    check("the pool escalated with nothing moved",
          (await store.get_pool(pid))["phase"] == "ESCALATED"
          and not await events(pid, "payout_submitted"))
    check("and the model was never asked to decide", llm.n == 0, llm.n)

    main.state.update(pp=pp, llm=llm, meter=agent_core.TokenMeter(budget=500_000), ticker=t,
                      started_at=time.time())
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(f"/api/pools/{pid}/approve-refund", json={"note": "approved by Anton"})
        check("a human can approve the refund", r.status_code == 200, r.text)
        out = r.json()
        check("it refunds exactly the three people who paid",
              out["kind"] == "unwind" and out["item_count"] == 3
              and out["total_cents"] == 13500, out)
        dec = (await events(pid, "decision"))[-1]
        check("the decision is attributed to the human, not the agent",
              dec["by"] == "human" and dec["attested"] is False, dec)
        sub = (await events(pid, "payout_submitted"))[-1]
        check("every item equals what that person actually paid",
              sorted(i["amount_cents"] for i in sub["items"]) == [4500, 4500, 4500], sub["items"])
        check("no address is published in the clear",
              all("***" in i["email_masked"] for i in sub["items"]), sub["items"])
        check("the pool is now settling", (await store.get_pool(pid))["phase"] == "SETTLING")
        moves = [(e["from"], e["to"]) for e in await events(pid, "phase")]
        check("every phase move the escalated pool makes is announced",
              ("ESCALATED", "SETTLING") in moves, moves)

        r = await c.post(f"/api/pools/{pid}/approve-refund", json={})
        check("approving twice is refused", r.status_code == 409, r.status_code)
        r = await c.post(f"/api/pools/{pid}/approve-refund")
        check("and a bodyless click is a clean refusal, not a validation error",
              r.status_code == 409, r.status_code)

        pid2, _, _ = await collecting(pp, b)
        r = await c.post(f"/api/pools/{pid2}/approve-refund", json={})
        check("a pool that has not escalated cannot be refunded by hand",
              r.status_code == 409, r.status_code)
        r = await c.post("/api/pools/pool_nope/approve-refund", json={})
        check("an unknown pool is a 404", r.status_code == 404, r.status_code)

    print("\n-- closing a pool nobody is left to close --")
    await asyncio.sleep(3.2)                       # the mock batch reaches SUCCESS after 3s
    await t.tick_once()
    check("code closes a human-refunded pool once PayPal confirms the batch",
          (await store.get_pool(pid))["phase"] == "CLOSED")
    closed = await events(pid, "pool_closed")
    check("and says plainly that the system did it, not the agent",
          closed and closed[-1]["by"] == "system" and closed[-1]["decided_by"] == "human",
          closed[-1:])
    check("the summary states only what PayPal reported, item by item",
          "success" in closed[-1]["summary"].lower()
          and "PayPal reports" in closed[-1]["summary"], closed[-1]["summary"])
    check("still with no model turn spent", llm.n == 0, llm.n)


async def agent_keeps_the_last_word(pp, b):
    await fresh()
    print("\n-- the agent still closes its own pools --")
    pid, ctx, invs = await collecting(pp, b, payers=("c1", "c2", "c3", "c4", "c5"))
    truth = await tools.refresh_from_paypal(ctx)
    await tools.execute_settlement(
        ctx, await store.get_pool(pid), decision="RELEASE", reason="everyone paid",
        policy_quote="Only buy it if we reach $180.", decided_by="agent",
        attest={k: truth[k] for k in ("collected_cents", "paid_count", "ledger_version")})
    llm = CountingLLM()
    t = Ticker(pp=pp, llm=llm, meter=agent_core.TokenMeter(budget=500_000), bus=b,
               interval_s=0.05)
    await asyncio.sleep(3.2)
    await t._reconcile(await store.get_pool(pid))
    closed = await t._auto_close(ctx, await store.get_pool(pid))
    check("code does NOT close a pool whose agent can still speak", not closed)
    check("the pool stays in SETTLING for it",
          (await store.get_pool(pid))["phase"] == "SETTLING")


async def batch_completion():
    print("\n-- when is a batch actually finished --")
    # The bug this exists to prevent: a live batch reported PROCESSING in its header for
    # tens of seconds AFTER both of its items had reported SUCCESS. Judging completion on
    # the header meant the pool never closed and the agent was never woken to say so.
    lagging = {"batch_status": "PROCESSING", "item_statuses": {"c1": "SUCCESS", "c2": "SUCCESS"}}
    check("a lagging header does not keep a finished batch open",
          guards.batch_is_done(lagging))
    check("a genuinely pending item does keep it open",
          not guards.batch_is_done({"batch_status": "SUCCESS",
                                    "item_statuses": {"c1": "SUCCESS", "c2": "PENDING"}}))
    check("an unclaimed item is terminal — thirty days is not a wait we can take",
          guards.batch_is_done({"batch_status": "PROCESSING",
                                "item_statuses": {"c1": "SUCCESS", "c2": "UNCLAIMED"}}))
    check("a failed item is terminal too",
          guards.batch_is_done({"batch_status": "PROCESSING",
                                "item_statuses": {"c1": "FAILED"}}))
    check("with no item detail yet, the header is all there is",
          guards.batch_is_done({"batch_status": "SUCCESS", "item_statuses": {}})
          and not guards.batch_is_done({"batch_status": "PENDING", "item_statuses": {}}))
    check("and nothing at all is not done", not guards.batch_is_done(None))
    try:
        guards.check_closable(lagging)
        check("closing is allowed once the items are terminal", True)
    except guards.GuardError as exc:
        check("closing is allowed once the items are terminal", False, exc.code)


async def migration():
    print("\n-- a schema change that reaches the deployed database --")
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("CREATE TABLE invoices (pool_id TEXT, cid TEXT, paid_cents INTEGER, "
                       "reminders INTEGER, PRIMARY KEY (pool_id, cid))")
    await conn.commit()
    cur = await conn.execute("PRAGMA table_info(invoices)")
    check("the old database really is missing the column",
          "settled_cents" not in {r["name"] for r in await cur.fetchall()})
    applied = await store._migrate(conn)
    check("the migration adds it", applied == ["invoices.settled_cents"], applied)
    cur = await conn.execute("PRAGMA table_info(invoices)")
    check("and it is there afterwards",
          "settled_cents" in {r["name"] for r in await cur.fetchall()})
    check("running it twice is a no-op", await store._migrate(conn) == [])
    await conn.close()


async def main_test():
    await store.init_db(os.path.join(tempfile.mkdtemp(), "settle.db"))
    b = bus_mod.bus
    async with get_paypal_client(on_call=bus_mod.stream_paypal_call) as pp:
        await one_path()
        await late_payments(pp, b)
        await closing_too_early(pp, b)
        await human_refund(pp, b)
        await agent_keeps_the_last_word(pp, b)
        await batch_completion()
        await migration()
    await store.close_db()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main_test())

"""Store tests — the concurrency guarantees the safety story rests on.

Run: ./.venv/bin/python tests/test_store.py
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


async def main():
    tmp = tempfile.mkdtemp()
    await store.init_db(os.path.join(tmp, "t.db"))

    # --- pool lifecycle ---
    pid = await store.create_pool(goal_text="collect $45 each for Dana's gift",
                                  policy_text="buy it only if we hit $360, else refund everyone")
    p = await store.get_pool(pid)
    check("new pool starts in DRAFT", p["phase"] == "DRAFT", p["phase"])
    check("verbatim goal stored unparsed", p["goal_text"].startswith("collect $45"))
    check("verbatim policy stored unparsed", "refund everyone" in p["policy_text"])

    roster = [{"cid": f"c{i}", "name": f"P{i}", "email": f"p{i}@x.com", "share_cents": 4500}
              for i in range(5)]
    roster.append({"cid": "rec", "name": "Dana", "email": "dana@x.com",
                   "share_cents": 0, "is_recipient": True})
    await store.add_contributors(pid, roster)
    invs = await store.get_invoices(pid)
    check("invoice rows created for contributors only (not recipient)", len(invs) == 5, len(invs))

    # --- phase machine ---
    check("DRAFT -> COLLECTING advances", await store.advance(pid, "DRAFT", "COLLECTING"))
    check("the same transition cannot replay", not await store.advance(pid, "DRAFT", "COLLECTING"))
    try:
        await store.advance(pid, "COLLECTING", "CLOSED")
        check("illegal edge rejected", False, "no raise")
    except ValueError:
        check("illegal edge rejected", True)

    # --- concurrency: exactly one winner ---
    results = await asyncio.gather(*[store.advance(pid, "COLLECTING", "RESOLVING") for _ in range(12)])
    check("12 concurrent advances -> exactly 1 winner", sum(results) == 1, sum(results))

    # --- claim lock: exactly one holder ---
    claims = await asyncio.gather(*[store.claim_pool(pid, 30) for _ in range(12)])
    check("12 concurrent ticker claims -> exactly 1 holder", sum(claims) == 1, sum(claims))

    # --- settle exactly once ---
    items = [{"cid": "c0", "amount_cents": 4500}]
    first = await store.insert_settlement(pid, decision="UNWIND", reason="short",
                                          policy_quote="refund everyone",
                                          frozen_collected_cents=4500,
                                          sender_batch_id=f"potluck-{pid}-UNWIND", items=items)
    second = await store.insert_settlement(pid, decision="RELEASE", reason="oops",
                                           policy_quote="x", frozen_collected_cents=99999,
                                           sender_batch_id=f"potluck-{pid}-RELEASE", items=items)
    check("first settlement is written", first)
    check("SECOND settlement is refused (settle-once)", not second)
    s = await store.get_settlement(pid)
    check("stored settlement is the first one, unmutated",
          s["decision"] == "UNWIND" and s["frozen_collected_cents"] == 4500, s["decision"])

    # --- invoice state changes ---
    changed = await store.set_invoice_state(pid, "c0", status="SENT", paid_cents=0, source=None)
    again = await store.set_invoice_state(pid, "c0", status="SENT", paid_cents=0, source=None)
    check("a real change reports changed=True", changed)
    check("an identical write reports changed=False", not again)
    await store.set_invoice_state(pid, "c1", status="MARKED_AS_PAID", paid_cents=4500,
                                  source="merchant_recorded")
    await store.set_invoice_state(pid, "c2", status="PARTIALLY_PAID", paid_cents=2000,
                                  source="paypal_payment")
    check("collected sums only real payments", await store.collected_cents(pid) == 6500,
          await store.collected_cents(pid))

    # --- event log + replay cursor ---
    s1 = await store.append_event(pid, "phase", {"to": "COLLECTING"})
    s2 = await store.append_event(pid, "ledger_row", {"cid": "c1"})
    s3 = await store.append_event(pid, "pool_totals", {"collected_cents": 6500})
    check("event seq is monotonic", s1 < s2 < s3, (s1, s2, s3))
    tail = await store.read_events(pid, from_seq=s1)
    check("replay from a cursor returns only later events", [e["seq"] for e in tail] == [s2, s3])
    check("event payload round-trips", tail[-1]["collected_cents"] == 6500)

    # --- ledger version ---
    v1 = await store.bump_ledger_version(pid)
    v2 = await store.bump_ledger_version(pid)
    check("ledger_version increments", v2 == v1 + 1, (v1, v2))

    await store.close_db()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

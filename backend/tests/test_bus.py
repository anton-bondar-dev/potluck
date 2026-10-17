"""Bus tests — the cases Overindex's single-queue transport could not survive."""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402
from bus import EventBus, QUEUE_MAX  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


async def take(gen, n, timeout=2.0):
    """Collect n non-heartbeat frames."""
    out = []

    async def pump():
        async for f in gen:
            if f is None:
                continue
            out.append(f)
            if len(out) >= n:
                return
    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        await gen.aclose()   # a real client disconnecting mid-stream
    return out


async def main():
    tmp = tempfile.mkdtemp()
    await store.init_db(os.path.join(tmp, "b.db"))
    b = EventBus()
    pid = await store.create_pool(goal_text="g", policy_text="p")

    # --- two simultaneous viewers both get every frame (the destructive-queue bug) ---
    g1, g2 = b.stream(pid), b.stream(pid)
    t1 = asyncio.create_task(take(g1, 3))
    t2 = asyncio.create_task(take(g2, 3))
    await asyncio.sleep(0.05)
    for i in range(3):
        await b.publish(pid, "tick", {"i": i})
    a, c = await t1, await t2
    check("viewer A receives all 3", [f["i"] for f in a] == [0, 1, 2], [f.get("i") for f in a])
    check("viewer B receives all 3 too (not consumed by A)",
          [f["i"] for f in c] == [0, 1, 2], [f.get("i") for f in c])

    # --- a viewer who arrives late still sees the whole history ---
    late = await take(b.stream(pid, from_seq=0), 3)
    check("late viewer replays full history", [f["i"] for f in late] == [0, 1, 2])

    # --- reconnect with Last-Event-ID resumes without duplicates or gaps ---
    resume_from = late[0]["seq"]
    resumed = await take(b.stream(pid, from_seq=resume_from), 2)
    check("resume from cursor skips what was already seen",
          [f["i"] for f in resumed] == [1, 2], [f.get("i") for f in resumed])

    # --- publishing with nobody listening is still durable ---
    await b.publish(pid, "tick", {"i": 99})
    after = await take(b.stream(pid, from_seq=0), 4)
    check("events published with no subscriber are not lost",
          [f["i"] for f in after] == [0, 1, 2, 99], [f.get("i") for f in after])

    # --- a stalled client cannot stall the producer or grow memory ---
    async with b.subscribe(pid) as q:
        for i in range(QUEUE_MAX + 50):      # never drained
            await b.publish(pid, "flood", {"i": i})
        check("stalled subscriber queue stays bounded", q.qsize() <= QUEUE_MAX, q.qsize())
    check("unsubscribe cleans up", b.subscriber_count(pid) == 0, b.subscriber_count(pid))

    # --- the flood is still fully durable on disk ---
    rows = await store.read_events(pid, from_seq=0, limit=5000)
    check("every flooded event is durable despite drops",
          sum(1 for r in rows if r["type"] == "flood") == QUEUE_MAX + 50)

    await store.close_db()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL)); sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

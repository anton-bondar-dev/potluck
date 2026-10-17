"""The HTTP surface: creation, the repaint, the stream, and what must never leak.

Two things here are worth more than the rest. First, resume: a judge WILL refresh, or open
a link we shared an hour ago, and both paths have to paint the truth — so the projection
and the Last-Event-ID replay are tested against the same pool. Second, the email
discipline: every contributor address is masked in every event and in every response,
because the demo is public and the addresses are real sandbox accounts.

The app's lifespan is replaced with hand-built state, so no Groq key and no network are
needed and the ticker is one we can drive a pass at a time.

ONE DELIBERATE SPLIT: the ordinary endpoints are driven over HTTP through httpx's
ASGITransport, but the SSE endpoint is driven by iterating the StreamingResponse's own
generator. ASGITransport cannot cancel a stream that never ends — it blocks on context
exit — and an SSE endpoint never ends by design. Iterating the generator tests the part
that can actually be wrong (frame shape, replay cursor, heartbeat, terminal sentinel,
subscriber cleanup); preflight.py checks the real socket end to end at deploy time.

Run: USE_MOCK_PAYPAL=1 ./.venv/bin/python tests/test_api.py
"""
import asyncio, json, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

import agent_core, bus as bus_mod, cast as cast_mod, main, store, tools  # noqa: E402
from config import settings  # noqa: E402
from paypal import get_paypal_client  # noqa: E402
from ticker import Ticker  # noqa: E402
from test_ticker import CountingLLM  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


GOAL = "Collect $45 each from the five of us for Dana's $180 leaving gift by Friday."
POLICY = "Only buy it if we reach $180. If we fall short, give everyone their money back."

# Keyed to the real cast (cast.py), not to a test fixture: these cids are what the API
# actually seeds, and a plan naming anyone else is refused by the guard — which is the
# point of the roster.
PLAN = {"restatement": "Five people chip in $45 each for Dana's gift.",
        "success_condition_restated": "Release at $180 or more, otherwise refund everyone.",
        "recipient_cid": "r1", "target_cents": 18000, "deadline_label": "Friday 9am",
        "horizon_seconds": 3600,
        "contributors": [{"cid": f"c{i}", "share_cents": 4500} for i in range(1, 6)]}


# --- SSE helpers -------------------------------------------------------------
def frames(text: str) -> list[dict]:
    """Parse an SSE body the way a client reducer would: id first, then the payload."""
    out = []
    for block in text.split("\n\n"):
        fid, payload = None, None
        for line in block.splitlines():
            if line.startswith("id: "):
                fid = int(line[4:])
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if payload is not None:
            out.append({"id": fid, **payload})
    return out


def fake_request(headers: dict | None = None, query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "root_path": "",
                    "scheme": "http", "server": ("test", 80), "client": ("10.0.0.1", 0),
                    "query_string": query.encode(),
                    "headers": [(k.lower().encode(), v.encode())
                                for k, v in (headers or {}).items()]})


async def drain(resp, *, deadline: float = 3.0) -> str:
    """Collect frames until the stream goes quiet (a heartbeat) or ends, then hang up.

    Hanging up mid-stream is the case that used to leak a subscriber on every dropped
    connection, so every call here is also a test of that.
    """
    body, it, t0 = "", resp.body_iterator, time.monotonic()
    try:
        while time.monotonic() - t0 < deadline:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=deadline)
            except (StopAsyncIteration, asyncio.TimeoutError):
                break
            body += chunk if isinstance(chunk, str) else chunk.decode()
            if ": hb" in body:                 # replay exhausted; the stream is now tailing
                break
    finally:
        await it.aclose()
    return body


async def open_stream(pool_id, *, last_event_id=None, from_seq=0):
    headers = {"last-event-id": str(last_event_id)} if last_event_id is not None else {}
    return await main.stream_pool(pool_id, fake_request(headers), from_seq=from_seq)


# =============================================================================
async def endpoints(c, pp, tick, llm):
    raw = {p["email"] for p in cast_mod.load_cast()}

    print("\n-- the cast --")
    r = await c.get("/api/cast")
    body = r.json()
    check("the cast is served", r.status_code == 200 and len(body["cast"]) == 6, body)
    check("by cid and masked address only",
          all(set(p) == {"cid", "name", "email_masked"} for p in body["cast"]), body["cast"])
    check("no real address is handed to the browser",
          not any(e in r.text for e in raw), r.text[:200])

    print("\n-- creating a pool --")
    r = await c.post("/api/pools", json={"goal_text": GOAL, "policy_text": POLICY})
    check("a pool is created",
          r.status_code == 200 and r.json()["pool_id"].startswith("pool_"), r.text)
    pid = r.json()["pool_id"]
    check("it starts in DRAFT", (await store.get_pool(pid))["phase"] == "DRAFT")
    check("the whole cast is seeded", len(await store.get_contributors(pid)) == 6)

    bad = await c.post("/api/pools", json={"goal_text": "short", "policy_text": POLICY})
    check("a goal that says nothing is refused", bad.status_code == 422, bad.status_code)
    bad = await c.post("/api/pools", json={"goal_text": "x" * 5000, "policy_text": POLICY})
    check("an unbounded goal is refused — it goes straight into the prompt",
          bad.status_code == 422, bad.status_code)
    bad = await c.post("/api/pools", json={"goal_text": GOAL, "policy_text": POLICY,
                                           "cids": ["c1"]})
    check("a pool of one person is refused", bad.status_code == 400, bad.status_code)
    r2 = await c.post("/api/pools", json={"goal_text": GOAL, "policy_text": POLICY,
                                          "cids": ["c1", "c2", "r1"]})
    check("a hand-picked roster is honoured",
          r2.status_code == 200
          and len(await store.get_contributors(r2.json()["pool_id"])) == 3)

    print("\n-- the repaint --")
    r = await c.get(f"/api/pools/{pid}")
    proj = r.json()
    check("the projection paints the pool", proj["phase"] == "DRAFT" and len(proj["rows"]) == 6)
    check("it quotes the organizer verbatim",
          proj["goal_text"] == GOAL and proj["policy_text"] == POLICY)
    check("every row is masked", all("***" in row["email_masked"] for row in proj["rows"]))
    check("NO REAL ADDRESS APPEARS IN THE PROJECTION",
          not any(e in r.text for e in raw), [e for e in raw if e in r.text])
    check("it carries a cursor for the stream", proj["last_seq"] >= 1, proj["last_seq"])
    check("an unknown pool is a clean 404",
          (await c.get("/api/pools/pool_nope")).status_code == 404)

    print("\n-- the stream --")
    main.HEARTBEAT_S = 0.2                     # so a quiet stream announces itself promptly
    fr = frames(await drain(await open_stream(pid)))
    check("the stream replays from the start", fr and fr[0]["type"] == "pool_created", fr[:1])
    check("every frame carries an id, which is what makes resume work",
          all(f["id"] for f in fr), fr[:2])
    check("the event name travels in the payload", all("type" in f for f in fr))
    check("the organizer's words reach the stream unparsed",
          fr[0]["goal_text"] == GOAL and fr[0]["policy_text"] == POLICY)
    check("the stream says plainly which PayPal it is talking to",
          fr[0]["paypal_env"] == "sandbox" and fr[0]["mock"] is True, fr[0])
    try:
        await open_stream("pool_nope")
        check("an unknown pool's stream is refused, not left hanging", False, "no 404 raised")
    except HTTPException as exc:
        check("an unknown pool's stream is refused, not left hanging", exc.status_code == 404)

    cursor = fr[-1]["id"]
    await bus_mod.bus.publish(pid, "agent_note", {"text": "after the cursor"})
    resumed = frames(await drain(await open_stream(pid, last_event_id=cursor)))
    check("Last-Event-ID resumes after the cursor, with no replay",
          [f["type"] for f in resumed] == ["agent_note"], [f["type"] for f in resumed])
    q = frames(await drain(await open_stream(pid, from_seq=cursor)))
    check("?from_seq does the same for a hand-driven client",
          [f["type"] for f in q] == ["agent_note"], [f["type"] for f in q])
    check("a frame already delivered is never re-sent",
          all(f["id"] > cursor for f in resumed), [f["id"] for f in resumed])
    check("DROPPING A STREAM LEAVES NO SUBSCRIBER BEHIND",
          bus_mod.bus.subscriber_count(pid) == 0, bus_mod.bus.subscriber_count(pid))

    print("\n-- paying as somebody (sandbox) --")
    r = await c.post(f"/api/pools/{pid}/pay/c1", json={})
    check("you cannot pay an invoice that was never sent", r.status_code == 409, r.status_code)

    ctx = tools.ToolContext(pid, pp, bus_mod.bus)
    planned = await tools.dispatch(ctx, "plan_pool", PLAN)
    check("the pool can be planned from the cast the API seeded",
          planned.get("n") == 5, planned)
    await tools.dispatch(ctx, "send_invoices", {"cids": ["all"], "item_label": "Gift share"})

    r = await c.post(f"/api/pools/{pid}/pay/c1", json={})
    check("paying as a contributor works",
          r.status_code == 200 and r.json()["amount_cents"] == 4500, r.text)
    paid_now = int([i for i in await store.get_invoices(pid) if i["cid"] == "c1"][0]["paid_cents"])
    check("the ledger is NOT written by the button — PayPal is the only definition of paid",
          paid_now == 0, paid_now)
    await tick.tick_once()
    paid_after = int([i for i in await store.get_invoices(pid) if i["cid"] == "c1"][0]["paid_cents"])
    check("the ticker's sweep is what records it", paid_after == 4500, paid_after)

    r = await c.post(f"/api/pools/{pid}/pay/c1", json={})
    check("paying twice is refused", r.status_code == 409, r.status_code)
    r = await c.post(f"/api/pools/{pid}/pay/c2", json={"amount_cents": 2000})
    check("a partial payment is allowed — the agent has to deal with it",
          r.status_code == 200 and r.json()["amount_cents"] == 2000, r.text)
    r = await c.post(f"/api/pools/{pid}/pay/c3", json={"amount_cents": 999_999})
    check("an implausible amount is refused", r.status_code == 400, r.status_code)
    r = await c.post(f"/api/pools/{pid}/pay/nobody", json={})
    check("an unknown cid is refused", r.status_code == 404, r.status_code)

    await tick.tick_once()
    rows = {row["cid"]: row for row in (await c.get(f"/api/pools/{pid}")).json()["rows"]}
    check("a partial payment is flagged for the viewer as well as the agent",
          "PARTIAL" in rows["c2"]["flags"], rows["c2"])
    check("the recipient is marked as such", "RECIPIENT" in rows["r1"]["flags"], rows["r1"])
    check("the repaint agrees with the ledger the ticker wrote",
          rows["c1"]["paid_cents"] == 4500 and rows["c1"]["status"] == "MARKED_AS_PAID",
          rows["c1"])
    check("and it carries the real payer-view link PayPal returned",
          bool(rows["c1"]["payer_url"]), rows["c1"].get("payer_url"))

    print("\n-- talking to the agent --")
    r = await c.post(f"/api/pools/{pid}/message", json={"text": "Sam will pay tonight."})
    check("a human message is accepted", r.status_code == 200, r.text)
    check("and queued for the agent, not acted on inside the request",
          tick.human.get(pid) == ["Sam will pay tonight."], tick.human)
    check("it is on the stream for everyone watching",
          any(e["type"] == "human_message" for e in await store.read_events(pid)))
    r = await c.post(f"/api/pools/{pid}/message", json={"text": ""})
    check("an empty message is refused", r.status_code == 422, r.status_code)

    print("\n-- the terminal sentinel --")
    closed = await c.post("/api/pools", json={"goal_text": GOAL, "policy_text": POLICY})
    cpid = closed.json()["pool_id"]
    await bus_mod.bus.publish(cpid, "pool_closed", {"phase": "CLOSED", "summary": "done"})
    body = await drain(await open_stream(cpid))
    tail = frames(body)
    check("a closed pool's stream ends rather than tailing forever",
          tail and tail[-1]["type"] == "end" and tail[-1]["phase"] == "CLOSED", tail[-1:])
    check("and it closes without a heartbeat first", ": hb" not in body)

    print("\n-- health --")
    h = (await c.get("/api/healthz")).json()
    check("healthz reports the deploy", h["ok"] and h["mock"] is True, h)
    check("and the token headroom, which is the thing that actually fails",
          isinstance(h["tokens_remaining_60s"], int), h)
    check("and how many pools are live", h["active_pools"] >= 1, h)
    check("and that the ticker is actually passing", h["ticker_passes"] >= 1, h)

    print("\n-- nothing leaks --")
    blob = json.dumps(await store.read_events(pid))
    check("NO REAL ADDRESS APPEARS IN ANY EVENT, ANYWHERE",
          not any(e in blob for e in raw), [e for e in raw if e in blob])
    check("the masked form is what is published", "***" in blob)

    print("\n-- the rate limiter --")
    limited = False
    for _ in range(settings.per_ip_per_min + 4):
        rr = await c.post("/api/pools", json={"goal_text": GOAL, "policy_text": POLICY})
        if rr.status_code == 429:
            limited = True
            break
    check("one network cannot create pools without limit", limited)


async def main_test():
    await store.init_db(os.path.join(tempfile.mkdtemp(), "api.db"))
    llm = CountingLLM()
    async with get_paypal_client(on_call=bus_mod.stream_paypal_call) as pp:
        meter = agent_core.TokenMeter(budget=500_000)
        tick = Ticker(pp=pp, llm=llm, meter=meter, bus=bus_mod.bus, interval_s=0.05)
        main.state.update(pp=pp, llm=llm, meter=meter, ticker=tick, started_at=time.time())
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await endpoints(c, pp, tick, llm)
    await store.close_db()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main_test())

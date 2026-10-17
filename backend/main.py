"""Potluck API: create a pool, watch it live, nudge it as a human.

The transport is the part that matters here. Overindex streamed from a single
asyncio.Queue consumed destructively by one generator, which works for exactly one viewer
watching one run to completion — and breaks the moment anyone refreshes. A money pool
lives for days and will be opened late, on a phone, by someone who was sent a link, while
we deploy underneath them. So every event is appended to SQLite first and the stream is a
PROJECTION of that log: `id:` frames, replay from a cursor, and a full-state repaint
endpoint as the safety net if SSE dies entirely.

Run with backend/ as the working directory:
    cd backend && uvicorn main:app --port 8001
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import agent_core
import bus as bus_mod
import cast as cast_mod
import clock
import store
import tools
from bus import bus
from config import settings
from guards import GuardError
from llm import make_llm
from paypal import get_paypal_client
from ticker import Ticker
from tools import mask_email

MAX_GOAL = 600
MAX_POLICY = 400
MAX_MESSAGE = 400
HEARTBEAT_S = 8.0

# Filled in at boot; read by /api/healthz.
state: dict = {"pp": None, "llm": None, "meter": None, "ticker": None, "preflight": None,
               "started_at": 0.0}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Refuse to start against live PayPal without an explicit acknowledgement. Moving real
    # money by accident is a one-way mistake, so it is a boot condition, not a check.
    settings.assert_sandbox()
    await store.init_db()
    pp = get_paypal_client(on_call=bus_mod.stream_paypal_call, default_origin="agent")
    meter = agent_core.TokenMeter()
    llm = make_llm()
    # Every pool this product runs is a demo pool, so the 2-second cadence is the real
    # one here; tick_real_s exists so the code is honest about what it would do against a
    # pool whose deadline is genuinely three days away.
    tick = Ticker(pp=pp, llm=llm, meter=meter, bus=bus, demo=True)
    state.update(pp=pp, llm=llm, meter=meter, ticker=tick, started_at=time.time())
    await tick.start()
    try:
        yield
    finally:
        await tick.stop()
        await pp.aclose()
        await store.close_db()


app = FastAPI(title="Potluck", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)


# --- rate limiting ----------------------------------------------------------
# One window PER BUCKET per IP. Sharing a single window across endpoints meant the cheap
# frequent calls (paying, messaging) consumed the allowance for the expensive rare one
# (creating a pool), so a judge who clicked "pay as Maya" six times could not start a
# second pool.
_ip_windows: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(deque))


def _client_ip(request: Request) -> str:
    """The real client behind nginx. X-Real-IP is set by our own nginx to $remote_addr and
    is trusted; the left-most X-Forwarded-For hop is client-spoofable, so prefer X-Real-IP
    and otherwise take the right-most (proxy-added) hop."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _rate_ok(bucket: str, ip: str, limit: int) -> bool:
    """Sliding-minute gate, per bucket per IP. Prunes its own table."""
    now = time.monotonic()
    window = _ip_windows[bucket]
    if len(window) > 2048:
        for k in [k for k, v in window.items() if not v or now - v[-1] > 60]:
            window.pop(k, None)
    dq = window[ip]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


# --- request bodies ---------------------------------------------------------
class PoolIn(BaseModel):
    goal_text: str = Field(min_length=10, max_length=MAX_GOAL)
    policy_text: str = Field(min_length=5, max_length=MAX_POLICY)
    cids: list[str] | None = None          # which of the cast are in; default: all of them
    demo: bool = True


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE)


class PayIn(BaseModel):
    amount_cents: int | None = None        # omitted = pay the share in full


class ApproveIn(BaseModel):
    # A note is optional: the button sends {} and that must not be a validation error.
    note: str = Field(default="", max_length=MAX_MESSAGE)


# --- projection -------------------------------------------------------------
async def _project(pool: dict) -> dict:
    """The full state of a pool, for the first paint and as the SSE safety net.

    Everything the stream would have told a viewer who had been watching from the start,
    computed from the rows rather than replayed — which is why a judge can open a shared
    link an hour late, or refresh mid-payout, and see the truth.
    """
    pid = pool["pool_id"]
    people = await store.get_contributors(pid)
    invoices = {i["cid"]: i for i in await store.get_invoices(pid)}
    settlement = await store.get_settlement(pid)
    left = clock.time_left_s(pool["deadline_at"])
    scale = int(pool["clock_scale"])
    collected = sum(int(i["paid_cents"]) for i in invoices.values())

    rows = []
    for p in people:
        inv = invoices.get(p["cid"]) or {}
        flags = []
        if int(p["is_recipient"] or 0):
            flags.append("RECIPIENT")
        share, paid = int(p["share_cents"] or 0), int(inv.get("paid_cents") or 0)
        if paid and share and paid < share:
            flags.append("PARTIAL")
        if paid and share and paid > share:
            flags.append("OVERPAID")
        rows.append({
            "cid": p["cid"], "name": p["name"], "email_masked": mask_email(p["email"]),
            "share_cents": share, "status": inv.get("status") or "pending",
            "paid_cents": paid, "paid_at": inv.get("paid_at"), "source": inv.get("source"),
            "invoice_id": inv.get("invoice_id"), "payer_url": inv.get("payer_url"),
            "reminders": int(inv.get("reminders") or 0),
            "last_reminder_at": inv.get("last_reminder_at"), "flags": flags})

    events = await store.tail_events(pid)
    return {
        "pool_id": pid, "phase": pool["phase"],
        "goal_text": pool["goal_text"], "policy_text": pool["policy_text"],
        "success_condition_restated": pool["success_condition_restated"],
        "recipient_cid": pool["recipient_cid"], "target_cents": int(pool["target_cents"]),
        "currency": pool["currency"], "overage": pool["overage"],
        "deadline_at": pool["deadline_at"], "deadline_label": pool["deadline_label"],
        "clock_scale": scale, "time_left_s": left,
        "time_left_label": clock.display_label(left, scale),
        "collected_cents": collected,
        "paid_count": sum(1 for r in rows if r["paid_cents"] > 0),
        "n": len([r for r in rows if "RECIPIENT" not in r["flags"]]),
        "ledger_version": int(pool["ledger_version"]),
        "episodes_used": int(pool["episodes_used"]),
        "rows": rows,
        "settlement": None if not settlement else {
            "decision": settlement["decision"], "reason": settlement["reason"],
            "policy_quote": settlement["policy_quote"],
            "frozen_collected_cents": int(settlement["frozen_collected_cents"]),
            "batch_id": settlement["batch_id"], "batch_status": settlement["batch_status"],
            "sender_batch_id": settlement["sender_batch_id"],
            "items": [{"cid": i["cid"], "name": i["name"],
                       "email_masked": mask_email(i["email"]),
                       "amount_cents": i["amount_cents"], "kind": i["kind"]}
                      for i in settlement["items"]]},
        "last_seq": events[-1]["seq"] if events else 0,
        "events": events,
        "paypal_env": "sandbox", "mock": settings.use_mock_paypal,
    }


async def _pool_or_404(pool_id: str) -> dict:
    pool = await store.get_pool(pool_id)
    if not pool:
        raise HTTPException(404, "unknown pool")
    return pool


# --- endpoints --------------------------------------------------------------
@app.get("/api/cast")
async def get_cast():
    """Who can be in a pool. A cid is all the model ever sees of a person."""
    return {"cast": cast_mod.public_cast(), "max_contributors": settings.max_contributors}


@app.post("/api/pools")
async def create_pool(body: PoolIn, request: Request):
    ip = _client_ip(request)
    if not _rate_ok("create", ip, settings.per_ip_per_min):
        raise HTTPException(429, "Too many pools from your network this minute — "
                                 "give it a few seconds.")
    if await store.count_open_pools() >= settings.max_live_pools:
        raise HTTPException(503, "The demo is running as many pools as it can hold right "
                                 "now. Try again in a minute.")

    roster = cast_mod.load_cast()
    if body.cids:
        wanted = [c for c in roster if c["cid"] in set(body.cids)]
        if len(wanted) < 2:
            raise HTTPException(400, "a pool needs at least two people: a recipient and "
                                     "one contributor")
    else:
        wanted = roster
    if len(wanted) > settings.max_contributors + 1:
        raise HTTPException(400, f"at most {settings.max_contributors} contributors")

    pool_id = await store.create_pool(
        goal_text=body.goal_text.strip(), policy_text=body.policy_text.strip(),
        clock_scale=settings.clock_scale_demo if body.demo else 1, demo=body.demo)
    await store.seed_cast(pool_id, wanted)
    # The organizer's words go on the stream exactly as typed. Nothing parses them, here
    # or anywhere else — the model restates the condition and is held to its restatement.
    await bus.publish(pool_id, "pool_created", {
        "pool_id": pool_id, "goal_text": body.goal_text.strip(),
        "policy_text": body.policy_text.strip(),
        "cast": [{"cid": p["cid"], "name": p["name"], "email_masked": mask_email(p["email"])}
                 for p in wanted],
        "clock_scale": settings.clock_scale_demo if body.demo else 1,
        "paypal_env": "sandbox", "mock": settings.use_mock_paypal,
        "model": getattr(state["llm"], "model", ""), "demo": body.demo})
    return {"pool_id": pool_id}


@app.get("/api/pools/{pool_id}")
async def get_pool(pool_id: str):
    return await _project(await _pool_or_404(pool_id))


@app.get("/api/pools/{pool_id}/stream")
async def stream_pool(pool_id: str, request: Request, from_seq: int = 0):
    await _pool_or_404(pool_id)
    # A reconnecting EventSource sends Last-Event-ID by itself, so resume costs the client
    # nothing; ?from_seq is for anyone driving the stream by hand.
    hdr = request.headers.get("last-event-id")
    if hdr:
        try:
            from_seq = max(from_seq, int(hdr))
        except ValueError:
            pass

    pool = await store.get_pool(pool_id)
    if pool and pool["phase"] == "CLOSED" and from_seq:
        # Opened on a pool that is already over, with a cursor past its final frame.
        # Nothing more will ever be published here, so say so and close rather than
        # holding a socket open sending heartbeats into the void — EventSource would
        # also treat the eventual close as a reason to reconnect, for ever.
        async def done():
            yield f"data: {json.dumps({'type': 'end', 'phase': 'CLOSED'})}\n\n"
        return StreamingResponse(done(), media_type="text/event-stream", headers={
            "X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    async def gen():
        # The subscription is closed explicitly. A dropped connection closes THIS
        # generator, which throws GeneratorExit at the yield below — and that does not
        # propagate into the generator being iterated. Without the aclose() the inner
        # one stays suspended holding its queue until garbage collection notices, which
        # is a leaked subscriber on every refresh, every phone that loses signal and
        # every judge who closes the tab.
        src = bus.stream(pool_id, from_seq=from_seq, idle_timeout=HEARTBEAT_S)
        try:
            async for ev in src:
                if ev is None:
                    yield ": hb\n\n"            # keeps proxies from idling the stream out
                    continue
                # The event name travels INSIDE the payload rather than on an `event:`
                # line: the client dispatches ~35 types through one reducer, and a named
                # SSE event would bypass its onmessage handler. `id:` is the line that
                # matters — it is what makes Last-Event-ID resume work at all.
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev["type"] in ("pool_closed", "escalated"):
                    yield ("data: " + json.dumps(
                        {"type": "end", "phase": ev.get("phase", "CLOSED")}) + "\n\n")
                    return
        finally:
            await src.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "X-Accel-Buffering": "no", "Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.post("/api/pools/{pool_id}/pay/{cid}")
async def pay_as(pool_id: str, cid: str, body: PayIn, request: Request):
    """The 'pay as Maya (sandbox)' button.

    All three ways a contributor can pay — this button, the real payer-view link on the
    invoice, and a merchant-recorded payment — converge on the invoice's own status, so
    the agent cannot tell them apart and nothing in its reasoning depends on which it was.
    """
    if not _rate_ok("pay", _client_ip(request), settings.per_ip_per_min * 4):
        raise HTTPException(429, "slow down a moment")
    pool = await _pool_or_404(pool_id)
    if pool["phase"] not in ("COLLECTING", "RESOLVING"):
        raise HTTPException(409, f"this pool is {pool['phase']} — it is not collecting")
    person = await store.get_contributor(pool_id, cid)
    if not person:
        raise HTTPException(404, "nobody by that cid in this pool")
    invoices = {i["cid"]: i for i in await store.get_invoices(pool_id)}
    inv = invoices.get(cid)
    if not inv or not inv["invoice_id"]:
        raise HTTPException(409, "no invoice has been sent to them yet")
    share = int(person["share_cents"] or 0)
    already = int(inv["paid_cents"] or 0)
    if already >= share and share:
        raise HTTPException(409, "they have already paid")
    amount = int(body.amount_cents or (share - already) or share)
    if not 1 <= amount <= max(1, share * 2):
        raise HTTPException(400, "amount is outside what this invoice could plausibly take")

    pp = state["pp"]
    with bus_mod.pool_scope(pool_id):
        try:
            await pp.record_payment(inv["invoice_id"], amount, origin="human")
        except Exception as exc:                          # noqa: BLE001
            raise HTTPException(502, f"PayPal refused the payment: {str(exc)[:160]}") from exc
    # The ledger is not written here. The ticker's next sweep reads it back from PayPal,
    # which is the only definition of "paid" this system has.
    return {"ok": True, "cid": cid, "amount_cents": amount}


@app.post("/api/pools/{pool_id}/message")
async def message_agent(pool_id: str, body: MessageIn, request: Request):
    if not _rate_ok("message", _client_ip(request), settings.per_ip_per_min * 2):
        raise HTTPException(429, "slow down a moment")
    pool = await _pool_or_404(pool_id)
    if pool["phase"] in ("CLOSED", "ESCALATED"):
        raise HTTPException(409, f"this pool is {pool['phase']}")
    text = body.text.strip()
    await bus.publish(pool_id, "human_message", {"text": text})
    state["ticker"].message(pool_id, text)
    return {"ok": True, "queued": True}


def _preflight() -> dict:
    """What preflight.py last proved, if it has run.

    `paypal_ok` and `payout_ready` on the health endpoint mean a batch actually cleared,
    not that the code compiled — so they can only come from a run that moved real
    sandbox money. The age is reported too: a pre-flight from last week is not evidence
    about today.
    """
    path = Path(__file__).with_name("preflight.json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    at = float(data.get("at") or 0)
    return {"paypal_ok": data.get("paypal_ok"), "payout_ready": data.get("payout_ready"),
            "invoice_ok": data.get("invoice_ok"), "groq_keys_ok": data.get("groq_keys_ok"),
            "preflight_ok": data.get("ok"),
            "preflight_age_s": int(time.time() - at) if at else None}


@app.post("/api/pools/{pool_id}/approve-refund")
async def approve_refund(pool_id: str, request: Request, body: ApproveIn | None = None):
    """The button on an escalated pool's card. The only way a human moves money here.

    An escalated pool is one the agent never resolved — the deadline and its grace window
    passed, or the model was unreachable through both. Code did not decide for it and
    will not: every cent sits exactly where it was, and this endpoint executes the one
    action a human can authorise, through the same settlement path the agent would have
    used. The human supplies an approval, not an amount and not a destination.
    """
    if not _rate_ok("refund", _client_ip(request), 6):
        raise HTTPException(429, "slow down a moment")
    pool = await _pool_or_404(pool_id)
    if pool["phase"] != "ESCALATED":
        raise HTTPException(409, f"this pool is {pool['phase']} — only an escalated pool "
                                 "is waiting on a human")
    note = (body.note.strip() if body and body.note else "")
    ctx = tools.ToolContext(pool_id, state["pp"], bus)
    with bus_mod.pool_scope(pool_id):
        try:
            out = await tools.execute_settlement(
                ctx, pool, decision="UNWIND",
                reason="A human approved returning every contribution after this pool "
                       "escalated undecided." + (f" Note: {note}" if note else ""),
                policy_quote="", decided_by="human", attest=None)
        except GuardError as exc:
            raise HTTPException(409, exc.as_dict()) from exc
        except Exception as exc:                          # noqa: BLE001
            await bus.publish(pool_id, "error", {"where": "approve_refund",
                                                 "message": str(exc)[:200],
                                                 "recoverable": True})
            raise HTTPException(502, f"the refund could not be submitted: {str(exc)[:160]}") from exc
    return out


@app.get("/api/healthz")
async def healthz():
    """Enough to verify a deploy and to watch the token budget. Key values and model names
    are withheld: they only help an abuser size the quota they would need to drain."""
    meter: agent_core.TokenMeter = state["meter"]
    pre = _preflight()
    return {
        "ok": True,
        "mock": settings.use_mock_paypal,
        **pre,
        "groq_keys": len(settings.groq_key_pool()),
        "tokens_remaining_60s": meter.remaining() if meter else None,
        "active_pools": await store.count_open_pools(),
        "last_settlement_at": await store.last_settlement_at(),
        "ticker_passes": state["ticker"].ticks if state.get("ticker") else 0,
        "uptime_s": int(time.time() - state["started_at"]) if state["started_at"] else 0,
    }

"""Event bus — SSE is a projection of a durable, append-only log.

Overindex streamed from a single asyncio.Queue consumed destructively by one generator.
That works for one viewer watching one run to completion, and breaks the moment anyone
refreshes, opens a shared link, watches on a second device, or we deploy mid-pool.

Here every event is appended to SQLite first and then fanned out. A subscriber may join
late, replay from any cursor, and reconnect without losing a frame — which is also what
makes the native EventSource `Last-Event-ID` header work for free on the client.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import store

# Per-subscriber bound. A slow or stalled client must never stall the producer or grow
# memory without limit, so the queue drops its OLDEST frame rather than blocking.
QUEUE_MAX = 256


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscriber_count(self, pool_id: str) -> int:
        return len(self._subs.get(pool_id, ()))

    async def publish(self, pool_id: str, etype: str, data: dict | None = None) -> int:
        """Durable first, fan-out second. Returns the monotonic seq."""
        data = data or {}
        seq = await store.append_event(pool_id, etype, data)
        frame = {"seq": seq, "type": etype, "pool_id": pool_id, "ts": time.time(), **data}
        for q in list(self._subs.get(pool_id, ())):
            if q.full():
                try:
                    q.get_nowait()          # drop oldest
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:        # lost a race with another producer; skip
                pass
        return seq

    def _add_sub(self, pool_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._subs[pool_id].add(q)
        return q

    def _remove_sub(self, pool_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(pool_id)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(pool_id, None)

    @asynccontextmanager
    async def subscribe(self, pool_id: str):
        q = self._add_sub(pool_id)
        try:
            yield q
        finally:
            self._remove_sub(pool_id, q)

    async def stream(self, pool_id: str, from_seq: int = 0, idle_timeout: float = 8.0):
        """Yield frames from `from_seq` onward, then tail live.

        Subscribes BEFORE replaying so nothing published in between is lost; the
        seq filter then discards the overlap. Yields None on idle so the caller can
        emit an SSE heartbeat and notice a dead client.

        Uses an explicit try/finally rather than `async with self.subscribe(...)`:
        an SSE client that disconnects leaves this generator suspended at a yield and
        it is closed with GeneratorExit. An @asynccontextmanager in that position
        raises "generator didn't stop after athrow()" and the subscriber is never
        removed — a leak on every single dropped connection.
        """
        q = self._add_sub(pool_id)
        try:
            last = from_seq
            for ev in await store.read_events(pool_id, from_seq=from_seq):
                last = ev["seq"]
                yield ev
            while True:
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    yield None               # heartbeat tick
                    continue
                if frame["seq"] <= last:     # already delivered during replay
                    continue
                last = frame["seq"]
                yield frame
        finally:
            self._remove_sub(pool_id, q)


bus = EventBus()


# --- Lane B: the PayPal wire ticker ------------------------------------------
# Every REST call we make is streamed as it happens, with an `origin` saying who asked
# for it. That is partly drama — one send_invoices turn becomes sixteen staggered frames —
# and partly the honesty move that makes the rest of the claim checkable: a judge can
# count the POSTs and see that six refunds are six items inside ONE batch call, not six
# calls dressed up as one.
#
# A PayPalClient has a single on_call hook but is shared across pools, so the pool a call
# belongs to travels in a ContextVar. Each pool is served in its own task, and a task
# inherits a copy of the context at creation, so concurrent pools cannot cross-publish.
current_pool: ContextVar[str | None] = ContextVar("current_pool", default=None)


@contextmanager
def pool_scope(pool_id: str | None):
    token = current_pool.set(pool_id)
    try:
        yield
    finally:
        current_pool.reset(token)


async def stream_paypal_call(rec: dict) -> None:
    """The `on_call` hook for PayPalClient: publish the call to whichever pool made it."""
    pool_id = current_pool.get()
    if pool_id:
        await bus.publish(pool_id, "paypal_call", dict(rec))

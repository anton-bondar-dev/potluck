"""The ticker: one code-only task that does all the waiting.

This is the piece that makes the agent cheap AND makes the demo dramatic, which turn out
to be the same thing. Every beat a viewer actually watches is produced here, with no model
in the loop: the ledger filling a row at a time, the countdown draining, the payout
cascade resolving item by item. The model is woken only when something happened that
needs a judgement.

So a routine payment costs ZERO tokens. The third person paying their $45 is not a
decision — it is a status change, and code records it, writes the row and streams the
flash. The model hears about it only if the transition is INTERESTING: the first payment,
the target being crossed, someone it personally nudged coming through, or an amount that
is not the amount that was asked for.

Four more jobs live here because they are code's, not the model's:

  * THE ROW LOCK. A pool is claimed before it is touched, so two workers — or a restart
    overlapping its predecessor — can never act on one pool at once.
  * THE DEADLINE. Reaching it wakes the model with the one trigger it cannot ignore.
    Failing to decide does NOT make code decide: past the grace window the pool escalates
    to a human with every cent left exactly where it is. Nothing here initiates a payout.
  * RECONCILIATION. A PENDING payout batch is resolved by GET, never by re-POST, so a
    crash between "batch created" and "batch confirmed" can never pay twice.
  * RECOVERY. On boot, stale claims are cleared, sleeping agents get their wake times
    back from the episode log, missed deadlines fire, and in-flight batches are read back.
"""
from __future__ import annotations

import asyncio
import json
import random
import time

import agent_core
import bus as bus_mod
import clock
import store
import tools
from bus import bus as event_bus
from config import settings

# How long a pool stays claimed. Must outlast the longest thing we might do while holding
# it — a full episode — or a second worker could start one on top of us.
CLAIM_TTL_S = 60.0

# After an episode that did not park itself with sleep_until (it stalled, errored, or ran
# out of turns), wait this long before spending another one.
RETRY_AFTER_S = 10.0

# The countdown turns red here.
IMMINENT_S = 15


class Ticker:
    """One asyncio task over every live pool. `tick_once` is the whole thing; the loop
    around it is four lines, which is what makes this testable without sleeping."""

    def __init__(self, *, pp, llm, meter: agent_core.TokenMeter, bus=event_bus,
                 interval_s: float | None = None, demo: bool = True):
        self.pp = pp
        self.llm = llm
        self.meter = meter
        self.bus = bus
        self.interval_s = float(interval_s if interval_s is not None
                                else (settings.tick_demo_s if demo else settings.tick_real_s))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Per-pool bookkeeping. All of it is either derivable from the database or safe to
        # lose: a restart rebuilds what matters in recover() and re-learns the rest on the
        # next pass.
        self.seen: dict[str, dict[str, tuple[str, int]]] = {}   # pool -> cid -> (status, paid)
        self.wake_at: dict[str, float] = {}
        self.nudged: dict[str, set[str]] = {}
        self.human: dict[str, list[str]] = {}
        self.human_block: dict[str, float] = {}
        self.deadline_fired: set[str] = set()
        self.warned: set[str] = set()
        self.ticks = 0
        self._pool_sem = asyncio.Semaphore(4)

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        await self.recover()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="potluck-ticker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001 — shutdown is best-effort
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:                      # noqa: BLE001
                # A ticker that dies takes every live pool with it. One pool's failure is
                # never allowed past this line.
                print(f"[ticker] pass failed: {type(exc).__name__}: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    def message(self, pool_id: str, text: str) -> None:
        """A human spoke to the agent. Queued, not acted on — the next pass delivers it."""
        self.human.setdefault(pool_id, []).append(text)

    # --- recovery -----------------------------------------------------------
    async def recover(self) -> dict:
        """Pick up where a killed process left off. Returns a summary for /api/healthz."""
        pools = await store.live_pools()
        reclaimed = resumed = reconciled = 0
        for pool in pools:
            pid = pool["pool_id"]
            if float(pool["claimed_until"] or 0) > time.time():
                # A claim held by a process that no longer exists. Ours now.
                await store.release_pool(pid)
                reclaimed += 1
            invoices = await store.get_invoices(pid)
            self.seen[pid] = {i["cid"]: (i["status"], int(i["paid_cents"])) for i in invoices}
            self.nudged[pid] = {i["cid"] for i in invoices if int(i["reminders"] or 0) > 0}
            if await self._restore_sleep(pid):
                resumed += 1
            if pool["phase"] == "SETTLING" and await self._reconcile(pool):
                reconciled += 1
        await store.prune_events()
        return {"pools": len(pools), "reclaimed": reclaimed, "sleeps_restored": resumed,
                "batches_reconciled": reconciled}

    async def _restore_sleep(self, pool_id: str) -> bool:
        """Give a sleeping agent its wake time back.

        The sleep lives in the episode log — `sleep_until`'s own persisted tool_call — so
        nothing extra had to be stored for this to survive a restart. A sleep whose time
        already passed simply wakes on the next pass.
        """
        ep = await store.last_episode(pool_id)
        if not ep or ep.get("reason") != "sleep" or not ep.get("messages_json"):
            return False
        try:
            tail = json.loads(ep["messages_json"])[-1]
        except (TypeError, ValueError, IndexError):
            return False
        if tail.get("role") != agent_core.PENDING_SLEEP:
            return False
        self.wake_at[pool_id] = float(ep["ended_at"] or time.time()) + int(tail.get("sleep_s") or 0)
        return True

    async def _reconcile(self, pool: dict) -> bool:
        """Read an in-flight payout batch back. By GET — never by re-POST."""
        s = await store.get_settlement(pool["pool_id"])
        if not s or not s.get("batch_id") or s.get("batch_status") == "SUCCESS":
            return False
        ctx = tools.ToolContext(pool["pool_id"], self.pp, self.bus)
        with bus_mod.pool_scope(pool["pool_id"]):
            await tools.poll_payout(ctx, {}, pool, origin="ticker")
        return True

    # --- one pass -----------------------------------------------------------
    async def tick_once(self) -> list[dict]:
        """One pass over every live pool. Returns what it did, which is what the tests read.

        Pools are served concurrently, a few at a time. Serially would be simpler, but a
        judge's pool stuck behind three others would see its own countdown stutter — and
        the countdown is the demo. Each pool runs in its own task, which is also what
        keeps the `paypal_call` context variable from crossing between them.
        """
        self.ticks += 1
        pools = await store.live_pools()
        done = await asyncio.gather(*[self._serve(p) for p in pools], return_exceptions=True)
        out = []
        for pool, did in zip(pools, done):
            if isinstance(did, BaseException):
                did = {"pool_id": pool["pool_id"],
                       "error": f"{type(did).__name__}: {did}"}
                print(f"[ticker] {pool['pool_id']}: {did['error']}")
            if did:
                out.append(did)
        return out

    async def _serve(self, pool: dict) -> dict | None:
        pid = pool["pool_id"]
        async with self._pool_sem:
            if not await store.claim_pool(pid, CLAIM_TTL_S):
                return None                               # someone else has it; no harm done
            try:
                with bus_mod.pool_scope(pid):
                    return await self._work(pool)
            finally:
                await store.release_pool(pid)

    async def _work(self, pool: dict) -> dict | None:
        pid, phase = pool["pool_id"], pool["phase"]
        ctx = tools.ToolContext(pid, self.pp, self.bus)

        # 1. The clock. Published every pass whether or not anything else happens, because
        #    a draining countdown is what makes the wait legible instead of looking hung.
        if phase in ("COLLECTING", "RESOLVING") and pool["deadline_at"]:
            await self._publish_clock(pool)

        # 2. The ledger sweep — the zero-token part. Poll what is not yet terminal, write
        #    what changed, stream the rows.
        changes: list[dict] = []
        if phase in ("COLLECTING", "RESOLVING"):
            changes, snap = await self._sweep(ctx, pid)
        else:
            snap = await tools.ledger_snapshot(ctx)

        # 3. An in-flight batch resolves itself, with no model turn spent on watching it.
        if phase == "SETTLING":
            await self._reconcile(pool)

        # 4. Freeze rather than guess: past the deadline AND past the grace, a pool that
        #    still has not been resolved goes to a human. Code does not decide for it.
        if phase in ("COLLECTING", "RESOLVING") and self._out_of_time(pool):
            await self._escalate(ctx, pool, snap,
                                 "the deadline and its grace window passed without a decision")
            return {"pool_id": pid, "action": "escalated"}
        if pool["demo"] and time.time() - float(pool["created_at"]) > 86400:
            await self._escalate(ctx, pool, snap, "demo pool expired (24h)")
            return {"pool_id": pid, "action": "expired"}

        # 5. Is any of this worth paying a model to look at?
        trigger, why = self._trigger(pool, snap, changes)
        if not trigger:
            return {"pool_id": pid, "action": "swept", "changes": len(changes)} if changes else None

        human = self.human.pop(pid, None) if trigger == "human" else None
        res = await agent_core.run_episode(
            pool_id=pid, trigger=trigger, pp=self.pp, llm=self.llm, meter=self.meter,
            bus=self.bus, changes=changes or None,
            human_message="\n\n".join(human) if human else None)
        self._schedule_next(pid, res)
        if human and not res.ran:
            # Undelivered. Put it back, but do not retry on every tick — a starved token
            # window would otherwise spin on it.
            self.human.setdefault(pid, [])[0:0] = human
            self.human_block[pid] = time.time() + max(RETRY_AFTER_S, res.retry_in_s)
        return {"pool_id": pid, "action": "episode", "trigger": trigger, "why": why,
                "reason": res.reason, "ran": res.ran}

    # --- the pieces ---------------------------------------------------------
    async def _publish_clock(self, pool: dict) -> None:
        left = clock.time_left_s(pool["deadline_at"])
        scale = int(pool["clock_scale"])
        total = max(1, int(float(pool["deadline_at"]) - float(pool["created_at"])))
        await self.bus.publish(pool["pool_id"], "clock_tick", {
            "time_left_s": left, "time_left_label": clock.display_label(left, scale),
            "pct_elapsed": min(100, int(100 * (total - left) / total))})
        if left <= IMMINENT_S and pool["pool_id"] not in self.warned:
            self.warned.add(pool["pool_id"])
            await self.bus.publish(pool["pool_id"], "deadline_imminent", {"time_left_s": left})

    async def _sweep(self, ctx: tools.ToolContext, pid: str) -> tuple[list[dict], dict]:
        """Poll PayPal, write the ledger, stream the drum beat. No LLM.

        The before/after diff is taken here rather than inside refresh_from_paypal because
        it is the TICKER's "what changed since I last looked" that decides whether to wake
        anyone — and that is not the same question as "what did this one poll change".
        """
        people = {c["cid"]: c for c in await store.get_contributors(pid)}
        before = self.seen.get(pid) or {
            i["cid"]: (i["status"], int(i["paid_cents"])) for i in await store.get_invoices(pid)}

        # Jitter: N invoices polled at exactly the same instant, every tick, is a
        # thundering herd against one rate-limited API. The semaphore inside ToolContext
        # bounds concurrency; this spreads the passes.
        await asyncio.sleep(random.uniform(0, 0.25))
        snap = await tools.refresh_from_paypal(ctx, origin="ticker")

        rows = await store.get_invoices(pid)
        after = {i["cid"]: (i["status"], int(i["paid_cents"])) for i in rows}
        self.seen[pid] = after
        # Re-read who has been nudged. The agent may have reminded someone in the episode
        # that just ran, and their payment is exactly the transition worth waking it for.
        self.nudged[pid] = {r["cid"] for r in rows if int(r["reminders"] or 0) > 0}

        changes = []
        for cid, (status, paid) in after.items():
            was_status, was_paid = before.get(cid, ("pending", 0))
            if (status, paid) == (was_status, was_paid):
                continue
            who = people.get(cid, {})
            ch = {"cid": cid, "name": who.get("name"), "from": was_status, "to": status,
                  "paid_cents": paid, "was_paid_cents": was_paid,
                  "share_cents": int(who.get("share_cents") or 0)}
            changes.append(ch)
            if paid > was_paid:
                await self.bus.publish(pid, "payment_detected", {
                    "cid": cid, "name": who.get("name"), "paid_cents": paid,
                    "invoice_status": status, "source": "paypal",
                    "pool_version": snap["ledger_version"]})
        if changes:
            await self.bus.publish(pid, "pool_totals", snap)
        return changes, snap

    def _out_of_time(self, pool: dict) -> bool:
        """Past the deadline and out of either patience or model budget."""
        if not pool["deadline_at"]:
            return False
        if int(pool["episodes_used"]) >= settings.max_episodes:
            return clock.reached(pool["deadline_at"])
        return clock.reached(pool["deadline_at"], settings.deadline_grace_s)

    def _interesting(self, pid: str, pool: dict, snap: dict, changes: list[dict]) -> str | None:
        """Which payment transitions are worth a model turn — and which are just bookkeeping.

        This is the single biggest token decision in the system. With eight contributors,
        waking on every change costs eight extra episodes for a pool whose middle is, by
        design, uneventful.
        """
        arrived = [c for c in changes if c["paid_cents"] > c["was_paid_cents"]]
        if not arrived:
            return None
        before_count = snap["paid_count"] - sum(1 for c in arrived if c["was_paid_cents"] == 0)
        if before_count <= 0:
            return "first payment arrived"
        target = snap["target_cents"]
        before_collected = snap["collected_cents"] - sum(
            c["paid_cents"] - c["was_paid_cents"] for c in arrived)
        if before_collected < target <= snap["collected_cents"]:
            return "the target was crossed"
        nudged = self.nudged.get(pid) or set()
        if any(c["cid"] in nudged for c in arrived):
            return "someone the agent nudged came through"
        for c in arrived:
            if c["share_cents"] and c["paid_cents"] != c["share_cents"]:
                return ("a partial payment" if c["paid_cents"] < c["share_cents"]
                        else "an overpayment")
        return None                                       # routine. Costs nothing.

    def _trigger(self, pool: dict, snap: dict, changes: list[dict]) -> tuple[str | None, str]:
        pid, phase = pool["pool_id"], pool["phase"]
        if self.human.get(pid) and time.time() >= self.human_block.get(pid, 0):
            return "human", "a human sent a message"
        if phase in ("COLLECTING", "RESOLVING") and snap["deadline_reached"]:
            # Reached once is a trigger; reached for the ninetieth consecutive tick is not.
            # Without this the 2-second demo cadence would spend the pool's entire episode
            # budget in under half a minute and never get to the grace window.
            if pid not in self.deadline_fired:
                self.deadline_fired.add(pid)
                return "deadline", "the deadline has been reached"
            if self._due(pid):
                return "deadline", "still undecided past the deadline"
            return None, ""
        if phase == "DRAFT":
            if int(pool["episodes_used"]) == 0:
                return "pool_created", "a new pool needs planning"
            return ("tick", "retrying the plan") if self._due(pid) else (None, "")
        why = self._interesting(pid, pool, snap, changes)
        if why:
            return "payment", why
        if self._due(pid):
            return "tick", "the window the agent asked for has elapsed"
        return None, ""

    def _due(self, pid: str) -> bool:
        at = self.wake_at.get(pid)
        return at is not None and time.time() >= at

    def _schedule_next(self, pid: str, res: agent_core.EpisodeResult) -> None:
        """When this pool may next cost a token."""
        if res.reason == "sleep" and res.sleep_s:
            self.wake_at[pid] = time.time() + res.sleep_s
        elif res.reason == "max_episodes":
            self.wake_at.pop(pid, None)                   # no more episodes, ever
        elif res.reason == "tpm":
            self.wake_at[pid] = time.time() + max(1, res.retry_in_s)
        elif res.reason == "settled":
            self.wake_at[pid] = time.time() + 2           # the batch needs a moment
        else:
            self.wake_at[pid] = time.time() + RETRY_AFTER_S

    async def _escalate(self, ctx: tools.ToolContext, pool: dict, snap: dict,
                        reason: str) -> None:
        """Freeze the pool and hand it to a human. The one thing this must not do is pay.

        A code-executed failsafe UNWIND would be exactly the hardcoded money-moving branch
        this architecture exists to not have — and it is not even obviously safe, since a
        policy like "release if we're within $20, I'll cover the rest" means a refund
        overrides the organizer. So: cancel what is unpaid (freeze before anything else),
        leave every cent where it is, and publish a snapshot with a button attached.
        """
        pid, phase = pool["pool_id"], pool["phase"]
        cancelled = []
        for inv in await store.get_invoices(pid):
            if int(inv["paid_cents"]) > 0 or inv["status"] == "CANCELLED" or not inv["invoice_id"]:
                continue
            try:
                await ctx.pp.cancel_invoice(inv["invoice_id"], origin="ticker")
            except Exception as exc:                      # noqa: BLE001
                await self.bus.publish(pid, "error", {
                    "where": "escalate.cancel", "message": str(exc)[:160], "recoverable": True})
                continue
            await store.set_invoice_state(pid, inv["cid"], status="CANCELLED",
                                          paid_cents=0, source=None)
            cancelled.append(inv["cid"])
            await self.bus.publish(pid, "invoice_cancelled", {
                "cid": inv["cid"], "invoice_id": inv["invoice_id"]})

        if not await store.advance(pid, phase, "ESCALATED"):
            return                                        # someone resolved it underneath us
        await self.bus.publish(pid, "phase", {"from": phase, "to": "ESCALATED",
                                              "reason": reason})
        await self.bus.publish(pid, "escalated", {
            "reason": reason, "snapshot": {**snap, "cancelled": cancelled},
            "proposed_action": "refund_everyone",
            "note": "No money has moved. Refunding every contributor exactly what they paid "
                    "needs one human approval."})
        self.wake_at.pop(pid, None)

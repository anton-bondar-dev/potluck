"""Drive one pool end to end against a running server, and print what a viewer sees.

This is the rehearsal harness. It creates a pool, tails the SSE stream, pays as whichever
contributors you name at whatever moment you choose, and stops when the pool closes — so
the run that happens on camera is a run that has already happened here, several times,
with the real model and the real PayPal sandbox.

    cd backend && ./.venv/bin/python live_demo.py                        # 4 of 5 pay -> RELEASE
    cd backend && ./.venv/bin/python live_demo.py --pay c1,c2            # short -> UNWIND
    cd backend && ./.venv/bin/python live_demo.py --pay c1,c2,c3,c4,c5 --partial c3:2000

The lanes are printed the way the UI lays them out: the agent's own trace, the PayPal
wire, and the ledger. What you are watching for is the branch — and that the model, not
this script and not the server, is the thing that chose it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx

GOAL = ("We're getting Dana a leaving present. Five of us, $45 each, so $180 in total. "
        "I need it by Friday morning because the party is Friday lunchtime.")
POLICY = ("Only buy the gift if we actually reach $180 by Friday. If we fall short, give "
          "everyone their money back — I'd rather no gift than a half-paid one.")

LANE = {
    "pool_created": "A", "phase": "A", "episode_start": "A", "thought": "A",
    "model_turn": "A", "step_start": "A", "step_result": "A", "guard_block": "A",
    "agent_note": "A", "episode_end": "A", "episode_deferred": "A", "budget": "A",
    "error": "A", "human_message": "A",
    "paypal_call": "B",
    "ledger_init": "C", "ledger_row": "C", "pool_totals": "C", "clock_tick": "C",
    "payment_detected": "C", "reminder_sent": "C", "agent_sleep": "C", "agent_wake": "C",
    "deadline_imminent": "C", "deadline_reached": "C",
    "decision": "D", "fact_check": "D", "invoice_cancelled": "D", "payout_submitted": "D",
    "payout_item": "D", "payout_settled": "D", "late_payment": "D", "receipt": "D",
    "pool_closed": "D", "escalated": "D",
}
COLOUR = {"A": "\033[36m", "B": "\033[90m", "C": "\033[33m", "D": "\033[1;35m"}
RESET = "\033[0m"


def money(c) -> str:
    c = int(c or 0)
    return f"${c // 100}.{c % 100:02d}"


def line(ev: dict) -> str | None:
    t = ev.get("type", "?")
    if t == "clock_tick":
        return None                                   # one per tick; too noisy to print
    if t == "pool_created":
        return f"pool created · {len(ev.get('cast') or [])} people · scale x{ev.get('clock_scale')}"
    if t == "phase":
        return f"PHASE {ev['from']} -> {ev['to']}  ({ev.get('reason')})"
    if t == "episode_start":
        s = ev.get("snapshot") or {}
        return (f"episode woken by {ev['trigger']} · {money(s.get('collected_cents'))} of "
                f"{money(s.get('target_cents'))} · {s.get('time_left_s')}s left")
    if t == "thought":
        return "thinking: " + " ".join(ev["text"].split())[:220]
    if t == "agent_note":
        return "says: " + " ".join(ev["text"].split())[:220]
    if t == "model_turn":
        return (f"turn {ev['turn']} · {ev['prompt_tokens']}+{ev['completion_tokens']} tokens · "
                f"{ev['ms']}ms · {ev['n_tool_calls']} call(s)")
    if t == "step_start":
        return f"-> {ev['tool']}  {json.dumps(ev.get('args'), ensure_ascii=False)[:260]}"
    if t == "step_result":
        return f"<- {'ok' if ev.get('ok') else 'BLOCKED'}  {ev.get('summary')}"
    if t == "guard_block":
        return f"GUARD {ev['rule']}: {ev.get('detail')}"
    if t == "episode_end":
        return (f"episode end · {ev['turns']} turns · {ev['prompt_tokens']}+"
                f"{ev['completion_tokens']} tokens · {ev['reason']}")
    if t == "episode_deferred":
        return f"episode DEFERRED ({ev['reason']}, retry in {ev.get('retry_in_s')}s)"
    if t == "paypal_call":
        return f"{ev['method']:4} {ev['path']:<46} {ev['status']} {ev['ms']:>5}ms  [{ev['origin']}]"
    if t == "ledger_init":
        return (f"ledger: {len(ev['rows'])} rows, target {money(ev['target_cents'])}, "
                f"to {ev['recipient_name']} · restated: \"{ev.get('success_condition_restated')}\"")
    if t == "ledger_row":
        return f"row {ev['cid']} {ev.get('name')}: {ev.get('status')} {money(ev.get('paid_cents'))}"
    if t == "pool_totals":
        return (f"TOTALS {money(ev['collected_cents'])} / {money(ev['target_cents'])} · "
                f"{ev['paid_count']}/{ev['n']} paid · {ev['time_left_s']}s left "
                f"({ev.get('time_left_label')})")
    if t == "payment_detected":
        return f"PAYMENT {ev.get('name')} {money(ev['paid_cents'])} ({ev.get('invoice_status')})"
    if t == "reminder_sent":
        return f"nudge -> {ev.get('name')}: \"{ev.get('note')}\""
    if t == "agent_sleep":
        return f"asleep {ev['sleep_s']}s ({ev.get('until_label')}) · {ev.get('reason')}"
    if t == "agent_wake":
        return f"awake ({ev['trigger']}) · {len(ev.get('changes') or [])} change(s)"
    if t == "deadline_imminent":
        return f"DEADLINE IN {ev['time_left_s']}s"
    if t == "decision":
        return (f"*** {ev['decision']} *** by {ev.get('by')} · attested={ev.get('attested')}\n"
                f"      reason: {ev.get('reason')}\n"
                f"      quote:  \"{ev.get('policy_quote')}\"")
    if t == "fact_check":
        return f"ATTESTED against PayPal · model said {ev.get('model_said')}"
    if t == "invoice_cancelled":
        return f"cancelled {ev.get('name') or ev.get('cid')}'s invoice"
    if t == "payout_submitted":
        items = ", ".join(f"{i.get('name')} {money(i['amount_cents'])}" for i in ev["items"])
        return (f"PAYOUT BATCH {ev['batch_id']} · {ev['item_count']} items · "
                f"{money(ev['total_cents'])}\n      {items}")
    if t == "payout_item":
        return f"item {ev.get('name') or ev.get('email_masked')} {money(ev.get('amount_cents'))} -> {ev['item_status']}"
    if t == "payout_settled":
        return (f"batch {ev['batch_status']} · {ev['succeeded']} success, {ev['pending']} pending, "
                f"{ev['unclaimed']} unclaimed, {ev['failed']} failed")
    if t == "late_payment":
        return f"LATE {ev.get('name')} {money(ev['paid_cents'])} -> {ev['action']}"
    if t == "pool_closed":
        return f"CLOSED by {ev.get('by', 'agent')}: {ev.get('summary')}"
    if t == "escalated":
        return f"ESCALATED: {ev['reason']} · proposed: {ev.get('proposed_action')}"
    if t == "end":
        return f"--- stream end ({ev.get('phase')}) ---"
    return json.dumps({k: v for k, v in ev.items() if k not in ("seq", "ts", "pool_id")})[:200]


async def pay_when_invoiced(c, base, pool_id, plan, state):
    """Pay as the named contributors once their invoices exist, staggered like people."""
    for cid, amount in plan:
        while not state["invoiced"] and not state["done"]:
            await asyncio.sleep(0.5)
        if state["done"]:
            return
        await asyncio.sleep(state["stagger"])
        body = {"amount_cents": amount} if amount else {}
        try:
            r = await c.post(f"{base}/api/pools/{pool_id}/pay/{cid}", json=body)
            tag = "partial " + money(amount) if amount else "in full"
            print(f"\033[32m[you]  paid as {cid} {tag} -> {r.status_code}{RESET}", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"[you]  pay as {cid} failed: {exc}", flush=True)


async def run(args) -> int:
    base = args.url.rstrip("/")
    plan: list[tuple[str, int | None]] = []
    partial = dict(p.split(":") for p in args.partial.split(",") if ":" in p)
    for cid in [c.strip() for c in args.pay.split(",") if c.strip()]:
        plan.append((cid, int(partial[cid]) if cid in partial else None))

    async with httpx.AsyncClient(timeout=30.0) as c:
        h = (await c.get(f"{base}/api/healthz")).json()
        print(f"server: mock={h['mock']} payout_ready={h['payout_ready']} "
              f"groq_keys={h['groq_keys']}\n")
        r = await c.post(f"{base}/api/pools", json={"goal_text": GOAL, "policy_text": POLICY,
                                                    "demo": True})
        if r.status_code != 200:
            print("could not create a pool:", r.status_code, r.text[:200])
            return 1
        pool_id = r.json()["pool_id"]
        print(f"pool {pool_id}\n{'-' * 100}", flush=True)

        state = {"invoiced": False, "done": False, "stagger": args.stagger}
        payer = asyncio.create_task(pay_when_invoiced(c, base, pool_id, plan, state))
        t0, tokens, calls = time.monotonic(), 0, 0
        decision = None
        try:
            async with c.stream("GET", f"{base}/api/pools/{pool_id}/stream",
                                timeout=httpx.Timeout(None)) as resp:
                async for raw in resp.aiter_lines():
                    if not raw.startswith("data: "):
                        continue
                    ev = json.loads(raw[6:])
                    t = ev.get("type")
                    if t == "model_turn":
                        tokens += ev["prompt_tokens"] + ev["completion_tokens"]
                    if t == "paypal_call":
                        calls += 1
                    if t == "step_result" and "invoice" in (ev.get("summary") or ""):
                        state["invoiced"] = True
                    if t == "decision":
                        decision = ev["decision"]
                    text = line(ev)
                    if text:
                        lane = LANE.get(t, "A")
                        print(f"{COLOUR[lane]}{time.monotonic() - t0:6.1f}s {lane} | {text}{RESET}",
                              flush=True)
                    if t in ("end", "escalated") or time.monotonic() - t0 > args.timeout:
                        break
        finally:
            state["done"] = True
            payer.cancel()

        print(f"{'-' * 100}")
        final = (await c.get(f"{base}/api/pools/{pool_id}")).json()
        print(f"phase {final['phase']} · collected {money(final['collected_cents'])} of "
              f"{money(final['target_cents'])} · {final['paid_count']}/{final['n']} paid")
        print(f"decision {decision} · {final['episodes_used']} episodes · ~{tokens} LLM tokens · "
              f"{calls} real PayPal calls · {time.monotonic() - t0:.0f}s wall clock")
        if final.get("settlement"):
            s = final["settlement"]
            print(f"settlement {s['decision']} · batch {s['batch_id']} {s['batch_status']} · "
                  f"{len(s['items'])} items · "
                  f"{money(sum(i['amount_cents'] for i in s['items']))}")
            delta = final["collected_cents"] - sum(i["amount_cents"] for i in s["items"])
            print(f"conservation: collected - paid out = {money(delta)}"
                  f"{'  (OK)' if delta == 0 else '  <-- RETAINED, must be explained'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Rehearse one Potluck pool end to end.")
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--pay", default="c1,c2,c3,c4", help="cids that pay, in order")
    ap.add_argument("--partial", default="", help="e.g. c3:2000 to pay only $20.00")
    ap.add_argument("--stagger", type=float, default=6.0, help="seconds between payments")
    ap.add_argument("--timeout", type=float, default=300.0)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

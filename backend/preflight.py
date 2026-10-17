"""Pre-flight: prove the rails are hot before anyone watches.

The largest unverified assumption in this project was never "does the agent reason well".
It was whether a payout batch reaches a terminal per-item status at all — because there is
no balance API on our scopes, an item sent to an address with no PayPal account sits
UNCLAIMED for thirty days, and that uncertainty sits directly under the climax of the
demo. A canary batch is the only proof available, so this script exists to run it on
purpose, cheaply, before it matters.

    cd backend && ./.venv/bin/python preflight.py              # everything but the payout
    cd backend && ./.venv/bin/python preflight.py --canary     # also moves 2 cents
    cd backend && ./.venv/bin/python preflight.py --canary --url https://potluck.overpro.fit

What it checks, in the order a failure would matter:
  1. config      the sandbox pin, and that credentials exist at all
  2. oauth       a token can be minted
  3. invoicing   create -> send -> get -> record -> get, and the PAID-AMOUNT JSON PATH,
                 which every refund amount is derived from and which nobody can see
                 from the documentation
  4. payouts     a two-item one-cent batch polled to a terminal per-item status
  5. groq        every key in the pool, so a dead key is found now and not at a deadline
  6. http        the deployed box: /api/healthz, and a real SSE socket that streams

The result is written to preflight.json, which /api/healthz reports, so "payout_ready"
on the health endpoint means a batch actually cleared rather than that the code compiled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

import cast as cast_mod
from config import settings
from llm import GroqPool, LLMError
from paypal import cents_to_str, get_paypal_client, paid_cents_from_invoice

RESULT_FILE = Path(__file__).with_name("preflight.json")
CANARY_CENTS = 1
POLL_TIMEOUT_S = 90
TERMINAL = {"SUCCESS", "FAILED", "RETURNED", "BLOCKED", "REFUNDED", "UNCLAIMED", "DENIED"}

OK, BAD, SKIP = "PASS", "FAIL", "skip"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.facts: dict = {}

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        mark = {OK: "  ok  ", BAD: " FAIL ", SKIP: " skip "}[status]
        print(f"{mark} {name:<34} {detail}")

    @property
    def failed(self) -> list[str]:
        return [n for n, s, _ in self.rows if s == BAD]

    def write(self) -> None:
        payload = {
            "ok": not self.failed, "at": time.time(),
            "mock": settings.use_mock_paypal,
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in self.rows],
            **self.facts,
        }
        RESULT_FILE.write_text(json.dumps(payload, indent=2))
        print(f"\nwritten to {RESULT_FILE}")


async def check_config(rep: Report) -> None:
    try:
        settings.assert_sandbox()
        rep.add("config: sandbox pin", OK, settings.paypal_base_url)
    except RuntimeError as exc:
        rep.add("config: sandbox pin", BAD, str(exc))
    if settings.use_mock_paypal:
        rep.add("config: PayPal mode", SKIP, "USE_MOCK_PAYPAL=1 — nothing below touches PayPal")
    elif settings.paypal_client_id and settings.paypal_secret:
        rep.add("config: credentials", OK, f"client id ends {settings.paypal_client_id[-6:]}")
    else:
        rep.add("config: credentials", BAD, "PAYPAL_CLIENT_ID / PAYPAL_SECRET are empty")
    keys = settings.groq_key_pool()
    rep.add("config: Groq keys", OK if keys else BAD, f"{len(keys)} in the pool")

    roster = cast_mod.load_cast()
    rep.add("config: cast", OK if len(roster) >= 3 else BAD,
            f"{len(roster)} people, recipient {roster[-1]['name'] if roster else '—'}")
    rep.facts["cast_size"] = len(roster)


async def check_paypal(rep: Report, pp, *, canary: bool) -> None:
    roster = cast_mod.load_cast()
    if len(roster) < 3:
        rep.add("paypal", BAD, "the cast is too small to test with")
        return

    t0 = time.monotonic()
    try:
        tok = await pp._access_token()
        if settings.use_mock_paypal:
            rep.add("oauth: token", SKIP, "mock mode mints nothing and touches no network")
        else:
            rep.add("oauth: token", OK if tok else BAD,
                    f"{int((time.monotonic() - t0) * 1000)}ms")
    except Exception as exc:                              # noqa: BLE001
        rep.add("oauth: token", BAD, f"{type(exc).__name__}: {str(exc)[:120]}")
        return

    payer = roster[0]
    try:
        iid = await pp.create_invoice(payer["email"], CANARY_CENTS, note="Potluck pre-flight",
                                      item_name="Pre-flight", origin="settle")
        rep.add("invoicing: create", OK, iid)
        url = await pp.send_invoice(iid, origin="settle")
        rep.add("invoicing: send", OK if url else BAD, (url or "no payer URL returned")[:72])
        body = await pp.get_invoice(iid, origin="settle")
        rep.add("invoicing: get", OK if body.get("status") == "SENT" else BAD,
                f"status {body.get('status')}")
        await pp.record_payment(iid, CANARY_CENTS, origin="settle")
        body = await pp.get_invoice(iid, origin="settle")
        parsed = paid_cents_from_invoice(body)
        # THE PIN. Every refund amount is derived from this one parse, and the live
        # response shape is not in the documentation. If it ever moves, it must fail here
        # and not in the settle path.
        rep.add("invoicing: PAID-AMOUNT JSON PATH",
                OK if parsed == CANARY_CENTS else BAD,
                f"recorded {CANARY_CENTS}c, parsed {parsed}c, status {body.get('status')}")
        rep.facts["invoice_ok"] = parsed == CANARY_CENTS
        rep.facts["paypal_ok"] = True
    except Exception as exc:                              # noqa: BLE001
        rep.add("invoicing", BAD, f"{type(exc).__name__}: {str(exc)[:140]}")
        rep.facts["paypal_ok"] = False
        return

    if not canary:
        rep.add("payouts: canary batch", SKIP, "pass --canary to move two cents")
        return

    receivers = [p for p in roster[:-1]][:2] or roster[:2]
    # Stable within the minute, new across runs: an accidental double-run dedupes at
    # PayPal instead of paying twice, but a deliberate re-check gets a fresh batch.
    sbid = f"potluck-preflight-{time.strftime('%Y%m%d%H%M', time.gmtime())}"
    try:
        batch = await pp.create_payout(
            sbid, [{"email": p["email"], "amount_cents": CANARY_CENTS,
                    "note": "Potluck pre-flight", "sender_item_id": p["cid"]}
                   for p in receivers],
            subject="Potluck pre-flight", origin="settle")
    except Exception as exc:                              # noqa: BLE001
        rep.add("payouts: canary batch", BAD, f"{type(exc).__name__}: {str(exc)[:140]}")
        rep.facts["payout_ready"] = False
        return
    batch_id = batch.get("payout_batch_id", "")
    rep.add("payouts: canary batch", OK,
            f"{batch_id} {batch.get('batch_status')} · {len(receivers)} x {cents_to_str(CANARY_CENTS)}")

    deadline = time.monotonic() + POLL_TIMEOUT_S
    statuses: list[str] = []
    while time.monotonic() < deadline:
        body = await pp.get_payout(batch_id, origin="settle")
        statuses = [i.get("transaction_status", "PENDING") for i in (body.get("items") or [])]
        header = (body.get("batch_header") or {}).get("batch_status")
        if statuses and all(s in TERMINAL for s in statuses):
            break
        await asyncio.sleep(3)
    else:
        header = None

    terminal = bool(statuses) and all(s in TERMINAL for s in statuses)
    cleared = terminal and all(s == "SUCCESS" for s in statuses)
    rep.add("payouts: items reach a terminal state", OK if terminal else BAD,
            f"batch {header} · items {statuses or 'none'}")
    if terminal and not cleared:
        # This is the honest-degradation case the architecture planned for: the on-screen
        # claim becomes "payout batch created · N items" with real per-item statuses, and
        # the film never says "refunds completed" unless the API said so.
        rep.add("payouts: items CLEARED", BAD,
                f"{statuses} — the finale must not claim refunds completed")
    elif cleared:
        rep.add("payouts: items CLEARED", OK, "SUCCESS on every item")
    rep.facts["payout_ready"] = cleared
    rep.facts["payout_statuses"] = statuses
    rep.facts["payout_batch_id"] = batch_id


async def check_groq(rep: Report) -> None:
    keys = settings.groq_key_pool()
    if not keys:
        rep.add("groq: keys", BAD, "none configured")
        return
    live = 0
    for i, key in enumerate(keys):
        try:
            pool = GroqPool(keys=[key])
            t0 = time.monotonic()
            resp = await pool.generate([{"role": "user", "content": "Reply with: ok"}],
                                       max_output_tokens=16)
            await pool.aclose()
            used = (resp.get("usage") or {}).get("total_tokens", 0)
            rep.add(f"groq: key #{i + 1}", OK,
                    f"{int((time.monotonic() - t0) * 1000)}ms · {used} tokens")
            live += 1
        except LLMError as exc:
            rep.add(f"groq: key #{i + 1}", BAD, str(exc)[:120])
    rep.facts["groq_keys_ok"] = live
    rep.add("groq: pool", OK if live else BAD, f"{live} of {len(keys)} keys answering")


async def check_http(rep: Report, base: str) -> None:
    base = base.rstrip("/")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        try:
            r = await c.get(f"{base}/api/healthz")
            h = r.json()
            rep.add("http: healthz", OK if r.status_code == 200 and h.get("ok") else BAD,
                    f"mock={h.get('mock')} pools={h.get('active_pools')} "
                    f"ticker={h.get('ticker_passes')}")
            rep.facts["deployed_healthz"] = h
        except Exception as exc:                          # noqa: BLE001
            rep.add("http: healthz", BAD, f"{type(exc).__name__}: {str(exc)[:120]}")
            return
        try:
            r = await c.get(f"{base}/api/cast")
            rep.add("http: cast", OK if r.status_code == 200 else BAD, f"{r.status_code}")
        except Exception as exc:                          # noqa: BLE001
            rep.add("http: cast", BAD, str(exc)[:120])

        # A real socket, through whatever proxy is in front of us. This is the one check
        # the unit tests cannot make: nginx buffering an SSE response is invisible until
        # a stream is opened over the network, and it looks exactly like a hung agent.
        pool = await c.post(f"{base}/api/pools", json={
            "goal_text": "Pre-flight: a throwaway pool to prove the stream reaches a client.",
            "policy_text": "Pre-flight only — this pool is not meant to collect anything.",
            "cids": [p["cid"] for p in cast_mod.load_cast()[:3]], "demo": True})
        if pool.status_code != 200:
            rep.add("http: SSE", BAD, f"could not create a pool: {pool.status_code} {pool.text[:90]}")
            return
        pid = pool.json()["pool_id"]
        got, t0 = [], time.monotonic()
        try:
            async with c.stream("GET", f"{base}/api/pools/{pid}/stream") as r:
                rep.add("http: SSE headers", OK if r.headers.get("x-accel-buffering") == "no" else BAD,
                        f"content-type={r.headers.get('content-type')} "
                        f"x-accel-buffering={r.headers.get('x-accel-buffering')}")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        got.append(json.loads(line[6:]).get("type"))
                    if got and time.monotonic() - t0 > 4:
                        break
        except Exception as exc:                          # noqa: BLE001
            rep.add("http: SSE", BAD, f"{type(exc).__name__}: {str(exc)[:120]}")
            return
        rep.add("http: SSE streams through the proxy", OK if got else BAD,
                f"{len(got)} frames in {time.monotonic() - t0:.1f}s: {got[:4]}")
        rep.facts["sse_frames"] = got[:12]


async def run(args) -> int:
    print(f"Potluck pre-flight · {time.strftime('%Y-%m-%d %H:%M:%S')} · "
          f"{'MOCK' if settings.use_mock_paypal else settings.paypal_base_url}\n")
    rep = Report()
    await check_config(rep)
    async with get_paypal_client(default_origin="settle") as pp:
        await check_paypal(rep, pp, canary=args.canary)
    if not args.no_groq:
        await check_groq(rep)
    if args.url:
        await check_http(rep, args.url)
    rep.write()
    print()
    if rep.failed:
        print(f"NOT READY — {len(rep.failed)} check(s) failed: {', '.join(rep.failed)}")
        return 1
    print("READY" + ("" if args.canary else " (payout rail unproven — rerun with --canary)"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Prove Potluck's rails before anyone watches.")
    ap.add_argument("--canary", action="store_true",
                    help="also run a two-item one-cent payout and poll it to terminal")
    ap.add_argument("--url", default="", help="also check a deployed instance over HTTP")
    ap.add_argument("--no-groq", action="store_true", help="skip the LLM keys")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

# Potluck — build plan (PayPal AI Hackathon)

> Submission deadline **2026-11-12** (Devpost, $67,500 cash). Hosted demo must stay
> live & free for judges through ~Dec 15. Deliverables: public repo + OSS license,
> hosted working demo, YouTube demo < 3 min, written description.
> Chosen by a 3-judge concept council, unanimous 9/9/9 (runner-ups: Latch, Cascade).

## One-liner
"Kickstarter's all-or-nothing, but for your friend group's everyday money, run by an AI agent."
You state a goal in plain language → an AI agent invoices every contributor via PayPal,
tracks who paid, reminds stragglers, and **at the deadline autonomously either RELEASES
the pool to the recipient (threshold met) or REFUNDS everyone to the cent (threshold
missed)** — every reasoning step and every real PayPal call streamed live.

## Why it wins (council consensus)
Most relatable (everyone's been "the organizer"), genuinely novel / off the cliché list
(uncrowded lane → least likely duplicated by ~500 entries), bulletproof PayPal-central
(both outcome branches are real money movements → passes Stage-1 gate cold), best demo
money-shot (pool fills live, then auto-refunds all 8 contributors on screen), most
feasible (near-exact reuse of the Overindex template). High ceiling (grand prize) + high
floor (can win Best Demo Delivery and Most Creative outright).
Targets: Best Use of Agentic Commerce, Best Use of PayPal+AI, Best Demo Delivery, Most
Creative, Most Impactful ($5k each) + AG Grid ($5k ledger grid) + Render (hosting credit).

## Stack — reuse the Overindex template almost verbatim
- Backend: FastAPI + async httpx + Pydantic v2. Groq **gpt-oss-120b** ReAct agent
  (OpenAI-style function-calling) chooses the tool sequence + the RELEASE/UNWIND branch
  live. Stream every reasoning token + tool-call + result over **SSE**.
- Frontend: React 19 + TS + Vite + Tailwind. Reuse Overindex's live reasoning-trace +
  firing-API-call cards. Add a live **pool ledger in AG Grid** (contributors, shares,
  paid/unpaid, timestamps, running total vs threshold) → targets the AG Grid $5k.
- LLM keys: reuse the Overindex Groq pool (4 keys, auto-failover) — no new LLM key.
- Hosting: Render (free tier → qualifies for the Render partner credit; keeps demo live
  through judging) — or reuse the Hetzner box. Decide at deploy.

## PayPal integration
- **PayPal Agent Toolkit / MCP** (official, ~40-50 function-calling tools, free, sandbox)
  for Invoicing + Transaction Search. This is the winnable path — the *branded*
  "Agentic Commerce" product (Agent Ready / Store Sync) is gated behind sales approval,
  so we do NOT use it.
- Tools the agent chains (self-chosen, conditional): `create_invoice` (per contributor),
  `generate_invoice_qr_code`, `send_invoice`, `search_invoicing` (+ `list_transactions`)
  to poll payment state, `send_invoice_reminder` (conditional nudges),
  `record_payment_for_invoice` (demo determinism), `cancel_sent_invoice` (unwind).
- **Custom thin REST wrapper for Payouts** (`POST /v1/payments/payouts`, batch,
  `sender_batch_id` for 30-day idempotency) — the only non-toolkit piece, trivial to
  wrap, and a real technical-depth signal (Payouts is deliberately absent from the
  toolkit). Used for BOTH branches: RELEASE (pay recipient) and UNWIND (refund all).
- Auth: OAuth2 client-credentials token mint + refresh (`POST /v1/oauth2/token`,
  SANDBOX). Creds in `backend/.env` (gitignored, never committed).

## State machine (what the agent does)
goal → plan → `create_invoice` ×N → `generate_invoice_qr_code` → `send_invoice` ×N →
poll loop (`search_invoicing`, update ledger + running total) → `send_invoice_reminder`
(conditional) → **DEADLINE BRANCH**:
- RELEASE (total ≥ threshold): custom Payouts → pay the recipient/buyer (+ pro-rata
  overage refund if overpaid).
- UNWIND (total < threshold): custom Payouts → refund every paid contributor their exact
  amount + `cancel_sent_invoice` for the unpaid → post final auditable ledger.

## 4-week milestones
- **Wk1:** sandbox + OAuth mint/refresh; wire Agent Toolkit/MCP; create+send invoices;
  SSE the trace. Validate riskiest tools early.
- **Wk2:** payment-state polling + the RELEASE/UNWIND branch via the Payouts wrapper;
  the poll-and-branch orchestration loop.
- **Wk3:** AG Grid pool ledger + React polish; script the two demo scenarios.
- **Wk4:** deploy (Render), harden sandbox choreography, record < 3-min video, write-up +
  OSI license on a public GitHub repo. (We'll likely finish faster; buffer before Nov 12.)

## Demo scenario
A pool: "$360 for Dana's gift, $45 × 8 by Friday." Two takes:
1. RELEASE — threshold hit → agent pays the recipient live.
2. UNWIND (the money-shot) — threshold missed at deadline → agent auto-refunds all 8
   contributors to the cent, live on the ledger.
Pre-seed sandbox accounts + use `record_payment_for_invoice` for on-camera determinism;
script the multi-account payment choreography (don't rely on live payments).

## Key risks & mitigations (from the council)
1. No auto-sweep of invoice receipts into a new order → model RELEASE as a **Payouts
   disbursement to the recipient** (the realistic "one person buys, everyone reimburses"
   pattern), fully real in sandbox. UNWIND (refund-all via Payouts) is 100% real.
2. `create_order → pay_order` is NOT cleanly autonomous (needs buyer approval) → we
   **collect via Invoicing**, which sidesteps it entirely.
3. Transaction Search sandbox lag → detect payment primarily via `search_invoicing`
   status + `record_payment_for_invoice`; don't rely solely on `list_transactions`.
4. Payouts needs its own OAuth + idempotent `sender_batch_id` → low risk, well-documented.
5. Live multi-account choreography is fiddly → pre-seed + script + record the branch.
6. **Payouts cannot reach Ukraine** (verified in sandbox 2026-10-16): a payout item to a
   UA personal account fails with `RECEIVER_COUNTRY_NOT_ALLOWED`; an unregistered email
   returns `UNCLAIMED / RECEIVER_UNREGISTERED`. Demo contributors must therefore be
   sandbox **personal accounts in an allowed country (US)**. The UA *business* account as
   payout SENDER is fine — only the receiver country is restricted.

## What's NEW vs Overindex (the only real build delta)
- The custom Payouts REST wrapper + OAuth for it.
- The poll-and-branch (RELEASE/UNWIND) orchestration loop (a money state machine).
- The AG Grid pool ledger view.
Everything else (ReAct loop, function-calling, SSE streaming, React trace UI, Groq pool)
is reused from Overindex.

## Anton's hands (the only blockers)
1. Create a **PayPal Developer sandbox** app → send sandbox Client ID + Secret (→ .env,
   gitignored). Note the sandbox business + several personal test accounts.
2. Later: screen-record the demo, create the YouTube video, submit on Devpost, Join
   hackathon. (Same division of labour as Overindex.)

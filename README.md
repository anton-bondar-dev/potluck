# Potluck

**Kickstarter's all-or-nothing, but for your friend group's everyday money — run by an AI agent.**

You state a goal in plain language ("collect $45 each from these 8 people for Dana's $360
gift by Friday; buy it only if we hit $360, otherwise give everyone their money back").
An AI agent then runs the whole thing on PayPal: it invoices every contributor, tracks who
has paid, reminds stragglers, and **at the deadline autonomously either releases the pool
to the recipient (threshold met) or refunds everyone to the cent (threshold missed)** —
with every reasoning step and every real PayPal API call streamed to the screen live.

Built for the **PayPal AI Hackathon**.

## How it works

A Groq `gpt-oss-120b` ReAct agent (OpenAI-style function calling) chooses a sequence of
PayPal tool-calls live — it is the money state machine, not a chat wrapper:

- **Collect** — `create_invoice` per contributor, `generate_invoice_qr_code`, `send_invoice`.
- **Track** — poll `search_invoicing` to read who has paid, updating a live pool ledger.
- **Nudge** — `send_invoice_reminder` for the contributors it decides need it.
- **Resolve at the deadline** — RELEASE (threshold met): pay the pooled funds out to the
  recipient via PayPal Payouts. UNWIND (threshold missed): refund every paid contributor
  and cancel the unpaid invoices.

## Stack

- **Backend:** FastAPI + async httpx + Pydantic, streaming every event over Server-Sent
  Events. Groq ReAct agent. PayPal Agent Toolkit / REST (Invoicing + Transaction Search)
  plus a thin Payouts wrapper.
- **Frontend:** React + TypeScript + Vite + Tailwind — a live reasoning trace and a pool
  ledger (AG Grid).

## Status

Work in progress. Build the whole flow in mock mode first (`USE_MOCK_PAYPAL=1`), then wire
the real PayPal sandbox.

## License

MIT (see `LICENSE`).

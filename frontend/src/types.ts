/** Every frame the backend can send. The event name travels in `type` rather than on
 *  an SSE `event:` line, because one reducer dispatches all of them. */
export type Ev = {
  seq: number
  type: string
  ts: number
  pool_id: string
  [k: string]: unknown
}

export type Phase = 'DRAFT' | 'COLLECTING' | 'RESOLVING' | 'SETTLING' | 'CLOSED' | 'ESCALATED'

export type Row = {
  cid: string
  name: string
  email_masked: string
  share_cents: number
  status: string
  paid_cents: number
  paid_at?: number | null
  source?: string | null
  invoice_id?: string | null
  payer_url?: string | null
  reminders: number
  last_reminder_at?: number | null
  flags: string[]
}

export type PayoutItem = {
  cid?: string
  name?: string
  email_masked: string
  amount_cents: number
  kind?: string
  item_status?: string
  transaction_id?: string
}

export type Settlement = {
  decision: 'RELEASE' | 'UNWIND'
  reason: string
  policy_quote: string
  frozen_collected_cents: number
  batch_id?: string | null
  batch_status?: string | null
  sender_batch_id: string
  items: PayoutItem[]
  decided_by?: string
}

/** The full projected state — what GET /api/pools/{id} returns and what the stream
 *  then keeps up to date. */
export type Pool = {
  pool_id: string
  phase: Phase
  goal_text: string
  policy_text: string
  success_condition_restated?: string | null
  recipient_cid?: string | null
  target_cents: number
  currency: string
  overage: string
  deadline_at?: number | null
  deadline_label?: string | null
  clock_scale: number
  time_left_s: number
  time_left_label: string
  collected_cents: number
  paid_count: number
  n: number
  ledger_version: number
  episodes_used: number
  rows: Row[]
  settlement: Settlement | null
  last_seq: number
  events?: Ev[]
  paypal_env: string
  mock: boolean
}

export type CastMember = { cid: string; name: string; email_masked: string }

/** Lane A — one card per thing the agent did or said. */
export type TraceItem = {
  id: string
  kind: 'episode' | 'thought' | 'note' | 'step' | 'guard' | 'deferred' | 'phase' | 'sleep' | 'wake'
  at: number
  title: string
  body?: string
  args?: unknown
  ok?: boolean
  summary?: string
  rule?: string
  tool?: string
  tokens?: { prompt: number; completion: number }
  turns?: number
  reason?: string
}

/** Lane B — one line per real REST call. */
export type WireCall = {
  seq: number
  method: string
  path: string
  status: number
  ms: number
  origin: string
}

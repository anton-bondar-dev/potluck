import { useCallback, useEffect, useRef, useState } from 'react'
import { getPool, streamUrl } from './api'
import type { Ev, PayoutItem, Phase, Pool, Row, TraceItem, WireCall } from './types'

export type PoolState = {
  pool: Pool | null
  trace: TraceItem[]
  wire: WireCall[]
  /** cids whose row changed on the last frame — drives the flash. */
  flashed: Set<string>
  connected: boolean
  ended: boolean
  error?: string
  /** The last frame applied. Replay re-delivers, so everything is keyed on this. */
  lastSeq: number
  receipt: Record<string, unknown> | null
  escalation: Record<string, unknown> | null
}

const TRACE_CAP = 240
const WIRE_CAP = 120

function money(c: number): string {
  const n = Math.abs(c)
  return `${c < 0 ? '-' : ''}$${Math.floor(n / 100)}.${String(n % 100).padStart(2, '0')}`
}

/** Fold one frame into the projection.
 *
 *  The reducer is IDEMPOTENT and keyed on seq, because ?from_seq and the native
 *  Last-Event-ID both re-deliver frames the client may already have applied: a judge
 *  who refreshes, or a phone that loses signal in a lift, must come back to the same
 *  screen rather than a double-counted one.
 */
function apply(s: PoolState, ev: Ev): PoolState {
  if (ev.seq <= s.lastSeq) return s
  const next: PoolState = { ...s, lastSeq: ev.seq, flashed: new Set() }
  const pool = next.pool ? { ...next.pool } : null
  const push = (t: Omit<TraceItem, 'id' | 'at'>) => {
    next.trace = [...next.trace, { ...t, id: `e${ev.seq}`, at: ev.ts }].slice(-TRACE_CAP)
  }
  const num = (k: string) => Number(ev[k] ?? 0)
  const str = (k: string) => String(ev[k] ?? '')

  switch (ev.type) {
    case 'pool_created':
      push({ kind: 'phase', title: 'Pool created', body: str('goal_text') })
      break

    case 'phase':
      if (pool) pool.phase = str('to') as Phase
      push({ kind: 'phase', title: `${str('from')} → ${str('to')}`, reason: str('reason') })
      break

    case 'episode_start':
      push({ kind: 'episode', title: `Woken by ${str('trigger')}` })
      break

    case 'thought':
      push({ kind: 'thought', title: 'Thinking', body: str('text') })
      break

    case 'agent_note':
      push({ kind: 'note', title: 'The agent says', body: str('text') })
      break

    case 'model_turn':
      next.trace = next.trace.map((t, i) =>
        i === next.trace.length - 1 && t.kind === 'episode'
          ? { ...t, tokens: { prompt: num('prompt_tokens'), completion: num('completion_tokens') } }
          : t,
      )
      break

    case 'step_start':
      push({ kind: 'step', title: str('tool'), tool: str('tool'), args: ev.args })
      break

    case 'step_result': {
      // Attach the outcome to the step it belongs to, so a card is one row, not two.
      const sid = str('step_id')
      let patched = false
      next.trace = next.trace
        .slice()
        .reverse()
        .map((t) => {
          if (!patched && t.kind === 'step' && t.summary === undefined) {
            patched = true
            return { ...t, ok: Boolean(ev.ok), summary: str('summary') }
          }
          return t
        })
        .reverse()
      if (!patched && sid) push({ kind: 'step', title: 'result', summary: str('summary'), ok: Boolean(ev.ok) })
      break
    }

    case 'guard_block':
      push({ kind: 'guard', title: str('rule'), body: str('detail'), tool: str('tool'), rule: str('rule') })
      break

    case 'episode_end':
      push({ kind: 'episode', title: `Episode ended · ${str('reason')}`, turns: num('turns') })
      break

    case 'episode_deferred':
      push({
        kind: 'deferred',
        title: 'Episode deferred',
        body: `${str('reason')} — retrying in ${num('retry_in_s')}s. ${str('detail')}`,
      })
      break

    case 'agent_sleep':
      push({
        kind: 'sleep',
        title: `Asleep for ${num('sleep_s')}s`,
        body: str('reason'),
        reason: str('until_label'),
      })
      break

    case 'agent_wake':
      push({ kind: 'wake', title: `Awake — ${str('trigger')}` })
      break

    case 'ledger_init': {
      if (pool) {
        const rows = (ev.rows as Row[]) || []
        pool.rows = pool.rows.map((r) => {
          const seeded = rows.find((x) => x.cid === r.cid)
          return seeded ? { ...r, ...seeded, flags: r.flags } : r
        })
        pool.target_cents = num('target_cents')
        pool.success_condition_restated = str('success_condition_restated')
        pool.clock_scale = num('clock_scale') || pool.clock_scale
        if (ev.deadline_at) pool.deadline_at = num('deadline_at')
        if (ev.deadline_label) pool.deadline_label = str('deadline_label')
      }
      break
    }

    case 'ledger_row': {
      if (pool) {
        pool.rows = pool.rows.map((r) =>
          r.cid === str('cid')
            ? {
                ...r,
                status: str('status') || r.status,
                paid_cents: num('paid_cents'),
                source: (ev.source as string) ?? r.source,
                invoice_id: (ev.invoice_id as string) ?? r.invoice_id,
                payer_url: (ev.payer_url as string) ?? r.payer_url,
                flags: flagsFor(r, num('paid_cents')),
              }
            : r,
        )
        next.flashed = new Set([str('cid')])
      }
      break
    }

    case 'pool_totals':
      if (pool) {
        pool.collected_cents = num('collected_cents')
        pool.target_cents = num('target_cents') || pool.target_cents
        pool.paid_count = num('paid_count')
        pool.n = num('n') || pool.n
        pool.time_left_s = num('time_left_s')
        pool.time_left_label = str('time_left_label')
        pool.ledger_version = num('ledger_version')
      }
      break

    case 'clock_tick':
      if (pool) {
        pool.time_left_s = num('time_left_s')
        pool.time_left_label = str('time_left_label')
      }
      break

    case 'payment_detected':
      if (pool) next.flashed = new Set([str('cid')])
      push({ kind: 'note', title: `${str('name')} paid ${money(num('paid_cents'))}`, body: '' })
      break

    case 'reminder_sent':
      // SET, never increment. The repaint already carries the current count and then
      // every historical reminder_sent is replayed on top of it — an increment here
      // showed six nudges against a cap of three. The event carries the true number.
      if (pool && ev.reminders !== undefined) {
        pool.rows = pool.rows.map((r) =>
          r.cid === str('cid') ? { ...r, reminders: num('reminders') } : r,
        )
      }
      push({ kind: 'note', title: `Nudged ${str('name')}`, body: str('note') })
      break

    case 'invoice_cancelled':
      if (pool) {
        pool.rows = pool.rows.map((r) =>
          r.cid === str('cid') ? { ...r, status: 'CANCELLED' } : r,
        )
        next.flashed = new Set([str('cid')])
      }
      break

    case 'decision':
      if (pool) {
        pool.settlement = {
          decision: str('decision') as 'RELEASE' | 'UNWIND',
          reason: str('reason'),
          policy_quote: str('policy_quote'),
          frozen_collected_cents: num('collected_cents'),
          sender_batch_id: '',
          items: [],
          decided_by: str('by'),
          batch_status: null,
          batch_id: null,
        }
      }
      break

    case 'payout_submitted':
      if (pool?.settlement) {
        pool.settlement = {
          ...pool.settlement,
          batch_id: str('batch_id'),
          sender_batch_id: str('sender_batch_id'),
          batch_status: str('batch_status'),
          items: (ev.items as PayoutItem[]) || [],
        }
      }
      break

    case 'payout_item':
      if (pool?.settlement) {
        pool.settlement = {
          ...pool.settlement,
          items: pool.settlement.items.map((i) =>
            (i.cid && i.cid === str('cid')) || i.email_masked === str('email_masked')
              ? { ...i, item_status: str('item_status'), transaction_id: str('transaction_id') }
              : i,
          ),
        }
      }
      break

    case 'payout_settled':
      if (pool?.settlement) {
        pool.settlement = { ...pool.settlement, batch_status: str('batch_status') }
      }
      break

    case 'paypal_call':
      next.wire = [
        ...next.wire,
        {
          seq: ev.seq,
          method: str('method'),
          path: str('path'),
          status: num('status'),
          ms: num('ms'),
          origin: str('origin'),
        },
      ].slice(-WIRE_CAP)
      break

    case 'pool_closed':
      if (pool) pool.phase = 'CLOSED'
      push({ kind: 'note', title: 'Pool closed', body: str('summary') })
      break

    case 'human_message':
      // A judge's own words belong in the trace, right before whatever the agent does
      // about them — that juxtaposition IS the demonstration.
      push({ kind: 'note', title: 'A human said', body: str('text') })
      break

    case 'receipt':
      next.receipt = { ...ev }
      break

    case 'escalated':
      if (pool) pool.phase = 'ESCALATED'
      next.escalation = { ...ev }
      push({ kind: 'guard', title: 'Escalated to a human', body: str('reason'), rule: 'ESCALATED' })
      break

    case 'error':
      push({ kind: 'guard', title: `Error in ${str('where')}`, body: str('message'), rule: 'ERROR' })
      break

    case 'end':
      next.ended = true
      break

    default:
      break
  }
  next.pool = pool
  return next
}

function flagsFor(r: Row, paid: number): string[] {
  const f = r.flags.filter((x) => x !== 'PARTIAL' && x !== 'OVERPAID')
  if (paid > 0 && r.share_cents > 0 && paid < r.share_cents) f.push('PARTIAL')
  if (paid > 0 && r.share_cents > 0 && paid > r.share_cents) f.push('OVERPAID')
  return f
}

const EMPTY: PoolState = {
  pool: null,
  trace: [],
  wire: [],
  flashed: new Set(),
  connected: false,
  ended: false,
  lastSeq: 0,
  receipt: null,
  escalation: null,
}

/** Paint from the projection, then tail the stream from that cursor.
 *
 *  GET /api/pools/{id} is the primary safety net: if SSE never connects, or dies and
 *  cannot come back, the screen is still correct — it just stops moving. That is why
 *  the repaint runs FIRST and the stream resumes from its last_seq rather than from 0.
 */
export function usePool(poolId: string | null): PoolState & { reload: () => void } {
  const [state, setState] = useState<PoolState>(EMPTY)
  const esRef = useRef<EventSource | null>(null)
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    if (!poolId) {
      setState(EMPTY)
      return
    }
    let dead = false
    setState(EMPTY)

    ;(async () => {
      let from = 0
      let closedAlready = false
      try {
        const pool = await getPool(poolId)
        if (dead) return
        let s: PoolState = { ...EMPTY, pool, flashed: new Set(), lastSeq: 0 }
        for (const ev of pool.events || []) s = apply(s, ev)
        // The cursor is the projection's own last_seq: the event list it ships is
        // capped, so the newest frame it contains is not necessarily the newest frame
        // there is, and resuming from the wrong one would re-deliver or skip.
        from = Math.max(s.lastSeq, pool.last_seq || 0)
        closedAlready = (s.pool?.phase || pool.phase) === 'CLOSED'
        setState({ ...s, pool: { ...pool, ...s.pool } as Pool, lastSeq: from })
      } catch (e) {
        if (!dead) setState((p) => ({ ...p, error: (e as Error).message }))
        return
      }

      // A closed pool has nothing left to say, and the cursor is already past its final
      // frame — opening a stream on one would sit on a heartbeat for ever. Decided from
      // the value in hand rather than from a setState updater, which React may batch or
      // (in StrictMode) call twice.
      if (closedAlready) {
        setState((p) => ({ ...p, ended: true }))
        return
      }

      const es = new EventSource(streamUrl(poolId, from))
      esRef.current = es
      es.onopen = () => setState((p) => ({ ...p, connected: true, error: undefined }))
      es.onmessage = (m) => {
        let ev: Ev
        try {
          ev = JSON.parse(m.data)
        } catch {
          return
        }
        setState((p) => apply(p, ev))
        if (ev.type === 'end') {
          // The server returns after the terminal sentinel, and EventSource treats a
          // closed connection as something to RETRY — so a finished pool would reconnect
          // for ever, which is most of the pools a judge will open. Nothing more is
          // coming; hang up.
          es.close()
          setState((p) => ({ ...p, connected: false, ended: true }))
        }
      }
      es.onerror = () => {
        // EventSource reconnects by itself and sends Last-Event-ID, so this is a
        // status change, not a failure. Only a closed socket is terminal.
        setState((p) => ({ ...p, connected: false }))
        if (es.readyState === EventSource.CLOSED) {
          // One repaint, so a stream that cannot come back still leaves a correct screen.
          getPool(poolId)
            .then((pool) => !dead && setState((p) => ({ ...p, pool })))
            .catch(() => undefined)
        }
      }
    })()

    return () => {
      dead = true
      esRef.current?.close()
      esRef.current = null
    }
  }, [poolId, nonce])

  return { ...state, reload }
}

export { money }

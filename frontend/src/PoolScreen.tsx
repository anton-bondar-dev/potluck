import { useState } from 'react'
import { messageAgent, payAs } from './api'
import Decision from './components/Decision'
import Escalated from './components/Escalated'
import { Authority, Gauges, Stepper } from './components/Gauges'
import Ledger from './components/Ledger'
import Receipt from './components/Receipt'
import Trace from './components/Trace'
import Wire from './components/Wire'
import { usePool } from './usePool'

/** Four lanes, laid out so the eye knows where to go when the agent acts:
 *  the trace on the left, the ledger in the middle, the gauges on the right, the
 *  PayPal wire beneath — and a full-width takeover when the branch lands.
 */
export default function PoolScreen({ poolId, onNew }: { poolId: string; onNew: () => void }) {
  const st = usePool(poolId)
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [toast, setToast] = useState<string | null>(null)

  const pool = st.pool
  if (st.error && !pool) {
    return (
      <div className="mx-auto max-w-lg px-5 py-24 text-center">
        <p className="text-[14px] text-danger">{st.error}</p>
        <button onClick={onNew} className="mt-4 text-[13px] text-accent-hi underline">
          start a new pool
        </button>
      </div>
    )
  }
  if (!pool) {
    return <div className="px-5 py-24 text-center text-[13px] text-text-faint">Loading the pool…</div>
  }

  const pay = async (cid: string, amount?: number) => {
    setBusy(cid)
    setToast(null)
    try {
      await payAs(poolId, cid, amount)
    } catch (e) {
      setToast((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const send = async () => {
    const text = note.trim()
    if (!text) return
    setNote('')
    try {
      await messageAgent(poolId, text)
    } catch (e) {
      setToast((e as Error).message)
    }
  }

  const settled = pool.settlement && pool.settlement.decision
  const closed = pool.phase === 'CLOSED'

  return (
    <div className="mx-auto max-w-[1560px] px-4 py-4 sm:px-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h1 className="text-[17px] font-semibold tracking-tight text-text-hi">Potluck</h1>
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                st.connected ? 'bg-paid' : closed ? 'bg-text-faint' : 'bg-guard pulse-ring'
              }`}
              title={st.connected ? 'live' : closed ? 'finished' : 'reconnecting'}
            />
            <span className="nums font-mono text-[11px] text-text-faint">
              {pool.mock ? 'MOCK' : 'PayPal sandbox'} · seq {st.lastSeq}
            </span>
          </div>
          <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed whitespace-pre-wrap text-text">
            {pool.goal_text}
          </p>
          <p className="mt-1 max-w-3xl border-l-2 border-border pl-2.5 text-[12px] leading-relaxed whitespace-pre-wrap text-text-dim italic">
            {pool.policy_text}
          </p>
          {pool.success_condition_restated && (
            <p className="mt-1.5 max-w-3xl text-[12px] leading-relaxed text-accent-hi">
              <span className="text-text-faint">the agent's restatement: </span>
              {pool.success_condition_restated}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <Stepper phase={pool.phase} />
          <button
            onClick={onNew}
            className="rounded-ctl border border-border bg-surface-1 px-2.5 py-1 text-[11px] text-text-dim transition hover:border-border-strong hover:text-text-hi"
          >
            new pool
          </button>
        </div>
      </header>

      {toast && (
        <p className="mt-3 rounded-ctl border border-guard/30 bg-guard-dim px-3 py-2 text-[12px] text-guard">
          {toast}
        </p>
      )}

      {pool.phase === 'ESCALATED' && (
        <div className="mt-4">
          <Escalated
            poolId={poolId}
            snapshot={st.escalation?.snapshot as Record<string, unknown>}
            reason={st.escalation?.reason as string}
            onDone={st.reload}
          />
        </div>
      )}

      {settled && pool.settlement && (
        <div className="mt-4">
          <Decision pool={pool} s={pool.settlement} />
        </div>
      )}

      {st.receipt && (
        <div className="mt-4">
          <Receipt r={st.receipt as never} />
        </div>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-[360px_minmax(0,1fr)_300px]">
        <div className="flex min-h-[520px] flex-col lg:h-[calc(100vh-230px)] lg:min-h-0">
          <Trace items={st.trace} />
        </div>

        <div className="space-y-4">
          <Ledger pool={pool} onPay={pay} busy={busy} />
          <Wire calls={st.wire} />
          <div className="rounded-card border border-border bg-surface-1 p-3">
            <label className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
              Say something to the agent
            </label>
            <div className="mt-1.5 flex gap-2">
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && send()}
                maxLength={400}
                placeholder="Sam says he'll pay tonight — can you give him until then?"
                className="min-w-0 flex-1 rounded-ctl border border-border bg-surface-2 px-3 py-2 text-[13px] text-text-hi outline-none transition placeholder:text-text-faint focus:border-accent/50"
              />
              <button
                onClick={send}
                disabled={!note.trim()}
                className="rounded-ctl border border-border-strong bg-surface-3 px-3 py-2 text-[12px] font-medium text-text-hi transition hover:border-accent/40 hover:text-accent-hi disabled:opacity-40"
              >
                send
              </button>
            </div>
            <p className="mt-1.5 text-[11px] text-text-faint">
              It wakes the agent out of its sleep. What you write is data, not an
              instruction it has to obey — ask it to pay someone outside the roster and
              watch the call get refused.
            </p>
          </div>
        </div>

        <div className="space-y-3">
          <Gauges pool={pool} />
          <Authority />
        </div>
      </div>

      <footer className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border pt-3 font-mono text-[11px] text-text-faint">
        <span>pool {pool.pool_id}</span>
        <span>episodes {pool.episodes_used}</span>
        <span>ledger v{pool.ledger_version}</span>
        <span className="ml-auto">
          share this page — it replays from the start for whoever opens it
        </span>
      </footer>
    </div>
  )
}

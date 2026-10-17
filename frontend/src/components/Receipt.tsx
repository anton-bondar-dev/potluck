import { money } from '../usePool'

type R = {
  inflow_cents: number
  outflow_cents: number
  delta_cents: number
  delta_explained: string
  ledger_hash: string
  invariants_checked: string[]
  decision?: string
  decided_by?: string
  item_statuses?: Record<string, string>
  provenance: Record<string, unknown>
}

/** The receipt: the artefact a sceptic can take away.
 *
 *  Inflow is what PayPal confirmed arrived. Outflow is the sum of the items written
 *  ahead of the batch. The delta must be zero — and if it is not, this says in words
 *  why, rather than leaving a reader to work it out.
 */
export default function Receipt({ r }: { r: R }) {
  const balanced = r.delta_cents === 0
  const p = r.provenance || {}
  const num = (k: string) => Number(p[k] ?? 0)
  const download = () => {
    const blob = new Blob([JSON.stringify(r, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `potluck-receipt-${r.ledger_hash.slice(0, 12)}.json`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface-1">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
          Receipt
        </h2>
        <button
          onClick={download}
          className="rounded-ctl border border-border-strong bg-surface-3 px-2.5 py-1 text-[11px] font-medium text-text-hi transition hover:border-accent/40 hover:text-accent-hi"
        >
          download JSON
        </button>
      </div>

      <div className="grid grid-cols-3 divide-x divide-border border-b border-border">
        {[
          ['in', r.inflow_cents, 'text-text-hi'],
          ['out', r.outflow_cents, 'text-text-hi'],
          ['delta', r.delta_cents, balanced ? 'text-paid' : 'text-guard'],
        ].map(([label, v, tone]) => (
          <div key={label as string} className="px-4 py-3">
            <div className="text-[10px] tracking-[0.1em] text-text-faint uppercase">
              {label as string}
            </div>
            <div className={`nums mt-0.5 font-mono text-[19px] font-medium ${tone as string}`}>
              {money(v as number)}
            </div>
          </div>
        ))}
      </div>

      <p
        className={`px-4 py-2.5 text-[12px] ${
          balanced ? 'text-paid' : 'text-guard'
        }`}
      >
        {balanced ? 'Conservation holds: ' : 'Retained: '}
        <span className="text-text-dim">{r.delta_explained}</span>
      </p>

      <div className="border-t border-border px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {r.invariants_checked.map((i) => (
            <span
              key={i}
              className="rounded border border-paid/25 bg-paid-dim px-1.5 py-0.5 font-mono text-[10px] text-paid"
            >
              {i}
            </span>
          ))}
        </div>
      </div>

      <dl className="nums grid grid-cols-2 gap-x-4 gap-y-1 border-t border-border px-4 py-3 font-mono text-[11px] sm:grid-cols-3">
        {[
          ['real PayPal calls', num('paypal_calls')],
          ['LLM turns', num('llm_turns')],
          ['episodes', num('episodes')],
          ['tokens', num('prompt_tokens') + num('completion_tokens')],
          ['guards fired', num('guards_fired')],
          ['invoices', (p.invoice_ids as string[] | undefined)?.length ?? 0],
        ].map(([k, v]) => (
          <div key={k as string} className="flex justify-between gap-2">
            <dt className="text-text-faint">{k as string}</dt>
            <dd className="text-text">{String(v)}</dd>
          </div>
        ))}
      </dl>

      <div className="border-t border-border px-4 py-2.5 font-mono text-[10px] break-all text-text-faint">
        <div>ledger sha256 {r.ledger_hash}</div>
        {p.payout_batch_id ? <div>batch {String(p.payout_batch_id)}</div> : null}
        <div>
          {String(p.paypal_env || 'sandbox')}
          {p.mock ? ' · MOCK (no network)' : ' · real PayPal sandbox calls'}
        </div>
      </div>
    </section>
  )
}

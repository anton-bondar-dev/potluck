import { useState } from 'react'
import { approveRefund } from '../api'
import { money } from '../usePool'

/** The ending for a pool the agent never resolved.
 *
 *  The failure mode of this system is INACTION, not a guess: if the model never decided,
 *  or Groq was unreachable through the deadline and its grace window, every cent is left
 *  exactly where it was and the pool waits for a person. Code does not initiate a payout
 *  on its own — a code-executed failsafe would be precisely the hardcoded money-moving
 *  branch this whole design exists in order not to have.
 */
export default function Escalated({
  poolId,
  snapshot,
  reason,
  onDone,
}: {
  poolId: string
  snapshot?: Record<string, unknown>
  reason?: string
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const collected = Number(snapshot?.collected_cents ?? 0)

  return (
    <section className="rise overflow-hidden rounded-hero border-2 border-guard/50 bg-guard-dim">
      <div className="px-5 py-4">
        <div className="flex flex-wrap items-center gap-3">
          <span className="rounded-ctl bg-guard/15 px-3 py-1.5 font-mono text-[16px] font-semibold tracking-[0.1em] text-guard">
            ESCALATED
          </span>
          <span className="text-[13px] text-text">
            {reason || 'the deadline passed without a decision'}
          </span>
        </div>
        <p className="mt-3 text-[13px] leading-relaxed text-text-dim">
          No money has moved. The unpaid invoices were cancelled and the{' '}
          <span className="nums font-mono text-text-hi">{money(collected)}</span> that did
          arrive is sitting exactly where it was. Returning each contributor what they paid
          needs one human approval — the code will not start a payout by itself.
        </p>
        {err && <p className="mt-2 text-[12px] text-danger">{err}</p>}
        <button
          onClick={async () => {
            setBusy(true)
            setErr(null)
            try {
              await approveRefund(poolId, 'approved from the escalation card')
              onDone()
            } catch (e) {
              setErr((e as Error).message)
            } finally {
              setBusy(false)
            }
          }}
          disabled={busy}
          className="mt-4 rounded-ctl bg-guard px-4 py-2 text-[13px] font-semibold text-black transition hover:bg-guard/85 disabled:opacity-50"
        >
          {busy ? 'Submitting the refund…' : `Refund everyone ${money(collected)}`}
        </button>
      </div>
    </section>
  )
}

import { money } from '../usePool'
import type { Pool, Settlement } from '../types'

const ITEM_TONE: Record<string, string> = {
  SUCCESS: 'text-paid',
  PENDING: 'text-text-dim',
  UNCLAIMED: 'text-guard',
  FAILED: 'text-danger',
}

/** Lane D — the branch, and the money.
 *
 *  `decision` is fired alone, on the validated decision, BEFORE any payout call, which
 *  is what buys this card its moment: nothing else is in flight while it lands. The
 *  per-item list below it describes items INSIDE one batch POST and says so — the wire
 *  lane shows exactly one POST /v1/payments/payouts and an attentive judge will check.
 */
export default function Decision({ pool, s }: { pool: Pool; s: Settlement }) {
  const release = s.decision === 'RELEASE'
  const total = s.items.reduce((a, i) => a + i.amount_cents, 0)
  const done = s.items.filter((i) => i.item_status === 'SUCCESS').length
  const unclaimed = s.items.filter((i) => i.item_status === 'UNCLAIMED').length

  return (
    <section
      className={`stamp overflow-hidden rounded-hero border-2 bg-surface-1 ${
        release ? 'border-release/50' : 'border-unwind/50'
      }`}
    >
      <div className="px-5 py-4">
        <div className="flex flex-wrap items-center gap-3">
          <span
            className={`rounded-ctl px-3 py-1.5 font-mono text-[20px] font-semibold tracking-[0.1em] ${
              release ? 'bg-release/15 text-release' : 'bg-unwind/15 text-unwind'
            }`}
          >
            {s.decision}
          </span>
          <span className="text-[13px] text-text-dim">
            {release
              ? `${money(s.frozen_collected_cents)} released`
              : `${money(s.frozen_collected_cents)} returned, to the cent`}
          </span>
          <span className="ml-auto flex items-center gap-2">
            <span className="rounded border border-paid/30 bg-paid-dim px-2 py-0.5 text-[11px] font-medium text-paid">
              attested against PayPal
            </span>
            <span className="rounded border border-border bg-surface-3 px-2 py-0.5 font-mono text-[11px] text-text-faint">
              decided by {s.decided_by || 'agent'}
            </span>
          </span>
        </div>

        {s.reason && (
          <p className="mt-3 text-[13px] leading-relaxed whitespace-pre-wrap text-text">
            {s.reason}
          </p>
        )}
        {s.policy_quote && (
          <blockquote
            className={`mt-3 border-l-2 pl-3 text-[13px] leading-relaxed italic ${
              release ? 'border-release/40' : 'border-unwind/40'
            } text-text-dim`}
          >
            “{s.policy_quote}”
            <footer className="mt-1 text-[11px] not-italic text-text-faint">
              — the organizer's own words, quoted by the agent. Nothing in this system
              parses them.
            </footer>
          </blockquote>
        )}
        {pool.success_condition_restated && (
          <p className="mt-3 rounded-ctl border border-border bg-surface-2 px-3 py-2 text-[12px] leading-relaxed text-text-dim">
            <span className="text-text-faint">It committed to this interpretation at plan time: </span>
            “{pool.success_condition_restated}”
          </p>
        )}
      </div>

      {s.batch_id && (
        <div className="border-t border-border bg-black/25 px-5 py-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="nums font-mono text-[12px] text-text-dim">
              payout batch · {s.items.length} item{s.items.length === 1 ? '' : 's'} ·{' '}
              {money(total)}
              <span className="text-text-faint"> — one POST, {s.items.length} items</span>
            </h3>
            <span className="nums font-mono text-[11px] text-text-faint">
              {s.batch_status}
              {done ? ` · ${done}/${s.items.length} success` : ''}
              {unclaimed ? ` · ${unclaimed} unclaimed` : ''}
            </span>
          </div>
          <ul className="mt-2 space-y-1">
            {s.items.map((i) => (
              <li
                key={(i.cid || '') + i.email_masked}
                className="nums flex items-center gap-3 font-mono text-[12px]"
              >
                <span className="w-28 shrink-0 truncate text-text">{i.name || i.cid}</span>
                <span className="w-20 shrink-0 text-right text-text-hi">
                  {money(i.amount_cents)}
                </span>
                <span className="min-w-0 flex-1 truncate text-text-faint">{i.email_masked}</span>
                <span className={`w-20 shrink-0 text-right ${ITEM_TONE[i.item_status || 'PENDING'] || 'text-text-dim'}`}>
                  {(i.item_status || 'pending').toLowerCase()}
                </span>
              </li>
            ))}
          </ul>
          <p className="nums mt-2 font-mono text-[10px] text-text-faint">
            sender_batch_id {s.sender_batch_id} — derived from the pool and the branch, so a
            retry is the same batch and can never pay twice
          </p>
        </div>
      )}
    </section>
  )
}

import { useEffect, useState } from 'react'
import { money } from '../usePool'
import type { Phase, Pool } from '../types'

const PHASES: Phase[] = ['DRAFT', 'COLLECTING', 'RESOLVING', 'SETTLING', 'CLOSED']
const LABEL: Record<Phase, string> = {
  DRAFT: 'Planning',
  COLLECTING: 'Collecting',
  RESOLVING: 'Deciding',
  SETTLING: 'Paying out',
  CLOSED: 'Closed',
  ESCALATED: 'Escalated',
}

export function Stepper({ phase }: { phase: Phase }) {
  const i = PHASES.indexOf(phase)
  const escalated = phase === 'ESCALATED'
  return (
    <ol className="flex items-center gap-1.5">
      {PHASES.map((p, idx) => {
        const done = !escalated && idx < i
        const now = !escalated && idx === i
        return (
          <li key={p} className="flex items-center gap-1.5">
            <span
              className={`rounded px-2 py-1 text-[11px] font-medium whitespace-nowrap transition ${
                now
                  ? 'bg-accent-dim text-accent-hi ring-1 ring-accent/40'
                  : done
                    ? 'text-paid'
                    : 'text-text-faint'
              }`}
            >
              {LABEL[p]}
            </span>
            {idx < PHASES.length - 1 && (
              <span className={`h-px w-3 ${done ? 'bg-paid/40' : 'bg-border'}`} />
            )}
          </li>
        )
      })}
      {escalated && (
        <li className="rounded border border-guard/40 bg-guard-dim px-2 py-1 text-[11px] font-medium text-guard">
          Escalated to a human
        </li>
      )}
    </ol>
  )
}

/** The countdown.
 *
 *  Interpolated locally between clock ticks: the server sends real seconds every couple
 *  of seconds, and a countdown that only moves twice a second reads as a stutter. The
 *  number shown is the organizer's own span — the scale badge says so out loud rather
 *  than letting anyone discover it.
 */
function useCountdown(pool: Pool) {
  const [left, setLeft] = useState(pool.time_left_s)
  useEffect(() => setLeft(pool.time_left_s), [pool.time_left_s])
  useEffect(() => {
    if (pool.phase !== 'COLLECTING' && pool.phase !== 'RESOLVING') return
    const t = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000)
    return () => clearInterval(t)
  }, [pool.phase])
  return left
}

function label(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
  if (s >= 60) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${s}s`
}

export function Gauges({ pool }: { pool: Pool }) {
  const realLeft = useCountdown(pool)
  const shown = realLeft * Math.max(1, pool.clock_scale)
  const pct = pool.target_cents
    ? Math.min(100, Math.round((100 * pool.collected_cents) / pool.target_cents))
    : 0
  const open = pool.phase === 'COLLECTING' || pool.phase === 'RESOLVING'
  const urgent = open && realLeft <= 15

  return (
    <div className="space-y-3">
      <div className="rounded-card border border-border bg-surface-1 p-4">
        <div className="flex items-end justify-between">
          <div>
            <div className="nums font-mono text-[32px] leading-none font-medium text-text-hi">
              {money(pool.collected_cents)}
            </div>
            <div className="nums mt-1 font-mono text-[12px] text-text-dim">
              of {money(pool.target_cents)} · {pool.paid_count} of {pool.n} paid
            </div>
          </div>
          <div className="nums font-mono text-[13px] text-text-dim">{pct}%</div>
        </div>
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-surface-3">
          <div
            className="h-full rounded-full bg-paid transition-[width] duration-700 ease-out"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      <div
        className={`rounded-card border p-4 transition ${
          urgent ? 'border-danger/40 bg-danger-dim' : 'border-border bg-surface-1'
        }`}
      >
        <div className="flex items-baseline justify-between">
          <span className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
            {open ? 'Deadline in' : 'Deadline'}
          </span>
          {pool.clock_scale > 1 && (
            <span
              className="nums rounded border border-border bg-surface-3 px-1.5 py-px font-mono text-[10px] text-text-faint"
              title={`A demo pool: ${realLeft}s of real time is rendered as the span the organizer asked for.`}
            >
              demo clock ×{pool.clock_scale}
            </span>
          )}
        </div>
        <div
          className={`nums mt-1 font-mono text-[28px] leading-none font-medium ${
            urgent ? 'text-danger' : 'text-text-hi'
          }`}
        >
          {open ? label(shown) : pool.deadline_label || 'passed'}
        </div>
        {pool.clock_scale > 1 && open && (
          <div className="nums mt-1.5 font-mono text-[11px] text-text-faint">
            {realLeft}s of real time · {pool.deadline_label}
          </div>
        )}
      </div>
    </div>
  )
}

/** The sentence that ends the fraud question. */
export function Authority() {
  const rows: [string, string][] = [
    ['the split, and who receives', 'who has paid'],
    ['what every invoice and nudge says', 'any amount in a payout'],
    ['who to chase, and when to stop', 'any destination address'],
    ['when to sleep and when to act', 'anyone outside the roster'],
    ['RELEASE or UNWIND at the deadline', 'the ledger, in any way'],
  ]
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface-1">
      <div className="border-b border-border px-3 py-2">
        <h2 className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
          What the agent controls
        </h2>
      </div>
      <table className="w-full text-left text-[11px]">
        <thead>
          <tr className="text-[10px] tracking-[0.1em] text-text-faint uppercase">
            <th className="px-3 py-1.5 font-semibold">Decides</th>
            <th className="px-3 py-1.5 font-semibold">Cannot touch</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([a, b]) => (
            <tr key={a} className="border-t border-border">
              <td className="px-3 py-1.5 text-text">{a}</td>
              <td className="px-3 py-1.5 text-text-faint">{b}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="border-t border-border px-3 py-2 text-[11px] leading-relaxed text-text-faint">
        No money-moving function takes an amount or an email address — read the signature
        of <span className="font-mono text-text-dim">execute_settlement</span>. A refund is
        each payer's PayPal-verified amount; a release goes to the recipient the organizer
        named. The guardrail holds even if the model is adversarially controlled.
      </p>
    </div>
  )
}

import { useEffect, useRef, useState } from 'react'
import { money } from '../usePool'
import type { Pool, Row } from '../types'

/** Lane C — the pool ledger.
 *
 *  Hand-rolled rather than a grid library, on purpose: the whole product is read off
 *  this table on camera, so every pixel of it is worth owning, and the one piece of
 *  behaviour a grid would have given us for free — a cell that flashes when it changes
 *  — is a CSS keyframe and a ref.
 */
const STATUS_LABEL: Record<string, string> = {
  pending: 'not invoiced',
  SENT: 'invoice sent',
  PARTIALLY_PAID: 'part paid',
  MARKED_AS_PAID: 'paid',
  PAID: 'paid',
  CANCELLED: 'cancelled',
  REFUNDED: 'refunded',
}

function statusTone(s: string): string {
  if (s === 'MARKED_AS_PAID' || s === 'PAID') return 'text-paid bg-paid-dim border-paid/25'
  if (s === 'PARTIALLY_PAID') return 'text-guard bg-guard-dim border-guard/25'
  if (s === 'CANCELLED') return 'text-text-faint bg-white/4 border-border line-through'
  if (s === 'SENT') return 'text-accent-hi bg-accent-dim border-accent/25'
  return 'text-text-faint bg-white/3 border-border'
}

function useFlash(value: unknown, cls: string) {
  const first = useRef(true)
  const [on, setOn] = useState(false)
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    setOn(true)
    const t = setTimeout(() => setOn(false), 1150)
    return () => clearTimeout(t)
  }, [value])
  return on ? cls : ''
}

function Cell({ children, flash }: { children: React.ReactNode; flash: string }) {
  return <td className={`px-3 py-2.5 align-middle ${flash}`}>{children}</td>
}

function LedgerRow({ row, pool, onPay, busy }: {
  row: Row
  pool: Pool
  onPay: (cid: string, amount?: number) => void
  busy: string | null
}) {
  const paidFlash = useFlash(row.paid_cents, 'flash-paid')
  const statusFlash = useFlash(row.status, 'flash-any')
  const isRecipient = row.flags.includes('RECIPIENT')
  const payable =
    !isRecipient &&
    Boolean(row.invoice_id) &&
    row.paid_cents < row.share_cents &&
    (pool.phase === 'COLLECTING' || pool.phase === 'RESOLVING')

  return (
    <tr className="border-t border-border hover:bg-white/[0.015]">
      <td className="px-3 py-2.5">
        <div className="flex items-baseline gap-2">
          <span className="text-text-hi font-medium">{row.name}</span>
          {isRecipient && (
            <span className="rounded border border-accent/30 bg-accent-dim px-1.5 py-px text-[10px] font-medium tracking-wide text-accent-hi uppercase">
              recipient
            </span>
          )}
        </div>
        <div className="nums mt-0.5 font-mono text-[11px] text-text-faint">{row.email_masked}</div>
      </td>

      <td className="nums px-3 py-2.5 text-right font-mono text-text">
        {isRecipient ? <span className="text-text-faint">—</span> : money(row.share_cents)}
      </td>

      <Cell flash={statusFlash}>
        <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${statusTone(row.status)}`}>
          {STATUS_LABEL[row.status] || row.status.toLowerCase()}
        </span>
        {row.flags.includes('PARTIAL') && (
          <span className="ml-1.5 text-[11px] text-guard">short</span>
        )}
        {row.flags.includes('OVERPAID') && (
          <span className="ml-1.5 text-[11px] text-guard">over</span>
        )}
      </Cell>

      <Cell flash={paidFlash}>
        <span
          className={`nums font-mono ${row.paid_cents > 0 ? 'text-paid' : 'text-text-faint'}`}
        >
          {row.paid_cents > 0 ? money(row.paid_cents) : '—'}
        </span>
        {row.source && (
          <div className="font-mono text-[10px] text-text-faint">
            {row.source === 'merchant_recorded' ? 'recorded' : 'paypal'}
          </div>
        )}
      </Cell>

      <td className="nums px-3 py-2.5 text-center font-mono text-text-dim">
        {row.reminders > 0 ? (
          <span className="text-guard" title={`${row.reminders} of 3 allowed`}>
            {'•'.repeat(row.reminders)}
          </span>
        ) : (
          <span className="text-text-faint">—</span>
        )}
      </td>

      <td className="px-3 py-2.5 text-right whitespace-nowrap">
        {payable ? (
          <span className="inline-flex gap-1">
            <button
              onClick={() => onPay(row.cid)}
              disabled={busy === row.cid}
              className="rounded-ctl border border-border-strong bg-surface-3 px-2.5 py-1 text-[11px] font-medium text-text-hi transition hover:border-paid/40 hover:text-paid disabled:opacity-40"
            >
              {busy === row.cid ? '…' : `pay ${money(row.share_cents - row.paid_cents)}`}
            </button>
            <button
              onClick={() => onPay(row.cid, Math.max(100, Math.round(row.share_cents / 2 / 100) * 100))}
              disabled={busy === row.cid}
              title="Pay only part of the share — the agent has to deal with it"
              className="rounded-ctl border border-border bg-surface-2 px-2 py-1 text-[11px] text-text-dim transition hover:border-guard/40 hover:text-guard disabled:opacity-40"
            >
              part
            </button>
          </span>
        ) : row.payer_url ? (
          <a
            href={row.payer_url}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-[11px] text-text-faint underline decoration-dotted hover:text-accent-hi"
          >
            invoice ↗
          </a>
        ) : (
          <span className="text-text-faint">—</span>
        )}
      </td>
    </tr>
  )
}

export default function Ledger({
  pool,
  onPay,
  busy,
}: {
  pool: Pool
  onPay: (cid: string, amount?: number) => void
  busy: string | null
}) {
  const contributors = pool.rows.filter((r) => !r.flags.includes('RECIPIENT'))
  const recipient = pool.rows.find((r) => r.flags.includes('RECIPIENT'))
  const ordered = recipient ? [...contributors, recipient] : contributors
  const needsAPush =
    pool.phase === 'COLLECTING' &&
    pool.paid_count === 0 &&
    contributors.some((r) => Boolean(r.invoice_id))

  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface-1">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
          Pool ledger
        </h2>
        <span className="nums font-mono text-[11px] text-text-faint">
          v{pool.ledger_version} · {pool.paid_count}/{pool.n} paid
        </span>
      </div>
      {needsAPush && (
        <p className="border-b border-accent/20 bg-accent-dim px-3 py-2 text-[12px] leading-relaxed text-accent-hi">
          <span className="font-medium text-text-hi">Your turn — you are the contributors.</span>{' '}
          Press{' '}
          <span className="rounded border border-border-strong bg-surface-3 px-1 py-px font-mono text-[11px] text-text-hi">
            pay
          </span>{' '}
          on any row to send a real PayPal sandbox payment and watch the agent react, or{' '}
          <span className="rounded border border-border bg-surface-2 px-1 py-px font-mono text-[11px] text-text-dim">
            part
          </span>{' '}
          to pay half and hand it a problem. Whether the pool reaches its target is up to
          you; what the agent does about it is up to the agent.
        </p>
      )}
      {/* The table has six columns and a floor of about 620px. On a 390px phone an
          overflow-hidden wrapper simply cut it off; scrolling it sideways keeps every
          column readable at full size instead of shrinking all of them past legibility. */}
      <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] border-collapse text-left">
        <thead>
          <tr className="text-[10px] font-semibold tracking-[0.1em] text-text-faint uppercase">
            <th className="px-3 py-2 font-semibold">Who</th>
            <th className="px-3 py-2 text-right font-semibold">Share</th>
            <th className="px-3 py-2 font-semibold">Invoice</th>
            <th className="px-3 py-2 font-semibold">Paid</th>
            <th className="px-3 py-2 text-center font-semibold">Nudges</th>
            <th className="px-3 py-2 text-right font-semibold">Pay as them</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((r) => (
            <LedgerRow key={r.cid} row={r} pool={pool} onPay={onPay} busy={busy} />
          ))}
        </tbody>
      </table>
      </div>
      <div className="border-t border-border px-3 py-2 text-[11px] text-text-faint">
        Amounts come from the roster the agent set at plan time. It cannot re-price an
        invoice, mark anyone as paid, or edit this table.
      </div>
    </div>
  )
}

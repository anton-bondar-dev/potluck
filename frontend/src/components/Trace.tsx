import { useEffect, useRef } from 'react'
import type { TraceItem } from '../types'

/** Lane A — what the agent thought, chose and was refused.
 *
 *  Every string here was written by the model and is rendered as TEXT, never as HTML:
 *  the organizer's goal and the contributors' notes reach this column, and on a public
 *  demo that is untrusted input.
 */
const KIND: Record<TraceItem['kind'], { dot: string; label: string }> = {
  episode: { dot: 'bg-accent', label: 'episode' },
  thought: { dot: 'bg-accent/50', label: 'reasoning' },
  note: { dot: 'bg-paid/70', label: 'says' },
  step: { dot: 'bg-accent', label: 'action' },
  guard: { dot: 'bg-guard', label: 'refused' },
  deferred: { dot: 'bg-ticker', label: 'deferred' },
  phase: { dot: 'bg-text-dim', label: 'phase' },
  sleep: { dot: 'bg-ticker', label: 'asleep' },
  wake: { dot: 'bg-accent', label: 'awake' },
}

function Args({ args }: { args: unknown }) {
  if (!args || typeof args !== 'object') return null
  const entries = Object.entries(args as Record<string, unknown>)
  if (!entries.length) return null
  return (
    <dl className="mt-1.5 space-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-2 text-[11px] leading-relaxed">
          <dt className="shrink-0 font-mono text-text-faint">{k}</dt>
          <dd className="min-w-0 break-words text-text">
            {typeof v === 'string' ? v : JSON.stringify(v)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function Card({ item }: { item: TraceItem }) {
  const k = KIND[item.kind]
  const isGuard = item.kind === 'guard'
  return (
    <li className="rise relative pl-5">
      <span
        className={`absolute top-[7px] left-0 h-2 w-2 rounded-full ${k.dot} ${
          item.kind === 'sleep' ? 'pulse-ring' : ''
        }`}
      />
      <div
        className={`rounded-ctl border px-2.5 py-2 ${
          isGuard
            ? 'border-guard/30 bg-guard-dim'
            : item.kind === 'step'
              ? 'border-border-strong bg-surface-2'
              : 'border-border bg-surface-1'
        }`}
      >
        <div className="flex items-baseline justify-between gap-2">
          <span
            className={`text-[12px] font-medium ${
              isGuard ? 'text-guard' : item.kind === 'step' ? 'text-accent-hi' : 'text-text-hi'
            }`}
          >
            {item.kind === 'step' ? item.tool : item.title}
          </span>
          <span className="shrink-0 font-mono text-[10px] tracking-wide text-text-faint uppercase">
            {k.label}
          </span>
        </div>

        {item.body && (
          <p className="mt-1 text-[12px] leading-relaxed whitespace-pre-wrap text-text-dim">
            {item.body}
          </p>
        )}
        {item.reason && item.kind === 'sleep' && (
          <p className="mt-0.5 font-mono text-[11px] text-text-faint">until {item.reason}</p>
        )}
        {item.kind === 'step' && <Args args={item.args} />}
        {item.summary !== undefined && (
          <p
            className={`nums mt-1.5 border-t border-border pt-1.5 font-mono text-[11px] ${
              item.ok ? 'text-paid' : 'text-guard'
            }`}
          >
            {item.summary}
          </p>
        )}
        {item.tokens && (
          <p className="nums mt-1 font-mono text-[10px] text-text-faint">
            {item.tokens.prompt}+{item.tokens.completion} tokens
          </p>
        )}
      </div>
    </li>
  )
}

export default function Trace({ items }: { items: TraceItem[] }) {
  const end = useRef<HTMLDivElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  useEffect(() => {
    const el = box.current
    if (!el) return
    const onScroll = () => {
      pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (pinned.current) end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [items.length])

  return (
    <div className="flex min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface-1/50">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
          Agent trace
        </h2>
        <span className="font-mono text-[11px] text-text-faint">{items.length}</span>
      </div>
      <div ref={box} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {items.length === 0 ? (
          <p className="py-6 text-center text-[12px] text-text-faint">
            Waiting for the agent to wake up.
          </p>
        ) : (
          <ol className="space-y-2">
            {items.map((i) => (
              <Card key={i.id} item={i} />
            ))}
          </ol>
        )}
        <div ref={end} />
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { createPool, getCast } from './api'
import type { CastMember } from './types'

/** Three presets, each written to be falsifiable.
 *
 *  The twin is the point: identical numbers, one phrase changed, and the agent takes the
 *  opposite branch. A judge can run both and compare — no schema could hold the second
 *  condition, because it counts PEOPLE rather than money.
 */
const PRESETS: { label: string; hint: string; goal: string; policy: string }[] = [
  {
    label: "Dana's leaving gift",
    hint: 'all-or-nothing · the classic',
    goal:
      "We're getting Dana a leaving present. Five of us, $45 each, so $180 in total. " +
      'I need it by Friday morning because the party is Friday lunchtime.',
    policy:
      'Only buy the gift if we actually reach $180 by Friday. If we fall short, give ' +
      "everyone their money back — I'd rather no gift than a half-paid one.",
  },
  {
    label: 'The twin: a count, not an amount',
    hint: 'same numbers, opposite branch',
    goal:
      "We're getting Dana a leaving present. Five of us, $45 each, so $180 in total. " +
      'I need it by Friday morning because the party is Friday lunchtime.',
    // Deliberately a condition the arithmetic cannot satisfy on its own. "At least 4 of
    // 5" was useless as a proof: with four payers at $45 the head-count and the total
    // are met together, so both presets release and the comparison shows nothing.
    // Requiring ALL five makes four payers reach $180 and still fail — same numbers,
    // opposite branch. Verified against the live agent twice.
    policy:
      'This only works if the whole team is in it, so refund everyone unless all five ' +
      'of us actually chip in. Even if the money adds up to $180 with four payers, give ' +
      "it all back — I don't want a gift that one person skipped.",
  },
  {
    label: 'Close enough',
    hint: 'the organizer covers the gap',
    goal:
      'Ski cabin deposit for the trip — $45 each from the five of us, $180 to the owner, ' +
      'needed before the weekend or we lose the booking.',
    policy:
      "Release it if we're within $20 of the total — I'll cover the rest myself. Below " +
      'that, refund everyone and I will find another cabin.',
  },
]

export default function CreateScreen({ onStart }: { onStart: (poolId: string) => void }) {
  const [cast, setCast] = useState<CastMember[]>([])
  const [goal, setGoal] = useState(PRESETS[0].goal)
  const [policy, setPolicy] = useState(PRESETS[0].policy)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getCast()
      .then(setCast)
      .catch((e) => setErr((e as Error).message))
  }, [])

  const start = async () => {
    setBusy(true)
    setErr(null)
    try {
      onStart(await createPool({ goal_text: goal.trim(), policy_text: policy.trim() }))
    } catch (e) {
      setErr((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-5 py-10 sm:py-16">
      <header>
        <h1 className="text-[28px] leading-tight font-semibold tracking-tight text-text-hi sm:text-[34px]">
          Potluck
        </h1>
        <p className="mt-2 max-w-xl text-[15px] leading-relaxed text-text-dim">
          Say what you're collecting for, in your own words. An AI agent invoices everyone
          on PayPal, watches the money arrive, chases whoever stalls, and at the deadline
          either releases the pool or refunds every contributor to the cent.
        </p>
        <p className="mt-2 max-w-xl text-[13px] leading-relaxed text-text-faint">
          Nothing here parses your success condition. The agent restates it in its own
          words, you see the restatement, and it is held to that at the deadline.
        </p>
      </header>

      <div className="mt-8 flex flex-wrap gap-2">
        {PRESETS.map((p) => (
          <button
            key={p.label}
            onClick={() => {
              setGoal(p.goal)
              setPolicy(p.policy)
            }}
            className={`rounded-ctl border px-3 py-2 text-left transition ${
              goal === p.goal && policy === p.policy
                ? 'border-accent/50 bg-accent-dim'
                : 'border-border bg-surface-1 hover:border-border-strong'
            }`}
          >
            <span className="block text-[12px] font-medium text-text-hi">{p.label}</span>
            <span className="block text-[11px] text-text-faint">{p.hint}</span>
          </button>
        ))}
      </div>

      <div className="mt-6 space-y-4">
        <label className="block">
          <span className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
            What are you collecting for
          </span>
          <textarea
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={3}
            maxLength={600}
            className="mt-1.5 w-full resize-y rounded-ctl border border-border bg-surface-1 px-3 py-2.5 text-[14px] leading-relaxed text-text-hi outline-none transition focus:border-accent/50"
          />
        </label>

        <label className="block">
          <span className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
            When should it go ahead — and what if it doesn't
          </span>
          <textarea
            value={policy}
            onChange={(e) => setPolicy(e.target.value)}
            rows={3}
            maxLength={400}
            className="mt-1.5 w-full resize-y rounded-ctl border border-border bg-surface-1 px-3 py-2.5 text-[14px] leading-relaxed text-text-hi outline-none transition focus:border-accent/50"
          />
          <span className="mt-1 block text-[11px] text-text-faint">
            Write a condition no form could hold — a count of people, a tolerance you'd
            cover yourself, an exception for one person. The agent reads it; nothing parses
            it. Run this preset and the first one with the same four payers: identical
            arithmetic, opposite branch.
          </span>
        </label>
      </div>

      {cast.length > 0 && (
        <div className="mt-6 rounded-card border border-border bg-surface-1 p-3">
          <div className="text-[11px] font-semibold tracking-[0.13em] text-text-dim uppercase">
            Who's in — the frozen roster
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {cast.map((c) => (
              <span
                key={c.cid}
                className="rounded border border-border bg-surface-2 px-2 py-1 font-mono text-[11px] text-text"
                title={c.email_masked}
              >
                <span className="text-text-faint">{c.cid}</span> {c.name}
              </span>
            ))}
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-text-faint">
            PayPal sandbox accounts. The agent refers to people by id and has no way to
            name an address — which is what stops a public demo being driven to invoice a
            stranger.
          </p>
        </div>
      )}

      {err && (
        <p className="mt-4 rounded-ctl border border-danger/30 bg-danger-dim px-3 py-2 text-[13px] text-danger">
          {err}
        </p>
      )}

      <button
        onClick={start}
        disabled={busy || goal.trim().length < 10 || policy.trim().length < 5}
        className="mt-6 w-full rounded-ctl bg-accent px-4 py-3 text-[15px] font-semibold text-white transition hover:bg-accent-hi disabled:opacity-40 sm:w-auto sm:px-6"
      >
        {busy ? 'Starting the pool…' : 'Hand it to the agent'}
      </button>
      <p className="mt-3 text-[11px] text-text-faint">
        Real PayPal sandbox invoices and payouts. A demo pool runs its three days in about
        ninety seconds — the countdown says so.
      </p>
    </div>
  )
}

import type { CastMember, Pool } from './types'

async function detail(r: Response, fallback: string): Promise<string> {
  try {
    const e = await r.json()
    if (e && typeof e.detail === 'string') return e.detail
    if (e && e.detail) return JSON.stringify(e.detail)
  } catch {
    /* non-JSON body */
  }
  return fallback
}

export async function getCast(): Promise<CastMember[]> {
  const r = await fetch('/api/cast')
  if (!r.ok) throw new Error(await detail(r, `Could not load the cast (${r.status})`))
  return (await r.json()).cast as CastMember[]
}

export async function createPool(body: {
  goal_text: string
  policy_text: string
  cids?: string[]
}): Promise<string> {
  const r = await fetch('/api/pools', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, demo: true }),
  })
  if (!r.ok) throw new Error(await detail(r, `Could not start the pool (${r.status})`))
  return (await r.json()).pool_id as string
}

export async function getPool(poolId: string): Promise<Pool> {
  const r = await fetch(`/api/pools/${poolId}`)
  if (!r.ok) throw new Error(await detail(r, `Could not load the pool (${r.status})`))
  return (await r.json()) as Pool
}

export async function payAs(poolId: string, cid: string, amountCents?: number) {
  const r = await fetch(`/api/pools/${poolId}/pay/${cid}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(amountCents ? { amount_cents: amountCents } : {}),
  })
  if (!r.ok) throw new Error(await detail(r, `That payment was refused (${r.status})`))
  return await r.json()
}

export async function messageAgent(poolId: string, text: string) {
  const r = await fetch(`/api/pools/${poolId}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!r.ok) throw new Error(await detail(r, `The agent did not take that (${r.status})`))
  return await r.json()
}

export async function approveRefund(poolId: string, note = '') {
  const r = await fetch(`/api/pools/${poolId}/approve-refund`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note }),
  })
  if (!r.ok) throw new Error(await detail(r, `The refund was refused (${r.status})`))
  return await r.json()
}

export function streamUrl(poolId: string, fromSeq = 0): string {
  return `/api/pools/${poolId}/stream${fromSeq ? `?from_seq=${fromSeq}` : ''}`
}

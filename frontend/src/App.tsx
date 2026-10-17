import { useCallback, useEffect, useState } from 'react'
import CreateScreen from './CreateScreen'
import PoolScreen from './PoolScreen'

/** Routing is two states and one path shape: /p/{pool_id}.
 *
 *  A pool's URL has to be shareable and refreshable — that is the whole point of the
 *  durable event log behind it — so the pool id lives in the address bar, not in state.
 */
function poolFromPath(): string | null {
  const m = window.location.pathname.match(/^\/p\/([A-Za-z0-9_-]+)\/?$/)
  return m ? m[1] : null
}

export default function App() {
  const [poolId, setPoolId] = useState<string | null>(poolFromPath)

  useEffect(() => {
    const onPop = () => setPoolId(poolFromPath())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const open = useCallback((id: string) => {
    window.history.pushState({}, '', `/p/${id}`)
    setPoolId(id)
  }, [])

  const reset = useCallback(() => {
    window.history.pushState({}, '', '/')
    setPoolId(null)
  }, [])

  return poolId ? (
    <PoolScreen poolId={poolId} onNew={reset} />
  ) : (
    <CreateScreen onStart={open} />
  )
}

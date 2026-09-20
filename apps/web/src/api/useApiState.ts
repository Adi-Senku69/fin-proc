import { useCallback, useEffect, useState } from 'react'

import type { ApiResult } from './client'

export type ApiState<T> =
  | { status: 'loading' }
  | { status: 'error'; httpStatus: number; message: string }
  | { status: 'ready'; data: T }

/**
 * Runs `fetcher` whenever `deps` changes and exposes the result as an exhaustive
 * loading/error/ready union — the same three states every screen in the vanilla UI
 * (nvplan/api/static/js/dom.js's loadingPanel / errorPanel / the rendered view) distinguishes,
 * so a fetch failure or an empty result is never mistaken for "still loading".
 */
export function useApiState<T>(
  fetcher: (signal: AbortSignal) => Promise<ApiResult<T>>,
  deps: readonly unknown[],
): { state: ApiState<T>; reload: () => void } {
  const [state, setState] = useState<ApiState<T>>({ status: 'loading' })
  const [nonce, setNonce] = useState(0)

  const load = useCallback(
    (signal: AbortSignal) => {
      setState({ status: 'loading' })
      fetcher(signal)
        .then((res) => {
          if (signal.aborted) return
          if (res.ok) setState({ status: 'ready', data: res.data })
          else setState({ status: 'error', httpStatus: res.status, message: res.message })
        })
        .catch((err: unknown) => {
          if (signal.aborted) return
          setState({ status: 'error', httpStatus: 0, message: err instanceof Error ? err.message : String(err) })
        })
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [...deps, nonce],
  )

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  return { state, reload: () => setNonce((n) => n + 1) }
}

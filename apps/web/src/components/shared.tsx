/**
 * Small shared building blocks — ported 1:1 from nvplan/api/static/js/dom.js's panel helpers so
 * the React screens read as the same product: a failed fetch is always a specific, visible
 * panel (never a blank one), and a code/identifier is always secondary to its plain label.
 */
import type { ReactNode } from 'react'

import type { ApiState } from '../api/useApiState'

export function Chip({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <span className={`chip ${className}`.trim()}>{children}</span>
}

/** A muted secondary badge carrying the raw technical identifier — never the only thing shown. */
export function CodeTag({ children }: { children: string | number | null | undefined }) {
  if (children === null || children === undefined || children === '') return null
  return (
    <span className="term-code" title={String(children)}>
      {children}
    </span>
  )
}

export function LoadingPanel({ text = 'Loading...' }: { text?: string }) {
  return (
    <div className="state-panel loading-panel">
      <span className="spinner" aria-hidden="true" />
      <span>{text}</span>
    </div>
  )
}

export function EmptyPanel({ text, action }: { text: string; action?: ReactNode }) {
  return (
    <div className="state-panel empty-panel">
      <div className="empty-text">{text}</div>
      {action}
    </div>
  )
}

/** A visible, specific failure: status code + the server's own message. */
export function ErrorPanel({
  httpStatus,
  message,
  retry,
}: {
  httpStatus: number
  message: string
  retry?: () => void
}) {
  if (httpStatus === 404) {
    return (
      <div className="panel not-available">
        <div className="error-title">Endpoint not available yet</div>
        <div className="error-message">
          This route returned 404. This can mean the route is not wired up on the backend yet. Retry once it is.
        </div>
        {retry && (
          <button className="btn btn-small" onClick={retry}>
            Retry
          </button>
        )}
      </div>
    )
  }
  return (
    <div className="panel error-panel">
      <div className="error-title">Request failed — HTTP {httpStatus || '0 (network)'}</div>
      <div className="error-message">{message || 'unknown error'}</div>
      {retry && (
        <button className="btn btn-small" onClick={retry}>
          Retry
        </button>
      )}
    </div>
  )
}

/** Renders the loading/error state for an `ApiState`, or `children(data)` once ready — every
 * screen's fetch boundary funnels through this one component. */
export function ApiBoundary<T>({
  state,
  loadingText,
  reload,
  children,
}: {
  state: ApiState<T>
  loadingText?: string
  reload?: () => void
  children: (data: T) => ReactNode
}) {
  if (state.status === 'loading') return <LoadingPanel text={loadingText} />
  if (state.status === 'error') return <ErrorPanel httpStatus={state.httpStatus} message={state.message} retry={reload} />
  return <>{children(state.data)}</>
}

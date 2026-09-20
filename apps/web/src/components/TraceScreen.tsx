/**
 * The standalone Trace screen — ported from nvplan/api/static/js/views/trace.js's `render`.
 * "The lineage tree for one figure. Every node here is exactly what the API returned — nothing
 * is computed in this page."
 */
import { useRef, useState } from 'react'

import { fetchTrace, fetchTraceText, type TraceKind } from '../api/adapters/trace'
import { useApiState } from '../api/useApiState'
import type { Route } from '../routing/route'
import { ApiBoundary } from './shared'
import { TraceNodeView } from './TraceTree'

type Navigate = (p: Record<string, string | number | null | undefined>) => void

export function TraceScreen({ route, navigate }: { route: Route; navigate: Navigate }) {
  const kindParam = route.params.get('kind')
  const kind: TraceKind = kindParam === 'statement-line' ? 'statement-line' : 'plan-value'
  const idParam = route.params.get('id') || ''

  const [kindInput, setKindInput] = useState<TraceKind>(kind)
  const [idInput, setIdInput] = useState(idParam)
  const [textView, setTextView] = useState<string | null>(null)
  const [textLoading, setTextLoading] = useState(false)
  const treeRef = useRef<HTMLDivElement>(null)

  const { state, reload } = useApiState(() => fetchTrace(kind, idParam), [kind, idParam])

  /** Same rule as nvplan/api/static/js/views/trace.js's `setAllOpen`: every `<details>` under
   * the tree opens or closes together. `<details>` is a native, uncontrolled element here (its
   * default `open` is set once per node by TraceTree from `depth < 2 && !node.ref`), so this
   * reaches into the real DOM rather than tracking per-node React state for an arbitrarily deep
   * tree. */
  function setAllOpen(open: boolean) {
    treeRef.current?.querySelectorAll('details.trace-node').forEach((d) => {
      if (open) d.setAttribute('open', '')
      else d.removeAttribute('open')
    })
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    navigate({ view: 'trace', kind: kindInput, id: idInput })
  }

  async function toggleTextView() {
    if (textView !== null) {
      setTextView(null)
      return
    }
    setTextLoading(true)
    const res = await fetchTraceText(kind, idParam)
    setTextLoading(false)
    setTextView(res.ok ? res.data : `HTTP ${res.status}: ${res.message}`)
  }

  return (
    <div>
      <h2>Trace</h2>
      <p className="muted">
        The lineage tree for one figure. Every node here is exactly what the API returned — nothing is computed in
        this page.
      </p>

      <form className="trace-form" onSubmit={submit}>
        <label>
          kind{' '}
          <select value={kindInput} onChange={(e) => setKindInput(e.target.value as TraceKind)}>
            <option value="plan-value">plan value</option>
            <option value="statement-line">statement line</option>
          </select>
        </label>
        <label>
          id{' '}
          <input type="number" min={1} value={idInput} onChange={(e) => setIdInput(e.target.value)} placeholder="id" />
        </label>
        <button type="submit" className="btn">
          Open
        </button>
      </form>

      {!idParam ? (
        <p className="muted">Enter a plan-value or statement-line id above, or click a cell in Plan.</p>
      ) : (
        <div className="trace-container">
          <ApiBoundary state={state} loadingText="Loading trace..." reload={reload}>
            {(node) => (
              <>
                <div className="trace-toolbar">
                  <button className="btn btn-small" onClick={() => setAllOpen(true)}>
                    Expand all
                  </button>
                  <button className="btn btn-small" onClick={() => setAllOpen(false)}>
                    Collapse all
                  </button>
                  <button className="btn btn-small" onClick={toggleTextView}>
                    {textLoading ? 'Loading...' : textView !== null ? 'Hide plain text' : 'View as plain text'}
                  </button>
                </div>
                {textView !== null && <pre className="prompt-box">{textView}</pre>}
                <div ref={treeRef}>
                  <TraceNodeView node={node} onOpenBrain={(claimId) => navigate({ view: 'brain', claim: claimId })} />
                </div>
              </>
            )}
          </ApiBoundary>
        </div>
      )}
    </div>
  )
}

/**
 * The lineage tree — ported from nvplan/api/static/js/views/trace.js's `renderTree`. Renders
 * exactly the shape `GET /trace/plan-value/{id}` / `GET /trace/statement-line/{id}` returned
 * (`../domain/trace.ts`); nothing here computes a number. Shared by the standalone Trace screen,
 * the Plan screen's click-through side panel, and Brain's impact links — one renderer, so the
 * three call sites can never drift into three different trees.
 */
import { Fragment } from 'react'

import { fmtDate, fmtNum, fmtParam } from '../domain/format'
import { MARKED_PATHS, paramTerm, pathTerm, tagTerm, touchpointLabel } from '../domain/terms'
import type { TraceClaim, TraceNode } from '../domain/trace'
import { Chip, CodeTag } from './shared'

function KvTable({ entries, termed = false }: { entries: [string, unknown][]; termed?: boolean }) {
  if (!entries.length) return null
  return (
    <table className="kv-table">
      <tbody>
        {entries.map(([k, v]) => {
          const term = termed ? paramTerm(k) : null
          const isFigure = typeof v === 'number' && Number.isFinite(v)
          const text = typeof v === 'object' && v !== null ? JSON.stringify(v) : fmtParam(k, v)
          return (
            <tr key={k}>
              <th>
                {term ? (
                  <>
                    {term.label} <CodeTag>{term.symbol || k}</CodeTag>
                  </>
                ) : (
                  k
                )}
              </th>
              <td className={isFigure ? '' : 'kv-value-prose'}>{text}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function PointsTable({ points }: { points: Record<string, unknown>[] | undefined }) {
  if (!Array.isArray(points) || !points.length) return null
  const cols = Object.keys(points[0])
  return (
    <details className="points-details">
      <summary>regression points ({points.length})</summary>
      <table className="data-table">
        <tbody>
          <tr>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
          {points.map((p, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{fmtParam(c, p[c])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  )
}

function AiBlock({ ai }: { ai: NonNullable<TraceNode['ai']> }) {
  return (
    <div className="ai-block">
      <div className="block-title">AI proposal</div>
      <div>
        <Chip className="chip-touchpoint">
          {touchpointLabel(ai.touchpoint)}
          <CodeTag>{ai.touchpoint}</CodeTag>
        </Chip>{' '}
        <Chip className={`chip-status chip-${ai.status}`}>{ai.status}</Chip>
        <span className="muted"> model {ai.model_version}</span>
      </div>
      <div>
        <strong>rationale: </strong>
        {ai.rationale || '-'}
      </div>
      <div>
        <strong>confirmer: </strong>
        {ai.confirmed_by ? `${ai.confirmed_by} on ${fmtDate(ai.confirmed_at)}` : 'not yet confirmed'}
      </div>
      <div className="prompt-label">prompt (verbatim):</div>
      <pre className="prompt-box">{ai.prompt_text || ''}</pre>
    </div>
  )
}

function ClaimBlock({ claim, onOpenBrain }: { claim: TraceClaim; onOpenBrain?: (claimId: number) => void }) {
  return (
    <div className="claim-block">
      <div className="block-title">Decision</div>
      <div>
        <strong>{claim.title || claim.slug}</strong> <Chip className={`chip-status chip-${claim.status}`}>{claim.status}</Chip>
      </div>
      <div className="muted">
        slug: {claim.slug} · decided: {claim.decided_on || '-'}
      </div>
      {claim.reversal_condition ? (
        <div>
          <strong>reversal condition: </strong>
          {claim.reversal_condition}
        </div>
      ) : (
        <div className="muted">reversal condition: not recorded</div>
      )}
      <div className="block-title">Evidence</div>
      <ul className="evidence-list">
        {(claim.evidence || []).map((e, i) => {
          const t = tagTerm(e.tag_raw)
          return (
            <li key={i}>
              <Chip className="chip-tag">{t.label}</Chip>
              <CodeTag>{e.tag_raw}</CodeTag> {e.text}
            </li>
          )
        })}
        {!(claim.evidence || []).length && <li className="muted">(no evidence rows in the trace)</li>}
      </ul>
      {onOpenBrain && (
        <button className="btn btn-small" onClick={() => onOpenBrain(claim.claim_id)}>
          Open this decision in Brain
        </button>
      )}
    </div>
  )
}

export function TraceNodeView({
  node,
  depth = 0,
  onOpenBrain,
}: {
  node: TraceNode
  depth?: number
  onOpenBrain?: (claimId: number) => void
}) {
  const marked = node.path ? MARKED_PATHS.has(node.path) : false
  const pt = node.path ? pathTerm(node.path) : null
  const paramEntries = Object.entries(node.parameters || {}).filter(([k]) => !k.startsWith('_'))
  const inputEntries = Object.entries(node.inputs || {}).filter(([k]) => !k.startsWith('_') && k !== 'points')

  return (
    <details className={`trace-node depth-${Math.min(depth, 6)}`} open={depth < 2 && !node.ref}>
      <summary className="trace-summary">
        <span className="trace-disclosure" aria-hidden="true">
          ▸
        </span>
        <span className="trace-label">{node.label || node.kind}</span>
        {node.value !== null && node.value !== undefined && (
          <span className="trace-value">{fmtNum(node.value)} k EUR</span>
        )}
        {pt && (
          <span className={`path-badge path-${node.path}${marked ? ' path-marked' : ''}`} title={pt.hint}>
            {pt.label}
            <CodeTag>{node.path}</CodeTag>
          </span>
        )}
        {node.source_label && String(node.source_label).includes('ILLUSTRATIVE') && (
          <Chip className="chip-illustrative">ILLUSTRATIVE</Chip>
        )}
        {node.ai && <Chip className="chip-touchpoint">ai</Chip>}
        {node.claim && <Chip className="chip-status chip-decided">decision</Chip>}
        {node.ref && <span className="muted"> (see above)</span>}
        {node.truncated && <span className="muted"> (truncated)</span>}
      </summary>

      {!node.ref && (
        <div className="trace-body">
          {node.formula_text && <div className="formula">{node.formula_text}</div>}
          {paramEntries.length > 0 && (
            <div>
              <div className="block-title">parameters</div>
              <KvTable entries={paramEntries} termed />
            </div>
          )}
          <PointsTable points={node.inputs?.points} />
          {inputEntries.length > 0 && (
            <div>
              <div className="block-title">inputs</div>
              <KvTable entries={inputEntries} />
            </div>
          )}
          {node.source_label && <div className="muted">source: {node.source_label}</div>}
          {node.ai && <AiBlock ai={node.ai} />}
          {node.claim && <ClaimBlock claim={node.claim} onOpenBrain={onOpenBrain} />}
        </div>
      )}

      {node.children && node.children.length > 0 && (
        <div className={`trace-children depth-guide-${depth % 6}`}>
          {node.children.map((child, i) => (
            <Fragment key={i}>
              <TraceNodeView node={child} depth={depth + 1} onOpenBrain={onOpenBrain} />
            </Fragment>
          ))}
        </div>
      )}
    </details>
  )
}

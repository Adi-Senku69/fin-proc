/**
 * Brain screen — ported from nvplan/api/static/js/views/brain.js. Decisions and hypotheses: a
 * validation banner up top (errors distinguished from warnings), a claim list, and a detail pane
 * with evidence tag chips, the reversal condition, the quantified effect, and the impact (which
 * figures moved, each linking into Trace). No counterpart in the reference React app — this is
 * one of the two screens (with Ask) that carry the platform direction (PLATFORM.md), so its
 * layout follows the same split-pane / chip / eyebrow vocabulary as Plan and Trace rather than
 * inventing a new one.
 */
import { fetchClaimDetail, fetchClaimImpact, fetchClaims, fetchValidation } from '../api/adapters/brain'
import type { FindingOut } from '../api/types'
import { useApiState } from '../api/useApiState'
import { fmtNum } from '../domain/format'
import { categoryName, claimKindLabel, MARKED_PATHS, pathTerm, tagTerm } from '../domain/terms'
import type { Route } from '../routing/route'
import { ApiBoundary, Chip, CodeTag, EmptyPanel } from './shared'

type Navigate = (p: Record<string, string | number | null | undefined>) => void

export function BrainScreen({ route, navigate }: { route: Route; navigate: Navigate }) {
  const selectedClaim = route.params.get('claim') || ''

  return (
    <div>
      <h2>Brain</h2>
      <ValidationBanner />
      <div className={`split-layout${selectedClaim ? ' has-selection' : ''}`}>
        <div className="pane list-pane">
          <ClaimList selectedClaim={selectedClaim} navigate={navigate} />
        </div>
        <div className="pane detail-pane">
          {selectedClaim ? (
            <ClaimDetail claimId={selectedClaim} navigate={navigate} />
          ) : (
            <p className="muted">Select a decision or hypothesis from the list.</p>
          )}
        </div>
      </div>
    </div>
  )
}

function ValidationBanner() {
  const { state, reload } = useApiState(() => fetchValidation(), [])
  return (
    <div className="panel validate-banner">
      <ApiBoundary state={state} loadingText="Checking brain/ validation..." reload={reload}>
        {(v) => {
          const errors = v.errors ?? []
          const warnings = v.warnings ?? []
          return (
            <>
              <div className={`validate-status ${v.clean ? 'ok' : 'bad'}`}>
                {v.clean ? 'brain/ validates clean' : `${errors.length} error(s), ${warnings.length} warning(s)`}
              </div>
              {errors.length > 0 && (
                <ul className="finding-list">
                  {errors.map((f, i) => (
                    <FindingItem key={i} f={f} />
                  ))}
                </ul>
              )}
              {warnings.length > 0 && (
                <ul className="finding-list">
                  {warnings.map((f, i) => (
                    <FindingItem key={i} f={f} />
                  ))}
                </ul>
              )}
            </>
          )
        }}
      </ApiBoundary>
    </div>
  )
}

function FindingItem({ f }: { f: FindingOut }) {
  return (
    <li>
      <Chip className={`chip-${f.severity}`}>{f.severity}</Chip>{' '}
      {f.path}
      {f.line ? `:${f.line}` : ''} — {f.message} <CodeTag>{f.code}</CodeTag>
    </li>
  )
}

function ClaimList({ selectedClaim, navigate }: { selectedClaim: string; navigate: Navigate }) {
  const { state, reload } = useApiState(() => fetchClaims(), [])
  return (
    <ApiBoundary state={state} loadingText="Loading claims..." reload={reload}>
      {(claims) => {
        if (!claims.length) {
          return (
            <EmptyPanel
              text="No claims ingested yet — the brain/ tree hasn't been indexed."
              action={
                <a href="#view=demo" className="btn btn-small">
                  Run “Rebuild the brain index” in Demo →
                </a>
              }
            />
          )
        }
        return (
          <table className="data-table">
            <tbody>
              <tr>
                <th>kind</th>
                <th>title</th>
                <th>status</th>
                <th>date</th>
                <th>effect</th>
              </tr>
              {claims.map((c) => (
                <tr key={c.id} className={String(c.id) === selectedClaim ? 'active-row' : ''}>
                  <td>
                    {claimKindLabel(c.kind)} <CodeTag>{c.kind}</CodeTag>
                  </td>
                  <td className="cell-wrap">
                    <a href={`#view=brain&claim=${c.id}`} onClick={(e) => (e.preventDefault(), navigate({ view: 'brain', claim: c.id }))}>
                      {c.title || c.slug}
                    </a>
                  </td>
                  <td>
                    <Chip className={`chip-status chip-${c.status}`}>{c.status}</Chip>
                  </td>
                  <td>{c.date || '-'}</td>
                  <td>{c.has_effect ? 'yes' : '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      }}
    </ApiBoundary>
  )
}

function ClaimDetail({ claimId, navigate }: { claimId: string; navigate: Navigate }) {
  const { state, reload } = useApiState(() => fetchClaimDetail(claimId), [claimId])
  return (
    <ApiBoundary state={state} loadingText={`Loading claim #${claimId}...`} reload={reload}>
      {(c) => (
        <>
          <h3>{c.title || c.slug}</h3>
          <div>
            <Chip className={`chip-status chip-${c.status}`}>{c.status}</Chip> {claimKindLabel(c.kind)}{' '}
            <CodeTag>{c.kind}</CodeTag> slug={c.slug} date={c.date || '-'}
          </div>
          <div className="muted">source file: {c.path || '-'}</div>
          <div>
            <strong>reversal condition: </strong>
            {c.reversal_condition || <span className="muted">not recorded</span>}
          </div>

          {c.effect && (
            <div>
              <strong>quantified effect: </strong>
              {categoryName(c.effect.category_code)} <CodeTag>{c.effect.category_code}</CodeTag> · {c.effect.year} ={' '}
              {fmtNum(c.effect.value)} {c.effect.unit}
            </div>
          )}

          <h4>Evidence</h4>
          <ul className="evidence-list">
            {(c.evidence || []).map((e, i) => {
              const t = tagTerm(e.tag_raw)
              return (
                <li key={i}>
                  <Chip className="chip-tag">{t.label}</Chip>
                  <CodeTag>{e.tag_raw}</CodeTag>
                  {e.resolved === false && <Chip className="chip-warning">unresolved</Chip>}
                  <span className="muted"> {e.section}: </span>
                  {e.text}
                </li>
              )
            })}
            {!(c.evidence || []).length && <li className="muted">no evidence rows</li>}
          </ul>

          {c.links && c.links.length > 0 && (
            <>
              <h4>Links</h4>
              <ul>
                {c.links.map((l, i) => (
                  <li key={i}>
                    {l.relation} -&gt; {l.other_slug || '?'}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h4>Impact — which figures this decision moved</h4>
          <ClaimImpact claimId={claimId} navigate={navigate} />
        </>
      )}
    </ApiBoundary>
  )
}

function ClaimImpact({ claimId, navigate }: { claimId: string; navigate: Navigate }) {
  const { state, reload } = useApiState(() => fetchClaimImpact(claimId), [claimId])
  return (
    <ApiBoundary state={state} loadingText="Loading impact..." reload={reload}>
      {(rows) => {
        if (!rows.length) {
          return (
            <EmptyPanel
              text="This decision has not moved any plan figure yet."
              action={
                <a href="#view=demo" className="btn btn-small">
                  Apply decided effects in Demo →
                </a>
              }
            />
          )
        }
        return (
          <table className="data-table">
            <tbody>
              <tr>
                <th>scenario</th>
                <th>category</th>
                <th>year</th>
                <th>value</th>
                <th>how it got there</th>
                <th>displaced default</th>
                <th></th>
              </tr>
              {rows.map((r, i) => {
                const t = pathTerm(r.path)
                const marked = MARKED_PATHS.has(r.path)
                return (
                  <tr key={i}>
                    <td>{r.scenario_kind}</td>
                    <td>
                      {categoryName(r.category_code)} <CodeTag>{r.category_code}</CodeTag>
                    </td>
                    <td>{String(r.year)}</td>
                    <td>{fmtNum(r.value)}</td>
                    <td>
                      <Chip className={`chip-path${marked ? ' chip-path-marked chip-path-' + r.path : ''}`}>
                        {t.label}
                      </Chip>
                      <CodeTag>{r.path}</CodeTag>
                    </td>
                    <td>{r.displaced_default != null ? fmtNum(r.displaced_default) : '-'}</td>
                    <td>
                      <button
                        className="btn btn-small"
                        onClick={() => navigate({ view: 'trace', kind: 'plan-value', id: r.plan_value_id })}
                      >
                        Trace
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )
      }}
    </ApiBoundary>
  )
}

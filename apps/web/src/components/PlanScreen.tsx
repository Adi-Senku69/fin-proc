/**
 * Plan screen — ported from nvplan/api/static/js/views/plan.js. Categories down, years across,
 * a tab per scenario kind. Every cell is exactly the value/path/plan_value_id the API attached
 * to it; clicking a cell opens its trace in the panel beside the grid.
 */
import { fetchPlanGrid, SCENARIO_KINDS, type ScenarioKind } from '../api/adapters/plan'
import { fetchTrace } from '../api/adapters/trace'
import type { PlanGridOut } from '../api/types'
import { useApiState } from '../api/useApiState'
import { fmtDate, fmtNum } from '../domain/format'
import { categoryName, MARKED_PATHS, pathTerm } from '../domain/terms'
import type { Route } from '../routing/route'
import { ApiBoundary, Chip, CodeTag, EmptyPanel } from './shared'
import { TraceNodeView } from './TraceTree'

type Navigate = (p: Record<string, string | number | null | undefined>) => void

export function PlanScreen({ route, navigate }: { route: Route; navigate: Navigate }) {
  const kind = (route.params.get('scenario') as ScenarioKind) || 'base'
  const traceId = route.params.get('trace') || ''

  const { state, reload } = useApiState(() => fetchPlanGrid(kind), [kind])

  return (
    <div>
      <h2>Plan</h2>
      <div className="tabs">
        {SCENARIO_KINDS.map((k) => (
          <button
            key={k}
            className={`tab-btn ${k === kind ? 'active' : ''}`}
            onClick={() => navigate({ view: 'plan', scenario: k, trace: traceId })}
          >
            {k}
          </button>
        ))}
      </div>

      <div className={`split-layout${traceId ? ' has-selection' : ''}`}>
        <div className="pane grid-pane">
          <ApiBoundary state={state} loadingText="Loading plan grid..." reload={reload}>
            {(grid) => <PlanGrid grid={grid} kind={kind} navigate={navigate} />}
          </ApiBoundary>
        </div>
        <div className="pane trace-pane">
          {traceId ? (
            <TracePane id={traceId} navigate={navigate} />
          ) : (
            <p className="muted">Click a cell to trace it here.</p>
          )}
        </div>
      </div>
    </div>
  )
}

function PlanGrid({ grid, kind, navigate }: { grid: PlanGridOut; kind: ScenarioKind; navigate: Navigate }) {
  if (!grid.years.length) {
    return (
      <EmptyPanel
        text="No plan values for this scenario yet."
        action={
          <a href="#view=demo" className="btn btn-small">
            Run the Demo loop →
          </a>
        }
      />
    )
  }

  return (
    <>
      <div className="grid-meta">
        <span>
          {grid.scenario_label} · created by {grid.created_by} · {fmtDate(grid.created_at)} · unit {grid.unit}
        </span>
        {grid.illustrative && <Chip className="chip-illustrative">ILLUSTRATIVE</Chip>}
      </div>

      <table className="data-table plan-table">
        <tbody>
          <tr>
            <th>category</th>
            {grid.years.map((y) => (
              <th key={y}>{y}</th>
            ))}
          </tr>
          {grid.rows.map((row) => (
            <tr key={row.category_code}>
              <th className="row-label">
                {row.name} <CodeTag>{row.category_code}</CodeTag>
              </th>
              {grid.years.map((y) => {
                const cell = row.cells[String(y)]
                if (!cell) return <td key={y}>-</td>
                const marked = MARKED_PATHS.has(cell.path)
                const pt = pathTerm(cell.path)
                return (
                  <td key={y} className={`plan-cell${marked ? ' cell-marked cell-' + cell.path : ''}`}>
                    <button
                      className="cell-btn"
                      title={`${pt.hint}  ·  plan_value_id=${cell.plan_value_id}`}
                      onClick={() => navigate({ view: 'plan', scenario: kind, trace: cell.plan_value_id })}
                    >
                      {fmtNum(cell.value)}
                    </button>
                    {marked && <div className="path-tag">{pt.label}</div>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Parameters</h3>
      <p className="muted">
        The fitted regression behind each cost category&apos;s default path: a fixed component (alpha) plus a
        variable rate per unit of revenue (beta), how well that fit matches history (R²), and how fast the fixed
        component and the default path each grow on their own (v, g).
      </p>
      <table className="data-table param-table">
        <tbody>
          <tr>
            <th>category</th>
            <th>
              <span className="term-head-primary">fixed component</span>
              <span className="term-head-code">alpha</span>
            </th>
            <th>
              <span className="term-head-primary">variable rate / revenue</span>
              <span className="term-head-code">beta</span>
            </th>
            <th>
              <span className="term-head-primary">fit quality</span>
              <span className="term-head-code">R²</span>
            </th>
            <th>
              <span className="term-head-primary">growth of fixed component</span>
              <span className="term-head-code">v</span>
            </th>
            <th>
              <span className="term-head-primary">growth of default path</span>
              <span className="term-head-code">g</span>
            </th>
            <th>fit window</th>
            <th>calc version</th>
          </tr>
          {grid.parameters.map((p, i) => (
            <tr key={p.parameter_id ?? i} data-parameter-id={p.parameter_id ?? ''}>
              <td>
                {categoryName(p.category_code)} <CodeTag>{p.category_code}</CodeTag>
              </td>
              <td>{fmtNum(p.alpha)}</td>
              <td>{fmtNum(p.beta)}</td>
              <td>{fmtNum(p.r_squared)}</td>
              <td>{fmtNum(p.valorization_rate)}</td>
              <td>{fmtNum(p.growth_rate)}</td>
              <td>{p.window_from != null ? `${p.window_from}-${p.window_to}` : '-'}</td>
              <td>{p.calc_version || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function TracePane({ id, navigate }: { id: string; navigate: Navigate }) {
  const { state, reload } = useApiState(() => fetchTrace('plan-value', id), [id])
  return (
    <>
      <button className="btn btn-small" onClick={() => navigate({ view: 'trace', kind: 'plan-value', id })}>
        Open in full Trace view
      </button>
      <ApiBoundary state={state} loadingText={`Loading trace for plan value #${id}...`} reload={reload}>
        {(node) => <TraceNodeView node={node} onOpenBrain={(claimId) => navigate({ view: 'brain', claim: claimId })} />}
      </ApiBoundary>
    </>
  )
}

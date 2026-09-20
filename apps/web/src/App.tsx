/**
 * Application shell — ported from nvplan/api/static/js/main.js. Left rail + header health
 * badge, no framework router: `../routing/useRoute.ts` reads the URL hash exactly the way
 * router.js does for the vanilla UI. Additive to that UI (UI.md's "React/Vite is the production
 * upgrade path"), not a replacement yet — `nvplan/api/static/` keeps serving unmodified.
 *
 * Eight views total per UI.md; four are built here (Plan, Trace, Brain, Ask — prioritised over
 * Statements/Backtest/AI records/Demo per this build's brief). The other four render an honest
 * "not built in this app yet" notice rather than a half-working screen, with a link to the
 * working vanilla view.
 */
import { useEffect, useState } from 'react'

import { api } from './api/client'
import type { HealthOut } from './api/types'
import { AskScreen } from './components/AskScreen'
import { BrainScreen } from './components/BrainScreen'
import { PlanScreen } from './components/PlanScreen'
import { TraceScreen } from './components/TraceScreen'
import { IMPLEMENTED_VIEWS, VIEWS, type ViewName } from './routing/route'
import { useRoute } from './routing/useRoute'

const RAIL_LABELS: Record<ViewName, string> = {
  plan: 'Plan',
  ask: 'Ask',
  trace: 'Trace',
  brain: 'Brain',
  statements: 'Statements',
  backtest: 'Backtest',
  ai: 'AI records',
  demo: 'Demo',
}

function HealthBadge() {
  const [state, setState] = useState<{ ok: boolean; text: string } | null>(null)

  async function refresh() {
    setState(null)
    const res = await api.get<HealthOut>('/health')
    if (!res.ok) {
      setState({ ok: false, text: `health: HTTP ${res.status} ${res.message}` })
      return
    }
    const h = res.data
    const bits = [`db ${h.db_url}`, `${h.n_actuals} actuals`, `${h.n_scenarios} scenarios`]
    if (h.illustrative) bits.push('ILLUSTRATIVE DATA')
    setState({ ok: true, text: bits.join('  ·  ') })
  }

  useEffect(() => {
    void refresh()
  }, [])

  return (
    <button
      className={`health-badge${state && !state.ok ? ' health-bad' : ''}`}
      title={state ? `${state.text}  ·  click to refresh` : undefined}
      onClick={() => void refresh()}
    >
      {state ? state.text : 'checking...'}
    </button>
  )
}

function NotBuiltYet({ view }: { view: ViewName }) {
  return (
    <div>
      <h2>{RAIL_LABELS[view]}</h2>
      <div className="panel not-available">
        <div className="error-title">Not built in the React app yet</div>
        <div className="error-message">
          This build prioritised Plan, Trace, Brain and Ask (see UI.md). The vanilla UI&apos;s{' '}
          {RAIL_LABELS[view]} view is still fully working — open{' '}
          <a href={`http://127.0.0.1:8000/static/index.html#view=${view}`} target="_blank" rel="noreferrer">
            http://127.0.0.1:8000/static/index.html#view={view}
          </a>{' '}
          there (the backend&apos;s own static origin; adjust the port if `nvplan-serve` is running
          on a different one).
        </div>
      </div>
    </div>
  )
}

function App() {
  const [route, navigate] = useRoute()

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-title">NewVision planning PoC — React build</div>
        <HealthBadge />
      </header>
      <nav className="app-rail">
        {VIEWS.map((v) => (
          <a
            key={v}
            className={`rail-link${route.view === v ? ' active' : ''}`}
            href={`#view=${v}`}
            onClick={(e) => {
              e.preventDefault()
              navigate({ view: v })
            }}
          >
            {RAIL_LABELS[v]}
          </a>
        ))}
      </nav>
      <main className="app-main">
        {!IMPLEMENTED_VIEWS.has(route.view) ? (
          <NotBuiltYet view={route.view} />
        ) : route.view === 'plan' ? (
          <PlanScreen route={route} navigate={navigate} />
        ) : route.view === 'trace' ? (
          <TraceScreen route={route} navigate={navigate} />
        ) : route.view === 'brain' ? (
          <BrainScreen route={route} navigate={navigate} />
        ) : (
          <AskScreen route={route} navigate={navigate} />
        )}
      </main>
    </div>
  )
}

export default App

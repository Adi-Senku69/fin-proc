/**
 * The URL hash is a query string (`#view=plan&scenario=base&trace=123`), exactly the scheme
 * nvplan/api/static/js/router.js uses for the vanilla UI — reload the page, or paste the URL,
 * and you land back on the same screen with the same parameters. No routing library; parsing is
 * native `URLSearchParams`.
 */

export type ViewName = 'plan' | 'trace' | 'brain' | 'ask' | 'statements' | 'backtest' | 'ai' | 'demo'

export const VIEWS: readonly ViewName[] = ['plan', 'ask', 'trace', 'brain', 'statements', 'backtest', 'ai', 'demo']
const VIEW_SET = new Set<string>(VIEWS)

/** The four views this build finished; the rest render a plain "not built here yet" notice
 * pointing at the vanilla UI instead of a half-working screen (see App.tsx). */
export const IMPLEMENTED_VIEWS: ReadonlySet<ViewName> = new Set(['plan', 'trace', 'brain', 'ask'])

export interface Route {
  view: ViewName
  params: URLSearchParams
}

const DEFAULT_VIEW: ViewName = 'plan'

export function parseHash(hash: string): Route {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const params = new URLSearchParams(raw)
  const viewParam = params.get('view')
  const view = viewParam && VIEW_SET.has(viewParam) ? (viewParam as ViewName) : DEFAULT_VIEW
  return { view, params }
}

/** Builds a hash from a plain object; assigning `location.hash` fires `hashchange`. */
export function buildHash(paramsObj: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(paramsObj)) {
    if (v !== undefined && v !== null && String(v) !== '') params.set(k, String(v))
  }
  return params.toString()
}

export function navigate(paramsObj: Record<string, string | number | null | undefined>): void {
  window.location.hash = buildHash(paramsObj)
}

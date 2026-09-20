/**
 * The only place this app talks to the network.
 *
 * `request<T>` returns a plain discriminated result, never throws — the same contract
 * nvplan/api/static/js/api.js uses for the vanilla UI, so a screen's loading/error/ready states
 * are exhaustive and a failed fetch can never render as a blank panel:
 *
 *   { ok: true,  status, data: T }
 *   { ok: false, status, message }
 *
 * `status` is 0 for a network failure (fetch itself rejected — server down, offline, CORS).
 *
 * `T` for every call below comes from `components['schemas'][...]` in `./schema.ts`, generated
 * by `npm run gen:api` from `apps/web/openapi.json` (itself generated from the live FastAPI app
 * by `scripts/export_openapi.py`) — never hand-written. Two routes, `/trace/*` and
 * `/assistant/ask`, are declared `response_model=None` on the server (see nvplan/api/app.py) and
 * so carry no schema; their wire shape is instead captured by hand in `../domain/trace.ts` and
 * `../domain/ask.ts`, each documented at the point it is asserted.
 */

export interface ApiOk<T> {
  ok: true
  status: number
  data: T
}

export interface ApiErr {
  ok: false
  status: number
  message: string
}

export type ApiResult<T> = ApiOk<T> | ApiErr

async function request<T>(method: string, path: string, body?: unknown): Promise<ApiResult<T>> {
  let res: Response
  try {
    const init: RequestInit = { method }
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = JSON.stringify(body)
    }
    res = await fetch(path, init)
  } catch (err) {
    return { ok: false, status: 0, message: `network error: ${err instanceof Error ? err.message : String(err)}` }
  }

  let raw = ''
  try {
    raw = await res.text()
  } catch (err) {
    return { ok: false, status: res.status, message: `could not read response body: ${String(err)}` }
  }

  let data: unknown = null
  if (raw) {
    try {
      data = JSON.parse(raw)
    } catch {
      data = raw
    }
  }

  if (!res.ok) {
    let message = res.statusText || `HTTP ${res.status}`
    if (data && typeof data === 'object' && 'detail' in data) {
      const detail = (data as { detail: unknown }).detail
      message = typeof detail === 'string' ? detail : JSON.stringify(detail)
    } else if (typeof data === 'string' && data) {
      message = data
    }
    return { ok: false, status: res.status, message }
  }

  return { ok: true, status: res.status, data: data as T }
}

export async function getText(path: string): Promise<ApiResult<string>> {
  let res: Response
  try {
    res = await fetch(path)
  } catch (err) {
    return { ok: false, status: 0, message: `network error: ${err instanceof Error ? err.message : String(err)}` }
  }
  const text = await res.text()
  if (!res.ok) return { ok: false, status: res.status, message: text || res.statusText }
  return { ok: true, status: res.status, data: text }
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  getText,
}

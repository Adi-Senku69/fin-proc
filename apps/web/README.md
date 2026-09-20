# NewVision planning PoC — React frontend

React 18 + Vite + TypeScript. Additive to `nvplan/api/static/` (the hand-written vanilla JS UI),
which keeps working unmodified — this app is served separately during development and is not yet
the default.

## Run it

```bash
# backend, from the repo root
uv run nvplan-serve --port 8000

# frontend, from apps/web
npm install
npm run dev
```

Vite proxies every backend route prefix (`/health`, `/plan`, `/trace`, `/brain`, `/assistant`, ...)
to `http://127.0.0.1:8000` in dev (`vite.config.ts`), so the browser never makes a cross-origin
request and `nvplan/api/app.py` needs no CORS middleware. Point at a backend on another port with
`NVPLAN_BACKEND_PORT=<port> npm run dev`.

## The contract gate

`apps/web/openapi.json` is generated from the live FastAPI app by
`uv run python scripts/export_openapi.py` (repo root) — never hand-written. `npm run gen:api`
turns it into `src/api/schema.d.ts` via `openapi-typescript`. Every adapter under `src/api/`
imports its types from there, never from a hand-written interface, so a change to
`nvplan/api/schemas.py` either updates both generated files or breaks CI
(`.github/workflows/ci.yml`'s `contract-gate` and `web` jobs).

Two routes — `GET /trace/*` and `POST /assistant/ask` — are `response_model=None` on the server
and so have no schema to generate from; their wire shapes are hand-typed in `src/domain/trace.ts`
and `src/domain/ask.ts` instead, each documented at the point it is asserted. Giving those routes
real Pydantic response models (outside this app's ownership) would let the gate cover them too.

## Layout

- `src/api/client.ts` — the only place this app calls `fetch`; every result is
  `{ ok, status, data|message }`, never a throw.
- `src/api/types.ts` — named aliases onto the generated schema.
- `src/api/adapters/*` — one adapter per screen's data needs; the seam between the wire shape and
  what a screen renders.
- `src/domain/*` — formatting (`format.ts`), the plain-language layer (`terms.ts`, ported from
  `nvplan/api/static/js/terms.js`), and the two hand-typed shapes above.
- `src/components/*` — Plan, Trace, Brain, Ask, plus shared loading/error/empty panels
  (`shared.tsx`) ported from `nvplan/api/static/js/dom.js`.
- `src/routing/*` — a hash-as-query-string router (`#view=plan&scenario=base&trace=123`), no
  routing library, ported from `nvplan/api/static/js/router.js`.
- `src/index.css` — carried over verbatim from `nvplan/api/static/app.css` so both UIs share one
  design language (slate/indigo/status-hue tokens, panel/chip/tab shapes).

## Status

Plan, Trace, Brain and Ask are built and have been exercised against a running backend with real
data. Statements, Backtest, AI records and Demo are not built here yet — the left rail links out
to the working vanilla-UI view for each instead of a half-finished screen.

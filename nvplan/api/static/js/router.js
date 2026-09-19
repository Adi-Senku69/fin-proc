/* router.js — no routing library. The URL hash is a query string
 * (`#view=plan&scenario=base&trace=123`) so any state is a link: reload the page, or paste
 * the URL, and you land back on the same view with the same parameters. Parsing is native
 * URLSearchParams; nothing here is a framework.
 */

import { el, clear, errorPanel } from "./dom.js";

const views = new Map();
let mainEl = null;
let railEl = null;

export function registerView(name, mod) {
  views.set(name, mod);
}

export function parseHash() {
  const raw = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const params = new URLSearchParams(raw);
  const view = params.get("view") || "demo";
  return { view, params };
}

/** Build a hash from a plain object and navigate to it (drives a hashchange -> re-render). */
export function navigate(paramsObj) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(paramsObj)) {
    if (v !== undefined && v !== null && String(v) !== "") params.set(k, String(v));
  }
  location.hash = params.toString();
}

function highlightRail(view) {
  if (!railEl) return;
  for (const a of railEl.querySelectorAll(".rail-link")) {
    a.classList.toggle("active", a.dataset.view === view);
  }
}

async function renderCurrent() {
  const { view, params } = parseHash();
  highlightRail(view);
  clear(mainEl);
  const mod = views.get(view);
  if (!mod) {
    mainEl.append(
      el("div", { class: "panel error-panel" }, [
        el("div", { class: "error-title" }, [`Unknown view: ${view}`]),
        el("div", { class: "error-message" }, ["Use the left rail to pick a view."]),
      ])
    );
    return;
  }
  try {
    await mod.render(mainEl, params, { navigate });
  } catch (err) {
    clear(mainEl);
    mainEl.append(errorPanel(0, (err && err.stack) || (err && err.message) || String(err)));
  }
}

export function initRouter(main, rail) {
  mainEl = main;
  railEl = rail;
  window.addEventListener("hashchange", renderCurrent);
  renderCurrent();
}

/* dom.js — small DOM builder + the shared "something went wrong" / "not built yet" panels
 * every view uses so a failed fetch never renders as a blank panel or a silent console error.
 */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "checked" || k === "disabled" || k === "open" || k === "selected") {
      if (v) node.setAttribute(k, "");
    } else node.setAttribute(k, v);
  }
  const list = Array.isArray(children) ? children : [children];
  for (const c of list) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function chip(text, extraClass = "") {
  return el("span", { class: `chip ${extraClass}`.trim() }, [String(text)]);
}

/** A visible, specific failure: status code + the server's own message. Never a blank panel. */
export function errorPanel(status, message, opts = {}) {
  const wrap = el("div", { class: "panel error-panel" }, [
    el("div", { class: "error-title" }, [`Request failed — HTTP ${status || "0 (network)"}`]),
    el("div", { class: "error-message" }, [String(message || "unknown error")]),
  ]);
  if (opts.retry) wrap.append(el("button", { class: "btn btn-small", onClick: opts.retry }, ["Retry"]));
  return wrap;
}

/** A route that is documented (UI.md Part 1) but 404s because the other half of this build
 * has not merged it yet. Distinct wording from errorPanel so it reads as "not wired up",
 * not "broken". Never fake the data this route would have returned. */
export function notAvailable(routeLabel, opts = {}) {
  const wrap = el("div", { class: "panel not-available" }, [
    el("div", { class: "error-title" }, [`${routeLabel} — endpoint not available yet`]),
    el("div", { class: "error-message" }, [
      "This route returned 404. Per UI.md this can mean the brain/bridge routes are not merged yet. Retry once they are.",
    ]),
  ]);
  if (opts.retry) wrap.append(el("button", { class: "btn btn-small", onClick: opts.retry }, ["Retry"]));
  return wrap;
}

/** Render the right one of the two above based on the api.js result's status. */
export function failurePanel(routeLabel, res, retry) {
  if (res.status === 404) return notAvailable(routeLabel, { retry });
  return errorPanel(res.status, res.message, { retry });
}

/** A considered loading state: a small spinner plus what is being fetched, so a view never
 * sits on a bare "Loading..." paragraph indistinguishable from a stuck page. */
export function loadingPanel(text = "Loading...") {
  return el("div", { class: "state-panel loading-panel" }, [
    el("span", { class: "spinner", "aria-hidden": "true" }),
    el("span", {}, [text]),
  ]);
}

/** A considered empty state: distinct from an error (nothing went wrong; there is simply
 * nothing here yet), and distinct from a loading state. Never a blank panel (UI.md: "A blank
 * panel is a bug"). ``opts.action`` renders a button (e.g. a link to the Demo view). */
export function emptyPanel(text, opts = {}) {
  const wrap = el("div", { class: "state-panel empty-panel" }, [el("div", { class: "empty-text" }, [text])]);
  if (opts.action) wrap.append(opts.action);
  return wrap;
}

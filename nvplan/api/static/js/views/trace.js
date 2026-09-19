/* views/trace.js — the lineage tree (PLATFORM.md §7.1, nvplan/services/trace.py
 * TraceNode.to_dict). Renders the exact shape the API returns; nothing here computes a
 * number. `renderTree` and `fetchTrace` are exported so the Plan view's side panel and the
 * Brain view's impact links can reuse the same renderer instead of duplicating it.
 */

import { el, clear, chip, failurePanel, loadingPanel } from "../dom.js";
import { fmtNum, fmtDate, fmtParam } from "../format.js";
import { api } from "../api.js";
import { pathTerm, MARKED_PATHS, paramTerm, tagTerm, touchpointLabel, codeTag, evidenceSectionLabel } from "../terms.js";

export async function fetchTrace(kind, id) {
  const path = kind === "statement-line" ? `/trace/statement-line/${id}` : `/trace/plan-value/${id}`;
  return api.get(path);
}

function pathBadge(path) {
  if (!path) return null;
  const t = pathTerm(path);
  const marked = MARKED_PATHS.has(path);
  const badge = el("span", { class: `path-badge path-${path}${marked ? " path-marked" : ""}` }, [t.label]);
  badge.title = t.hint;
  badge.append(codeTag(path));
  return badge;
}

/** A parameter's kv rows get the plain label as the row header and the raw key/symbol as a
 * secondary, muted tag (UI.md: "the identifier is never the only thing shown"). Any other
 * derivation's inputs/parameters (not a Parameter row) keep the raw key, since most of those
 * (a category code, a formula input) already read fine on their own. */
function kvTable(entries, { termed = false } = {}) {
  if (!entries.length) return null;
  const table = el("table", { class: "kv-table" });
  for (const [k, v] of entries) {
    const term = termed ? paramTerm(k) : null;
    const head = term
      ? el("th", {}, [term.label, " ", codeTag(term.symbol || k)])
      : el("th", {}, [k]);
    // A parameter/input value is a figure (alpha, beta, a year, a count) far more often
    // than not, and a figure must never break mid-token (app.css: .kv-table td). Only the
    // genuinely non-numeric case - a string, a compound value JSON has to stringify - opts
    // back into wrapping via .kv-value-prose.
    const isFigure = typeof v === "number" && Number.isFinite(v);
    const text = typeof v === "object" && v !== null ? JSON.stringify(v) : fmtParam(k, v);
    table.append(el("tr", {}, [head, el("td", { class: isFigure ? "" : "kv-value-prose" }, [text])]));
  }
  return table;
}

function pointsTable(points) {
  if (!Array.isArray(points) || !points.length) return null;
  const cols = Object.keys(points[0]);
  const table = el("table", { class: "data-table" });
  table.append(el("tr", {}, cols.map((c) => el("th", {}, [c]))));
  for (const p of points) table.append(el("tr", {}, cols.map((c) => el("td", {}, [fmtParam(c, p[c])]))));
  return el("details", { class: "points-details" }, [el("summary", {}, [`regression points (${points.length})`]), table]);
}

function aiBlock(ai) {
  const tpChip = chip(touchpointLabel(ai.touchpoint), "chip-touchpoint");
  tpChip.append(codeTag(ai.touchpoint));
  return el("div", { class: "ai-block" }, [
    el("div", { class: "block-title" }, ["AI proposal"]),
    el("div", {}, [
      tpChip,
      " ",
      chip(ai.status, `chip-status chip-${ai.status}`),
      el("span", { class: "muted" }, [` model ${ai.model_version}`]),
    ]),
    el("div", {}, [el("strong", {}, ["rationale: "]), ai.rationale || "-"]),
    el("div", {}, [
      el("strong", {}, ["confirmer: "]),
      ai.confirmed_by ? `${ai.confirmed_by} on ${fmtDate(ai.confirmed_at)}` : "not yet confirmed",
    ]),
    el("div", { class: "prompt-label" }, ["prompt (verbatim):"]),
    el("pre", { class: "prompt-box" }, [ai.prompt_text || ""]),
  ]);
}

function claimBlock(claim, opts) {
  const wrap = el("div", { class: "claim-block" }, [
    el("div", { class: "block-title" }, ["Decision"]),
    el("div", {}, [el("strong", {}, [claim.title || claim.slug]), " ", chip(claim.status, `chip-status chip-${claim.status}`)]),
    el("div", { class: "muted" }, [`slug: ${claim.slug}  ·  decided: ${claim.decided_on || "-"}`]),
  ]);
  if (claim.reversal_condition) {
    wrap.append(el("div", {}, [el("strong", {}, ["reversal condition: "]), claim.reversal_condition]));
  } else {
    wrap.append(el("div", { class: "muted" }, ["reversal condition: not recorded"]));
  }
  wrap.append(el("div", { class: "block-title" }, ["Evidence"]));
  const evList = el("ul", { class: "evidence-list" });
  for (const e of claim.evidence || []) {
    const t = tagTerm(e.tag_raw);
    const tagChip = chip(t.label, "chip-tag");
    tagChip.title = e.tag_raw;
    evList.append(el("li", {}, [tagChip, codeTag(e.tag_raw), " ", e.text]));
  }
  if (!(claim.evidence || []).length) evList.append(el("li", { class: "muted" }, ["(no evidence rows in the trace)"]));
  wrap.append(evList);
  if (opts.navigate) {
    wrap.append(
      el("button", { class: "btn btn-small", onClick: () => opts.navigate({ view: "brain", claim: claim.claim_id }) }, [
        "Open this decision in Brain",
      ])
    );
  }
  return wrap;
}

export function renderTree(container, node, opts = {}) {
  const depth = opts.depth || 0;
  const details = el("details", { class: `trace-node depth-${Math.min(depth, 6)}` });
  if (depth < 2 && !node.ref) details.open = true;

  const summary = el("summary", { class: "trace-summary" });
  summary.append(el("span", { class: "trace-disclosure", "aria-hidden": "true" }, ["▸"]));
  summary.append(el("span", { class: "trace-label" }, [node.label || node.kind]));
  if (node.value !== null && node.value !== undefined) {
    summary.append(el("span", { class: "trace-value" }, [fmtNum(node.value) + " k EUR"]));
  }
  const badge = pathBadge(node.path);
  if (badge) summary.append(badge);
  if (node.source_label && String(node.source_label).includes("ILLUSTRATIVE")) {
    summary.append(chip("ILLUSTRATIVE", "chip-illustrative"));
  }
  if (node.ai) summary.append(chip("ai", "chip-touchpoint"));
  if (node.claim) summary.append(chip("decision", "chip-status chip-decided"));
  if (node.ref) summary.append(el("span", { class: "muted" }, [" (see above)"]));
  if (node.truncated) summary.append(el("span", { class: "muted" }, [" (truncated)"]));
  details.append(summary);

  if (node.ref) {
    container.append(details);
    return;
  }

  const body = el("div", { class: "trace-body" });
  if (node.formula_text) body.append(el("div", { class: "formula" }, [node.formula_text]));

  const paramEntries = Object.entries(node.parameters || {}).filter(([k]) => !k.startsWith("_"));
  const pt = kvTable(paramEntries, { termed: true });
  if (pt) body.append(el("div", {}, [el("div", { class: "block-title" }, ["parameters"]), pt]));

  const pts = pointsTable(node.inputs && node.inputs.points);
  if (pts) body.append(pts);

  const inputEntries = Object.entries(node.inputs || {}).filter(([k]) => !k.startsWith("_") && k !== "points");
  const it = kvTable(inputEntries);
  if (it) body.append(el("div", {}, [el("div", { class: "block-title" }, ["inputs"]), it]));

  if (node.source_label) body.append(el("div", { class: "muted" }, [`source: ${node.source_label}`]));

  if (node.ai) body.append(aiBlock(node.ai));
  if (node.claim) body.append(claimBlock(node.claim, opts));

  details.append(body);

  const kids = node.children || [];
  if (kids.length) {
    const childrenWrap = el("div", { class: `trace-children depth-guide-${depth % 6}` });
    for (const child of kids) renderTree(childrenWrap, child, { ...opts, depth: depth + 1 });
    details.append(childrenWrap);
  }

  container.append(details);
}

/** The collapse/expand affordance UI.md asks for, at tree scope rather than per node: every
 * <details> under ``root`` opens or closes together. */
function setAllOpen(root, open) {
  for (const d of root.querySelectorAll("details.trace-node")) d.open = open;
}

async function loadInto(body, kind, id, ctx) {
  clear(body);
  body.append(loadingPanel("Loading trace..."));
  const res = await fetchTrace(kind, id);
  clear(body);
  if (!res.ok) {
    body.append(failurePanel(`/trace/${kind}/${id}`, res, () => loadInto(body, kind, id, ctx)));
    return;
  }

  const toolbar = el("div", { class: "trace-toolbar" });
  const expandBtn = el("button", { class: "btn btn-small" }, ["Expand all"]);
  const collapseBtn = el("button", { class: "btn btn-small" }, ["Collapse all"]);
  expandBtn.addEventListener("click", () => setAllOpen(body, true));
  collapseBtn.addEventListener("click", () => setAllOpen(body, false));
  toolbar.append(expandBtn, collapseBtn);
  const textBtn = el("button", { class: "btn btn-small" }, ["View as plain text"]);
  let textBox = null;
  textBtn.addEventListener("click", async () => {
    if (textBox) {
      textBox.remove();
      textBox = null;
      textBtn.textContent = "View as plain text";
      return;
    }
    textBtn.textContent = "Loading...";
    const t = await api.getText(
      kind === "statement-line" ? `/trace/statement-line/${id}?format=text` : `/trace/plan-value/${id}?format=text`
    );
    textBtn.textContent = t.ok ? "Hide plain text" : "View as plain text";
    textBox = el("pre", { class: "prompt-box" }, [t.ok ? t.data : `HTTP ${t.status}: ${t.message}`]);
    toolbar.after(textBox);
  });
  toolbar.append(textBtn);
  body.append(toolbar);

  renderTree(body, res.data, { navigate: ctx.navigate, depth: 0 });
}

export async function render(container, params, ctx) {
  const kind = params.get("kind") === "statement-line" ? "statement-line" : "plan-value";
  const id = params.get("id") || "";

  container.append(el("h2", {}, ["Trace"]));
  container.append(
    el("p", { class: "muted" }, [
      "The lineage tree for one figure. Every node here is exactly what the API returned — nothing is computed in this page.",
    ])
  );

  const form = el("form", { class: "trace-form" });
  const kindSel = el(
    "select",
    { name: "kind" },
    [
      el("option", { value: "plan-value" }, ["plan value"]),
      el("option", { value: "statement-line" }, ["statement line"]),
    ]
  );
  kindSel.value = kind;
  const idInput = el("input", { type: "number", name: "id", value: id, placeholder: "id", min: "1" });
  form.append(
    el("label", {}, ["kind ", kindSel]),
    el("label", {}, ["id ", idInput]),
    el("button", { type: "submit", class: "btn" }, ["Open"])
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    ctx.navigate({ view: "trace", kind: kindSel.value, id: idInput.value });
  });
  container.append(form);

  if (!id) {
    container.append(
      el("p", { class: "muted" }, ["Enter a plan-value or statement-line id above, or click a cell in Plan / Statements."])
    );
    return;
  }

  const body = el("div", { class: "trace-container" });
  container.append(body);
  await loadInto(body, kind, id, ctx);
}

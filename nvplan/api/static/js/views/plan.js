/* views/plan.js — categories down, years across, a tab per scenario kind (UI.md Part 2 §1).
 * Every cell is exactly the value/path/plan_value_id the API attached to it; clicking a cell
 * opens its trace in the panel beside the grid (and offers to open the full Trace view).
 */

import { el, clear, chip, failurePanel, loadingPanel, emptyPanel } from "../dom.js";
import { fmtNum, fmtDate } from "../format.js";
import { api } from "../api.js";
import { renderTree, fetchTrace } from "./trace.js";
import { pathTerm, MARKED_PATHS, termHeader, codeTag, categoryName } from "../terms.js";

const KINDS = ["base", "best", "worst"];

export async function render(container, params, ctx) {
  const kind = params.get("scenario") || "base";
  const traceId = params.get("trace") || "";
  const highlightParam = params.get("highlight_param") || "";

  container.append(el("h2", {}, ["Plan"]));

  const tabs = el("div", { class: "tabs" });
  for (const k of KINDS) {
    const btn = el("button", { class: `tab-btn ${k === kind ? "active" : ""}` }, [k]);
    btn.addEventListener("click", () => ctx.navigate({ view: "plan", scenario: k, trace: traceId }));
    tabs.append(btn);
  }
  container.append(tabs);

  const layout = el("div", { class: "split-layout" });
  const gridPane = el("div", { class: "pane grid-pane" });
  const tracePane = el("div", { class: "pane trace-pane" });
  layout.append(gridPane, tracePane);
  container.append(layout);

  await loadGrid(gridPane, tracePane, kind, traceId, highlightParam, ctx);
}

async function loadGrid(gridPane, tracePane, kind, traceId, highlightParam, ctx) {
  clear(gridPane);
  gridPane.append(loadingPanel("Loading plan grid..."));
  const res = await api.get(`/plan/grid?scenario_kind=${encodeURIComponent(kind)}`);
  clear(gridPane);
  if (!res.ok) {
    gridPane.append(failurePanel("/plan/grid", res, () => loadGrid(gridPane, tracePane, kind, traceId, highlightParam, ctx)));
    return;
  }
  renderGrid(gridPane, tracePane, res.data, kind, ctx);

  if (highlightParam) {
    const row = gridPane.querySelector(`tr[data-parameter-id="${CSS.escape(String(highlightParam))}"]`);
    if (row) {
      row.classList.add("active-row");
      row.scrollIntoView({ block: "center" });
    }
  }

  if (traceId) {
    await loadTracePane(tracePane, traceId, ctx);
  } else {
    clear(tracePane);
    tracePane.append(el("p", { class: "muted" }, ["Click a cell to trace it here."]));
  }
}

function renderGrid(pane, tracePane, grid, kind, ctx) {
  const meta = el("div", { class: "grid-meta" }, [
    el("span", {}, [`${grid.scenario_label} · created by ${grid.created_by} · ${fmtDate(grid.created_at)} · unit ${grid.unit}`]),
  ]);
  if (grid.illustrative) meta.append(chip("ILLUSTRATIVE", "chip-illustrative"));
  pane.append(meta);

  if (!grid.years.length) {
    pane.append(emptyPanel("No plan values for this scenario yet.", {
      action: el("a", { href: "#view=demo", class: "btn btn-small" }, ["Run the Demo loop →"]),
    }));
    return;
  }

  const table = el("table", { class: "data-table plan-table" });
  table.append(el("tr", {}, [el("th", {}, ["category"]), ...grid.years.map((y) => el("th", {}, [String(y)]))]));
  for (const row of grid.rows) {
    const tr = el("tr", {});
    // Plain name first (Category.name, already human-readable), the code secondary and muted
    // (UI.md: "plain language is the primary label; the identifier is never the only thing
    // shown" — category codes specifically stay visible too, since the finance module uses them).
    tr.append(el("th", { class: "row-label" }, [row.name, " ", codeTag(row.category_code)]));
    for (const y of grid.years) {
      const cell = row.cells[y];
      if (!cell) {
        tr.append(el("td", {}, ["-"]));
        continue;
      }
      const marked = MARKED_PATHS.has(cell.path);
      const td = el("td", { class: `plan-cell${marked ? " cell-marked cell-" + cell.path : ""}` });
      const btn = el("button", { class: "cell-btn" }, [fmtNum(cell.value)]);
      const pt = pathTerm(cell.path);
      btn.title = `${pt.hint}  ·  plan_value_id=${cell.plan_value_id}`;
      btn.addEventListener("click", () => ctx.navigate({ view: "plan", scenario: kind, trace: cell.plan_value_id }));
      td.append(btn);
      if (marked) td.append(el("div", { class: "path-tag" }, [pt.label]));
      tr.append(td);
    }
    table.append(tr);
  }
  pane.append(table);

  pane.append(el("h3", {}, ["Parameters"]));
  pane.append(
    el("p", { class: "muted" }, [
      "The fitted regression behind each cost category's default path: a fixed component (alpha) plus a variable " +
        "rate per unit of revenue (beta), how well that fit matches history (R²), and how fast the fixed " +
        "component and the default path each grow on their own (v, g).",
    ])
  );
  const pt = el("table", { class: "data-table param-table" });
  pt.append(
    el(
      "tr",
      {},
      [
        el("th", {}, ["category"]),
        termHeader("th", "fixed component", "alpha"),
        termHeader("th", "variable rate / revenue", "beta"),
        termHeader("th", "fit quality", "R²"),
        termHeader("th", "growth of fixed component", "v"),
        termHeader("th", "growth of default path", "g"),
        el("th", {}, ["fit window"]),
        el("th", {}, ["calc version"]),
      ]
    )
  );
  for (const p of grid.parameters) {
    const tr = el("tr", { "data-parameter-id": p.parameter_id != null ? String(p.parameter_id) : "" }, [
      el("td", {}, [categoryName(p.category_code), " ", codeTag(p.category_code)]),
      el("td", {}, [fmtNum(p.alpha)]),
      el("td", {}, [fmtNum(p.beta)]),
      el("td", {}, [fmtNum(p.r_squared)]),
      el("td", {}, [fmtNum(p.valorization_rate)]),
      el("td", {}, [fmtNum(p.growth_rate)]),
      el("td", {}, [p.window_from != null ? `${p.window_from}-${p.window_to}` : "-"]),
      el("td", {}, [p.calc_version || "-"]),
    ]);
    pt.append(tr);
  }
  pane.append(pt);
}

async function loadTracePane(tracePane, id, ctx) {
  clear(tracePane);
  tracePane.append(el("p", { class: "muted" }, [`Loading trace for plan value #${id}...`]));
  const res = await fetchTrace("plan-value", id);
  clear(tracePane);

  const openFull = el("button", { class: "btn btn-small" }, ["Open in full Trace view"]);
  openFull.addEventListener("click", () => ctx.navigate({ view: "trace", kind: "plan-value", id }));
  tracePane.append(openFull);

  if (!res.ok) {
    tracePane.append(failurePanel(`/trace/plan-value/${id}`, res, () => loadTracePane(tracePane, id, ctx)));
    return;
  }
  renderTree(tracePane, res.data, { navigate: ctx.navigate, depth: 0 });
}

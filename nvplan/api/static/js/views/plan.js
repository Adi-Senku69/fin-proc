/* views/plan.js — categories down, years across, a tab per scenario kind (UI.md Part 2 §1).
 * Every cell is exactly the value/path/plan_value_id the API attached to it; clicking a cell
 * opens its trace in the panel beside the grid (and offers to open the full Trace view).
 */

import { el, clear, chip, failurePanel } from "../dom.js";
import { fmtNum, fmtDate } from "../format.js";
import { api } from "../api.js";
import { renderTree, fetchTrace } from "./trace.js";

const KINDS = ["base", "best", "worst"];
const MARKED_PATHS = new Set(["decided", "ai_proposed"]);

export async function render(container, params, ctx) {
  const kind = params.get("scenario") || "base";
  const traceId = params.get("trace") || "";

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

  await loadGrid(gridPane, tracePane, kind, traceId, ctx);
}

async function loadGrid(gridPane, tracePane, kind, traceId, ctx) {
  clear(gridPane);
  gridPane.append(el("p", { class: "muted" }, ["Loading plan grid..."]));
  const res = await api.get(`/plan/grid?scenario_kind=${encodeURIComponent(kind)}`);
  clear(gridPane);
  if (!res.ok) {
    gridPane.append(failurePanel("/plan/grid", res, () => loadGrid(gridPane, tracePane, kind, traceId, ctx)));
    return;
  }
  renderGrid(gridPane, tracePane, res.data, kind, ctx);

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
    pane.append(el("p", { class: "muted" }, ["No plan values for this scenario yet."]));
    return;
  }

  const table = el("table", { class: "data-table plan-table" });
  table.append(el("tr", {}, [el("th", {}, ["category"]), ...grid.years.map((y) => el("th", {}, [String(y)]))]));
  for (const row of grid.rows) {
    const tr = el("tr", {});
    tr.append(el("th", { class: "row-label" }, [`${row.category_code} · ${row.name}`]));
    for (const y of grid.years) {
      const cell = row.cells[y];
      if (!cell) {
        tr.append(el("td", {}, ["-"]));
        continue;
      }
      const marked = MARKED_PATHS.has(cell.path);
      const td = el("td", { class: `plan-cell${marked ? " cell-marked cell-" + cell.path : ""}` });
      const btn = el("button", { class: "cell-btn" }, [fmtNum(cell.value)]);
      btn.title = `path=${cell.path}  plan_value_id=${cell.plan_value_id}`;
      btn.addEventListener("click", () => ctx.navigate({ view: "plan", scenario: kind, trace: cell.plan_value_id }));
      td.append(btn);
      if (marked) td.append(el("div", { class: "path-tag" }, [cell.path]));
      tr.append(td);
    }
    table.append(tr);
  }
  pane.append(table);

  pane.append(el("h3", {}, ["Parameters"]));
  const pt = el("table", { class: "data-table param-table" });
  pt.append(el("tr", {}, ["category", "alpha", "beta", "R²", "valorization v", "growth g", "window", "calc"].map((h) => el("th", {}, [h]))));
  for (const p of grid.parameters) {
    pt.append(
      el("tr", {}, [
        el("td", {}, [p.category_code]),
        el("td", {}, [fmtNum(p.alpha)]),
        el("td", {}, [fmtNum(p.beta)]),
        el("td", {}, [fmtNum(p.r_squared)]),
        el("td", {}, [fmtNum(p.valorization_rate)]),
        el("td", {}, [fmtNum(p.growth_rate)]),
        el("td", {}, [p.window_from != null ? `${p.window_from}-${p.window_to}` : "-"]),
        el("td", {}, [p.calc_version || "-"]),
      ])
    );
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

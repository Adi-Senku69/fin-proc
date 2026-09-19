/* views/statements.js — P&L, balance sheet, cash flow (UI.md Part 2 §4). The consistency
 * check is rendered exactly as the API reports it (a list of problem strings; empty = clean)
 * rather than asserted in prose. Kept simple per the stated priority order.
 */

import { el, clear, chip, failurePanel, loadingPanel, emptyPanel } from "../dom.js";
import { fmtNum } from "../format.js";
import { api } from "../api.js";

const KINDS = ["base", "best", "worst"];
const STATEMENTS = [["pl", "P&L"], ["bs", "Balance sheet"], ["cf", "Cash flow"]];

export async function render(container, params, ctx) {
  const kind = params.get("scenario") || "base";
  const statement = params.get("statement") || "pl";

  container.append(el("h2", {}, ["Statements"]));

  const tabs = el("div", { class: "tabs" });
  for (const k of KINDS) {
    const btn = el("button", { class: `tab-btn ${k === kind ? "active" : ""}` }, [k]);
    btn.addEventListener("click", () => ctx.navigate({ view: "statements", scenario: k, statement }));
    tabs.append(btn);
  }
  container.append(tabs);

  const subtabs = el("div", { class: "tabs subtabs" });
  for (const [code, label] of STATEMENTS) {
    const btn = el("button", { class: `tab-btn ${code === statement ? "active" : ""}` }, [label]);
    btn.addEventListener("click", () => ctx.navigate({ view: "statements", scenario: kind, statement: code }));
    subtabs.append(btn);
  }
  container.append(subtabs);

  const pane = el("div", { class: "pane grid-pane" });
  container.append(pane);
  await load(pane, kind, statement, ctx);
}

async function load(pane, kind, statement, ctx) {
  clear(pane);
  pane.append(loadingPanel("Loading statement..."));
  const res = await api.get(`/statements/${encodeURIComponent(kind)}?statement=${encodeURIComponent(statement)}`);
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel(`/statements/${kind}`, res, () => load(pane, kind, statement, ctx)));
    return;
  }
  const grid = res.data;

  const meta = el("div", { class: "grid-meta" }, [
    el("span", {}, [`${grid.scenario_label} · ${grid.statement.toUpperCase()} · unit ${grid.unit}`]),
  ]);
  if (grid.illustrative) meta.append(chip("ILLUSTRATIVE", "chip-illustrative"));
  pane.append(meta);

  if (grid.statement === "bs") {
    const box = el("div", { class: "consistency-panel" });
    if (!grid.consistency.length) {
      box.append(el("div", { class: "consistency-ok" }, ["Consistency check: PASS — no balance / cash-tie problem reported."]));
    } else {
      box.append(el("div", { class: "consistency-bad" }, ["Consistency check: FAIL"]));
      const list = el("ul", {});
      for (const p of grid.consistency) list.append(el("li", {}, [p]));
      box.append(list);
    }
    pane.append(box);
  }

  if (!grid.years.length) {
    pane.append(
      emptyPanel("No statement lines for this scenario yet.", {
        action: el("a", { href: "#view=demo", class: "btn btn-small" }, ["Run the Demo loop →"]),
      })
    );
    return;
  }

  const table = el("table", { class: "data-table" });
  table.append(el("tr", {}, [el("th", {}, ["line"]), ...grid.years.map((y) => el("th", {}, [String(y)]))]));
  for (const row of grid.rows) {
    const tr = el("tr", {});
    tr.append(el("th", { class: "row-label" }, [row.line_code]));
    for (const y of grid.years) {
      const cell = row.cells[y];
      if (!cell) {
        tr.append(el("td", {}, ["-"]));
        continue;
      }
      const btn = el("button", { class: "cell-btn" }, [fmtNum(cell.value)]);
      btn.title = `mapping_ref=${cell.mapping_ref}  statement_line_id=${cell.statement_line_id}`;
      btn.addEventListener("click", () => ctx.navigate({ view: "trace", kind: "statement-line", id: cell.statement_line_id }));
      tr.append(el("td", {}, [btn]));
    }
    table.append(tr);
  }
  pane.append(table);
}

/* views/backtest.js — error per category and horizon, verdict coloured against the
 * threshold (UI.md Part 2 §5). `verdict` (within / marginal / missed) is already computed
 * server-side (nvplan/core/backtest.py); this view only renders that string as a chip, it
 * never compares numbers itself.
 */

import { el, clear, chip, failurePanel } from "../dom.js";
import { fmtCell } from "../format.js";
import { api } from "../api.js";

export async function render(container, params, ctx) {
  container.append(el("h2", {}, ["Backtest"]));
  const pane = el("div", {});
  container.append(pane);
  await load(pane, ctx);
}

async function load(pane, ctx) {
  clear(pane);
  pane.append(el("p", { class: "muted" }, ["Loading backtest report..."]));
  const res = await api.get("/backtest");
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel("/backtest", res, () => load(pane, ctx)));
    return;
  }
  const data = res.data;

  const meta = el("div", { class: "grid-meta" }, [
    el("span", {}, [
      `${data.n_cases} cases · threshold within<=${fmtCell(data.thresholds.mape_within)} · marginal<=${fmtCell(
        data.thresholds.mape_marginal
      )}`,
    ]),
  ]);
  if (data.illustrative) meta.append(chip("ILLUSTRATIVE", "chip-illustrative"));
  pane.append(meta);

  pane.append(el("h3", {}, ["Error per category and horizon"]));
  pane.append(renderTable(data.summary));
  pane.append(el("h3", {}, ["Error against the valorized default path"]));
  pane.append(renderTable(data.summary_default_path));
  pane.append(el("h3", {}, ["Per-window regression parameters"]));
  pane.append(renderTable(data.fits));

  pane.append(el("h3", {}, ["Report (markdown, verbatim)"]));
  pane.append(el("pre", { class: "prompt-box" }, [data.markdown || ""]));
}

function renderTable(rows) {
  if (!rows || !rows.length) return el("p", { class: "muted" }, ["no rows"]);
  const cols = Object.keys(rows[0]);
  const table = el("table", { class: "data-table" });
  table.append(el("tr", {}, cols.map((c) => el("th", {}, [c]))));
  for (const r of rows) {
    const tr = el("tr", { class: r.verdict ? `verdict-${r.verdict}` : "" });
    for (const c of cols) {
      tr.append(el("td", {}, [c === "verdict" && r[c] ? chip(r[c], `chip-verdict chip-${r[c]}`) : fmtCell(r[c])]));
    }
    table.append(tr);
  }
  return table;
}

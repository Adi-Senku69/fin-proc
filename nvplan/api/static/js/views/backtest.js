/* views/backtest.js — error per category and horizon, verdict coloured against the
 * threshold (UI.md Part 2 §5). `verdict` (within / marginal / missed) is already computed
 * server-side (nvplan/core/backtest.py); this view only renders that string as a chip, it
 * never compares numbers itself.
 */

import { el, clear, chip, failurePanel, loadingPanel, emptyPanel } from "../dom.js";
import { fmtCell, fmtParam } from "../format.js";
import { api } from "../api.js";
import { columnTerm, categoryName } from "../terms.js";

export async function render(container, params, ctx) {
  container.append(el("h2", {}, ["Backtest"]));
  container.append(
    el("p", { class: "muted" }, [
      "How well each category's fitted model would have predicted years it never saw, refit on a rolling window. " +
        "Average percentage error (MAPE) drives the verdict; typical error size (RMSE) is in the same unit as the figure.",
    ])
  );
  const pane = el("div", { class: "pane" });
  container.append(pane);
  await load(pane, ctx);
}

async function load(pane, ctx) {
  clear(pane);
  pane.append(loadingPanel("Loading backtest report..."));
  const res = await api.get("/backtest");
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel("/backtest", res, () => load(pane, ctx)));
    return;
  }
  const data = res.data;

  if (!data.n_cases) {
    pane.append(
      emptyPanel("No backtest cases yet — run the plan with enough historical actuals first.", {
        action: el("a", { href: "#view=demo", class: "btn btn-small" }, ["Open Demo →"]),
      })
    );
    return;
  }

  const meta = el("div", { class: "grid-meta" }, [
    el("span", {}, [
      `${data.n_cases} cases · ‘within’ threshold: average error ≤ ${fmtCell(
        data.thresholds.mape_within
      )} · ‘marginal’: ≤ ${fmtCell(data.thresholds.mape_marginal)}`,
    ]),
  ]);
  if (data.illustrative) meta.append(chip("ILLUSTRATIVE", "chip-illustrative"));
  pane.append(meta);

  pane.append(el("h3", {}, ["Error per category and horizon"]));
  pane.append(renderTable(data.summary));
  pane.append(el("h3", {}, ["Error against the valorized default path"]));
  pane.append(el("p", { class: "muted" }, ["The same comparison, but against the path that grows on its own trend rather than following an actual."]));
  pane.append(renderTable(data.summary_default_path));
  pane.append(el("h3", {}, ["Per-window fitted parameters"]));
  pane.append(renderTable(data.fits));

  pane.append(el("h3", {}, ["Report (markdown, verbatim)"]));
  pane.append(el("pre", { class: "prompt-box" }, [data.markdown || ""]));
}

function renderTable(rows) {
  if (!rows || !rows.length) return emptyPanel("no rows");
  const cols = Object.keys(rows[0]);
  const table = el("table", { class: "data-table" });
  table.append(
    el(
      "tr",
      {},
      cols.map((c) => {
        const t = columnTerm(c);
        return el("th", {}, [el("span", { class: "term-head-primary" }, [t.label]), el("span", { class: "term-head-code" }, [t.symbol || c])]);
      })
    )
  );
  for (const r of rows) {
    const tr = el("tr", { class: r.verdict ? `verdict-${r.verdict}` : "" });
    for (const c of cols) {
      if (c === "verdict" && r[c]) {
        tr.append(el("td", {}, [chip(r[c], `chip-verdict chip-${r[c]}`)]));
      } else if (c === "category_code" && r[c]) {
        tr.append(el("td", {}, [categoryName(r[c]), " ", el("span", { class: "term-code" }, [r[c]])]));
      } else {
        tr.append(el("td", {}, [fmtParam(c, r[c])]));
      }
    }
    table.append(tr);
  }
  return table;
}

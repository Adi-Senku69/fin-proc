/* views/airecords.js — each record's touchpoint, status, model, the literal prompt, and the
 * per-call audit log with real token usage (UI.md Part 2 §6). Kept as simpler tables per
 * the stated priority order.
 */

import { el, clear, chip, failurePanel, loadingPanel, emptyPanel } from "../dom.js";
import { fmtDate, fmtNum } from "../format.js";
import { api } from "../api.js";
import { touchpointLabel, categoryName, codeTag } from "../terms.js";

export async function render(container, params, ctx) {
  const selected = params.get("record") || "";
  container.append(el("h2", {}, ["AI records"]));

  const layout = el("div", { class: "split-layout" });
  const listPane = el("div", { class: "pane list-pane" });
  const detailPane = el("div", { class: "pane detail-pane" });
  layout.append(listPane, detailPane);
  container.append(layout);

  await loadList(listPane, selected);
  if (selected) {
    await loadDetail(detailPane, selected);
  } else {
    detailPane.append(el("p", { class: "muted" }, ["Select a record."]));
  }
}

async function loadList(pane, selected) {
  clear(pane);
  pane.append(loadingPanel("Loading records..."));
  const res = await api.get("/ai/records");
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel("/ai/records", res, () => loadList(pane, selected)));
    return;
  }
  const rows = res.data;
  if (!rows.length) {
    pane.append(
      emptyPanel("No AI records yet — no touchpoint has run.", {
        action: el("a", { href: "#view=ask", class: "btn btn-small" }, ["Ask a question →"]),
      })
    );
    return;
  }
  const table = el("table", { class: "data-table" });
  table.append(el("tr", {}, ["#", "touchpoint", "status", "model", "category", "year"].map((h) => el("th", {}, [h]))));
  for (const r of rows) {
    const tr = el("tr", { class: String(r.id) === String(selected) ? "active-row" : "" });
    tr.append(
      el("td", {}, [el("a", { href: `#view=ai&record=${r.id}` }, [String(r.id)])]),
      el("td", {}, [touchpointLabel(r.touchpoint), " ", codeTag(r.touchpoint)]),
      el("td", {}, [chip(r.status, `chip-status chip-${r.status}`)]),
      el("td", {}, [r.model_version]),
      el("td", {}, r.category_code ? [categoryName(r.category_code), " ", codeTag(r.category_code)] : ["-"]),
      el("td", {}, [r.year != null ? String(r.year) : "-"])
    );
    table.append(tr);
  }
  pane.append(table);
}

async function loadDetail(pane, id) {
  clear(pane);
  pane.append(loadingPanel(`Loading record #${id}...`));
  const res = await api.get(`/ai/records/${id}`);
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel(`/ai/records/${id}`, res, () => loadDetail(pane, id)));
    return;
  }
  const r = res.data;

  pane.append(el("h3", {}, [`Record #${r.id} — ${touchpointLabel(r.touchpoint)}`]));
  pane.append(
    el("div", {}, [
      chip(r.status, `chip-status chip-${r.status}`),
      codeTag(r.touchpoint),
      ` model=${r.model_version}  confirmed_by=${r.confirmed_by || "-"}  confirmed_at=${fmtDate(r.confirmed_at)}`,
    ])
  );
  if (r.proposed_value != null) {
    pane.append(el("div", {}, [el("strong", {}, ["proposed value: "]), `${fmtNum(r.proposed_value)} k EUR`]));
  }
  pane.append(el("div", {}, [el("strong", {}, ["rationale: "]), r.rationale || "-"]));
  pane.append(el("div", { class: "prompt-label" }, ["prompt (verbatim):"]));
  pane.append(el("pre", { class: "prompt-box" }, [r.prompt_text || ""]));
  pane.append(el("div", { class: "prompt-label" }, ["response (verbatim):"]));
  pane.append(el("pre", { class: "prompt-box" }, [r.response_text || ""]));

  pane.append(el("h4", {}, ["Per-call audit log"]));
  if (r.total_usage && Object.keys(r.total_usage).length) {
    pane.append(
      el("div", { class: "muted" }, [
        "total usage: " + Object.entries(r.total_usage).map(([k, v]) => `${k}=${v}`).join("  "),
      ])
    );
  } else if (r.total_usage) {
    pane.append(el("div", { class: "muted" }, ["total usage: {} (fake model — no provider token counts)"]));
  } else {
    pane.append(el("div", { class: "muted" }, ["total usage: not available for this record"]));
  }
  const log = r.call_log || [];
  if (!log.length) {
    pane.append(el("p", { class: "muted" }, ["no call log recorded"]));
    return;
  }
  let i = 0;
  for (const call of log) {
    i += 1;
    const usage = call.usage ? Object.entries(call.usage).map(([k, v]) => `${k}=${v}`).join(" ") : "";
    const det = el("details", { class: "call-log-entry" });
    det.append(el("summary", {}, [`call ${i}${usage ? "  " + usage : ""}`]));
    det.append(el("pre", { class: "prompt-box" }, [JSON.stringify(call, null, 2)]));
    pane.append(det);
  }
}

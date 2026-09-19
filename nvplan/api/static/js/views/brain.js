/* views/brain.js — decisions and hypotheses (UI.md Part 2 §3). A validation banner up top
 * (errors distinguished from warnings), a claim list, and a detail pane with evidence tag
 * chips, the reversal condition, the quantified effect, and the impact (which figures moved,
 * each linking into Trace).
 */

import { el, clear, chip, failurePanel } from "../dom.js";
import { fmtNum } from "../format.js";
import { api } from "../api.js";

export async function render(container, params, ctx) {
  const selectedClaim = params.get("claim") || "";

  container.append(el("h2", {}, ["Brain"]));

  const banner = el("div", { class: "panel validate-banner" });
  container.append(banner);
  await loadValidation(banner);

  const layout = el("div", { class: "split-layout" });
  const listPane = el("div", { class: "pane list-pane" });
  const detailPane = el("div", { class: "pane detail-pane" });
  layout.append(listPane, detailPane);
  container.append(layout);

  await loadList(listPane, ctx, selectedClaim);

  if (selectedClaim) {
    await loadDetail(detailPane, selectedClaim, ctx);
  } else {
    detailPane.append(el("p", { class: "muted" }, ["Select a decision or hypothesis from the list."]));
  }
}

async function loadValidation(banner) {
  clear(banner);
  banner.append(el("p", { class: "muted" }, ["Checking brain/ validation..."]));
  const res = await api.get("/brain/validate");
  clear(banner);
  if (!res.ok) {
    banner.append(failurePanel("/brain/validate", res, () => loadValidation(banner)));
    return;
  }
  const { errors = [], warnings = [], clean } = res.data;
  banner.append(
    el("div", { class: `validate-status ${clean ? "ok" : "bad"}` }, [
      clean ? "brain/ validates clean" : `${errors.length} error(s), ${warnings.length} warning(s)`,
    ])
  );
  if (errors.length) {
    const list = el("ul", { class: "finding-list" });
    for (const f of errors) list.append(findingItem(f));
    banner.append(list);
  }
  if (warnings.length) {
    const list = el("ul", { class: "finding-list" });
    for (const f of warnings) list.append(findingItem(f));
    banner.append(list);
  }
}

function findingItem(f) {
  return el("li", {}, [
    chip(f.severity, `chip-${f.severity}`),
    ` ${f.path}${f.line ? ":" + f.line : ""} [${f.code}] ${f.message}`,
  ]);
}

async function loadList(pane, ctx, selectedClaim) {
  clear(pane);
  pane.append(el("p", { class: "muted" }, ["Loading claims..."]));
  const res = await api.get("/brain/claims");
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel("/brain/claims", res, () => loadList(pane, ctx, selectedClaim)));
    return;
  }
  const claims = res.data;
  if (!claims.length) {
    pane.append(el("p", { class: "muted" }, ["No claims ingested yet. Use Demo -> \"Ingest the brain\"."]));
    return;
  }
  const table = el("table", { class: "data-table" });
  table.append(el("tr", {}, ["kind", "title", "status", "date", "effect"].map((h) => el("th", {}, [h]))));
  for (const c of claims) {
    const tr = el("tr", { class: String(c.id) === String(selectedClaim) ? "active-row" : "" });
    const link = el("a", { href: `#view=brain&claim=${c.id}` }, [c.title || c.slug]);
    tr.append(
      el("td", {}, [c.kind]),
      el("td", {}, [link]),
      el("td", {}, [chip(c.status, `chip-status chip-${c.status}`)]),
      el("td", {}, [c.date || "-"]),
      el("td", {}, [c.has_effect ? "yes" : "-"])
    );
    table.append(tr);
  }
  pane.append(table);
}

async function loadDetail(pane, claimId, ctx) {
  clear(pane);
  pane.append(el("p", { class: "muted" }, [`Loading claim #${claimId}...`]));
  const res = await api.get(`/brain/claims/${claimId}`);
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel(`/brain/claims/${claimId}`, res, () => loadDetail(pane, claimId, ctx)));
    return;
  }
  const c = res.data;

  pane.append(el("h3", {}, [c.title || c.slug]));
  pane.append(
    el("div", {}, [chip(c.status, `chip-status chip-${c.status}`), ` kind=${c.kind}  slug=${c.slug}  date=${c.date || "-"}`])
  );
  pane.append(el("div", { class: "muted" }, [`path: ${c.path || "-"}`]));

  pane.append(
    el("div", {}, [
      el("strong", {}, ["reversal condition: "]),
      c.reversal_condition || el("span", { class: "muted" }, ["not recorded"]),
    ])
  );

  if (c.effect) {
    pane.append(
      el("div", {}, [
        el("strong", {}, ["quantified effect: "]),
        `${c.effect.category_code} ${c.effect.year} = ${fmtNum(c.effect.value)} ${c.effect.unit}`,
      ])
    );
  }

  pane.append(el("h4", {}, ["Evidence"]));
  const evList = el("ul", { class: "evidence-list" });
  for (const e of c.evidence || []) {
    evList.append(
      el("li", {}, [
        chip(e.tag_raw, "chip-tag"),
        e.resolved === false ? chip("unresolved", "chip-warning") : null,
        el("span", { class: "muted" }, [` [${e.section}] `]),
        e.text,
      ])
    );
  }
  if (!(c.evidence || []).length) evList.append(el("li", { class: "muted" }, ["no evidence rows"]));
  pane.append(evList);

  if (c.links && c.links.length) {
    pane.append(el("h4", {}, ["Links"]));
    const ll = el("ul", {});
    for (const l of c.links) ll.append(el("li", {}, [`${l.relation} -> ${l.other_slug || "?"}`]));
    pane.append(ll);
  }

  pane.append(el("h4", {}, ["Impact — which figures this decision moved"]));
  const impactWrap = el("div", {});
  pane.append(impactWrap);
  await loadImpact(impactWrap, claimId, ctx);
}

async function loadImpact(wrap, claimId, ctx) {
  clear(wrap);
  wrap.append(el("p", { class: "muted" }, ["Loading impact..."]));
  const res = await api.get(`/brain/claims/${claimId}/impact`);
  clear(wrap);
  if (!res.ok) {
    wrap.append(failurePanel(`/brain/claims/${claimId}/impact`, res, () => loadImpact(wrap, claimId, ctx)));
    return;
  }
  const rows = res.data;
  if (!rows.length) {
    wrap.append(el("p", { class: "muted" }, ["This decision has not moved any plan figure yet. Apply it from the Demo view."]));
    return;
  }
  const table = el("table", { class: "data-table" });
  table.append(
    el("tr", {}, ["scenario", "category", "year", "value", "path", "displaced default", ""].map((h) => el("th", {}, [h])))
  );
  for (const r of rows) {
    const btn = el("button", { class: "btn btn-small" }, ["Trace"]);
    btn.addEventListener("click", () => ctx.navigate({ view: "trace", kind: "plan-value", id: r.plan_value_id }));
    table.append(
      el("tr", {}, [
        el("td", {}, [r.scenario_kind]),
        el("td", {}, [r.category_code]),
        el("td", {}, [String(r.year)]),
        el("td", {}, [fmtNum(r.value)]),
        el("td", {}, [chip(r.path, `chip-status chip-${r.path}`)]),
        el("td", {}, [r.displaced_default != null ? fmtNum(r.displaced_default) : "-"]),
        el("td", {}, [btn]),
      ])
    );
  }
  wrap.append(table);
}

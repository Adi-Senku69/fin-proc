/* views/brain.js — decisions and hypotheses (UI.md Part 2 §3). A validation banner up top
 * (errors distinguished from warnings), a claim list, and a detail pane with evidence tag
 * chips, the reversal condition, the quantified effect, and the impact (which figures moved,
 * each linking into Trace).
 */

import { el, clear, chip, failurePanel, loadingPanel, emptyPanel } from "../dom.js";
import { fmtNum } from "../format.js";
import { api } from "../api.js";
import { claimKindLabel, tagTerm, evidenceSectionLabel, pathTerm, MARKED_PATHS, codeTag, categoryName } from "../terms.js";

export async function render(container, params, ctx) {
  const selectedClaim = params.get("claim") || "";

  container.append(el("h2", {}, ["Brain"]));

  const banner = el("div", { class: "panel validate-banner" });
  container.append(banner);
  await loadValidation(banner);

  // Same rule as Plan's trace pane (app.css): the detail pane only earns its half of the
  // grid once a claim is actually selected.
  const layout = el("div", { class: `split-layout${selectedClaim ? " has-selection" : ""}` });
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
  banner.append(loadingPanel("Checking brain/ validation..."));
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
  // The message (from brainkit.validate) is already plain English; it is the primary text.
  // The code is a secondary, muted identifier — never the only thing shown, never the lede.
  return el("li", {}, [
    chip(f.severity, `chip-${f.severity}`),
    " ",
    `${f.path}${f.line ? ":" + f.line : ""} — ${f.message} `,
    codeTag(f.code),
  ]);
}

async function loadList(pane, ctx, selectedClaim) {
  clear(pane);
  pane.append(loadingPanel("Loading claims..."));
  const res = await api.get("/brain/claims");
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel("/brain/claims", res, () => loadList(pane, ctx, selectedClaim)));
    return;
  }
  const claims = res.data;
  if (!claims.length) {
    pane.append(
      emptyPanel("No claims ingested yet — the brain/ tree hasn't been indexed.", {
        action: el("a", { href: "#view=demo", class: "btn btn-small" }, ["Run “Rebuild the brain index” in Demo →"]),
      })
    );
    return;
  }
  const table = el("table", { class: "data-table" });
  table.append(el("tr", {}, ["kind", "title", "status", "date", "effect"].map((h) => el("th", {}, [h]))));
  for (const c of claims) {
    const tr = el("tr", { class: String(c.id) === String(selectedClaim) ? "active-row" : "" });
    const link = el("a", { href: `#view=brain&claim=${c.id}` }, [c.title || c.slug]);
    tr.append(
      el("td", {}, [claimKindLabel(c.kind), " ", codeTag(c.kind)]),
      // the decision/hypothesis title is prose, not a figure - it may be an arbitrary-
      // length sentence, so this cell (unlike the rest of the row) opts back into wrapping.
      el("td", { class: "cell-wrap" }, [link]),
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
  pane.append(loadingPanel(`Loading claim #${claimId}...`));
  const res = await api.get(`/brain/claims/${claimId}`);
  clear(pane);
  if (!res.ok) {
    pane.append(failurePanel(`/brain/claims/${claimId}`, res, () => loadDetail(pane, claimId, ctx)));
    return;
  }
  const c = res.data;

  pane.append(el("h3", {}, [c.title || c.slug]));
  pane.append(
    el("div", {}, [
      chip(c.status, `chip-status chip-${c.status}`),
      ` ${claimKindLabel(c.kind)} `,
      codeTag(c.kind),
      `  slug=${c.slug}  date=${c.date || "-"}`,
    ])
  );
  pane.append(el("div", { class: "muted" }, [`source file: ${c.path || "-"}`]));

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
        categoryName(c.effect.category_code),
        " ",
        codeTag(c.effect.category_code),
        ` · ${c.effect.year} = ${fmtNum(c.effect.value)} ${c.effect.unit}`,
      ])
    );
  }

  pane.append(el("h4", {}, ["Evidence"]));
  const evList = el("ul", { class: "evidence-list" });
  for (const e of c.evidence || []) {
    const t = tagTerm(e.tag_raw);
    const tagChip = chip(t.label, "chip-tag");
    tagChip.title = e.tag_raw;
    evList.append(
      el("li", {}, [
        tagChip,
        codeTag(e.tag_raw),
        e.resolved === false ? chip("unresolved", "chip-warning") : null,
        el("span", { class: "muted" }, [` ${evidenceSectionLabel(e.section)}: `]),
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
  wrap.append(loadingPanel("Loading impact..."));
  const res = await api.get(`/brain/claims/${claimId}/impact`);
  clear(wrap);
  if (!res.ok) {
    wrap.append(failurePanel(`/brain/claims/${claimId}/impact`, res, () => loadImpact(wrap, claimId, ctx)));
    return;
  }
  const rows = res.data;
  if (!rows.length) {
    wrap.append(
      emptyPanel("This decision has not moved any plan figure yet.", {
        action: el("a", { href: "#view=demo", class: "btn btn-small" }, ["Apply decided effects in Demo →"]),
      })
    );
    return;
  }
  const table = el("table", { class: "data-table" });
  table.append(
    el("tr", {}, ["scenario", "category", "year", "value", "how it got there", "displaced default", ""].map((h) => el("th", {}, [h])))
  );
  for (const r of rows) {
    const btn = el("button", { class: "btn btn-small" }, ["Trace"]);
    btn.addEventListener("click", () => ctx.navigate({ view: "trace", kind: "plan-value", id: r.plan_value_id }));
    const t = pathTerm(r.path);
    const marked = MARKED_PATHS.has(r.path);
    const pchip = chip(t.label, `chip-path${marked ? " chip-path-marked chip-path-" + r.path : ""}`);
    pchip.title = t.hint;
    pchip.append(codeTag(r.path));
    table.append(
      el("tr", {}, [
        el("td", {}, [r.scenario_kind]),
        el("td", {}, [categoryName(r.category_code), " ", codeTag(r.category_code)]),
        el("td", {}, [String(r.year)]),
        el("td", {}, [fmtNum(r.value)]),
        el("td", {}, [pchip]),
        el("td", {}, [r.displaced_default != null ? fmtNum(r.displaced_default) : "-"]),
        el("td", {}, [btn]),
      ])
    );
  }
  wrap.append(table);
}

/* views/demo.js — the loop as ordered buttons (UI.md Part 2 §7): ingest actuals, run the
 * plan, ingest the brain, apply decided effects, then a link straight to the figure the
 * decision moved. This is the script someone follows in a room, so each step reports exactly
 * what its endpoint returned — nothing here is computed or guessed ahead of the response.
 */

import { el, clear, chip, failurePanel } from "../dom.js";
import { fmtNum } from "../format.js";
import { api } from "../api.js";
import { categoryName, pathTerm, codeTag } from "../terms.js";

export async function render(container, params, ctx) {
  container.append(el("h2", {}, ["Demo"]));
  container.append(
    el("p", { class: "muted" }, [
      "Run each step in order. Every line below is the endpoint's own response — this page adds no arithmetic of its own.",
    ])
  );

  const steps = el("div", { class: "demo-steps" });
  container.append(steps);

  addStep(steps, {
    title: "1. Ingest actuals",
    endpoint: "POST /ingest",
    action: () => api.post("/ingest?with_notes=true"),
    render: (out, data) => {
      out.append(
        kv("rows", data.rows),
        kv("source_label", data.source_label),
        kv("source", data.source),
        kv("illustrative", String(data.illustrative)),
        kv("notes_seeded", data.notes_seeded)
      );
    },
  });

  addStep(steps, {
    title: "2. Run the plan",
    endpoint: "POST /plan/run",
    action: () => api.post("/plan/run", { created_by: "demo-ui", label_suffix: " (demo)" }),
    render: (out, data) => {
      out.append(
        kv("scenario_ids", JSON.stringify(data.scenario_ids)),
        kv("n_plan_values", data.n_plan_values),
        kv("n_statement_lines", data.n_statement_lines),
        kv("n_derivations", data.n_derivations),
        kv("label", data.label)
      );
      out.append(el("a", { href: "#view=plan&scenario=base" }, ["Open the Plan grid ->"]));
    },
  });

  addStep(steps, {
    title: "3. Rebuild the brain index",
    endpoint: "POST /brain/reindex",
    action: () => api.post("/brain/reindex"),
    render: (out, data) => {
      out.append(
        kv("files_seen", data.files_seen),
        kv("indexed", data.indexed),
        kv("skipped_unchanged", data.skipped_unchanged),
        kv("rejected", (data.rejected || []).length)
      );
      if ((data.rejected || []).length) {
        const list = el("ul", {});
        for (const p of data.rejected) list.append(el("li", {}, [p]));
        out.append(el("div", { class: "muted" }, ["rejected files:"]), list);
      }
      if ((data.findings || []).length) {
        const list = el("ul", { class: "finding-list" });
        for (const f of data.findings) {
          list.append(el("li", {}, [chip(f.severity, `chip-${f.severity}`), ` ${f.path} — ${f.message} `, codeTag(f.code)]));
        }
        out.append(el("div", { class: "muted" }, ["findings:"]), list);
      }
      out.append(el("a", { href: "#view=brain" }, ["Open Brain ->"]));
    },
  });

  addStep(steps, {
    title: "4. Apply decided effects",
    endpoint: "POST /bridge/apply",
    action: () => api.post("/bridge/apply", {}),
    render: (out, data) => {
      out.append(kv("applied (years)", JSON.stringify(data.applied)), kv("shadowed (claim ids)", JSON.stringify(data.shadowed)));
      if (data.message) out.append(el("div", { class: "muted" }, [data.message]));
      if (data.plan_run) {
        out.append(kv("plan_run.label", data.plan_run.label), kv("plan_run.n_plan_values", data.plan_run.n_plan_values));
        out.append(el("a", { href: "#view=plan&scenario=base" }, ["Open the Plan grid ->"]));
      }
    },
  });

  addStep(steps, {
    title: "5. Show what the decision moved",
    endpoint: "GET /brain/claims?status=decided  ->  GET /brain/claims/{id}/impact",
    action: async () => {
      const claims = await api.get("/brain/claims?status=decided");
      if (!claims.ok) return claims;
      const withEffect = (claims.data || []).find((c) => c.has_effect);
      if (!withEffect) {
        return { ok: false, status: 200, data: null, message: "no decided claim with a quantified effect found — run step 3 first" };
      }
      const impact = await api.get(`/brain/claims/${withEffect.id}/impact`);
      if (!impact.ok) return impact;
      return { ok: true, status: 200, message: "", data: { claim: withEffect, impact: impact.data } };
    },
    render: (out, data) => {
      out.append(
        el("div", {}, [el("strong", {}, ["decision: "]), data.claim.title || data.claim.slug, " ", chip(data.claim.status, "chip-status chip-decided")])
      );
      if (!data.impact.length) {
        out.append(el("p", { class: "muted" }, ["This decision has not moved any figure yet."]));
        return;
      }
      out.append(el("div", { class: "muted" }, ["figures this decision moved:"]));
      for (const row of data.impact) {
        const pt = pathTerm(row.path);
        const link = el("a", { href: `#view=trace&kind=plan-value&id=${row.plan_value_id}` }, [
          `${categoryName(row.category_code)} · ${row.year} · ${row.scenario_kind}  =  ${fmtNum(row.value)} k EUR  ` +
            `(${pt.label})  — open trace`,
        ]);
        out.append(el("div", { class: "impact-link" }, [link]));
      }
    },
  });
}

function kv(k, v) {
  return el("div", { class: "kv-line" }, [el("strong", {}, [`${k}: `]), String(v)]);
}

function addStep(container, { title, endpoint, action, render }) {
  const card = el("div", { class: "demo-step" });
  const head = el("div", { class: "demo-step-head" }, [
    el("div", {}, [el("h3", {}, [title]), el("div", { class: "muted endpoint-label" }, [endpoint])]),
  ]);
  const btn = el("button", { class: "btn" }, ["Run"]);
  head.append(btn);
  const out = el("div", { class: "demo-step-out" });
  card.append(head, out);
  container.append(card);

  const run = async () => {
    btn.disabled = true;
    clear(out);
    out.append(el("p", { class: "muted" }, ["Running..."]));
    let res;
    try {
      res = await action();
    } catch (err) {
      res = { ok: false, status: 0, data: null, message: (err && err.message) || String(err) };
    }
    clear(out);
    btn.disabled = false;
    if (!res.ok) {
      out.append(failurePanel(endpoint, res, run));
      return;
    }
    render(out, res.data);
  };
  btn.addEventListener("click", run);
}

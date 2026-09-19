/* main.js — entry point. Builds the left rail, registers the seven views, starts the
 * router. No framework: this file and router.js are the whole "routing library".
 */

import { initRouter, registerView } from "./router.js";
import { api } from "./api.js";
import { el, clear } from "./dom.js";
import * as demo from "./views/demo.js";
import * as plan from "./views/plan.js";
import * as trace from "./views/trace.js";
import * as brain from "./views/brain.js";
import * as statements from "./views/statements.js";
import * as backtest from "./views/backtest.js";
import * as airecords from "./views/airecords.js";

const RAIL_VIEWS = [
  { key: "demo", label: "Demo" },
  { key: "plan", label: "Plan" },
  { key: "trace", label: "Trace" },
  { key: "brain", label: "Brain" },
  { key: "statements", label: "Statements" },
  { key: "backtest", label: "Backtest" },
  { key: "ai", label: "AI records" },
];

registerView("demo", demo);
registerView("plan", plan);
registerView("trace", trace);
registerView("brain", brain);
registerView("statements", statements);
registerView("backtest", backtest);
registerView("ai", airecords);

function buildRail(railEl) {
  for (const v of RAIL_VIEWS) {
    const a = el("a", { class: "rail-link", href: `#view=${v.key}` }, [v.label]);
    a.dataset.view = v.key;
    railEl.append(a);
  }
}

async function refreshHealth(badge) {
  clear(badge);
  badge.append("checking...");
  const res = await api.get("/health");
  clear(badge);
  if (!res.ok) {
    badge.classList.add("health-bad");
    badge.append(`health: HTTP ${res.status} ${res.message}`);
    return;
  }
  badge.classList.remove("health-bad");
  const h = res.data;
  const bits = [`db ${h.db_url}`, `${h.n_actuals} actuals`, `${h.n_scenarios} scenarios`];
  if (h.illustrative) bits.push("ILLUSTRATIVE DATA");
  badge.append(bits.join("  ·  "));
}

function main() {
  const railEl = document.getElementById("rail");
  const mainEl = document.getElementById("view");
  const healthBadge = document.getElementById("health");

  buildRail(railEl);
  healthBadge.addEventListener("click", () => refreshHealth(healthBadge));
  refreshHealth(healthBadge);

  initRouter(mainEl, railEl);
}

main();

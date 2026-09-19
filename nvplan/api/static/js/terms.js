/* terms.js — the plain-language layer (UI.md: "the interface must not speak in our shorthand").
 *
 * Rule applied everywhere in this UI: plain language is the PRIMARY label; the internal
 * identifier (a code, an enum value, a single-letter symbol) is never removed — it still
 * renders, but secondary: smaller, muted, alongside the plain word rather than in place of it.
 * Engineers still need the identifier; a reader who isn't one needs the sentence.
 *
 * This module holds every such mapping once, so there is one wording for each identifier
 * across every view, and does no arithmetic and fetches nothing — it only renders labels for
 * data the views already have.
 */

import { el } from "./dom.js";

// ---- category codes (finance module) --------------------------------------------------------
// The API's Category.name is already plain ("Personnel costs"); this is a fallback for the few
// places only the bare code is on hand, and matches nvplan.services.trace._PRETTY_CODES so the
// wording is identical to what the trace tree already prints server-side.
export const CATEGORY_NAMES = {
  REV: "Revenue", MAT: "Material costs", EXT: "External services",
  PERS: "Personnel costs", OTH: "Other costs", DEPR: "Depreciation",
};
export function categoryName(code) {
  return CATEGORY_NAMES[code] || code;
}

// ---- plan value / statement path (PlanPath) --------------------------------------------------
export const PATH_TERMS = {
  cascaded: { label: "follows revenue", hint: "cascaded — moves automatically when revenue moves" },
  valorized: { label: "grown on its own trend", hint: "valorized — grown on its own historical trend, independent of revenue" },
  decided: { label: "set by a decision", hint: "decided — a decision in the brain set this figure directly" },
  ai_proposed: {
    label: "proposed by the assistant, confirmed by a person",
    hint: "ai_proposed — an AI proposal a named person confirmed",
  },
};
export function pathTerm(path) {
  return PATH_TERMS[path] || { label: path, hint: path };
}
/** Marked paths (UI.md: "A figure whose path is decided or ai_proposed is visually marked"). */
export const MARKED_PATHS = new Set(["decided", "ai_proposed"]);

// ---- fitted parameters (alpha / beta / R² / valorization / growth) ---------------------------
export const PARAM_TERMS = {
  alpha: { label: "fixed component", symbol: "alpha", hint: "the part of the cost that does not move with revenue" },
  beta: { label: "variable rate per unit of revenue", symbol: "beta", hint: "how much this moves per unit of revenue" },
  r_squared: { label: "fit quality", symbol: "R²", hint: "how well the fitted line matches the historical actuals — 0 to 1, higher is better" },
  valorization_rate: { label: "growth of the fixed component", symbol: "v", hint: "the fixed component's own year-on-year growth rate" },
  v: { label: "growth of the fixed component", symbol: "v", hint: "the fixed component's own year-on-year growth rate" },
  growth_rate: { label: "growth rate of the default path", symbol: "g", hint: "the growth rate used for the valorized default path" },
  g: { label: "growth rate of the default path", symbol: "g", hint: "the growth rate used for the valorized default path" },
  window_from: { label: "fit window start", symbol: "window_from" },
  window_to: { label: "fit window end", symbol: "window_to" },
  calc_version: { label: "calculation version", symbol: "calc_version" },
  spread: { label: "spread", symbol: "spread" },
  t0: { label: "base year", symbol: "t0" },
  n: { label: "sample count", symbol: "n" },
};
export function paramTerm(key) {
  return PARAM_TERMS[key] || null;
}

// ---- backtest metrics --------------------------------------------------------------------------
export const METRIC_TERMS = {
  mape: { label: "average percentage error", symbol: "MAPE" },
  rmse: { label: "typical error size", symbol: "RMSE" },
  mape_within: { label: "‘within’ threshold", symbol: "MAPE ≤" },
  mape_marginal: { label: "‘marginal’ threshold", symbol: "MAPE ≤" },
  threshold_low: { label: "‘within’ threshold", symbol: "threshold_low" },
  threshold_high: { label: "‘marginal’ threshold", symbol: "threshold_high" },
  category_code: { label: "category", symbol: "code" },
  derivation_key: { label: "calculation key", symbol: "derivation_key" },
  basis: { label: "compared against", symbol: "basis" },
  horizon: { label: "years ahead", symbol: "horizon" },
};
export function metricTerm(key) {
  return METRIC_TERMS[key] || PARAM_TERMS[key] || null;
}
/** Best-effort plain label for any generic table column key (backtest tables, etc). */
export function columnTerm(key) {
  return metricTerm(key) || { label: key.replace(/_/g, " "), symbol: key };
}

// ---- provenance tags (PLATFORM.md §4.1) --------------------------------------------------------
export const TAG_TERMS = {
  ingestion: { label: "synthesized record", hint: "ingestion — a record we wrote from raw material" },
  source: { label: "raw source document", hint: "source — an unedited artifact: a transcript or a document" },
  stakeholder_verbal: { label: "said by a stakeholder", hint: "stakeholder-verbal" },
  intuition: { label: "instinct call", hint: "intuition — a judgment call, no external evidence" },
  industry_knowledge: { label: "general industry knowledge", hint: "industry-knowledge — no specific source" },
  chat: { label: "said in chat, no record kept", hint: "chat, no artifact" },
  computed: { label: "calculated by the engine", hint: "computed — resolves to a real calculation in the engine" },
};
/** ``tag_raw`` is the exact written form (e.g. "(stakeholder-verbal, Dana, 2026-01-01)" or a
 * markdown link); the plain label keys off its leading word since that's the only closed-set
 * part (PLATFORM.md §4.1). Falls back to the raw tag itself when it matches nothing known. */
export function tagTerm(tagRaw) {
  if (!tagRaw) return { label: "untagged", hint: tagRaw || "" };
  const s = String(tagRaw).toLowerCase();
  if (s.startsWith("[ingestion") || s.startsWith("ingestion")) return TAG_TERMS.ingestion;
  if (s.startsWith("[source") || s.startsWith("source")) return TAG_TERMS.source;
  if (s.includes("stakeholder-verbal")) return TAG_TERMS.stakeholder_verbal;
  if (s.includes("intuition")) return TAG_TERMS.intuition;
  if (s.includes("industry-knowledge")) return TAG_TERMS.industry_knowledge;
  if (s.includes("chat")) return TAG_TERMS.chat;
  if (s.includes("computed")) return TAG_TERMS.computed;
  return { label: tagRaw, hint: tagRaw };
}

// ---- claim kind (the record's actual kind) ------------------------------------------------------
export const CLAIM_KIND_TERMS = {
  decision: "decision",
  hypothesis: "hypothesis",
  ingestion: "research note",
  knowledge: "product knowledge",
  computed: "calculated figure",
  ai_proposal: "AI proposal, confirmed by a person",
};
export function claimKindLabel(kind) {
  return CLAIM_KIND_TERMS[kind] || kind;
}

// ---- evidence section --------------------------------------------------------------------------
export const EVIDENCE_SECTION_TERMS = {
  evidence_for: "supporting",
  evidence_against: "against",
  not_doing: "explicitly not doing",
};
export function evidenceSectionLabel(section) {
  return EVIDENCE_SECTION_TERMS[section] || section;
}

// ---- decision / hypothesis status (already close to plain English; kept centralized) ----------
export const STATUS_TERMS = {
  pending: "not yet decided",
  decided: "decided",
  superseded: "superseded by a newer decision",
  open: "still open",
  supported: "supported by evidence",
  refuted: "refuted",
  proposed: "awaiting confirmation",
  confirmed: "confirmed",
  rejected: "rejected",
  within: "within threshold",
  marginal: "marginal",
  missed: "missed threshold",
};
export function statusLabel(status) {
  return STATUS_TERMS[status] || status;
}

// ---- AI touchpoints -------------------------------------------------------------------------------
export const TOUCHPOINT_TERMS = {
  env_scan: "Environment scan",
  revenue_proposal: "Revenue proposal",
  deviation_explanation: "Deviation explanation",
  assistant: "Assistant conversation",
};
export function touchpointLabel(tp) {
  return TOUCHPOINT_TERMS[tp] || tp;
}

// ---- figure ref kind (Ask view citations) -------------------------------------------------------
export const FIGURE_REF_KIND_TERMS = {
  plan_value: "planned figure",
  parameter: "fitted parameter",
  derivation: "how this was calculated",
  claim: "decision",
};
export function figureRefKindLabel(kind) {
  return FIGURE_REF_KIND_TERMS[kind] || kind;
}

// ---- DOM helpers: plain label + secondary identifier ----------------------------------------------

/** A small muted secondary badge carrying the raw technical identifier — never the only thing
 * shown (the plain label always precedes it), always available for anyone who needs the code. */
export function codeTag(text) {
  if (text === null || text === undefined || text === "") return null;
  return el("span", { class: "term-code", title: String(text) }, [String(text)]);
}

/** Plain label followed by its secondary identifier, as inline children for any el(...) call. */
export function termInline(plain, code) {
  const kids = [plain];
  const tag = codeTag(code);
  if (tag) kids.push(" ", tag);
  return kids;
}

/** A <th>/<td>-ready two-part header: the plain word on top, the symbol/code muted beneath. */
export function termHeader(tag, plain, code) {
  const kids = [el("span", { class: "term-head-primary" }, [plain])];
  const c = codeTag(code);
  if (c) kids.push(el("span", { class: "term-head-code" }, [String(code)]));
  return el(tag, {}, kids);
}

// ---- free-text prettifier -------------------------------------------------------------------
// The assistant (Ask view) writes its own figure labels and is not required to speak our plain
// language (its citation is the ref, not its wording) - in practice it reaches for the same
// category codes and path enum values everywhere else in this UI, e.g. "REV 2026 (base,
// valorized)". Word-boundary substitution keeps the model's exact sentence structure while
// replacing the identifiers it used with the same plain words the rest of the app uses, so the
// terminology rule holds even for text this app did not itself generate.
const _CODE_RE = new RegExp(`\\b(${Object.keys(CATEGORY_NAMES).join("|")})\\b`, "g");
const _PATH_RE = new RegExp(`\\b(${Object.keys(PATH_TERMS).join("|")})\\b`, "g");

export function prettifyLabel(text) {
  if (!text) return text;
  return String(text)
    .replace(_CODE_RE, (m) => categoryName(m))
    .replace(_PATH_RE, (m) => pathTerm(m).label);
}

/** A chip for a plan/statement path, primary wording + the raw path as its title/secondary. */
export function pathChip(path, chipFn) {
  if (!path) return null;
  const t = pathTerm(path);
  const marked = MARKED_PATHS.has(path);
  const node = chipFn(t.label, `chip-path${marked ? " chip-path-marked chip-path-" + path : ""}`);
  node.title = t.hint || path;
  node.append(codeTag(path));
  return node;
}

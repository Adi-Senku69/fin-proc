/* views/ask.js — the conversational view (UI.md Part 3). The centrepiece: a question box and
 * answers rendered from POST /assistant/ask as flowing prose, not a table. The one hard
 * constraint this view exists to demonstrate: "the assistant may not assert a figure it cannot
 * source" — every number is a citation that resolves to a row in the engine, or the whole
 * answer is refused (HTTP 422) and nothing is persisted. That refusal is rendered prominently,
 * as the most persuasive thing this product does, never hidden or styled as a crash.
 *
 * This view computes nothing: every segment, every usage figure, every refusal message is
 * exactly what the API returned. Each call to /assistant/ask spends real money, so the view
 * shows the token usage the answer already reports and warns once, quietly, above the form.
 */

import { el, clear, chip, failurePanel, loadingPanel } from "../dom.js";
import { fmtNum } from "../format.js";
import { api } from "../api.js";
import { categoryName, figureRefKindLabel, codeTag, prettifyLabel } from "../terms.js";

const SCENARIOS = ["base", "best", "worst"];

export async function render(container, params, ctx) {
  container.append(el("h2", {}, ["Ask"]));
  container.append(
    el("p", { class: "muted" }, [
      "Ask a question about the plan. Every figure in the answer is a citation that resolves to a real row " +
        "in the engine — a planned figure, a fitted parameter, a calculation, or a decision. When the " +
        "assistant can't source a number this way, the whole answer is refused rather than shown with a made-up figure.",
    ])
  );
  container.append(
    el("div", { class: "ask-cost-notice" }, [
      "Each question below calls the live model and costs real money (it is billed per call, including when it is refused). Ask deliberately.",
    ])
  );

  const thread = el("div", { class: "ask-thread" });

  const form = el("form", { class: "ask-form" });
  const scenarioSel = el("select", { name: "scenario_kind" }, SCENARIOS.map((k) => el("option", { value: k }, [k])));
  const textarea = el("textarea", {
    class: "ask-question",
    rows: "2",
    placeholder: "e.g. Why did personnel costs move in 2027, and what set that figure?",
  });
  const submitBtn = el("button", { type: "submit", class: "btn" }, ["Ask"]);
  form.append(el("label", {}, ["scenario ", scenarioSel]), textarea, submitBtn);
  container.append(form, thread);

  // Enter submits; Shift+Enter inserts a newline (a real call takes many seconds, so the
  // pending state below matters more here than in any other view's form).
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = textarea.value.trim();
    if (!question) return;
    const scenario_kind = scenarioSel.value;

    submitBtn.disabled = true;
    textarea.disabled = true;
    const pending = el("div", { class: "ask-exchange" }, [
      el("div", { class: "ask-question-echo" }, [question]),
      el("div", { class: "ask-pending" }, [
        el("span", { class: "spinner", "aria-hidden": "true" }),
        el("span", {}, ["Asking the assistant… a real call can take a while."]),
      ]),
    ]);
    thread.prepend(pending);

    const res = await api.post("/assistant/ask", { question, scenario_kind });

    submitBtn.disabled = false;
    textarea.disabled = false;

    const exchange = el("div", { class: "ask-exchange" }, [el("div", { class: "ask-question-echo" }, [question])]);
    pending.replaceWith(exchange);

    if (!res.ok) {
      if (res.status === 422) {
        exchange.append(refusalPanel(res.message));
      } else {
        exchange.append(failurePanel("POST /assistant/ask", res, () => {}));
      }
      return;
    }

    renderAnswer(exchange, res.data, ctx);
    textarea.value = "";
    textarea.focus();
  });
}

// --------------------------------------------------------------------------- refusal (UI.md Part 3: a feature, not an error)

/** The API's 422 detail is one string: "assistant answer rejected (N unsourced figure(s)): a; b; c".
 * Split it into a plain-language lede and the list of what specifically failed — verbatim,
 * because that precision is the demonstration. */
function refusalPanel(detail) {
  const text = String(detail || "the assistant produced an answer that could not be verified");
  const m = /^(.*?):\s*(.+)$/.exec(text);
  const items = m ? m[2].split("; ").map((s) => s.trim()).filter(Boolean) : [];

  const panel = el("div", { class: "ask-refusal" }, [
    el("div", { class: "ask-refusal-title" }, ["Refused: the assistant tried to state a figure it could not source"]),
    el("div", { class: "ask-refusal-lede" }, [
      "This is the provenance rule working as intended, not a crash: the assistant produced a number (or a claim, " +
        "or a proposal) that did not resolve to a real row in the engine, so the whole answer was discarded before " +
        "anything was shown or saved. Nothing was persisted — no ai_record exists for this attempt.",
    ]),
  ]);
  if (items.length) {
    panel.append(
      el("div", { class: "muted" }, ["What failed:"]),
      el("ul", { class: "ask-refusal-list" }, items.map((it) => el("li", {}, [it])))
    );
  } else {
    panel.append(el("div", { class: "ask-refusal-lede" }, [text]));
  }
  return panel;
}

// --------------------------------------------------------------------------- answer rendering

function renderAnswer(exchange, answer, ctx) {
  const prose = el("div", { class: "ask-answer-prose" });
  for (const seg of answer.segments || []) {
    if (seg.type === "text") {
      prose.append(document.createTextNode(seg.text), " ");
    } else if (seg.type === "figure") {
      prose.append(figureChip(seg, ctx), " ");
    } else if (seg.type === "claim") {
      prose.append(claimChip(seg, ctx), " ");
    }
  }
  if (!(answer.segments || []).length) {
    prose.append(el("span", { class: "muted" }, ["(empty answer)"]));
  }
  exchange.append(prose);

  if (answer.proposal) {
    exchange.append(proposalCard(answer.proposal, ctx));
  }

  exchange.append(usageLine(answer));
}

function figureChip(seg, ctx) {
  const plainLabel = prettifyLabel(seg.label);
  const btn = el("button", { type: "button", class: "figure-chip" }, [
    el("span", { class: "figure-chip-label" }, [plainLabel + ":"]),
    el("span", { class: "figure-chip-value" }, [`${fmtNum(seg.value)} ${seg.unit}`]),
  ]);
  const kindLabel = figureRefKindLabel(seg.ref.kind);
  btn.title = `opens the ${kindLabel} this cites (${seg.ref.kind} #${seg.ref.id})` + (plainLabel !== seg.label ? `  ·  as written: ${seg.label}` : "");
  let note = null;
  btn.addEventListener("click", () => {
    if (seg.ref.kind === "plan_value") {
      ctx.navigate({ view: "trace", kind: "plan-value", id: seg.ref.id });
      return;
    }
    if (seg.ref.kind === "claim") {
      ctx.navigate({ view: "brain", claim: seg.ref.id });
      return;
    }
    if (seg.ref.kind === "parameter") {
      ctx.navigate({ view: "plan", scenario: "base", highlight_param: seg.ref.id });
      return;
    }
    // "derivation": verified against the engine (the server checked this before the answer was
    // ever shown) but there is no page in this build addressable by a raw derivation id —
    // only by the plan value or statement line that sits on top of it. Say so honestly instead
    // of guessing a link that might land on an unrelated row of the same id.
    if (note) {
      note.remove();
      note = null;
      return;
    }
    note = el("span", { class: "term-code", style: "display:inline-block;margin-left:4px;" }, [
      `verified derivation #${seg.ref.id} — no direct page for a calculation id yet; open it from the plan value that uses it`,
    ]);
    btn.after(note);
  });
  return btn;
}

function claimChip(seg, ctx) {
  const btn = el("button", { type: "button", class: "claim-chip" }, [
    el("span", {}, [seg.title]),
    chip(seg.status, `chip-status chip-${seg.status}`),
  ]);
  btn.title = `opens this decision in Brain (claim #${seg.claim_id})`;
  btn.addEventListener("click", () => ctx.navigate({ view: "brain", claim: seg.claim_id }));
  return btn;
}

function usageLine(answer) {
  const usage = answer.usage || {};
  const bits = Object.entries(usage).map(([k, v]) => `${k}=${v}`);
  return el("div", { class: "ask-usage" }, [
    `ai_record #${answer.ai_record_id}`,
    "  ·  token usage: ",
    bits.length ? bits.join("  ") : "not reported",
  ]);
}

// --------------------------------------------------------------------------- proposal card

function proposalCard(proposal, ctx) {
  const card = el("div", { class: "ask-proposal-card" });
  card.append(
    el("div", { class: "ask-proposal-title" }, ["Proposal — awaiting confirmation"]),
    el("div", {}, [categoryName(proposal.category_code), " ", codeTag(proposal.category_code), ` · ${proposal.year}`]),
    el("div", { class: "ask-proposal-value" }, [`${fmtNum(proposal.proposed_value)} k EUR`]),
    el("div", {}, [el("strong", {}, ["rationale: "]), proposal.rationale || "-"])
  );

  if (proposal.ai_record_id == null) {
    card.append(el("div", { class: "muted" }, ["no ai_record id was returned with this proposal — cannot confirm or reject it here."]));
    return card;
  }

  const nameInput = el("input", { type: "text", value: "demo-ui", placeholder: "your name", class: "" });
  const status = el("div", { class: "muted" }, []);
  const confirmBtn = el("button", { type: "button", class: "btn btn-small btn-confirm" }, ["Confirm"]);
  const rejectBtn = el("button", { type: "button", class: "btn btn-small btn-reject" }, ["Reject"]);
  const actions = el("div", { class: "ask-proposal-actions" }, [
    el("label", {}, ["confirmed/rejected by ", nameInput]),
    confirmBtn,
    rejectBtn,
  ]);
  card.append(actions, status);

  const finish = (label) => {
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;
    nameInput.disabled = true;
    clear(status);
    status.append(label);
  };

  confirmBtn.addEventListener("click", async () => {
    const who = nameInput.value.trim();
    if (!who) {
      status.textContent = "enter a name first.";
      return;
    }
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;
    clear(status);
    status.append(loadingPanel("Confirming and re-running the plan…"));
    const res = await api.post(`/ai/records/${proposal.ai_record_id}/confirm`, { confirmed_by: who });
    clear(status);
    if (!res.ok) {
      status.append(failurePanel(`/ai/records/${proposal.ai_record_id}/confirm`, res, () => {}));
      confirmBtn.disabled = false;
      rejectBtn.disabled = false;
      return;
    }
    finish(`confirmed by ${who}${res.data.run ? ` · plan re-run: ${res.data.run.label}` : ""}`);
    status.append(el("div", {}, [el("a", { href: "#view=ai&record=" + proposal.ai_record_id }, ["Open this record in AI records →"])]));
  });

  rejectBtn.addEventListener("click", async () => {
    const who = nameInput.value.trim();
    if (!who) {
      status.textContent = "enter a name first.";
      return;
    }
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;
    clear(status);
    status.append(loadingPanel("Rejecting…"));
    const res = await api.post(`/ai/records/${proposal.ai_record_id}/reject`, { rejected_by: who });
    clear(status);
    if (!res.ok) {
      status.append(failurePanel(`/ai/records/${proposal.ai_record_id}/reject`, res, () => {}));
      confirmBtn.disabled = false;
      rejectBtn.disabled = false;
      return;
    }
    finish(`rejected by ${who}`);
  });

  return card;
}

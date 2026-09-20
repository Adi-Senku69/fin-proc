/**
 * Ask screen — ported from nvplan/api/static/js/views/ask.js. The conversational view. The one
 * hard constraint this screen exists to demonstrate: "the assistant may not assert a figure it
 * cannot source" — every number is a citation that resolves to a real row in the engine, or the
 * whole answer is refused (HTTP 422) and nothing is persisted. That refusal is rendered
 * prominently, as the most persuasive thing this product does — never hidden, never styled as a
 * crash. No counterpart in the reference React app; this and Brain are where the platform
 * direction (PLATFORM.md) actually shows up on screen.
 *
 * Computes nothing: every segment, every usage figure, every refusal message is exactly what
 * `POST /assistant/ask` returned. Each call spends real money (billed per call, including when
 * refused), so a warning sits above the form and this view never auto-submits anything.
 */
import { useRef, useState } from 'react'

import { askQuestion, confirmProposal, rejectProposal } from '../api/adapters/ask'
import type { ApiResult } from '../api/client'
import type { AskProposal, AskSegment, AssistantAnswer } from '../domain/ask'
import { fmtNum } from '../domain/format'
import { categoryName, figureRefKindLabel, prettifyLabel } from '../domain/terms'
import type { Route } from '../routing/route'
import { Chip, CodeTag, LoadingPanel } from './shared'

type Navigate = (p: Record<string, string | number | null | undefined>) => void

const SCENARIOS = ['base', 'best', 'worst'] as const

interface Exchange {
  id: number
  question: string
  result: 'pending' | ApiResult<AssistantAnswer>
}

export function AskScreen({ navigate }: { route: Route; navigate: Navigate }) {
  const [question, setQuestion] = useState('')
  const [scenarioKind, setScenarioKind] = useState<string>('base')
  const [thread, setThread] = useState<Exchange[]>([])
  const [busy, setBusy] = useState(false)
  const nextId = useRef(0)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const q = question.trim()
    if (!q || busy) return
    const id = ++nextId.current
    setThread((t) => [{ id, question: q, result: 'pending' }, ...t])
    setBusy(true)
    const res = await askQuestion(q, scenarioKind)
    setBusy(false)
    setThread((t) => t.map((x) => (x.id === id ? { ...x, result: res } : x)))
    setQuestion('')
  }

  return (
    <div>
      <h2>Ask</h2>
      <p className="muted">
        Ask a question about the plan. Every figure in the answer is a citation that resolves to a real row in the
        engine — a planned figure, a fitted parameter, a calculation, or a decision. When the assistant can&apos;t
        source a number this way, the whole answer is refused rather than shown with a made-up figure.
      </p>
      <div className="ask-cost-notice">
        Each question below calls the live model and costs real money (it is billed per call, including when it is
        refused). Ask deliberately.
      </div>

      <form
        className="ask-form"
        onSubmit={submit}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            void submit(e as unknown as React.FormEvent)
          }
        }}
      >
        <label>
          scenario{' '}
          <select value={scenarioKind} onChange={(e) => setScenarioKind(e.target.value)}>
            {SCENARIOS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <textarea
          className="ask-question"
          rows={2}
          placeholder="e.g. Why did personnel costs move in 2027, and what set that figure?"
          value={question}
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button type="submit" className="btn" disabled={busy}>
          Ask
        </button>
      </form>

      <div className="ask-thread">
        {thread.map((ex) => (
          <Exchange key={ex.id} exchange={ex} navigate={navigate} />
        ))}
      </div>
    </div>
  )
}

function Exchange({ exchange, navigate }: { exchange: Exchange; navigate: Navigate }) {
  return (
    <div className="ask-exchange">
      <div className="ask-question-echo">{exchange.question}</div>
      {exchange.result === 'pending' ? (
        <div className="ask-pending">
          <span className="spinner" aria-hidden="true" />
          <span>Asking the assistant… a real call can take a while.</span>
        </div>
      ) : exchange.result.ok ? (
        <Answer answer={exchange.result.data} navigate={navigate} />
      ) : exchange.result.status === 422 ? (
        <RefusalPanel detail={exchange.result.message} />
      ) : (
        <div className="panel error-panel">
          <div className="error-title">Request failed — HTTP {exchange.result.status || '0 (network)'}</div>
          <div className="error-message">{exchange.result.message}</div>
        </div>
      )}
    </div>
  )
}

/** The API's 422 detail is one string: "assistant answer rejected (N unsourced figure(s)): a; b;
 * c". Split it into a plain-language lede and the list of what specifically failed — verbatim,
 * because that precision is the demonstration. */
function RefusalPanel({ detail }: { detail: string }) {
  const text = detail || 'the assistant produced an answer that could not be verified'
  const m = /^(.*?):\s*(.+)$/.exec(text)
  const items = m ? m[2].split('; ').map((s) => s.trim()).filter(Boolean) : []
  return (
    <div className="ask-refusal">
      <div className="ask-refusal-title">Refused: the assistant tried to state a figure it could not source</div>
      <div className="ask-refusal-lede">
        This is the provenance rule working as intended, not a crash: the assistant produced a number (or a claim, or
        a proposal) that did not resolve to a real row in the engine, so the whole answer was discarded before
        anything was shown or saved. Nothing was persisted — no ai_record exists for this attempt.
      </div>
      {items.length > 0 ? (
        <>
          <div className="muted">What failed:</div>
          <ul className="ask-refusal-list">
            {items.map((it, i) => (
              <li key={i}>{it}</li>
            ))}
          </ul>
        </>
      ) : (
        <div className="ask-refusal-lede">{text}</div>
      )}
    </div>
  )
}

function Answer({ answer, navigate }: { answer: AssistantAnswer; navigate: Navigate }) {
  return (
    <>
      <div className="ask-answer-prose">
        {(answer.segments || []).map((seg, i) => (
          <Segment key={i} seg={seg} navigate={navigate} />
        ))}
        {!(answer.segments || []).length && <span className="muted">(empty answer)</span>}
      </div>
      {answer.proposal && <ProposalCard proposal={answer.proposal} navigate={navigate} />}
      <UsageLine answer={answer} />
    </>
  )
}

function Segment({ seg, navigate }: { seg: AskSegment; navigate: Navigate }) {
  if (seg.type === 'text') return <span className="text-segment">{seg.text} </span>
  if (seg.type === 'figure') return <FigureChip seg={seg} navigate={navigate} />
  return <ClaimChip seg={seg} navigate={navigate} />
}

function FigureChip({ seg, navigate }: { seg: Extract<AskSegment, { type: 'figure' }>; navigate: Navigate }) {
  const [showNote, setShowNote] = useState(false)
  const plainLabel = prettifyLabel(seg.label)
  const kindLabel = figureRefKindLabel(seg.ref.kind)
  const title = `opens the ${kindLabel} this cites (${seg.ref.kind} #${seg.ref.id})${plainLabel !== seg.label ? `  ·  as written: ${seg.label}` : ''}`

  function onClick() {
    if (seg.ref.kind === 'plan_value') {
      navigate({ view: 'trace', kind: 'plan-value', id: seg.ref.id })
      return
    }
    if (seg.ref.kind === 'claim') {
      navigate({ view: 'brain', claim: seg.ref.id })
      return
    }
    if (seg.ref.kind === 'parameter') {
      navigate({ view: 'plan', scenario: 'base', highlight_param: seg.ref.id })
      return
    }
    setShowNote((v) => !v)
  }

  return (
    <>
      {' '}
      <button type="button" className="figure-chip" title={title} onClick={onClick}>
        <span className="figure-chip-label">{plainLabel}:</span>
        <span className="figure-chip-value">
          {fmtNum(seg.value)} {seg.unit}
        </span>
      </button>
      {showNote && seg.ref.kind === 'derivation' && (
        <span className="term-code" style={{ display: 'inline-block', marginLeft: 4 }}>
          verified derivation #{seg.ref.id} — no direct page for a calculation id yet; open it from the plan value
          that uses it
        </span>
      )}{' '}
    </>
  )
}

function ClaimChip({ seg, navigate }: { seg: Extract<AskSegment, { type: 'claim' }>; navigate: Navigate }) {
  return (
    <>
      {' '}
      <button
        type="button"
        className="claim-chip"
        title={`opens this decision in Brain (claim #${seg.claim_id})`}
        onClick={() => navigate({ view: 'brain', claim: seg.claim_id })}
      >
        <span>{seg.title}</span>
        <Chip className={`chip-status chip-${seg.status}`}>{seg.status}</Chip>
      </button>{' '}
    </>
  )
}

function UsageLine({ answer }: { answer: AssistantAnswer }) {
  const usage = answer.usage || {}
  const bits = Object.entries(usage).map(([k, v]) => `${k}=${v}`)
  return (
    <div className="ask-usage">
      ai_record #{answer.ai_record_id} · token usage: {bits.length ? bits.join('  ') : 'not reported'}
    </div>
  )
}

function ProposalCard({ proposal, navigate }: { proposal: AskProposal; navigate: Navigate }) {
  const [name, setName] = useState('demo-ui')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<React.ReactNode>(null)
  const [done, setDone] = useState(false)

  if (proposal.ai_record_id == null) {
    return (
      <div className="ask-proposal-card">
        <ProposalHeader proposal={proposal} />
        <div className="muted">no ai_record id was returned with this proposal — cannot confirm or reject it here.</div>
      </div>
    )
  }

  async function confirm() {
    const who = name.trim()
    if (!who) {
      setStatus('enter a name first.')
      return
    }
    setBusy(true)
    setStatus(<LoadingPanel text="Confirming and re-running the plan…" />)
    const res = await confirmProposal(proposal.ai_record_id!, who)
    setBusy(false)
    if (!res.ok) {
      setStatus(
        <div className="panel error-panel">
          <div className="error-title">Request failed — HTTP {res.status}</div>
          <div className="error-message">{res.message}</div>
        </div>,
      )
      return
    }
    setDone(true)
    setStatus(
      <>
        confirmed by {who}
        {res.data.run ? ` · plan re-run: ${res.data.run.label}` : ''}
        <div>
          <a href={`#view=ai&record=${proposal.ai_record_id}`} onClick={() => navigate({ view: 'ai', record: proposal.ai_record_id })}>
            Open this record in AI records →
          </a>
        </div>
      </>,
    )
  }

  async function reject() {
    const who = name.trim()
    if (!who) {
      setStatus('enter a name first.')
      return
    }
    setBusy(true)
    setStatus(<LoadingPanel text="Rejecting…" />)
    const res = await rejectProposal(proposal.ai_record_id!, who)
    setBusy(false)
    if (!res.ok) {
      setStatus(
        <div className="panel error-panel">
          <div className="error-title">Request failed — HTTP {res.status}</div>
          <div className="error-message">{res.message}</div>
        </div>,
      )
      return
    }
    setDone(true)
    setStatus(`rejected by ${who}`)
  }

  return (
    <div className="ask-proposal-card">
      <ProposalHeader proposal={proposal} />
      <div className="ask-proposal-actions">
        <label>
          confirmed/rejected by <input type="text" value={name} disabled={busy || done} onChange={(e) => setName(e.target.value)} />
        </label>
        <button type="button" className="btn btn-small btn-confirm" disabled={busy || done} onClick={confirm}>
          Confirm
        </button>
        <button type="button" className="btn btn-small btn-reject" disabled={busy || done} onClick={reject}>
          Reject
        </button>
      </div>
      <div className="muted">{status}</div>
    </div>
  )
}

function ProposalHeader({ proposal }: { proposal: AskProposal }) {
  return (
    <>
      <div className="ask-proposal-title">Proposal — awaiting confirmation</div>
      <div>
        {categoryName(proposal.category_code)} <CodeTag>{proposal.category_code}</CodeTag> · {proposal.year}
      </div>
      <div className="ask-proposal-value">{fmtNum(proposal.proposed_value)} k EUR</div>
      <div>
        <strong>rationale: </strong>
        {proposal.rationale || '-'}
      </div>
    </>
  )
}

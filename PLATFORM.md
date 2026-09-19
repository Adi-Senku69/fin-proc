# Platform architecture — decision provenance across product management

Status: architecture contract, settled 2026-09-19. `PLAN.md` continues to govern the finance module.
References studied: `phuryn/pm-skills` (capability catalogue), `phuryn/pm-brain` (decision memory).

## 1. What this becomes

Three layers over one substrate.

| Layer | Role | State |
|---|---|---|
| **Quantification** | The NewVision planning engine. Turns decisions into money: five-year plan, three scenarios, statements, backtest. | Built. Becomes the first module. |
| **Decision memory** | The brain. Decisions, hypotheses, evidence, ingested research, product knowledge. | To build. Phase P1. |
| **Capabilities** | Procedures a product manager runs: strategy, positioning, discovery, execution. Skill or code per §8. | Strategy cluster first. Phase P3. |

## 2. Governing principle, unchanged and generalized

The finance module already enforces that **a number cannot exist without its derivation**, by a non-null
foreign key rather than by convention. The platform extends the same rule from arithmetic to judgment:

> A claim cannot exist without its provenance. An AI-produced claim cannot enter the record without a
> named human confirming it. Both are enforced in code, not asked for in a prompt.

## 3. Source of truth

**Markdown files under `brain/` are authoritative.** They are git-native, diffable, reviewable, and
readable without our software. The database is a **derived index**, rebuildable at any time by
reindexing the tree. Two rules follow, and they are absolute:

- Never edit the index as a way of changing a fact. Edit the file, reindex.
- Never treat the index as the record. If the two disagree, the file wins and the index is stale.

## 4. The provenance contract

### 4.1 Tag enum — closed set, nothing else is valid

Path-typed. Must be written as markdown links and must resolve from the containing file:

- `[ingestion/<path>](../ingestion/<path>)` — a record we synthesized from raw material
- `[source/<path>](../source/<path>)` — a raw artifact, transcript or document

Parenthetical, exact forms:

- `(stakeholder-verbal, <name>, <YYYY-MM-DD>)`
- `(intuition, <role>, <YYYY-MM-DD>)`
- `(industry-knowledge)`
- `(chat, no artifact)`

Ours, and the reason the two halves of this platform are worth joining:

- `(computed, <derivation-key>)` — a figure the finance engine produced. At reindex time it **must**
  resolve to a real `derivation` row, or the claim is rejected. This is how a product decision cites
  money.

### 4.2 The hard rule

Every bullet under an evidence heading carries exactly one tag from §4.1. A bullet without one is an
orphan and the file is rejected. Commentary, gaps and unknowns belong under a "Remaining ambiguities"
heading and are never evidence. Aggregate rows ("three customers, mixed sentiment") are not evidence.

### 4.3 Status lifecycles — closed enums

| Record | Lifecycle |
|---|---|
| decision | `pending` → `decided` → `superseded` |
| hypothesis | `open` → `supported` or `refuted` → `superseded` |
| ai proposal (existing) | `proposed` → `confirmed` or `rejected` |

A decided decision is never edited in place. It is superseded by a new file that links back to it.

### 4.4 Every decision states its reversal condition

A decision file must carry an observable condition that would reverse it: a metric threshold, a named
signal, or a date. "If things change" is rejected. This is the judgment-side equivalent of the
backtest: it makes a decision falsifiable in advance.

## 5. Layout

```
brain/                          <- source of truth, plain markdown
├── decisions/YYYY-MM-DD-<slug>.md
├── hypotheses/<feature-slug>.md
├── ingestion/{interviews,meetings,market,adhoc}/
├── source/                     <- raw artifacts, never rewritten
├── knowledge/
│   ├── strategy.md
│   ├── product/{metrics.md,roadmap.md,features/}
│   ├── users/{personas.md,segments.md,insights.md}
│   ├── market/{landscape.md,trends.md,competitors/}
│   └── org/{team.md,rituals.md,tools.md}
└── _SCHEMA.md per collection

provenance/    <- shared core: tag enum, parser, lifecycle, index models, validators
brainkit/      <- markdown parse, structural validation, reindex into the index
nvplan/        <- finance module, behaviour unchanged
```

## 6. Index tables

Additive. The nine existing tables keep their shape and their tests.

- `claim(id, kind, slug, path unique-nullable, title, status, date, body_sha256, derivation_id FK-nullable, ai_record_id FK-nullable, ingested_at)`
  where `kind` is one of `decision | hypothesis | ingestion | knowledge | computed | ai_proposal`.
- `evidence(id, claim_id FK **not null**, section, text, tag_kind, tag_raw, target_path nullable, target_claim_id FK-nullable, target_derivation_id FK-nullable, resolved bool not null)`
  where `section` is one of `evidence_for | evidence_against | not_doing` and `tag_kind` is the §4.1 enum.
- `claim_link(id, from_claim_id FK, to_claim_id FK, relation)` where `relation` is one of
  `supersedes | tests | informs | quantifies`.

`evidence.claim_id` is non-null for the same reason `plan_value.derivation_id` is: it makes an
unsourced claim unstorable.

## 7. The bridge — where this stops being two products

Both directions must work, and both must be traceable.

**Decision drives money.** A `decided` decision carrying a quantified effect for a category and year
becomes the revenue override the planning run already accepts. The affected plan values record the
decision as their provenance, so opening any downstream figure reaches the decision record, its
evidence, and the person who confirmed it. The existing confirmation gate is reused unchanged.

**Money informs decisions.** A decision cites a computed figure with `(computed, <derivation-key>)`.
Reindexing resolves that to the derivation row, so the decision's evidence is pinned to a specific
calculation over specific inputs, not to a number someone retyped.

### 7.1 The bridge contract (P2)

**Decision schema gains one optional block**, valid only on a `decided` decision:

```markdown
## Quantified effect
- category: REV
- year: 2027
- value: 22365.1
- unit: kEUR
```

Validation adds three codes: `effect_on_undecided` (error, the block is present but status is not
`decided`), `bad_effect` (error, unknown category, year outside the plan horizon, non-numeric or
non-positive value, unit other than `kEUR`), and `effect_not_wired` (warning, a category other than
`REV`, which parses and indexes but drives nothing in P2).

**`bridge/` is the only package allowed to import both** `nvplan` and `brainkit`. Everything else stays
decoupled. Its surface:

```python
# bridge/db.py
def init_platform_db(engine) -> Engine        # creates BOTH metadatas in one SQLite file

# bridge/lookup.py
def make_derivation_lookup(session) -> Callable[[str], int | None]
    # resolves a (computed, <key>) tag to a derivation row id by matching
    # json_extract(derivation.inputs_json, '$._key')

# bridge/effects.py
@dataclass(frozen=True)
class QuantifiedEffect:
    claim_id: int; decision_slug: str; decision_title: str
    category_code: str; year: int; value: float; unit: str
    status: str; decided_on: date | None

def decided_effects(session) -> list[QuantifiedEffect]      # from the claim index, decided only
def revenue_override(effects) -> dict[int, Override]         # year -> Override, REV effects only
```

**`nvplan` changes, additive only.** `RevenueOverride` becomes
`Mapping[int, tuple[float, int] | Override]` so every existing tuple caller keeps working, where:

```python
@dataclass(frozen=True)
class Override:
    value: float
    ai_record_id: int | None = None
    claim_id: int | None = None
    formula_text: str = AI_OVERRIDE_FORMULA
    label: str = ""
```

`plan_value` gains a nullable `claim_id`, a plain integer with no foreign key, consistent with §6's
cross-package rule. A claim-sourced override writes `formula_text = "confirmed decision"` and records
`claim_id`, `decision_slug`, `value` and the displaced `default_value` in the derivation inputs, so the
discarded default stays traceable exactly as it does for a confirmed AI proposal.

**Trace.** A plan value carrying a `claim_id` gets a `claim` block beside the existing `ai` block,
holding the decision title, status, decided date, its evidence rows with their provenance tags, and its
reversal condition. `render_trace` prints it. Both tables live in one SQLite file, so one session reads
both; `init_platform_db` is what guarantees that.

**Both directions must be demonstrable end to end**: a decided decision with a quantified effect
recalculates the plan and every affected figure traces back to the decision and its evidence; and a
decision citing `(computed, param:PERS)` resolves to the real derivation row.

## 8. Skill or code

The test: **would two competent people, given the same inputs, be expected to produce the same
answer?** If yes it is code, because code can be tested, traced and cross-checked. If no it is a
skill, because its value is judgment.

| Becomes code | Stays a skill |
|---|---|
| Prioritization scoring, market sizing, cohort and retention maths, experiment significance, metric trees | Interview synthesis, positioning, brainstorming, competitive narrative, vision |
| Anything that will appear as a number on screen | Anything whose output is an argument |

Everything in the left column inherits the derivation ledger. Everything in the right column produces
claims that must wear provenance tags.

## 9. Enforcement, twice

1. **Write time.** A hook validates a brain file on every write: tag on every evidence row, links
   resolve, status in the enum, reversal condition present. Fast feedback, same mechanism pm-brain uses.
2. **Reindex time.** The same validation runs again, and a claim that fails it cannot become a row. The
   schema is the backstop, so a hook that is disabled or bypassed cannot corrupt the index.

## 10. Phases

| Phase | Work | Done when |
|---|---|---|
| P0 | `provenance/`: tag parser, lifecycle enums, index models, structural validators | Tag enum round-trips; an orphan row is rejected; models create cleanly |
| P1 | `brainkit/` + `brain/` scaffold and schemas; parse, validate, reindex, rebuild | A hand-written decision file indexes cleanly; a bad one is rejected with a precise message; reindexing is idempotent |
| P2 | The bridge, both directions | A decision cascades into a plan and the trace reaches it; a `computed` tag resolves to a derivation |
| P3 | Strategy and positioning cluster, per §8 | Skills load through the existing mechanism; coded parts carry derivations |
| P4 | Remaining capability clusters | Out of scope for now |

## 11. Explicitly not now

The other seven capability domains from the reference catalogue. Multi-user and auth. A web interface.
Vector search or embeddings, which the file-plus-index design exists to avoid. Guardrail middleware,
still deferred. None of these are blocked by this architecture; all are noise until P0 to P2 stand up.

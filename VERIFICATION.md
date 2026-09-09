# Hand-verification pass

Per the PDF (section 8 and day 15): "a model that runs without errors is not the same as a model that is
correct." This file records what the orchestrator checked **independently of the code under test**, using
plain numpy on the raw CSV, and what was found. Date: 2026-09-09. Data: `data/illustrative/*` (ILLUSTRATIVE).

## 1. Regression, projection, scenarios — recomputed by hand

Independent script: read `actuals.csv`, window 2021–2025, `np.linalg.lstsq` with an intercept for
Personnel costs on Revenue, fixed part = cost − β·revenue, v = mean YoY growth of the fixed part,
g = mean YoY revenue growth, revenue 2028 = revenue 2025 · (1+g)³, PERS 2028 = α(1+v)³ + β·revenue 2028.

| Quantity | Hand | Database (`run_plan`) | Difference |
|---|---|---|---|
| α (PERS) | 3,833.6504 | 3,833.6504 | 0 |
| β (PERS) | 0.487661 | 0.487661 | 0 |
| R² (PERS) | 0.999283 | 0.999283 | 0 |
| v (PERS) | −0.001842 | −0.001842 | 0 |
| g (revenue) | 0.061965 | 0.061965 | 0 |
| Revenue 2028 base | 24,870.1000 | 24,870.1000 | 0.0 |
| Personnel 2028 base | 15,940.6796 | 15,940.6796 | 0.0 |
| Best / base revenue 2028 | 1.08 | 1.080000 | 0 |
| Worst / base revenue 2028 | 0.92 | 0.920000 | 0 |
| Other costs 2027 = regressed + depreciation | depreciation 505.1 from investment plan | DEPR row 505.1 | 0 |

## 2. Statements — balance and tie, recomputed by hand

For the base scenario, every plan year 2026–2030:

- total_assets − total_liabilities_equity: max |diff| 1.8e−12.
- Net cash flow recomputed as NI + depreciation − Δreceivables − Δinventory + Δpayables − capex equals the
  stored net_cash_flow and equals Δcash on the balance sheet, to 4 decimals, every year.
- Tax = 25% × EBIT in every year (EBIT positive throughout).

Design note: cash is computed from the cash-flow statement and the balance identity is *checked*, not forced.
The mapping YAML's `balancing:` formula for cash is documentation only (annotated in the file).

## 3. Formula integrity (from the test suite, read and confirmed)

- +1000 k€ on base revenue 2027 moves each cost category in 2027 by exactly β·1000 and nothing else.
- Rerunning the plan creates new parameter/scenario/derivation rows; old rows are byte-identical afterwards.
- A `plan_value` or `statement_line` without a derivation raises `IntegrityError` (schema, not convention).
- Confirming a proposal reruns the cascade; PERS in the proposal year equals α(1+v)² + β·proposed to 1e−12.
- Rejecting a proposal leaves no plan_value referencing it and creates no scenario.

## 4. Backtest (deterministic)

12 rolling cases (5-year windows 2016–2020 … 2020–2024, horizons 1–3). All 15 category × horizon cells are
within the 5% MAPE threshold; worst is External services h2 at 3.97% (costs given actual revenue) and
4.26% end-to-end along the default revenue path. See `GET /backtest` or `uv run nvplan-demo` for the tables.

## 5. Findings that a sceptical reviewer will raise — stated plainly

### 5.1 OLS with a constant intercept cannot separate a *growing* fixed part from the variable rate

The illustrative data was generated with a fixed component that valorizes (α·(1+v)^t, v = 2.8% for
Personnel) plus β·Revenue. The PDF's estimator fits a **constant** α over the window. Over five years the
growth of α is almost collinear with the growth of revenue, so OLS absorbs it into β:

| Category | Generating β | Fitted β (2021–2025) | Generating v | Fitted v |
|---|---|---|---|---|
| Material | 0.045 | 0.054 | 2.0% | −0.5% |
| External services | 0.035 | 0.040 | 3.0% | 1.5% |
| Personnel | 0.300 | 0.488 | 2.8% | −0.2% |
| Other (excl. depreciation) | 0.025 | 0.045 | 2.5% | 1.0% |

R² is above 0.966 everywhere, and the backtests are within threshold, because the *sum* is predicted well.
But the fixed/variable *split* shown on the trace panel is not the generating economics. This is not a code
bug (the regression tests reconcile the fit to the identifiable parameters exactly, and carry two strict
xfails documenting the bias); it is a property of the method in the PDF. Options, in order of fidelity to
the PDF: (a) keep as is and state it, since the PDF says to surface R² and report honestly; (b) fit α, v, β
jointly (non-linear least squares on α(1+v)^t + β·R_t) as an alternative estimator, selectable and recorded
in the derivation; (c) longer window. Recommendation: (b) as a switchable method, default still the PDF's.

### 5.2 The valorization estimator is fragile when the fitted intercept is near zero

Window 2019–2023, External services: α ≈ 10 k€, so the fixed part hovers around zero and its "mean YoY
growth" is −305%. The forecast still lands within threshold because β dominates, but the displayed v is
meaningless. A guard is needed: if |α| is below a share of mean cost, or the fixed part changes sign,
report v as undefined and valorize at 0 (or at inflation from the control table), and say so in the trace.

### 5.3 What the dummy data cannot prove

Backtest accuracy here reflects generator noise of 1–2%, not Newvision's real volatility. The 5–8%
threshold will be materially harder on real actuals, especially Revenue over horizons 2–3, as the PDF warns.

## 6. Demo output spot-checks (`uv run nvplan-demo`, scripted fake AI)

| Check | Hand | Demo |
|---|---|---|
| Confirmed proposal 2027 = default × (1 − 4.5%) | 23,418.9 × 0.955 = 22,365.1 | 22,365.1 |
| Personnel 2027 change = β × Δrevenue | 0.487661 × (−1,053.8) = −513.9 | −513.9 |
| Material 2025 deviation split: β × Δrevenue | 0.0491 × (−337.5) = −16.57 | −16.57 |
| Material residual = deviation − revenue-driven | −5.87 + 16.57 = 10.70 | 10.70 |
| Balance sheet of the post-confirmation scenario | balances 2025–2030 | diff 0.0000 |
| Nothing enters the plan unconfirmed | REV 2027 stays 23,418.9 after proposal, before confirmation | confirmed in output |

Note: the AI words in the demo are scripted fakes (no API key on the build machine). The tool calls,
control-table validation, prompt persistence, and the confirmation gate are the real code paths.

## 7. Test suite at the time of writing

`uv run pytest -q`: 159 passed, 2 xfailed (the two documented β-bias cases).

AI-layer rules added after the first pass: context management is eviction-only (large tool results move to a
readable file, nothing is ever blanked from the model's context), and every deviation explanation is checked
field by field against the deterministic plan-vs-actual table before it is stored; a mismatch rejects it.

## 8. Follow-ups recommended before the Newvision pilot

1. Add a guard for the valorization rate when the fitted fixed part is near zero (5.2).
2. Offer a joint α/v/β estimator as a selectable method, recorded in the derivation (5.1).
3. Run the three touchpoints against the real model (`ANTHROPIC_API_KEY`) and review the prompts on output.
4. React trace-panel UI over the existing JSON endpoints (deferred by design).

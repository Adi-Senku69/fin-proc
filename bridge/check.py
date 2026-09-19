"""bridge/check.py — prove both directions of the P2 bridge end to end (PLATFORM.md §7,
§7.1, §10). Console script: ``nvplan-bridge-check``.

Runs entirely against a fresh temporary SQLite database, makes no network call, and needs
no API key: this is a deterministic proof, not a demo of the AI layer.

    1. init both metadatas, seed categories, ingest the illustrative actuals, run a
       baseline plan.
    2. ingest brain/, resolving (computed, <key>) tags via bridge.lookup, and report how
       many resolved to a real derivation row (money informs decisions).
    3. read decided_effects, build the revenue override, rerun the plan.
    4. print before/after revenue and personnel for the affected year, and assert the
       personnel delta equals beta times the revenue delta (decision drives money).
    5. print the trace of that personnel plan value, so the decision appears in the
       lineage of a real downstream figure.

Exit 0 on success; non-zero with a legible reason otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nvplan import config as nvplan_config
from nvplan.db.models import Category, Parameter
from nvplan.db.session import get_engine, seed_categories
from nvplan.ingest.actuals import ingest_actuals, load_actuals_csv
from nvplan.services.planning import run_plan
from nvplan.services.trace import find_plan_value, render_trace, trace_plan_value

from provenance.models import Evidence, TagKind

from bridge.db import init_platform_db
from bridge.effects import WIRED_CATEGORY, decided_effects, revenue_override
from bridge.lookup import make_derivation_lookup
from brainkit.ingest import ingest_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAIN_ROOT = REPO_ROOT / "brain"


def _step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="nvplan-bridge-check-") as tmp:
        db_path = Path(tmp) / "platform.db"
        engine = get_engine(f"sqlite:///{db_path}")
        init_platform_db(engine)

        with Session(engine) as session:
            _step("1. seed categories, ingest illustrative actuals, run the baseline plan")
            seed_categories(session)
            actuals_df = load_actuals_csv(nvplan_config.DATA_DIR / "actuals.csv")
            n_actuals = ingest_actuals(session, actuals_df)
            print(f"ingested {n_actuals} actual rows from {nvplan_config.DATA_DIR / 'actuals.csv'}")
            baseline = run_plan(session, created_by="bridge-check")
            print(
                f"baseline plan: {baseline.n_plan_values} plan values, "
                f"{baseline.n_statement_lines} statement lines, {baseline.n_derivations} derivations"
            )

            _step("2. ingest brain/, resolving (computed, <key>) tags to real derivations")
            lookup = make_derivation_lookup(session)
            report = ingest_tree(session, BRAIN_ROOT, strict=True, derivation_lookup=lookup)
            print(
                f"files_seen={report.files_seen} ingested={report.ingested} "
                f"skipped_unchanged={report.skipped_unchanged} rejected={len(report.rejected)}"
            )
            if report.rejected:
                for path in report.rejected:
                    print(f"  REJECTED: {path}")
                print("brain/ did not validate cleanly under strict ingest - aborting")
                return 1

            computed = session.execute(select(Evidence).where(Evidence.tag_kind == TagKind.computed)).scalars().all()
            resolved = [e for e in computed if e.resolved]
            print(f"(computed, ...) evidence tags: {len(computed)} found, {len(resolved)} resolved to a derivation")
            if computed and not resolved:
                print("no (computed, ...) tag resolved - the money-informs-decisions direction is broken")
                return 1

            _step("3. decided_effects -> revenue_override -> rerun the plan")
            effects = decided_effects(session)
            print(f"decided decisions carrying a quantified effect: {len(effects)}")
            for eff in effects:
                print(f"  {eff.decision_slug}: {eff.category_code} {eff.year} = {eff.value:,.1f} {eff.unit}")

            try:
                plan = revenue_override(effects)
            except ImportError as exc:
                print(f"cannot build the revenue override yet: {exc}")
                return 1

            override = plan.overrides
            if not override:
                print(f"no {WIRED_CATEGORY!r} decided effect found - nothing to override; aborting")
                return 1
            print(f"revenue override built for year(s): {sorted(override)}")
            if plan.shadowed:
                print(f"shadowed (superseded by a newer decision on the same year) claim ids: {plan.shadowed}")

            year = sorted(override)[0]
            before_rev = find_plan_value(session, scenario_kind="base", category_code="REV", year=year)
            before_pers = find_plan_value(session, scenario_kind="base", category_code="PERS", year=year)
            before_rev_value, before_pers_value = before_rev.value, before_pers.value

            try:
                after_run = run_plan(
                    session, created_by="bridge-check", revenue_override=override, label_suffix=" +decision"
                )
            except Exception as exc:  # noqa: BLE001 - this IS the legible-failure path
                print(f"rerunning the plan with the decision's revenue override failed: {exc!r}")
                return 1

            _step("4. before / after")
            after_rev = find_plan_value(
                session, scenario_kind="base", category_code="REV", year=year, scenario_id=after_run.scenario_ids["base"]
            )
            after_pers = find_plan_value(
                session, scenario_kind="base", category_code="PERS", year=year, scenario_id=after_run.scenario_ids["base"]
            )
            print(f"year {year}  revenue   before={before_rev_value:,.1f} kEUR  after={after_rev.value:,.1f} kEUR")
            print(f"year {year}  personnel before={before_pers_value:,.1f} kEUR  after={after_pers.value:,.1f} kEUR")

            pers_cat = session.execute(select(Category).where(Category.code == "PERS")).scalar_one()
            pers_param_id = after_run.parameter_ids.get("PERS")
            beta = None
            if pers_param_id is not None:
                param = session.get(Parameter, pers_param_id)
                if param is not None and param.category_id == pers_cat.id:
                    beta = param.beta
            if beta is None:
                print("could not find the PERS regression parameter for the rerun - cannot check the cascade")
                return 1

            revenue_delta = after_rev.value - before_rev_value
            personnel_delta = after_pers.value - before_pers_value
            expected_personnel_delta = beta * revenue_delta
            print(f"beta_PERS={beta:.6f}  revenue_delta={revenue_delta:,.4f}  personnel_delta={personnel_delta:,.4f}")
            print(f"expected personnel_delta (beta * revenue_delta) = {expected_personnel_delta:,.4f}")
            if abs(personnel_delta - expected_personnel_delta) > 1e-6:
                print("ASSERTION FAILED: personnel delta does not equal beta * revenue delta")
                return 1
            print("OK: personnel delta == beta * revenue delta")

            _step("5. trace of the affected personnel plan value")
            tree = trace_plan_value(session, after_pers.id)
            print(render_trace(tree))

    print("\nbridge check passed: both directions demonstrated end to end.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

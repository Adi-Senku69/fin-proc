"""The end-to-end demo runs offline with the fakes and tells the whole story."""

from __future__ import annotations

import time

from sqlalchemy.orm import sessionmaker

from nvplan.api.demo import main
from nvplan.api.queries import table_counts
from nvplan.db.session import get_engine


def test_demo_runs_with_fakes(tmp_path, capsys):
    db = tmp_path / "demo.db"
    t0 = time.perf_counter()
    assert main(db_path=str(db), use_fake_ai=True) == 0
    elapsed = time.perf_counter() - t0
    out = capsys.readouterr().out
    assert db.exists()
    with sessionmaker(bind=get_engine(f"sqlite:///{db}"))() as s:
        counts = table_counts(s)
    assert "ILLUSTRATIVE" in out and "SCRIPTED FAKE" in out
    assert "Personnel costs · 2028 · Base" in out and "## Backtest" in out
    assert "confirmed_by=C. Andres" in out and "prompt (verbatim)" in out
    assert "consistency: OK" in out and "deviation explanation -> ai_record" in out
    # three plan runs (first, confirmed rerun, backtest fit) x three scenarios; three ai records
    assert counts["scenario"] == 9 and counts["ai_record"] == 3 and counts["parameter"] == 12
    assert counts["actual"] == 60 and counts["external_note"] >= 4
    assert elapsed < 30, f"demo took {elapsed:.1f}s"

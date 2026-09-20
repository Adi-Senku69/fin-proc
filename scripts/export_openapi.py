"""Write `apps/web/openapi.json` — the contract gate's single definition of "current".

The React frontend (`apps/web/`) never hand-writes a type for anything the API
returns. Instead: this script dumps the FastAPI app's own `openapi()` schema,
`openapi-typescript` turns that JSON into `apps/web/src/api/schema.d.ts`, and CI
regenerates both and fails the build if either has drifted from what is
committed. A change to `nvplan/api/schemas.py` or a new route in
`nvplan/api/app.py` therefore either updates this file (and the generated
types with it) or breaks CI — it can never silently drift out from under the
frontend.

This module does not import anything from `apps/web` and is not itself a
frontend file; it is the Python-side half of the gate, kept here (not inside
`apps/web/`) because it only ever touches the backend app object.

`sort_keys=True` because FastAPI's natural key order is insertion order, which
reshuffles every time a route or model is added elsewhere in the file — a
sorted diff shows only what actually changed.

Usage:
    uv run python scripts/export_openapi.py          # (re)write apps/web/openapi.json
    uv run python scripts/export_openapi.py --check   # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "apps" / "web" / "openapi.json"


def render() -> str:
    """Build the schema from the app itself, against a throwaway database.

    Importing `create_app` and calling it is the whole point: this is not a
    hand-maintained document, it is FastAPI's own introspection of the real
    routes and Pydantic models in `nvplan/api/app.py` / `nvplan/api/schemas.py`.
    A fresh, temporary sqlite file is used so exporting the schema has no side
    effect on a developer's own `nvplan_demo.db` (`create_app` opens and seeds
    whatever database URL it is given).
    """
    from nvplan.api.app import create_app

    scratch = pathlib.Path(tempfile.mkdtemp()) / "openapi-export.db"
    app = create_app(db_url=f"sqlite:///{scratch}")
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if apps/web/openapi.json is stale, instead of rewriting it.",
    )
    args = parser.parse_args(argv)

    current = render()

    if not args.check:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(current)
        print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
        return 0

    committed = TARGET.read_text() if TARGET.exists() else ""
    if committed == current:
        print(f"{TARGET.relative_to(REPO_ROOT)} is current")
        return 0

    print(
        f"{TARGET.relative_to(REPO_ROOT)} is stale relative to the FastAPI app.\n"
        "Run `uv run python scripts/export_openapi.py` (and, in apps/web, `npm run gen:api`) "
        "and commit both files.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

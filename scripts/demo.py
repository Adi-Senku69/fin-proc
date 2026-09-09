"""Thin wrapper: ``uv run python scripts/demo.py [--db FILE] [--fake-ai]`` == ``uv run nvplan-demo``."""

from nvplan.api.demo import main

if __name__ == "__main__":
    raise SystemExit(main())

"""FastAPI layer and end-to-end demo (PLAN.md phase 5).

* :mod:`nvplan.api.app`     - ``create_app`` / ``serve``: JSON routes that expose the
  plan grid, click-through traces, scenarios, statements, the prompt viewer, the
  confirmation gate, the three AI touchpoints, the backtest and the deviation.
* :mod:`nvplan.api.queries` - read-only helpers over a Session shared by the API and
  the demo (grids, backtest report, deviation, default revenue lookup, note seeding).
* :mod:`nvplan.api.demo`    - ``main``: the "story told live" on the illustrative data.
"""

"""Services layer: the ONLY code that writes planned numbers into the database.

* :mod:`nvplan.services.planning` - ``run_plan``: core -> derivation / parameter /
  scenario / plan_value / statement_line rows, one transaction, append-only.
* :mod:`nvplan.services.trace`    - lineage tree for any plan_value / statement_line.
* :mod:`nvplan.services.gate`     - human confirmation gate for AI proposals.
"""

from nvplan.services.gate import confirm_proposal, reject_proposal
from nvplan.services.planning import PlanRun, run_plan
from nvplan.services.trace import (
    TraceNode,
    find_plan_value,
    render_trace,
    trace_derivation,
    trace_plan_value,
    trace_statement_line,
)

__all__ = [
    "PlanRun",
    "run_plan",
    "TraceNode",
    "find_plan_value",
    "render_trace",
    "trace_derivation",
    "trace_plan_value",
    "trace_statement_line",
    "confirm_proposal",
    "reject_proposal",
]

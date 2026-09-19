"""bridge — the P2 join between the finance engine and the decision brain (PLATFORM.md §7, §7.1).

``bridge`` is the **only** package allowed to import both ``nvplan`` and ``brainkit`` /
``provenance``. Everything else in the platform stays decoupled: ``nvplan`` never imports
``brainkit``/``provenance``, and ``brainkit``/``provenance`` never import ``nvplan`` services
(``brainkit.validate`` reads only the plain constants in ``nvplan.config``, never a service).

Both directions of the bridge live here:

- **Decision drives money** (``bridge.effects``): a ``decided`` decision's ``## Quantified
  effect`` block, once indexed as ``Claim.effect_json``, becomes the revenue override that
  ``nvplan.services.planning.run_plan`` already accepts.
- **Money informs decisions** (``bridge.lookup``): a decision's ``(computed, <key>)``
  evidence tag resolves to a real ``derivation`` row via a lookup built here.

``bridge.db.init_platform_db`` puts both sides' tables in one SQLite database so a single
``Session`` can read plan values and claims together; ``bridge.check`` is the executable
proof that both directions actually work end to end.
"""

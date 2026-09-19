"""deepagents skills: SKILL.md layout, the skill index in the system prompt, and the
scripted read_file of a skill through the composite backend's /skills/ route."""

from __future__ import annotations

import re

from sqlalchemy import select

from nvplan.ai import build_advisor, build_touchpoint_agent, run_deviation_explanation, run_env_scan, run_revenue_proposal
from nvplan.ai.agents import TOUCHPOINTS, PromptCapture, touchpoint_subagents
from nvplan.ai.context import (
    SKILL_NAMES,
    SKILLS_DIR,
    SKILLS_SOURCE,
    make_backend,
    make_shared_touchpoint_backend,
    skills_for_surface,
    skills_index_route,
)
from nvplan.ai.fake import (
    SKILL_PATHS,
    FakeToolCallingModel,
    scripted_deviation_model,
    scripted_env_scan_model,
    scripted_revenue_model,
)
from nvplan.ai.prompts import TOUCHPOINT_SYSTEM_PROMPTS
from nvplan.ai.tools import AiRunContext
from nvplan.db.models import AiRecord, ExternalNote
from test_ai_fixtures import PLAN_YEAR, ai_db  # noqa: F401 (fixture)

TOUCHPOINT_SKILL = {
    "env_scan": "env-scan-54-positions",
    "revenue_proposal": "revenue-proposal-method",
    "deviation_explanation": "deviation-explanation-method",
}


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    return dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)


def test_skill_files_have_valid_frontmatter():
    dirs = sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir())
    assert dirs == sorted(SKILL_NAMES)
    for name in SKILL_NAMES:
        text = (SKILLS_DIR / name / "SKILL.md").read_text()
        fm = _frontmatter(text)
        assert fm["name"].strip() == name and re.fullmatch(r"[a-z0-9-]{1,64}", name)
        assert 1 <= len(fm["description"].strip()) <= 1024
    assert "materiality" in (SKILLS_DIR / "env-scan-54-positions" / "SKILL.md").read_text().lower()
    assert "worked example" in (SKILLS_DIR / "revenue-proposal-method" / "SKILL.md").read_text().lower()
    assert "residual" in (SKILLS_DIR / "deviation-explanation-method" / "SKILL.md").read_text().lower()


def test_system_prompts_point_to_their_skill_and_hold_no_procedure():
    for tp, skill in TOUCHPOINT_SKILL.items():
        sp = TOUCHPOINT_SYSTEM_PROMPTS[tp]
        assert f"/skills/{skill}/SKILL.md" in sp and "read_file" in sp
        assert "Ground rules" in sp and "k EUR" in sp
    # the procedure text moved out (it lives in the SKILL.md files)
    assert "one call per finding" not in TOUCHPOINT_SYSTEM_PROMPTS["env_scan"]
    assert "21 900" not in TOUCHPOINT_SYSTEM_PROMPTS["revenue_proposal"]
    assert "split the deviation into" not in TOUCHPOINT_SYSTEM_PROMPTS["deviation_explanation"]
    for tp, skill in TOUCHPOINT_SKILL.items():
        assert f"name: {skill}" in (SKILLS_DIR / skill / "SKILL.md").read_text()


def test_composite_backend_serves_skills_from_repo():
    backend = make_backend()
    listing = backend.ls(SKILLS_SOURCE)
    paths = {e["path"] for e in listing.entries}
    assert {f"/skills/{n}" for n in SKILL_NAMES} <= {p.rstrip("/") for p in paths}
    got = backend.download_files([f"/skills/{n}/SKILL.md" for n in SKILL_NAMES])
    for n, resp in zip(SKILL_NAMES, got):
        assert resp.error is None and f"name: {n}" in resp.content.decode()
    # everything else is in-memory state: no disk write path exists for the model
    assert type(backend.default).__name__ == "StateBackend"


def test_skill_index_in_system_prompt_and_read_file_returns_skill(ai_db):
    factory, _ = ai_db
    ctx = AiRunContext(year=2027, default_value=22_000.0, model_version="fake")
    with factory() as s:
        note_id = s.scalar(select(ExternalNote.id).order_by(ExternalNote.id))
    model = scripted_revenue_model(year=2027, proposed_value=21_000.0, rationale="r", cited_note_ids=[note_id])
    agent = build_touchpoint_agent("revenue_proposal", model=model, session_factory=factory, extra_context=ctx)
    capture = PromptCapture()
    result = agent.invoke({"messages": [{"role": "user", "content": "propose 2027"}]}, config={"callbacks": [capture]})

    assert "Skills System" in capture.system_prompt
    # scoped to revenue_proposal's own skill (nvplan.ai.context.SKILL_SURFACE) - not the other nine
    assert "**revenue-proposal-method**" in capture.system_prompt
    assert "/skills/revenue-proposal-method/SKILL.md" in capture.system_prompt
    for name in SKILL_NAMES:
        if name != "revenue-proposal-method":
            assert f"**{name}**" not in capture.system_prompt
    assert "skills_metadata" not in result  # private state key: loaded by SkillsMiddleware, not returned

    skill_msg = next(m for m in result["messages"] if m.type == "tool" and m.tool_call_id == "skill-revenue_proposal")
    expected = (SKILLS_DIR / "revenue-proposal-method" / "SKILL.md").read_text()
    assert "name: revenue-proposal-method" in skill_msg.content
    assert "21 900 * (1 - 0.025)" in skill_msg.content  # the worked example came back
    assert "Control-table rules" in skill_msg.content
    # read_file renders numbered lines; every source line is present
    for line in expected.splitlines()[:20]:
        if line.strip():
            assert line in skill_msg.content


def test_each_touchpoint_reads_its_skill_and_prompt_text_lists_skills(ai_db):
    factory, _ = ai_db
    with factory() as s:
        note_id = s.scalar(select(ExternalNote.id).order_by(ExternalNote.id))
    recs = {
        "env_scan": run_env_scan(factory, model=scripted_env_scan_model(), positions_subset=["D2"]),
        "revenue_proposal": run_revenue_proposal(
            factory, scenario_kind="base", year=2027, default_value=22_000.0,
            model=scripted_revenue_model(year=2027, proposed_value=21_000.0, rationale="r", cited_note_ids=[note_id]),
        ),
        "deviation_explanation": run_deviation_explanation(
            factory, scenario_kind="base", year=PLAN_YEAR,
            model=scripted_deviation_model(scenario_kind="base", year=PLAN_YEAR, summary="s", contributions=[]),
        ),
    }
    with factory() as s:
        for tp, rec in recs.items():
            r = s.get(AiRecord, rec.id)
            assert "Skills System" in r.prompt_text
            # scoped to this touchpoint's own skill (SKILL_SURFACE) - not the other nine
            own_skill = TOUCHPOINT_SKILL[tp]
            assert f"**{own_skill}**" in r.prompt_text
            for name in SKILL_NAMES:
                if name != own_skill:
                    assert f"**{name}**" not in r.prompt_text
            assert TOUCHPOINT_SYSTEM_PROMPTS[tp] in r.prompt_text
            # first model call asked for the skill; second call saw its content
            log = r.call_log_json
            assert log[0]["response"]["tool_calls"] == ["read_file"]
            skill_entry = next(m for m in log[1]["request"]["messages"] if m["role"] == "tool")
            assert skill_entry["tool_name"] == "read_file"
            assert f"name: {TOUCHPOINT_SKILL[tp]}" in skill_entry["content_excerpt"]
            assert SKILL_PATHS[tp] == f"/skills/{TOUCHPOINT_SKILL[tp]}/SKILL.md"


def test_advisor_subagents_carry_skills(ai_db):
    factory, _ = ai_db
    fake = FakeToolCallingModel(responses=[])
    backend = make_shared_touchpoint_backend(TOUCHPOINTS)
    specs = touchpoint_subagents(factory, AiRunContext(), backend=backend, model=fake)
    # each subagent's own scoped index route, not the shared whole-directory SKILLS_SOURCE
    assert [s["skills"] for s in specs] == [[skills_index_route(tp)] for tp in TOUCHPOINTS]
    assert all("ContextAuditMiddleware" in {m.name for m in s["middleware"]} for s in specs)
    # each route on the shared backend lists only that touchpoint's own skill
    for tp in TOUCHPOINTS:
        listing = backend.ls(skills_index_route(tp))
        assert {e["path"].split("/")[-2] for e in listing.entries} == set(skills_for_surface(tp))
    # the canonical, unscoped route is still there and unfiltered - every touchpoint's authored
    # system prompt hardcodes a literal read_file("/skills/<skill>/SKILL.md") that has to keep
    # resolving no matter which subagent is running (nvplan.ai.prompts)
    assert {e["path"].rstrip("/").rsplit("/", 1)[-1] for e in backend.ls(SKILLS_SOURCE).entries} == set(SKILL_NAMES)
    advisor = build_advisor(fake, factory)
    task = advisor.nodes["tools"].bound.tools_by_name["task"]
    assert "- revenue-proposal:" in task.description

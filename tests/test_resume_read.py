"""Successor resume (teeth package, 2026-11-10 plan).

On a kanban run's first turn, when an earlier run of the same task left
a checkpoint file, a bounded excerpt is injected so the successor
continues instead of restarting. Flag-gated, kanban only, first turn
only — and with no prior checkpoint it injects nothing (adversarial d).
"""

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core

PRIOR = (
    "agent-checkpoint: budget checkpoint persisted mid-run "
    "(source: watchdog).\n\nPRIOR RUN STATE: step 3 of 5 done. "
    "Next step: wire the adapter."
)


def patch_section(monkey, section):
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def _section(tmp_path, **over):
    section = {
        "checkpoint_dir": str(tmp_path / "ck") + "/",
        "watchdog": False,
        "teeth": {"resume_read": True},
    }
    section.update(over)
    return section


def _write_prior(tmp_path, task_id="t_resume", content=PRIOR):
    ck = tmp_path / "ck"
    ck.mkdir(parents=True, exist_ok=True)
    (ck / f"{task_id}.md").write_text(content, encoding="utf-8")


def test_resume_injects_prior_checkpoint_on_first_turn(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    _write_prior(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_resume")
    out = ac.on_pre_llm_call(session_id="s1", task_id="t_resume", is_first_turn=True)
    assert out is not None
    ctx = out["context"]
    assert "[agent-checkpoint resume]" in ctx
    assert "t_resume" in ctx
    assert "before trusting it" in ctx  # verify-before-trusting line
    assert "step 3 of 5 done" in ctx


def test_resume_only_on_first_turn(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    _write_prior(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_resume")
    first = ac.on_pre_llm_call(session_id="s1", task_id="t_resume", is_first_turn=True)
    assert first is not None
    second = ac.on_pre_llm_call(
        session_id="s1", task_id="t_resume", is_first_turn=False
    )
    assert second is None


def test_resume_no_prior_checkpoint_injects_nothing(stubs, monkeypatch, tmp_path):
    """Adversarial (d): resume flag on, no checkpoint file on disk →
    the first turn gets nothing."""
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_resume")
    out = ac.on_pre_llm_call(session_id="s1", task_id="t_resume", is_first_turn=True)
    assert out is None


def test_resume_flag_off_injects_nothing(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    section = _section(tmp_path)
    del section["teeth"]
    patch_section(monkeypatch, section)
    _write_prior(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_resume")
    out = ac.on_pre_llm_call(session_id="s1", task_id="t_resume", is_first_turn=True)
    assert out is None


def test_resume_excerpt_is_bounded(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    _write_prior(tmp_path, content="A" * 5000 + "TAILMARKER")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_resume")
    out = ac.on_pre_llm_call(session_id="s1", task_id="t_resume", is_first_turn=True)
    assert out is not None
    assert "TAILMARKER" not in out["context"]
    # Header + at most RESUME_EXCERPT_MAX_CHARS of excerpt.
    assert len(out["context"]) <= ac.RESUME_EXCERPT_MAX_CHARS + 400


def test_resume_chat_dialect_never(stubs, monkeypatch, tmp_path):
    """No kanban env → chat dialect → no resume, even when a checkpoint
    file exists under the session's run id."""
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    _write_prior(tmp_path, task_id="sess-chat")
    out = ac.on_pre_llm_call(session_id="sess-chat", task_id="", is_first_turn=True)
    assert out is None

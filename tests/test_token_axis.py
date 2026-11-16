"""Token-budget axis (0.6.0): a third budget axis in ``enabled_axes``.

When the host exposes the run's token usage and a token budget is
configured (``caps.tokens``), checkpoint tiers evaluate on token
fraction exactly as on the time axis — tier 1 at the effective 50%,
tier 2 at 80%. When usage data is absent the axis is inert: no
notices, no errors, no invented numbers. The axis is active only when
listed in ``enabled_axes``; the default axes stay [time, turns].

Hermetic seams follow the existing suite: pure tier math, fixed config
sections, env-exposed usage, a real sqlite board in tmp_path.
"""

import os
import sqlite3

import pytest

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core


def patch_section(monkey, section):
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def caps(turns=None, tsec=None, tokens=None, dialect="kanban"):
    return ac.RunCaps(
        turns_cap=turns, time_cap=tsec, dialect=dialect, tokens_cap=tokens
    )


TOKEN_CFG = ac.PluginConfig(enabled_axes=("time", "turns", "tokens"))


# ---------------------------------------------------------------------------
# Tier math: fires at 50% / 80% with usage present
# ---------------------------------------------------------------------------


def test_tier1_at_50pct_token_axis():
    plan = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[],
        tokens_used=50_000,
    )
    assert plan is not None
    assert plan.fire_tiers == [0]
    assert plan.effective_fraction == pytest.approx(0.5)
    assert plan.tokens_used == 50_000
    assert plan.tokens_cap == 100_000
    assert plan.tokens_left == 50_000


def test_tier2_at_80pct_token_axis():
    plan = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[0],
        tokens_used=80_000,
    )
    assert plan is not None
    assert plan.fire_tiers == [1]


def test_below_50pct_no_tier():
    plan = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[],
        tokens_used=49_000,
    )
    assert plan is not None
    assert plan.fire_tiers == []


def test_token_axis_drives_effective_fraction():
    """Time barely used, tokens at 60%: tier 1 fires on the token
    fraction — the effective fraction is the max across axes."""
    plan = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(turns=20, tsec=3600, tokens=100_000),
        turns_used=1,
        elapsed=60.0,
        fired_tiers=[],
        tokens_used=60_000,
    )
    assert plan is not None
    assert plan.fire_tiers == [0]
    assert plan.effective_fraction == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Inert without usage data / without the axis enabled
# ---------------------------------------------------------------------------


def test_inert_when_usage_absent():
    """Budget configured, axis enabled, but the host exposed no usage:
    the axis does not exist — no plan from tokens alone."""
    plan = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[],
        tokens_used=None,
    )
    assert plan is None


def test_inert_when_axis_not_enabled():
    """Default axes (time, turns): a token budget + usage change
    nothing — behavior identical to 0.5.0."""
    plan = ac.compute_tier_plan(
        ac.PluginConfig(),
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[],
        tokens_used=95_000,
    )
    assert plan is None


def test_inert_default_config_no_token_fields():
    plan = ac.compute_tier_plan(
        ac.PluginConfig(),
        caps(turns=20, tsec=3600),
        turns_used=10,
        elapsed=60.0,
        fired_tiers=[],
    )
    assert plan is not None
    assert plan.tokens_used is None
    assert plan.tokens_cap is None


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_config_parses_token_axis_and_budget(monkeypatch):
    patch_section(
        monkeypatch,
        {
            "enabled_axes": ["time", "turns", "tokens"],
            "caps": {"tokens": 250_000},
        },
    )
    cfg = ac.load_plugin_config()
    assert cfg.enabled_axes == ("time", "turns", "tokens")
    assert cfg.caps.tokens == 250_000


def test_config_defaults_have_no_token_axis(monkeypatch):
    patch_section(monkeypatch, {})
    cfg = ac.load_plugin_config()
    assert cfg.enabled_axes == ("time", "turns")
    assert cfg.caps.tokens is None


def test_config_tokens_only_axis_list(monkeypatch):
    patch_section(monkeypatch, {"enabled_axes": ["tokens"]})
    cfg = ac.load_plugin_config()
    assert cfg.enabled_axes == ("tokens",)


# ---------------------------------------------------------------------------
# Usage discovery seam
# ---------------------------------------------------------------------------


def test_tokens_used_from_env(monkeypatch):
    monkeypatch.setenv("HERMES_RUN_TOKENS_USED", "42000")
    assert ac._tokens_used("s1", caps(dialect="chat")) == 42000


def test_tokens_used_absent_is_none(monkeypatch):
    monkeypatch.delenv("HERMES_RUN_TOKENS_USED", raising=False)
    assert ac._tokens_used("s1", caps(dialect="chat")) is None


def test_tokens_used_from_kanban_task_runs(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_RUN_TOKENS_USED", raising=False)
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE task_runs (id INTEGER, task_id TEXT, tokens_used INTEGER)"
    )
    con.executemany(
        "INSERT INTO task_runs VALUES (?, ?, ?)",
        [(1, "t_tok", 111), (2, "t_tok", 222), (3, "t_other", 999)],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_tok")
    # Latest run row for THIS task wins.
    assert ac._tokens_used("s1", caps(dialect="kanban")) == 222


def test_tokens_used_no_token_column_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_RUN_TOKENS_USED", raising=False)
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id INTEGER, task_id TEXT)")
    con.execute("INSERT INTO task_runs VALUES (1, 't_tok')")
    con.commit()
    con.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_tok")
    assert ac._tokens_used("s1", caps(dialect="kanban")) is None


# ---------------------------------------------------------------------------
# Rendering: the notice names the token axis in the time-axis shape
# ---------------------------------------------------------------------------


def test_render_names_tokens():
    plan = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[],
        tokens_used=50_000,
    )
    text = ac.render_checkpoint_text(TOKEN_CFG, plan, 0, "/ck/t_tok.md")
    assert text is not None
    assert "50000 of 100000 tokens (50000 left)" in text
    plan2 = ac.compute_tier_plan(
        TOKEN_CFG,
        caps(tokens=100_000),
        turns_used=0,
        elapsed=0.0,
        fired_tiers=[0],
        tokens_used=85_000,
    )
    text2 = ac.render_checkpoint_text(TOKEN_CFG, plan2, 1, "/ck/t_tok.md")
    assert text2 is not None
    assert "15000 tokens remain" in text2


# ---------------------------------------------------------------------------
# Hook-level integration: fires end-to-end with usage, inert without
# ---------------------------------------------------------------------------


def _board(tmp_path, task_id="t_tok"):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL, body TEXT)")
    con.execute("INSERT INTO tasks VALUES (?, ?, ?)", (task_id, None, "Do the work."))
    con.commit()
    con.close()
    return str(db)


def _token_section(tmp_path):
    return {
        "checkpoint_dir": str(tmp_path / "ck") + "/",
        "allow_tiny_tier1": True,
        "watchdog": False,
        "enabled_axes": ["tokens"],
        "caps": {"tokens": 1000},
    }


def test_hook_fires_tier1_with_usage(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, _token_section(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", _board(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_tok")
    monkeypatch.setenv("HERMES_RUN_TOKENS_USED", "600")
    ac.on_pre_llm_call(session_id="s1", task_id="t_tok", is_first_turn=True)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_tok")
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_tok"))
    assert 0 in st["persisted"]
    ck = tmp_path / "ck" / "t_tok.md"
    assert ck.exists()
    assert "tokens" in ck.read_text()


def test_hook_inert_without_usage(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, _token_section(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", _board(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_tok")
    monkeypatch.delenv("HERMES_RUN_TOKENS_USED", raising=False)
    ac.on_pre_llm_call(session_id="s1", task_id="t_tok", is_first_turn=True)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_tok")
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_tok"))
    assert st["persisted"] == []
    assert not (tmp_path / "ck" / "t_tok.md").exists()
    # The capless fallback must not claim this run either: it has a
    # token budget, so it is not capless — nothing fires, silently.
    assert st["uncapped_fires"] == 0
    assert os.environ.get("HERMES_KANBAN_TASK") == "t_tok"

"""End-to-end hook behavior: latching through the hook, fail-open paths,
per-session isolation, and the section 6 dialect branches."""
import re

import pytest

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core  # real module (shim re-exports)

TIME_FIGURE = re.compile(r"\d+h\d+m|\d+m\d+s|\b\d+[hms]\b")


def call(first, sid="s1", tid="", **kw):
    """Fire one pre_llm_call (is_first_turn=first)."""
    return ac.on_pre_llm_call(
        session_id=sid, task_id=tid, is_first_turn=first, **kw)


def begin(sid="s1", tid=""):
    return call(True, sid, tid)


def patch_section(monkey, section):
    """Point the plugin's config reader at a fixed section (hermetic)."""
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def advance(sid, tid, n, **kw):
    """Fire n non-first calls; return the last result."""
    r = None
    for _ in range(n):
        r = call(False, sid, tid, **kw)
    return r


# ---------------------------------------------------------------------------
# Latching once per tier, through the hook
# ---------------------------------------------------------------------------

def test_hook_fires_tier1_then_tier2_each_once(stubs, monkeypatch):
    """A kanban run fires tier 1 at 50% of turns and tier 2 at 90% — each
    exactly once across the run; the checkpoint path is named in tier 1."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")  # time axis: 5430s
    stubs["config"] = {}  # hermetic profile: no agent.max_turns
    patch_section(monkeypatch, {
        "caps": {"turns": 20},
        "checkpoint_dir": "/tmp/agent-checkpoints-test/",
    })

    begin(tid="t_test")  # turns=0
    # 9 more → turns=9 (0.45): nothing due.
    assert advance("s1", "t_test", 9) is None
    # 10th non-first → turns=10 (0.5): tier 1.
    r = call(False, tid="t_test")
    assert r is not None
    assert "write everything found so far" in r["context"]
    # B1: run-id is the stable board task id (HERMES_KANBAN_TASK) or the
    # session id — never the hook's task_id kwarg (a per-turn uuid in the
    # goal loop). This chat-dialect test has no kanban env, so the stable
    # session id "s1" names the file.
    assert "/tmp/agent-checkpoints-test/s1.md" in r["context"]
    # One more turn: tier 1 latched, tier 2 not yet → silent.
    assert call(False, tid="t_test") is None
    # Advance to turns=18 (0.9): tier 2.
    r = advance("s1", "t_test", 7)
    assert r is not None
    assert r["context"].count("Final budget checkpoint") == 1
    # Final turn: nothing new (both latched).
    assert call(False, tid="t_test") is None


def test_hook_latches_reset_on_new_run(stubs, monkeypatch):
    """A new run (is_first_turn) resets latches: tier 1 fires again."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")
    stubs["config"] = {}
    patch_section(monkeypatch, {"caps": {"turns": 20}})

    begin(tid="t_reset")
    advance("s1", "t_reset", 9)
    r = call(False, tid="t_reset")  # turns=10 → tier 1
    assert r is not None and "write everything" in r["context"]

    # Restart the run → fresh latches; tier 1 fires again at turn 10.
    begin(tid="t_reset")
    advance("s1", "t_reset", 9)
    r = call(False, tid="t_reset")
    assert r is not None and "write everything" in r["context"]


def test_hook_registered_mid_run_still_counts(stubs, monkeypatch):
    """Hook registered mid-run: first non-first fire begins the run and
    works (conservative elapsed) instead of crashing."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")
    stubs["config"] = {}
    patch_section(monkeypatch, {"caps": {"turns": 20}})
    r = ac.on_pre_llm_call(session_id="s1", task_id="t", is_first_turn=False)
    assert r is None  # turn 1 of 20 — nothing due, no crash


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

def test_fail_open_malformed_config(stubs):
    """Config loader throws → defaults → no caps → silent no-op, no crash."""
    stubs["on"] = True

    def boom():
        raise ValueError("config exploded")

    import hermes_cli
    hermes_cli.config.load_config_readonly = boom
    assert ac.on_pre_llm_call(session_id="s1", task_id="",
                              is_first_turn=True) is None
    assert ac.on_pre_llm_call(session_id="s1", task_id="",
                              is_first_turn=False) is None


def test_fail_open_missing_db(stubs, tmp_path, monkeypatch):
    """Kanban env + a board DB that doesn't exist (and no TERMINAL_TIMEOUT
    derivation) → fail-open no-op, no exception."""
    stubs["on"] = True
    stubs["db_path"] = str(tmp_path / "nope.db")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing")
    assert ac.on_pre_llm_call(session_id="s1", task_id="t_missing",
                              is_first_turn=True) is None
    assert ac.on_pre_llm_call(session_id="s1", task_id="t_missing",
                              is_first_turn=False) is None


def test_fail_open_corrupt_db(stubs, tmp_path, monkeypatch):
    """Kanban env + a DB file that is not a SQLite DB → fail-open no-op."""
    stubs["on"] = True
    bad = tmp_path / "bad.db"
    bad.write_text("this is not sqlite")
    stubs["db_path"] = str(bad)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_bad")
    assert ac.on_pre_llm_call(session_id="s1", task_id="t_bad",
                              is_first_turn=True) is None


def test_fail_open_config_section_wrong_type(stubs):
    """agent_checkpoint section that isn't a dict → defaults, no crash."""
    stubs["on"] = True
    import hermes_cli
    hermes_cli.config.load_config_readonly = lambda: {"agent_checkpoint": "nope"}
    assert ac.on_pre_llm_call(session_id="s1", task_id="",
                              is_first_turn=True) is None


# ---------------------------------------------------------------------------
# Per-session isolation (concurrent sessions in one process)
# ---------------------------------------------------------------------------

def test_per_session_isolation(stubs, monkeypatch):
    """Two concurrent sessions: A's fired tier must not leak into B, and
    each counts its own turns."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")
    stubs["config"] = {}
    patch_section(monkeypatch, {"caps": {"turns": 20}})

    begin(sid="A", tid="ta")
    begin(sid="B", tid="tb")

    # Drive A to its tier 1 (turns=10).
    advance("A", "ta", 9)
    rA = call(False, sid="A", tid="ta")
    assert rA is not None and "write everything" in rA["context"]

    # B at 8 consumed → 0.4 → silent.
    advance("B", "tb", 8)
    # B's 9th non-first: turns=9 → 0.45 → still silent (A's latch can't
    # fire for B, and B hasn't reached its own 50%).
    assert call(False, sid="B", tid="tb") is None
    # B's 10th: ITS OWN tier 1 fires.
    rB = call(False, sid="B", tid="tb")
    assert rB is not None and "write everything" in rB["context"]
    # A (turns=12) does not re-fire its latched tier 1.
    assert call(False, sid="A", tid="ta") is None


def test_latches_bounded():
    """The latch dict evicts under pressure instead of growing unbounded."""
    for i in range(ac.MAX_LATCHED_SESSIONS + 10):
        ac.LATCHES.begin_run(f"s{i}")
    assert len(ac.LATCHES.snapshot()) <= ac.MAX_LATCHED_SESSIONS


# ---------------------------------------------------------------------------
# Section 6 dialect branches
# ---------------------------------------------------------------------------

def test_goal_dialect_turns_only_rendering(stubs):
    """Goal session: turn axis only. The injected text states the turns and
    contains NO time figures (goal has no in-process wall-clock budget)."""
    import types
    state = types.SimpleNamespace(turns_used=10, max_turns=20)
    stubs["on"] = True
    stubs["goals"]["g1"] = state

    ac.on_pre_llm_call(session_id="g1", task_id="", is_first_turn=True)
    advance("g1", "", 9)
    r = ac.on_pre_llm_call(session_id="g1", task_id="", is_first_turn=False)
    assert r is not None
    ctx = r["context"]
    assert "10 of 20 turns" in ctx
    assert "10 left" in ctx
    assert not TIME_FIGURE.search(ctx)  # no h/m/s figures anywhere
    assert "write everything found so far" in ctx
    assert "g1.md" in ctx  # run-id = session id when no task id


def test_chat_noop_when_unarmed(stubs):
    """Chat with nothing armed (no run_budget_seconds, no caps) → silent
    no-op. It never warns against a budget that doesn't exist."""
    stubs["on"] = True
    stubs["config"] = {"agent": {}}
    assert ac.on_pre_llm_call(session_id="c1", task_id="",
                              is_first_turn=True) is None
    assert ac.on_pre_llm_call(session_id="c1", task_id="",
                              is_first_turn=False) is None


def test_chat_fires_when_run_budget_armed(stubs, monkeypatch):
    """Chat with agent.run_budget_seconds armed → the plugin runs its axes
    against it and fires tier 1 at 50% of the time budget.

    The budget is a real 1h cap and the elapsed clock is rewound
    (deterministic) instead of slept: a 2s budget would be a sub-5-minute
    *tiny cap*, which the plan's collapse rule deliberately suppresses, so
    a sleep-against-tiny-budget test would be asserting the wrong behavior.
    """
    stubs["on"] = True
    stubs["config"] = {"agent": {"run_budget_seconds": 3600.0}}
    patch_section(monkeypatch, {})  # no plugin caps

    ac.on_pre_llm_call(session_id="c2", task_id="", is_first_turn=True)
    monkeypatch.setattr(
        ac.LATCHES, "elapsed", lambda _key: 2000.0)  # ~55% of 3600s
    r = ac.on_pre_llm_call(session_id="c2", task_id="", is_first_turn=False)
    assert r is not None
    assert "about half your budget remains" in r["context"]


def test_chat_noop_with_profile_max_turns_only(stubs):
    """Chat + profile max_turns but no time cap: the profile cap is NOT a
    chat arming source (plan §6 — chat needs run_budget_seconds or a
    configured cap) → silent no-op."""
    stubs["on"] = True
    stubs["config"] = {"agent": {"max_turns": 20}}
    assert ac.on_pre_llm_call(session_id="c3", task_id="",
                              is_first_turn=True) is None
    assert ac.on_pre_llm_call(session_id="c3", task_id="",
                              is_first_turn=False) is None


def test_kanban_dialect_both_axes(stubs, tmp_path, monkeypatch):
    """Kanban worker with a real board DB: both axes present; tier text
    states both numbers and names the exact checkpoint path."""
    import sqlite3
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL)")
    con.execute("INSERT INTO tasks VALUES ('t_kb', 3600)")
    con.commit()
    con.close()
    stubs["on"] = True
    stubs["db_path"] = str(db)
    stubs["config"] = {"agent": {"max_turns": 20}}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_kb")

    ac.on_pre_llm_call(session_id="s1", task_id="t_kb", is_first_turn=True)
    advance("s1", "t_kb", 9)
    r = ac.on_pre_llm_call(session_id="s1", task_id="t_kb", is_first_turn=False)
    assert r is not None
    ctx = r["context"]
    assert "10 of 20 turns" in ctx      # turn axis, from profile cap
    assert " of 1h00m" in ctx           # time axis, from board cap
    assert "t_kb.md" in ctx             # checkpoint path = <dir>/t_kb.md


# ---------------------------------------------------------------------------
# Modes + register()
# ---------------------------------------------------------------------------

def test_notify_mode_numbers_only(stubs, monkeypatch):
    """notify mode: injected text has the numbers and no directive."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")
    stubs["config"] = {}
    patch_section(monkeypatch, {"mode": "notify", "caps": {"turns": 20}})
    begin(tid="t_n")
    advance("s1", "t_n", 9)
    r = call(False, tid="t_n")
    assert r is not None
    assert "Budget checkpoint:" in r["context"]
    assert "write everything found so far" not in r["context"]


def test_log_only_injects_nothing(stubs, monkeypatch):
    """log-only mode: due tiers produce no injection (plugin log only)."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")
    stubs["config"] = {}
    patch_section(monkeypatch, {"mode": "log-only", "caps": {"turns": 20}})
    begin(tid="t_l")
    got = None
    for _ in range(11):
        got = call(False, tid="t_l")
    assert got is None  # nothing ever injected


def test_disabled_injects_nothing(stubs, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "5400")
    stubs["config"] = {}
    patch_section(monkeypatch, {"enabled": False})
    assert ac.on_pre_llm_call(session_id="s", task_id="t",
                              is_first_turn=True) is None


def test_register_wires_pre_llm_call():
    """register(ctx) wires the declared hooks (loader contract).

    The hook set grew with the 2026-10-25 mid-run fix (plugin.yaml
    provides_hooks): pre_llm_call plus the mid-run surfaces.
    """
    calls = {}

    class Ctx:
        def register_hook(self, name, fn):
            calls[name] = fn

    ac.register(Ctx())
    assert set(calls) == {"pre_llm_call", "pre_tool_call",
                          "post_tool_call", "on_session_end"}
    assert calls["pre_llm_call"] is ac.on_pre_llm_call
    assert calls["pre_tool_call"] is ac.on_pre_tool_call
    assert calls["post_tool_call"] is ac.on_post_tool_call
    assert calls["on_session_end"] is ac.on_session_end

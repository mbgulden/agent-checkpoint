"""Kanban time-cap discovery: the board DB value or None — never derived.

Regression suite for the 2026-11-10 incident (t_6ee6ea3d runs 2026/2028):
a task whose board row carries ``max_runtime_seconds = NULL`` got a
manufactured ~210s cap from ``TERMINAL_TIMEOUT + 30`` (the worker env
carries the terminal tool's 180s per-command timeout, not the run cap),
and the watchdog fired a "Final budget checkpoint: about 39s remain"
minutes into a 2-hour run. These tests pin the honest behavior: no DB
value → no time axis → no time-driven tier, however long the run gets.
"""
import sqlite3

import agent_checkpoint as ac


def _board(tmp_path, rows):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL)")
    con.executemany("INSERT INTO tasks VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


def _kanban_env(monkeypatch, db, task_id, terminal_timeout="180"):
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    # The production worker env carries the terminal tool's per-command
    # timeout here. It must never become a run cap.
    monkeypatch.setenv("TERMINAL_TIMEOUT", terminal_timeout)


def test_null_row_yields_no_time_cap(tmp_path, monkeypatch):
    """NULL max_runtime_seconds + TERMINAL_TIMEOUT set → None (no derivation)."""
    db = _board(tmp_path, [("t_null", None)])
    _kanban_env(monkeypatch, db, "t_null")
    assert ac._kanban_time_cap() is None


def test_missing_row_yields_no_time_cap(tmp_path, monkeypatch):
    """Task id with no board row at all → None, even with TERMINAL_TIMEOUT set."""
    db = _board(tmp_path, [("t_other", 3600)])
    _kanban_env(monkeypatch, db, "t_absent")
    assert ac._kanban_time_cap() is None


def test_explicit_row_wins(tmp_path, monkeypatch):
    """An explicit DB cap is returned as-is; the env value is ignored."""
    db = _board(tmp_path, [("t_capped", 240)])
    _kanban_env(monkeypatch, db, "t_capped")
    assert ac._kanban_time_cap() == 240.0


def test_no_time_axis_no_time_tier_however_long_the_run(tmp_path, monkeypatch):
    """Incident shape end-to-end at the tier-math level: NULL-cap kanban
    run, turns barely used, elapsed far beyond the old derived 210s cap
    → no tier fires and the plan carries no time axis at all."""
    db = _board(tmp_path, [("t_null", None)])
    _kanban_env(monkeypatch, db, "t_null")
    caps = ac.RunCaps(turns_cap=90, time_cap=ac._kanban_time_cap(),
                      dialect="kanban")
    assert caps.time_cap is None
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps, turns_used=0, elapsed=171.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == []
    assert plan.time_left is None
    # Even hours in, elapsed alone can never manufacture a final tier.
    plan = ac.compute_tier_plan(
        ac.PluginConfig(), caps, turns_used=0, elapsed=7200.0, fired_tiers=[])
    assert plan is not None
    assert plan.fire_tiers == []
    assert plan.time_left is None

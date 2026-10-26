"""Regression tests for Build B1 integration defect (t_9ff5ec42).

The as-shipped latch key was ``f"{task_id}::{session_id}"``. In the kanban
goal loop the hook's ``task_id`` kwarg is a fresh per-turn uuid (cli.py
``_run_turn`` passes no task_id -> turn_context.py:580
``effective_task_id = task_id or uuid.uuid4()``), so the key churned every
turn, the turn counter reset to 0 each turn, and NEITHER tier ever fired.
The checkpoint filename was also named from the per-turn uuid, so tier 1 and
tier 2 would each point at a different random ``<uuid>.md``.

These tests drive the REAL ``on_pre_llm_call`` with the goal-loop's exact
kwargs (fresh uuid task_id every turn, stable session_id) and assert the
fixed behavior: both tiers fire exactly once, at the right turns, and the
checkpoint filename is the stable board task id (never the per-turn uuid).
Caps are supplied via the hermetic ``_read_config_section`` seam (the same
seam test_hook.py uses) so they are dialect-independent.
"""
import uuid

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core  # real module (shim re-exports)

SECTION = {
    "mode": "snapshot",
    "enabled_axes": ["turns"],
    "caps": {"turns": 10},
    "checkpoint_dir": "/tmp/ac-b1-cp/",
}


def _call(turn, sid):
    return ac.on_pre_llm_call(
        session_id=sid,
        task_id=str(uuid.uuid4()),  # goal-loop: fresh uuid every turn
        is_first_turn=(turn == 1),
    )


def _drive(n, sid):
    ac.LATCHES._state.clear()
    out = []
    for turn in range(1, n + 1):
        out.append((turn, _call(turn, sid)))
    return out


def test_goal_loop_keying_fires_both_tiers_once(stubs, monkeypatch):
    """Fresh uuid task_id per turn + stable session_id -> both tiers fire,
    each exactly once, at the documented turns (50% / 90% of a 10-turn cap)."""
    monkeypatch.setattr(ac_core, "_read_config_section", lambda: dict(SECTION))
    out = _drive(10, "sess-1")
    injected = [(t, r) for t, r in out if r]
    # The hook reports turns_used = "turns already consumed". Turn n of the
    # run is the n-th LLM call: by call 6, 5 of 10 turns are consumed (50%)
    # -> tier 1; by call 9, 8 consumed... wait, 8/10=0.8<0.9. Recheck: tier 2
    # fires at call 9 because remaining = 10-8 = 2 <= PENULTIMATE_TURNS (2)
    # triggers it early (Hermes' own penultimate-turn rule), so the finalize
    # directive lands on the second-to-last turn. Exactly two injections, each
    # tier once.
    assert [t for t, _ in injected] == [6, 9], injected
    texts = [r["context"] for _, r in injected]
    assert any("write everything found so far" in t for t in texts)  # tier 1
    assert any("Final budget checkpoint" in t for t in texts)          # tier 2
    # The whole 10-turn run collapsed to ONE latched session (no leak).
    assert ac.LATCHES.snapshot().keys() == {"sess-1"}
    assert sorted(ac.LATCHES.snapshot()["sess-1"]["fired"]) == [0, 1]


def test_as_shipped_bug_is_gone_no_latch_leak(stubs, monkeypatch):
    """Baseline leaked 10 latch entries (one per turn). Fixed: one run."""
    monkeypatch.setattr(ac_core, "_read_config_section",
                        lambda: dict(SECTION, mode="log-only"))
    _drive(10, "sess-2")
    assert len(ac.LATCHES.snapshot()) == 1, ac.LATCHES.snapshot()
    assert sorted(ac.LATCHES.snapshot()["sess-2"]["fired"]) == [0, 1]


def test_checkpoint_filename_uses_kanban_board_task_id(stubs, monkeypatch):
    """Tier 1 and tier 2 name the SAME stable file = the board task id
    (HERMES_KANBAN_TASK), never the per-turn uuid task_id kwarg."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_boardtask")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(ac_core, "_read_config_section", lambda: dict(SECTION))
    ac.LATCHES._state.clear()
    t1 = t2 = None
    for turn in range(1, 11):
        res = ac.on_pre_llm_call(
            session_id="sess-3", task_id=str(uuid.uuid4()),
            is_first_turn=(turn == 1))
        if res and res["context"].startswith("Budget checkpoint"):
            t1 = res["context"]
        if res and res["context"].startswith("Final budget checkpoint"):
            t2 = res["context"]
    assert t1 and t2, (t1, t2)
    for txt in (t1, t2):
        assert "/tmp/ac-b1-cp/t_boardtask.md" in txt, txt
        assert "uuid" not in txt.lower()


def test_run_id_falls_back_to_session_id_without_kanban(stubs, monkeypatch):
    """No HERMES_KANBAN_TASK -> the stable session id names the file."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(ac_core, "_read_config_section", lambda: dict(SECTION))
    ac.LATCHES._state.clear()
    t1 = t2 = None
    for turn in range(1, 11):
        res = ac.on_pre_llm_call(
            session_id="sess-x", task_id=str(uuid.uuid4()),
            is_first_turn=(turn == 1))
        if res and res["context"].startswith("Budget checkpoint"):
            t1 = res["context"]
        if res and res["context"].startswith("Final budget checkpoint"):
            t2 = res["context"]
    assert t1 and t2
    assert "/tmp/ac-b1-cp/sess-x.md" in t1
    assert "/tmp/ac-b1-cp/sess-x.md" in t2


def test_stable_key_does_not_leak_between_sessions(stubs, monkeypatch):
    """Two concurrent sessions in one process (the gateway case) each latch
    independently and fire independently — the fix did not break isolation."""
    monkeypatch.setattr(ac_core, "_read_config_section",
                        lambda: dict(SECTION, mode="log-only"))
    ac.LATCHES._state.clear()
    for turn in range(1, 11):
        ac.on_pre_llm_call(session_id="A", task_id=str(uuid.uuid4()),
                           is_first_turn=(turn == 1))
        ac.on_pre_llm_call(session_id="B", task_id=str(uuid.uuid4()),
                           is_first_turn=(turn == 1))
    snap = ac.LATCHES.snapshot()
    assert set(snap) == {"A", "B"}
    assert sorted(snap["A"]["fired"]) == [0, 1]
    assert sorted(snap["B"]["fired"]) == [0, 1]

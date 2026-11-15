"""Notice fix A (0.5.0): provenance-aware suppression of the budget
notice.

A kanban run's injected budget notice is emitted only when its cap is
the board's own (``cap_source == "db"``). A kanban run whose budget fell
back to configuration (NULL board row -> profile/plugin caps) or that
has no budget at all gets no budget notice — the board never set that
budget. The persist surfaces (checkpoint file + persisted comment) are
NOT the notice and are unchanged. Non-kanban dialects are untouched:
there, configuration is the arming mechanism itself (chat arms via
plugin caps or ``agent.run_budget_seconds``; a goal's stored cap is
``db``), so deliberately armed runs keep their notices.

Adversarial case: ``test_notice_suppressed_for_config_provenance`` —
without fix A, that run's prologue returns the budget text (the
phantom-budget shape); with fix A it returns None. Demonstrated against
the pre-change code on a stash in the build report. The capless case is
a regression pin (a capless run could never fire a tier, before or
after).

Hermetic seams follow test_teeth_gate.py: fixed config section, fake
clock, a real board DB in tmp_path.
"""

import sqlite3
import types

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core


def patch_section(monkey, section):
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def set_elapsed(monkey, seconds):
    monkey.setattr(ac.LATCHES, "elapsed", lambda _key: float(seconds))


def _board(tmp_path, rows):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL, body TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


def _section(tmp_path, **over):
    section = {
        "checkpoint_dir": str(tmp_path / "ck") + "/",
        "watchdog": False,
    }
    section.update(over)
    return section


def _begin_kanban(monkeypatch, task_id="t_notice"):
    ac.on_pre_llm_call(session_id="s1", task_id=task_id,
                       is_first_turn=True)


def _prologue(task_id="t_notice"):
    return ac.on_pre_llm_call(session_id="s1", task_id=task_id,
                              is_first_turn=False)


# ---------------------------------------------------------------------------
# Emitted: the board's own cap (and the preserved non-kanban cases)
# ---------------------------------------------------------------------------


def test_notice_emitted_for_db_cap(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    db = _board(tmp_path, [("t_notice", 7200, "Notice test task.")])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_notice")
    set_elapsed(monkeypatch, 0.0)
    _begin_kanban(monkeypatch)
    set_elapsed(monkeypatch, 3700.0)  # 51% of the board cap: tier 1
    r = _prologue()
    assert r is not None
    assert r["context"].startswith("Budget checkpoint")
    assert str(tmp_path / "ck" / "t_notice.md") in r["context"]


def test_notice_chat_run_budget_armed_still_emits(stubs, monkeypatch,
                                                  tmp_path):
    """Scope pin: a chat session deliberately armed via
    ``agent.run_budget_seconds`` keeps its notice — configuration is
    the arming mechanism outside kanban, and fix A must not silence
    it."""
    stubs["config"] = {"agent": {"run_budget_seconds": 3600.0}}
    patch_section(monkeypatch, _section(tmp_path))
    set_elapsed(monkeypatch, 0.0)
    _begin_kanban(monkeypatch)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.dialect == "chat"
    assert caps.cap_source == "config"
    set_elapsed(monkeypatch, 1900.0)  # 53% of the armed budget
    r = _prologue()
    assert r is not None
    assert r["context"].startswith("Budget checkpoint")


def test_notice_goal_native_cap_still_emits(stubs, monkeypatch, tmp_path):
    """A goal's stored max_turns is a native cap (cap_source 'db'):
    its notices are board-derived in the provenance sense and emit."""
    stubs["config"] = {}
    stubs["goals"]["s1"] = types.SimpleNamespace(max_turns=20)
    patch_section(monkeypatch, _section(tmp_path))
    _begin_kanban(monkeypatch)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.dialect == "goal"
    assert caps.cap_source == "db"
    r = None
    for _ in range(10):  # 10 of 20 turns: tier 1 at 50%
        r = _prologue() or r
    assert r is not None
    assert r["context"].startswith("Budget checkpoint")


# ---------------------------------------------------------------------------
# Suppressed: kanban runs whose budget is not the board's own
# ---------------------------------------------------------------------------


def test_notice_suppressed_for_config_provenance(stubs, monkeypatch,
                                                 tmp_path):
    """ADVERSARIAL: NULL board cap + plugin-configured cap (provenance
    'config') on a kanban run — tier 1 is due, but no budget notice is
    injected. Without fix A this prologue returns the budget text."""
    stubs["config"] = {}
    patch_section(monkeypatch,
                  _section(tmp_path, caps={"time_seconds": 7200}))
    db = _board(tmp_path, [("t_notice", None, "Notice test task.")])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_notice")
    set_elapsed(monkeypatch, 0.0)
    _begin_kanban(monkeypatch)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.dialect == "kanban"
    assert caps.cap_source == "config"
    set_elapsed(monkeypatch, 3700.0)  # 51% of the configured cap
    assert _prologue() is None
    # The suppressed tier still latches: the notice does not re-offer
    # itself on the next turn.
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_notice"))
    assert 0 in st["fired"]
    assert _prologue() is None


def test_notice_suppressed_for_capless_kanban(stubs, monkeypatch,
                                              tmp_path):
    """Regression pin: a kanban run with no cap anywhere (provenance
    'none') emits no budget notice."""
    stubs["config"] = {}
    patch_section(monkeypatch, _section(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_notice")
    set_elapsed(monkeypatch, 0.0)
    _begin_kanban(monkeypatch)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.cap_source == "none"
    set_elapsed(monkeypatch, 3700.0)
    assert _prologue() is None


def test_persist_unchanged_when_notice_suppressed(stubs, monkeypatch,
                                                  tmp_path):
    """The persist surface is the durable record, not the notice: a
    config-capped kanban run whose notice is suppressed still gets its
    mid-run checkpoint file at the tier crossing."""
    stubs["config"] = {}
    patch_section(monkeypatch,
                  _section(tmp_path, caps={"time_seconds": 7200}))
    db = _board(tmp_path, [("t_notice", None, "Notice test task.")])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_notice")
    set_elapsed(monkeypatch, 0.0)
    _begin_kanban(monkeypatch)
    set_elapsed(monkeypatch, 3700.0)
    assert _prologue() is None  # notice suppressed
    ac.on_post_tool_call(tool_name="read_file", session_id="s1",
                         task_id="t_notice")
    path = tmp_path / "ck" / "t_notice.md"
    assert path.exists()
    assert "budget checkpoint" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The decision itself, pinned as a unit
# ---------------------------------------------------------------------------


def test_budget_notice_allowed_unit():
    mk = ac.RunCaps
    assert ac._budget_notice_allowed(
        mk(turns_cap=None, time_cap=100.0, dialect="kanban",
           cap_source="db"))
    assert not ac._budget_notice_allowed(
        mk(turns_cap=None, time_cap=100.0, dialect="kanban",
           cap_source="config"))
    assert not ac._budget_notice_allowed(
        mk(turns_cap=None, time_cap=None, dialect="kanban",
           cap_source="none"))
    # Non-kanban: configuration is the arming mechanism — allowed.
    assert ac._budget_notice_allowed(
        mk(turns_cap=None, time_cap=100.0, dialect="chat",
           cap_source="config"))
    assert ac._budget_notice_allowed(
        mk(turns_cap=20, time_cap=None, dialect="goal",
           cap_source="db"))
    assert not ac._budget_notice_allowed(None)

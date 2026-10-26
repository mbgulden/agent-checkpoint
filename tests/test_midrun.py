"""Mid-run persistence (2026-10-25 fix): tool-boundary evaluation, watchdog
persistence, latch separation, inert mode, and fail-open DB behavior.

Hermetic, same seams as the rest of the suite: the plugin config section is
pointed at a fixed dict, ``LATCHES.elapsed`` is replaced with a fake clock,
and the kanban comment / checkpoint-file writers are spied (or, in two
tests, exercised for real against a temp SQLite DB in the fork's schema).
"""
import sqlite3
import threading
import time

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core  # real module (shim re-exports)


def patch_section(monkey, section):
    """Point the plugin's config reader at a fixed section (hermetic)."""
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def set_elapsed(monkey, seconds):
    """Fake clock: the session's elapsed time is ``seconds``."""
    monkey.setattr(ac.LATCHES, "elapsed", lambda _key: float(seconds))


class PersistSpy:
    """Records checkpoint-file writes and kanban comment posts."""

    def __init__(self, monkey):
        self.files = []     # (path, content)
        self.comments = []  # (task_id, body)
        monkey.setattr(ac_core, "_write_checkpoint_file", self._write)
        monkey.setattr(ac_core, "_post_kanban_comment", self._post)

    def _write(self, path, content):
        self.files.append((path, content))
        return True

    def _post(self, task_id, body):
        self.comments.append((task_id, body))
        return 4242  # fake comment id


def kanban_section(tmp_path, **over):
    """Plugin config section for a kanban run capped at 120s.

    ``allow_tiny_tier1`` is on because a 120s cap is a "tiny" cap and the
    shipped tiny-cap rule would otherwise suppress tier 1 at this scale
    (the live proof's tier-2-only behavior at 120s is the default)."""
    section = {
        "caps": {"time_seconds": 120},
        "checkpoint_dir": str(tmp_path) + "/",
        "allow_tiny_tier1": True,
        "watchdog": False,  # tool-hook tests drive evaluation directly
    }
    section.update(over)
    return section


def begin_kanban(monkey, sid="s1", tid="t_mid"):
    """A kanban run begins (env dialect + first prologue)."""
    monkey.setenv("HERMES_KANBAN_TASK", tid)
    return ac.on_pre_llm_call(session_id=sid, task_id=tid,
                              is_first_turn=True)


# ---------------------------------------------------------------------------
# Tool-hook persist at a crossing
# ---------------------------------------------------------------------------

def test_tool_hook_persists_at_crossing(stubs, monkeypatch, tmp_path):
    """Crossing 50% inside a run: the next tool boundary persists tier 1 —
    checkpoint file written, kanban comment posted, run-id = board task."""
    stubs["config"] = {}
    patch_section(monkeypatch, kanban_section(tmp_path))
    spy = PersistSpy(monkeypatch)

    assert begin_kanban(monkeypatch) is None  # elapsed 0: nothing due
    set_elapsed(monkeypatch, 61.0)  # 61/120 > 50%
    ac.on_post_tool_call(tool_name="read_file", session_id="s1",
                         task_id="t_mid")
    assert len(spy.files) == 1
    path, content = spy.files[0]
    assert path == str(tmp_path) + "/t_mid.md"
    assert "persisted mid-run (source: post_tool_call)" in content
    assert len(spy.comments) == 1
    ctid, body = spy.comments[0]
    assert ctid == "t_mid"
    assert "agent-checkpoint" in body

    # Still past 50% but tier 1 is latched persisted: no second persist,
    # and tier 2 is not due yet (61 < 96).
    ac.on_pre_tool_call(tool_name="read_file", session_id="s1",
                        task_id="t_mid")
    assert len(spy.files) == 1
    assert len(spy.comments) == 1

    # Tier 2 crossing persists the same way, once.
    set_elapsed(monkeypatch, 100.0)  # > 80% of 120
    ac.on_post_tool_call(tool_name="terminal", session_id="s1",
                         task_id="t_mid")
    assert len(spy.files) == 2
    assert len(spy.comments) == 2
    assert "Final budget checkpoint" in spy.comments[1][1]


# ---------------------------------------------------------------------------
# Watchdog persist with a fake clock
# ---------------------------------------------------------------------------

def test_watchdog_persists_with_fake_clock(stubs, monkeypatch, tmp_path):
    """No tool boundaries at all: the watchdog notices the crossing on
    its own poll and persists tier 1 with source=watchdog."""
    stubs["config"] = {}
    # Begin with the watchdog OFF so the prologue does not auto-start one;
    # then flip it on and drive exactly one watchdog thread by hand.
    patch_section(monkeypatch, kanban_section(tmp_path))
    spy = PersistSpy(monkeypatch)

    assert begin_kanban(monkeypatch) is None
    set_elapsed(monkeypatch, 61.0)  # past 50%, no tool call ever happens
    patch_section(monkeypatch, kanban_section(
        tmp_path, watchdog=True, watchdog_poll_seconds=0.05))

    key = ac.SessionLatches.key("s1", "t_mid")
    stop = threading.Event()
    th = threading.Thread(target=ac._watchdog_loop,
                          args=(key, "s1", "t_mid", stop), daemon=True)
    th.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not spy.comments:
            time.sleep(0.02)
    finally:
        stop.set()
        th.join(timeout=5.0)
    assert not th.is_alive()
    assert len(spy.comments) == 1
    assert "persisted mid-run (source: watchdog)" in spy.comments[0][1]
    st = ac.LATCHES.get(key)
    assert st["persisted"] == [0]


# ---------------------------------------------------------------------------
# Latch separation
# ---------------------------------------------------------------------------

def test_persist_does_not_suppress_prologue_injection(
        stubs, monkeypatch, tmp_path):
    """A mid-run persist latches ONLY the persisted set: the next turn
    prologue still injects the same tier's text to the worker."""
    stubs["config"] = {}
    patch_section(monkeypatch, kanban_section(tmp_path))
    spy = PersistSpy(monkeypatch)

    assert begin_kanban(monkeypatch) is None
    set_elapsed(monkeypatch, 61.0)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1",
                         task_id="t_mid")
    assert len(spy.comments) == 1  # tier 1 persisted mid-run

    # Next prologue: tier 1 injects as if the persist never happened.
    r = ac.on_pre_llm_call(session_id="s1", task_id="t_mid",
                           is_first_turn=False)
    assert r is not None
    assert "write everything found so far" in r["context"]
    # And the prologue injection does not re-persist anything.
    assert len(spy.comments) == 1
    # A later tool hook persists nothing new (both surfaces handled it).
    ac.on_post_tool_call(tool_name="read_file", session_id="s1",
                         task_id="t_mid")
    assert len(spy.comments) == 1


# ---------------------------------------------------------------------------
# Inert with mid_run_persist off (adversarial)
# ---------------------------------------------------------------------------

def test_inert_when_mid_run_persist_off(stubs, monkeypatch, tmp_path):
    """Adversarial: mid_run_persist=false — a simulated run crossing 80%
    via tool hooks persists nothing and posts nothing, while the classic
    prologue injection still works."""
    stubs["config"] = {}
    patch_section(monkeypatch, kanban_section(
        tmp_path, mid_run_persist=False))
    spy = PersistSpy(monkeypatch)

    assert begin_kanban(monkeypatch) is None
    set_elapsed(monkeypatch, 100.0)  # past BOTH 50% and 80%
    ac.on_pre_tool_call(tool_name="terminal", session_id="s1",
                        task_id="t_mid")
    ac.on_post_tool_call(tool_name="terminal", session_id="s1",
                         task_id="t_mid")
    assert spy.files == []
    assert spy.comments == []
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_mid"))
    assert st["persisted"] == []

    # Prologue injection is untouched by the switch.
    r = ac.on_pre_llm_call(session_id="s1", task_id="t_mid",
                           is_first_turn=False)
    assert r is not None
    assert "Final budget checkpoint" in r["context"]


# ---------------------------------------------------------------------------
# Fail-open on a DB error (+ the real writer against the fork's schema)
# ---------------------------------------------------------------------------

def test_comment_db_error_fails_open(stubs, monkeypatch, tmp_path):
    """The real comment writer against an unopenable DB returns None and
    raises nothing; the tool hook still completes and the checkpoint
    file half of the persist still lands."""
    stubs["config"] = {}
    patch_section(monkeypatch, kanban_section(tmp_path))
    # Real writers (no spy): point the comment path at a directory that
    # cannot be opened as a SQLite DB.
    bad = tmp_path / "notadb"
    bad.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(bad))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_mid")
    assert ac._post_kanban_comment("t_mid", "body") is None

    assert ac.on_pre_llm_call(session_id="s1", task_id="t_mid",
                              is_first_turn=True) is None
    set_elapsed(monkeypatch, 100.0)
    # Must not raise even though every comment post fails underneath.
    ac.on_post_tool_call(tool_name="terminal", session_id="s1",
                         task_id="t_mid")
    path = tmp_path / "t_mid.md"
    assert path.exists()  # the file half of the persist still landed
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_mid"))
    assert st["persisted"] == [0, 1]


def test_comment_write_mirrors_fork_schema(stubs, monkeypatch, tmp_path):
    """The real comment writer against a real DB in the fork's schema:
    one task_comments row (author agent-checkpoint) + one 'commented'
    task_events row — exactly the fork's add_comment shape."""
    stubs["config"] = {}
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    con.execute(
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,"
        " created_at INTEGER NOT NULL)")
    con.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
        " payload TEXT, created_at INTEGER NOT NULL)")
    con.execute("INSERT INTO tasks (id) VALUES ('t_mid')")
    con.commit()
    con.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    cid = ac._post_kanban_comment("t_mid", "hello checkpoint")
    assert cid is not None and cid >= 1
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT task_id, author, body FROM task_comments").fetchall()
    evts = con.execute(
        "SELECT task_id, run_id, kind, payload FROM task_events").fetchall()
    con.close()
    assert rows == [("t_mid", "agent-checkpoint", "hello checkpoint")]
    assert len(evts) == 1
    assert evts[0][0] == "t_mid" and evts[0][1] is None
    assert evts[0][2] == "commented"
    assert '"agent-checkpoint"' in evts[0][3]
    # Unknown task: no row, no raise.
    assert ac._post_kanban_comment("t_nope", "body") is None

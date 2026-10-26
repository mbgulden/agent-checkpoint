"""Uncapped-run fallback (0.3.0, audit F21): a run with NO discoverable
cap on either axis gets a watchdog/tool-hook-persisted checkpoint every
``uncapped_interval_seconds`` of elapsed time, so it still leaves a
durable record. First interval fire posts the kanban comment (kanban
dialect only); later fires are file-only. Interval 0 = off, and then a
capless session's watchdog exits on its first poll instead of polling
for the session's whole life (audit F5 resolution).

Hermetic, same seams as test_midrun: the plugin config section is a
fixed dict, ``LATCHES.elapsed`` is a fake clock, writers are spied.
"""
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


def capless_kanban_section(tmp_path, **over):
    """Config for a kanban run with NO cap anywhere: the board DB is
    pointed at a nonexistent path by the test and TERMINAL_TIMEOUT is
    stripped by conftest, so discovery resolves to RunCaps(None, None).
    """
    section = {
        "checkpoint_dir": str(tmp_path) + "/",
        "uncapped_interval_seconds": 60,
        "watchdog": False,  # tests drive surfaces by hand unless stated
    }
    section.update(over)
    return section


def begin_capless_kanban(monkey, sid="s_unc", tid="t_unc"):
    monkey.setenv("HERMES_KANBAN_DB", "/nonexistent-dir/no-board.db")
    monkey.setenv("HERMES_KANBAN_TASK", tid)
    return ac.on_pre_llm_call(session_id=sid, task_id=tid,
                              is_first_turn=True)


def wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ---------------------------------------------------------------------------
# Config: default, clamp, fail-open (audit A4 conventions)
# ---------------------------------------------------------------------------

def test_uncapped_config_default_clamp_and_failopen(stubs, monkeypatch):
    assert ac.PluginConfig().uncapped_interval_seconds == 1800.0
    patch_section(monkeypatch, {"uncapped_interval_seconds": 45})
    assert ac.load_plugin_config().uncapped_interval_seconds == 45.0
    # Negative clamps to 0 (off) — the watchdog_poll_seconds precedent.
    patch_section(monkeypatch, {"uncapped_interval_seconds": -5})
    assert ac.load_plugin_config().uncapped_interval_seconds == 0.0
    # Non-numeric: the whole load fails open to defaults (loader contract).
    patch_section(monkeypatch, {"uncapped_interval_seconds": "abc"})
    assert ac.load_plugin_config().uncapped_interval_seconds == 1800.0


# ---------------------------------------------------------------------------
# (a) Watchdog-driven fires + first-fire comment policy (kanban, capless)
# ---------------------------------------------------------------------------

def test_uncapped_watchdog_fires_comment_policy(stubs, monkeypatch,
                                               tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, capless_kanban_section(tmp_path))
    spy = PersistSpy(monkeypatch)

    # Begin: capless runs never inject, at any usage level.
    assert begin_capless_kanban(monkeypatch) is None
    key = ac.SessionLatches.key("s_unc", "t_unc")
    st = ac.LATCHES.get(key)
    assert st["caps"].turns_cap is None and st["caps"].time_cap is None

    patch_section(monkeypatch, capless_kanban_section(
        tmp_path, watchdog=True, watchdog_poll_seconds=0.05))
    stop = threading.Event()
    th = threading.Thread(target=ac._watchdog_loop,
                          args=(key, "s_unc", "t_unc", stop), daemon=True)
    th.start()
    try:
        # Before the first boundary: nothing, however long we poll.
        set_elapsed(monkeypatch, 59.0)
        time.sleep(0.3)
        assert spy.files == [] and spy.comments == []

        # At the boundary: fire 1 — file + the run's one comment.
        set_elapsed(monkeypatch, 60.0)
        assert wait_until(lambda: len(spy.files) == 1)
        path, content = spy.files[0]
        assert path == str(tmp_path) + "/t_unc.md"
        assert "uncapped-run checkpoint 1" in content
        assert "no discoverable turn or time cap" in content
        assert "(source: watchdog)" in content
        assert len(spy.comments) == 1
        assert spy.comments[0][0] == "t_unc"
        assert "uncapped-run checkpoint 1" in spy.comments[0][1]
        assert st["uncapped_fires"] == 1

        # Between boundaries: no repeat fire.
        set_elapsed(monkeypatch, 119.0)
        time.sleep(0.3)
        assert len(spy.files) == 1

        # Second boundary: file rewritten, NO second comment.
        set_elapsed(monkeypatch, 120.0)
        assert wait_until(lambda: len(spy.files) == 2)
        assert "uncapped-run checkpoint 2" in spy.files[1][1]
        assert len(spy.comments) == 1
        assert st["uncapped_fires"] == 2
    finally:
        stop.set()
        th.join(timeout=5.0)
    assert not th.is_alive()


# ---------------------------------------------------------------------------
# Tool-hook surface fires the same fallback (earlier persists in
# turn-rich capless sessions); the inject surface never does.
# ---------------------------------------------------------------------------

def test_uncapped_tool_hook_fires_inject_never(stubs, monkeypatch,
                                               tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, capless_kanban_section(tmp_path))
    spy = PersistSpy(monkeypatch)

    assert begin_capless_kanban(monkeypatch) is None
    set_elapsed(monkeypatch, 3600.0)  # far past several boundaries

    # Prologue (inject surface): still nothing, and writes nothing.
    r = ac.on_pre_llm_call(session_id="s_unc", task_id="t_unc",
                           is_first_turn=False)
    assert r is None
    assert spy.files == [] and spy.comments == []

    # Tool boundary: the fallback fires (one boundary per call).
    ac.on_post_tool_call(tool_name="read_file", session_id="s_unc",
                         task_id="t_unc")
    assert len(spy.files) == 1
    assert "(source: post_tool_call)" in spy.files[0][1]
    assert len(spy.comments) == 1
    ac.on_pre_tool_call(tool_name="read_file", session_id="s_unc",
                        task_id="t_unc")
    assert len(spy.files) == 2
    assert len(spy.comments) == 1  # later fires are file-only


# ---------------------------------------------------------------------------
# (b) Interval 0: capless session fully silent + watchdog exits promptly
# ---------------------------------------------------------------------------

def test_uncapped_off_silent_and_watchdog_exits(stubs, monkeypatch,
                                                tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, {
        "checkpoint_dir": str(tmp_path) + "/",
        "uncapped_interval_seconds": 0,
        "watchdog": True,
        "watchdog_poll_seconds": 0.05,
    })
    spy = PersistSpy(monkeypatch)
    sid = "s_off_xyz"
    assert ac.on_pre_llm_call(session_id=sid, task_id="",
                              is_first_turn=True) is None

    # The auto-started watchdog must exit on its own (first poll):
    # capless + fallback off means nothing can ever fire (audit F5).
    def watchdog_alive():
        return any(t.is_alive() and sid[:24] in t.name
                   for t in threading.enumerate()
                   if t.name.startswith("agent-checkpoint-watchdog"))
    assert wait_until(lambda: not watchdog_alive())

    # And every persist surface stays silent at any elapsed time.
    set_elapsed(monkeypatch, 100000.0)
    ac.on_pre_tool_call(tool_name="terminal", session_id=sid, task_id="")
    ac.on_post_tool_call(tool_name="terminal", session_id=sid, task_id="")
    cfg = ac.load_plugin_config()
    key = ac.SessionLatches.key(sid, "")
    assert ac._uncapped_fallback(cfg, key, sid, "", "watchdog") is False
    assert spy.files == [] and spy.comments == []
    assert ac.LATCHES.get(key)["uncapped_fires"] == 0


# ---------------------------------------------------------------------------
# (d) Drift-free boundary math: fires at interval, 2x, 3x — never early
# ---------------------------------------------------------------------------

def test_uncapped_boundary_math(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, {
        "checkpoint_dir": str(tmp_path) + "/",
        "uncapped_interval_seconds": 60,
        "watchdog": False,
    })
    spy = PersistSpy(monkeypatch)
    sid = "s_math"  # chat dialect: files only, never comments
    assert ac.on_pre_llm_call(session_id=sid, task_id="",
                              is_first_turn=True) is None
    cfg = ac.load_plugin_config()
    key = ac.SessionLatches.key(sid, "")

    cases = [(59.999999, False), (60.0, True), (119.999999, False),
             (120.0, True), (179.9, False), (180.0, True)]
    for elapsed, expected in cases:
        set_elapsed(monkeypatch, elapsed)
        assert ac._uncapped_fallback(cfg, key, sid, "", "watchdog") \
            is expected, f"elapsed={elapsed}"
    st = ac.LATCHES.get(key)
    assert st["uncapped_fires"] == 3
    assert len(spy.files) == 3
    assert spy.comments == []  # chat dialect posts no board comments


# ---------------------------------------------------------------------------
# (c) Capped regression: a capped run NEVER touches the fallback
# ---------------------------------------------------------------------------

def test_capped_session_never_uses_fallback(stubs, monkeypatch, tmp_path):
    stubs["config"] = {}
    patch_section(monkeypatch, {
        "caps": {"time_seconds": 120},
        "checkpoint_dir": str(tmp_path) + "/",
        "allow_tiny_tier1": True,
        "uncapped_interval_seconds": 60,  # armed — must stay unused
        "watchdog": False,
    })
    spy = PersistSpy(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_cap")

    assert ac.on_pre_llm_call(session_id="s_cap", task_id="t_cap",
                              is_first_turn=True) is None
    key = ac.SessionLatches.key("s_cap", "t_cap")

    set_elapsed(monkeypatch, 61.0)  # tier 1 crossing AND past interval 1
    ac.on_post_tool_call(tool_name="read_file", session_id="s_cap",
                         task_id="t_cap")
    assert len(spy.files) == 1
    assert "persisted mid-run" in spy.files[0][1]
    assert "uncapped-run checkpoint" not in spy.files[0][1]

    set_elapsed(monkeypatch, 100.0)  # tier 2 crossing
    ac.on_post_tool_call(tool_name="terminal", session_id="s_cap",
                         task_id="t_cap")
    assert len(spy.files) == 2
    assert len(spy.comments) == 2

    # Even directly invoked, far past every boundary: predicate refuses.
    set_elapsed(monkeypatch, 5000.0)
    cfg = ac.load_plugin_config()
    assert ac._uncapped_fallback(cfg, key, "s_cap", "t_cap",
                                 "watchdog") is False
    assert ac.LATCHES.get(key)["uncapped_fires"] == 0


# ---------------------------------------------------------------------------
# Caps not yet discovered: the fallback waits (distinct from capless)
# ---------------------------------------------------------------------------

def test_uncapped_waits_for_discovery(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, {
        "checkpoint_dir": str(tmp_path) + "/",
        "uncapped_interval_seconds": 60,
        "watchdog": False,
    })
    spy = PersistSpy(monkeypatch)
    # State begun by hand — caps never discovered (st["caps"] is None).
    key = ac.SessionLatches.key("s_new", "")
    ac.LATCHES.begin_run(key)
    set_elapsed(monkeypatch, 3600.0)
    cfg = ac.load_plugin_config()
    assert ac._uncapped_fallback(cfg, key, "s_new", "", "watchdog") \
        is False
    assert spy.files == []

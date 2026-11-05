"""Mid-run persistence (2026-10-25 fix): tool-boundary evaluation, watchdog
persistence, latch separation, inert mode, and fail-open DB behavior.

Hermetic, same seams as the rest of the suite: the plugin config section is
pointed at a fixed dict, ``LATCHES.elapsed`` is replaced with a fake clock,
and the kanban comment / checkpoint-file writers are spied (or, in the
dispatch tests below, the comment writer is exercised for real against a
fake plugin context).
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
# Fail-open when comment posting is unavailable (+ the real writer)
# ---------------------------------------------------------------------------

def test_comment_unavailable_fails_open(stubs, monkeypatch, tmp_path):
    """The real comment writer with no plugin context registered (so the
    kanban_comment dispatch has nowhere to go) returns None and raises
    nothing; the tool hook still completes and the checkpoint file half
    of the persist still lands."""
    stubs["config"] = {}
    patch_section(monkeypatch, kanban_section(tmp_path))
    # Real writers (no spy), and conftest guarantees no ctx is
    # registered: every comment post underneath fails open to None.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
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


class _FakeCtx:
    """Minimal plugin context: records tool dispatches."""

    def __init__(self, result=None, raises=False):
        self.calls = []
        self.result = {"comment_id": 7} if result is None else result
        self.raises = raises

    def register_hook(self, name, fn):
        pass

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if self.raises:
            raise RuntimeError("dispatch exploded")
        return self.result


def test_comment_posts_via_dispatch_tool():
    """The real comment writer goes through the registered plugin
    context: dispatch_tool("kanban_comment", {task_id, body}) with the
    body headed by the plugin name (core sets the author). The dispatch
    result is returned to the caller."""
    ctx = _FakeCtx()
    ac.register(ctx)  # register() captures the ctx for the hooks
    try:
        result = ac._post_kanban_comment("t_mid", "hello checkpoint")
        assert result == {"comment_id": 7}
        assert ctx.calls == [("kanban_comment", {
            "task_id": "t_mid",
            "body": "agent-checkpoint: checkpoint comment\n\n"
                    "hello checkpoint",
        })]
        # A body that already leads with the plugin name (the persist
        # surfaces build one) is passed through unchanged.
        headed = "agent-checkpoint: budget checkpoint persisted mid-run"
        assert ac._post_kanban_comment("t_mid", headed)
        assert ctx.calls[-1][1]["body"] == headed
        # Empty body: no dispatch, no raise.
        assert ac._post_kanban_comment("t_mid", "  ") is None
        assert len(ctx.calls) == 2
    finally:
        ac_core._PLUGIN_CTX = None
    # No context registered: fail-open None, nothing dispatched.
    assert ac._post_kanban_comment("t_mid", "hello checkpoint") is None


def test_comment_dispatch_error_fails_open():
    """A dispatch that raises degrades to None; the writer never
    propagates (the watchdog thread calls this path)."""
    ac.register(_FakeCtx(raises=True))
    try:
        assert ac._post_kanban_comment("t_mid", "body") is None
    finally:
        ac_core._PLUGIN_CTX = None

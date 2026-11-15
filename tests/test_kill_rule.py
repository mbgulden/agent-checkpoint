"""Half-cap kill rule (teeth package, 0.5.0; Nov 9 audit rule, approved
Nov 15, 2026).

On a board-capped kanban run (``cap_source == "db"``, deliverable path
parsed) whose deliverable has not moved since run start, the
``pre_tool_call`` veto puts the run in report mode at >= 50% of cap:
stage 1 allows deliverable + checkpoint writes and the kanban wrap-up
channels; stage 2 (>= 65%) allows checkpoint writes + wrap-up only.
Entering report mode posts ONE kanban comment naming the kill and the
snapshot evidence. Any deliverable movement exits report mode and
disarms the rule for the run.

Adversarial cases: the flag-off run, the configured-cap run, and the
moved-deliverable run must NEVER be blocked — and every positive case
here fails against the pre-0.5.0 code (no flag, no report mode), which
the build report demonstrates on a stash.

Hermetic seams follow test_teeth_gate.py: fixed config section, fake
clock, spied comment writer, a real board DB in tmp_path.
"""

import os
import sqlite3

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core


def patch_section(monkey, section):
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def set_elapsed(monkey, seconds):
    monkey.setattr(ac.LATCHES, "elapsed", lambda _key: float(seconds))


class CommentSpy:
    def __init__(self, monkey):
        self.comments = []  # (task_id, body)
        monkey.setattr(ac_core, "_post_kanban_comment", self._post)

    def _post(self, task_id, body):
        self.comments.append((task_id, body))
        return 4242

    def kill_comments(self):
        return [c for c in self.comments if "kill rule" in c[1]]


def _board(tmp_path, rows):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL, body TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


def _body(deliverable):
    return (
        "DELIVERABLE-FIRST (hard rule): Your FIRST action is to "
        f"create the deliverable file at {deliverable} with its "
        "full skeleton — every section heading — and save it."
    )


def _section(tmp_path, **over):
    section = {
        "checkpoint_dir": str(tmp_path / "ck") + "/",
        "allow_tiny_tier1": True,
        "watchdog": False,
        "teeth": {"kill_at_half_cap": True},
    }
    section.update(over)
    return section


def _begin_db_run(stubs, monkeypatch, tmp_path, task_id="t_kill", cap=240,
                  pre_create=False):
    """A kanban run whose cap is the board row's own value (source db).

    The deliverable baseline is captured at the first prologue: missing
    unless ``pre_create`` writes the file to disk first.
    """
    stubs["config"] = {}
    deliverable = str(tmp_path / "work" / "deliverable.md")
    db = _board(tmp_path, [(task_id, cap, _body(deliverable))])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    if pre_create:
        os.makedirs(os.path.dirname(deliverable), exist_ok=True)
        with open(deliverable, "w", encoding="utf-8") as fh:
            fh.write("skeleton\n")
    ac.on_pre_llm_call(session_id="s1", task_id=task_id, is_first_turn=True)
    return deliverable


def _call(tool_name, args, task_id="t_kill"):
    return ac.on_pre_tool_call(
        tool_name=tool_name, args=args, session_id="s1", task_id=task_id)


OTHER = {"path": "/home/ubuntu/work/other.txt", "content": "x"}


# ---------------------------------------------------------------------------
# Report mode: entry, allowances, the one comment
# ---------------------------------------------------------------------------


def test_kill_enters_report_mode_at_half_cap(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)  # 54% of the 240s board cap
    out = _call("write_file", OTHER)
    assert out is not None
    assert out["action"] == "block"
    assert "kill rule" in out["message"]
    assert "report mode" in out["message"]
    # Exactly one kill comment, on the task, naming the evidence.
    kills = spy.kill_comments()
    assert len(kills) == 1
    ctid, body = kills[0]
    assert ctid == "t_kill"
    assert "report mode" in body
    assert "deliverable" in body
    # A second refused call does not re-comment.
    out = _call("terminal", {"command": "ls /var/tmp"})
    assert out is not None and out["action"] == "block"
    assert len(spy.kill_comments()) == 1


def test_kill_stage1_allows_deliverable_and_checkpoint_writes(
        stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)
    assert _call("write_file",
                 {"path": deliverable, "content": "progress"}) is None
    checkpoint = str(tmp_path / "ck" / "t_kill.md")
    assert _call("write_file",
                 {"path": checkpoint, "content": "report"}) is None


def test_kill_stage1_allows_kanban_wrapup(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)
    for tool in ("kanban_comment", "kanban_complete", "kanban_block",
                 "kanban_heartbeat"):
        assert _call(tool, {"task_id": "t_kill"}) is None


def test_kill_stage1_blocks_reads_of_other_paths(
        stubs, monkeypatch, tmp_path):
    """Report mode is not the final-tier gate: read-class tools get no
    blanket pass — only path-referencing calls and wrap-up pass."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)
    out = _call("read_file", {"path": "/etc/hostname"})
    assert out is not None and out["action"] == "block"


def test_kill_silent_before_half_cap(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 100.0)  # 42% of cap
    assert _call("write_file", OTHER) is None
    assert spy.kill_comments() == []


def test_kill_existing_deliverable_unchanged_engages(
        stubs, monkeypatch, tmp_path):
    """Baseline 'present' (a successor run's inherited file) that never
    changes is still no movement: report mode engages."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path, pre_create=True)
    set_elapsed(monkeypatch, 130.0)
    out = _call("write_file", OTHER)
    assert out is not None and out["action"] == "block"


def test_kill_turns_axis_can_trigger(stubs, monkeypatch, tmp_path):
    """The fraction is the max across axes: 55% of the turns cap with a
    nearly untouched time cap still enters report mode."""
    stubs["config"] = {"agent": {"max_turns": 20}}
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = str(tmp_path / "work" / "deliverable.md")
    db = _board(tmp_path, [("t_kill", 7200, _body(deliverable))])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_kill")
    ac.on_pre_llm_call(session_id="s1", task_id="t_kill",
                       is_first_turn=True)
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_kill"))
    st["turns"] = 11  # 55% of the turns cap; ~0% of the time cap
    out = _call("write_file", OTHER)
    assert out is not None and out["action"] == "block"


# ---------------------------------------------------------------------------
# Stage 2 tightening
# ---------------------------------------------------------------------------


def test_kill_stage2_tightens_to_report_only(stubs, monkeypatch,
                                             tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    deliverable = _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)
    assert _call("write_file", OTHER) is not None  # stage 1 entered
    set_elapsed(monkeypatch, 160.0)  # 67% of cap: stage 2
    # The deliverable itself is now refused — the rest of the budget
    # belongs to the report.
    out = _call("write_file", {"path": deliverable, "content": "late"})
    assert out is not None and out["action"] == "block"
    assert "stage 2" in out["message"]
    # Checkpoint writes and wrap-up still pass.
    checkpoint = str(tmp_path / "ck" / "t_kill.md")
    assert _call("write_file",
                 {"path": checkpoint, "content": "report"}) is None
    assert _call("kanban_comment", {"task_id": "t_kill"}) is None
    # Still exactly one kill comment for the whole run.
    assert len(spy.kill_comments()) == 1


# ---------------------------------------------------------------------------
# Movement: before half-cap (never triggers) and mid-mode (exits)
# ---------------------------------------------------------------------------


def test_kill_movement_before_half_cap_never_triggers(
        stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    deliverable = _begin_db_run(stubs, monkeypatch, tmp_path)
    # The worker creates the deliverable early (the deliverable-first
    # pattern): baseline was 'missing', the file now exists = movement.
    os.makedirs(os.path.dirname(deliverable), exist_ok=True)
    with open(deliverable, "w", encoding="utf-8") as fh:
        fh.write("skeleton\n")
    set_elapsed(monkeypatch, 130.0)
    assert _call("write_file", OTHER) is None
    set_elapsed(monkeypatch, 200.0)  # 83%: still disarmed
    assert _call("write_file", OTHER) is None
    assert spy.kill_comments() == []


def test_kill_movement_exits_report_mode(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    deliverable = _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)
    assert _call("write_file", OTHER) is not None  # report mode
    assert len(spy.kill_comments()) == 1
    # The worker unsticks itself and writes the deliverable for real.
    os.makedirs(os.path.dirname(deliverable), exist_ok=True)
    with open(deliverable, "w", encoding="utf-8") as fh:
        fh.write("the work itself\n")
    assert _call("write_file", OTHER) is None  # exited
    set_elapsed(monkeypatch, 200.0)
    assert _call("write_file", OTHER) is None  # and stays disarmed


# ---------------------------------------------------------------------------
# Adversarial: provenance and flag discipline
# ---------------------------------------------------------------------------


def test_kill_flag_off_is_inert(stubs, monkeypatch, tmp_path):
    section = _section(tmp_path)
    del section["teeth"]
    patch_section(monkeypatch, section)
    spy = CommentSpy(monkeypatch)
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 200.0)
    assert _call("write_file", OTHER) is None
    assert spy.kill_comments() == []


def test_kill_config_cap_never_engages(stubs, monkeypatch, tmp_path):
    """Provenance, not fraction, decides: a NULL board cap with a
    plugin-configured 240s cap (cap_source == 'config') at 83% elapsed
    is never killed — a configured cap must not sit behind a refusal."""
    section = _section(tmp_path, caps={"time_seconds": 240})
    patch_section(monkeypatch, section)
    spy = CommentSpy(monkeypatch)
    _begin_db_run(stubs, monkeypatch, tmp_path, cap=None)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.cap_source == "config"
    set_elapsed(monkeypatch, 200.0)
    assert _call("write_file", OTHER) is None
    assert spy.kill_comments() == []


def test_kill_no_deliverable_path_no_kill(stubs, monkeypatch, tmp_path):
    """No path, no kill — even with a DB cap and 83% elapsed."""
    patch_section(monkeypatch, _section(tmp_path))
    stubs["config"] = {}
    db = _board(
        tmp_path,
        [("t_kill", 240, "Do the work and report back. No clause here.")])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_kill")
    ac.on_pre_llm_call(session_id="s1", task_id="t_kill",
                       is_first_turn=True)
    set_elapsed(monkeypatch, 200.0)
    assert _call("write_file", OTHER) is None


def test_kill_fail_open_on_internal_error(stubs, monkeypatch, tmp_path):
    """A failure inside the kill check allows the call: the veto must
    never be able to brick a run."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)

    def _boom(_path):
        raise RuntimeError("stat exploded")

    monkeypatch.setattr(ac_core, "_deliverable_snapshot", _boom)
    assert _call("write_file", OTHER) is None

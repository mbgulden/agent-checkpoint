"""Movement signal (teeth package, 2026-11-10 plan).

Each mid-run persist appends one snapshot to ``<run-id>.movement.json``:
deliverable path/size/mtime (or ``missing`` / ``unknown``), the tiers that
fired, and the checkpoint file's size — the mechanical evidence the
review sweep reads. Real file writes (no writer spy): the sizes asserted
here must be the sizes on disk.
"""

import json
import os
import sqlite3

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core


def patch_section(monkey, section):
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


def set_elapsed(monkey, seconds):
    monkey.setattr(ac.LATCHES, "elapsed", lambda _key: float(seconds))


def _board(tmp_path, rows):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL, body TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


def _section(tmp_path, **over):
    section = {
        "caps": {"time_seconds": 120},
        "checkpoint_dir": str(tmp_path / "ck") + "/",
        "allow_tiny_tier1": True,
        "watchdog": False,
        "teeth": {"movement_signal": True},
    }
    section.update(over)
    return section


def _begin(stubs, monkeypatch, tmp_path, task_id="t_move", body=None):
    stubs["config"] = {}
    deliverable = tmp_path / "work" / "deliverable.md"
    if body is None:
        body = (
            "DELIVERABLE-FIRST (hard rule): Your FIRST action is to "
            f"create the deliverable file at {deliverable} with its "
            "full skeleton."
        )
    db = _board(tmp_path, [(task_id, None, body)])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    ac.on_pre_llm_call(session_id="s1", task_id=task_id, is_first_turn=True)
    return deliverable


def _read_movement(tmp_path, task_id="t_move"):
    path = tmp_path / "ck" / f"{task_id}.movement.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_movement_records_present_deliverable(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = _begin(stubs, monkeypatch, tmp_path)
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("findings v1\n", encoding="utf-8")
    set_elapsed(monkeypatch, 61.0)  # tier 1 (50% of 120)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_move")
    doc = _read_movement(tmp_path)
    assert doc["run_id"] == "t_move"
    assert len(doc["snapshots"]) == 1
    snap = doc["snapshots"][0]
    assert snap["tiers"] == [0]
    d = snap["deliverable"]
    assert d["path"] == str(deliverable)
    assert d["status"] == "present"
    assert d["size"] == len("findings v1\n")
    assert d["mtime"] == os.path.getmtime(deliverable)
    ckpt = tmp_path / "ck" / "t_move.md"
    assert snap["checkpoint_size"] == os.path.getsize(ckpt)
    # The checkpoint file itself carries the movement line.
    assert "Movement: deliverable" in ckpt.read_text(encoding="utf-8")


def test_movement_missing_deliverable_records_missing(stubs, monkeypatch, tmp_path):
    """Adversarial (c): the path is parsed but the file was never
    created → the snapshot records 'missing' and the persist itself
    still succeeds (checkpoint file written, movement doc written)."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 61.0)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_move")
    doc = _read_movement(tmp_path)
    snap = doc["snapshots"][0]
    assert snap["deliverable"]["status"] == "missing"
    assert snap["deliverable"]["size"] is None
    assert (tmp_path / "ck" / "t_move.md").exists()


def test_movement_appends_per_tier_and_tracks_growth(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = _begin(stubs, monkeypatch, tmp_path)
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("v1\n", encoding="utf-8")
    set_elapsed(monkeypatch, 61.0)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_move")
    with open(deliverable, "a", encoding="utf-8") as fh:
        fh.write("more findings, second half of the run\n")
    set_elapsed(monkeypatch, 100.0)  # tier 2 (80% of 120)
    ac.on_post_tool_call(tool_name="terminal", session_id="s1", task_id="t_move")
    doc = _read_movement(tmp_path)
    assert [s["tiers"] for s in doc["snapshots"]] == [[0], [1]]
    sizes = [s["deliverable"]["size"] for s in doc["snapshots"]]
    assert sizes[0] == len("v1\n")
    assert sizes[1] > sizes[0]


def test_movement_unknown_path_degrades(stubs, monkeypatch, tmp_path):
    """No parseable path in the body: movement still records, with the
    deliverable status 'unknown' (checkpoint-growth-only mode)."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin(
        stubs,
        monkeypatch,
        tmp_path,
        body="Do the work; no deliverable clause in this body.",
    )
    set_elapsed(monkeypatch, 61.0)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_move")
    doc = _read_movement(tmp_path)
    snap = doc["snapshots"][0]
    assert snap["deliverable"]["status"] == "unknown"
    assert snap["deliverable"]["path"] is None
    assert snap["checkpoint_size"] is not None


def test_movement_flag_off_writes_nothing(stubs, monkeypatch, tmp_path):
    section = _section(tmp_path)
    del section["teeth"]
    patch_section(monkeypatch, section)
    _begin(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 61.0)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_move")
    assert not (tmp_path / "ck" / "t_move.movement.json").exists()
    ckpt = tmp_path / "ck" / "t_move.md"
    assert "Movement:" not in ckpt.read_text(encoding="utf-8")

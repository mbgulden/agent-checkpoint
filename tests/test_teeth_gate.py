"""Final-tier gate (teeth package, 2026-11-10 plan).

The gate refuses non-finishing tool calls once the final tier has fired —
but ONLY for kanban runs whose cap came from the board DB itself
(``cap_source == "db"``) and whose task body named a deliverable path.
The two adversarial cases pin the provenance rule: a configured cap (a)
and the incident shape — NULL board cap + TERMINAL_TIMEOUT in the env
(b) — must NEVER engage the gate, however the latches look.

Hermetic seams follow test_midrun.py: fixed config section, fake clock,
spied comment writer.
"""

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


def _board(tmp_path, rows):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL, body TEXT)")
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
        "teeth": {"gate_final_tier": True},
    }
    section.update(over)
    return section


def _begin_db_run(stubs, monkeypatch, tmp_path, task_id="t_gate", cap=240, body=None):
    """A kanban run whose cap is the board row's own value (source db)."""
    stubs["config"] = {}
    deliverable = str(tmp_path / "work" / "deliverable.md")
    db = _board(
        tmp_path, [(task_id, cap, body if body is not None else _body(deliverable))]
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    # The production worker env carries this; it must never matter.
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    ac.on_pre_llm_call(session_id="s1", task_id=task_id, is_first_turn=True)
    return deliverable


def _fire_final_tier(monkeypatch, task_id="t_gate"):
    """Drive the run past 80% of its 240s cap on a tool boundary."""
    set_elapsed(monkeypatch, 200.0)
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id=task_id)


# ---------------------------------------------------------------------------
# The gate engaging (cap_source == "db", final tier fired, path parsed)
# ---------------------------------------------------------------------------


def test_gate_refuses_out_of_scope_write(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    before = len(spy.comments)
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/home/ubuntu/work/other.txt", "content": "x"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is not None
    assert out["action"] == "block"
    assert "agent-checkpoint gate" in out["message"]
    # The refusal is visible in the run: exactly one more comment, on the
    # task, naming the gate.
    assert len(spy.comments) == before + 1
    ctid, body = spy.comments[-1]
    assert ctid == "t_gate"
    assert "gate" in body


def test_gate_allows_deliverable_write(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": deliverable, "content": "final"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


def test_gate_allows_checkpoint_dir_write(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": str(tmp_path / "ck" / "t_gate.md"), "content": "checkpoint"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


def test_gate_allows_read_class_tools(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    for tool, args in (
        ("read_file", {"path": "/etc/hostname"}),
        ("search_files", {"pattern": "x", "path": "/var"}),
        ("kanban_show", {"task_id": "t_gate"}),
    ):
        assert (
            ac.on_pre_tool_call(
                tool_name=tool, args=args, session_id="s1", task_id="t_gate"
            )
            is None
        )


def test_gate_allows_kanban_wrapup(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    for tool in ("kanban_complete", "kanban_block", "kanban_comment"):
        assert (
            ac.on_pre_tool_call(
                tool_name=tool,
                args={"task_id": "t_gate"},
                session_id="s1",
                task_id="t_gate",
            )
            is None
        )


def test_gate_allows_terminal_touching_deliverable(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    out = ac.on_pre_tool_call(
        tool_name="terminal",
        args={"command": f"cat >> {deliverable}"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


def test_gate_refuses_terminal_unrelated(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    out = ac.on_pre_tool_call(
        tool_name="terminal",
        args={"command": "ls /var/tmp"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is not None and out["action"] == "block"


def test_gate_silent_before_final_tier(stubs, monkeypatch, tmp_path):
    """Tier 1 fired, tier 2 not yet: the gate does not engage."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(stubs, monkeypatch, tmp_path)
    set_elapsed(monkeypatch, 130.0)  # tier 1 (50%) yes, tier 2 (80%) no
    ac.on_post_tool_call(tool_name="read_file", session_id="s1", task_id="t_gate")
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/home/ubuntu/work/other.txt", "content": "x"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


def test_gate_flag_off_is_inert(stubs, monkeypatch, tmp_path):
    section = _section(tmp_path)
    del section["teeth"]
    patch_section(monkeypatch, section)
    _begin_db_run(stubs, monkeypatch, tmp_path)
    _fire_final_tier(monkeypatch)
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/home/ubuntu/work/other.txt", "content": "x"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


def test_gate_no_deliverable_path_no_gate(stubs, monkeypatch, tmp_path):
    """No path, no gate — even with a DB cap and the final tier fired."""
    patch_section(monkeypatch, _section(tmp_path))
    _begin_db_run(
        stubs,
        monkeypatch,
        tmp_path,
        body="Do the work and report back. No clause here.",
    )
    _fire_final_tier(monkeypatch)
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/home/ubuntu/work/other.txt", "content": "x"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


# ---------------------------------------------------------------------------
# Adversarial: provenance, not tier, decides
# ---------------------------------------------------------------------------


def test_adversarial_config_cap_never_gated(stubs, monkeypatch, tmp_path):
    """(a) Gate on + final tier fired + cap_source == 'config' → the
    out-of-scope write is ALLOWED."""
    section = _section(tmp_path, caps={"time_seconds": 240})
    patch_section(monkeypatch, section)
    _begin_db_run(stubs, monkeypatch, tmp_path, cap=None)
    _fire_final_tier(monkeypatch)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.cap_source == "config"
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/home/ubuntu/work/other.txt", "content": "x"},
        session_id="s1",
        task_id="t_gate",
    )
    assert out is None


def test_adversarial_null_cap_incident_shape(stubs, monkeypatch, tmp_path):
    """(b) NULL board cap + TERMINAL_TIMEOUT=180 (the 2026-11-10 incident
    shape): no time axis exists, provenance is not 'db', and the gate
    cannot engage even with the final-tier latch set by hand."""
    stubs["config"] = {"agent": {"max_turns": 90}}
    patch_section(monkeypatch, _section(tmp_path))
    deliverable = str(tmp_path / "work" / "deliverable.md")
    db = _board(tmp_path, [("t_null", None, _body(deliverable))])
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_null")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    ac.on_pre_llm_call(session_id="s1", task_id="t_null", is_first_turn=True)
    caps = ac.discover_caps("s1", ac.load_plugin_config())
    assert caps.time_cap is None
    assert caps.cap_source != "db"
    st = ac.LATCHES.get(ac.SessionLatches.key("s1", "t_null"))
    st["persisted"].append(1)  # even a fired final tier cannot arm it
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/home/ubuntu/work/other.txt", "content": "x"},
        session_id="s1",
        task_id="t_null",
    )
    assert out is None


# ---------------------------------------------------------------------------
# Deliverable-path parsing
# ---------------------------------------------------------------------------


def test_parse_deliverable_clause_form():
    body = _body("/home/ubuntu/work/out/report.md")
    assert ac.parse_deliverable_path(body) == "/home/ubuntu/work/out/report.md"


def test_parse_deliverable_line_form():
    body = "Header\nDeliverable: `/home/ubuntu/work/x.md`\nMore text."
    assert ac.parse_deliverable_path(body) == "/home/ubuntu/work/x.md"


def test_parse_deliverable_no_match():
    assert ac.parse_deliverable_path("Write the report soon.") is None
    assert ac.parse_deliverable_path("") is None
    assert ac.parse_deliverable_path(None) is None

"""Completion-contract gate (teeth package, 0.6.0).

A kanban card that declares a PR contract on a ``Contract:`` line must
not complete while the named PR's required checks are not all green on
the PR's current head. The gate reads PR state through the ``gh`` CLI
(the ``_fetch_pr_checks`` seam, mocked here — no network, no live
board) and FAILS OPEN on any gh error: completion allowed, failure
logged, and a comment notes the contract was not verified. It refuses
only on proven non-green state: red, pending, or no result for a
required check on the current head (stale).

Hermetic seams follow test_teeth_gate.py: fixed config section, real
sqlite board in tmp_path, spied comment writer, mocked gh seam.
"""

import sqlite3
import subprocess

import agent_checkpoint as ac
from agent_checkpoint import core as ac_core


def patch_section(monkey, section):
    monkey.setattr(ac_core, "_read_config_section", lambda: section)


class CommentSpy:
    def __init__(self, monkey):
        self.comments = []  # (task_id, body)
        monkey.setattr(ac_core, "_post_kanban_comment", self._post)

    def _post(self, task_id, body):
        self.comments.append((task_id, body))
        return 4242


class GhSpy:
    """Mocked gh seam: returns a fixed payload (or None = gh failure)
    and records every contract it was asked about."""

    def __init__(self, monkey, payload):
        self.calls = []
        self.payload = payload
        monkey.setattr(ac_core, "_fetch_pr_checks", self._fetch)

    def _fetch(self, contract):
        self.calls.append(contract)
        return self.payload


def _board(tmp_path, rows):
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT, max_runtime_seconds REAL, body TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


CONTRACT_LINE = (
    "Contract: mbgulden/prismatic-engine PR #661; required checks: test, test-extended"
)


def _body(deliverable, contract=CONTRACT_LINE):
    body = (
        "DELIVERABLE-FIRST (hard rule): Your FIRST action is to "
        f"create the deliverable file at {deliverable} with its "
        "full skeleton — every section heading — and save it.\n\n"
    )
    if contract:
        body += contract + "\n"
    return body


def _section(tmp_path, **over):
    section = {
        "checkpoint_dir": str(tmp_path / "ck") + "/",
        "allow_tiny_tier1": True,
        "watchdog": False,
        "teeth": {"contract_gate": True},
    }
    section.update(over)
    return section


def _begin_run(stubs, monkeypatch, tmp_path, task_id="t_contract", body=None):
    stubs["config"] = {}
    deliverable = str(tmp_path / "work" / "deliverable.md")
    db = _board(
        tmp_path,
        [(task_id, 240, body if body is not None else _body(deliverable))],
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    ac.on_pre_llm_call(session_id="s1", task_id=task_id, is_first_turn=True)
    return deliverable


def _complete(task_id="t_contract"):
    return ac.on_pre_tool_call(
        tool_name="kanban_complete",
        args={"task_id": task_id, "summary": "done"},
        session_id="s1",
        task_id=task_id,
    )


def _pr_payload(checks, head="abc123"):
    """checks: list of (name, status, conclusion)."""
    return {
        "headRefOid": head,
        "statusCheckRollup": [
            {"name": n, "status": s, "conclusion": c} for n, s, c in checks
        ],
    }


GREEN = _pr_payload(
    [
        ("test", "COMPLETED", "SUCCESS"),
        ("test-extended", "COMPLETED", "SUCCESS"),
    ]
)
RED = _pr_payload(
    [
        ("test", "COMPLETED", "SUCCESS"),
        ("test-extended", "COMPLETED", "FAILURE"),
    ]
)
PENDING = _pr_payload(
    [
        ("test", "COMPLETED", "SUCCESS"),
        ("test-extended", "IN_PROGRESS", ""),
    ]
)
# The required "test-extended" check has no result on the current head:
# whatever green exists belongs to an older head — stale evidence.
STALE = _pr_payload(
    [
        ("test", "COMPLETED", "SUCCESS"),
    ]
)


# ---------------------------------------------------------------------------
# The gate engaging on proven non-green state
# ---------------------------------------------------------------------------


def test_green_checks_allow_completion(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    gh = GhSpy(monkeypatch, GREEN)
    _begin_run(stubs, monkeypatch, tmp_path)
    assert _complete() is None
    assert len(gh.calls) == 1
    assert gh.calls[0].repo == "mbgulden/prismatic-engine"
    assert gh.calls[0].pr_number == 661
    assert gh.calls[0].required_checks == ("test", "test-extended")


def test_red_check_refuses_completion(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    GhSpy(monkeypatch, RED)
    _begin_run(stubs, monkeypatch, tmp_path)
    out = _complete()
    assert out is not None
    assert out["action"] == "block"
    assert "contract gate" in out["message"]
    assert "test-extended" in out["message"]
    # The refusal is visible on the task.
    assert spy.comments
    ctid, body = spy.comments[-1]
    assert ctid == "t_contract"
    assert "contract gate" in body


def test_pending_check_refuses_completion(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    GhSpy(monkeypatch, PENDING)
    _begin_run(stubs, monkeypatch, tmp_path)
    out = _complete()
    assert out is not None and out["action"] == "block"
    assert "test-extended" in out["message"]


def test_stale_head_refuses_completion(stubs, monkeypatch, tmp_path):
    """A required check with no result on the current head is stale
    evidence — the gate refuses rather than trusting an older head."""
    patch_section(monkeypatch, _section(tmp_path))
    GhSpy(monkeypatch, STALE)
    _begin_run(stubs, monkeypatch, tmp_path)
    out = _complete()
    assert out is not None and out["action"] == "block"
    assert "stale" in out["message"]


# ---------------------------------------------------------------------------
# Fail-open: gh errors never refuse
# ---------------------------------------------------------------------------


def test_gh_failure_allows_completion_with_comment(stubs, monkeypatch, tmp_path):
    """gh seam returns None (missing gh / network / unparsable): the
    completion is ALLOWED, and a comment records that the contract
    could not be verified."""
    patch_section(monkeypatch, _section(tmp_path))
    spy = CommentSpy(monkeypatch)
    GhSpy(monkeypatch, None)
    _begin_run(stubs, monkeypatch, tmp_path)
    assert _complete() is None
    assert spy.comments
    _ctid, body = spy.comments[-1]
    assert "could not verify" in body
    assert "ALLOWED" in body


def test_fetch_pr_checks_missing_gh_binary(monkeypatch):
    """The fetch seam itself: gh not installed → None (fail-open)."""

    def _boom(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(ac_core.subprocess, "run", _boom)
    contract = ac.parse_contract(CONTRACT_LINE)
    assert ac_core._fetch_pr_checks(contract) is None


def test_fetch_pr_checks_nonzero_exit(monkeypatch):
    def _fail(*a, **k):
        return subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="not logged in"
        )

    monkeypatch.setattr(ac_core.subprocess, "run", _fail)
    contract = ac.parse_contract(CONTRACT_LINE)
    assert ac_core._fetch_pr_checks(contract) is None


def test_fetch_pr_checks_unparsable_output(monkeypatch):
    def _junk(*a, **k):
        return subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="<html>oops</html>", stderr=""
        )

    monkeypatch.setattr(ac_core.subprocess, "run", _junk)
    contract = ac.parse_contract(CONTRACT_LINE)
    assert ac_core._fetch_pr_checks(contract) is None


# ---------------------------------------------------------------------------
# Untouched cards: no contract, flag off, other tools
# ---------------------------------------------------------------------------


def test_no_contract_line_untouched(stubs, monkeypatch, tmp_path):
    """No Contract line → the gate never even reads the PR."""
    patch_section(monkeypatch, _section(tmp_path))
    gh = GhSpy(monkeypatch, RED)
    deliverable = str(tmp_path / "work" / "deliverable.md")
    _begin_run(stubs, monkeypatch, tmp_path, body=_body(deliverable, contract=None))
    assert _complete() is None
    assert gh.calls == []


def test_flag_off_untouched_even_with_red_pr(stubs, monkeypatch, tmp_path):
    section = _section(tmp_path)
    del section["teeth"]
    patch_section(monkeypatch, section)
    gh = GhSpy(monkeypatch, RED)
    _begin_run(stubs, monkeypatch, tmp_path)
    assert _complete() is None
    assert gh.calls == []


def test_gate_only_intercepts_kanban_complete(stubs, monkeypatch, tmp_path):
    patch_section(monkeypatch, _section(tmp_path))
    gh = GhSpy(monkeypatch, RED)
    _begin_run(stubs, monkeypatch, tmp_path)
    out = ac.on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/tmp/x", "content": "y"},
        session_id="s1",
        task_id="t_contract",
    )
    assert out is None
    assert gh.calls == []


# ---------------------------------------------------------------------------
# Inert-state proof: with the flag off and default config, a contract-
# carrying card behaves exactly as under 0.5.0 (no such gate existed).
# ---------------------------------------------------------------------------


def test_inert_state_default_config(stubs, monkeypatch, tmp_path):
    cfg = ac.PluginConfig()
    assert cfg.teeth.contract_gate is False
    assert cfg.enabled_axes == ("time", "turns")
    # A fully default section parses to the same defaults.
    patch_section(monkeypatch, {})
    loaded = ac.load_plugin_config()
    assert loaded.teeth.contract_gate is False
    assert loaded.enabled_axes == ("time", "turns")
    # And behaviorally: red PR, contract card, default section → the
    # completion passes with no gh read at all.
    gh = GhSpy(monkeypatch, RED)
    _begin_run(stubs, monkeypatch, tmp_path)
    assert _complete() is None
    assert gh.calls == []


# ---------------------------------------------------------------------------
# Parsing + pure state evaluation
# ---------------------------------------------------------------------------


def test_parse_contract_exact_shape():
    c = ac.parse_contract("Some intro.\n" + CONTRACT_LINE + "\nMore.")
    assert c is not None
    assert c.repo == "mbgulden/prismatic-engine"
    assert c.pr_number == 661
    assert c.required_checks == ("test", "test-extended")


def test_parse_contract_no_match():
    assert ac.parse_contract("No contract here.") is None
    assert ac.parse_contract("") is None
    assert ac.parse_contract(None) is None
    # Missing the required-checks list is not a contract.
    assert ac.parse_contract("Contract: mbgulden/prismatic-engine PR #661") is None
    assert (
        ac.parse_contract(
            "Contract: mbgulden/prismatic-engine PR #661; required checks: "
        )
        is None
    )


def test_check_state_pure():
    contract = ac.parse_contract(CONTRACT_LINE)
    assert ac._contract_check_state(contract, GREEN) == ("green", [])
    state, problems = ac._contract_check_state(contract, RED)
    assert state == "not_green"
    assert any("test-extended" in p for p in problems)
    state, _ = ac._contract_check_state(contract, PENDING)
    assert state == "not_green"
    state, problems = ac._contract_check_state(contract, STALE)
    assert state == "not_green"
    assert any("stale" in p for p in problems)

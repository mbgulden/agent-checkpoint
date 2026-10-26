"""Shared fixtures for the agent-checkpoint unit suite.

Stubs the ``hermes_cli`` import surface the plugin pokes (config load, goal
load, kanban DB path) ONLY when a test opts in via the ``use_stub`` marker
mechanism (setting ``USE_STUBS`` on this module). This keeps the suite
hermetic and deterministic regardless of whether the live Hermes fork is
importable in the test shell.
"""
import os
import sys
import types

import pytest

# Make the plugin package importable as ``agent_checkpoint`` no matter where
# the suite is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import agent_checkpoint as ac  # noqa: E402

USE_STUBS = {"on": False, "config": {}, "goals": {}, "db_path": None}

_stubbed = set()


def _ensure_stub(name, module):
    if name not in _stubbed:
        sys.modules[name] = module
        _stubbed.add(name)


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """Install hermes_cli stubs when a test flips USE_STUBS['on']."""
    yield
    # No teardown: stubs are plain dicts in sys.modules; per-test values are
    # reset below in a fixture finalizer to avoid cross-test bleed.
    for k in list(sys.modules):
        if k == "hermes_cli" or k.startswith("hermes_cli."):
            del sys.modules[k]
    _stubbed.clear()
    USE_STUBS["on"] = False
    USE_STUBS["config"] = {}
    USE_STUBS["goals"] = {}
    USE_STUBS["db_path"] = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Hermetic env: strip kanban/terminal vars the plugin reads from env.
    Tests that need a dialect set the vars explicitly via monkeypatch."""
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "TERMINAL_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_latches():
    """Fresh per-session latches for every test — no state bleed."""
    ac.LATCHES._state.clear()
    yield


@pytest.fixture
def stubs(monkeypatch):
    """Enable the hermes_cli stub surface for the calling test."""
    USE_STUBS["on"] = True

    hc = types.ModuleType("hermes_cli")
    cfg_mod = types.ModuleType("hermes_cli.config")
    goals_mod = types.ModuleType("hermes_cli.goals")
    kanban_mod = types.ModuleType("hermes_cli.kanban_db")

    def _load_config_readonly():
        return USE_STUBS["config"]

    def _load_goal(session_id):
        return USE_STUBS["goals"].get(session_id)

    def _kanban_db_path(board=None):
        return USE_STUBS["db_path"]

    cfg_mod.load_config_readonly = _load_config_readonly
    goals_mod.load_goal = _load_goal
    kanban_mod.kanban_db_path = _kanban_db_path
    hc.config = cfg_mod
    hc.goals = goals_mod
    hc.kanban_db = kanban_mod
    _ensure_stub("hermes_cli", hc)
    _ensure_stub("hermes_cli.config", cfg_mod)
    _ensure_stub("hermes_cli.goals", goals_mod)
    _ensure_stub("hermes_cli.kanban_db", kanban_mod)
    yield USE_STUBS

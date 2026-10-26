"""Test-only package shim for the agent-checkpoint unit suite.

The Hermes plugin loader execs the plugin DIRECTORY's ``__init__.py`` as the
plugin module (``hermes_cli/plugins.py:5200`` — ``init_file = plugin_dir /
"__init__.py"``; ``5202`` raises ``FileNotFoundError("No __init__.py in ...")``
if it is absent), so the shipped layout keeps ALL plugin code in the top-level
``__init__.py`` of the ``agent-checkpoint/`` directory. A top-level hyphenated
directory is not directly importable, which makes that layout un-testable with a
plain ``import agent_checkpoint``.

This package loads that SAME shipped ``__init__.py`` and re-exports it, so the
suite imports the EXACT file the loader will exec — there is no parallel copy to
keep in sync.

    (Build B0, 2026-10-22: the old design shipped a byte-identical
    ``core.py`` copy plus this ``from . import core`` shim, guarded by an
    ``md5sum`` invariant. The copy is removed; the invariant is now structural —
    the tests import the shipped file by path, so they can never test a
    different file than what ships.)

It is NOT part of what the Hermes loader sees: when the plugin is installed
under ``<HERMES_HOME>/plugins/agent-checkpoint/``, the loader imports the
directory's ``__init__.py`` directly.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# The shipped plugin module is the top-level ``__init__.py`` of the plugin
# directory, one level up from this ``agent_checkpoint/`` test package.
_SHIPPED_INIT = os.path.join(os.path.dirname(_HERE), "__init__.py")

_spec = importlib.util.spec_from_file_location("agent_checkpoint.core", _SHIPPED_INIT)
core = importlib.util.module_from_spec(_spec)
sys.modules["agent_checkpoint.core"] = core  # so ``import agent_checkpoint.core`` also resolves
_spec.loader.exec_module(core)

# Bind every non-dunder name from the real module (star-import would skip
# underscore-prefixed names like ``_fmt_time`` / ``_read_config_section``).
for _name in dir(core):  # noqa: C402
    if not _name.startswith("__"):
        globals()[_name] = getattr(core, _name)  # noqa: A001

__all__ = sorted(n for n in dir(core) if not n.startswith("__")) + ["core"]

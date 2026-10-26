"""agent-checkpoint — durable progress checkpoints for budget-capped agent runs.

A Hermes plugin. Registers ``pre_llm_call`` plus ``pre_tool_call`` /
``post_tool_call`` / ``on_session_end`` hooks (worker/CLI/gateway processes
alike) via ``register(ctx)``. At configured fractions of a run's turn and/or
time budget it appends a short instruction to the current turn's user
message:

* **Tier 1** (default 50% of the effective budget): persist everything found
  so far into a named durable checkpoint file, then continue at normal pace.
* **Tier 2** (default 80% of the time budget / 90% of the turn budget): stop
  expanding, bring the checkpoint file and deliverable to final state, post
  the summary, verify the file exists.

Design constraints (locked plan 2026-10-21, ``AGENT-CHECKPOINT-PLAN.md``):

* **Fail-open everywhere.** Any error (config, DB, math) returns ``None`` and
  never breaks a turn.
* **Latched per tier, per session.** Each tier fires at most once per run;
  state is keyed by session/run id in a process-local dict so concurrent
  sessions in one gateway process never leak into each other.
* **No caps → no budget warnings, ever.** The plugin never warns against a
  budget that does not exist: with no discoverable cap it injects nothing.
  Cap discovery is per dialect — kanban: the board cap, or the
  ``TERMINAL_TIMEOUT`` derivation; goal: the goal's ``max_turns``, then the
  profile ``agent.max_turns``; chat: plugin-configured caps (``caps``) or
  ``agent.run_budget_seconds`` only (a profile ``agent.max_turns`` alone
  never arms a chat session). Since 0.3.0 a capless run is still not
  silent on disk: the watchdog persists a checkpoint every
  ``uncapped_interval_seconds`` (default 1800; 0 disables the fallback)
  so a long uncapped run leaves a durable record too.
* **The plugin writes nothing at prologues.** It names the checkpoint file
  in the injected text; the agent writes it.
* **Mid-run persistence (2026-10-25 fix).** Turn prologues are not the only
  evaluation point: the tool-call hooks evaluate the same tier plan at tool
  boundaries inside a run, and a per-session watchdog thread covers a run
  parked inside one long tool call. On a mid-run crossing the plugin itself
  persists — it writes the checkpoint file and posts the checkpoint as a
  kanban comment (author ``agent-checkpoint``) — because a kanban run is a
  single turn and may never see another prologue. Prologue injection is
  unchanged, and a mid-run persist never suppresses the later injection
  (separate latches).

Hook contract (verified against the fork source): ``pre_llm_call`` fires once
per ``run_conversation`` (per user turn / run), before the tool loop, with
``session_id, task_id, turn_id, user_message, conversation_history,
is_first_turn, model, platform, parent_session_id, sender_id``. The agent
object is NOT passed, so the plugin keeps its own per-session turn counter,
reset on ``is_first_turn``. A callback returning ``{"context": text}`` has
the text appended to the current turn's user message (prompt-cache-safe;
never the system prompt).

Loader contract (verified against ``hermes_cli/plugins.py`` and the
``hermes-kanban-stream`` reference plugin): a plugin package must expose
``register(ctx)``; ``ctx.register_hook(name, fn)`` wires the callback. The
manifest declares ``provides_hooks``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (plan section 2 — decided, do not relitigate)
# ---------------------------------------------------------------------------

DEFAULT_TIER_1_FRACTION = 0.5          # 50% of the effective budget
DEFAULT_TIER_2_TIME_FRACTION = 0.8     # 80% of the time budget
DEFAULT_TIER_2_TURN_FRACTION = 0.9     # 90% of the turn budget
DEFAULT_MODE = "snapshot"              # snapshot | notify | log-only
DEFAULT_CHECKPOINT_DIR = "$HERMES_HOME/agent-checkpoints/"

# Tiny-cap collapse (plan section 3): if tier 1 and tier 2 would land within
# ~2 turns of each other (turn cap < 10 or time cap < 5 min), suppress tier 1
# unless the operator opts in.
TINY_TURN_CAP = 10
TINY_TIME_CAP_SECONDS = 5 * 60
PENULTIMATE_TURNS = 2  # tier 2 fires no later than the penultimate turn

# Mid-run persistence (2026-10-25 fix): evaluate tiers at tool-call
# boundaries and from a watchdog thread; on a mid-run crossing the plugin
# itself persists (checkpoint file + kanban comment). Default ON per
# Michael's directive — a capped run must not cross silently.
DEFAULT_MID_RUN_PERSIST = True
DEFAULT_WATCHDOG = True
DEFAULT_WATCHDOG_POLL_SECONDS = 10.0
WATCHDOG_GRACE_SECONDS = 180.0   # watchdog exits at cap + this grace

# Uncapped-run fallback (0.3.0): a run with NO discoverable cap on either
# axis gets a watchdog-persisted checkpoint every this-many elapsed
# seconds, so it still leaves a durable record. 1800s = the tier-1 point
# of the fleet's standard 3600s cap: a capless run's first record lands
# no later than a capped run's. 0 disables the fallback entirely.
DEFAULT_UNCAPPED_INTERVAL_SECONDS = 1800.0
COMMENT_MAX_CHARS = 4000         # kanban comment body cap for persists
COMMENT_AUTHOR = "agent-checkpoint"

# Bound on latched sessions in one process (gateway hosts many).
MAX_LATCHED_SESSIONS = 512

VALID_MODES = ("snapshot", "notify", "log-only")
VALID_DIALECTS = ("kanban", "goal", "chat")

PLUGIN_ID = "agent-checkpoint"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapConfig:
    """Plugin-supplied caps for runs whose native cap is undiscoverable."""
    time_seconds: Optional[float] = None
    turns: Optional[int] = None


@dataclass(frozen=True)
class PluginConfig:
    mode: str = DEFAULT_MODE
    tier_fractions: list = field(default_factory=lambda: [DEFAULT_TIER_1_FRACTION])
    tier2_time_fraction: float = DEFAULT_TIER_2_TIME_FRACTION
    tier2_turn_fraction: float = DEFAULT_TIER_2_TURN_FRACTION
    min_budget_floor_turns: int = 0        # axis below this is ignored
    min_budget_floor_seconds: float = 0.0  # axis below this is ignored
    enabled_axes: tuple = ("time", "turns")  # per-axis enable/disable (plan §2)
    checkpoint_dir: str = DEFAULT_CHECKPOINT_DIR
    allow_tiny_tier1: bool = False         # opt-in: fire tier 1 even on tiny caps
    caps: CapConfig = field(default_factory=CapConfig)
    tier1_template: Optional[str] = None   # None → shipped default
    tier2_template: Optional[str] = None
    enabled: bool = True
    mid_run_persist: bool = DEFAULT_MID_RUN_PERSIST
    watchdog: bool = DEFAULT_WATCHDOG
    watchdog_poll_seconds: float = DEFAULT_WATCHDOG_POLL_SECONDS
    uncapped_interval_seconds: float = DEFAULT_UNCAPPED_INTERVAL_SECONDS


def _default_config() -> PluginConfig:
    return PluginConfig()


def _read_config_section() -> dict:
    """Locate the plugin's settings, fail-open.

    Canonical namespaced path first: ``plugins.entries.agent-checkpoint.
    settings``. Fallback: top-level ``agent_checkpoint:`` section. Either
    form is documented in the README.
    """
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly() or {}
    if not isinstance(cfg, dict):
        return {}
    plugins = cfg.get("plugins")
    entries = (plugins or {}).get("entries") if isinstance(plugins, dict) else None
    entry = (entries or {}).get(PLUGIN_ID) if isinstance(entries, dict) else None
    if isinstance(entry, dict):
        for sub in ("settings", "config"):
            val = entry.get(sub)
            if isinstance(val, dict) and val:
                return val
    section = cfg.get("agent_checkpoint")
    return section if isinstance(section, dict) else {}


def load_plugin_config() -> PluginConfig:
    """Read the plugin config (fail-open: any error → defaults)."""
    try:
        section = _read_config_section()
        if not section:
            return _default_config()
        mode = str(section.get("mode", DEFAULT_MODE))
        if mode not in VALID_MODES:
            logger.warning("agent-checkpoint: bad mode %r; using default", mode)
            mode = DEFAULT_MODE
        fracs = section.get("tier_fractions", [DEFAULT_TIER_1_FRACTION])
        if not isinstance(fracs, (list, tuple)) or not fracs:
            fracs = [DEFAULT_TIER_1_FRACTION]
        fracs = [float(f) for f in fracs]
        caps_raw = section.get("caps") or {}
        if not isinstance(caps_raw, dict):
            caps_raw = {}
        caps = CapConfig(
            time_seconds=(
                float(caps_raw["time_seconds"])
                if caps_raw.get("time_seconds") is not None else None
            ),
            turns=(
                int(caps_raw["turns"]) if caps_raw.get("turns") is not None else None
            ),
        )
        cp_dir = str(section.get("checkpoint_dir", DEFAULT_CHECKPOINT_DIR))
        if cp_dir.startswith("$"):
            cp_dir = os.path.expandvars(cp_dir)
        ax_raw = section.get("enabled_axes")
        if ax_raw is None:
            ax = ("time", "turns")
        elif isinstance(ax_raw, (list, tuple)) and ax_raw:
            ax = tuple(str(a) for a in ax_raw)
            bad = [a for a in ax if a not in ("time", "turns")]
            if bad:
                logger.warning("agent-checkpoint: bad enabled_axes %s; ignoring", ax_raw)
                ax = ("time", "turns")
        else:
            ax = ("time", "turns")
        return PluginConfig(
            mode=mode,
            tier_fractions=fracs,
            tier2_time_fraction=float(section.get(
                "tier2_time_fraction", DEFAULT_TIER_2_TIME_FRACTION)),
            tier2_turn_fraction=float(section.get(
                "tier2_turn_fraction", DEFAULT_TIER_2_TURN_FRACTION)),
            min_budget_floor_turns=int(section.get("min_budget_floor_turns", 0)),
            min_budget_floor_seconds=float(
                section.get("min_budget_floor_seconds", 0.0)),
            enabled_axes=ax,
            checkpoint_dir=cp_dir,
            allow_tiny_tier1=bool(section.get("allow_tiny_tier1", False)),
            caps=caps,
            tier1_template=section.get("tier1_template"),
            tier2_template=section.get("tier2_template"),
            enabled=bool(section.get("enabled", True)),
            mid_run_persist=bool(section.get(
                "mid_run_persist", DEFAULT_MID_RUN_PERSIST)),
            watchdog=bool(section.get("watchdog", DEFAULT_WATCHDOG)),
            watchdog_poll_seconds=max(0.05, float(section.get(
                "watchdog_poll_seconds", DEFAULT_WATCHDOG_POLL_SECONDS))),
            uncapped_interval_seconds=max(0.0, float(section.get(
                "uncapped_interval_seconds",
                DEFAULT_UNCAPPED_INTERVAL_SECONDS))),
        )
    except Exception as exc:  # fail-open: malformed config → defaults
        logger.warning("agent-checkpoint: config load failed (%s); using defaults", exc)
        return _default_config()


# ---------------------------------------------------------------------------
# Cap discovery (plan section 3 fallback chain)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunCaps:
    """The effective budgets for one run, after the full discovery chain."""
    turns_cap: Optional[int] = None
    time_cap: Optional[float] = None
    dialect: str = "chat"   # kanban | goal | chat


def _kanban_time_cap() -> Optional[float]:
    """Kanban task cap: board DB ``max_runtime_seconds`` via HERMES_KANBAN_TASK,
    or the ``TERMINAL_TIMEOUT + 30`` derivation (the worker's terminal timeout
    is ``max_runtime - 30s``, so the cap ≈ TERMINAL_TIMEOUT + 30)."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if task_id:
        try:
            db_path = os.environ.get("HERMES_KANBAN_DB")
            if not db_path:
                from hermes_cli.kanban_db import kanban_db_path
                db_path = str(kanban_db_path())
            if db_path and os.path.exists(db_path):
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    row = con.execute(
                        "SELECT max_runtime_seconds FROM tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                finally:
                    con.close()
                if row and row[0]:
                    return float(row[0])
        except Exception as exc:
            logger.debug("agent-checkpoint: kanban DB read failed: %s", exc)
    # Fallback derivation: TERMINAL_TIMEOUT + grace.
    tt = os.environ.get("TERMINAL_TIMEOUT")
    if tt:
        try:
            return float(tt) + 30.0
        except (TypeError, ValueError):
            pass
    return None


def _kanban_db_path() -> Optional[str]:
    """Resolve the kanban DB path the same way cap discovery does."""
    db_path = os.environ.get("HERMES_KANBAN_DB")
    if not db_path:
        try:
            from hermes_cli.kanban_db import kanban_db_path
            db_path = str(kanban_db_path())
        except Exception:
            return None
    return db_path or None


def _post_kanban_comment(task_id: str, body: str) -> Optional[int]:
    """Post a comment on a kanban task, mirroring the fork's own write path
    (``hermes_cli/kanban_db.py`` ``add_comment``) exactly: same tables, same
    strip semantics, same ``commented`` event row, one ``BEGIN IMMEDIATE``
    transaction. Returns the new comment id, or None when the task/DB is
    unavailable. Fail-open: raises nothing."""
    try:
        if not task_id or not body or not body.strip():
            return None
        db_path = _kanban_db_path()
        if not db_path or not os.path.exists(db_path):
            return None
        author = COMMENT_AUTHOR
        now = int(time.time())
        con = sqlite3.connect(db_path, timeout=5.0)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("BEGIN IMMEDIATE")
            try:
                if not con.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                ).fetchone():
                    con.execute("ROLLBACK")
                    return None
                cur = con.execute(
                    "INSERT INTO task_comments "
                    "(task_id, author, body, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (task_id, author.strip(), body.strip(), now),
                )
                payload = json.dumps(
                    {"author": author, "len": len(body)},
                    ensure_ascii=False)
                con.execute(
                    "INSERT INTO task_events "
                    "(task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, None, "commented", payload,
                     int(time.time())),
                )
                con.execute("COMMIT")
                return int(cur.lastrowid or 0) or None
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            con.close()
    except Exception as exc:
        logger.warning(
            "agent-checkpoint: kanban comment post failed for %s: %s",
            task_id, exc)
        return None


def _goal_turns_cap(session_id: str) -> Optional[int]:
    """Goal-mode ``goals.max_turns`` via the public ``load_goal`` accessor."""
    try:
        from hermes_cli.goals import load_goal
        state = load_goal(session_id)
        if state is not None and state.max_turns:
            return int(state.max_turns)
    except Exception as exc:
        logger.debug("agent-checkpoint: goal read failed: %s", exc)
    return None


def _profile_turns_cap() -> Optional[int]:
    """Profile ``agent.max_turns`` from config.yaml."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        agent = cfg.get("agent") or {}
        v = agent.get("max_turns")
        if v is not None:
            return int(v)
    except Exception as exc:
        logger.debug("agent-checkpoint: profile config read failed: %s", exc)
    return None


def _run_budget_seconds() -> Optional[float]:
    """``agent.run_budget_seconds`` armed via config (the chat arming path)."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        v = (cfg.get("agent") or {}).get("run_budget_seconds")
        if v:
            return float(v)
    except Exception:
        pass
    return None


def detect_dialect(session_id: str) -> str:
    """Kanban worker / goal session / plain chat — from in-process signals."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return "kanban"
    try:
        from hermes_cli.goals import load_goal
        if load_goal(session_id) is not None:
            return "goal"
    except Exception:
        pass
    return "chat"


def discover_caps(session_id: str, cfg: PluginConfig) -> RunCaps:
    """Full cap-discovery chain (plan section 3).

    kanban board cap → goal max_turns → profile agent.max_turns →
    plugin-configured caps → no caps (caller no-ops).
    """
    dialect = detect_dialect(session_id)
    turns_cap: Optional[int] = None
    time_cap: Optional[float] = None

    if dialect == "kanban":
        time_cap = _kanban_time_cap()
        # Turn axis for kanban: profile cap, then plugin caps (below).
        turns_cap = _profile_turns_cap()
    elif dialect == "goal":
        # Goal dialect is turn-axis only (no in-process wall-clock budget).
        turns_cap = _goal_turns_cap(session_id) or _profile_turns_cap()
    # Chat: no native budget. Armed only via plugin-configured caps or an
    # explicit ``agent.run_budget_seconds`` (read below as a time axis).

    if time_cap is None:
        time_cap = cfg.caps.time_seconds
    if turns_cap is None:
        turns_cap = cfg.caps.turns
    if dialect == "chat" and time_cap is None:
        time_cap = _run_budget_seconds()
    if dialect == "goal":
        time_cap = None  # no in-process time axis for goal sessions

    return RunCaps(turns_cap=turns_cap, time_cap=time_cap, dialect=dialect)


# ---------------------------------------------------------------------------
# Tier math (plan section 2)
# ---------------------------------------------------------------------------

@dataclass
class TierPlan:
    """Which tiers should fire this tick, given usage and latches."""
    fire_tiers: list = field(default_factory=list)
    effective_fraction: float = 0.0
    # Render context (populated only for axes that exist / are enabled):
    turns_used: Optional[int] = None
    turns_cap: Optional[int] = None
    turns_left: Optional[int] = None
    time_used: Optional[float] = None
    time_cap: Optional[float] = None
    time_left: Optional[float] = None


def compute_tier_plan(
    cfg: PluginConfig,
    caps: RunCaps,
    turns_used: int,
    elapsed: float,
    fired_tiers: list,
) -> Optional[TierPlan]:
    """Pure tier math — the unit under test. Returns None when nothing may fire.

    Tier semantics (plan sections 2-3):
    * **Tier 1** (fraction list[0], default 0.5) fires at the *effective*
      fraction = max(time fraction, turn fraction) across the axes that exist
      (after per-axis enable/disable and minimum-budget floors).
    * **Tier 2** (finalize) fires when *either* axis crosses its own threshold:
      default 80% of the time budget and/or 90% of the turn budget. If the
      operator configured a two-element fraction list, the second element
      replaces both per-axis thresholds.
    * **Tiny-cap collapse:** if the caps are small enough that tier 1 and tier
      2 would land within ~2 turns of each other (turn cap < 10 or time cap
      < 5 min), tier 1 is suppressed unless ``allow_tiny_tier1`` is set.
    * **Penultimate-turn rule:** tier 2 fires no later than the penultimate
      turn (Hermes' own rule, generalized).
    * A 1-turn run gets no checkpoint at all.
    """
    try:
        if caps.turns_cap is None and caps.time_cap is None:
            return None
        if caps.turns_cap is not None and int(caps.turns_cap) < 2:
            return None  # 1-turn run: no checkpoint

        fracs = list(cfg.tier_fractions)
        # Tier 2 per-axis thresholds: a 2-element fraction list makes the
        # second element the tier-2 fraction for both axes; otherwise the
        # locked asymmetric defaults (0.8 time / 0.9 turns) govern.
        if len(fracs) >= 2:
            t2_time_frac = float(fracs[1])
            t2_turn_frac = float(fracs[1])
        else:
            t2_time_frac = cfg.tier2_time_fraction
            t2_turn_frac = cfg.tier2_turn_fraction

        has_time = caps.time_cap is not None and float(caps.time_cap) > 0
        has_turns = caps.turns_cap is not None and int(caps.turns_cap) >= 2
        # Per-axis enable/disable (plan §2).
        if has_time and "time" not in cfg.enabled_axes:
            has_time = False
        if has_turns and "turns" not in cfg.enabled_axes:
            has_turns = False
        # Minimum-budget floors: an axis below the floor does not count.
        if has_time and float(caps.time_cap) < cfg.min_budget_floor_seconds:
            has_time = False
        if has_turns and int(caps.turns_cap) < cfg.min_budget_floor_turns:
            has_turns = False
        if not has_time and not has_turns:
            return None

        time_frac = (elapsed / float(caps.time_cap)) if has_time else None
        turn_frac = (turns_used / int(caps.turns_cap)) if has_turns else None

        plan = TierPlan()
        if has_turns:
            plan.turns_used = int(turns_used)
            plan.turns_cap = int(caps.turns_cap)
            plan.turns_left = max(0, int(caps.turns_cap) - int(turns_used))
        if has_time:
            plan.time_used = elapsed
            plan.time_cap = float(caps.time_cap)
            plan.time_left = max(0.0, float(caps.time_cap) - elapsed)

        # --- Tier 1: effective fraction = max of the axis fractions ---
        axis_fracs = [f for f in (time_frac, turn_frac) if f is not None]
        effective = max(axis_fracs) if axis_fracs else 0.0
        plan.effective_fraction = effective
        t1_frac = float(fracs[0])
        tiny = (
            (has_turns and int(caps.turns_cap) < TINY_TURN_CAP)
            or (has_time and float(caps.time_cap) < TINY_TIME_CAP_SECONDS)
        )
        t1_suppressed = tiny and not cfg.allow_tiny_tier1
        if (0 < t1_frac <= 1.0 and effective >= t1_frac
                and 0 not in fired_tiers and not t1_suppressed):
            plan.fire_tiers.append(0)

        # --- Tier 2: either axis crosses its own threshold ---
        t2_due = False
        if has_time and time_frac is not None and time_frac >= t2_time_frac:
            t2_due = True
        if has_turns and turn_frac is not None and turn_frac >= t2_turn_frac:
            t2_due = True
        # Penultimate-turn rule: tier 2 must fire no later than the
        # penultimate turn — if we're at/inside the last turns, fire it now
        # rather than letting the run end without the finalize directive.
        if has_turns:
            remaining = int(caps.turns_cap) - int(turns_used)
            if remaining <= PENULTIMATE_TURNS:
                t2_due = True
        if t2_due and 1 not in fired_tiers:
            plan.fire_tiers.append(1)

        return plan
    except Exception as exc:  # fail-open: never let math break a turn
        logger.warning("agent-checkpoint: tier math failed (%s); no-op", exc)
        return None


# ---------------------------------------------------------------------------
# Text rendering (section 5 templates; render only the axes that exist)
# ---------------------------------------------------------------------------

def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _build_default_text(
    mode: str,
    tier_idx: int,
    plan: TierPlan,
    checkpoint_path: str,
) -> Optional[str]:
    """Build the shipped default text (plan §5, verbatim) for the axes that
    exist. A dialect with a single axis (e.g. goal = turns only) renders the
    same sentences with that axis — the absent axis's clause is simply not
    there, so nothing is ever printed as ``None``."""
    parts_t1 = []
    if plan.turns_used is not None:
        parts_t1.append(f"{plan.turns_used} of {plan.turns_cap} turns "
                        f"({plan.turns_left} left)")
    if plan.time_used is not None:
        parts_t1.append(f"{_fmt_time(plan.time_used)} of "
                        f"{_fmt_time(plan.time_cap)} "
                        f"({_fmt_time(plan.time_left)} left)")
    rem = []
    if plan.turns_left is not None:
        rem.append(f"{plan.turns_left} turns")
    if plan.time_left is not None:
        rem.append(_fmt_time(plan.time_left))
    if not parts_t1 and not rem:
        return None  # no axis survived enable/disable/floors

    if tier_idx == 0:
        if mode == "notify":
            n_parts = []
            if plan.turns_used is not None:
                n_parts.append(f"{plan.turns_used}/{plan.turns_cap} turns used "
                               f"({plan.turns_left} left)")
            if plan.time_used is not None:
                n_parts.append(f"{_fmt_time(plan.time_used)}/"
                               f"{_fmt_time(plan.time_cap)} elapsed "
                               f"({_fmt_time(plan.time_left)} left)")
            return ("Budget checkpoint: " + "; ".join(n_parts)) if n_parts else None
        # snapshot (default): plan §5 tier 1, verbatim, both-axes shape.
        return (
            "Budget checkpoint: you have used " + " and ".join(parts_t1)
            + " — about half your budget remains; keep working at normal pace. "
            + f"Before continuing: write everything found so far into "
            + f"{checkpoint_path}, including what remains to be done. "
            + "Then continue the task."
        )
    # tier 2
    head = "Final budget checkpoint: about " + " / ".join(rem) + " remain."
    if mode == "notify":
        return head
    # snapshot (default): plan §5 tier 2, verbatim, both-axes shape.
    return (
        head
        + " Do not start new subtasks or new lines of investigation. "
        + "Finish the step in progress, then: (1) bring "
        + f"{checkpoint_path}"
        + " (and your deliverable, if the task names one) to its final state "
        + "for this run — if the work is unfinished, mark it INCOMPLETE and "
        + "state the exact next step; (2) post your summary through the "
        + "normal completion/blocking channel; (3) verify the file exists "
        + "before you finish."
    )


def render_checkpoint_text(
    cfg: PluginConfig,
    plan: TierPlan,
    tier_idx: int,
    checkpoint_path: str,
) -> Optional[str]:
    """Render the injected text for one tier (fail-open → None).

    Shipped defaults use the axis-aware builder (correct for single-axis
    dialects). Operator-supplied templates get placeholder substitution with
    missing-axis clauses stripped.
    """
    try:
        if tier_idx == 0:
            tmpl = cfg.tier1_template
        else:
            tmpl = cfg.tier2_template
        if tmpl is None:
            return _build_default_text(cfg.mode, tier_idx, plan, checkpoint_path)

        ctx: Dict[str, Any] = {"checkpoint_file": checkpoint_path}
        if plan.turns_used is not None:
            ctx.update(turns_used=plan.turns_used, turns_cap=plan.turns_cap,
                       turns_left=plan.turns_left)
        if plan.time_used is not None:
            ctx.update(time_used=_fmt_time(plan.time_used),
                       time_cap=_fmt_time(plan.time_cap),
                       time_left=_fmt_time(plan.time_left))
        out = tmpl
        out = _strip_placeholder_clauses(out, ctx)
        for k, v in ctx.items():
            out = out.replace("{" + k + "}", str(v))
        out = re.sub(r"\s{2,}", " ", out).strip(" ,").strip()
        return out or None
    except Exception as exc:
        logger.warning("agent-checkpoint: render failed (%s); no-op", exc)
        return None


def _strip_placeholder_clauses(text: str, ctx: Dict[str, Any]) -> str:
    """Remove every clause carrying a placeholder for an ABSENT axis.

    Operates on maximal runs of consecutive absent-axis markers. For each
    run, the deleted region spans:

    * from the run's first marker, extended backwards over a trailing
      ``and``/``or`` conjunction (the connector to the dropped clause);
    * through the text that follows the run's last marker up to the next
      axis marker (or end of string), cut at the first sentence
      punctuation so a trailing ``.``/``;``/``:``/``!``/``?`` survives.

    Example: ``"T1: {turns_used}/{turns_cap} turns ({turns_left} left)
    and {time_used} of {time_cap} ({time_left} left)."`` with no time
    axis renders ``"T1: 10/20 turns (10 left)."``, instead of a naive
    per-marker delete collapsing the kept clause to ``"10/20 turns ."``.
    """
    axis_keys = ("turns_used", "turns_cap", "turns_left",
                 "time_used", "time_cap", "time_left")
    pattern = re.compile(r"\{(" + "|".join(axis_keys) + r")\}")
    present = set(ctx)
    out = text
    # Process runs from the END of the string so earlier indices stay
    # valid across deletions.
    markers = list(pattern.finditer(out))
    runs = []
    i = 0
    while i < len(markers):
        if markers[i].group(1) in present:
            i += 1
            continue
        j = i
        while (j + 1 < len(markers)
               and markers[j + 1].group(1) not in present):
            j += 1
        runs.append((i, j))
        i = j + 1
    for i, j in reversed(runs):
        first, last = markers[i], markers[j]
        start = first.start()
        # Backwards: absorb a trailing "and"/"or" connector, including the
        # whitespace that preceded it (so "left) and <dropped>" becomes
        # "left)" instead of "left) .").
        prefix = out[:start]
        cm = re.search(r"\b(and|or)\s*$", prefix)
        if cm:
            start = cm.start()
        # Forwards: the tail after the run's last marker, up to the next
        # axis marker or end of string; cut at the first sentence
        # punctuation so the terminator survives.
        nxt = next((mm for mm in markers if mm.start() > last.end()), None)
        tail_start = last.end()
        tail_end = nxt.start() if nxt else len(out)
        tail = out[tail_start:tail_end]
        cut = len(tail)
        for p, ch in enumerate(tail):
            if ch in ".;:!?":
                cut = p
                break
        head = out[:start]
        if cm:
            head = head.rstrip()
        out = head + out[tail_start + cut:]
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\(\s*$", "", out)  # orphaned "(" left by a deletion
    out = re.sub(r",\s*$", "", out)   # orphaned trailing comma
    return out.strip()


def checkpoint_path_for(cfg: PluginConfig, run_id: str) -> str:
    """``<checkpoint_dir>/<run-id>.md`` — the durable file the agent writes."""
    d = cfg.checkpoint_dir or DEFAULT_CHECKPOINT_DIR
    if d.startswith("$"):
        d = os.path.expandvars(d)
    if not d.endswith("/"):
        d = d + "/"
    return f"{d}{run_id}.md"


def _write_checkpoint_file(path: str, content: str) -> bool:
    """Write the checkpoint file (mid-run persist). Atomic via a temp file
    in the same directory + ``os.replace``. Fail-open → False."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("agent-checkpoint: checkpoint write failed (%s): %s",
                       path, exc)
        return False


def run_id_for(session_id: str, task_id: str = "") -> str:
    """Stable run identifier for the checkpoint filename (plan section 4:
    "run-id = kanban task id or session id").

    NOT the hook's ``task_id`` kwarg: in the kanban goal loop that is a fresh
    per-turn uuid (cli.py ``_run_turn`` passes no task_id ->
    turn_context.py:580 ``task_id or uuid.uuid4()``), which would name tier 1
    and tier 2 two different random ``<uuid>.md`` files and make the run id
    meaningless. The kanban board task id (``HERMES_KANBAN_TASK``) is stable for
    the whole worker run and takes precedence. Next, the stable session id.
    The hook's ``task_id`` kwarg is only a last-resort fallback: it is a real,
    stable id in the dialects/CLI passes that supply one, but a per-turn uuid
    in the goal loop — so it must never shadow the two stable sources.
    """
    kanban_task = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if kanban_task:
        return kanban_task
    if session_id:
        return session_id
    return task_id or "-"


# ---------------------------------------------------------------------------
# Per-session latched state (concurrency-safe; no leakage between sessions)
# ---------------------------------------------------------------------------

class SessionLatches:
    """Process-local per-session run state.

    One entry per active session/run. Created on the run's first
    ``pre_llm_call`` (``is_first_turn``) and discarded on later runs of the
    same session (``begin_run`` replaces the entry). A lock guards
    concurrent sessions in one gateway process; the dict is bounded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # session_key -> {"fired": [tier_idx, ...]      (prologue-injected)
        #                 "persisted": [tier_idx, ...]  (mid-run persisted)
        #                 "turns": int, "start_mono": float,
        #                 "caps": RunCaps | None, "cfg": PluginConfig | None,
        #                 "run_id": str, "dialect": str,
        #                 "uncapped_fires": int (interval checkpoints done),
        #                 "uncapped_commented": bool (first-fire comment
        #                 attempted; later interval fires are file-only),
        #                 "watchdog_stop": threading.Event | None}
        self._state: Dict[str, dict] = {}

    @staticmethod
    def key(session_id: str, task_id: str = "") -> str:
        # Keyed by session id ONLY (docstring + plan section 3: "latch state
        # keyed per session/run id"). The hook's ``task_id`` kwarg is a
        # per-turn uuid in the kanban goal loop (cli.py _run_turn passes no
        # task_id -> turn_context.py:580 ``task_id or uuid.uuid4()``), so
        # including it churned the key every turn and reset the turn counter
        # (Build B1 baseline: 0 tiers fired). session_id is stable for the
        # whole worker process and still isolates concurrent gateway sessions.
        return f"{session_id or '-'}"

    def begin_run(self, key: str) -> dict:
        """Start (or restart) a run for this key. Returns its state dict."""
        with self._lock:
            if len(self._state) >= MAX_LATCHED_SESSIONS:
                # Evict the oldest half (by run start) to stay bounded.
                ordered = sorted(self._state.items(),
                                 key=lambda kv: kv[1]["start_mono"])
                for k, _ in ordered[: len(ordered) // 2]:
                    self._state.pop(k, None)
            st = {
                "fired": [],
                "persisted": [],
                "turns": 0,
                "start_mono": time.monotonic(),
                "caps": None,
                "cfg": None,
                "run_id": "",
                "dialect": "chat",
                "uncapped_fires": 0,
                "uncapped_commented": False,
                "watchdog_stop": None,
            }
            self._state[key] = st
            return st

    def advance(self, key: str) -> Optional[dict]:
        """Count one turn for the key; None if the run was never begun."""
        with self._lock:
            st = self._state.get(key)
            if st is None:
                return None
            st["turns"] += 1
            return st

    def mark_fired(self, key: str, tiers: list) -> None:
        with self._lock:
            st = self._state.get(key)
            if st is not None:
                for t in tiers:
                    if t not in st["fired"]:
                        st["fired"].append(t)

    def mark_persisted(self, key: str, tiers: list) -> None:
        with self._lock:
            st = self._state.get(key)
            if st is not None:
                for t in tiers:
                    if t not in st["persisted"]:
                        st["persisted"].append(t)

    def get(self, key: str) -> Optional[dict]:
        """The live state dict for key (None if the run was never begun)."""
        with self._lock:
            return self._state.get(key)

    def drop(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def elapsed(self, key: str) -> float:
        with self._lock:
            st = self._state.get(key)
            if st is None:
                return 0.0
            return time.monotonic() - st["start_mono"]

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {k: dict(v, fired=list(v["fired"]),
                            persisted=list(v["persisted"]))
                    for k, v in self._state.items()}


# ---------------------------------------------------------------------------
# The hooks
# ---------------------------------------------------------------------------

LATCHES = SessionLatches()


def _build_result(context: str) -> Dict[str, str]:
    return {"context": context}


def _run_context(session_id: str, task_id: str, cfg: PluginConfig,
                 key: str) -> tuple:
    """``(caps, run_id)`` for a session, cached on its latch state.

    Cap discovery reads the kanban DB / profile config, so it runs once
    per run (at first evaluation) — never per tool call.
    """
    st = LATCHES.get(key)
    if st is not None and st.get("caps") is not None:
        return st["caps"], st.get("run_id") or run_id_for(session_id, task_id)
    caps = discover_caps(session_id, cfg)
    run_id = run_id_for(session_id, task_id)
    if st is not None:
        st["caps"] = caps
        st["cfg"] = cfg
        st["run_id"] = run_id
        st["dialect"] = caps.dialect
    return caps, run_id


def _evaluate(session_id: str, task_id: str = "",
              cfg: Optional[PluginConfig] = None,
              surface: str = "persist") -> Optional[tuple]:
    """Shared tier evaluation for every hook surface.

    Returns ``(cfg, caps, plan, key)`` when at least one tier is newly due
    for THIS surface, else None. The latches are split per surface: the
    prologue (``surface="inject"``) evaluates against the injected latch,
    the mid-run surfaces (tool hooks, watchdog; ``surface="persist"``)
    against the persisted latch — so a persist never suppresses the later
    prologue injection, an injection never suppresses the persist that
    guarantees the durable record, and each surface fires a tier at most
    once per run. Fail-open: any error → None.
    """
    try:
        if cfg is None:
            cfg = load_plugin_config()
        if not cfg.enabled:
            return None
        key = SessionLatches.key(session_id, task_id)
        st = LATCHES.get(key)
        if st is None:
            # No prologue seen yet (hook registered mid-run, or a tool hook
            # is the first contact): begin now so elapsed/turns still count
            # from a conservative origin.
            st = LATCHES.begin_run(key)
        caps, _run_id = _run_context(session_id, task_id, cfg, key)
        turns_used = st["turns"]
        elapsed = LATCHES.elapsed(key)
        handled = st["fired"] if surface == "inject" else st["persisted"]
        plan = compute_tier_plan(cfg, caps, turns_used, elapsed,
                                 list(handled))
        if plan is None or not plan.fire_tiers:
            return None
        return cfg, caps, plan, key
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: evaluation failed (%s); no-op", exc)
        return None


def _persist(cfg: PluginConfig, caps: RunCaps, plan: TierPlan, key: str,
             session_id: str, task_id: str, source: str,
             elapsed: float) -> list:
    """The mid-run persist action: checkpoint file + kanban comment + log.

    Renders one combined checkpoint for all newly due tiers in ``plan``,
    writes the checkpoint file, posts it as a kanban comment (kanban
    dialect only — the comment's task IS the run id there), and latches
    the tiers persisted. Returns the tiers persisted. Fail-open: a
    failing piece is logged and the rest still run.
    """
    done: list = []
    try:
        run_id = run_id_for(session_id, task_id)
        path = checkpoint_path_for(cfg, run_id)
        pieces = []
        for tier in sorted(plan.fire_tiers):
            text = render_checkpoint_text(cfg, plan, tier, path)
            if text:
                pieces.append((tier, text))
        if not pieces:
            return done
        body_text = "\n\n".join(t for _, t in pieces)
        header = (f"{PLUGIN_ID}: budget checkpoint persisted mid-run "
                  f"(source: {source}). The worker may not have seen a "
                  f"turn boundary yet; this comment is the durable "
                  f"record, mirrored in the checkpoint file.")
        wrote = _write_checkpoint_file(
            path, f"# {header}\n\n{body_text}\n")
        comment_id = None
        if caps.dialect == "kanban":
            comment = (header + "\n\n" + body_text)[:COMMENT_MAX_CHARS]
            comment_id = _post_kanban_comment(run_id, comment)
        for tier, _text in pieces:
            logger.info(
                "agent-checkpoint: tier %d fired for %s (dialect=%s, "
                "turns=%s/%s, elapsed=%.0fs/%s, source=%s, file=%s, "
                "comment=%s)",
                tier, key, caps.dialect,
                plan.turns_used, plan.turns_cap, elapsed,
                f"{caps.time_cap:.0f}" if caps.time_cap else "-",
                source, "written" if wrote else "FAILED",
                comment_id if comment_id is not None else "-",
            )
            done.append(tier)
        LATCHES.mark_persisted(key, done)
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: persist failed (%s); no-op", exc)
    return done


def _log_only_due(cfg: PluginConfig, caps: RunCaps, plan: TierPlan,
                  key: str, session_id: str, task_id: str,
                  source: str) -> None:
    """log-only mode on a mid-run surface: record, latch persisted, done."""
    for tier in sorted(plan.fire_tiers):
        logger.info(
            "agent-checkpoint [log-only]: tier %d due for %s "
            "(dialect=%s, source=%s); checkpoint file would be %s",
            tier, key, caps.dialect, source,
            checkpoint_path_for(cfg, run_id_for(session_id, task_id)),
        )
    LATCHES.mark_persisted(key, plan.fire_tiers)


# ---------------------------------------------------------------------------
# Uncapped-run fallback (0.3.0): absolute-time checkpoints when a run has
# no discoverable cap on either axis, so it still leaves a durable record.
# ---------------------------------------------------------------------------

def _caps_are_capless(caps: Optional[RunCaps]) -> bool:
    """True when caps were discovered and NEITHER axis exists — distinct
    from "not yet discovered" (``caps is None``)."""
    return (caps is not None and caps.turns_cap is None
            and caps.time_cap is None)


def _uncapped_fallback(cfg: PluginConfig, key: str, session_id: str,
                       task_id: str, source: str) -> bool:
    """Persist an interval checkpoint for a capless run when one is due.

    Called from the persist surfaces only (watchdog loop, tool hooks) —
    never the inject surface: an uncapped run has no budget to frame, and
    budget text for it would be a lie. Due when the elapsed time crosses
    the next absolute interval boundary (interval, 2x interval, ...
    anchored at run start, so poll jitter cannot drift the schedule).
    The checkpoint file is rewritten at every fire (one path per run —
    it cannot spam); a kanban comment is posted on the FIRST fire only,
    later fires are file-only. ``log-only`` mode logs the fire instead.
    Returns True when a fire happened. Fail-open: any error → False.
    """
    try:
        interval = float(cfg.uncapped_interval_seconds or 0.0)
        if interval <= 0.0:
            return False
        st = LATCHES.get(key)
        if st is None:
            return False
        caps = st.get("caps")
        if not _caps_are_capless(caps):
            return False  # undiscovered yet, or capped: tiers own it
        elapsed = LATCHES.elapsed(key)
        fires = int(st.get("uncapped_fires", 0))
        if elapsed < interval * (fires + 1):
            return False
        fire_no = fires + 1
        st["uncapped_fires"] = fire_no  # latch first: one fire per boundary
        run_id = run_id_for(session_id, task_id)
        path = checkpoint_path_for(cfg, run_id)
        if cfg.mode == "log-only":
            logger.info(
                "agent-checkpoint [log-only]: uncapped interval "
                "checkpoint %d due for %s (dialect=%s, source=%s, "
                "elapsed=%.0fs, interval=%.0fs); checkpoint file would "
                "be %s",
                fire_no, key, caps.dialect, source, elapsed, interval,
                path)
            return True
        header = (
            f"{PLUGIN_ID}: uncapped-run checkpoint {fire_no} persisted "
            f"(source: {source}). This run has no discoverable turn or "
            f"time cap, so no budget tiers apply; this checkpoint was "
            f"persisted on the uncapped interval ({interval:.0f}s) to "
            f"leave a durable record. Elapsed at persist: "
            f"{elapsed:.0f}s.")
        facts = (f"Run: {run_id}\n"
                 f"Dialect: {caps.dialect}\n"
                 f"Turns so far: {st.get('turns', 0)}\n"
                 f"Elapsed: {elapsed:.0f}s\n"
                 f"Uncapped interval: {interval:.0f}s "
                 f"(checkpoint {fire_no})")
        wrote = _write_checkpoint_file(path, f"# {header}\n\n{facts}\n")
        comment_id = None
        if caps.dialect == "kanban" and not st.get("uncapped_commented"):
            # First fallback fire only: latch the attempt (success or
            # not) so later fires stay file-only and a broken board DB
            # is not retried every interval.
            st["uncapped_commented"] = True
            comment_id = _post_kanban_comment(
                run_id, (header + "\n\n" + facts)[:COMMENT_MAX_CHARS])
        logger.info(
            "agent-checkpoint: uncapped checkpoint %d for %s (dialect=%s, "
            "turns=%s, elapsed=%.0fs, interval=%.0fs, source=%s, "
            "file=%s, comment=%s)",
            fire_no, key, caps.dialect, st.get("turns", 0), elapsed,
            interval, source, "written" if wrote else "FAILED",
            comment_id if comment_id is not None else "-")
        return True
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: uncapped fallback failed (%s); "
                       "no-op", exc)
        return False


# ---------------------------------------------------------------------------
# Watchdog (backstop for a run parked inside one long tool call)
# ---------------------------------------------------------------------------

def _ensure_watchdog(key: str, session_id: str, task_id: str,
                     cfg: Optional[PluginConfig] = None) -> None:
    """Start the per-session watchdog thread on first hook contact.

    Daemon thread; exits when the session ends (``on_session_end`` sets
    its stop event), when the config turns it off, when the session state
    disappears, or at cap + ``WATCHDOG_GRACE_SECONDS``.
    """
    try:
        if cfg is None:
            cfg = load_plugin_config()
        if not cfg.enabled or not cfg.watchdog:
            return
        st = LATCHES.get(key)
        if st is None:
            return
        existing = st.get("watchdog_stop")
        if existing is not None and not existing.is_set():
            return  # already running for this run
        stop = threading.Event()
        st["watchdog_stop"] = stop
        th = threading.Thread(
            target=_watchdog_loop,
            args=(key, session_id, task_id, stop),
            name=f"agent-checkpoint-watchdog-{key[:24]}",
            daemon=True,
        )
        th.start()
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: watchdog start failed (%s)", exc)


def _watchdog_loop(key: str, session_id: str, task_id: str,
                   stop: threading.Event) -> None:
    """Poll the tier plan until the run ends or cap + grace passes.

    Also drives the uncapped-run fallback: a session whose caps are
    discovered and capless gets an interval checkpoint per
    ``uncapped_interval_seconds`` — and when that fallback is OFF (0),
    a capless session can never fire anything, so the loop exits on
    its first poll instead of polling for the session's whole life.
    """
    try:
        while not stop.is_set():
            cfg = load_plugin_config()
            if not cfg.enabled or not cfg.watchdog:
                return
            st = LATCHES.get(key)
            if st is None:
                return  # session state gone (ended / evicted)
            caps = st.get("caps")
            if caps is None:
                # Discover once (cached on the state by _run_context)
                # so the capless checks below see this run's real caps
                # even when no evaluation has run yet.
                caps, _rid = _run_context(session_id, task_id, cfg, key)
            if (_caps_are_capless(caps)
                    and float(cfg.uncapped_interval_seconds or 0.0) <= 0.0):
                return  # capless + fallback off: nothing can ever fire
            if caps is not None and caps.time_cap:
                if (LATCHES.elapsed(key)
                        > float(caps.time_cap) + WATCHDOG_GRACE_SECONDS):
                    return  # past cap + grace; nothing left to save
            if cfg.mid_run_persist:
                result = _evaluate(session_id, task_id, cfg, "persist")
                if result is not None:
                    ecfg, ecaps, plan, _k = result
                    if ecfg.mode == "log-only":
                        _log_only_due(ecfg, ecaps, plan, key,
                                      session_id, task_id, "watchdog")
                    else:
                        _persist(ecfg, ecaps, plan, key, session_id,
                                 task_id, "watchdog", LATCHES.elapsed(key))
                else:
                    _uncapped_fallback(cfg, key, session_id, task_id,
                                       "watchdog")
            stop.wait(cfg.watchdog_poll_seconds)
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: watchdog for %s exited: %s",
                       key, exc)


# ---------------------------------------------------------------------------
# Hook entry points
# ---------------------------------------------------------------------------

def on_pre_llm_call(
    session_id: str = "",
    task_id: str = "",
    is_first_turn: bool = False,
    **_kw: Any,
) -> Optional[Dict[str, str]]:
    """``pre_llm_call`` hook entry point. Fail-open: returns None on error.

    Returns ``{"context": text}`` (injected into the current turn's user
    message) when a checkpoint tier is due, else None. Behavior is the
    pre-fix behavior, with evaluation shared with the mid-run surfaces.
    """
    try:
        key = SessionLatches.key(session_id, task_id)
        if is_first_turn:
            LATCHES.begin_run(key)
        else:
            st = LATCHES.advance(key)
            if st is None:
                # No begin seen (e.g. hook registered mid-run) — begin now
                # so we can still count; elapsed starts from here
                # (conservative).
                LATCHES.begin_run(key)
        _ensure_watchdog(key, session_id, task_id)

        result = _evaluate(session_id, task_id, surface="inject")
        if result is None:
            return None
        cfg, caps, plan, key = result

        if cfg.mode == "log-only":
            # log-only: record in the plugin log, inject nothing.
            for tier in sorted(plan.fire_tiers):
                logger.info(
                    "agent-checkpoint [log-only]: tier %d due for %s "
                    "(dialect=%s); checkpoint file would be %s",
                    tier, key, caps.dialect,
                    checkpoint_path_for(cfg, run_id_for(session_id, task_id)),
                )
            LATCHES.mark_fired(key, plan.fire_tiers)
            return None

        path = checkpoint_path_for(cfg, run_id_for(session_id, task_id))
        elapsed = LATCHES.elapsed(key)
        pieces = []
        for tier in sorted(plan.fire_tiers):
            text = render_checkpoint_text(cfg, plan, tier, path)
            if text:
                pieces.append(text)
                logger.info(
                    "agent-checkpoint: tier %d fired for %s (dialect=%s, "
                    "turns=%s/%s, elapsed=%.0fs/%s)",
                    tier, key, caps.dialect,
                    plan.turns_used, plan.turns_cap, elapsed,
                    f"{caps.time_cap:.0f}" if caps.time_cap else "-",
                )
        if not pieces:
            return None
        LATCHES.mark_fired(key, plan.fire_tiers)
        return _build_result("\n\n".join(pieces))
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: hook failed (%s); injecting nothing", exc)
        return None


def _tool_hook(session_id: str, task_id: str, source: str) -> None:
    """Shared body of the tool-call hooks: evaluate + persist mid-run."""
    try:
        cfg = load_plugin_config()
        if not cfg.enabled or not cfg.mid_run_persist:
            return
        key = SessionLatches.key(session_id, task_id)
        if LATCHES.get(key) is None:
            LATCHES.begin_run(key)
        _ensure_watchdog(key, session_id, task_id, cfg)
        result = _evaluate(session_id, task_id, cfg, "persist")
        if result is None:
            # No tier due — the only remaining persist this surface can
            # owe is an uncapped-interval checkpoint (capless runs get
            # no tier plan at all). No-op for capped runs.
            _uncapped_fallback(cfg, key, session_id, task_id, source)
            return
        ecfg, caps, plan, key = result
        if ecfg.mode == "log-only":
            _log_only_due(ecfg, caps, plan, key, session_id, task_id,
                          source)
            return
        _persist(ecfg, caps, plan, key, session_id, task_id, source,
                 LATCHES.elapsed(key))
    except Exception as exc:  # fail-open, always
        logger.warning("agent-checkpoint: %s hook failed (%s); no-op",
                       source, exc)


def on_pre_tool_call(tool_name: str = "", session_id: str = "",
                     task_id: str = "", **_kw: Any) -> None:
    """``pre_tool_call`` observer: evaluate tiers before a tool runs.

    Returns None always — this plugin never blocks or modifies a call.
    """
    _tool_hook(session_id, task_id, "pre_tool_call")
    return None


def on_post_tool_call(tool_name: str = "", session_id: str = "",
                      task_id: str = "", **_kw: Any) -> None:
    """``post_tool_call`` observer: evaluate tiers after a tool ran."""
    _tool_hook(session_id, task_id, "post_tool_call")
    return None


def on_session_end(session_id: str = "", task_id: str = "",
                   **_kw: Any) -> None:
    """``on_session_end``: stop the session's watchdog. Latch state is
    kept (latches span the turns of one session; worker processes are
    reaped per run and the state dict is bounded)."""
    try:
        key = SessionLatches.key(session_id, task_id)
        st = LATCHES.get(key)
        if st is not None:
            stop = st.get("watchdog_stop")
            if stop is not None:
                stop.set()
    except Exception:  # fail-open, always
        pass
    return None


def register(ctx: Any) -> None:
    """Plugin entry point (required by the Hermes plugin loader)."""
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)

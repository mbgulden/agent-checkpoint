# agent-checkpoint

[![tests](https://github.com/mbgulden/agent-checkpoint/actions/workflows/tests.yml/badge.svg)](https://github.com/mbgulden/agent-checkpoint/actions/workflows/tests.yml)

Durable progress checkpoints for long, budget-capped agent runs.

A run that hits its turn or time cap mid-thought used to leave nothing
behind: no summary, no state, no record of what it had found. This plugin
makes that failure mode impossible to do silently. At configured
fractions of a run's budget it tells the agent to persist everything it
has so far into a durable checkpoint file — and, because some runs never
see another turn boundary, the plugin can also persist the checkpoint
itself, mid-run, from tool-call boundaries and from a watchdog thread.

It guarantees a paper trail, not completion: a run can still fail, but
it can no longer fail without leaving a record of where it got to.

## What it does

- **Tier 1** (default 50% of the effective budget): a `pre_llm_call`
  injection tells the agent to write everything found so far into its
  checkpoint file, then continue at normal pace.
- **Tier 2** (default 80% of the time budget / 90% of the turn budget):
  a final injection tells the agent to stop expanding, bring the
  checkpoint file and its deliverable to a final state (marked
  INCOMPLETE with the exact next step if unfinished), post its summary,
  and verify the file exists.
- **Mid-run persistence** (default on): the same tier plan is evaluated
  at tool-call boundaries (`pre_tool_call` / `post_tool_call`) and by a
  per-session watchdog thread, so a run parked inside one long tool call
  still gets its checkpoint. On a mid-run crossing the plugin itself
  writes the checkpoint file and — in kanban runs — posts the checkpoint
  as a task comment (author `agent-checkpoint`). A mid-run persist never
  suppresses the later prologue injection, and vice versa (separate
  latches per surface; each fires a tier at most once per run).
- **Uncapped-run fallback** (0.3.0): if no cap is discoverable on either
  axis, there is no budget to warn against, so nothing is ever injected.
  The watchdog instead persists a checkpoint every
  `uncapped_interval_seconds` (default 1800) of elapsed time, so a long
  uncapped run still leaves a durable record. The checkpoint file is
  rewritten at each interval; a kanban comment is posted on the first
  interval fire only — later fires are file-only, so a long run cannot
  spam its board. Set the interval to 0 to disable the fallback; a
  capless session's watchdog then exits on its first poll instead of
  polling for the session's whole life.

Everything is **fail-open**: any error in config loading, cap discovery,
tier math, rendering, or persistence degrades to a log line and a no-op.
The plugin never breaks a turn, never blocks a tool call, and never
prompts or waits on a person.

## What a checkpoint looks like

A real checkpoint file, persisted by the watchdog mid-run on the
author's fleet (run ids and paths generalized). The worker was inside a
single long stretch of work; the plugin wrote this itself — the worker
only had to exist:

```markdown
# agent-checkpoint: budget checkpoint persisted mid-run (source: watchdog). The worker may not have seen a turn boundary yet; this comment is the durable record, mirrored in the checkpoint file.

Budget checkpoint: you have used 0 of 10 turns (10 left) and 30m00s of 1h00m (29m59s left) — about half your budget remains; keep working at normal pace. Before continuing: write everything found so far into $HERMES_HOME/agent-checkpoints/<task-id>.md, including what remains to be done. Then continue the task.
```

Capless runs get the interval form instead:

```markdown
# agent-checkpoint: uncapped-run checkpoint 4 persisted (source: watchdog). This run has no discoverable turn or time cap, so no budget tiers apply; this checkpoint was persisted on the uncapped interval (20s) to leave a durable record. Elapsed at persist: 80s.

Run: <session-id>
Dialect: chat
Turns so far: 0
Elapsed: 80s
Uncapped interval: 20s (checkpoint 4)
```

Want to watch one appear on your own install? [`demo/`](demo/) runs a
90-second capped session in a throwaway profile and prints the
checkpoint the watchdog persists.

## Install

From the Hermes Plugin Catalog (once catalogued):

```
hermes plugins install agent-checkpoint
```

Manual install (before cataloguing, or from this repo directly): copy
this directory ( `__init__.py` + `plugin.yaml` ) into
`$HERMES_HOME/plugins/agent-checkpoint/` — or into a profile's
`plugins/agent-checkpoint/` — and enable `agent-checkpoint` in that
profile's plugin list. No dependencies; Python standard library only.
Requires Hermes >= 0.20.5.

## Which runs get budget checkpoints

Cap discovery is per dialect, first match wins per axis:

| Dialect | Time axis | Turn axis |
|---|---|---|
| **kanban** (worker run; `HERMES_KANBAN_TASK` set) | the board task's `max_runtime_seconds`, read from the kanban DB; fallback: `TERMINAL_TIMEOUT` + 30 s | profile `agent.max_turns`, then plugin `caps.turns` |
| **goal** | — (no in-process time axis for goal sessions) | the goal's `max_turns`, then profile `agent.max_turns`, then plugin `caps.turns` |
| **chat** | plugin `caps.time_seconds`, or `agent.run_budget_seconds` from the Hermes config | plugin `caps.turns` only |

A profile `agent.max_turns` alone never arms a plain chat session.
Plugin-configured `caps` fill whichever axis is still unset in every
dialect. If neither axis resolves, the run is *capless*: no injections,
and the uncapped-run fallback above is the only coverage.

Two refinements: on tiny caps (turn cap < 10 or time cap < 5 min),
tier 1 would land within ~2 turns of tier 2, so tier 1 is suppressed
unless `allow_tiny_tier1` is set; and tier 2 always fires no later than
the penultimate turn. A 1-turn run gets no checkpoint at all.

## Configuration

Settings live under `plugins.entries.agent-checkpoint.settings` in the
Hermes config (a top-level `agent_checkpoint:` section is also read, for
compatibility). Every key is optional; a malformed section falls back to
defaults with a warning in the plugin log. The manifest's
`config_schema` declares the same keys for load-time validation and the
Desktop settings form.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch. `false` makes every hook a no-op. |
| `mode` | str | `snapshot` | `snapshot`: inject the persistence directive. `notify`: inject the budget numbers only, no directive. `log-only`: record due tiers/fires in the plugin log; inject and persist nothing. |
| `tier_fractions` | list | `[0.5]` | Tier fractions of the effective budget (max of the axis fractions). First element = tier 1. An optional second element replaces both tier-2 per-axis fractions. |
| `tier2_time_fraction` | float | `0.8` | Tier 2 threshold on the time axis. |
| `tier2_turn_fraction` | float | `0.9` | Tier 2 threshold on the turn axis. |
| `min_budget_floor_turns` | int | `0` | A turn axis whose cap is below this is ignored. |
| `min_budget_floor_seconds` | float | `0.0` | A time axis whose cap (seconds) is below this is ignored. |
| `enabled_axes` | list | `[time, turns]` | Which budget axes count towards tiers. |
| `checkpoint_dir` | str | `$HERMES_HOME/agent-checkpoints/` | Directory for checkpoint files, named `<run-id>.md` (kanban task id, else session id). Values starting with `$` are environment-expanded. |
| `allow_tiny_tier1` | bool | `false` | Fire tier 1 even on tiny caps (see above). |
| `caps.time_seconds` | float | — (none) | Plugin-supplied time cap in seconds, for runs whose native cap is undiscoverable (part of the `caps` mapping). |
| `caps.turns` | int | — (none) | Plugin-supplied turn cap (part of the `caps` mapping). |
| `tier1_template` | str | shipped default | Custom tier-1 text. Placeholders: `{turns_used}`, `{turns_cap}`, `{turns_left}`, `{time_used}`, `{time_cap}`, `{time_left}`, `{checkpoint_file}`. Clauses naming an absent axis are stripped. |
| `tier2_template` | str | shipped default | Custom tier-2 text; same placeholders. |
| `mid_run_persist` | bool | `true` | Evaluate tiers at tool-call boundaries and from the watchdog, persisting the checkpoint itself mid-run. `false` restores prologue-only behavior — no mid-run persists at all, including the uncapped fallback. |
| `watchdog` | bool | `true` | Run the per-session watchdog thread. |
| `watchdog_poll_seconds` | float | `10.0` | Watchdog poll interval (floored at 0.05 s). Each poll reloads the config, so settings changes take effect live. |
| `uncapped_interval_seconds` | float | `1800.0` | Capless runs: persist a checkpoint every this many elapsed seconds (boundaries are absolute multiples from run start). First fire also posts the kanban comment (kanban runs only); later fires are file-only. `0` disables uncapped-run coverage. |

Example:

```yaml
plugins:
  entries:
    agent-checkpoint:
      settings:
        mode: snapshot
        uncapped_interval_seconds: 1800
```

## What it reads, writes, and runs (disclosures)

- **Reads:** the kanban board DB, read-only, once per run — one
  `SELECT max_runtime_seconds` for cap discovery (path from
  `HERMES_KANBAN_DB` or Hermes' own kanban DB path); Hermes core config
  via `load_config_readonly` (its own settings, `agent.max_turns`,
  `agent.run_budget_seconds`); goal state via `load_goal`. Environment
  variables read: `HERMES_KANBAN_TASK`, `HERMES_KANBAN_DB`,
  `TERMINAL_TIMEOUT` — all Hermes runtime signals, none of them secrets.
- **Writes:** checkpoint files under `checkpoint_dir` (default
  `$HERMES_HOME/agent-checkpoints/<run-id>.md`), written atomically
  (temp file + rename), only from mid-run persist surfaces and the
  uncapped fallback — the prologue hooks write nothing. In kanban runs,
  a persist also posts the checkpoint as a task comment. There is no
  public Hermes API for posting board comments, so the comment write
  uses the board DB directly, mirroring `hermes_cli.kanban_db`'s own
  `add_comment` write path exactly (same tables, same transaction
  shape, author `agent-checkpoint`, body capped at 4000 chars).
- **Background:** one daemon watchdog thread per session. It starts on
  first hook contact, polls every `watchdog_poll_seconds`, and exits at
  session end, when the config turns it off, when the session state is
  evicted (state is process-local, bounded at 512 sessions), or at
  cap + 180 s grace. For a capless session with the fallback disabled
  it exits on its first poll.
- **Network:** none. **Credentials:** none read, through any route.
  **Telemetry:** none. **Self-update:** none. **Shell/subprocess:** none.

## Evidence

The plugin has run in production on a six-profile Hermes fleet since
October 23, 2026 (v0.1.0; v0.2.0 added mid-run persistence on
October 25 after it was proven that kanban runs are effectively
single-turn, so prologue-only firing could never rescue a capped run).
A fleet reliability audit on October 26, 2026 counted 88 checkpoint
comments and 53 checkpoint files produced by the plugin in the audit
window, and failed runs in that window were diagnosable after the fact
from the plugin's trail — the failure mode it was built against was a
run dying at its cap with zero comments and nothing written down. The
same audit is the source of the caveat above: checkpoint coverage did
not by itself raise task completion (fleet-only completion was flat in
the window), and the persisted records are plugin-side state snapshots,
not a substitute for a worker's own report. It guarantees a paper
trail, not completion.

## Support & expectations

This plugin is maintained by one person, in the open, alongside the
fleet it was built for. Issues are triaged in batches rather than
daily; a good bug report (use the issue templates — log lines plus the
checkpoint file, or the fact that none appeared) gets a faster answer
than a vague one. There is no SLA and no paid support tier. Fixes ship
as tagged releases, and the test suite in `tests/` runs in CI on every
push, so the badge at the top of this README is the current truth
about the code.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Michael Gulden.

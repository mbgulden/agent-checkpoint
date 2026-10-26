#!/usr/bin/env bash
# agent-checkpoint — 2-minute demo.
#
# Creates a throwaway Hermes profile, installs this plugin into it the
# way a new user would, sets a small 90-second run budget, and runs one
# capped chat session that spends its budget inside long tool calls.
# The plugin's watchdog should persist a checkpoint file mid-run; the
# script prints it and exits 0. If no checkpoint file appears, it exits
# non-zero — the demo proves nothing if it can't fail.
#
# See demo/README.md for prerequisites, the control experiment, and
# cleanup. Run with:  bash demo/run-demo.sh
set -u

PROFILE="${DEMO_PROFILE:-ac-demo}"
BUDGET="${DEMO_BUDGET_SECONDS:-90}"
CHAT_TIMEOUT="${DEMO_CHAT_TIMEOUT:-420}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$(dirname "$SCRIPT_DIR")"

fail() { echo "DEMO FAIL: $*" >&2; exit 1; }
prereq() { echo "DEMO PREREQ: $*" >&2; exit 2; }

# --- 1. Prerequisites ---------------------------------------------------
command -v hermes >/dev/null 2>&1 || prereq "the 'hermes' CLI is not on PATH.
Install Hermes (>= 0.20.5) and make sure 'hermes --version' works, then re-run."
command -v python3 >/dev/null 2>&1 || prereq "python3 is not on PATH."
python3 -c "import yaml" 2>/dev/null || prereq "python3 cannot import yaml (PyYAML).
Hermes ships PyYAML in its own environment; if 'python3' here is not that
environment, run this script with Hermes' python or 'pip install pyyaml'."

HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
PDIR="$HERMES_ROOT/profiles/$PROFILE"
[ -e "$PDIR" ] && prereq "profile '$PROFILE' already exists at $PDIR.
Delete it first ('hermes profile delete -y $PROFILE') or pick another
name with DEMO_PROFILE=<name>."

# --- 2. Throwaway profile (clone of your active profile) ---------------
echo "== Creating throwaway profile '$PROFILE' (clone of your active profile)"
HERMES_HOME="$HERMES_ROOT" hermes profile create "$PROFILE" --clone --no-alias \
  || fail "profile create failed"
[ -d "$PDIR" ] || fail "profile dir $PDIR was not created"

# --- 3. Config: shipped defaults only + one small discoverable cap -----
echo "== Configuring: no plugin settings, agent.run_budget_seconds=$BUDGET"
python3 - "$PDIR/config.yaml" "$BUDGET" <<'PY' || fail "config edit failed"
import sys, yaml
path, budget = sys.argv[1], int(sys.argv[2])
cfg = yaml.safe_load(open(path)) or {}
cfg.pop("agent_checkpoint", None)
plugins = cfg.setdefault("plugins", {})
entries = plugins.get("entries")
if isinstance(entries, dict):
    entries.pop("agent-checkpoint", None)
enabled = plugins.get("enabled")
if isinstance(enabled, list) and "agent-checkpoint" in enabled:
    enabled.remove("agent-checkpoint")  # enabled via CLI below, like a new user
cfg.setdefault("agent", {})["run_budget_seconds"] = budget
with open(path, "w") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False)
print("config ok: run_budget_seconds =", budget)
PY

# --- 4. Install the plugin from this checkout ---------------------------
echo "== Installing agent-checkpoint from $PLUGIN_SRC"
DEST="$PDIR/plugins/agent-checkpoint"
mkdir -p "$DEST"
cp "$PLUGIN_SRC/__init__.py" "$PLUGIN_SRC/plugin.yaml" "$DEST/" \
  || fail "plugin copy failed"

# --- 5. Enable it the new-user way --------------------------------------
echo "== Enabling the plugin"
HERMES_HOME="$PDIR" hermes plugins enable agent-checkpoint \
  || fail "plugin enable failed"

# --- 6. One capped session, spent inside long tool calls ----------------
PROMPT='Run these four terminal commands one at a time, in order, waiting for each to finish before starting the next: sleep 25, sleep 25, sleep 25, sleep 25. When all four are done, reply with exactly: DEMO DONE'
echo "== Running one capped chat session (budget ${BUDGET}s; chat timeout ${CHAT_TIMEOUT}s)"
echo "   (if the chat itself times out on a busy model endpoint, that does"
echo "    not fail the demo — the checkpoint file is the evidence)"
HERMES_HOME="$PDIR" timeout "$CHAT_TIMEOUT" hermes --yolo chat -q "$PROMPT"
echo "chat exit code: $?"

# --- 7. The checkpoint file is the verdict ------------------------------
CKDIR="$PDIR/agent-checkpoints"
FOUND=""
for f in "$CKDIR"/*.md; do [ -f "$f" ] && FOUND="$f" && break; done
if [ -n "$FOUND" ]; then
  echo
  echo "== DEMO PASS — the watchdog persisted this checkpoint mid-run:"
  echo "--- $FOUND"
  cat "$FOUND"
  echo "---"
  echo "Cleanup: hermes profile delete -y $PROFILE"
  exit 0
fi
fail "no checkpoint file appeared under $CKDIR — the plugin did not persist.
Check $PDIR/logs/agent.log for agent-checkpoint lines, and confirm the
profile's model endpoint is reachable."

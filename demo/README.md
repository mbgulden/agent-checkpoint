# The 2-minute demo

`run-demo.sh` proves the plugin does what the README says — on your own
Hermes install, with a profile you can delete afterwards.

```bash
bash demo/run-demo.sh
```

(Git checkouts preserve the executable bit; if you arrived here from a
downloaded archive and the script won't execute, the `bash …` form
above always works.)

## What it does

1. Creates a throwaway profile (default name `ac-demo`, a clone of your
   active profile) — your real profiles are never touched.
2. Strips any agent-checkpoint configuration from the clone, so the
   plugin runs on **shipped defaults only**, and sets one small
   discoverable budget: `agent.run_budget_seconds: 90`.
3. Installs the plugin from this checkout and enables it with
   `hermes plugins enable agent-checkpoint` — the new-user sequence.
4. Runs one chat session instructed to spend its time inside four
   sequential `sleep 25` tool calls — the shape where a run never sees
   a turn boundary in time, which is exactly what the watchdog exists
   for.
5. Prints the checkpoint file the watchdog persisted mid-run and exits
   0. **If no checkpoint file appears, the script exits non-zero** —
   a demo that can't fail proves nothing.

Wall-clock time is roughly the 90-second budget plus model latency;
on a busy model endpoint the chat itself may run into the script's
420-second timeout *after* the checkpoint has landed. That still
counts as a pass — the checkpoint file is the evidence, not the chat.

## Prerequisites

- Hermes >= 0.20.5 installed, configured, and able to complete a chat
  (a working model/provider on the profile being cloned).
- `python3` with PyYAML importable (Hermes' own environment has it).
- A terminal tool available to the agent (the session runs `sleep`).

## The control experiment

To see that the plugin — not Hermes itself — writes the checkpoint:

1. Re-run the demo so a checkpoint exists.
2. Disable the plugin in the demo profile:
   `HERMES_HOME=$HERMES_HOME/profiles/ac-demo hermes plugins disable agent-checkpoint`
   (adjust the path if you used a different `DEMO_PROFILE`).
3. Delete the profile's `agent-checkpoints/` directory and run the
   same session again by hand:
   `HERMES_HOME=… hermes --yolo chat -q "Run these four terminal commands one at a time: sleep 25, sleep 25, sleep 25, sleep 25."`
4. No checkpoint file appears. Nothing else in Hermes writes one.

## Knobs

| Env var | Default | Meaning |
|---|---|---|
| `DEMO_PROFILE` | `ac-demo` | Throwaway profile name (must not already exist) |
| `DEMO_BUDGET_SECONDS` | `90` | The `run_budget_seconds` cap for the demo session |
| `DEMO_CHAT_TIMEOUT` | `420` | Hard timeout for the chat process, in seconds |

## Cleanup

```bash
hermes profile delete -y ac-demo
```

The demo writes nothing outside the throwaway profile.

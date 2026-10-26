---
name: Bug report
about: Something the plugin did wrong, or failed to do
title: ""
labels: bug
assignees: ""
---

**What happened**
A clear description of the bug — what the plugin did (or didn't do).

**What you expected**
What should have happened instead.

**Environment**
- Plugin version (see `plugin.yaml`):
- Hermes version (`hermes --version`):
- Run type: kanban / goal / chat
- Anything unusual about the run's budget (turn cap, time cap, uncapped)?

**Evidence**
- The relevant lines from the profile's `logs/agent.log` (grep for
  `agent-checkpoint`):
- The checkpoint file involved, if one was written (or the fact that
  none was, with the run's session/task id):

**Config**
Your `plugins.entries.agent-checkpoint.settings` block, if you have one
(remove anything sensitive — the plugin reads no credentials, so there
should be nothing to redact).

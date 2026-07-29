---
name: factory-supervisor
version: 1.8.0
description: Deprecated compatibility alias for deterministic Factory reconciliation.
---

# Factory supervisor compatibility

Contract 1.8 has no agentic supervisor and starts no child dispatcher.

1. Run `~/.factory/bin/factory-launch <project> contract --json` and require
   Contract `1.8.0`.
2. Run `~/.factory/bin/factory-launch <project> doctor --json` and stop on an
   error category.
3. Run `~/.factory/bin/factory-launch <project> reconcile --json` once.

The installed per-product launchd job normally performs that same non-agent
reconciliation every 15 seconds. `dispatch-plan --claim --json` remains a
deprecated deterministic compatibility alias for one release; it never starts
an agent or exposes a lease to a prompt.

Never invoke roles, worker CLIs, approval, merge, release activation, or manual
state edits from this skill.

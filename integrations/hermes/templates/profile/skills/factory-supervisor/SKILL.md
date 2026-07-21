---
name: factory-supervisor
version: 1.6.0
description: Start bounded factory dispatchers from atomic launcher claims.
---

# Factory supervisor

Accept only a trusted project slug from installed job configuration. Use no
repository path, ticket prose, board scan, or mutable checkout script.

1. Run `~/.factory/bin/factory-launch <project> contract --json`, require
   Contract `1.6.0`, then run
   `~/.factory/bin/factory-launch <project> doctor --json` and stop on any
   error category.
2. Run `factory-launch <project> dispatch-plan --claim --json` once.
3. On `WAIT`, exit successfully. On `ESCALATE`, publish one redacted operator
   message containing only its fixed reason code, then exit without retrying.
4. On `START`, keep the returned lease capability only in supervisor memory.
   Start one ephemeral child session using the installed `factory-dispatch`
   skill with the returned project, ticket, exact worktree, and lease. Never
   put the lease in prompts, logs, files, board state, evidence, or status.
5. The child dispatcher renews before every sequencing or role action and
   releases only at the contract's terminal state. A refusal or unknown result
   is terminal and must be escalated, never retried or worked around.

One invocation starts at most one child and then exits. The existing Hermes
gateway/cron owns scheduling and overlap suppression; do not add a daemon,
poll, busy-loop, scan Markdown, or create a second queue. Disabling that one
job returns the product to manual `factory-dispatch` operation.

Use Hermes native child-session delegation only. Never invoke a worker CLI,
approve, merge, activate, clear maintenance, recover leases, or mutate factory
state outside the stable launcher.

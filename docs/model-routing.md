# Model routing and fallback

This document is the operator-facing source of truth for which model runs each
role, what “fallback” means, and when the factory must stop.

## Default model order

If the operator has not activated another profile, the factory uses
`balanced-v2`. Its primary and secondary routes are:

| Role | Lane | Primary route | Secondary route | Effort |
|---|---|---|---|---|
| Planner | Production | Codex CLI — GPT-5.6 Sol | Cursor CLI — GPT-5.6 Sol High | High |
| Builder | Production | Codex CLI — GPT-5.6 Terra | Cursor CLI — GPT-5.6 Sol High | High |
| Narrator | Production | Codex CLI — GPT-5.6 Terra | Cursor CLI — GPT-5.6 Sol High | High |
| Spec-linter | Checking | Claude CLI — Fable 5 | Cursor CLI — Claude Fable 5 Thinking Medium | Medium |
| Test-author | Checking | Claude CLI — Fable 5 | Cursor CLI — Claude Fable 5 Thinking Medium | Medium |
| Reviewer | Checking | Claude CLI — Sonnet 5 | Cursor CLI — Claude Sonnet 5 Thinking High | High |

“Secondary” is a same-family transport/account alternative, not an independent
review family. Cursor GPT remains in the OpenAI family; Cursor Claude remains in
the Anthropic family.

The route catalog currently enables two families:

- OpenAI through native Codex CLI or Cursor CLI.
- Anthropic through native Claude CLI or Cursor CLI.

Kimi K2.6 is a disabled experimental Moonshot-family route through Claude CLI
and OpenRouter. It is not in any profile and is never selected by the current
workflow.

## Selection before a ticket starts

`models pin` probes the ordered candidates without sending ticket content:

1. `READY` selects the candidate.
2. `UNAVAILABLE` tries the next candidate, then the next complete portfolio.
3. `INVALID` or `UNKNOWN` stops. The factory does not route around ambiguous or
   untrusted evidence.
4. A portfolio is accepted only if all six roles resolve and the production and
   checking families are different.

The selected six-role plan and Kit SHA are committed to the ticket branch.
Every role then uses its exact pinned route. Profile activation after pinning
does not silently change an in-progress ticket.

## Fallback after a run has started

A task-bearing process is never silently retried. If it fails because credits
are exhausted or the provider is unavailable, the operator may request a
Contract 1.4 fallback:

1. `models fallback-plan` verifies the latest failed GO attempt, current Git and
   remote state, accounting, current route-journal head, and model readiness.
2. It excludes the exact failed route and resolves the failed role plus every
   remaining role.
3. The operator posts the generated one-line approval comment in Linear.
4. Normal Linear sync records the authenticated author and a 15-minute,
   one-use approval.
5. `models fallback` revalidates everything, preserves only role-authorized
   partial work, appends a route-journal revision, commits and pushes the
   handoff, then consumes the approval.

The usual default-profile result is native CLI → same-family Cursor:

- failed Codex route → Cursor GPT route;
- failed Claude route → Cursor Claude route.

This is not guaranteed. The resolver must find one complete valid assignment.
If the failed route was already the last ready same-family candidate, the
factory stops and escalates.

## Family-separation rules

Family history is tracked across all attempts, including failed attempts whose
work is preserved:

- Spec-linter cannot use any family that contributed Planner work (`spec-linter
  ∉ P`).
- Builder cannot use any family that contributed Test-author work (`builder ∉
  T`).
- Reviewer cannot use any family that contributed Builder work (`reviewer ∉
  B`).

A producer that continues in a new family adds both its old and new families to
that boundary. The corresponding checker then needs a family outside the full
history. With only OpenAI and Anthropic enabled, a cross-family producer
continuation will normally require an unavailable third family and therefore
stop. This is intentional: the factory does not weaken independent checking to
make a fallback succeed.

Completed roles remain immutable. The exact failed route is excluded from all
remaining roles in that fallback revision. `INVALID` and `UNKNOWN` readiness
remain hard stops during fallback; only `UNAVAILABLE` is skippable.

## Other profiles

The operator may activate a different profile before pinning:

- `openai-priority-v1`: tries OpenAI production with Anthropic checking first,
  then the family-swapped portfolio.
- `claude-priority-v1`: tries Anthropic production with OpenAI checking first,
  then the reverse.
- `cursor-priority-v1`: gives the exact Cursor routes priority over native
  routes in both portfolio orders.
- `legacy-balanced-v1`: preserves the previous medium-effort
  Builder/Narrator/Reviewer policy and Sonnet Cursor fallback for existing
  activation records and pinned-plan migration.

These profiles change candidate order, not the separation, approval, evidence,
or one-process-per-attempt rules. The committed ticket route plan or journal,
not the machine’s current active profile, is authoritative for an existing
ticket.

## Operator commands

```bash
# See and preview profiles.
~/.factory/bin/factory-launch <project> models profiles --json
~/.factory/bin/factory-launch <project> models plan --profile <profile-id> --json

# Pin the initial six-role plan.
~/.factory/bin/factory-launch <project> models pin \
  --ticket T-123 --workdir /absolute/ticket-worktree --json

# Preview an eligible mid-ticket fallback.
~/.factory/bin/factory-launch <project> models fallback-plan \
  --ticket T-123 --failed-run <run-id> \
  --workdir /absolute/ticket-worktree \
  --reason credits_exhausted --json

# After posting the exact Linear approval and running normal Linear sync:
~/.factory/bin/factory-launch <project> models fallback \
  --ticket T-123 --failed-run <run-id> \
  --workdir /absolute/ticket-worktree \
  --reason credits_exhausted --json
```

Only `credits_exhausted` and `provider_unavailable` are eligible mid-ticket
reasons. Logic errors, test failures, unsafe worktree changes, stale evidence,
and ambiguous accounting do not authorize a model fallback.

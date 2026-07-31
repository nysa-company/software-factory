# Model routing and fallback

This document is the operator-facing source of truth for which model runs each
role, what “fallback” means, and when the factory must stop.

## Default model order

If the operator has not activated another profile, the factory uses
`cursor-balanced-v2`. Its primary and secondary routes are:

| Role | Lane | Primary route | Secondary route | Effort |
|---|---|---|---|---|
| Planner | Production | Cursor CLI — GPT-5.6 Sol High | Codex CLI — GPT-5.6 Sol | High |
| Builder | Production | Cursor CLI — GPT-5.6 Sol High | Codex CLI — GPT-5.6 Terra | High |
| Narrator | Production | Cursor CLI — GPT-5.6 Sol High | Codex CLI — GPT-5.6 Terra | High |
| Spec-linter | Checking | Cursor CLI — Claude Fable 5 Thinking Medium | Claude CLI — Fable 5 | Medium |
| Test-author | Checking | Cursor CLI — Claude Fable 5 Thinking Medium | Claude CLI — Fable 5 | Medium |
| Reviewer | Checking | Cursor CLI — Claude Sonnet 5 Thinking High | Claude CLI — Sonnet 5 | High |

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

## Product-owned policy backend

A product may own `factory/model-policy.json` with schema
`factory-model-policy/v1`. It specifies the production and checking families
and an explicit primary route, secondary route, and effort for all six roles.
When present, this policy is used by normal plan and pin resolution. New pins
embed the validated policy snapshot in a `model-resolution-plan/v2`, so a later
project policy edit cannot reinterpret or invalidate an existing ticket pin.
Kit-owned profiles and all existing v1 plans remain unchanged.

The backend exposes `policy-candidates`, `policy-preview`, and `policy-apply`.
Candidates are computed from the kit catalog and exclude disabled or
experimental routes. Preview hashes bind both the current policy hash and the
exact proposed document. Apply requires that preview hash plus the expected
current hash, providing compare-and-swap behavior. Validation requires
different production/checking families and a same-family secondary route.
Readiness remains fail-closed: only `UNAVAILABLE` advances to the secondary.

`ticket-status --ticket T-NNN` is a read-only product ticket status endpoint.
The Reviewer same-family exception is intentionally unavailable through normal
policy. `reviewer-exception-contract` reports that a future implementation must
be ticket-scoped and one-use; normal validation is not weakened.

Hermes integration note: the launcher and contract are deliberately unchanged
in this change. A later Hermes release can map authenticated API methods to
these model-control commands and must pass preview/current hashes through
without rewriting them.

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

Fallback accounting is reduced directly from committed durable rows plus the
authoritative terminal manifests. The ignored runtime ledger is only a
materialized view, so a stale copy in a linked qualification worktree cannot
hide the latest failed attempt or authorize a different one.

The usual default-profile result is Cursor → same-family native CLI:

- failed Cursor GPT route → Codex route;
- failed Cursor Claude route → Claude route.

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
  routes in both portfolio orders while retaining the older medium-effort
  policy.
- `balanced-v2`: preserves the high-effort native-first policy for explicit
  activation and existing records.
- `legacy-balanced-v1`: preserves the previous medium-effort
  Builder/Narrator/Reviewer policy and Sonnet Cursor fallback for existing
  activation records and pinned-plan migration.

These profiles change candidate order, not the separation, approval, evidence,
or one-process-per-attempt rules. The committed ticket route plan or journal,
not the machine’s current active profile, is authoritative for an existing
ticket.

Release migration preserves every logical selection and all parent-hashed
history. It re-probes the already-selected routes and records current adapter
versions and reported identities in a new release-migration revision. Missing,
disabled, unavailable, or logically changed routes stop migration.

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

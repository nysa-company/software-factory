# Model routing and fallback

This document is the operator-facing source of truth for which model runs each
role, what “fallback” means, and when the factory must stop.

## Default model order

If the operator has not activated another profile, the factory uses
`cursor-opus-v1`. Its primary and secondary routes are:

| Role | Lane | Primary route | Secondary route | Effort |
|---|---|---|---|---|
| Planner | Production | Cursor CLI — GPT-5.6 Sol High | Codex CLI — GPT-5.6 Sol | High |
| Builder | Production | Cursor CLI — GPT-5.6 Sol High | Codex CLI — GPT-5.6 Terra | High |
| Narrator | Production | Cursor CLI — GPT-5.6 Sol High | Codex CLI — GPT-5.6 Terra | High |
| Spec-linter | Checking | Cursor CLI — Claude Opus 5 Thinking Medium | Claude CLI — Fable 5 | Medium |
| Test-author | Checking | Cursor CLI — Claude Opus 5 Thinking Medium | Claude CLI — Fable 5 | Medium |
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

Cursor model evidence is exact. The Opus selection currently reports
`Opus 5 300K Medium`. The GPT-5.6 Sol High selection may report either its
canonical `GPT-5.6 Sol 272K High` label or the explicitly certified
`GPT-5.6 Sol 1M High` task-context label. Every other context-window, effort,
selection, or family label is rejected. Native Claude fallback defaults to the
certified CLI version `2.1.223`.
Native Claude readiness runs version, help, OAuth, and authenticated-status
checks through one disposable owner-only configuration containing only a
validated credential copy. Ambient Claude settings and hooks therefore cannot
change route readiness, and the disposable credential is removed after every
probe result.
Qualification preparation snapshots the owner-only global model configuration
into the isolated lane. Later production-config edits cannot change its pins;
only a drained qualification upgrade with `--global-env` replaces the snapshot.

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

Production admission first selects one candidate in shadow mode and resolves
the active profile before creating a dispatcher lease or worktree. One healthy
result covers that reconciliation's bounded claim batch; the normal batch pin
still re-probes after claim to catch a readiness race. Permanent drift in that
narrow window retains the exact authenticated claim and records a ticket-bound
incident without starting a provider; admission does not invent partial
worktree/branch cleanup authority. A temporary outage waits without claiming,
while invalid or unknown evidence creates a ticket-bound admission incident.
Within one profile resolution, routes that share the same native Codex or
Claude adapter also share that adapter's model-independent version, contract,
and authentication result. The remaining independent probes run in batches of
at most five, with separate adapter probe homes and deterministic per-route
aggregation after every batch completes. Cursor route checks remain
model-specific.
Model-control errors expose only the strictly validated
per-route readiness table (`state`, typed reason, adapter version, and reported
identity); resolver stderr and unsafe probe detail are never returned. Doctor
performs the same active-profile check for an installed production release and
reports even temporary unavailability as an error because the approved profile
cannot currently resolve. The controller still treats that typed temporary
condition as a no-claim wait and returns the bounded ticket outcome; a permanent
failure returns the same evidence with error status. Existing pinned work is
submitted before either new-admission outcome.

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

Factory runtime note: the installed launcher maps only its fixed authenticated
grammar to these model-control commands and passes preview/current hashes
through without rewriting them.

## Fallback after a run has started

A task-bearing process is never silently retried. If it fails because credits
are exhausted or the provider is unavailable, the operator may request a
Contract 1.4 fallback:

1. `models fallback-plan` verifies the latest failed GO attempt, current Git and
   remote state, accounting, current route-journal head, and model readiness.
2. It excludes the exact failed route and resolves the failed role plus every
   remaining role.
3. The operator runs
   `factory-kit.sh operator fallback-approve --project <project> --product <path> --ticket T-123 --preview-hash <hash> --failed-run <run-id> --reason <reason>`,
   which issues a 15-minute, one-use receipt.
4. The receipt projects into `factory/operator-map.json` and a zero-authority
   audit copy is written under `factory/receipts/T-123/`.
5. `models fallback` revalidates everything, preserves only role-authorized
   partial work, appends a route-journal revision, commits and pushes the
   handoff, then consumes the approval.

Fallback accounting is reduced directly from committed durable rows plus the
authoritative terminal manifests. The ignored runtime ledger is only a
materialized view, so a stale copy in a linked qualification worktree cannot
hide the latest failed attempt or authorize a different one.

The handoff snapshot follows Git's tracked and non-ignored worktree boundary.
Ignored dependency/build directories such as `node_modules/` are not role
output and are excluded; tracked or non-ignored symlinks, special files,
nested repositories, hardlinks, and unsafe parent paths still fail closed.

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

Qualification additionally requires every selected Cursor role to have one
ready same-family native fallback at its exact pinned CLI version. Preparation
records the bounded readiness report, Doctor exposes route plus expected and
installed versions, and its controller repeats that qualification-specific
check immediately before a new claim. Production uses the separate
active-profile admission check above, so ambient CLI drift cannot become a
paid late failure in either lane.

If Cursor completes a role but reports one exact catalog-approved identity
alias, the controller authenticates the receipt, route, terminal, progress,
role-owned Git delta, and charge before creating or extending the signed
passport. It never replays the provider call. Evidence mismatch remains a
release-bound refusal; a local push or materialization failure remains
retryable and cannot create a second charge or completed-role record.

Automatic qualification fallback failures are persisted only as a bounded
typed family. The same release does not hot-loop them; a sealed successor may
retry after repair. A passportless fallback already followed by release
migration can reopen only when its failed terminal and approval authenticate
the final fallback revision and every later revision is an exact Kit migration.

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
No-change revisions bind the prior resolution by SHA-256 instead of copying it;
full refreshed evidence is recorded only when its physical identity changes.
Legacy revisions containing full prior resolutions remain valid.

Contract 1.9 may preview one to four authorized ticket worktrees together with
`models migrate-batch-plan`. Its approval hash binds the protected-main
snapshot and every ticket's branch, head, worktree, migration preview, and
readiness digest. `models migrate-batch` applies the existing per-ticket
transaction concurrently only up to `MAX_CONCURRENT_TICKETS`; each push still
has independent remote authorization and compare-and-swap protection. Results
are recorded in an owner-only signed journal, so a partial failure or crash is
retried without discarding successful siblings or repeating completed pushes.

## Operator commands

```bash
# See and preview profiles.
~/.factory/bin/factory-launch <project> models profiles --json
~/.factory/bin/factory-launch <project> models plan --profile <profile-id> --json

# Pin the initial six-role plan.
~/.factory/bin/factory-launch <project> models pin \
  --ticket T-123 --workdir /absolute/ticket-worktree --json

# Preview and apply a bounded Contract 1.9 release-migration batch.
~/.factory/bin/factory-launch <project> models migrate-batch-plan \
  --ticket T-123 --workdir /absolute/ticket-worktree \
  --ticket T-124 --workdir /absolute/second-worktree --json
~/.factory/bin/factory-launch <project> models migrate-batch \
  --approve-hash <batch-approval-hash> --approved-by <operator-id> \
  --ticket T-123 --workdir /absolute/ticket-worktree \
  --ticket T-124 --workdir /absolute/second-worktree --json

# Preview an eligible mid-ticket fallback.
~/.factory/bin/factory-launch <project> models fallback-plan \
  --ticket T-123 --failed-run <run-id> \
  --workdir /absolute/ticket-worktree \
  --reason credits_exhausted --json

# After the operator fallback-approve receipt above is issued:
~/.factory/bin/factory-launch <project> models fallback \
  --ticket T-123 --failed-run <run-id> \
  --workdir /absolute/ticket-worktree \
  --reason credits_exhausted --json
```

Only `credits_exhausted` and `provider_unavailable` are eligible mid-ticket
reasons. Logic errors, test failures, unsafe worktree changes, stale evidence,
and ambiguous accounting do not authorize a model fallback.

For a certified `https://github.com/...` product origin, fallback and route
migration receive the owner-authenticated GitHub token only for their exact
`ls-remote`, push, and remote verification subprocesses through
`gh auth git-credential`. The token is
removed before model readiness runs and is never placed in a URL, argument,
repository configuration, fallback journal, or role environment. SSH and local
remotes keep their existing credential-free path.

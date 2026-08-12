# Hermes Factory Orchestrator Plan

## Status

Proposed for later execution. No runtime, profile, board, product, release, or
service change is authorized by this document.

Independent multi-agent corroboration is still required before implementation.
This plan was written in a side conversation where sub-agents were unavailable;
Phase 0 records the reviews that the executing thread must obtain.

## Outcome

Use Hermes as a thin supervisor for factory-controlled ticket dispatch while
keeping the existing ownership boundaries:

- The operator, through one-use receipts (`factory-kit.sh operator ACTION`),
  remains the only authority for priority, Ready, approval, and unblock
  decisions. There is no external board.
- The software factory remains the authority for eligibility, capacity,
  ticket leases, sequencing, budgets, worktree validity, role launches,
  maintenance, activation, rollback, and escalation.
- Hermes schedules work only through the installed `factory-launch` contract.
- Each ticket runs in its own exact linked Git worktree and ephemeral Hermes
  dispatcher session.
- Codex and Claude remain role workers selected by the factory's backend policy.
- Operator approval, product activation, rollback, and shared service changes
  remain explicitly serialized.

The first live version supervises one product and one ticket at a time. It may
increase to two Nysa tickets only after the bounded-concurrency contract is
merged, certified, activated, and its evidence policy passes.

## Current state

Today Hermes can dispatch one explicitly named project/ticket through the
`factory-dispatch` skill. That skill calls the stable launcher for contract,
doctor, preflight, sequencing, role execution, and close-out reordering. The
launcher resolves one sealed release and rejects an invalid ticket branch or
worktree.

Ticket selection is still initiated by an operator/session rather than a
durable supervisor. Operator receipts are read into `factory/operator-map.json`
on demand and must remain outside the synchronous execution path.

Hermes Kanban is not part of the live product workflow. Do not copy Nysa or
Relay tickets into it. Historical conformance tasks on a Hermes board are not
an orchestration dependency.

## Target flow

```text
operator receipt (factory-kit.sh operator ACTION)
        |
        v
existing per-product reconciler
        |
        v
local product ticket records
        |
        v
factory-owned deterministic queue/claim command
        |
        v
Hermes factory supervisor
        |
        +--> ephemeral dispatcher: project A / ticket 1 / worktree 1
        |
        +--> ephemeral dispatcher: project A / ticket 2 / worktree 2
                    |
                    v
          existing factory-launch role boundary
                    |
                    v
       Planner -> Spec-linter -> Test-author -> Builder
               -> Reviewer -> Narrator -> operator wait
```

Hermes never selects work by interpreting ticket prose. It consumes only
versioned launcher JSON produced from locally reconciled factory state.

## Non-goals

- Do not replace the operator receipt flow with Hermes Kanban.
- Do not add a second gateway, queue database, orchestration web service, merge
  queue, or long-lived integration branch.
- Do not let Hermes read or write lease files, activation records, ledgers,
  credentials, runtime controls, or registry files directly.
- Do not let Hermes approve, merge, change product pins, activate releases,
  recover stale leases, clear maintenance, or silently retry failed role runs.
- Do not move role sequencing, budget enforcement, model-family separation, or
  worktree validation out of the factory.
- Do not automatically ingest arbitrary Hermes or Codex desktop sessions as
  factory work.

## Phase 0 — Independent corroboration

Before editing code, delegate three read-only reviews and save their notes in
the implementation PR:

1. **Factory safety review**
   - Verify that the proposal preserves role separation, approval, budget,
     maintenance, activation, rollback, lease, and worktree invariants.
   - Identify every launcher, sequencer, release-manager, and kill-switch path
     affected by supervisor-driven dispatch.
2. **Hermes integration review**
   - Verify supported gateway/cron process launching, child-session monitoring,
     profile isolation, shutdown, and dashboard behavior for the installed
     Hermes version.
   - Confirm that Hermes Kanban and native Kanban workspaces can remain disabled.
3. **Operations and failure review**
   - Model duplicate wakeups, supervisor crashes, stale reconciliation,
     exhausted capacity, blocked tickets, budget refusal, maintenance, and
     operator recovery.
   - Confirm the smallest safe rollback and the observability needed to operate
     it.

Resolve disagreements in the plan before implementation. Any recommendation
that gives Hermes authority currently owned by the factory or the operator is
a stop condition.

## Phase 1 — Freeze the supervisor contract

Add the smallest versioned, read-only/atomic launcher boundary needed for a
supervisor. Prefer extending the current contract over introducing a service.

### Deterministic selection

Expose one factory-owned command, provisionally:

```text
factory-launch <project> dispatch-plan --json
```

It returns one of these actions:

- `START <ticket>`: one locally reconciled ticket is eligible and capacity is
  available;
- `WAIT`: no eligible ticket or no capacity;
- `ESCALATE`: stale/invalid state requires an operator.

The implementation, not Hermes, must:

- require a fresh read of `factory/operator-map.json` before selecting new work;
- consider only tickets whose locally recorded state is `Ready` or an
  explicitly resumable factory state;
- apply a documented deterministic ordering using locally reconciled priority
  and a stable ticket-ID tie-breaker;
- exclude leased, nonterminal, blocked, approval-waiting, malformed, wrong-pin,
  and unsafe tickets;
- return no repository path, credential, or unredacted diagnostic supplied by
  ticket content;
- make selection and claim one atomic operation when concurrency is enabled.

Do not make Hermes scan Markdown and then claim in a second step; that creates a
selection race.

### Worktree preparation

Expose or reuse one factory-owned helper that resolves the configured
`WORKTREES_DIR`, exact ticket branch, and physical linked worktree. It may:

- safely reuse an existing clean exact-ticket worktree;
- create the exact ticket branch/worktree from current protected `origin/main`
  when neither exists; and
- refuse symlinks, a detached/wrong branch, foreign repositories, dirty reused
  worktrees, branch divergence, or path collisions.

The existing launcher remains responsible for validating the result again at
every role launch. Hermes receives the validated worktree path; it does not
construct one from ticket text.

### Lease capability

When the active factory contract supports bounded concurrency, the atomic
selection result includes an opaque ticket lease capability. Hermes may pass it
back only to documented launcher commands. It must never place the value in a
role prompt, child-agent environment, board comment, log, evidence bundle, or
dashboard message.

Contract 1.0 behavior remains supported for a one-ticket supervisor without a
dispatch lease. Do not emulate concurrency on contract 1.0.

## Phase 2 — Add the Hermes supervisor skill

Add a canonical `factory-supervisor` profile skill beside `factory-dispatch`.
It performs only this loop:

1. Accept a trusted project slug from its installed job configuration.
2. Run contract and doctor; stop on unknown schema/version or any error.
3. Ask `dispatch-plan` for one atomic action.
4. On `WAIT`, exit successfully without creating a session.
5. On `ESCALATE`, publish one redacted operator message and exit.
6. On `START`, prepare/resolve the exact worktree through the launcher.
7. Start one ephemeral `factory-dispatch` session with project, ticket,
   validated worktree, selected sealed release, and lease capability.
8. Record only non-secret identifiers needed to detect a duplicate live
   dispatcher.
9. Repeat only while capacity remains; never busy-loop.

The existing per-ticket dispatcher remains responsible for `preflight`,
`next-stage`, and every role launch. Avoid merging the two skills until real
operation proves that a separate supervisor adds no useful boundary.

## Phase 3 — Trigger through the existing Hermes gateway

Use one existing Hermes cron/gateway job per supervised product. Do not add a
second daemon.

- Run on a short fixed interval, initially every three minutes; there is no
  external reconciliation cycle to wait on.
- Start with `nysa-app` only; add Relay only after the pilot proves isolation.
- Make duplicate wakeups harmless through the factory's atomic claim and
  Hermes live-session check.
- Disable the job with one reversible profile/job change.
- Keep the dashboard observational: show project, ticket, stage, worktree, kit
  SHA, elapsed time, and redacted terminal reason. It is not a board.

Do not copy secrets into a cron definition or LaunchAgent. The validated
factory profile remains the only credential source accepted by the launcher.

## Phase 4 — Define terminal and recovery behavior

The supervisor treats every launcher refusal, malformed result, post-submit
failure, timeout, budget stop, maintenance state, activation state, and unknown
action as terminal for that attempt. It never selects a fallback path itself.

- `AWAIT-OPERATOR`: finish close-out preparation, release execution capacity
  only after no matching role run is active, and wait for the operator's
  `operator approve` receipt.
- `BLOCKED`/`ESCALATE`: project a concise reason through the normal ticket log,
  release only when the factory permits it, and stop.
- Hermes crash before a role launch: a duplicate wakeup must observe the atomic
  claim and refuse duplicate execution.
- Hermes crash during a role run: the active-run record remains authoritative;
  no new dispatcher may overlap it.
- Stale lease: never steal it. Recovery requires maintenance, proof that no
  matching run is alive, and the existing explicit release-manager recovery.
- Maintenance, kill, activation, and rollback block new supervisor claims and
  drain existing role runs through the factory's current controls.

## Phase 5 — Verification

### Deterministic tests

Add the smallest tests proving:

- fresh Ready tickets are selected deterministically;
- an invalid or unreadable operator map cannot start a new ticket;
- duplicate wakeups produce one claim and one dispatcher;
- a duplicate ticket claim fails;
- capacity one starts only one ticket;
- capacity two starts two distinct tickets and a third waits;
- a blocked ticket does not stop the other ticket;
- exact branches and distinct worktrees remain mandatory;
- lease IDs never reach role prompts, adapters, logs, or evidence;
- budget reservations remain atomic;
- maintenance, kill, activation, and rollback stop new selection;
- crash/stale recovery cannot overlap a live process;
- contract 1.0 single-ticket manual dispatch still works;
- Relay remains functional when it does not enable supervisor dispatch.

Run the full repository suite, repository check, secret scan, Linux CI, macOS
system-Bash CI, and immutability gate.

### Isolated real-Hermes canary

Create a separate canary profile, registry, product, tickets, worktree root,
ledger, and mock adapters. Do not copy production credentials or board state.
Prove:

1. one supervisor wakeup starts exactly one eligible ticket;
2. the dispatcher follows every authorized role to an operator wait;
3. a second wakeup does not duplicate it;
4. two-ticket mode respects capacity and worktree isolation;
5. a third ticket waits;
6. a simulated supervisor crash cannot overlap a live role;
7. maintenance prevents a new claim and drains existing work;
8. all manifests bind the exact sealed kit and product trees.

## Phase 6 — Safe rollout

1. Merge the factory candidate through protected Git; it remains inert.
2. Install it as a sealed release and run the isolated canary.
3. Run the supervisor in read-only shadow mode for several reconciliation
   cycles. Compare its proposed action with the operator's expected ticket and
   record disagreements without launching anything.
4. Certify the exact Nysa tree and candidate release.
5. Through a protected Nysa compatibility PR, enable only the single-ticket
   supervisor job; do not enable two-ticket concurrency yet.
6. Activate under maintenance using the standard journaled cutover and health
   checks.
7. Complete at least two low/medium-risk tickets without duplicate execution,
   manual state repair, worktree contamination, or budget drift.
8. Only after the bounded-concurrency evidence gates pass, enable capacity two
   for two independent low/medium-risk tickets with no external sends or
   overlapping file ownership.
9. Keep Relay on its existing behavior during the Nysa pilot. Add it to the
   supervisor only through a separate certification and activation.

## Rollback

The first response to an orchestration defect is to disable the Hermes
supervisor job. Existing manual `factory-dispatch` remains available through
the same sealed launcher, so removing automatic selection does not stop manual
ticket delivery.

If the defect is in the activated factory contract rather than the Hermes job:

1. publish maintenance and drain active role runs;
2. reconcile any incomplete activation transaction;
3. restore the protected previous product pin/tree;
4. run formal factory rollback when a committed previous generation exists;
5. restore the previous factory profile/job bundle;
6. verify contract, doctor, worktree state, budgets, and both
   Nysa and Relay health before clearing maintenance.

Never resolve an orchestration incident by deleting a lease, PID record,
ledger row, activation journal, or worktree.

## Parallel interactive sessions

Codex/Hermes interactive sessions may run while the supervisor operates when
all of these are true:

- each coding session owns a different branch and linked worktree;
- no interactive session edits a factory-owned active ticket worktree;
- no session launches a role outside `factory-launch`;
- activation, rollback, maintenance, product pin changes, and shared gateway
  changes remain serialized;
- interactive work does not count as an unrecorded substitute for a factory
  ticket.

The supervisor should expose enough status for an operator to see which
worktrees are factory-owned before starting manual work.

## Acceptance criteria

The orchestrator is ready for normal Nysa use only when:

- the operator, through receipts, is still the only source of priority/Ready/approval/unblock decisions;
- Hermes makes no eligibility, sequencing, safety, budget, approval, activation,
  or recovery decision independently of the launcher;
- disabling one Hermes job immediately returns the system to manual dispatch;
- duplicate wakeups and supervisor restarts cannot duplicate ticket execution;
- every role run uses an exact linked ticket worktree and sealed kit release;
- single-ticket behavior remains compatible with the previous contract;
- the two-ticket canary and live pilot prove bounded capacity without budget or
  worktree races;
- maintenance, kill, activation, rollback, and stale recovery remain fail-closed;
- Nysa and Relay health and rollback evidence pass after the activation drill.

## Deliverables

- Independent review notes from Phase 0.
- Versioned launcher contract and deterministic queue/claim implementation.
- Canonical `factory-supervisor` Hermes skill and profile template.
- Focused and full regression tests.
- Updated architecture, Hermes integration, setup, operator runbook, and durable
  decision log.
- Isolated real-Hermes canary evidence.
- Protected Nysa compatibility/policy PRs and rollback PR.
- Single-ticket operating report followed, only if accepted, by the bounded
  two-ticket pilot report.

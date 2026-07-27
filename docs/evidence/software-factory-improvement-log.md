# Software Factory improvement log

This is the durable index of systemic delivery failures. Ticket state,
accounting manifests, and qualification results remain authoritative in their
existing records. Append an occurrence to an existing root cause instead of
creating a duplicate entry.

## FI-20260726-001 — Product decisions reached provider roles too late

Status: Validating
Area: readiness
Owner: Product
First seen: 2026-07-26, Generation 9, T-079/T-081
Impact: two tickets could not enter an executable lifecycle
Evidence:
- `docs/evidence/2026-07-26-sandbox-factory-rolling-ten-recovery-handoff.md`
Root cause: admission checked ticket shape but not whether product identity and
navigation decisions were settled.
Smallest change: resolve stable keys and navigation outcomes before Ready.
Validation: T-079 and T-081 reach Spec-linter without a product-decision return.

## FI-20260726-002 — Spec PASS did not prove fixture executability

Status: Validating
Area: lifecycle
Owner: Factory
First seen: 2026-07-26, Generation 9, T-080/T-084
Impact: two downstream role calls exposed contract contradictions after Spec PASS
Evidence:
- T-080 uppercase UUID fixture conflict
- T-084 unauthenticated workspace fixture conflict
Root cause: Spec-lint validated semantics but not that named fixtures could
execute inside the isolated role boundary.
Smallest change: require executable fixture and authentication seams before PASS.
Validation: focused fixtures execute unchanged in Test-author and Builder.

## FI-20260726-003 — Cross-role repair ownership caused semantic churn

Status: Validating
Area: lifecycle
Owner: Factory
First seen: 2026-07-26, Generation 9, T-080/T-082
Impact: repair ownership was known in prose but the controller selected the
ordinary next role
Evidence:
- Generation 9 T-080 contract blocker and T-082 Reviewer manifests
Root cause: repair instructions did not expose one machine-readable named owner.
Smallest change: preserve signed role detail and honor one committed
`OPERATOR RESUME` target only after the latest contract-blocked attempt.
Validation: the next repair returns directly to Reviewer with no successful-role replay.

## FI-20260726-004 — Batch drain blocked independent ticket recovery

Status: Validating
Area: scheduling
Owner: Factory
First seen: 2026-07-26, Generation 9, T-080/T-082/T-084
Impact: terminal T-080/T-084 waited about 41 minutes for active T-082
Evidence:
- `scripts/factory-dev-lane.sh`
Root cause: checkpoint and resume gates required global lane drain.
Smallest change: ticket-scoped approvals, selected-ticket drain, and portable
v2 passport export.
Validation: export one terminal ticket while a sibling provider attempt is active.

## FI-20260726-005 — Cancellation surfaces did not converge

Status: Implemented; qualification pending
Area: recovery
Owner: Factory
First seen: 2026-07-26, Generation 9, T-084
Impact: coordinator was terminal while manifest, PID, claim, and dirty worktree disagreed
Evidence:
- Generation 9 T-084 cancellation attempt and retained lane evidence
Root cause: wrapper termination could bypass final manifest, ledger, PID, and
claim cleanup after coordinator terminalization.
Smallest change: one ticket pause/cancel path must prove process-group drain and
all terminal evidence before releasing capacity; interrupted worktree output is
retained on a diagnostic ref before rollback.
Validation: a post-GO cancellation leaves zero selected reservations, leases,
claims, PID files, or reserved manifests.

## FI-20260726-006 — Qualification totals lagged terminal attempts

Status: Open
Area: accounting
Owner: Factory
First seen: 2026-07-26, Generation 9
Impact: tracked evidence omitted 22 later attempts and about $220
Evidence:
- `factory/qualification/generation-9.json` in the isolated Nysa qualification worktree
Root cause: generation evidence was updated at publication boundaries instead
of reducing terminal manifests continuously.
Smallest change: reconcile terminal attempts into generation evidence before
each dispatch and merge decision.
Validation: manifest-derived totals equal the generation record at every merge.

## FI-20260727-007 — Portable checkpoints could double-count prior charges

Status: Implemented; qualification pending
Area: accounting
Owner: Factory
First seen: 2026-07-27, T-080/T-084 passport recovery
Impact: completed role charges could consume the only hard delivery limit twice
Evidence:
- retained v5 accounting manifests and successor v2 passports
Root cause: the checkpoint writer did not retain its seed accounting snapshot,
while lineage validation treated cumulative checkpoint charges as incremental.
Smallest change: retain the seed manifest, export cumulative charges from it,
and require monotonic equality between checkpoint charges and reservations.
Validation: a successor preserves prior totals exactly and rejects additive
double-counting.

## FI-20260727-008 — Contract-repair passports retained an exception state

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-080 successor resume
Impact: an authenticated `FIX test-author` checkpoint stopped before dispatch
because its prior contract blocker had returned the ticket to Backlog; T-082
later completed Reviewer round 3 but could not reconcile its two imported
reviewer verdicts.
Evidence:
- T-080 successor timing report contains no attempt and reports
  `FAILED_STAGE=T-080:state-transition`
- T-082 Reviewer manifest is successful while its controller reports
  `FAILED_STAGE=T-082:reviewer`
Root cause: portable import restored the exception state even though the
checkpoint's authenticated next stage already identified the repair phase,
and reviewer reconciliation counted only current-lane manifests.
Smallest change: materialize the phase from v2 `next_stage` during import and
count its authenticated reviewer prefix during reconciliation.
Validation: T-080 resumes at Test-author, and T-082 records round 3 without
replaying either role.

## FI-20260727-009 — Evidence validation rejected Markdown emphasis

Status: Implemented; qualification pending
Area: evidence
Owner: Factory
First seen: 2026-07-27, T-084 Narrator
Impact: a successful Narrator stopped after writing both required visual-gate
markers because it emphasized them with ordinary Markdown bold syntax.
Evidence:
- T-084 Narrator manifest succeeds while bundle validation reports
  `development evidence bundle lacks backend-only screenshot evidence`
Root cause: the semantic marker check accepted only unformatted text.
Smallest change: accept the same exact marker with optional Markdown emphasis.
Validation: emphasized markers pass; missing or mismatched markers still fail.

## FI-20260727-010 — Late provider callbacks polluted portable review detail

Status: Implemented; qualification pending
Area: evidence
Owner: Factory
First seen: 2026-07-27, T-084 checkpoint export
Impact: an otherwise complete ticket could not export because a late provider
callback appended its disposable lane path after the Reviewer approval.
Evidence:
- T-084 signed Reviewer detail contains a background-process callback naming
  its old `/private/tmp/nysa-sf-dev.*` lane
Root cause: verdict parsing validated the terminal assistant message but
persisted callback chatter after its canonical verdict.
Smallest change: preserve review text only through the terminal verdict and
required repair owner.
Validation: a concatenated approval callback parses, is removed from durable
detail, and cannot carry its lane path into a successor.

## FI-20260727-011 — Passport import rejected required Narrator evidence

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, T-084 canonical passport
Impact: a clean completed-role passport could not create its successor lane.
Evidence:
- T-084 import reports `product seed commit crosses a control boundary`
Root cause: retained ticket commits allowed the exact ticket contract but not
its exact required `T-NNN-bundle.md`.
Smallest change: allow only the selected ticket contract and selected ticket
bundle under `factory/tickets/`.
Validation: the required bundle survives import; sibling ticket and other
Factory control paths remain rejected.

## Maintenance rule

Record only a systemic failure, backward transition after Spec PASS, sibling
block, or trust/accounting/cancellation divergence. Include immutable evidence,
measured impact, owner, smallest change, and validation. Close an entry only
after a focused regression and one real sandbox canary; then promote the stable
rule into `context/memory.md` and the relevant operating document.

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

## FI-20260727-012 — Completed passports required role replay before export

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, T-080 successor export
Impact: a terminal authenticated passport reached Awaiting Operator with zero
provider calls but could not export until Reviewer and Narrator were replayed.
Evidence:
- T-080 successor reports `TICKET_STATUS=T-080:AWAIT-OPERATOR`
- its export reports `product ticket role evidence is incomplete`
Root cause: the export gate counted authenticated checkpoint roles but still
required Reviewer and Narrator manifests from the current lane.
Smallest change: accept the complete authenticated role set across checkpoint
and current-lane evidence.
Validation: a completed passport exports without replay; a missing role still
refuses unless a successful current-lane manifest supplies it.

## FI-20260727-013 — Patch export ignored the passport Reviewer head

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, T-080 successor publication
Impact: a complete terminal passport passed role validation but could not bind
its approved application patch without replaying Reviewer.
Evidence:
- T-080 publication lane reports `TICKET_STATUS=T-080:AWAIT-OPERATOR`
- export reports `successful reviewer run evidence is missing`
Root cause: patch export searched only the current lane ledger and manifests.
Smallest change: when no current Reviewer exists, bind the parent of the last
authenticated imported Reviewer reconciliation commit.
Validation: the imported reviewed head exports; checkpoint-digest,
reconciliation-path, and post-review drift checks still refuse.

## FI-20260727-014 — Fresh lanes passed a nonexistent Reviewer checkpoint

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-081 final-four attempt
Impact: a successful Reviewer could not reconcile its named Test-author repair,
so the retained ticket could not produce a safe resume approval
Evidence:
- T-081 lane `/private/tmp/nysa-sf-dev.KTvrot` reported
  `reviewer checkpoint is unsafe`
- successful Reviewer manifest `1785145135-51774.meta`
Root cause: Reviewer reconciliation always exported
`FACTORY_DEV_PRODUCT_CHECKPOINT`, even when a fresh lane had no checkpoint
import.
Smallest change: pass the checkpoint environment only when its regular import
file exists.
Validation: `bash ci/factory-dev-lane-test.sh` passes at executable candidate
`d79819d83b0982c201575d3edb49342c08410960`; a successor real-ticket repair
must still prove the no-import path.

## FI-20260727-015 — macOS SIGTERM cancellation could not recover its worktree

Status: Implemented; qualification pending
Area: recovery
Owner: Factory
First seen: 2026-07-27, T-083 final-four attempt
Impact: cancellation terminalized its process and accounting but initially
left the Builder worktree dirty and made pause report failure
Evidence:
- T-083 manifest `1785145126-50967.meta` recorded authenticated
  `cancelled_conservative`, `role_exit=cancelled`, and `exit_status=143`
- diagnostic snapshot `00d2c50e396c664b26e0650ecc8a09f0008e21fc`
Root cause: interrupted-output recovery accepted shell cancellation status 130
but not the observed post-SIGTERM status 143.
Smallest change: accept either 130 or 143 only when every existing
ticket/role/head/remote/GO/accounting binding is valid.
Validation: the focused lane regression passes and the corrected controller
retained T-083's partial Builder tree before restoring a clean trusted head;
the lane has zero active provider attempts, claims, leases, PID files, or
matching processes.

## FI-20260727-016 — Checkpoint-free reconciliation still used a Bash 3-unsafe array

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-081 passport export
Impact: the trusted Reviewer result could not resolve its Test-author repair
stage on the supported macOS Bash 3.2 host
Evidence:
- `scripts/ticket-state.sh` failed at the empty `CHECKPOINT_ARGS` expansion
- T-081 retained all successful roles and did not launch another provider call
Root cause: the scheduler stopped passing a nonexistent checkpoint, but the
shared ticket-state helper still represented that absent option as an empty
array under `set -u`.
Smallest change: invoke Reviewer reconciliation in two explicit branches,
adding `--checkpoint` only when the authenticated path exists.
Validation: `bash ci/ticket-state-test.sh` passes on the macOS host and
exercises a successful checkpoint-free Reviewer reconciliation.

## FI-20260727-017 — Authorized Planner repairs were not runnable

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-079 retained resume
Impact: the authenticated contract-repair stage resolved to `FIX planner`, but
the controller stopped before submission instead of running Planner
Evidence:
- `product_contract_repair_stage` emitted `FIX planner`
- `product_role_for_stage` rejected the same explicit stage
Root cause: the repair parser supported Planner and Spec-linter, while the
shared stage-to-role map supported only Test-author and Builder repairs.
Smallest change: map the four already-authorized repair roles explicitly and
continue rejecting ambiguous or unsupported repair strings.
Validation: `bash ci/factory-dev-lane-test.sh` passes at executable candidate
`655020b610fffe73b005679cba86b91e3cc92469`.

## FI-20260727-018 — Passport replay rejected normal indented Spec evidence

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, T-081 successor import
Impact: an otherwise valid five-role passport stopped before Test-author repair
with `development checkpoint binding is invalid`
Evidence:
- the passport carried canonical `SPEC-LINT: PASS`
- the retained ticket carried the same marker with normal Markdown indentation
Root cause: passport export normalized optional leading whitespace, while the
sequencer's checkpoint-prefix validator required the marker at column zero.
Smallest change: use the exporter's existing semantic match and strip only
leading/trailing whitespace before exact prefix comparison.
Validation: `bash ci/factory-dev-lane-test.sh` passes at executable candidate
`5d611470182614f26fccc61eb751360dfc27c473` with an indented checkpoint fixture.

## FI-20260727-019 — Ticket-scoped completion could not export

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-27, T-081 publication
Impact: a drained `AWAIT-OPERATOR` ticket with its ticket approval consumed
could not produce publication artifacts
Evidence:
- T-081 completed its exact successor repair, Reviewer, and Narrator roles
- `product-export` stopped only because the unused batch approval still existed
Root cause: ticket planning intentionally creates independent ticket approvals
alongside a compatibility batch approval, but export applied the batch-only
unused-approval gate even when an exact ticket selection was supplied.
Smallest change: retain the unused-approval refusal for batch export and let
ticket-scoped export rely on its consumed ticket approval plus existing
terminal role, drain, head, and review checks.
Validation: `bash ci/factory-dev-lane-test.sh` passes at executable candidate
`c64a9247d566198803ff429f24535bfd057c2618` with an inert unused batch
approval present during a selected-ticket export.

## FI-20260727-020 — Retained contract blockers were authenticated against the wrong SHA

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-083 retained repair
Impact: an authenticated Builder contract blocker could not return T-083 to
Backlog after the qualification selected a newer successor candidate
Evidence:
- T-083 manifest `1785150963-94647.meta` records the lane-pinned
  `5d611470182614f26fccc61eb751360dfc27c473`,
  `role_exit_contract_blocked`, exit 12, and conservative accounting
- the transition refused with
  `qualification backlog return lacks authenticated failure evidence`
Root cause: the transition compared the role manifest with the qualification
manifest's mutable candidate field instead of the immutable Factory checkout
that executed the role.
Smallest change: bind the failure manifest to the controller checkout's exact
Git SHA while retaining protected qualification membership and every existing
role, state, exit, and accounting check.
Validation: `bash ci/ticket-state-test.sh` passes at executable candidate
`805de58e2d5c9a9730ee790a0d2d19cc4cb17671`, including stale-successor and
unpinned-role cases.

## Maintenance rule

Record only a systemic failure, backward transition after Spec PASS, sibling
block, or trust/accounting/cancellation divergence. Include immutable evidence,
measured impact, owner, smallest change, and validation. Close an entry only
after a focused regression and one real sandbox canary; then promote the stable
rule into `context/memory.md` and the relevant operating document.

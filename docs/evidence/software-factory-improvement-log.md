# Software Factory improvement log

This is the durable index of systemic delivery failures. Ticket state,
accounting manifests, and qualification results remain authoritative in their
existing records. Append an occurrence to an existing root cause instead of
creating a duplicate entry.

## FI-20260726-001 — Product decisions reached provider roles too late

Status: Implemented; qualification pending
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
Contract 1.8 occurrence: provider-free readiness now rejects unresolved product
decisions before Planner and emits no run or charge record.

## FI-20260726-002 — Spec PASS did not prove fixture executability

Status: Implemented; qualification pending
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
Contract 1.8 occurrence: readiness validates tracked fixture and authentication
seams before the first provider call.

## FI-20260726-003 — Cross-role repair ownership caused semantic churn

Status: Implemented; qualification pending
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
Contract 1.8 occurrence: tickets use authenticated passports and disposable
cells; controller restart and one live relocation are immutable qualification
events.

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

Status: Implemented; qualification pending
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
Contract 1.8 occurrence: the qualification reducer authenticates each passport,
deduplicates run IDs and manifest digests, and matches exact cumulative charges
to the $2/$25/$100 envelope.

## FI-20260727-007 — Portable checkpoints could double-count prior charges

Status: Implemented in development; Contract 1.8 automation pending
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
`805de58cd6be311cbe2046da3a62a5d73be8ad85`, including stale-successor and
unpinned-role cases.

## FI-20260727-021 — Reviewer callback normalization diverged from reconciliation

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-083 Reviewer round 2
Impact: the trusted role wrapper accepted one Request-changes verdict, but
Reviewer reconciliation refused the same authenticated output and could not
route the named Test-author repair
Evidence:
- manifest `1785152416-67411.meta` records a successful, accounted Reviewer
- its Cursor stream repeats the final verdict only as a late background-shell
  callback and consistent `FIX-OWNER: test-author` restatement
- resume stopped with
  `contract 1.7 request changes requires exactly one FIX-OWNER`
Root cause: the shared verdict parser recognized backtick-first callback text
but not Cursor's observed `background shell (the first ...)` wording and
`My round-2 verdict stands` summary.
Smallest change: extend the existing callback/summary normalization patterns;
retain the later-summary and identical-owner checks.
Validation: all 24 focused Cursor-stream tests pass at executable candidate
`39240c4fcdd18e4cc274f878d70de6bb14189f51`, and the exact T-083 output now
parses as one `REQUEST CHANGES` / `test-author` result.

## FI-20260727-022 — Documentation-only operator notes invalidated Reviewer evidence

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, T-083 bounded repair authorization
Impact: canonical Reviewer evidence could not reconcile after the operator
recorded its permitted ticket-only budget ruling
Evidence:
- Reviewer attempt `1785152416-67411` is bound to product head `2776910`
- current head `cc33870` is its descendant and changes only
  `factory/tickets/T-083.md`
- reconciliation stopped with
  `unmatched reviewer evidence is not bound to the current ticket head`
Root cause: the trust check required SHA equality even though the qualification
handoff explicitly permits operator ticket/contract/evidence corrections.
Smallest change: accept only an ancestor chain whose complete changed-path set
is the selected ticket document; preserve exact Reviewer before/remote equality
and reject every other path.
Validation: all 25 focused Reviewer/Cursor tests pass at executable candidate
`4e68e0b11d18c55c24c5a75d6f556727337d37f3`; the exact T-083 evidence
reconciles to `FIX test-author`.

## FI-20260727-023 — Protected CI could not reopen a completed checkpoint

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-27, T-081 protected publication
Impact: T-081 reached operator-await and exported without replay, but a stale
sibling-owned protected test failed after publication and the Factory had no
authenticated route back to its owning Test-author.
Evidence:
- PR 222 failed only
  `apps/web/tests/meetings-page.test.tsx` criterion 10 after T-081 replaced the
  old detail placeholder with its approved Meeting detail page
- the imported checkpoint's exact next stage remained `AWAIT-OPERATOR`
- the existing `OPERATOR RESUME` trigger correctly required a prior
  contract-blocked role and could not authenticate this publication failure
Root cause: operator-await checkpoints were terminal to development sequencing
even when protected GitHub CI supplied a new, exact role-owned repair.
Smallest change: accept one exact GitHub Actions job URL plus one named
`OPERATOR PUBLICATION REPAIR` directive on an authenticated operator-await
checkpoint; run only Builder or Test-author, then require fresh Reviewer and
Narrator evidence.
Validation: shell syntax checks, the focused publication-repair parser
fixture, and the focused checkpoint sequence
`AWAIT → Test-author → Reviewer → Narrator → AWAIT` pass at executable
candidate `592d57f2d2d6e656b6349fe83d5a8726c19b3d59`. The first zero-submission
T-081 canary proved portable export records the authenticated reopen directly
as `FIX test-author`; that exact checkpoint shape now passes the same focused
sequence.
Contract 1.8 occurrence: publication leases now release on a repeated
same-head CI failure so unrelated green work can merge. The authenticated
publication-repair record now routes only the typed Builder/Test-author repair,
then fresh Reviewer and Narrator roles. Status: Implemented; qualification
pending.

## FI-20260727-024 — Zero-attempt failed plans could not re-export lineage

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, T-081 portable publication repair
Impact: the first T-081 successor consumed its authenticated accounting
lineage, then stopped before provider submission; recovery could not issue the
next successor checkpoint.
Evidence:
- the provider coordinator reports zero attempts and zero active reserve
- planning stopped before creating `product/factory/runtime-ledger.csv`
- checkpoint export failed only while opening that absent zero-row ledger
Root cause: checkpoint export treated the runtime ledger as mandatory even
though failed pre-submission plans legitimately have no runtime rows or file.
Smallest change: reduce an absent runtime ledger to the same empty attempt list
as an existing header-only ledger.
Validation: shell syntax passes and the exact retained T-081 failed-plan lane
exports its authenticated `FIX test-author` checkpoint successfully at
executable candidate `31c56c0ed4204703093ad6dd734b202f792746d9`.

## FI-20260727-025 — Portable checkpoints stopped at a newer protected product base

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, T-083 publication
Impact: T-083 completed its retained roles and exported, but protected `main`
advanced through T-081 before T-083 could publish. Its exact approved product
patch then conflicted in `apps/api/src/app.ts`; replaying successful roles or
resolving the canonical product branch by hand would violate qualification.
Evidence:
- PR 224 is the exact sealed T-083 product patch and reports `CONFLICTING`
  against protected `main`
- the conflict is Builder-owned while the checkpoint remains authenticated at
  operator-await
- the old and current qualification bases differ from their protected bases
  only in qualification control paths
Root cause: portable role evidence was independent of the physical lane but
seed replay still required one unchanged product base.
Smallest change: bind one live conflicting PR to the sealed product patch,
preserve current protected content only at safe Builder-owned conflicts, record
the exact replay, then run Builder, fresh Reviewer, and Narrator. Every test,
configuration, lock, control, symlink, submodule, rename, or unowned conflict
still fails closed.
Validation: shell syntax and one focused synthetic protected-base replay pass;
the real T-083 canary remains the qualification gate.

## FI-20260727-026 — A transient protected test failure reopened ticket work

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-27, T-079 protected publication
Impact: PR 227's first protected `app-tests` execution failed one inherited
T-077 timing assertion. The repair planner prepared a Test-author reopen even
though the reviewed product head had not changed. No provider call occurred,
but the unnecessary control transition delayed T-085 publication.
Evidence:
- workflow run 30276028647 first failed
  `apps/web/tests/product-shell.test.tsx` criterion 13
- policy and test-immutability were green
- the unchanged PR head `bc77d6cc1762fc26dc1bd51da455e5f98a7b7785`
  passed the failed-job rerun and auto-merged as
  `ae91514863a41acffe83491d05dbc115c0b2e491`
Root cause: the publication recovery rule treated the first protected test
failure as durable before distinguishing an unchanged-head transient from a
code defect.
Smallest change: when policy and test-immutability are green and only a test
job failed, rerun failed GitHub Actions jobs exactly once on the same PR head
before reopening a role. A green rerun continues publication; a second failure
routes the exact Builder or Test-author. Control-plane failures never rerun.
Validation: the T-079 same-head rerun passed without a code change, role call,
or successful-role replay, and protected auto-merge completed.
Contract 1.8 occurrence: `ci-rerun` permits one exact-head failed-job rerun only
for a single application-test failure with protected classes green, and
persists the consumed rerun identity owner-locally.

## FI-20260727-027 — A relocated Linear overlay duplicated live tickets

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 setup
Impact: a clean successor qualification root recreated T-110 through T-113 as
SF-64 through SF-67 while their original SF-60 through SF-63 issues remained
active.
Evidence:
- the predecessor and successor `factory/linear-map.json` overlays bind the
  same four ticket titles to distinct issue IDs
- no provider call or ticket transition occurred before detection
Root cause: issue creation trusted only the disposable local mapping and did
not reconcile a missing entry against existing Factory-managed Linear issues.
Smallest change: on a missing mapping, adopt one active exact-title issue with
the Factory banner; ignore canceled issues and fail closed on ambiguity.
Validation: the focused sync test adopts one issue without creating another,
refuses two active matches, and the corrected live sync must recover
SF-60 through SF-63 after SF-64 through SF-67 are recorded as obsolete.

## FI-20260727-028 — Kickoff preflight disagreed with deterministic Planning

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four
Impact: T-110 through T-113 all stopped before their first provider call even
though admission, route pinning, and state-machine transition succeeded.
Evidence:
- all four controller claims recorded `ticket_blocked` with reason `preflight`
- every ticket was in `State: Planning`; no run manifest, reservation, charge,
  or provider process existed
Root cause: legacy preflight required `State: Ready` after Contract 1.8 had
correctly consumed the transition receipt and entered Planning; the controller
also repeated the kickoff check before every later role.
Smallest change: require Planning for the Contract 1.8 Planner receipt and run
preflight only before Planner. Later roles continue from authenticated evidence.
Validation: focused preflight and controller regressions cover exact state
agreement and prove Builder does not repeat preflight; live canary pending.

## FI-20260727-029 — Fresh cells rebuilt over retained remote ticket branches

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 successor root
Impact: all four T-110 through T-113 route pins completed locally but their
non-force pushes refused, so the restarted controller stopped before
state-machine or provider execution.
Evidence:
- each controller error is exactly `could not push ticket pin commit`
- remote ticket branches retain the prior candidate's canonical pin and
  Ready-to-Planning commits, while each fresh local branch starts from newer
  protected main
- there is no role manifest, provider reservation, or charge in either attempt
Root cause: admission queried the remote branch but, when a fresh clone lacked
the local ref, created a new branch from protected main instead of reconciling
the retained exact remote head.
Smallest change: require a protected-main exact-head authorization, validate
that the old branch contains only canonical pin/Planning control changes with
an unchanged ticket contract, non-force merge current main, reset only those
controls, and preserve the old commits in branch history before repinning.
Validation: focused dispatch tests prove the authorized control-only recovery
and reject a similarly authorized branch with ticket-contract drift; live
canary pending.

## FI-20260727-030 — Qualification stripped its isolated-runtime root

Status: Implemented; qualification pending
Area: provider
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four
Impact: all four Planner calls were admitted by the deterministic controller
but refused before provider GO, so no role could execute in the sealed
qualification environment.
Evidence:
- T-110 through T-113 each recorded one `launch_void` Planner manifest with
  `go_issued=0`, `task_submitted=0`, `effective_cost=0`, and
  `failed_pre_go`
- every role log reports
  `subscription CLI isolation requires a valid development-lane attempt`
- the qualification provider database contains four terminal, zero-charge
  attempts and no active reservation
Root cause: the qualification launcher intentionally sanitized its child
environment but did not restore the sealed qualification-root binding, while
the role runner accepted only marked `nysa-sf-dev.*` roots.
Smallest change: generate a qualification marker, pass the already-validated
qualification root through the trusted launcher environment, and let the
existing isolated-runtime setup accept that marked root.
Validation: the focused qualification-environment test, launcher/runner syntax
checks, and exact Hermes contract suite pass; live four-ticket canary pending.

## FI-20260727-031 — Pre-provider recovery was not repeatable

Status: Implemented; qualification pending
Area: checkpoint
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four
Impact: the corrected qualification environment stopped before claiming any
ticket because all four exact authorized branches had already passed through
one prior protected-main recovery.
Evidence:
- T-110 through T-113 each contain only the original canonical pin/Planning
  commits, one Factory-authored recovery merge and supersede commit, and the
  newer canonical pin/Planning pair
- each current branch diff remains exactly its ticket control fields and route
  plan, with no role manifest or provider-authored product change
- admission reports `pre-provider branch is not control-only` for the exact
  authorized head
Root cause: FI-029 validated only a single pin/transition pair and prohibited
all merge commits, including the canonical recovery merge it creates itself.
Smallest change: validate the first-parent history as a repeatable canonical
grammar and require every recovery merge's protected parent to remain an
ancestor of current protected main.
Validation: all 14 focused dispatch tests pass, including two consecutive
protected-main recoveries that preserve lineage and restore the exact current
main tree; the exact Hermes contract suite also passes. Live four-ticket
canary pending.

## FI-20260727-032 — Qualification accepted an unusable Cursor scratch path

Status: Implemented; qualification pending
Area: preflight
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four
Impact: all four Planner preflights passed, but each provider attempt became a
zero-charge launch void before GO because the qualification root made Cursor's
isolated data path longer than its supported scratch limit.
Evidence:
- T-110 through T-113 each record `failed_pre_go`, `go_issued=0`,
  `task_submitted=0`, and `effective_cost=0`
- every role log reports
  `Cursor attempt data path is too long for isolated scratch`
- the provider coordinator has zero active attempts and four terminal
  zero-charge records
Root cause: qualification-environment validation accepted any safe root name,
while the role runner enforced the Cursor data-path limit only after ticket
admission and provider reservation.
Smallest change: reject a qualification root during environment preparation
when a conservative attempt-ID path would exceed the existing Cursor limit.
Validation: focused short-root success and long-root rejection tests pass;
the exact Hermes contract suite also passes. Live four-ticket canary pending.

## FI-20260727-033 — Provider sanitation invalidated the host receipt recheck

Status: Implemented; qualification pending
Area: lifecycle
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four
Impact: all four valid Planner receipts stopped before GO after successful
preflight and provider admission.
Evidence:
- T-110 through T-113 each recorded one terminal `launch_void` Planner
  manifest with `go_issued=0`, `task_submitted=0`, and zero effective cost
- the provider coordinator records all four attempts as `failed_pre_go` with
  zero active reservations
- every role log reports
  `consumed transition receipt is unavailable before GO`
Root cause: the runner cleared `FACTORY_PROJECT` before spawning the provider
wrapper, then its own pre-GO receipt verifier tried to reuse that cleared
variable.
Smallest change: capture the validated project once in a non-exported host
variable and use it for every receipt verification while continuing to remove
project model controls from the provider environment.
Validation: the focused state-machine regression executes the real runner
verification function after provider sanitation, proves the project receipt
binding survives, and proves `FACTORY_PROJECT` is absent from the child
environment. Live four-ticket canary pending.

## FI-20260727-034 — A terminal provider failure stranded its ticket claim

Status: Implemented; qualification pending
Area: provider
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four
Impact: T-112 stopped after its first Planner provider failure while T-110,
T-111, and T-113 continued independently.
Evidence:
- run `1785190722-90871` expected `GPT-5.6 Sol 272K High`, observed
  `GPT-5.6 Sol 1M High`, and terminalized `provider_failed`
- the run charged its conservative $2 reservation and recorded no successful
  Planner evidence
- the controller released provider capacity but marked T-112 blocked instead
  of selecting its approved same-family direct CLI
Root cause: the controller treated every nonzero terminal role as blocked and
never invoked the existing automatic qualification fallback; that helper also
accepted only the obsolete qualification v1 manifest.
Smallest change: accept qualification v2 and invoke the existing fallback only
for the first terminal Cursor failure, retaining the claim and same state. Make
the applied fallback recoverable across controller restart.
Validation: focused fallback and controller regressions cover v2 authorization,
direct-CLI selection, claim retention, idempotent recovery, and refusal after a
second task-submitted attempt. Live four-ticket canary pending.

## FI-20260727-035 — Cancellation plan and apply could never share a hash

Status: Implemented; qualification pending
Area: cancellation
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 final four drain
Impact: targeted cancellation of the three active Spec-linter attempts was
refused before any request was published; all three calls drained normally.
Evidence:
- each apply returned `attempt cancellation preview changed`
- no cancellation request or receipt was created
- the launcher independently generated the plan once for operator preview and
  again for apply
Root cause: each preview generated a new random nonce and wall-clock timestamp,
so identical run state could not reproduce its approved hash.
Smallest change: derive the nonce and timestamp from the exact authenticated
manifest/PID snapshot and reason. Preserve the existing snapshot CAS so any
real drift still refuses.
Validation: the focused cancellation regression proves independent previews
match and manifest/PID mutation still invalidates apply. Live cancellation
canary pending.

## FI-20260727-036 — Qualification fallback assumed a migrated route journal

Status: Implemented; qualification pending
Area: provider
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 successor final four
Impact: T-116's first Test-author Cursor failure retained its $2 conservative
charge but the automatic direct-CLI fallback blocked before preserving the
handoff. T-114, T-115, and T-117 completed Test-author independently.
Evidence:
- run `1785193560-14897` terminalized `provider_failed` on the pinned Cursor
  route with no successful Test-author evidence
- the controller recorded `route journal cannot select its profile` and
  failed closed
- T-116 still had the v1 route plan created by normal ticket pinning
Root cause: `fallback-auto` required a v2 journal even though a same-release
ticket is not migrated before its first role.
Smallest change: for qualification fallback only, atomically preserve the
initial v1 plan as revision zero of a same-release v2 journal and append the
fallback in the same handoff commit. Keep operator fallback restricted to an
existing v2 journal.
Validation: the focused fallback suite proves v1 migration, direct-CLI
selection, one commit, and restart-safe recovery. Live four-ticket canary
pending.

## FI-20260727-037 — Newly created PR checks were parsed before publication

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 successor final four
Impact: T-118 and T-119 created concurrent protected PRs #40 and #39, then
released their ticket leases before Reviewer because GitHub had not yet
reported the required check runs.
Evidence:
- both exact ticket-head PRs were created successfully and their required
  `ci` and `test-immutability` checks later passed
- the controller recorded `GitHub returned invalid required-check evidence`
  for both tickets immediately after PR creation
- GitHub CLI emits an empty nonzero result with the exact
  `no required checks reported` message during this publication gap
Root cause: the required-check parser handled JSON `pending` and an empty JSON
array as wait, but tried to parse the CLI's exact no-checks-yet response as
JSON.
Smallest change: classify only the exact GitHub CLI no-checks-reported response
with empty stdout as `wait`; retain fail-closed handling for every other
non-JSON or nonzero response.
Validation: the focused PR helper regression covers the unreported-to-pending
to-pass lifecycle while retaining malformed-response refusal. Live concurrent
PR canary pending.

## FI-20260727-038 — One failed remote read discarded a valid role

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 successor final four
Impact: T-120's direct-CLI Builder completed the frozen implementation, passed
all 38 application tests and immutability, and committed a clean one-file
change, but the wrapper terminalized it as a failed role instead of publishing
the commit.
Evidence:
- run `1785196489-35130` recorded one successful provider turn, $0.5166, local
  commit `4aac81c`, and `role_exit_remote_mismatch`
- its authenticated pre-role remote head was `b43f148`, and the remote branch
  remained exactly `b43f148` after terminalization
- the provider log contains no fetch, remote-configuration, or push command
Root cause: the shared remote-head observation suppressed transport failure
into an empty head, making one unavailable read indistinguishable from branch
drift before the trusted host push.
Smallest change: retry the exact read once in the shared remote observation
function. Preserve the existing exact-head comparison and fail closed on a
second transport failure or any different head.
Validation: the focused role-exit regression fails the first post-provider
remote read, then proves the same successful role commit is pushed without
replay. Live four-ticket canary pending.

## FI-20260727-039 — Explicit Reviewer heading blocked a valid ticket

Status: Implemented; qualification pending
Area: state machine
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 generation 4
Impact: T-124 completed its read-only Reviewer for $2 with an approval, but
reconciliation rejected the terminal prose and permanently blocked the ticket
while the three independent Narrators continued.
Evidence:
- run `1785201904-89978` ended cleanly at unchanged head `3386c39`
- the final assistant emitted the explicit `## Verdict: APPROVE` heading
- the manifest recorded `role_exit=ok`, then controller event
  `1785202107810107000-2b8e00c62226f380` recorded
  `reviewer result must contain one unambiguous verdict`
Root cause: the shared parser recognized standalone and bold verdicts but not
the exact Markdown heading already accepted by the development lifecycle; the
wrapper also recorded Reviewer success before validating semantic output.
Smallest change: accept only the exact Markdown `Verdict:` heading form, run
the same parser before Reviewer terminal success, and rerun only semantically
invalid Reviewer output under the existing ticket budget.
Validation: focused parser and controller tests cover the observed heading and
targeted retry without preserving invalid completed-role evidence. Fresh live
four-ticket qualification pending.

## FI-20260727-040 — Aggregate model-pin timeout blocked every ticket

Status: Implemented; qualification pending
Area: state machine
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 generation 5
Impact: T-126 through T-129 were recovered in four cells, then all four claims
were blocked before Planner and before any provider charge.
Evidence:
- the provider ledger remained empty
- all four controller errors recorded `models pin` timing out after exactly
  300 seconds
- each six-role model transaction was still running its own bounded readiness
  probes when the controller killed the aggregate command
Root cause: the controller's generic subprocess timeout duplicated the
model-control probe timeouts and turned their combined wall time into a
delivery stop.
Smallest change: retain the existing bounded readiness probes but remove only
the redundant aggregate timeout from `models pin`; other controller commands
keep their safety timeouts.
Validation: the focused controller test binds model pinning to `timeout=None`
while preserving the default timeout for other launcher calls. Fresh live
four-ticket qualification pending.

## FI-20260727-041 — Concurrent model probes starved every route

Status: Implemented; qualification pending
Area: state machine
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 generations 6 and 7
Impact: T-130 through T-137 were blocked before Planner and before any provider
charge even though the exact installed CLIs passed independent bounded probes.
Evidence:
- both generations retained empty provider-attempt and accounting directories
- generation 7 launched four `models pin` transactions concurrently
- each transaction reported every Cursor, Codex, and Claude route as
  `UNAVAILABLE:version_probe_failed`
- immediately before generation 7, independent exact CLI probes returned
  successfully for Cursor status and Claude version
Root cause: four ticket workers duplicated the same machine-level readiness
probe set concurrently. CLI startup contention exhausted each bounded probe,
including version-only probes, so the resolver had no usable route.
Smallest change: serialize only `models pin` inside one controller
reconciliation. Keep its individually bounded probes and unlimited aggregate
duration; preserve four-way concurrency for task-bearing roles and protected
PR validation.
Validation: the focused controller test starts four ticket workers and proves
that at most one model pin is active while every ticket still progresses.
Fresh live four-ticket qualification pending.

## FI-20260727-042 — Readiness outage permanently blocked clean tickets

Status: Implemented; qualification pending
Area: state machine
Owner: Factory
First seen: 2026-07-27, Relay Contract 1.8 generation 8
Impact: T-139 was permanently blocked before Planner and before any provider
charge when host contention made every bounded CLI readiness probe temporarily
unavailable. The qualification had to stop before a sibling submitted work.
Evidence:
- generation 8 had zero provider-attempt and accounting files
- the host load average was approximately 30 while version probes ran
- the single serialized pin returned no route plan and T-139 became blocked
- the same exact six-role route contract had passed immediately before the
  generation under lower contention
Root cause: model resolution collapsed a plan containing only `READY` and
`UNAVAILABLE` evidence into the same terminal `profile_resolution_failed`
classification used for invalid or unknown route evidence. The controller
therefore released the lease instead of waiting for external readiness.
Smallest change: classify only ready-plus-unavailable resolution failures as
`profile_temporarily_unavailable`; one serialized probe broadcasts `waiting`
to all four claims. Invalid and unknown evidence keeps the terminal fail-closed
path.
Validation: focused controller coverage proves one transient probe leaves all
four tickets waiting without reaching the state machine. A focused backend
policy assertion covers the typed classification. Fresh live qualification
pending.

## FI-20260728-043 — Lease prechecks starved the serialized readiness probe

Status: Implemented; qualification pending
Area: state machine
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 9
Impact: T-142 through T-145 retained their claims through seven shared waits,
but never reached Planner despite zero provider attempts or charges.
Evidence:
- generation 9 recorded 28 authenticated `model_pin_wait` events
- each controller cycle ran four release-integrity-backed lease renewals before
  the sole model pin
- under the controller, Cursor `agent --version` spent its complete 30-second
  bound in local I/O
- with the controller drained, the exact seven-route diagnostic completed in
  86 seconds with Codex and every required Cursor route ready
Root cause: serializing `models pin` did not serialize the four launcher
prechecks that immediately preceded it. Their redundant release-tree scans
created enough local I/O contention to starve the bounded readiness command.
Smallest change: resolve one task-free model plan before the ticket worker pool,
then pin all route-less ticket branches from that in-process batch resolution.
Do not weaken release validation or any adapter timeout.
Validation: focused controller coverage requires one batch readiness call for
four tickets and one shared wait on transient failure. The protected full
regression covers launcher grammar and exact batch commits. Fresh live
four-ticket qualification pending.

## FI-20260728-044 — Background LaunchAgent QoS starved readiness

Status: Implemented; qualification pending
Area: controller
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 10
Impact: T-146 through T-149 recovered in four cells but remained before
Planner with zero provider attempts because every route version probe expired.
Evidence:
- the generation recorded one owner and three authenticated shared waits
- all seven routes reported `version_probe_failed` inside the controller
- the exact Cursor version command completed directly within its unchanged
  30-second bound
- a normal/background launchd job timed out, while an otherwise identical
  `Interactive` LaunchAgent returned the pinned version in 17 seconds
Root cause: the canonical controller plist assigned macOS `Background`
process type to a job that performs bounded local provider-readiness probes.
Smallest change: run the same non-overlapping one-shot controller with
`ProcessType=Interactive`; keep every probe assertion and timeout unchanged.
Validation: the focused controller test parses the canonical plist and binds
its process type. Protected GitHub CI retains the full regression. Fresh live
four-ticket qualification pending.

## FI-20260728-045 — Provider-free preflight repeated every route probe

Status: Implemented; qualification pending
Area: preflight
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 11
Impact: T-150 through T-153 pinned their six-role plans successfully, then all
four stopped before Planner with zero provider attempts or charges.
Evidence:
- one authenticated `model_pin_batch` event covered all four tickets
- all four pinned route plans were committed and pushed
- four concurrent Planner preflights each exceeded the unchanged 300-second
  controller command bound
- each preflight loop re-ran credential-bearing readiness for all six roles
  after the controller had resolved the shared machine plan
Root cause: legacy preflight treated a pinned route as a request to re-probe
each role, contradicting the Contract 1.8 provider-free kickoff boundary.
Smallest change: preflight structurally validates all six pinned selections
without probing; the role runner retains its existing exact selected-route
readiness check immediately before provider admission.
Validation: the focused preflight regression runs a real pinned Contract 1.8
plan, requires Planning agreement, and proves no adapter probe occurs.
Protected GitHub CI retains the full regression. Fresh live qualification
pending.

## FI-20260728-046 — Concurrent fallback probes competed for one launch lock

Status: Implemented; qualification pending
Area: recovery
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 12
Impact: three charged Cursor Planner failures reached the authorized automatic
fallback boundary, then all three tickets blocked before any replay.
Evidence:
- T-154, T-156, and T-157 each recorded one terminal `provider_failed`
  manifest with conservative $2 accounting
- all three controller workers invoked `fallback-auto` concurrently
- every fallback performs task-free machine readiness before acquiring the
  same product `.launch.lock`
- the controller terminalized with `launch lock stuck`; no fallback event,
  fallback commit, or second role manifest exists
Root cause: initial model pinning was batch-aware, but exception recovery still
fanned out three independent readiness transactions before a shared mutation.
Smallest change: serialize only `fallback-auto` within the already
non-overlapping controller. Preserve four-way provider roles and protected PR
validation.
Validation: focused controller coverage requires the automatic fallback path
to enter the shared guard. Protected GitHub CI retains the complete
concurrency regression. Fresh live qualification pending.

## FI-20260728-047 — Failed-role fallback gated on an unrelated future route

Status: Implemented; qualification pending
Area: recovery
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 13
Impact: T-161's charged Cursor Planner failure could not use the authorized
direct-CLI fallback because the future Reviewer Cursor model was unavailable.
Evidence:
- T-161 recorded one terminal `provider_failed` Planner manifest with
  conservative $2 accounting
- its controller error was `reviewer route
  cursor-claude-sonnet-5-thinking-high is INVALID: model_unavailable`
- no Reviewer work had started and the Planner fallback did not need to change
  the pinned Reviewer selection
Root cause: qualification fallback inherited operator fallback's complete
future-role resolution and re-gated every unstarted role on current machine
readiness.
Smallest change: automatic qualification fallback resolves only the exact
failed role and preserves every future pinned selection. Each future role
keeps its existing selected-route probe at provider admission.
Validation: the focused fallback regression makes the unrelated Reviewer route
invalid and requires Builder fallback to direct Codex without changing the
Reviewer selection. Protected GitHub CI retains the complete regression.
Fresh live qualification pending.

## FI-20260728-048 — Concurrent publication refresh blocked independent PRs

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15
Impact: T-167 merged through protected auto-merge, then T-166, T-168, and
T-169 were permanently excluded from the next controller cycle instead of
refreshing against the new protected main.
Evidence:
- Relay PR #59 merged automatically at
  `e3859ecb21a220201fedb2bbde163206dc3ba7ec`
- the concurrent closeout/refresh cycle recorded `cannot lock ref
  'refs/remotes/origin/main'` for T-168
- T-166 and T-169 failed the exact-PR refresh gate while GitHub had not yet
  projected their stale heads as `BEHIND`
- the following automatic cycle admitted only T-167; the three siblings
  remained fail-closed
Root cause: four controller threads mutated one shared Git common directory
during protected-base refresh and closeout. The refresh gate also duplicated
the exact remote-tip and ancestry proof with GitHub's eventually consistent
`mergeStateStatus`.
Smallest change: serialize only protected-base Git mutations inside the
already non-overlapping controller. Keep PR validation concurrent, and bind
refresh to the exact certified remote tip, ancestry, and open PR identity
without trusting `mergeStateStatus`.
Validation: focused controller coverage requires refresh and closeout fetches
to use the same guard; focused attestation coverage accepts `UNKNOWN` only
after exact remote-tip and ancestry proof. Protected GitHub CI retains the
complete regression. Passport-only live recovery is pending.

## FI-20260728-049 — Completion path shadowed claim release

Status: Implemented; qualification pending
Area: controller lifecycle
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15
Impact: T-167's application and closeout PRs merged successfully, but the
controller crashed instead of releasing its completed claim.
Evidence: the authenticated `COMPLETE` transition raised `TypeError:
'PosixPath' object is not callable` at `self.release(claim)`.
Root cause: the controller stored the immutable release directory in an
instance attribute named `release`, shadowing its claim-release method.
Smallest change: rename only the release-directory attribute to
`release_path`.
Validation: focused controller coverage drives a claimed ticket through
`COMPLETE` and requires its claim file to be removed. Protected GitHub CI
retains the complete regression. Live recovery is pending.

## FI-20260728-050 — Qualification release had no trusted upgrade path

Status: Implemented; qualification pending
Area: release migration
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15 recovery
Impact: the retained four-ticket cohort could not use its authenticated
passports with a proven successor without either copying secrets into a fresh
root or hand-editing the active release record.
Evidence: the sealed qualification preparer refused every existing root and
offered only generation-one creation, while passport authentication and the
cumulative provider ledger are rooted in the existing controller directory.
Root cause: qualification release preparation implemented creation but not
the Contract 1.8 cross-release recovery boundary.
Smallest change: add one drained, lock-protected `--upgrade` path that seals
the exact clean successor, requires the product pin and provider policy to
match it, atomically advances the active record, and leaves controller state
untouched.
Validation: focused qualification-environment coverage advances generation
one to two and proves the old release and passport key remain byte-identical.
Protected GitHub CI retains the complete regression. Live recovery is pending.

## FI-20260728-051 — Migrated blocked claims could not reacquire leases

Status: Implemented; qualification pending
Area: controller recovery
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15 recovery
Impact: T-166, T-168, and T-169 preserved authenticated passports and
successfully migrated their route journals, but remained excluded from every
controller cycle because their old failure paths had released their dispatcher
leases and left their claims blocked.
Evidence: all three owner-only claims were `blocked`, their exact lease files
were absent, and `runnable` excludes blocked claims before reconciliation.
Root cause: cross-release passport migration did not include the claim/lease
rebind needed after an old controller error.
Smallest change: only when a blocked claim's passport names an older release,
claim its exact ticket lease, authenticate and migrate the passport, then
return it to `claimed`. Same-release blockers remain unchanged.
Validation: focused controller coverage proves the old missing lease is
replaced, passport migration runs, and only then does the claim become
runnable. Protected GitHub CI retains the complete regression. Live recovery
is pending.

## FI-20260728-052 — One protected remote read blocked recovered work

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 retained recovery
Impact: T-166's authenticated claim and passport recovered under the final
candidate, but its protected-base refresh terminalized before mutation and
blocked the ticket on one failed `git ls-remote`.
Evidence: the automatic controller returned `ticket-attest: Git operation
failed: ls-remote`; the ticket branch, PR head, role evidence, and passport
were unchanged, and no provider run existed.
Root cause: the role wrapper already retried one exact read-only remote
observation, but protected attestation treated the first transport failure as
durable.
Smallest change: retry only a failed `ls-remote` once inside the shared
attestation Git helper. A second failure, any mutation failure, or any
semantic mismatch still fails closed.
Validation: one focused unit drives a failed then successful exact
`ls-remote` and requires two calls. Protected GitHub CI retains the complete
regression. Live recovery is pending.

## FI-20260728-053 — Terminal orphan claim blocked trusted upgrade

Status: Implemented; qualification pending
Area: release migration
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 retained recovery
Impact: the controller lock was free and no active-run marker or provider
manifest was live, but T-168 retained `status=running` after its authenticated
terminal Reviewer boundary and the trusted upgrade refused the cutover.
Root cause: the upgrade gate treated a claim's bookkeeping status as runtime
liveness even after the owning controller and provider action had ended.
Smallest change: keep the controller lock and active-run markers authoritative;
preserve the orphaned claim for authenticated successor recovery.
Validation: focused qualification coverage upgrades with a retained running
claim and no live action. Protected GitHub CI retains the complete regression.

## FI-20260728-054 — Reviewer formatting consumed valid repair verdicts

Status: Implemented; qualification pending
Area: Reviewer terminalization
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15 recovery
Impact: T-166 recorded three rejected Reviewer outputs and T-168 recorded six,
consuming $18 of conservative qualification budget without advancing either
ticket.
Evidence:
- all nine immutable outputs contain `REQUEST CHANGES` and the explicit
  `test-author` repair owner
- eight concatenate Cursor background-completion text directly to the owner
  line; one uses an exact verdict-only heading plus a bold owner line
- the old parser classified six as missing owner, two as callback-summary
  failures, and one as lacking an unambiguous verdict
Root cause: normalization recognized only a smaller set of semantically
identical Markdown and callback serialization shapes.
Smallest change: normalize exact verdict-only headings, exact wrapped owner
lines, and the observed callback boundary, then require every verdict and owner
signal to agree. Never infer a missing owner.
Validation: all 27 focused Cursor/parser tests pass, and the new parser reduces
all nine immutable rejected outputs to `REQUEST CHANGES / test-author`.
Protected GitHub CI retains the complete regression.

## FI-20260728-055 — Base refresh validity is bound to commit identity

Status: Implemented; qualification pending
Area: publication evidence
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15 audit
Impact: T-169's protected-base refresh reset successful Reviewer and Narrator
evidence and selected another Reviewer call even though protected main changed
only Factory-control metadata.
Evidence:
- the three recorded T-166/T-168/T-169 refreshes all included T-167 changes to
  `app/server.js` and `app/tests/job-detail.test.js`, so their invalidations
  were correct
- T-169's generation-one receipt binds old head `8f01645`, protected base
  `9186fa6`, and an exact base delta of modified `factory/KIT_PIN`, modified
  `factory/QUALIFICATION.json`, and added
  `factory/migrations/inflight-release/42614d9….json`
- its retained Reviewer `1785265731-36177` and Narrator
  `1785266011-49012` remain on the merged branch lineage, with no later Builder
  or Test-author run
- the old resolver nevertheless returned `RUN reviewer`
Root cause: refresh validity was bound to protected-base commit identity, not
the immutable base delta that formed the role's semantic input.
Smallest change: one shared classifier derives the previous protected base from
the receipt's immutable heads. It preserves review only for ordinary regular
blob modifications to exact `factory/KIT_PIN` and
`factory/QUALIFICATION.json`, plus ordinary additions matching exact
`factory/migrations/inflight-release/<40-hex>.json`. The attestor additionally
requires every retained control blob at ticket HEAD to equal the receipt's
protected base. Every other status or path—including application code, tests,
contracts, CI, configuration, renames, type changes, and unknown `factory/**`
paths—keeps the existing invalidation behavior.
Validation: four focused attestation refresh tests pass, including exact
control preservation and unknown-Factory-path refusal. The patched resolver
returns the bundle-attestation boundary for the exact live T-169 receipt
without a provider call, and read-only attestation reduction retains its exact
Reviewer/Narrator manifests with no unexpected post-review path. Protected
GitHub CI retains the complete regression.

## FI-20260728-056 — Authenticated push repair could not re-enter the state machine

Status: Implemented; qualification pending
Area: controller recovery
Owner: Factory
First seen: 2026-07-28, Relay T-169 recovery
Impact: T-169's Test-author output passed focused checks but its trusted
non-force push failed after rewriting the test commit. The exact tested head
was operator-authorized, published, and reconnected to its signed passport
lineage, yet its same-release blocked claim could not resume.
Root cause: the controller recovered blocked claims only across Factory
releases; it had no typed recovery for a repaired `role_exit_push_failed`.
Smallest change: re-admit only that exact terminal failure after the signed
passport validates the clean cell and its exact head equals the remote branch
tip. Rebind the ticket lease, clear only the failed receipt, and let the state
machine rerun the invalidated role. Every other blocked claim remains blocked.
Validation: focused controller coverage requires the authenticated passport,
exact remote head, typed terminal failure, and new ticket lease. Protected
GitHub CI retains the complete regression.

## FI-20260728-057 — Deterministic refresh refusal had no recovery receipt

Status: Implemented; qualification pending
Area: state-machine recovery
Owner: Factory
First seen: 2026-07-28, Relay T-169 recovery
Impact: the state machine correctly detected that T-169's rewritten refresh
receipt no longer directly followed its recorded merge, but discarded the
deterministic refusal before issuing the one-use receipt required by the
authenticated refresh action.
Root cause: `ticket-attest refresh` accepted `REFUSE` receipts while the sole
state resolver treated every nonzero `next-stage` result as an execution error;
the controller therefore could not route the documented repair.
Smallest change: receipt an exact single-line `REFUSE` result only when
`next-stage` exits 1 with empty stderr. Route only the named direct-after-merge
topology refusal through authenticated protected-base refresh. Every other
refusal remains blocked.
Validation: focused state-machine coverage proves the refusal is receipt-bound;
focused controller coverage proves only the exact topology refusal invokes
refresh with that receipt. Protected GitHub CI retains the complete regression.

## FI-20260728-058 — Receipt-bound refresh rejected the failed role state

Status: Implemented; qualification pending
Area: protected-base recovery
Owner: Factory
First seen: 2026-07-28, Relay T-169 recovery
Impact: T-169 obtained and consumed the new exact topology-refusal receipt, but
authenticated refresh stopped because the failed Test-author boundary correctly
left the ticket in `Building`.
Root cause: refresh admitted only publication states even when the sole
state-machine receipt explicitly authorized repair of this topology refusal.
Smallest change: allow `Building` only when the trusted launcher supplies the
exact receipt-verified topology-refusal stage. The refresh commit performs the
existing sealed reset to `Review`; every unreceipted or differently receipted
`Building` refresh remains refused.
Validation: all 46 focused attestation tests pass, including refusal without
the exact stage and successful reset with it. Protected GitHub CI retains the
complete regression.

## FI-20260728-059 — Pre-submission controller interruption stranded exact roles

Status: Implemented; qualification pending
Area: controller recovery
Owner: Factory
First seen: 2026-07-28, Relay T-166/T-168 recovery
Impact: controller replacement terminated two admitted Reviewer actions before
provider submission, conservatively charged $2 each, and left both tickets
blocked even though neither produced role output.
Evidence: both immutable manifests have `phase=abandoned`,
`accounting_state=abandoned_conservative`, `task_submitted=0`,
`exit_status=143`, an empty `role_exit`, and no output digest. Their signed
passports, clean cells, and remote ticket tips still agree exactly.
Root cause: authenticated recovery admitted repaired push failures but not the
equally bounded pre-submission interruption shape required by controller
restart recovery.
Smallest change: re-admit only that exact terminal shape after the existing
passport and remote-head checks, clear only the interrupted receipt, and let
the state machine rerun the same role. Submitted, differently terminated,
untyped, or identity-mismatched actions remain blocked.
Validation: all 19 focused controller tests pass, including exact recovery and
refusal when `task_submitted=1`. Protected GitHub CI retains the complete
regression.

## FI-20260728-060 — Authorized history repair could not migrate its passport

Status: Implemented; qualification pending
Area: passport migration
Owner: Factory
First seen: 2026-07-28, Relay T-169 generation 19 recovery
Impact: T-169's tested rewrite removed the stale test-after-implementation
ancestry and was published under exact operator authorization, but its signed
passport still named the pre-rewrite head. Ordinary ancestry validation
correctly refused the move; reconnecting that head would reintroduce the
protected immutability failure.
Evidence: the passport head is `db5e08f`, the exact clean remote/cell head is
`ff595b6`, protected main `9054d62` authorizes that head for the `edee50e` to
`8b556b5` cutover, and both heads bind the same authenticated route digest.
Smallest change: during cross-release migration only, accept a non-ancestral
head when the protected in-flight authorization exactly binds repository,
source/target kits, ticket, branch, new head, and state, the cell is clean, and
the signed prior route digest is unchanged. Same-release, dirty, unlisted,
route-changing, malformed, or unknown rewrites remain blocked.
Validation: both focused passport continuity tests pass, including refusal
without authorization, and a copied live T-169 passport migrates to the exact
authorized head without mutating production state. Protected GitHub CI retains
the complete regression.

## FI-20260728-061 — Upgrade recovery preceded route migration

Status: Implemented; qualification pending
Area: controller recovery
Owner: Factory
First seen: 2026-07-28, Relay generation 20 recovery
Impact: T-166, T-168, and T-169 migrated their signed passports and reopened
their claims before their preview-bound route migrations ran. The state
machine correctly refused all three because their ticket and journal Kit-SHAs
still named `edee50e`; no provider call started.
Evidence: all three owner-only transition receipts record the exact
`REFUSE ticket Kit-SHA lease does not match the selected kit SHA` stage under
`c13b59f`, while their claims are blocked with empty role/run receipts.
Root cause: cross-release claim recovery checked the passport release but not
the ticket and route-journal release affinity before reopening the claim.
Smallest change: keep the blocked claim and old passport unchanged until both
the exact ticket Kit-SHA and route journal name the successor release. Emit one
typed migration-required event, then use the existing authenticated migration
and claim recovery after the preview-bound route change commits.
Validation: all 19 focused controller tests pass, including hold-before-route
migration and recovery-after-route migration. Protected GitHub CI retains the
complete regression.

## FI-20260728-062 — Budget stage ignored authenticated ticket-cap overrides

Status: Implemented; qualification pending
Area: budget reconciliation
Owner: Factory
First seen: 2026-07-28, Relay T-168 recovery
Impact: T-168 had spent $26 under an operator-authorized $35 ticket cap, but
the state machine stopped at `AWAIT_BUDGET 26000000/25000000` before a provider
call because the budget reducer read only the immutable $25 base envelope.
Root cause: role launch and the ticket-budget stage used different effective
envelope reductions, and budget-wait claims watched only the base envelope
digest.
Smallest change: reuse the authenticated override loader for ticket-scoped
caps and bind budget-wait wakeups to the envelope plus immutable override
records. Invalid, conflicting, expired, or non-ticket overrides remain
fail-closed and cannot raise the cap.
Validation: the focused budget test proves exact exhaustion, then availability
under an authenticated ticket override; the retained T-168 and T-169 evidence
both reduce to `AVAILABLE`. All 19 focused controller tests prove an override
record or base-envelope change reopens reconciliation. Protected GitHub CI
retains the complete regression.

## FI-20260728-063 — Authorized rewrite exposed an unhandled stale refresh topology

Status: Implemented; qualification pending
Area: protected-base recovery
Owner: Factory
First seen: 2026-07-28, Relay T-169 recovery
Impact: the exact operator-authorized T-169 test-history repair correctly
removed obsolete ancestry, but its retained generation-two refresh receipt
named the removed merge. The state machine failed closed before a provider
call with `REFUSE stale refresh receipt does not bind this branch history`;
only the sibling direct-after-merge topology refusal had a recovery route.
Root cause: the controller and `Building` refresh admission enumerated one of
the two exact receipt-topology failures even though the existing authenticated
refresh action safely replaces either with a new protected-base merge and
receipt.
Smallest change: route both exact topology refusals through that existing
receipt-bound refresh action. Do not accept or rewrite the stale receipt, and
do not admit any other `Building` refresh.
Validation: all 19 focused controller tests prove both exact strings invoke
only authenticated refresh, and the focused attestation test proves the stale
history refusal admits the sealed reset while an unreceipted `Building`
refresh remains refused. Protected GitHub CI retains the complete regression.

## FI-20260728-064 — Exported terminal checkpoint was not restart-idempotent

Status: Implemented; qualification pending
Area: controller recovery
Owner: Factory
First seen: 2026-07-28, Relay T-166 generation 21 recovery
Impact: T-166's successful Reviewer run and $2 charge were already recorded in
its authenticated passport, and Reviewer reconciliation had pushed, but the
controller stopped before clearing the `running` claim. Restart would attempt
to export the consumed receipt again instead of completing the checkpoint.
Root cause: terminal recovery distinguished only active versus terminal runs;
it did not recognize the exact post-export/pre-claim-clear boundary.
Smallest change: when the passport contains exactly one matching charge record
and, for success, exactly one completed-role record for the claim's run, role,
and receipt, authenticate and migrate that passport instead of re-exporting.
All partial, duplicate, mismatched, or unsigned checkpoints still fail closed.
Validation: all 20 focused controller tests pass, including proof that the
exact checkpoint migrates twice through the existing idempotent boundary,
never calls export, and clears the claim. The retained T-166 state resolves
`True` only for run `1785262040-61879` with `role_exit=ok`. Protected GitHub CI
retains the complete regression.

## FI-20260728-065 — Budget reopen fell back through fresh admission

Status: Implemented; qualification pending
Area: admission and controller recovery
Owner: Factory
First seen: 2026-07-28, Relay generation 23 recovery
Impact: the corrected budget digest removed T-168's budget-wait claim. Fresh
admission then treated its mid-ticket branch as a pre-provider branch divergent
from protected main, hit `ticket remote branch does not match reset
authorization`, and aborted the controller cycle before T-166 or T-169 could
reconcile. No provider call started.
Root cause: budget wakeup deleted portable controller identity instead of
reacquiring its lease, and fresh-admission errors were still a top-level
head-of-line blocker.
Smallest change: retain and rebind a budget claim directly. For state left by
the older deletion behavior, reconstruct only from one signed nonterminal
passport, its exact checked-out branch/cell, current ticket and route Kit-SHAs,
and a fresh ticket lease. Treat any new-admission error as an admission stop,
not a stop for existing authenticated claims.
Validation: all 22 focused controller tests pass, covering override/base
wakeup with fresh leases, exact missing-claim recovery, and continued
reconciliation when admission refuses. Protected GitHub CI retains the
complete regression.

## FI-20260728-066 — Attestation rejected a valid same-release schema migration

Status: Implemented; qualification pending
Area: route-journal attestation
Owner: Factory
First seen: 2026-07-28, Relay T-169 generation 24
Impact: T-169 completed Reviewer and Narrator successfully, but bundle
attestation blocked before publication with `ticket route migration provenance
does not match`. No sibling provider role was replayed.
Evidence: the immutable 13-revision T-169 journal embeds the exact legacy v1
plan and begins with the generator's valid schema migration from `fab7c04` to
the same `fab7c04`; all later release migrations form a valid hash chain to
`2951ae1`. The validator alone required revision zero to change Kit-SHA.
Root cause: `model-manager` correctly distinguishes schema migration from
release migration, while `ticket-attest` applied the release-change invariant
to both.
Smallest change: permit equality only for revision-zero `migration`; keep the
exact legacy bytes, digest, policy, selections, pin commit, Kit identities, and
revision hash checks. Later `release-migration` revisions still must change
Kit-SHA.
Validation: all 46 focused ticket-attestation tests pass with a same-Kit
revision-zero fixture, and the patched reducer validates the exact live T-169
journal and all 12 successful run manifests. Protected GitHub CI retains the
complete regression.

## FI-20260728-067 — Protected-base advance introduced a fixture conflict after preflight

Status: Observed; defer until after qualification
Area: deterministic preflight
Owner: Factory
First seen: 2026-07-28, Relay T-166 after T-167 merged
Impact: T-166 passed its original preflight and Review on a base without
T-167. After protected main advanced, both ticket suites owned port `4761`;
the refreshed full application test became deterministically untestable and a
Test-author call was spent discovering a planner-owned contract conflict.
Evidence: T-166's frozen contract and `event-detail.test.js` both require
`4761`; protected T-167's `job-detail.test.js` also binds `4761`; either suite
passes alone and the concurrent full application suite fails.
Smallest follow-up: after a semantic protected-base change, rerun the existing
deterministic dependency/fixture preflight before the first invalidated
provider role. A typed missing-decision result should wait without a provider
charge. Control-only allowlisted base changes must not trigger it.

## FI-20260728-068 — Same-release non-fast-forward role output lacks a portable repair boundary

Status: Implemented; qualification pending
Area: passport and push recovery
Owner: Factory
First seen: 2026-07-28, Relay T-168 generation 24
Impact: T-168 Test-author produced exact clean head `05b40fc` to repair
test-before-implementation ordering, but the non-force push correctly failed
against remote `7d91acc`. Passport export then rejected the non-ancestral head
before the existing exact-remote push recovery could become usable.
Evidence: terminal run `1785266450-63639` is receipt-bound
`role_exit_push_failed`, records both prior local and remote head `7d91acc`,
and left one clean local head `05b40fc`; no force push occurred.
Root cause: the exact protected rewrite authorization binds the unchanged
pre-route digest, but upgrade recovery waited until the route migration had
changed that digest before attempting passport authentication.
Smallest change: migrate the signed passport to the successor first while
keeping the claim blocked, persist a restart-safe pending marker, apply the
existing preview-approved route migration, then migrate the passport again
through ordinary descendant ancestry before reopening. Never authorize or
perform an automatic force push.
Validation: all 22 focused controller tests pass, including the two migration
boundaries, retained blocked state, restart marker, fresh lease, and exact
claim reopen. Protected GitHub CI retains the complete regression.

## FI-20260728-069 — Active budget overrides could not be raised immutably

Status: Implemented; qualification pending
Area: budget control
Owner: Factory
First seen: 2026-07-28, Relay T-168 qualification recovery
Impact: the operator authorized raising T-168 from $35 to $40 after $32 of
authenticated charges, but publishing a second ticket-cap record would make
every role and budget-stage reduction fail closed on an active override
conflict. Waiting for expiry would stall the qualification; deleting or editing
the $35 record would destroy authenticated evidence.
Evidence: active immutable record
`0a516916741c6f740daa45a17b121a650701bb863005b20eae96705bca1a1e47`
sets T-168's `PER_TICKET_BUDGET_USD=35.00`, and the Contract 1.8 envelope
surface exposes only creation and next-attempt consumption.
Root cause: immutable override creation had no authenticated replacement
lineage for a still-active persistent override.
Smallest change: when a persistent override preview finds exactly one active
record with the same scope, target, base-envelope identity, and setting-key
set, emit a v2 record that names that exact record in `supersedes`. The
replacement must be issued later and expire no earlier. Both records remain
immutable; reduction excludes the predecessor only while the authenticated
replacement is active. Missing targets, different keys, ambiguous
replacements, shortened lifetimes, collisions, malformed records, and ordinary
overlapping overrides remain fail-closed. One-use next-attempt records are
unchanged.
Validation: all six focused envelope tests pass, including byte preservation,
effective replacement, and refusal after removing the named predecessor. The
focused budget-stage and all 22 controller tests pass. Protected GitHub CI
retains the complete regression.

## FI-20260728-070 — Refusal migrated a passport outside controller recovery

Status: Implemented; qualification pending
Area: state-machine recovery
Owner: Factory
First seen: 2026-07-28, Relay T-169 generation 26
Impact: the first successor reconciliation resolved T-169 to the expected
old-route refusal, but the state machine migrated its passport to `f31a019`
before the controller blocked the claim. No provider call started, yet the
blocked claim had neither a prior-release passport nor the durable pre-route
pending marker, so ordinary post-route recovery could never reopen it.
Evidence: authenticated T-169 passport `916a0132…` names `f31a019` and parent
`55f8c839…`; its ticket and route journal still named `42614d9` at refusal.
The controller emitted `ticket_blocked` but no
`passport_migrated_awaiting_route` or `route_migration_required` event, and no
T-169 successor pending marker existed.
Root cause: `state-machine.py` migrated the passport unconditionally after
every resolved stage, including `REFUSE`, bypassing the controller's
restart-safe upgrade boundary.
Smallest change: issue and bind the exact refusal receipt without migrating the
passport. The controller then blocks the claim, and its next one-shot performs
the existing authenticated pre-route migration and marker protocol. Every
non-refusal transition retains the existing migration behavior.
Validation: the focused refusal test asserts that no passport migration occurs;
the focused state-machine and controller suites cover the refusal receipt and
two-phase recovery. Protected GitHub CI retains the complete regression.

## FI-20260728-071 — Qualification closeout ignored its authenticated recovery evidence

Status: Implemented; qualification pending
Area: qualification reducer and controller
Owner: Factory
First seen: 2026-07-28, Relay generation 27
Impact: T-168 and T-169 carried valid signed `$40` and `$35` ticket caps but
the reducer still rejected any passport over the base `$25`. It also required
four terminal tickets at the final Factory SHA even after the operator accepted
three, which would reopen the intentionally parked T-166 claim and discard
authenticated role, restart, relocation, and publication evidence from earlier
releases.
Evidence: current authenticated passports total T-166 `$26`, T-167 `$12`,
T-168 `$32`, and T-169 `$28`; T-168 effective Reviewer/Narrator contexts name
only replacement record `7380793e…` at `$40`. The reducer nevertheless compared
every ticket to literal `25_000_000` and every role to the final SHA. The one
restart/recovery boundary and cell relocation are authenticated under the
shared `fab7c04` passport history for T-166–T-169.
Root cause: the qualification reducer implemented the initial fixed canary
shape rather than the Contract 1.8 passport and envelope contracts, and the
controller treated every persisted claim as a target even after an explicit
three-ticket closeout.
Smallest change: allow an exact target of three or four at capacity four;
filter reconciliation to those target tickets without deleting excluded
claims; validate historical evidence only against each passport's authenticated
Factory release history; derive per-ticket caps from the sealed envelope
reducer; retain the fixed `$100` cohort ceiling; and require the original
four-ticket restart/recovery/relocation proof plus a final no-SHA-change freeze.
Validation: focused reducer coverage includes a three-ticket successor,
historical role/charge evidence, an authenticated `$30` cap, and refusal at
`$25`; focused controller coverage proves an excluded fourth claim remains
untouched. Protected GitHub CI retains the complete regression.

## FI-20260728-072 — Ticket PR discarded an authenticated semantic refresh

Status: Implemented; qualification pending
Area: publication and review evidence
Owner: Factory
First seen: 2026-07-28, Relay T-169 generation 28
Impact: T-169 retained an approved Reviewer result through an authenticated
control-only protected-base refresh and route migration, but the ticket-PR
boundary rejected publication because its branch head differed from the
Reviewer's execution head. No provider call started; the controller blocked
the ticket before PR mutation.
Evidence: latest successful Reviewer head `931a4cb9…` and exact ticket head
`1838a4d1…` differ only in the ticket/bundle/route metadata already accepted
by the boundary plus modified `factory/KIT_PIN`,
`factory/QUALIFICATION.json`, added authenticated in-flight release metadata,
and the committed `factory/attestations/T-169/refresh.json`. The existing
shared classifier returns preserve and every retained control blob equals the
receipt-bound protected base.
Root cause: `next-stage` and bundle attestation used the shared semantic
refresh contract, while `ticket-pr.py` independently allowed only ticket,
bundle, attestation, and route paths. It therefore invalidated review from the
base commit SHA rather than the already-authenticated semantic input.
Smallest change: at the ticket-PR boundary, reuse the existing shared
classifier only for Contract 1.8 receipt-authorized stages and require the
current refresh receipt to be regular, exact-schema, committed directly after
its bound two-parent merge, descended from the reviewed head, and ancestral to
the current head. Permit only refresh control blobs that still equal that
receipt's protected base. Unknown, application, test, CI, contract,
configuration, rename, deletion, type-change, and malformed-refresh inputs
remain fail-closed; route journals retain independent append-only validation.
Validation: all focused ticket-PR tests pass, including one control-only
refresh acceptance followed by an unknown-path refusal. The exact live T-169
range classifies the four expected refresh/control paths and zero unexpected
paths. Protected GitHub CI retains the complete regression.

## FI-20260728-073 — Control-only refresh counted Reviewer evidence from a discarded lineage

Status: Implemented; qualification pending
Area: state machine and review evidence
Owner: Factory
First seen: 2026-07-28, Relay T-168 generation 29
Impact: after the authenticated `ce7ef86` route migration, T-168's passport
correctly required Reviewer, but deterministic reconciliation selected
Narrator and failed at the ticket-PR gate once per one-shot cycle. No provider
call started and no passport changed.
Evidence: current exact branch head `729ec5c…` does not descend the latest
successful Reviewer head `e9165f1…`; the latter belongs to a discarded
force-pushed lineage. The receipt-bound protected-base delta is control-only,
so the sequencer counted its baseline Reviewer/Narrator totals without first
binding their manifest heads to the receipt's `old_head`. The Reviewer-stage
ticket-PR check succeeds on the same exact head while its Narrator-stage check
fails closed.
Root cause: semantic base classification answered whether protected main
changed review inputs, but preservation did not separately prove that the
evidence being preserved belonged to the surviving ticket lineage.
Smallest change: retain the existing allowlist, then bind the latest effective
Reviewer and its later effective Narrator to the exact old head. An orphaned
Reviewer invalidates Reviewer and downstream Narrator; an orphaned or
pre-Reviewer Narrator invalidates only Narrator. Earlier superseded rows remain
auditable and do not invalidate a later valid pair. Apply the same ancestry
rule at bundle attestation; never delete or rewrite historical runs or charges.
Validation: focused attestation coverage creates linked Reviewer/Narrator
commits outside the live ticket ancestry and requires a new Reviewer after a
control-only refresh. The sequencer regression covers the same discarded-head
topology, and the exact live T-168 resolver must return `RUN reviewer`.
Protected GitHub CI retains the complete regression.

## FI-20260728-074 — Stale publication readiness blocked an independent green PR

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-28, Relay generation 30
Impact: T-169 was approved, exact-head green, and clean against protected main,
but could not acquire the free publication lease because T-168 retained an
older lease-free queue position after it stopped being merge-ready.
Evidence: the controller was drained with no active publication lease.
T-168's queue record bound obsolete head `a7b5d514…` at ready time
`1785242337`; T-169's record bound current head `b7555549…` at
`1785279935`. Deterministic acquisition therefore selected T-168 first even
though its current transition awaited operator approval and held no lease.
Root cause: queue creation and capability-bound lease release were implemented,
but deterministic transitions away from publication readiness did not remove
the lease-free queue record.
Smallest change: add an authenticated `publication withdraw` action that
removes only the named ticket's queue record and refuses any active lease for
that ticket. The controller invokes it whenever the sole state machine no
longer classifies the ticket as merge-ready; active leases continue through
their existing capability-bound release.
Validation: focused publication coverage proves withdrawing stale T-168 lets
independent T-169 acquire the lease, and focused controller coverage proves a
non-publication transition withdraws its record. Protected GitHub CI retains
the complete regression.

## FI-20260728-075 — One publication transport failure stranded a valid passport

Status: Implemented; qualification pending
Area: publication
Owner: Factory
First seen: 2026-07-28, Relay generation 31
Impact: T-169 reached the authenticated approval stage at exact head
`9c0c9817…` with both required checks green and unchanged `$28` accounting,
but one SSH connection closure during ticket-PR verification made the
controller fail closed and park its otherwise valid claim. T-168 continued
independently.
Evidence: the terminal controller result records a `ticket-pr/v1` error from
the exact branch-head read, followed by a blocked T-169 claim. No provider run,
role replay, branch mutation, publication lease, or new charge occurred.
Root cause: the role runner and protected attestor retried one failed exact
`ls-remote`, while the ticket-PR helper's shared Git boundary made only one
attempt.
Smallest change: retry only the exact read-only ticket-PR `ls-remote` once.
Every other Git failure remains single-attempt, and a second transport failure
still refuses publication and leaves the passport intact.
Validation: all 11 focused ticket-PR tests pass; the added regression requires
one failed read followed by an exact success and separately requires two
failures to refuse. Protected GitHub CI retains the complete regression.

## FI-20260728-076 — An excluded ticket retained publication head-of-line priority

Status: Implemented; qualification pending
Area: publication and qualification
Owner: Factory
First seen: 2026-07-28, Relay generation 31
Impact: after FI-074 correctly removed stale queue state for selected tickets,
T-168 and T-169 still could not acquire the free merge lease. Excluded T-166
retained the oldest queue position even though its claim was deliberately
parked outside the three-ticket target.
Evidence: publication had no active lease, but its queue contained blocked
T-166 at head `7687c2aa…` and ready time `1785242337`, ahead of green T-168
and T-169. T-166's authenticated passport already declared publication state
`none`; qualification filtering prevented the controller from reconciling the
claim and invoking FI-074 withdrawal.
Root cause: the controller filtered excluded claims before reconciling their
publication control state.
Smallest change: during qualification startup, withdraw publication state for
claims outside the selected cohort, then retain the existing filter. Their
claim files, passports, roles, worktrees, charges, and ticket states remain
parked.
Validation: all 24 focused controller tests pass; the three-ticket regression
requires the excluded fourth claim to remain present while its publication
state is withdrawn. Protected GitHub CI retains the complete regression.

## FI-20260728-077 — Same-release Test-author repair forced Factory candidate churn

Status: Implemented; qualification pending
Area: passport and repair recovery
Owner: Factory
First seen: 2026-07-28, Relay T-169 generation 32
Impact: after T-168 merged an application test using port `4771`, T-169's
Reviewer correctly invalidated only Test-author. The bounded repair changed its
test port to `4779` and kept test-before-implementation ordering by rewriting
ticket history. Its trusted non-force push failed as designed, but passport
export rejected the non-ancestral clean head. The only existing escape required
another Factory release even though no Factory semantics had changed.
Evidence: consumed receipt `f48e76b5…` binds `FIX test-author` at old head
`9ac81f2`; terminal run `1785283996-18796` is a submitted, conservatively
charged `role_exit_push_failed`; clean head `c155e13` differs from the old tree
only at `app/tests/outbox-detail.test.js` and `factory/tickets/T-169.md`.
Root cause: non-ancestral passport migration recognized only cross-release
in-flight authorization, coupling a ticket-branch repair to Factory release
identity.
Smallest change: accept a same-release rewrite only through one exact protected
record binding repository, Factory SHA, ticket/branch/state, signed prior
passport, old/new heads, unchanged route, consumed Test-author receipt, and the
typed terminal push failure. Require a clean cell and an added/modified
regular-blob delta limited to configured test paths plus the exact ticket log.
Fold the failed attempt's charge—not successful-role evidence—into the passport.
The controller does not force-push; after the exact authorized head becomes the
remote tip it reopens only the invalidated Test-author stage.
Validation: all three focused passport tests and all 25 focused controller
tests pass, including unknown semantic-path refusal, exact charge retention,
pre-validation passport migration, exact remote-head matching, and no automatic
force push. Protected GitHub CI retains the complete regression.

## FI-20260728-078 — Contract 1.8 batch pin test used a Contract 1.2 release

Status: Implemented; protected CI pending
Area: release verification
Owner: Factory
First seen: 2026-07-28, protected-main Factory 1.8 promotion
Impact: both Linux and macOS Hermes shards refused the nominally valid
`models pin-batch` fixture, preventing the merged release from being sealed.
Evidence: protected Factory run `30417416161`; jobs `90466865328` and
`90466865310` report
`factory-launch: batch model pinning requires contract 1.8.0`.
Root cause: the Hermes contract test correctly retained a Contract 1.2 release
for compatibility coverage but reused it after the valid pin invocation was
upgraded to Contract 1.8-only `pin-batch`.
Smallest change: retain the Contract 1.2 model-control coverage, add one
Contract 1.8 model fixture, and switch the active release only at the batch-pin
boundary.
Validation: shell syntax and exact fixture/order assertions pass locally.
Protected Factory run `30417748648` passed this batch-contract boundary on
both operating systems before exposing FI-20260728-079.

## FI-20260728-079 — Batch pin overwrote the launcher helper snapshot

Status: Implemented; protected CI pending
Area: release verification
Owner: Factory
First seen: 2026-07-28, protected-main Factory 1.8 promotion
Impact: both Linux and macOS Hermes shards rejected a valid Contract 1.8 batch
pin after it succeeded, preventing the merged release from being sealed.
Evidence: protected Factory run `30417748648`; jobs `90467839766` and
`90467839758` report
`FAIL: caller control propagated to helper: FACTORY_PROBE_CODEX`.
Root cause: the test helper recorded the clean launcher boundary, then the
batch implementation's internal `pin` subprocess overwrote that file after
loading trusted machine configuration. The assertion therefore inspected an
internal call instead of the launcher boundary it was designed to verify.
Smallest change: retain the existing environment assertions and skip only the
internal batch subprocess when writing the launcher-boundary snapshot.
Validation: shell syntax and focused helper-boundary assertions pass locally.
Protected GitHub CI owns execution of the complete Hermes regression.

## FI-20260728-080 — Contract assertion omitted the batch-pin grammar

Status: Implemented; protected CI pending
Area: release verification
Owner: Factory
First seen: 2026-07-28, protected-main Factory 1.8 promotion
Impact: the Linux Hermes shard reached and rejected the static public-contract
assertion after all preceding runtime boundaries passed, preventing release
sealing.
Evidence: protected Factory run `30418059944`, job `90468771301`, reports an
`AssertionError` at the exact `commands["models"]["grammars"]` comparison.
Root cause: the public Contract 1.8 manifest and launcher both included
`models pin-batch`, but the exact expected grammar list still reflected the
pre-batch interface.
Smallest change: add the existing public batch-pin grammar to the expected
list; runtime behavior and assertions are otherwise unchanged.
Validation: shell syntax and the isolated static contract assertion block pass
locally. Protected GitHub CI owns execution of the complete Hermes regression.

## FI-20260728-081 — Ticket-state mutated a sealed release with Python bytecode

Status: Implemented; protected CI pending
Area: release integrity
Owner: Factory
First seen: 2026-07-28, protected-main Factory 1.8 promotion
Impact: a valid state-machine transition changed the physical sealed-release
tree, so the next deterministic stage resolution failed closed.
Evidence: protected Factory run `30418343411`; jobs `90469803443` and
`90469803478` report `transition changed the resolved stage`, followed by
`physical release tree does not match trusted release provenance`.
Root cause: `ticket-state.sh` imported a release-local Python module without
disabling bytecode writes. Its `__pycache__` changed the immutable tree.
Smallest change: disable Python bytecode writes at the ticket-state boundary,
matching the existing next-stage boundary.
Validation: ticket-state's focused transition/push suite passes locally.
Protected GitHub CI owns the sealed-release regression.

## FI-20260728-082 — Contract 1.8 tests used incomplete authenticated fixtures

Status: Implemented; protected CI pending
Area: release verification
Owner: Factory
First seen: 2026-07-28, protected-main Factory 1.8 promotion
Impact: the budget reducer correctly refused successful role manifests without
an output digest, while passport tests could not advance their cloned
protected-main fixture because the bare remote still advertised `master`.
Evidence: protected Factory run `30418343411`; Factory jobs `90469803443` and
`90469803478` report `ticket budget could not be reduced`, and release jobs
`90469803429` and `90469803463` reject the two protected authorization pushes.
Root cause: the shared successful-run fixture predated authenticated output
evidence, and the passport remote fixture created `main` without moving its
symbolic HEAD.
Smallest change: emit the successful output plus its SHA-256 in the shared run
fixture, and point the bare fixture's HEAD at `main`.
Validation: the focused budget reducer and all three authenticated passport
tests pass locally. Protected GitHub CI owns the complete regression.

## FI-20260728-083 — Mac-only release tests leaked host assumptions

Status: Implemented; protected CI pending
Area: protected CI portability
Owner: Factory
First seen: 2026-07-28, protected-main Factory 1.8 promotion
Impact: Linux release verification invoked macOS `libproc`, assumed the
macOS-only `/private/tmp` qualification trust root, and used BSD `stat` for a
portable pause receipt. The macOS development-lane test also evaluated
`product_resume_stage` without its `product_resolve_stage` dependency.
Evidence: protected Factory run `30418343411`; Linux release job
`90469803429` and macOS release job `90469803463`.
Root cause: platform-specific probes lacked exact platform guards, the pause
receipt used a host-specific metadata command, and the focused shell extractor
omitted one direct dependency.
Smallest change: skip only the macOS-only probe and qualification-root tests on
other hosts, validate the pause receipt with one secure Python read, and load
the missing resolver in the focused lane test.
Validation: focused cancellation, qualification-environment, ticket-state,
passport, shell syntax, and the clean-tree development-lane check pass locally.
Protected GitHub CI owns the complete regression.

## FI-20260728-084 — Checkpoint repair fixture omitted Narrator output

Status: Implemented; protected CI pending
Area: release verification
Owner: Factory
First seen: 2026-07-28, focused promotion repair
Impact: the development-lane check reached the recovered Narrator boundary
and escalated even after recording a successful Narrator run.
Evidence: focused `ci/factory-dev-lane-test.sh` reported
`publication repair did not return to operator-await`.
Root cause: the checkpoint fixture recorded a successful Narrator ledger row
without creating the required evidence bundle. The state machine correctly
treated the missing output as invalid.
Smallest change: add a valid bundle to the successful Narrator fixture; keep
the fail-closed resolver and its retry limit unchanged.
Validation: the focused development-lane check passes from the amended clean
commit; protected GitHub CI owns the complete regression.

## FI-20260728-085 — Development roles bypassed concurrent admission

Status: Superseded by FI-20260728-086
Area: provider coordination
Owner: Factory
First seen: 2026-07-28, focused promotion repair
Impact: four explicit provider probes overlapped, but the following 24
synthetic lifecycle roles ran through legacy serialization and never appeared
in the provider coordinator.
Evidence: the focused development-lane check expected 28 terminal attempts but
the coordinator contained only the four explicit probes, all terminal with
zero active reservation.
Root cause: Contract 1.8 moved the provider-contract snapshot before mutable
kit validation. Development lanes derive their contract during that validation,
so the early snapshot remained empty.
Smallest change attempted: initialize the snapshot empty and bind it once after
kit validation.
Validation: the four-ticket assertion passed locally, but protected run
`30420132613` proved that this over-bound unrelated mutable fixtures to
Contract 1.8. FI-20260728-086 replaces that change.

## FI-20260728-086 — Post-validation contract binding overreached

Status: Implemented; protected CI pending
Area: role execution
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: 80 factory-script cases refused before their intended boundary because
mutable direct fixtures were treated as Contract 1.8 executions without
transition receipts.
Evidence: protected run `30420132613`; Linux job `90475085324` and macOS job
`90475085355` repeatedly report
`consumed transition receipt is unavailable; no task was submitted`.
Root cause: FI-20260728-085 moved the provider-contract snapshot after mutable
kit validation, where the current kit manifest supplied Contract 1.8 even when
the caller had not selected a Contract 1.8 execution path.
Smallest change: restore the intentional pre-validation snapshot and add only
the trusted development contract as its final fallback. Sealed production
still receives Contract 1.8 from release provenance and therefore still
requires transition receipts; explicit Contract 1.7 development lanes retain
concurrent provider admission.
Validation: focused shell syntax and the unchanged four-ticket development
lane assertion pass with 28 terminal attempts and zero active reserve;
protected GitHub CI owns the complete regression.

## FI-20260728-087 — Retry fixture used BSD-only in-place sed syntax

Status: Implemented; protected CI pending
Area: protected CI portability
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: Linux release verification stopped before running the development-lane
suite.
Evidence: protected run `30420132613`, job `90475085316`, reports
`sed: can't read s/output_sha256=same/output_sha256=changed/`.
Root cause: the retry fixture used `sed -i ''`, whose empty backup argument is
BSD-specific.
Smallest change: use the repository's existing portable `sed -i.bak` pattern
and remove the temporary backup.
Validation: focused shell syntax and the development-lane suite pass;
protected GitHub CI owns the complete regression.

## FI-20260729-088 — Development-lane trust checks used BSD-only stat

Status: Implemented; protected CI pending
Area: protected CI portability
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: Linux release verification could not validate the product source and
stopped the development-lane suite before exercising its state transitions.
Evidence: protected run `30421453064`, Linux release job `90479005243`,
reports `stat: cannot read file system information for '%Su:%Lp:%l'`.
Root cause: development-lane file, directory, and test-fixture trust checks
used BSD `stat` formats throughout a cross-platform release suite.
Smallest change: route all of those metadata reads through one Python `lstat`
helper using numeric owner, octal mode, and link count; keep every existing
fail-closed comparison.
Validation: focused development-lane and shell syntax checks run locally;
protected GitHub CI owns the complete cross-platform regression.

## FI-20260729-089 — Publication ordering test relied on a one-second clock tie

Status: Implemented; protected CI pending
Area: release verification
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: macOS release verification expected ticket-ID ordering, but the two
normal-priority records occasionally crossed a wall-clock second and correctly
ordered by their different `publication_ready_at` values.
Evidence: protected run `30421453064`, macOS release job `90479005246`,
reports T-111 remained `queued` after T-112 released.
Root cause: the fixture assumed two subprocesses would receive the same
second-resolution ready timestamp instead of creating that tie explicitly.
Smallest change: make the two queue records share an exact fixture timestamp;
retain all lease-ordering assertions and production behavior unchanged.
Validation: the focused publication-lease suite runs locally; protected GitHub
CI owns the complete regression.

## FI-20260729-090 — Static lane assertions short-circuited GNU sed

Status: Implemented; protected CI pending
Area: protected CI portability
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: Linux release verification stopped in the development-lane suite even
though the required budget-day binding was present and macOS Release passed.
Evidence: protected run `30422592572`, Linux release job `90482284118`,
reports `sed: couldn't flush stdout: Broken pipe` immediately before the false
missing-binding assertion.
Root cause: under `pipefail`, `grep -q` exited after its first match and GNU
`sed` treated the closed pipe as an error.
Smallest change: keep the exact static assertions but let `grep` consume the
complete extracted function while discarding its output.
Validation: focused static extraction and shell syntax checks run locally;
protected GitHub CI owns the complete regression.

## FI-20260729-091 — Operator-resume fixture retained BSD in-place syntax

Status: Implemented; protected CI pending
Area: protected CI portability
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: Linux release verification reached operator-resolved Planner recovery
but could not materialize the resumed state; macOS Release passed.
Evidence: protected run `30423208926`, Linux release job `90484058519`,
reports `sed: can't read s/^State: Blocked-Escalated$/State: Planning/`.
Root cause: a later state-materialization fixture retained the BSD-only
`sed -i ''` form after FI-087 repaired the earlier retry fixture.
Smallest change: use the existing portable `sed -i.bak` pattern and remove its
single backup; no state-machine behavior or assertion changes.
Validation: focused operator-resume and shell syntax checks run locally;
protected GitHub CI owns the complete regression.

## FI-20260729-092 — Linux mock lanes lacked a process-identity probe

Status: Implemented; protected CI pending
Area: protected CI portability
Owner: Factory
First seen: 2026-07-29, protected-main Factory 1.8 promotion
Impact: the Linux release shard reached the mock lifecycle but refused Planner
before submission, leaving no role manifest.
Evidence: protected run `30423761930`, Linux release job `90485699126`; the
retained Linux reproduction contained an empty Planner claim and no run
evidence.
Root cause: the lane's restricted `ps` helper loaded macOS `libproc` on every
platform, so Linux could not record the launcher's process-start identity.
Smallest change: preserve the narrow helper interface and macOS implementation,
and resolve Linux process groups plus opaque raw start ticks from `/proc`;
never reconstruct a lock identity from an adjustable wall clock. Run the
existing identity regression on Linux as well as macOS.
Validation: shell syntax and the focused process-identity regression pass on
Linux and macOS; a fresh focused Linux mock lane completed all six roles and
reached deterministic `AWAIT-OPERATOR`. Protected GitHub CI owns the complete
regression.

## FI-20260729-093 — Activation rejected terminal canceled tickets

Status: Implemented; protected CI pending
Area: release activation
Owner: Factory
First seen: 2026-07-29, Factory 1.8 sealing
Impact: the qualified candidate could not activate against Nysa protected
`main`, which contains three valid lease-free `Canceled` tickets.
Evidence: the activation lease validator accepted only lease-free `Ready`,
`Backlog`, and `Blocked-Escalated`, while the canonical workflow defines
`Canceled` as the terminal withdrawn-ticket state.
Root cause: the activation allowlist omitted `Canceled` even though the shared
ticket parser and Linear workflow recognize it.
Smallest change: accept only lease-free `Canceled` tickets during activation;
a canceled ticket that still carries a lease remains fail-closed through the
existing lease path. Add the state to the existing activation fixture.
Validation: shell syntax and the focused validator boundary run locally;
protected GitHub CI owns the complete regression.

## FI-20260729-094 — Factory upgrades replayed previously certified legacy Done

Status: Implemented; protected CI pending
Area: release activation
Owner: Factory
First seen: 2026-07-29, Relay Factory 1.8 sealing
Impact: Relay activation stopped at T-101 even though T-101 through T-105 and
T-107 were already unchanged `Done` records in generation 7's certified
product tree.
Evidence: generation 7 records product tree `bcadcf0…`; its ancestor commit
`83162d6…` contains the same six terminal ticket blobs, while activation
required newer attestation formats that did not exist when those tickets
terminalized.
Root cause: cross-release activation revalidated every historical Done claim
as newly terminal instead of carrying forward the prior certified product-tree
boundary.
Smallest change: accept an otherwise-unattested Done claim only when its exact
ticket blob is unchanged from an ancestor commit with the previous active
generation's certified product tree. New or changed Done claims, non-ancestor
trees, deleted modern evidence, stale branches, and non-protected HEADs remain
fail-closed.
Validation: the focused legacy-terminal regression and the exact Relay lease
validator boundary run locally; protected GitHub CI owns the complete
regression.

## FI-20260729-095 — Linear soft wrapping caused endless description rewrites

Status: Implemented; protected CI pending
Area: Linear reconciliation
Owner: Factory
First seen: 2026-07-29, Nysa Factory 1.8 production admission
Impact: completed tickets were rewritten every three-minute cycle, shared API
quota was exhausted, and four Ready tickets remained fail-closed before any
provider admission.
Evidence: authenticated cycles at 10:10, 10:14, 10:18, 10:22, and 10:26 UTC
repeatedly patched T-021 and T-025. Exact remote comparison showed Linear
inserted `\n   ` inside one ordered-list sentence in each issue while every
other normalized byte matched Git.
Root cause: Markdown comparison normalized bullet glyphs and link wrappers but
not Linear's semantically inert three-space list-paragraph soft wrap.
Smallest change: collapse only that continuation boundary during comparison;
do not collapse nested list markers, mutate projected text, or change canonical
ticket contracts and sync health.
Validation: the focused Linear sync test simulates the remote wrap and proves a
second reconciliation issues no update while nested list structure remains
distinct; protected GitHub CI owns the complete regression.

## FI-20260729-096 — Contract blockers had no same-release controller resume

Status: Implemented; protected CI pending
Area: deterministic lifecycle
Owner: Factory
First seen: 2026-07-29, Nysa T-093/T-094/T-096/T-100
Impact: four authenticated Planner contract blockers preserved their $10
charges but stayed excluded forever after the operator supplied the missing
product decisions; only a Factory upgrade could reopen them.
Evidence: each claim remained `blocked` with a unique
`role_exit_contract_blocked` terminal manifest and signed passport, while the
controller recovered only cross-release claims, push failures, or
pre-submission interruptions.
Root cause: Contract 1.8 exported the terminal passport and released the lease
without asking the state machine to enter `Blocked-Escalated`; it also had no
receipt-bound path to consume the exact Linear resume overlay.
Smallest change: reuse `ticket-state` behind two controller-only state-machine
actions. `block` requires the consumed receipt and unique exact terminal
manifest; `resume` accepts only the recorded prior role state, migrates the
passport, and lets the controller reclaim one ticket lease. Successful roles,
sibling claims, and existing charges remain untouched.
Validation: focused state-machine and controller tests cover malformed
terminal refusal, exact block, operator wait, exact resume, and single-ticket
reclaim; shell syntax and diff checks pass. Protected GitHub CI owns the
complete regression.

## FI-20260729-097 — Atomic runtime-ledger staging crossed the checkout guard

Status: Implemented; protected CI pending
Area: concurrent accounting
Owner: Factory
First seen: 2026-07-29, protected run `30461033308`, Linux release job
`90606879576`
Impact: four concurrent mock lifecycles reached Builder, then a role failed
closed because two `.runtime-ledger.csv.*` files briefly appeared as untracked
registered-checkout mutations.
Evidence: the retained job logged the two exact paths and
`role_exit_control_plane_mutation`; the retained Linux reproduction observed
the same untracked path while pausing the atomic writer immediately before
rename.
Root cause: the derived runtime ledger was ignored, but `ledger-view.py`
created its same-directory atomic temporary beside it, outside every ignored
runtime path.
Smallest change: stage the temporary inside the existing ignored real
`factory/runs/` directory and atomically rename it to the sibling runtime
ledger. Keep checkout mutation detection, assertions, timeouts, and ignore
rules unchanged.
Validation: the focused ledger test observes clean Git status at the exact
pre-rename boundary on macOS and Linux. A focused Linux mock-concurrency lane
advanced past the original checkout-mutation boundary, then separately refused
one manifest whose link count was unsafe before its disposable Colima VM wedged
under memory pressure. No speculative second repair was made; protected GitHub
CI proved the release shard green and did not reproduce that later signal.
Protected Factory shards then exposed the first exact follow-up boundary:
valid external `FACTORY_LEDGER` targets have no sibling `runs/` directory.
The writer now receives the already-authoritative runs root rather than deriving
it from the output target.

## FI-20260729-098 — Activation confused product slice branches with ticket state

Status: Implemented; protected CI pending
Area: release activation
Owner: Factory
First seen: 2026-07-29, Nysa Factory 1.8 activation
Impact: a fully green, certified generation 24 plan passed, but activation
stopped before receipt claim while the independent T-092 follow-up PR #261
remained open.
Evidence: the product remote contained canonical Factory branches
`ticket/T-093`, `ticket/T-094`, `ticket/T-096`, and `ticket/T-100`, plus the
repository-valid slice branch `ticket/T-092-board-receipts`. Activation's
broad `refs/heads/ticket/T-*` query rejected the latter as
`remote ticket ref is malformed`; the preceding plan did not run ticket-lease
validation and therefore reported `PLAN OK`.
Root cause: remote discovery treated every branch under the broad Git pattern
as canonical Factory state even though deterministic ticket identity uses only
the exact `ticket/T-NNN` ref. Planning and activation also validated different
inputs.
Smallest change: ignore noncanonical slice refs while retaining fail-closed
validation for every exact `ticket/T-NNN` ref, and make plan run the same
ticket/passport/release validation used by activation. Do not relax ticket
identity, migration authorization, protected-main, or exact-head checks.
Validation: shell syntax and diff checks pass; the exact maintained Nysa plan
now validates the live canonical branches while PR #261 stays open. Focused
regressions cover a noncanonical slice ref and a wrong authorized exact head;
protected GitHub CI owns the complete regression.

## FI-20260729-099 — Activation plan swallowed a lease refusal

Status: Implemented; protected CI pending
Area: release activation
Owner: Factory
First seen: 2026-07-29, protected run `30472453618`, Linux release job
`90645861195` and macOS release job `90645861106`
Impact: activation correctly refused a wrong authorized ticket head, but the
read-only plan printed `PLAN OK` after the same validator rejected it.
Evidence: both protected release jobs failed the exact
`plan and activation reject the same wrong authorized remote head` assertion;
the validator emitted
`nonterminal ticket does not match its exact in-flight release authorization`
before planning continued.
Root cause: `plan_activation` runs inside a command substitution, where
Bash 3 clears `errexit`. Its ticket-lease validator returned nonzero, but the
following plan-tuple print replaced that status.
Smallest change: explicitly return the validator status before emitting the
plan tuple. Keep every validation rule, refusal, assertion, and timeout
unchanged.
Validation: shell syntax, the exact status-propagation boundary, and the
maintained Nysa plan run locally; protected GitHub CI owns the complete
regression.

## FI-20260729-100 — Release migration cleared the contract-repair checkpoint

Status: Implemented; protected CI pending
Area: deterministic recovery
Owner: Factory
First seen: 2026-07-29, Nysa T-094 after Factory activation to `8ee1d58`
Impact: no provider call started, but the successor controller cleared the
blocked Builder receipt and role while migrating the signed passport. The
state machine then saw the exact `OPERATOR RESUME: test-author` directive
without its HMAC-bound repair record and refused every scheduled reconciliation.
Sibling claims remained preserved, but T-094 could not reach Test-author.
Evidence: controller event `upgraded_claim_recovered` was followed by
`operator resume lacks authenticated contract repair state`; the current
passport retained the $10 Builder charge, old Factory SHA, old head, exact
manifest digest, and current release migration, while the controller claim
had empty receipt/role fields. A later cleanup also attempted to release an
already absent exact lease and emitted a raw `FileNotFoundError`.
Root cause: upgrade recovery treated every blocked checkpoint like an
interrupted non-contract failure and erased its role/receipt before the
successor state machine could authenticate it. The contract-block validator
also required the current Factory SHA and original lease even when the signed
passport proved an authorized release migration.
Smallest change: preserve recognized contract-block fields during upgrades;
recover fields cleared by an earlier controller only from the latest
passport-bound charge, transition receipt, and exact terminal manifest; and
allow the state machine to validate that historical receipt only through the
current signed release lineage and a live exact-ticket lease. Exact release
cleanup is idempotent when the lease is already absent, while wrong, unsafe, or
extant ownership still fails closed.
Validation: focused state-machine tests cover the historical release/charge
binding plus absent and mismatched current-lease rejection, controller tests
cover reconstruction of the exact cleared checkpoint, and dispatch-lease tests
cover absent-release idempotence without weakening ownership. An offline copy
of T-094's actual signed passport, consumed receipt, five run manifests, branch
history, and a fresh isolated exact lease resolves only to Builder. Protected
GitHub CI owns the complete regression.

## FI-20260729-101 — Production activation silently retained provider serialization

Status: Implemented; protected CI pending
Area: release activation and provider admission
Owner: Factory
First seen: 2026-07-29, Nysa Factory 1.8 activation and T-093/T-100
Impact: three ticket cells and controller workers were available, but T-100
waited while T-093 owned the sole legacy Cursor interval. The activated
multi-ticket release therefore demonstrated ticket concurrency without
provider-runtime concurrency.
Evidence: the sealed launcher selected production paths
`~/.factory/provider-policy.json` and `~/.factory/isolated-v1.enabled`, while
the release workflow created neither and certification did not require them.
Only qualification launchers exported `FACTORY_CLI_LANE_ROOT`; a production
activation with manually supplied JSON would still fail before submission
because `prepare_cli_runtime` required a development/qualification lane.
Codex was excluded from that preparation and retained shared `HOME`.
Root cause: qualification-owned provider preparation was never promoted into
the installed release contract. The runtime still treated subscription
isolation as lane state instead of owner-local attempt state, and absent
activation silently selected the legacy lock even when Contract 1.8 declared
multi-ticket capacity.
Smallest change: add one approval-hash-bound owner-local provider configurator
for the sealed route catalog; anchor every production attempt to a
ticket/cell-neutral runtime root; isolate Codex authentication and writable
state alongside the existing Claude and Cursor copies; and make doctor,
certification, activation, and Contract 1.8 role pre-admission refuse missing,
incomplete, drifted, or under-capacity configuration. Keep the legacy lock for
older contracts, capacity one, and non-activated legacy routes.
Validation: focused tests prove canonical owner-local configuration covers
Cursor, Claude Code, and Codex, three distinct mock CLI routes reach submitted
state during the same measured interval, then terminalize and remove their
mode-0700 runtimes independently. Three same-Cursor-account reservations also
admit, each adapter receives a distinct runtime with mode-0600 authentication
copies, source authentication remains unchanged, and doctor fails until the
exact multi-ticket configuration is ready. Certification records and
activation revalidates the exact policy digest, ticket capacity, route and
adapter coverage, Factory SHA/tree, and runtime-root identity. A real
three-route provider overlap is still required after protected CI,
installation, explicit owner apply, and activation; no readiness probe or live
provider call was run during this repair.

## FI-20260729-102 — Downstream dependency checked after paid admission

Status: Implemented; protected CI and live proof pending
Priority: P0
Area: deterministic readiness and provider admission
Owner: Factory
First seen: T-100 Test-author admission while its `Depends-On: T-094`
prerequisite was not merged
Impact: a paid Test-author call ran for about 86 seconds before terminalizing
as contract-blocked. This consumed provider budget and a concurrency slot for
work that could not yet proceed.
Evidence: controller preflight currently runs only before Planner. Contract
1.8 ticket readiness validates decisions, protected-test conflicts, and
fixture/authentication feasibility, but does not reduce `Depends-On` against
authoritative protected-main ticket state. Dependency checks therefore occur
after downstream provider admission.
Root cause: dependency scheduling and ticket readiness do not share one
provider-free admission boundary for every paid role.
Smallest change: before reserving provider capacity or invoking any paid role,
reduce declared dependencies against authoritative protected main. An
unresolved dependency must transition deterministically to
`AWAIT_DEPENDENCY`, with no provider reservation, call, replay, or charge.
Re-evaluate only after a terminal dependency event or protected-main advance.
Validation required: prove Planner, Test-author, Builder, and Reviewer cannot
be admitted while a prerequisite is unresolved; prove sibling tickets
continue; prove the ticket resumes at its exact stage after the prerequisite
merges.
Decision: the activation/concurrency repair exposed this as a direct paid-work
admission defect, so the same candidate now checks protected dependency truth
before every role. A resolved dependency whose protected merge is absent from
the ticket branch takes a receipt-bound, no-provider `dependency-refresh`
transition. That action requires no PR, preserves Planning/Building state,
merges and pushes the exact protected SHA, records terminal-truth digests, and
migrates the authenticated passport before the exact next role. Main movement
between observation and fetch remains a nonterminal wait.

## FI-20260729-103 — Live provider stdout spool is bounded only at publication

Status: Backlog
Priority: P1
Area: provider output containment
Owner: Factory
First seen: 2026-07-29 concurrency activation audit
Impact: final `.out` artifacts and every evidence consumer now share an exact
8 MiB, mode-0600 streaming contract, but the anonymous unlinked spool may grow
until the provider exits. A noisy provider can therefore create avoidable
temporary disk pressure before bounded publication rejects the output.
Evidence: `run-agent.sh` retains stdout on an unlinked descriptor during the
provider interval, then streams it through `role_output.py`. The final
publisher, passport reducer, and state-machine refuse oversized, linked,
non-regular, non-owner, or non-0600 artifacts without loading them in memory.
Root cause: the original spool optimized against symlink/path substitution but
did not enforce the artifact-size contract during the live write.
Smallest change: introduce a bounded streaming tee (or an equivalent
descriptor-level byte counter) that preserves real-time redaction and provider
drain semantics, stops retaining bytes after 8 MiB, and terminalizes with the
same typed `role_exit_invalid_output` accounting result. Do not truncate into
apparently successful evidence and do not kill a provider solely because its
diagnostic output crossed the proof limit.
Validation required: exact-boundary acceptance, one-byte-over refusal during a
live long-running process, bounded temporary-file size, unchanged
cancellation/process-group drainage, conservative accounting, and no plaintext
credential regression.

## FI-20260729-104 — Strict dependency admission stranded legacy merged work

Status: Implemented; protected CI and Nysa activation proof pending
Priority: P0
Area: deterministic readiness and release migration
Owner: Factory
First seen: 2026-07-29 pre-promotion audit of Nysa T-093/T-094/T-100
Impact: the repaired pre-provider gate correctly rejected T-094 on T-092 and
would later reject T-093 on T-040 forever. Both prerequisites have application
work and successful protected checks on main, but their old Backlog ticket
records predate authenticated terminal receipts. Replaying either ticket would
waste roles and charges; accepting any matching commit or `factory/**` change
would weaken the gate.
Evidence: Nysa PR #181 merged T-040 at
`a0e0a07a693667be614afe7560eefa2857d3f3a3`; PR #267 merged T-092 at
`8984cd7bb5af776cfe0133d08b37e92f979a9fe5`. Protected main still records both
tickets as Backlog and contains no normal, backfill, legacy-closeout, or
protected-merge-reconciliation terminal receipt.
Root cause: Contract 1.8 made authoritative terminal truth the only dependency
predicate without a bounded migration shape for application work merged before
that protocol.
Smallest change: add a dependency-only fulfillment batch. One fresh manual
operator authorization and atomic protected commit bind the repository,
Backlog ticket blobs, exact PR heads/merge commits, required successful check
identities, protected basis, and target Factory SHA while installing that SHA.
The batch does not project Done. Partial or malformed terminal evidence,
unknown paths, mutation/reintroduction, auto-merge, bypass, wrong basis, failed
checks, or missing receipts fail closed.
Validation: the focused dependency suite proves exact adoption, no Done
projection, exact plan-hash application, immutable-history refusal, and
terminal-evidence precedence. Focused state-machine, dispatch-plan, and
ticket-attest suites prove the shared predicate blocks admission and preserves
the provider-free dependency-refresh path. Protected GitHub CI owns the full
registry; the exact T-040/T-092 Nysa batch remains gated on the successor
Factory SHA and separate Nysa activation authorization.

## FI-20260729-105 — Certification discarded valid provider evidence

Status: Implemented; protected CI and live recertification pending
Priority: P0
Area: release certification
Owner: Factory
First seen: Relay certification for Factory
`3f64b9a23fb5deff019e2d3ea1e1c7988658d195`
Impact: certification stopped before product tests even though the sealed
capacity-four provider configuration independently reported `status=ready`.
No receipt or product state changed, but release sealing could not progress.
Evidence: `provider-concurrency apply` and `check` returned canonical ready
JSON for policy
`5d89d08654a135eb26847edb7b25f535ac315fabb27dbf04b307b8a6c04686a7`;
the certification parser raised `JSONDecodeError` at byte zero.
Root cause: `require_provider_concurrency_ready` piped the check result into
`python3 -` while also supplying the Python program through a here-document.
The here-document became standard input, so `json.load(sys.stdin)` saw EOF.
Smallest change: run the same normalizer with `python3 -c`, leaving standard
input attached to the provider check output. Preserve the exact ready-status,
Factory-SHA, Factory-tree, policy, route, capacity, and runtime-root bindings.
Validation: shell syntax and an exact ready-evidence normalization check run
locally; protected GitHub CI owns the complete regression. The live Relay
certification must then succeed on the sealed successor before this entry is
closed.

## Maintenance rule

Record only a systemic failure, backward transition after Spec PASS, sibling
block, or trust/accounting/cancellation divergence. Include immutable evidence,
measured impact, owner, smallest change, and validation. Close an entry only
after a focused regression and one real sandbox canary; then promote the stable
rule into `context/memory.md` and the relevant operating document.

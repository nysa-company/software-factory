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

Status: Confirmed design gap; defer until after qualification
Area: publication evidence
Owner: Factory
First seen: 2026-07-28, Relay Contract 1.8 generation 15 audit
Impact: a future refresh caused only by non-semantic Factory-control movement
would replay Reviewer and Narrator even when their semantic inputs are
unchanged.
Evidence:
- the three recorded T-166/T-168/T-169 refreshes all included T-167 changes to
  `app/server.js` and `app/tests/job-detail.test.js`, so their invalidations
  were correct
- protected main later advanced from `e37587b` to `e919138` through only
  `factory/KIT_PIN`, `factory/QUALIFICATION.json`, and two exact
  `factory/migrations/inflight-release/<sha>.json` records
- the current refresh receipt invalidates review solely because protected main
  advanced; it has no semantic-input digest
Smallest change: bind Reviewer and Narrator validity to authenticated semantic
input plus role-contract digests. Exclude only the three exact control-path
forms above; application code, tests, contracts, CI, configuration, and every
unknown path remain invalidating.
Validation: add focused preservation/refusal cases and one live control-only
base advance after the current qualification. Do not retrofit the current
receipts or change the frozen candidate for this deferred optimization.

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

Status: Observed; defer until after qualification
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
Smallest follow-up: add a protected, exact per-ticket reset authorization that
binds prior remote head, replacement head/tree, receipt, terminal manifest,
branch, and passport parent. Export that one failed boundary without marking
the role successful, then reopen only after the remote equals the authorized
replacement. Never authorize arbitrary or automatic force pushes.

## Maintenance rule

Record only a systemic failure, backward transition after Spec PASS, sibling
block, or trust/accounting/cancellation divergence. Include immutable evidence,
measured impact, owner, smallest change, and validation. Close an entry only
after a focused regression and one real sandbox canary; then promote the stable
rule into `context/memory.md` and the relevant operating document.

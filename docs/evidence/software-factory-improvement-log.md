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

Status: Implemented; protected-CI and live successor proof pending
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

## FI-20260730-106 — Release migration stranded a successful terminal role

Status: Repairing after live two-release canary failure
Priority: P0
Area: portable ticket passports
Owner: Factory
First seen: Nysa generation 28 recovery of T-093
Impact: T-093's successful Test-author output and charge were immutable, and
its old head was an ancestor of the authenticated release-migration head, but
terminal export stopped before adding that role to the passport. Replaying
Test-author would waste a provider call and violate Contract 1.8 evidence
reuse.
Evidence: the consumed receipt bound passport file
`999f688844c45fce00be2ba098f70ef0cf72eb04ecf835a84b759b498b56a5be`.
The first repair accepted its immediate authenticated successor, but the live
canary then crossed the contiguous signed release suffix
`77c8661... -> 51f80d3... -> 8c86539...`; the final passport no longer carried
the original file digest as its immediate `parent_file_sha256`.
Root cause: passport export accepted only a receipt bound to the current
passport file. Controller recovery intentionally migrates the passport before
retrying an unexported terminal boundary, so the receipt instead binds the
immediate authenticated parent.
Smallest change: every new versioned migration edge retains the raw and
embedded digests of the authenticated passport it consumed. A multi-edge
suffix must start with the exact passport file named by the receipt and end at
the exact current Factory/head/base. T-093's already-written legacy edges use
one exact protected-main bridge binding its receipt, source passport, complete
legacy-history digest, target Factory/head/base/route, and terminal manifest,
output, charge, role, Factory, and contract. The new signed edge records the
authorization commit/path/blob/digest, and export rereads it from protected
ancestry. That commit must add only the mode-`100644` record, and export
requires protected main to equal the signed lineage endpoint; later main
movement needs another authenticated edge. Matching terminal evidence now also
requires the receipt's exact Factory and contract. Do not walk arbitrary
ancestors or relax branch, ancestry, authentication, or accounting checks.
Validation: the focused passport suite proves zero-, one-, and two-edge
terminal export, raw-parent and broken-chain refusal, wrong-Factory terminal
refusal, exact protected legacy bridging, authorization-tamper and reuse
refusal, mixed-change authorization refusal, post-migration base-advance
refusal and explicit-edge recovery, cumulative charge uniqueness, and
completed-role preservation.
Protected GitHub CI owns the complete regression; live T-093 recovery must add
the existing Test-author evidence without another provider run.

## FI-20260730-107 — Activation combined a new release with an old launcher

Status: Implemented; protected CI and Nysa recutover pending
Priority: P0
Area: release activation and deterministic recovery
Owner: Factory
First seen: Nysa generation 32 recovery of T-094
Impact: the sealed release issued the correct provider-free
`dependency-refresh` receipt, but the stable launcher rejected that action.
T-094 parked without a provider call or charge, while T-093 and T-100
continued to wait on their declared dependencies.
Evidence: active generation 32 named Factory
`4d726fbaf9cbda8e9de112f991346c5d9eb4901a`; the installed launcher digest was
`361bcd18708fca9fcd00d5748fa6786a1f9998e812a8c68cb10e5523c64d0f59`,
while its sealed counterpart was
`848eaf29f3d296e7f09d3ac471fdd9f46b161029190d0b384b4c56953f08d26e`.
The authenticated T-094 transition remained unconsumed and the final
state-machine independently resolved its preserved repair owner as
`FIX test-author`.
Root cause: release certification bound the kit tree, product tree, provider
policy, and receipt, but omitted the separately installed trust-root launcher.
Contract 1.8 evolved valid controller actions without forcing that executable
to advance.
Smallest change: require the installed launcher to be a safe executable whose
bytes exactly equal the sealed candidate during certification and every later
receipt validation. If they differ, drain work, retain the prior executable
for rollback, and atomically bootstrap the sealed copy before certification.
Validation: shell syntax plus the focused Factory-kit mismatch check run
locally; protected GitHub CI owns the full regression. The successor Nysa
cutover must reopen T-094 through authenticated release migration, consume the
dependency-refresh receipt, and start only Test-author.

## FI-20260730-108 — Admission scans starved independent ticket launches

Status: Implemented; protected CI and live Nysa proof pending
Priority: P0
Area: deterministic controller concurrency
Owner: Factory
First seen: Nysa generation 33 recovery of T-093/T-094/T-100
Impact: T-094 and T-100 independently reached valid provider-free recovery
boundaries, but their workers reported `launch lock stuck` while no provider
call was active. Both released their dispatcher leases without a charge or
evidence loss, yet repeated reconciliation could not reliably reach T-094's
preserved Test-author repair.
Evidence: authenticated controller events
`1785413388672074000-6d8a659094e4d814.json` and
`1785413466610939000-3a809c50e1072026.json` record the two failures. During
both intervals the controller also ran `dispatch-plan --claim` with
T-093/T-094/T-100 excluded, and `factory/.launch.lock` remained present while
that admission process resolved candidates.
Root cause: `dispatch-plan` acquired the product launch lock before evaluating
the Linear candidate set and protected dependency truth. Those read-only,
potentially remote checks ran concurrently with existing ticket resolvers, so
an empty admission scan could exhaust the same lock window required for role
launch and matching lease release.
Smallest change: serialize only admission wakeups with a process-scoped
owner-only `flock` in the generation-wide worktree coordinator. Resolve
candidates and dependencies before the product launch lock, then fail closed
unless the registered checkout, Linear map, control markers, capacity, and
selected ticket remain valid under the launch and dispatcher-lease locks.
The operating-system lock releases on process death and never becomes ticket
state.
Validation: the exact focused regression stalls candidate resolution and
proves an independent launch-lock acquisition before allowing the claim to
complete. All 16 dispatch-plan tests and 37 controller tests pass. Protected
GitHub CI owns the complete regression; live closure requires three concurrent
Nysa resolvers to progress without another launch-lock error and T-094 to start
only its preserved Test-author repair.

## FI-20260730-109 — Live roles have no authenticated progress signal

Status: Backlog
Priority: P0
Area: provider observability and deterministic recovery
Owner: Factory
First seen: Nysa generation 34 T-094 Test-author recovery
Impact: T-094 reattached its execution cell and entered its preserved
Test-author repair through the Cursor CLI Anthropic route, but after the
45-minute soft interval the controller exposed neither authenticated progress
nor a terminal event. The operator cannot distinguish productive slow work
from a stalled provider session. Inspecting raw logs, probing readiness, or
restarting the controller would risk false failure classification, replay, and
duplicate charges.
Evidence: authenticated controller event
`1785416425984707000-c88520856d0d3fe9.json` records T-094's cell reattachment.
The event stream then contains sibling dependency waits and lease recovery but
no T-094 progress boundary. Inspection after the supported stop found more
than 40 terminal manifests for the same Test-author transition receipt over
roughly two hours. Every attempt was `launch_void`, `go_issued=0`,
`task_submitted=0`, `turns=0`, `effective_cost=0`, and exit status 6. The
private controller role log identified the exact pre-GO boundary as
`Cursor subscription credential is unsafe`; metadata-only inspection found
`~/.cursor/cli-config.json` at mode `0644`, while the immutable credential-copy
guard correctly requires no group or world access. A prior successful
Test-author role at commit `948869a4f2d91a0e58a0c0775ad0f097c5f33ace`
remains in the authenticated passport and must not be replayed.
Root cause: `factory-controller.py` omitted `launch_void` from terminal
accounting. `finish_pending_run` therefore treated every valid zero-cost
pre-GO terminal manifest as missing evidence, cleared the receipt, and
immediately resolved the same repair again. Separately, the provider runtime
already wrote a private, sequenced progress journal, but the controller did
not project it into content-free lifecycle events.
Smallest change implemented: recognize `launch_void` as terminal, coalesce
only identical historical zero-cost/no-GO duplicates for recovery, block the
same release after one pre-GO failure, and permit one exact-stage retry only
when an upgraded Factory SHA is authenticated. The wrapper now records a
typed redacted pre-GO reason. The controller emits digest-bound
`attempt_started`, `attempt_bound`, `attempt_progress`, and
`attempt_terminal` events from the existing progress journal. Events bind the
ticket, role, receipt, run, route, Factory SHA, sequence, and journal digest;
they never contain role output or credential data. Progress observation does
not cancel or classify a quiet provider as failed.
Focused validation now covers repeated `launch_void` refusal, one
release-upgrade retry, monotonic/deduplicated content-free progress, controller
restart behavior, sibling scheduling, and existing structured Cursor stream
evidence. Protected GitHub CI remains responsible for the complete regression.
Live successor proof must show T-094 records one start, one GO/submission,
monotonic progress, and one terminal event without replaying its earlier
successful Test-author role. The remaining validation is to prove a producing
mock emits monotonic signed progress;
a silent healthy mock crosses the soft interval without cancellation; a
stalled process reaches the existing hard limit exactly once; forged,
reordered, cross-attempt, wrong-head, and wrong-Factory heartbeats fail closed;
controller restart preserves the latest valid heartbeat without creating a
second attempt; sibling tickets continue; and no heartbeat leaks output or
credentials.

## FI-20260730-110 — Cursor readiness invalidated its own credential source

Status: Implemented; protected CI and live role proof pending
Priority: P0
Area: provider readiness and credential isolation
Owner: Factory
First seen: Nysa generation 35 in-flight route migration
Impact: all four route migrations completed without a provider task, but
Cursor's task-free readiness commands rewrote the source
`~/.cursor/cli-config.json` from mode `0600` to `0644`. The next Cursor role
would therefore fail the unchanged strict credential-copy boundary before GO,
recreating the exact T-094 `cursor_credential_unsafe` failure and preventing
useful ticket work.
Evidence: the source file was mode `0600` immediately before migration;
after four sealed `models migrate-plan`/`models migrate` checks it was `0644`,
with no controller or provider task running. The focused production-boundary
reproduction restored `0600`, ran the patched real Cursor readiness probe,
and observed `READY:local_contract_ready`, unchanged source digest, and mode
`0600` afterward.
Root cause: `factory_probe_adapter` invoked Cursor `--version`, `--help`,
`status`, and `models` with the credential source as `HOME`. Cursor legitimately
rewrites its CLI configuration during those task-free commands. Later runtime
preparation correctly rejected the now-world-readable source, so readiness
made its own successful route impossible to launch.
Smallest change: validate and copy both Cursor session files into one
disposable mode-`0700` probe home using no-follow, owner, link-count, mode, and
size checks. Run every command for that probe against only the copy, then
delete it. Missing credentials still produce ordinary authentication
unavailability; present but unsafe or partial source state fails closed before
Cursor is invoked. The role-time credential guard and all security assertions
remain unchanged.
Validation: the focused regression uses a Cursor stub that deliberately
rewrites its probe-local config to `0644`, proves the real source bytes and
`0600` mode remain unchanged, proves the probe home is removed, and proves an
unsafe source is refused before process invocation. The model-control suite,
production-concurrency suite, shell syntax, and the real task-free Cursor
boundary pass. Protected GitHub CI owns the complete regression. Live closure
requires T-094 to cross GO/submission once and emit authenticated progress
without another credential failure.

## FI-20260730-111 — A one-shot re-admitted tickets after a real wait

Status: Repair in progress after the defect blocked T-094 recovery
Priority: P0
Area: deterministic controller scheduling and lease handoff
Owner: Factory
First seen: Nysa generation 36 recovery of T-093/T-094/T-100
Impact: the controller correctly recovered cross-release passports, but after
T-093 and T-100 returned real dependency waits it scheduled them again inside
the same invocation. Their zero-provider lease acquire/release cycles competed
with T-094's exact Test-author repair. T-094 then reached
`ticket already has a dispatcher lease`, parked, and never opened a provider
attempt. No provider task ran and no charge was created.
Evidence: generation-36 authenticated events show
`dependency_wait -> parked_lease_released` for T-093, repair-record recovery
for T-094/T-100, repeated `ticket_lease_recovered`, and a T-094
`controller_error` naming the existing lease. The controller process remained
live for more than six minutes with all three claims at wait/blocked
boundaries and three dispatcher lease records, instead of terminating its
one-shot. Provider coordination reported zero active attempts and tokens.
An unrelated admission scan also repeatedly reported that a candidate remote
branch did not match its reset authorization; this must not keep already-owned
tickets alive or block their exact stages.
Root cause: the concurrent scheduler records a terminal worker result but uses
only a short cooldown before considering the same `waiting` claim runnable
again. A real wait is therefore treated as an internal polling interval rather
than the terminal boundary of the current one-shot. The overlapping recovery,
parking, and re-admission paths can then observe different generations of a
claim and its lease record.
Safest batched repair: maintain an invocation-local settled-ticket set.
`waiting`, `blocked`, `budget`, `error`, and `maintenance` results must not be
submitted again during that invocation; terminal run/watch events or the next
launchd invocation provide the external wake. Make lease handoff one
transactional controller action whose claim and lease record cannot diverge,
and prove a failed unrelated admission cannot keep settled owned tickets
cycling. Preserve concurrent live provider workers and sibling admission.
Validation required before promotion: three waiting claims execute once and
the one-shot exits; one live provider may continue while a newly visible
sibling starts; an external terminal event launches a fresh one-shot; lease
release/reclaim survives interruption at every write boundary; an unrelated
reset-authorization refusal does not alter owned claims; no provider call,
successful role, or charge is replayed.

## FI-20260730-112 — A later receipt hid an authenticated contract repair

Status: Successor amendment implemented; protected CI and live continuation pending
Priority: P0
Area: deterministic controller recovery
Owner: Factory
First seen: Nysa generation 36 recovery of T-094
Impact: T-094 retained its authenticated Test-author repair record, complete
passport lineage, original failed Builder charge, and clean current head, but
the controller could not reopen it. A later deterministic wait/repair receipt
had replaced the original consumed Builder receipt file. Recovery looked only
at that mutable latest-receipt slot, left the claim blocked, and made no
provider call.
Evidence: the owner-only repair record binds the original Builder receipt and
Test-author owner; the current passport authenticates the complete migration
suffix to the exact current head and Factory; the parked cell is clean and
matches the remote. Direct state-machine validation resolves `FIX test-author`.
The controller claim nevertheless remains blocked because
`restore_contract_blocker` requires the latest receipt file itself to still be
the original consumed blocker.
Root cause: recovery conflated the latest transition-receipt cache with the
durable authenticated repair checkpoint. Replacing a transition receipt is
normal across dependency waits and release migration, so the latest slot is
not the repair authority.
Safest batched repair: when a blocked receipt-free claim has a contract-repair
record, validate the current remote passport and ask the deterministic state
machine to resolve the exact current stage. Only a valid, non-refusal result
may reopen the claim. The resolver authenticates the repair record, passport,
failed charge, terminal manifest, ancestry, repair owner, and current head.
The issued receipt is reused unchanged when the worker begins. On refusal,
release only a newly acquired lease and keep the claim blocked.
Validation required before promotion: reproduce a valid repair record whose
original blocker receipt has been replaced by a later unconsumed receipt,
recover only the named owner, and prove malformed record, broken passport,
changed remote, ambiguous failure, and `REFUSE` keep the ticket blocked without
a provider call or charge.

2026-07-31 successor occurrence: T-094's authenticated Builder blocker was
resumed to its exact Test-author owner under Factory `0bd2c79...`. The state
machine materialized Building, migrated the passport, and persisted the signed
repair, but the controller claim correctly still named the failed Builder
receipt and role. `restore_recorded_contract_repair` accepted only claims whose
receipt and role were already empty, so it skipped the valid repair. The
fallback then attempted to materialize the already-resumed blocker again and
failed with `contract blocker receipt is invalid` on every one-shot. No
provider call or additional charge occurred after the resume.

Successor amendment: a blocked inactive claim with a repair record is admitted
to recorded-repair recovery when its receipt and role are both empty, or when
they exactly equal the repair record's blocked receipt and role. The state
machine remains the authentication authority for the repair, passport,
terminal evidence, charge, ancestry, owner, and current stage before the
controller clears the stale claim fields. Partial or mismatched fields remain
blocked without calling the state machine. Focused controller coverage proves
the real retained-field topology routes only `FIX test-author` and that a
receipt mismatch makes no recovery call.

## FI-20260730-113 — A successful repair left its checkpoint active

Status: Successor amendment implemented; protected CI and live continuation pending
Priority: P0
Area: passport lineage and targeted-repair completion
Owner: Factory
First seen: Nysa generation 37 after T-094 Test-author
Impact: the upgraded controller recovered T-094's exact Test-author checkpoint
and the role completed successfully with authenticated progress and terminal
events. Its terminal passport then became the next blocker: ordinary export
consumed a one-use legacy lineage authorization by intentionally omitting that
migration history, while the still-active repair record required the omitted
history on every later transition. The state machine rejected
`contract repair record is invalid` instead of advancing to Builder. No role
was replayed and the successful Test-author evidence and charge remained in
the authenticated passport.
Evidence: run `1785435281-45017` is the unique successful Test-author descendant
of the signed repair head. Its consumed `FIX test-author` receipt binds the
prior passport file, and the exported passport binds that receipt, exact run
manifest, charge, head, Factory SHA, and parent file digest. The exported
passport contains the complete successful-role evidence but no
`migration_history`, matching the required one-use lineage-consumption rule.
Root cause: repair records represented pending work but had no deterministic
terminal consumption boundary. The resolver continued treating a completed
repair as pending, and ordinary passport export also failed to carry normal
v2-only migration history when no one-use authorization required its removal.
Smallest repair: preserve normal v2 migration history across terminal export
but continue omitting any history containing a consumed lineage authorization.
After exactly one successful repair-role manifest, archive the signed repair
record so it cannot affect later stages. For the already-exported legacy case,
allow retirement only when the consumed `FIX <owner>` receipt binds the prior
passport file and the current authenticated passport binds the same unique
manifest, charge, head, role, and receipt. If the completed export is then
migrated before the next transition, accept only its contiguous authenticated
v2 migration suffix: the suffix must begin at the repair Factory and a
descendant of the repair head, end at the exact current Factory, head,
protected base, route plan, and passport parent, and contain no legacy lineage
authorization. Missing success, duplicate success, tampered lineage, broken
suffixes, unknown paths, or any other missing binding still refuses.
Validation: focused passport and state-machine regressions prove normal
migration history persists, a legacy authorization remains one-use, the exact
lost-history terminal proof and its one-edge v2 migration successor each retire
one repair record, repeat resolution no longer sees it, and malformed or
duplicated evidence remains fail closed. A disposable copy of the real T-094
passport, repair record, 425 run manifests, and route-migrated head
`7f9314bed1e0a7bea1ac079a8864655ea9a977e1` resolved to Builder and archived
the active record without replaying Test-author.

## FI-20260730-114 — A protected test conflict had no deterministic owner

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: dependency refresh and exact-stage recovery
Owner: Factory
First seen: Nysa generation 39 T-094 recovery after protected main advanced
Impact: the controller correctly refused an unsafe automatic merge and spent
no provider budget, but converted one classifiable protected-test conflict
into a generic blocked claim. T-100 and T-093 then remained at their declared
dependency waits, so additional cells or provider capacity could not create
progress.
Evidence: authenticated controller event
`1785443497967850000-6c347494cdde648c.json` records
`protected dependency base conflicts with the ticket branch`. Exact
`merge-tree` evidence names only
`apps/web/tests/knowledge-index.test.tsx`; the base, ticket, and protected
entries are regular mode `100644`, and protected
`factory/PROJECT.env` assigns `apps/web/tests/` to Test-author. The merge
aborted cleanly at ticket head
`1883d896cc8419f0be8d2c7f4c8a2cb7c60e512d`; no new run manifest or charge
was created.
Root cause: provider-free dependency refresh had only two outcomes: clean
merge or generic refusal. It did not authenticate conflict inputs, classify a
single safe owner, or issue an exact repair stage.
Smallest repair: extend the existing refresh transaction with a v2 receipt
only for regular both-modified paths wholly under protected `TEST_PATHS`.
Bind exact base/ticket/protected modes and blobs, protected project and delta,
dependencies and terminal digests, old/protected heads and trees, transition
receipt, merge topology, Factory, and contract. Retain the protected blobs as
the safe merge baseline, migrate the passport, and let the state machine
create one HMAC-bound `FIX test-author` checkpoint. Earlier successful roles
do not satisfy this boundary and remain preserved. Sibling protected-main
advancement does not interrupt the signed historical repair; normal refresh
absorbs it afterward, while merged publication truth closes before another
refresh. Retirement requires the exact consumed FIX receipt and head, the
authenticated terminal passport, one matching completed/charge pair, and
only regular modifications to the listed tests or ticket log. Every
application, mixed, control, contract, CI, configuration, rename, add/delete,
non-regular, missing-receipt, unknown, or tampered conflict restores or
remains at the safe head and refuses.
Validation: the affected attestation suite passed all 51 tests before the
launcher-boundary assertion was added; the exact launcher, clean refresh,
safe test-conflict, and unsafe application-conflict checks pass together. The
state-machine suite passes 13 and the controller suite passes 45. A disposable
mirror of the exact T-094 and protected-main heads produced one v2 receipt,
one two-parent merge, one protected-baseline test blob, and the exact
Test-author owner; the independent state validator accepted all receipt and
topology bindings. Protected GitHub CI owns the complete regression. Live
closure requires the successor to route T-094 to one Test-author run, then
Builder, without replaying Planner or Spec-linter or charging an unchanged
role.

## FI-20260730-115 — Conservative accounting hid a successful repair

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: exact-stage recovery and accounting
Owner: Factory
First seen: Nysa generation 40 T-094 Test-author repair
Impact: Test-author completed the authenticated dependency-conflict repair,
committed the exact allowed test and ticket-log changes, and exported unique
role and charge evidence. The next reconciliation nevertheless parked the
ticket with `dependency conflict repair success is invalid`, preventing
Builder and all declared dependents from advancing.
Evidence: run `1785452093-97960` exited zero with `role_exit=ok`, 92
authenticated progress events, consumed FIX receipt
`7d8eccd5652ee0d1c6b166fed8e1cdc89cccdb130552cc9652fcfbb90d7e8b7a`,
and committed head `6fd39e6438ac823c56f911f1a7c45b238657d6bf`. Its Cursor CLI route cannot
report actual cost, so the immutable manifest and passport conservatively
charged the full reservation with `accounting_state=abandoned_conservative`,
`cost_basis=conservative_reservation`, and equal effective and reserved cost.
Root cause: ordinary terminal, passport, publication, and contract-block
validation already recognize that fully bound conservative charge as
accounted. The new dependency-conflict success validator alone required the
literal state `completed` in both the manifest and passport charge.
Smallest repair: use the existing fail-closed accounting rule at this one
boundary. Accept `completed`, or accept `abandoned_conservative` only when the
manifest binds conservative-reservation cost and exact equality between
effective and reserved cost. Require the passport charge to carry the same
accounting state and the same immutable manifest digest. Cancellation,
launch-void, missing cost proof, unequal reservation, and every unknown state
remain invalid.
Validation: the exact dependency-conflict state-machine test now exercises a
successful conservative Cursor charge and separately rejects a forged
`cost_basis=actual` variant. A disposable clone of the real T-094 head,
passport, repair checkpoint, transition receipt, and run manifest crossed the
former error boundary and retired the repair; ordinary release/lease
requirements then refused later resolution as expected. Protected GitHub CI
owns the complete regression.

## FI-20260730-116 — Release migration hid a completed repair boundary

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: portable checkpoints and release migration
Owner: Factory
First seen: Nysa generation 41 T-094 recovery
Impact: T-094's exact Test-author repair had already succeeded and committed
head `6fd39e6438ac823c56f911f1a7c45b238657d6bf`. The subsequent Factory and
route migration advanced the ticket to
`508c29be2ee88a3356da9281a5dccb00f0f4fc81`. Recovery rejected
`dependency conflict passport is invalid` before the completion validator
could archive the checkpoint. No provider call or duplicate charge occurred,
but T-094 and its declared dependents remained unable to advance.
Evidence: the authenticated passport contains one unique successful
Test-author manifest and charge bound to consumed FIX receipt
`7d8eccd5652ee0d1c6b166fed8e1cdc89cccdb130552cc9652fcfbb90d7e8b7a`.
Its v2 migration suffix begins at completed repair head `6fd39e6...`, preserves
the historical b375 Factory evidence, and ends at the exact c013 Factory,
route-plan digest, protected base, passport parent, and current head. The
signed active repair record still names the earlier pre-role head
`b979651...`, as it must. Protected main advanced through only the reviewed
Factory-control activation, so it was not yet an ancestor of the ticket branch;
ordinary dependency refresh is the next required operation.
Root cause: the pending-checkpoint validator required a migration suffix to
start at the pre-role repair head. After a successful role, the authenticated
suffix correctly starts at the role's terminal export head instead. The later
repair-output validator also diffed the pre-role head against the post-route
migration head, incorrectly treating the route journal as Test-author output.
Finally, the pre-resolution guard required the new protected base to already
be branch ancestry even though completed-checkpoint retirement must happen
before ordinary dependency refresh.
Smallest repair: recognize one completed checkpoint bridge only when the
consumed FIX receipt, original Factory, role head, manifest, conservative or
actual charge, authenticated completed-role evidence, allowed repair diff, and
one unique contiguous v2 suffix all agree. Validate Test-author mutations only
through the suffix's terminal export head; retain historical Factory identity
for the evidence. Permit a non-ancestor protected base only for that fully
completed bridge and perform no provider work; the next deterministic
transition remains provider-free dependency refresh. Pending repairs,
ambiguous successes, legacy lineage authorizations, broken parents, unknown
paths, or incomplete suffixes remain invalid.
Validation: all 13 focused state-machine tests pass. The exact regression now
includes a successful repair followed by a release migration, route-journal
change, and non-ancestor protected-control base, plus a broken passport-parent
negative case. A disposable clone of the real T-094 branch, passport, signed
repair, transition receipt, and run manifests archived the repair without
replay and resolved to the exact dependency-refresh refusal for protected main
`a3e5548c5b76a62981162be15282444da25b599a`. Protected GitHub CI owns the
complete regression.

## FI-20260730-117 — Waiting claims skipped release recovery

Status: Repair implemented; protected CI and live continuation pending
Priority: P0
Area: release cutover and deterministic controller recovery
Owner: Factory
First seen: Nysa generation 42 T-100 recovery
Impact: T-100's route migration advanced its clean branch to
`196a45a020e7cfb047c1841f96cde91328a3c02a` under Factory
`8e2fb1f051a18e454deb35f49ff195dfccfb5940`, while its authenticated passport
still named the prior Factory and head. The first controller invocation tried
to interpret the repair record against that split identity and emitted
`contract repair record is invalid`. It made no provider call or charge. A
later invocation recovered the same passport and reached the correct
`AWAIT_DEPENDENCY T-094` state without replay.
Root cause: release recovery considered only claims whose coarse controller
status was `blocked`. A dependency-wait checkpoint can be just as stale after
an authorized cutover, but `waiting` and clean `claimed` claims went directly
to ordinary reconciliation.
Smallest repair: before any runnable scheduling, apply the existing
authenticated release-recovery transaction to `blocked`, `waiting`, and
`claimed` claims. It still acts only when the signed passport names a prior
release or a durable route-migration marker exists; same-release claims are
unchanged. Recovery reacquires only the exact ticket lease, preserves every
completed role and charge, and keeps contract-blocked or successful-terminal
boundaries blocked for their existing specialist recovery.
Validation: a focused controller regression starts with a released waiting
lease, prior-release route, and prior-release passport; proves the passport
migrates while the claim stays blocked; then advances the route and proves
exact-ticket reclaim happens before reconciliation. The complete 48-test
controller suite passes. Live closure requires the successor cutover to
recover T-100 on its first controller invocation and return directly to its
dependency wait without a provider attempt.

## FI-20260730-118 — Maintenance raced a second stage resolution

Status: Repair implemented; protected CI and live continuation pending
Priority: P0
Area: deterministic state-machine and controller drain
Owner: Factory
First seen: Nysa generation 42 T-094 maintenance drain
Impact: T-094 completed a provider-free dependency refresh at head
`821d11e75e583633b6132952af614798a6f4d950`. Maintenance was published after
the state machine resolved its next role but before it finished. A second
`next-stage` call observed the new maintenance marker, differed from the
already resolved role, and raised `transition changed the resolved stage`.
The controller converted the clean, receipt-free, role-free claim to
`blocked`. No provider process, active run, or new charge existed, but no
ordinary recovery path could reopen that claim.
Root cause: execution recomputed a transition already owned by `next-stage`,
contradicting the Contract 1.8 single-resolution rule. The controller also did
not include `maintenance` among the terminal results that park a clean claim
and release its dispatcher lease.
Smallest repair: resolve the stage exactly once, complete the state-machine
receipt transaction, then recheck maintenance in the controller before any
PR or provider action. If maintenance appeared, record
`stage_resolution_paused`, leave the receipt unconsumed, settle and park the
ticket, and release its lease. The next invocation chains an ordinary new
receipt from the abandoned one. Ambiguous state-machine, passport,
publication, or provider errors remain fail closed.
Validation: one state-machine regression proves a role stage calls
`next-stage` once while binding the resulting role and receipt. A completed
repair returns that already-resolved normal stage to its caller instead of
hiding another lookup. One controller regression publishes maintenance inside
that exact boundary and proves no role runner is called and the lease drains;
malformed transition evidence at the same boundary still blocks. The complete
focused suites pass 15
state-machine and 48 controller tests. A disposable copy of the exact
T-094 head, passport, repair archive, run history, protected base, and lease
resolves `RUN builder` through the repaired one-resolution path. Protected
GitHub CI owns the complete regression; live closure requires T-094 to resume
Builder from preserved evidence after the successor cutover.

## FI-20260730-119 — Lease rotation rejected an already-materialized blocker

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: contract-blocker restart recovery
Owner: Factory
First seen: Nysa generation 43 T-094 Test-author resume
Impact: T-094's Builder contract blocker had already been authenticated,
charged, committed as `Blocked-Escalated`, exported into its signed passport,
and parked without replay. After the one-shot controller restarted, normal
lease recovery replaced the expired ticket lease. Recovery then retried the
completed `block` transition using the new lease and emitted
`contract blocker receipt is invalid` on every reconciliation. No provider
call or additional charge occurred, but the exact Test-author repair could not
start and T-093/T-100 remained in their declared dependency waits.
Evidence: consumed Builder receipt
`9b0e39dcfbb846af63cbefad61d4aa45fa08e4c616d8bc993db5f4cc8ee07b38`
and run `1785465865-25540` produced blocked head `0532469...`, an authenticated
passport with state `Blocked-Escalated`, cumulative charge $80, and the same
receipt and role stage. The operator-only commit `2ef4e82...` appended exactly
`OPERATOR RESUME: test-author`; Linear already exposed the recorded Building
resume state. Authenticated controller events then showed lease recovery
followed by repeated receipt rejection before any role attempt.
Root cause: initial block materialization is correctly authorized by the
receipt's exact lease. The controller restart correctly acquires a fresh
ticket lease, but its recovery path calls `block` again before `resume`.
Same-release receipt validation treated that idempotent replay as a new block
and required the historical lease, whose raw value is intentionally not
recoverable from its stored digest.
Smallest repair: keep the historical lease mandatory for the initial block.
After lease rotation, permit only the already-materialized block to be
revalidated when the authenticated passport binds the exact project, ticket,
branch, Factory, contract, receipt, unique blocker charge, role stage,
`Blocked-Escalated` state, exact resume target, and
receipt-head → passport-head → current-head ancestry, with no successful role
evidence for the blocked receipt. Any missing, tampered, ambiguous, unblocked,
or unrelated evidence still rejects the new lease.
Validation: a focused state-machine regression creates a consumed Planner
blocker, materializes the blocked commit and authenticated passport, rotates
the lease, and proves exact idempotent validation succeeds. Changing only the
authenticated passport state back to Planning is rejected. A read-only run of
the candidate validator against T-094's exact parked worktree, live
authenticated passport, blocker receipt, and rotated current lease crosses the
former boundary and returns the historical Builder owner. Protected GitHub CI
owns the complete regression; live closure requires the successor to
authenticate T-094's existing block and start exactly one Test-author repair.

## FI-20260730-120 — Activation excluded preserved blocked tickets

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: in-flight release cutover
Owner: Factory
First seen: Nysa generation 43 successor activation
Impact: Factory `25c73e3...` installed successfully after all protected
release checks passed, but its exact in-flight authorization could not name
T-094 because the ticket was correctly committed as `Blocked-Escalated`.
Omitting T-094 would make the authorization partial, and changing its state
before migration would invalidate the authenticated blocker passport that the
successor was built to recover. No product certification, activation, route
migration, provider call, or additional charge occurred.
Evidence: protected remote ticket heads were T-093 Building, T-094
Blocked-Escalated, T-096 Planning, and T-100 Building. The activation
validator accepted Ready, Planning, Building, Review, Awaiting Approval, and
Approved, while the passport's protected in-flight rewrite validator repeated
the same closed set. Controller recovery, development-lane export, and blocker
resume already treat Blocked-Escalated as a preserved nonterminal checkpoint.
Root cause: the protected cutover schema was introduced before blocked-ticket
upgrade recovery and its duplicated state allowlists were not extended when
that recovery became a supported invariant. The activation boundary and the
subsequent passport migration therefore disagreed with the controller.
Smallest repair: admit only the exact canonical `Blocked-Escalated` value in
both in-flight validators. Preserve it byte-for-byte through activation and
route/passport migration; do not use cutover as a resume action. Existing
exact repository, protected-main authorization, source/target Factory, branch,
head, state, route journal, maintenance, active-run, and dispatcher-lease
checks remain mandatory. Backlog, Canceled, Done, unknown, partial, extra, or
state-drifted entries remain invalid.
Validation: the focused passport test now migrates one authenticated blocked
passport through an exact protected authorization. The protected release test
uses a blocked remote ticket with `Resume-State: Planning`, proves activation
accepts its exact tuple, then proves the sealed route migration preserves
Blocked-Escalated rather than resuming it. Protected GitHub CI owns the full
activation/install regression. Live closure requires the successor to migrate
T-094 while it remains blocked, then let the deterministic state machine
consume the already-present operator resume separately.

## FI-20260730-121 — Historical same-role directives blocked a current repair

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: exact-stage repair recovery
Owner: Factory
First seen: Nysa generation 44 T-094 Test-author resume
Impact: T-094 migrated successfully to Factory `d82fde3...`, but deterministic
recovery rejected its current Test-author directive before any provider call.
T-093 and T-100 remained in dependency waits, so all three tickets made no
product progress despite preserved passports and available capacity.
Evidence: the current directive commit `2ef4e82...` has authenticated blocked
parent `0532469...` and is followed by exact route-migration head `7067d96...`.
The ticket history also contains older same-role additions `a79c64a...` and
`b8b9cb9...`; their parents are outside the current passport repair window.
Generation 44 emitted only authenticated `ticket_recovery_failed` events with
`contract repair operator directive is invalid`; it launched no provider call
and added no charge.
Root cause: `operator_resume_role` searched the entire ticket history for the
directive text and required exactly one `git log -S` result. A legitimate
second repair owned by the same role therefore became permanently
unrecoverable even though passport lineage identified the current repair
unambiguously.
Smallest repair: select only the unique normal directive commit whose parent
is an authenticated current-passport or v2 migration head and whose commit is
current branch ancestry. Preserve the existing exact append, single-ticket-
file diff, one visible directive, and current-head checks. Historical
same-role directives outside that window no longer collide; zero, multiple,
merge, malformed, multi-path, or drifted in-window candidates still fail
closed.
Validation: a focused state-machine regression creates a completed historical
Test-author directive, a new authenticated blocker, the same current
Test-author directive, and a later route migration. It proves only the current
repair resolves, then adds a second in-window candidate and proves ambiguity
is rejected. The exact live T-094 history confirms only `2ef4e82...` has a
parent in the current authenticated repair window. Protected GitHub CI owns
the complete regression; live closure requires generation 44's successor to
resume exactly Test-author without replaying earlier roles.
Additional qualification occurrence: while separating Nysa gate policy into
protected PR #305, an explanatory operator commit preceded the first receipt
binding. Sealed passport migrations then authenticated the withdrawal and
final binding endpoints. Candidate `991c5f8...` counted the withdrawal's
`git log -S` match as a second authorization and refused before provider GO.
The validator now admits a history match as a candidate only when that
commit's resulting ticket contains the exact visible role-and-receipt pair.
The focused 24-test state-machine suite proves the withdrawal is ignored,
while the existing two-actual-authorizations negative remains fail-closed.
Successor seal and live T-094 resume remain pending.

## FI-20260730-122 — Repeated blocker could not hand recovery to an earlier owner

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: exact-stage repair recovery
Owner: Factory
First seen: Nysa generation 45 T-094 Test-author recovery
Impact: T-094's bounded Test-author repair terminalized with 25 authenticated
progress events and proved a contradiction in frozen contract version 2.
The ticket was correctly parked as `Blocked-Escalated`, but the supported
resume path could name only the already-visible Test-author directive and
could not move the repair owner back to Planner. T-093 and T-100 remained in
dependency waits, leaving all three tickets provider-idle.
Evidence: terminal run `1785477349-40736` is uniquely bound to transition
receipt `1f9a6694...`, exit 12, `role_exit_contract_blocked`, and conservative
accounting. Ticket head `78928664...` records that no standards-compliant DOM
can satisfy both the frozen descendant `Meeting` text and the ancestor's exact
copy without that text. Its authenticated passport says `FIX test-author`,
while `Resume-State: Building` is the coarse state mandated for a Test-author
blocker. The state machine's repair loop had no legal `Building → Planning`
case, and its exact-append grammar could not replace the single active
Test-author directive with Planner.
Root cause: the earlier repair was validated as separate boundaries—directive
lineage, repair-owner selection, and one successful repair—but never as a
complete repeated-blocker lifecycle. The implementation described earlier
owners catching up beneath the coarse state, yet `resume_transition` still
required the coarse state to equal the repair owner's state. It also treated
the one visible directive as append-only and treated that immutable directive
as an error after the signed repair record was archived.
Smallest repair: allow the one visible repair-owner directive to be replaced
exactly in a normal ticket-only commit whose parent is in the authenticated
passport window. When the selected owner precedes the coarse state, keep the
coarse state unchanged and persist the HMAC-bound repair record for the exact
earlier role. After the repair succeeds, accept the visible directive as
historical only when a safe signed completed record for that role and branch
is in current head ancestry. Missing, mismatched, tampered, multi-directive,
multi-path, merge, or unrelated histories remain fail closed.
Validation: the focused state-machine suite now contains one end-to-end
regression for the exact failure family: Test-author blocks in Building, the
operator replaces its active directive with Planner, resume performs no
general backward state transition, only Planner is returned, its success
continues to Spec-linter, and the archived signed repair makes the historical
directive inert on the next reconciliation. All 21 focused state-machine
tests pass. Protected GitHub CI owns the complete regression; live closure
requires T-094 to perform this same handoff without replaying its earlier
successful role evidence.

## FI-20260730-123 — A stale resume decision relaunched the same blocker twice

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: exact-stage repair recovery
Owner: Factory
First seen: Nysa generation 45 T-094 Test-author recovery
Impact: after Test-author had already proved the frozen criterion-8
contradiction, the controller launched the same role twice more instead of
waiting for a new operator decision. T-094 gained no test or contract
resolution, T-093 and T-100 remained parked, and conservative accounting added
$20 across two redundant calls.
Evidence: runs `1785478242-61245` and `1785478871-78303` each terminalized
with exit 12, `role_exit_contract_blocked`, 30 and 33 authenticated progress
events, and distinct consumed receipts `a2e37f02...` and `848baf2a...`.
Ticket commits `954355a0...` and `edb45131...` only re-confirm the same
criterion-8 contradiction. No new ruling or contract version appeared.
`contract_blocker_recovered` events show the controller immediately treated
each new blocker as resumable.
Root cause: repair authorization named only a role. The old visible
`OPERATOR RESUME: test-author` line remained in branch history/current text,
and Linear still exposed the earlier Building resume. Passport ancestry proved
the line was authentic but did not prove it authorized the latest blocked
receipt, so every later blocker could reuse the same decision.
Smallest repair: require exactly one adjacent role-and-receipt directive pair.
The receipt digest must equal the current consumed blocker receipt, and the
unique ticket-only directive commit must remain bound to the authenticated
passport window. A later blocker therefore requires replacement with its new
receipt even when the owner is unchanged. No directive now defaults to no
resume. Completed signed repair history recognizes only the matching role and
blocked receipt.
Validation: the end-to-end repeated-blocker regression begins with a valid
historical Test-author decision for receipt A and a new Test-author blocker
receipt B; it proves the stale pair cannot resume, then replaces the pair with
Planner plus receipt B, runs only Planner beneath Building, continues to
Spec-linter, and recognizes the archived exact pair. All 22 focused
state-machine tests pass. A separate compatibility regression upgrades the
single legacy role-only directive already present on T-094 into the exact
Planner/`848baf2a...` pair in one ticket-only commit; partial directives,
unrelated changes, and unbound receipts remain refused. Protected GitHub CI
owns the complete regression; live closure requires T-094 to remain
provider-idle until that exact decision is committed and ingested.

## FI-20260730-124 — Preflight rejected the authenticated earlier repair owner

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: exact-stage repair admission
Owner: Factory
First seen: Nysa generation 46 T-094 Planner repair
Impact: the deterministic state machine authenticated the exact Planner repair,
refreshed the protected base without a provider call, and reattached T-094 to
an execution cell. Preflight then parked the ticket before submission because
its visible coarse state remained Building. T-093 and T-100 stayed in their
declared dependency waits; no provider call or new charge occurred.
Evidence: transition receipt
`c0769645f48418542ac2698a00148ac10754d9608df2e55c463995d7d807ae70`
is head-, passport-, lease-, role-, and Factory-bound to `FIX planner`.
The exact preflight returned readiness, route, budget, repository, and Linear
passes followed by `FAIL: ticket not Planning (State: Building)`. The
controller emitted authenticated `ticket_blocked` reason `preflight` and
parked the clean cell.
Root cause: repeated-blocker recovery was tested through state-machine stage
selection but not through the next admission boundary. Preflight recomputed
Planner eligibility solely from the visible state instead of consuming the
stage already returned by authenticated receipt verification.
Smallest repair: the installed launcher's empty helper environment carries the
exact verified transition stage into preflight. Normal `RUN planner` continues
to require Planning. Only exact `FIX planner` bypasses that duplicate state
comparison; readiness, receipt, lease, route, budget, repository, initiative,
and provider admission checks remain unchanged.
Validation: the focused preflight regression proves a Building ticket passes
only with verified `FIX planner`, while the same ticket with `RUN planner`
still fails. Shell syntax, the focused state-machine suite, and an exact live
T-094 preflight remain required; protected GitHub CI owns the complete
regression.

## FI-20260730-125 — Repeated preflight used a writable sealed-release fixture

Status: Repair implemented; protected CI pending
Priority: P0
Area: release verification
Owner: Factory
First seen: Factory protected-main run `30614711253`
Impact: the Linux Hermes shard rejected the second Planner-repair preflight
before the successor Factory could be sealed. Other protected shards continued
independently; no product state, provider call, or charge changed.
Evidence: Linux job `91105190009` passed the first sealed preflight, then both
new repeated calls failed at `physical release tree does not match trusted
release provenance`. The same focused suite passed on macOS. The fixture was a
writable copy, unlike an installed sealed release.
Root cause: the new regression correctly reused one release across multiple
preflights but did not apply the installation boundary's read-only mode.
Release-local Python imports could add Linux bytecode after the first tree
digest, and the second validation correctly detected that physical drift.
Smallest repair: make only the repeated Planner-repair release fixture
read-only after its tree digest is computed. Keep the independent writable
fixture used to prove intentional physical-drift refusal and every existing
provenance assertion unchanged.
Validation: shell syntax and the focused preflight suite must pass on Linux;
protected GitHub CI owns the complete regression.

## FI-20260731-126 — A successful migrated repair invalidated its own lineage

Status: Repair implemented; protected CI and live T-094 proof pending
Priority: P0
Area: exact-stage repair and passport migration
Owner: Factory
First seen: Nysa generation 47 T-094 Planner repair
Impact: T-094 completed its authorized Planner repair successfully and
committed contract v3, but the next deterministic transition rejected the
still-valid repair record. The controller parked T-094, while dependent T-100
and T-093 remained safely waiting. No successful role was replayed and no
sibling provider call was launched, but all three tickets stopped advancing.
Evidence: consumed `FIX planner` receipt
`b1912d7410fa13ec7a888cb24d674f78279c7fc2b5b3d255e7fb0afadecc82a0`
binds Factory `2ef229720d70cf9be0bc3c6e4903d14585495bd0` and role-input
head `ee91c3338b50188aff7a8c37ab6dcb2cca1ce42d`. Run
`1785490307-75649` terminalized `role_exit=ok` and advanced the authenticated
passport and branch to `065477d1fef538e4656d033cec6fa08d48dc8728`.
The passport contains exactly one matching completed-role record and one
matching conservative charge. The signed repair's contiguous v2 migration
suffix correctly ends at the role-input head. The next state-machine call
emitted `contract repair record is invalid`. Factory issue #174 preserves the
full immutable evidence.
Repeated Linear `unsafe_state` admission noise observed during the live call is
tracked separately in Factory issue #175; it did not alter the active claim.
Root cause: migrated repair validation required the migration suffix to end at
the current branch head. That invariant is correct before execution but
becomes false when the authorized repair role succeeds and creates its output
commit. The terminal passport advances to the output head, while migration
history correctly remains an immutable record ending at the role input. A
pre-resume audit after promotion found the same incomplete invariant at the
next unavoidable boundary: migrating that terminal success passport to the
new Factory appends a second lineage segment whose source is the role-output
head. Treating both segments as one contiguous chain would reject valid
evidence after activation even though no role, receipt, or charge changed.
Smallest repair: when exactly one migrated repair success exists, validate the
migration suffix against that success's role-input head. Separately require
the consumed FIX receipt, role and branch, exact Factory release, authenticated
current passport, descendant output head, terminal manifest, output digest,
completed-role evidence, receipt-bound input-passport digest, canonical
nonnegative charge, successful accounting record, and original failed charge
to match uniquely. If the terminal passport is subsequently migrated, accept
exactly one separate contiguous v2 suffix beginning at its strict descendant
output head and ending at the current Factory/head/base/route. Bind the gap by
requiring the pre-success endpoint and post-success source to retain the same
Factory, protected-base, and route identities, and bind the final edge to the
current passport's parent file and semantic digests. The ordinary pre-run
migration rule and dependency-conflict repair path are unchanged.
Validation: the focused state-machine regression now proves a valid migrated
repair can advance its head, cross one successor Factory migration, and retire
the signed repair without replay. It also proves that changing the
input-passport link, substituting the role-input head for the authenticated
post-success source, or removing/malforming the matching success charge remains
fail closed. The focused state-machine suite must pass before publication;
protected GitHub CI owns the complete regression. Live closure requires T-094
to cross the installed successor and continue to its next deterministic stage
without another Planner call.

## FI-20260731-127 — Separate regressions missed the composite historical state

Status: Shared-path repair and successor-only submission recovery implemented; sealed launcher smoke passed; ordered three-ticket qualification, protected CI, and live closure pending
Priority: P0
Area: controller recovery and aggregate historical-state verification
Owner: Factory
First seen: Nysa generation 50 T-094 dependency refresh
Impact: 27 distinct Factory releases were activated across generations 24–50
in 43 hours, 27 minutes, 3 seconds while 47 Factory PRs merged across 46 hours,
10 minutes, 43 seconds. Narrow protected checks generally passed, but resuming
T-093, T-094, or T-100 against accumulated passports, repairs, receipts,
routes, charges, and protected-base history exposed the next untested
relationship. T-094 reached generation 50 with 13 conservative charges, 6
successful-role records, 41 zero-cost pre-GO terminals, 31 passport migration
edges, 27 route revisions, and 24 release-history entries, yet remained
provider-idle and unpublished. T-093 and T-100 remained safely waiting and
T-096 remained dormant.
Evidence: generation 50's sole resolver issued the exact provider-free
`REFUSE dependency refresh required` receipt because T-092 was merged but its
protected-main head was not an ancestor of T-094's ticket branch. Before
ordinary reconciliation could consume that branch, controller reclaim called
the state machine a second time from
`restore_recorded_contract_repair`, treated the valid dependency-refresh
refusal as an unsafe repair stage, and emitted repeated recovery failures. No
provider call, successful-role replay, publication lease, or additional charge
occurred. The controller and installed launcher paths contain no Nysa Agents
plugin edge for this lifecycle.
Root cause: recorded-repair recovery was a second transition resolver. It
authenticated a passport, called `next-stage`, interpreted its result, and
then ordinary reconciliation called `next-stage` again. This violated the
one-resolution-per-transition rule and inverted the normative priority of a
provider-free dependency refresh over a still-valid repair. The broader
program tested FI-106 through FI-126 relationships separately instead of
reducing their historical aggregate before each release, so the real tickets
became the integration harness.
Smallest repair: recorded-repair recovery authenticates only the current
remote passport and the exact retained blocker receipt/role, reacquires its
lease, clears stale claim-cache fields, and emits a prepared event. It does
not inspect, accept, or reject a stage. Ordinary reconciliation remains the
single transition resolver and consumes the resulting dependency-refresh,
repair, wait, publication, or completion branch. Invalid signed repair
evidence therefore still fails closed inside the state machine before a
provider call.
Validation: one sanitized immutable scenario replays the T-094-shaped 13/6/41
accounting checksum, 27-revision route, 31-edge passport, two successor
migrations, controller restarts, cell and lease rotation, exact repair-owner
hand-offs, dependency refresh before repair, maintenance pause, sibling
progress, Reviewer/Narrator publication, and terminal reduction. Its critical
boundary invokes the real controller and state-machine helpers and asserts one
state-machine call per transition attempt. A one-field-at-a-time matrix covers
HMAC, passport parent, migration edge/gap, head, tree, route, base, receipt,
charge, repair directive, safe-test conflict blob/path/mode, lease, approval
head, and publication queue; every mutation fails before a provider call and
leaves the sibling unchanged. Protected GitHub CI, sealed install,
certification, activation, rollback, recutover, and live T-094 → T-100 → T-093
closure remain required before this item can close.

Post-activation evidence extended the same aggregate root. T-094 crossed the
repaired dependency-refresh and exact Test-author stage once, but run
`1785516585-60814` exited 125 after durable GO and before the submission marker.
Its immutable manifest records `task_submitted=0`, empty output, zero progress,
and one exact $10 conservative charge; the provider process, reservation, and
lease all drained. The current release correctly retained a blocked claim, but
had no authenticated way to distinguish a safe successor retry from blind
same-release replay.

The shared recovery now accepts only that complete terminal shape after an
actual Factory release change and only when the signed passport contains its
charge exactly once. The same release, a submitted task, different exit or
reason, progress, invalid output digest, unequal reservation, missing export,
or repeated failure under the successor remains blocked. The runner emits a
typed reason and retains the bounded diagnostic-output digest for new
occurrences, while only the exact legacy blank-reason/empty-output shape remains
readable. The composite replay adds this conservative charge before successor
B, refuses same-release replay, preserves sibling state, and resumes Reviewer
only after the successor migration. The sealed launcher smoke proved exact
production code and contract selection but did not authorize or execute the
Nysa cohort. The attempted promotion stopped before installation, product
certification, or activation, and its automatic full-main run was cancelled.
The shared qualification contract now admits an acyclic T-094, T-100, T-093
cohort at capacity three, requires protected external dependencies, and reduces
that exact restart, relocation, protected publication, and Done lifecycle
without making a four-ticket concurrency claim. Full Factory CI and every
production release transaction remain downstream of a green sealed reducer;
the separate development product lane remains limited to shared
runner/provider and application compatibility. Production-successor
qualification reuses the canonical drained controller passports, route state,
provider accounting, and reconciliation lock instead of copying or re-signing
them. Its reducer requires the source-to-candidate migration, validates all
historical charge identities, and caps/reports only the candidate's additional
spend. This closes the state-split that would otherwise make a fresh sandbox
green while leaving the real T-094/T-100/T-093 passports unproved.

The first sealed takeover occurrence exposed one more relationship in this
same aggregate root before promotion. T-094's authenticated passport retained
seven successful roles, including two Planner runs, one Spec-linter run, and
four Test-author runs. After the candidate Test-author repair succeeded, the
current product runtime ledger contained only that newest run. `next-stage`
therefore counted Planner as zero, returned ordinary `RUN planner` beneath the
coarse Building state, and planner preflight correctly refused. The controller
parked T-094; T-100 and T-093 stayed dependency-waiting; no sibling provider
or publication authority was admitted. The shared resolver now validates the
current passport HMAC plus ticket/branch/head/route/release identity and gives
the sole `next-stage` call its ordered completed-role sequence through an
owner-only ephemeral capability. New tickets without passports retain the
ordinary ledger path. This preserves historical successful-role identity for
scheduling without rewriting the accounting ledger or relabelling its Factory
SHA. A successor candidate, a replay of this exact parked boundary, the full
ordered cohort, protected CI, and final release transactions remain required.

Re-sealing that successor exposed the paired qualification-side assumption:
the takeover preparer and final reducer required each current passport, or one
direct migration edge, to name the still-installed production source. The
first candidate had already authenticated a production-to-candidate edge, so
the passports correctly named that intermediate release while production was
unchanged. The shared successor model now validates ordered, unique release
history and one contiguous v2 cross-release suffix from the manifest's
installed source through intermediate qualification candidates. Preparation
accepts the authenticated current endpoint; the reducer requires that same
suffix to terminate at the frozen final candidate and continues to count only
that final candidate's additional spend. Focused environment and reducer
tests cover the two-edge path and reject a disconnected edge. A new sealed
root and the ordered live cohort remain required.

The next sealed occurrence reached the correctly selected T-094 Builder, whose
Cursor route returned HTTP 503 after GO with zero turns and a conservative $10
terminal charge. The qualification-only automatic fallback failed before a
handoff because it read the qualification control worktree's stale ignored
`runtime-ledger.csv`; ticket execution had materialized accounting through the
ticket worktree's canonical Git root. The shared fallback evidence boundary
now invokes the existing ledger reducer over tracked durable rows plus
authoritative run manifests and hashes that exact effective CSV. A focused
regression leaves a deliberately stale runtime view in place and still accepts
only the unique latest terminal manifest; model-fallback, ledger-view, and
controller suites pass. Re-sealing, the automatic same-role fallback, the
ordered cohort, protected CI, and final release transactions remain required.

That live automatic fallback next rejected the product's ordinary ignored pnpm
workspace symlink at `node_modules/@nysa/web`. The snapshot already derived
commit candidates from the tree, index, and Git's non-ignored untracked set,
but its separate hazard walk traversed ignored dependency/build state too. The
shared handoff now asks sanitized Git for exact ignored paths and prunes only
those paths from the hazard walk. Candidate reads still use no-follow
descriptors, and focused tests retain rejection of tracked or non-ignored
symlinks, hardlinks, FIFOs, nested repositories, and submodules while proving
an ignored dependency symlink is absent from the snapshot. A successor seal
and live fallback replay remain required.

The next T-094 occurrence reached the exact authenticated `FIX planner`
transition but stopped before GO. All role-specific checks passed; preflight
rejected the clean qualification control branch solely because it was named
`local/qualification-6fe56bb` rather than `main`. That branch carries the
required local qualification authority and cannot also equal protected product
main, so the production branch assertion made the sealed lane behaviorally
incomplete. The launcher now supplies the exact product tree already validated
against its owner-only qualification activation record, and preflight accepts
only a clean checkout matching that tree. Production and every unbound call
still fetch and require clean current `main`. Focused tests prove the unbound
branch fails, the sealed exact tree passes, and a forged tree fails. A successor
seal, the pending Planner repair, ordered cohort, protected CI, and final release
transactions remain required.

That successor advanced T-094 through the repaired Planner, Test-author, and
Builder stages, proving real state-machine progress, but Builder run
`1785537237-15335` replaced the authenticated input ancestry after GO. Its
terminal bound receipt
`6a5eadff77082ca4b3a8dd4b62fcc64f1ab22fde9cf6ec44c8f4786a52a074e4`,
input `be0d9d10e6b299f86cd7b93e8762f7ce9a8d3cb6`, Factory
`fef0bea4f1d933df689b96aac7a31cfb918ae232`, one submitted task, and one full
$10 conservative charge. Output
`741a55a400795a93e8d1dd275104779ca45c16e6` had unrelated parent
`459b9cbcb24443a14dcbaa294ee3343fb61a5031`; the trusted non-force push refused
and the remote stayed at the input. The runner had no typed pre-push ancestry
check, left the cell on the output, and passport export then failed because the
remote/input lineage no longer matched the local cell. The output is preserved
at `refs/factory/failed-role/T-094/1785537237-15335`, and the cell was restored
exactly to the input before any further controller call.

The shared runner now rejects non-Test-author ancestry loss before publication,
creates that collision-checked diagnostic ref, restores the exact input with
compare-and-swap ref updates plus `git restore`, and revalidates the clean cell
and unchanged remote. The typed `role_exit_history_rewritten` terminal can be
reclaimed only by a successor Factory after the exact signed passport proves
one matching charge, no matching completion, and the terminal's input head.
Same-release replay and every malformed topology stay blocked. Builder's role
contract now explicitly forbids rebases, resets, amends, and other input-history
rewrites; the existing protected Test-author rewrite lifecycle is untouched.
Focused runner and controller suites prove quarantine/restoration, conservative
accounting, same-release refusal, successor-only recovery, and unchanged
Test-author behavior. A new sealed successor and live T-094 Builder retry remain
required before the ordered cohort can continue.

The next sealed successor migrated the exact revision-33 routes and passports
for T-094, T-100, and T-093 to Factory
`bb05660edeafb19ea67e0ea56afd60de53fdba02` without a provider call. T-094 then
stopped at ordinary scheduling with `ticket already has a dispatcher lease`.
Upgrade recovery had correctly reacquired one fresh lease after the old lease
was released, but saved it beside the prior claim's `lease_released=true`
marker. The next `ensure_lease` treated the fresh lease as released, attempted
a second claim, and the lease helper correctly refused. The controller released
and parked T-094; T-100 and T-093 returned to dependency waiting, and no new run
or charge was created. Recovery now clears the stale released marker whenever
the successor renews or reacquires the exact lease. A focused controller
regression starts with that historical flag, crosses the two-phase route
migration, and proves ordinary scheduling receives one reusable lease. Another
sealed successor remains required before the Builder retry.

That same `bb05660` pass exposed a second ordering gap: after migrating T-094's
passport, `recover_upgraded_claims` cleared the failed Builder receipt because
it retained only contract-blocked and successful terminals. The later typed
recovery therefore had no claim identity to consume, even though immutable run
`1785537237-15335` and its attempt-terminal event still bound the exact receipt
and role. This created no provider call or charge, and the branch, passport,
diagnostic ref, and remote remained intact. Upgrade recovery now preserves every
receipt that has one terminal manifest. Existing success export and typed
failure recovery consume recognized shapes; an unknown shape remains blocked,
and only missing terminal evidence can clear stale claim cache. A focused test
proves a migrated history-rewrite failure keeps its receipt and role for the
successor recovery pass. The one legacy claim whose cache was cleared is
restored from its exact immutable manifest before the next sealed successor.

## FI-20260731-128 — Qualification restart proof was not candidate-scoped

Status: Shared-path repair implemented and focused controller suite green;
successor seal, live restart proof, ordered cohort, protected CI, and final
release transactions pending
Priority: P0
Area: sealed qualification restart recovery
Owner: Factory
First seen: Nysa successor qualification candidate
`134976c10030c20de20a08bbe34c656112d13751`
Impact: after all three revision-35 routes and passports named the candidate,
the controller found `qualification-restart-boundary.json` and
`qualification-recovered.json` from Factory `5f32510` in canonical takeover
state. It treated existence alone as current proof and could have entered the
cohort without demonstrating the required restart under `134976c`. The
controller was stopped before any provider submission; the three branches,
passports, charges, and production installation remained unchanged.
Evidence: both legacy markers authenticated Factory `5f32510` and the exact
three tickets while the sealed activation and qualification manifest bound
Factory `134976c`. No candidate-scoped restart or recovered marker existed.
Root cause: qualification markers had fixed filenames and reads checked only
filesystem existence. Their contents were never compared with the active
release or authorized ticket set.
Smallest repair: restart-boundary and recovered markers now include the exact
candidate SHA in their filename and validate an exact value containing the
current Factory SHA, event schema, and sorted qualification ticket set. Legacy
markers are ignored; a malformed current-candidate marker fails closed.
Validation: the controller regression preloads both valid-looking legacy
markers from another release, proves the current candidate still returns
`restart_required`, and proves the second invocation writes and consumes only
the current candidate's exact boundary and recovery markers. All 55 controller
tests pass. Live successor replay and final qualification reduction remain
required before closure.

## FI-20260731-129 — Clean remote validation skipped failed-charge export

Status: Shared-path repair implemented and focused controller suite green;
successor seal, live charge export, ordered cohort, protected CI, and final
release transactions pending
Priority: P0
Area: failed-role recovery and passport accounting
Owner: Factory
First seen: Nysa successor qualification candidate
`134976c10030c20de20a08bbe34c656112d13751`
Impact: T-094's exact legacy Builder push failure emitted
`push_failure_recovered` and cleared its claim receipt, but the authenticated
passport remained at 22 charges and did not contain run
`1785537237-15335`'s full conservative charge. The controller was stopped
before a provider submission, so the branch and cumulative provider ledger did
not advance, but ordinary scheduling could have resumed without carrying the
failed attempt in the ticket passport.
Evidence: the immutable terminal retained receipt
`6a5eadff77082ca4b3a8dd4b62fcc64f1ab22fde9cf6ec44c8f4786a52a074e4`;
the post-recovery claim had empty role and receipt, while the passport's last
charge was the earlier Test-author run and its charge count remained 22.
Root cause: the legacy push-failure path invoked preserving passport migration
only when remote-passport validation raised an error. History quarantine had
restored the exact input and remote topology, so validation succeeded even
though it proves head identity, not failed-charge inclusion. The same gap
applied to a pre-submission interruption.
Smallest repair: before either recovery can clear its claim, the controller
checks the exact terminal export. If absent, it runs the existing receipt-bound
passport export and validates the result. An authorized stale head first uses
the existing preserving migration and then retries that export. Remote passport
validation and fresh lease acquisition remain separate downstream gates.
Validation: the focused recovery regression starts with a valid remote
passport that lacks each failed charge, proves one receipt-bound export occurs
for both push failure and pre-submission interruption, and permits claim
clearance only after the terminal export check changes to true. The authorized
rewrite regression proves failed export, stale-head migration, and successful
bound re-export in that order. All 55 controller tests pass. Live restoration
from the immutable T-094 manifest and one successor replay remain required.

## FI-20260731-130 — Successor runtime budget counted earlier candidates

Status: Shared-path repair implemented and focused budget/controller suites
green; successor seal, live Reviewer continuation, ordered cohort, protected
CI, and final release transactions pending
Priority: P0
Area: successor qualification runtime budget and controller recovery
Owner: Factory
First seen: Nysa successor qualification candidate
`4101a4a3f86097e5dca7ebd58444c2327a0cf091`
Impact: T-094 successfully completed and pushed Builder run
`1785549789-96973`, advanced to `4e1dc4c1151831ccee5687b47c109711edbe6a16`,
and exported its 24th charge plus 10th completed role. Before Reviewer, runtime
budget admission returned `AWAIT_BUDGET` at exactly 100,000,000/100,000,000
micro-USD and parked the ticket. Only 10,000,000 micro-USD belonged to the
frozen candidate; the other 90,000,000 came from prior qualification
candidates. T-100 and T-093 remained dependency-waiting, all leases drained,
and no Reviewer provider call started.
Evidence: the final qualification reducer already computes candidate spend by
including only charge records whose `factory_sha` equals the manifest's frozen
candidate. `budget-stage.py` instead summed every immutable run manifest for
the ticket, so iterative successor qualification used cumulative candidate
history against a candidate-only cap.
Root cause: successor preparation and reduction defined a candidate-scoped
budget, but runtime stage resolution reused ordinary production's lifetime
ticket sum. A resulting budget claim also reopened only when the envelope file
changed, even though a new frozen successor SHA changes the authenticated
budget basis without changing that envelope.
Smallest repair: the runtime budget helper accepts the launcher-authenticated
Factory SHA, strictly validates the exact successor qualification manifest,
uses its fixed $100 cap, and sums only current-candidate charges. Ordinary and
fresh qualification behavior retains the existing envelope/override reducer.
The controller may reopen a `budget` claim only across an authenticated
successor release migration, removes the obsolete envelope digest after that
migration, and never reopens a same-release or ordinary production budget wait.
Validation: the focused budget test creates $100 of historical charges plus
$10 under the current candidate, proves admission remains available, reaches
the stop only when current-candidate charges equal $100, and rejects a Factory
SHA mismatch. The controller regression proves cross-release successor reopen,
same-release refusal, and ordinary-production refusal. The budget suite's 3
tests and controller suite's 56 tests pass. Live Reviewer continuation remains
required.

## FI-20260731-131 — Test immutability ignored frozen-contract epochs

Status: Shared-path repair implemented and focused gate/reorder suite green;
successor seal, instantiated Nysa gate proof, ordered cohort, protected CI, and
final release transactions pending
Priority: P0
Area: product test immutability and authenticated repair history
Owner: Factory
First seen: Nysa successor qualification candidate
`5953b185dbe11d38d2c9828d4af26d552213532a`
Impact: T-094 preserved and pushed its v4 Builder result, entered Review, and
completed Reviewer run `1785553142-55592`. Required `ci` and
`test-immutability` checks failed because v3 implementation commit `7b66d7ff`
precedes v4 Test-author commit `be0d9d10`. Reviewer correctly requested a
Builder fix; Builder run `1785553918-73363` proved that an append-only role
cannot reorder authenticated input history and recorded the blocker. A second
Reviewer admission was interrupted before submission and terminalized with
zero progress, preventing a duplicate paid review loop. T-100 and T-093
remained dependency-waiting.
Evidence: contract v4 was frozen append-only in Planner commit `a44d58ec`,
then its protected tests landed in `be0d9d10`, followed by the exact v4
implementation correction `4e1dc4c1`. The existing gate carried one global
`SEEN_IMPL` bit across superseded contracts. The reorder helper could make the
final tree identical only by changing the exact input heads of later Planner,
Test-author, Builder, and Reviewer evidence, so history rewriting was rejected.
Root cause: tests-first ownership had no mechanical contract-epoch boundary.
A legitimate newer frozen contract reopened Test-author semantically, while
the gate continued treating every earlier implementation commit as current.
Smallest repair: a commit that changes exactly one canonical ticket file, adds
exactly one higher numbered `Frozen contract` heading, and adds its matching
`Freeze result — PASS` begins a new tests-first epoch. Removed, repeated,
older, mixed, prose-only, or incomplete markers do not reset the gate. The
reorder helper shares the same classifier and refuses a required rewrite when
merge history remains.
Validation: the focused suite passes nine scenarios including valid v1→v2
epoch reopening; incomplete, repeated, removed, mixed, and noncanonical-marker
refusals; same-contract reordering; conflict abort; dirty-tree refusal; and
exact bookkeeping exemptions. Gate and helper agree on every marker case. The
updated gate passes the real synchronized T-094, T-100, and T-093 histories;
the T-094 helper reports `NOTHING-TO-DO` without changing its head or tree.

## FI-20260731-132 — Qualification required protected product activation first

Status: Shared-path repair implemented and focused environment suite green;
successor seal, ordered cohort, protected CI, and final release transactions
pending
Priority: P0
Area: production-successor qualification admission
Owner: Factory
First seen: Nysa qualification after protected control PR #305
Impact: protected `main` advanced with the shared test-immutability policy while
the active production checkout correctly remained unchanged. Candidate `60a2eff`
could not prepare a successor root because takeover admission required both
checkouts and the activation to equal current protected main. No controller,
provider, installation, certification, promotion, or activation action ran.
Root cause: the preparer conflated two trust facts: the product tree currently
authenticated by production activation and the newer protected base consumed
by the qualification control worktree. Satisfying it would have required the
preliminary product activation that qualification exists to precede.
Smallest repair: require the clean activated source checkout's tree to equal the
activation record, require current protected main to contain that source commit,
and continue requiring the qualification worktree to descend from current
protected main with only its exact control-file allowlist. Divergent protected
history and active-tree drift fail closed.
Validation: the focused takeover regression now advances protected policy after
creating the active source worktree, successfully prepares qualification, then
separately rejects an active-tree mismatch and a protected ref that does not
contain the active commit. The four-test environment suite is green. A new
sealed root and live ordered cohort remain required.

## FI-20260731-133 — Builder broad verification blocked its own fallback

Status: Closed — focused validation and live successor recovery green; later
publication evidence defect tracked separately in FI-20260801-134
Priority: P0
Area: qualification role verification and provider fallback recovery
Owner: Factory
First seen: Nysa qualification candidate
`202b6c07d0ae8393450e09824acf2b3767b22122`
Impact: T-094 Builder run `1785562372-25208` produced 229 structured progress
events and useful application changes, but exhausted the exact ninety-minute
hard boundary while running the root workspace test command. Its terminal kept
the full conservative $10 charge and left permitted partial changes in the
ticket cell. The controller then tried the clean-worktree passport export
before its automatic fallback, so fallback did not preserve the changes and
the claim remained blocked. T-100 and T-093 stayed dependency-waiting; no
publication, certification, promotion, or production activation ran.
Evidence: the immutable terminal records exit 124, `provider_failed`,
`hard_timeout`, four turns, and receipt
`c70a4c53c818940a5722fde7f8c244213bd564cdbee90251e55ebdbcd4801aa6`.
The cell contains only modified product implementation and ticket-log paths,
while the controller emitted `factory-launch: ticket worktree must be clean`
before `fallback-auto` could run. A direct retry through that sealed helper
then failed without mutation because the Builder boundary rejected its own
required `factory/tickets/T-094.md` root-cause log. The corrected successor
accepted that boundary, then refused because its first-attempt guard counted
all nine historical T-094 Builder runs rather than the sole submitted Builder
attempt under candidate `202b6c07`.
After candidate-scoped counting passed, the sealed successor still could not
authorize the old failure because the helper required its local successor
manifest SHA to equal the older journal Kit-SHA before route migration.
Once the handoff and route migration succeeded, upgrade recovery retained the
failed receipt as `blocked`; blocked claims are not runnable, and fallback
recovery recognized only a journal whose final revision was the fallback rather
than the legitimate successor migration that now followed it.
Root cause: Builder v5 required “all tests green locally,” which encouraged a
repository-wide suite despite qualification's ticket-scoped iteration policy.
Independently, `finish_pending_run` exported every terminal passport before
classifying the one failure whose trusted fallback explicitly accepts and
commits a dirty permitted worktree.
Smallest repair: Builder v6 forbids root, workspace-wide, and full local suites
and requires the narrowest existing acceptance and static checks. For the
first qualification Cursor provider failure only, the controller now invokes
the existing idempotent fallback before passport export and then migrates the
failed charge onto the clean fallback head. The handoff boundary permits the
current ticket log as the sole Builder exception to `factory/**`; sibling
tickets, tests, route journals, and other controls remain forbidden. All other
terminal ordering is unchanged. The automatic fallback's one-attempt guard now
counts only submitted GO attempts for the exact failed candidate; historical
attempts remain immutable accounting evidence but do not consume that
candidate's retry boundary. For sealed successor takeover only, the local
manifest binds the executing release SHA while the failure and journal retain
their exact older SHA; ordinary qualification still requires one shared SHA.
This permits a clean handoff commit before the existing route migration and
does not relax head, route, latest-run, ticket, or successor-manifest checks.
Recovery validates the full journal, accepts only a release-migration suffix,
and requires one unique ancestor commit with the exact fallback trailer. A
successor qualification Cursor terminal is reopened as running with its receipt
intact so the ordinary finish path exports accounting and clears it.
Validation: all 56 focused controller tests pass, including the assertion that
no eager passport export occurs and that `fallback-auto` precedes preserving
migration. The Builder contract check proves v6 contains the full-suite ban and
no longer contains the v5 all-tests requirement. Changed-scope CI passed its
targeted `ci-scope`, immutability, and artifact-policy selection while deferring
broad suites to required protected CI; repository and secret checks are green.
The focused fallback, handoff, approval, and model-control suites pass all 29
tests, including current-ticket acceptance, sibling-ticket rejection, and a
historical predecessor attempt that does not consume the current candidate's
fallback. The sealed-local-manifest regression also proves the executing
successor SHA may differ from the exact failed journal SHA while an unrelated
protected manifest is ignored. Migrated-fallback suffix and upgraded-claim
reopen regressions pass in all 12 fallback tests and all 57 controller tests;
the focused handoff, approval, and model-control suites remain green. Live
candidate `c23fa933` reopened the old failed receipt, exported its charge once,
and resumed Builder run `1785572530-77467` under v6. Builder finished in about
six minutes with the focused web acceptance suite at 7/7 and scoped TypeScript
green; Reviewer then approved the exact branch after 155 observable progress
events. No root Builder suite, duplicate provider attempt, publication,
certification, promotion, or production activation occurred.

## FI-20260801-134 — Publication read the sealed checkout's stale runtime ledger

Status: Closed by focused validation and live PR-boundary recovery; follow-on
lane-isolation defect recorded as FI-20260801-135
Priority: P0
Area: qualification publication evidence and canonical runtime accounting
Owner: Factory
First seen: Nysa qualification candidate
`c23fa933ea7fedd8dca5adc97238046502316a0f`
Impact: T-094 completed Builder and Reviewer successfully, Reviewer returned an
authenticated APPROVE verdict, both exact branch commits were pushed, and the
provider ledger drained. The next `ticket-pr` boundary nevertheless blocked
the claim with `successful reviewer run evidence is missing`. T-100 and T-093
remained dependency-waiting; no PR, certification, promotion, or production
activation ran.
Evidence: sealed run manifest `1785573332-1737.meta` records Reviewer exit 0,
role exit `ok`, 155 progress events, and conservative accounting. The matching
ledger row exists in the canonical Nysa product checkout. The qualification
control checkout's ignored `factory/runtime-ledger.csv` predates the run, so
`ticket-pr.py` found no successful row even though role execution had written
the authoritative canonical runtime view. `ticket-attest.py` independently
used the same incorrect control-checkout default and would have failed after
Narrator.
Root cause: qualification correctly separates sealed controller run manifests
from linked canonical ticket worktrees, but the two publication helpers assumed
both evidence types lived under `FACTORY_ROOT`. Role execution already resolves
ignored ledgers through the worktree's Git common directory.
Smallest repair: one shared Python runtime-path helper applies that established
canonical-worktree rule. Ticket PR validation and ticket attestation keep
reading manifests from the sealed product, but read effective ledger rows from
the claimed worktree's canonical main checkout unless an explicit trusted
ledger override is present. Their existing exact branch, origin, lease,
manifest, cost-basis, lineage, and GitHub checks remain unchanged.
Validation: the ticket PR suite passes 12/12, including a split control/runtime
checkout whose stale control ledger cannot hide the canonical successful row.
The attestation run passed all 53 existing cases; its new split-checkout case
reached the correct Reviewer and Narrator rows but exposed an order-sensitive
test expectation. That assertion now compares the role set, and the corrected
regression passes independently. Sealed successor
`178ab9016c0f68fd8fe70f60b491060cb7b2d1ff` then reattached T-094, reused PR
#304, and crossed the Reviewer-bound ticket-PR boundary with every required
check green before launching Narrator. That live canary closes this defect; the
later overwrite of the shared canonical ignored ledger is a distinct
multi-lane isolation defect below.

## FI-20260801-135 — Shared ignored ledger and unbound Narrator inputs regressed publication

Status: Follow-on provider-scope repair implemented and focused qualification
regression green; sealed successor canary pending
Priority: P0
Area: qualification accounting isolation and Narrator publication evidence
Owner: Factory
First seen: Nysa qualification candidate
`178ab9016c0f68fd8fe70f60b491060cb7b2d1ff`
Impact: T-094 crossed the repaired ticket-PR boundary and launched Narrator,
but Narrator lacked the exact PR, preview, and accounting inputs promised by
its role contract. It ran root `npm test` for 494 seconds, encountered 33
unrelated web-test timeouts under broad-suite load, and committed an explicitly
non-approvable bundle. After that commit, the controller's next ticket-PR
validation failed with `successful reviewer run evidence is missing` and parked
T-094; T-100 and T-093 remained dependency-waiting. No certification,
promotion, or production activation ran.
Evidence: Narrator run `1785577279-77281` completed exit 0 with 115 progress
events and committed `b9171a37`. Its terminal transcript records root
`npm test`; the command's web workspace ended 18 files failed / 20 passed and
33 tests failed / 115 passed. PR #304 had all required checks green and a
current `railway-app` comment naming successful API and web preview endpoints,
but the bundle stated that no PR, preview, or runtime ledger existed. The sealed
Reviewer manifest `1785573332-1737.meta` remained intact while the canonical
ignored runtime ledger no longer contained its row; production sync had reduced
that same file from a different run root.
Root cause: qualification and production used identical accounting code but
shared one mutable ignored ledger while owning different manifest roots, so the
last reducer writer could erase the other lane's effective rows. Separately,
the controller supplied Narrator only a generic task, while the sanitized
provider process intentionally inherited neither GitHub credentials nor Factory
control paths; nothing bound the promised PR, preview, or accounting inputs, and
the role contract did not explicitly prohibit broad verification reruns.
Smallest repair: the qualification launcher supplies trusted lane-local runtime
and durable ledger overrides to every unchanged helper, and stage selection
refreshes that view from the lane's own manifest root before consuming it.
`ticket-pr` extracts
only validated `railway-app` Web endpoints on `*.up.railway.app`, waits while
none are reported, and returns them with the exact PR/head/check result. The
controller binds those values into Narrator's task; the runner adds the
post-reservation accounting snapshot. Narrator v8 forbids tests, builds,
repository checks, secret scans, and broad suites. The sequencer treats an
explicit `NOT APPROVABLE:` bundle as its one bounded retry, and ticket
attestation refuses to advance one.
Validation: ticket PR passes 12/12, controller passes 58/58, and ticket
attestation passes 55/55. The full factory-script suite passes, including the
lane-local ledger refresh, explicit non-approvable retry, planning, repair, and
contract-1.8 refresh cases. The complete isolated Hermes contract suite also
passes, including serialized execution, ticket PR, project-ledger closeout, and
the final launcher schema audit. The sealed live successor remains pending.

Follow-on occurrence: sealed candidate `b2c1b722` recovered all three
passports and reached T-094's bounded Narrator retry, but provider admission
refused before GO with exact denial `budget_micro_usd/ticket`. Its zero-cost
terminal was run `1785590302-77633`; the controller parked T-094 and did not
replay Builder, Reviewer, or any sibling role. The state-machine allowance
correctly counted only candidate `b2c1b722`, while the shared coordinator used
generic product ID `product_` and counted $100 of same-day
predecessor-candidate reservations. The smallest shared-path correction binds
sealed qualification provider product/ticket scope to project plus frozen
candidate SHA. Same-candidate roots still share a cap and the machine-day scope
remains global. The focused qualification environment suite passes 4/4; shell
syntax and diff-integrity checks are green.

Follow-on occurrence: candidate `ba3ff2d3` migrated T-094, T-100, and T-093
to exact successor routes without a provider call, but restart selection found
only two of the required three runnable claims. T-094 retained the predecessor
candidate's valid zero-cost `launch_void`; upgrade recovery classified every
non-Cursor terminal as blocked before the existing prior-release launch-void
reducer could clear it. The smallest correction makes only a valid
prior-release launch-void receipt runnable during migration, leaving
same-release or malformed receipts blocked. Focused controller regression
covers both sides of that boundary, and a three-ticket restart regression
proves the preserved receipt contributes to the complete restart cohort. Edge
coverage rejects a same-release receipt, invalid release SHA, non-abandoned
phase, prior GO or submission, nonzero cost, and a non-launch-void cost basis;
it also proves the successful prior-release clear is idempotent. Live successor
proof remains pending.

Second follow-on occurrence: tested candidate `029b09f` passed the 3/3 live
restart boundary, migrated every passport, cleared T-094's predecessor
launch-void exactly once, and kept T-100/T-093 in authenticated dependency
wait. Its first Narrator launch then refused before manifest creation, GO,
submission, or charge with `qualification provider product identity is
invalid`. Takeover keeps its linked product outside the sealed root, while the
new identity check incorrectly required the derived nested-lane product path.
Because the launcher exited before writing a terminal manifest, the controller
also cleared the fresh receipt and resolved Narrator again until the operator
stopped it. The smallest repair validates the launcher-supplied sealed
qualification root directly and adds a generic fail-closed controller guard:
any completed role subprocess without terminal evidence remains blocked with
its receipt intact and lease released. Focused coverage includes successful and
failed subprocess exit codes, so absence of terminal evidence can never imply
progress.

Third follow-on occurrence: sealed candidate `d8e768e8` admitted T-094 under
the exact candidate-scoped provider identity, submitted one Narrator attempt,
and produced terminal run `1785596620-61582` with 147 structured progress
events. The committed bundle correctly began `NOT APPROVABLE:` because the PR
web preview called the production API and the PR API did not allow the PR web
origin. With the one bounded Narrator retry exhausted, `next-stage.sh` emitted
`ESCALATE evidence bundle remained invalid after one Narrator retry`; the typed
Python resolver rejected that documented non-role action as unsupported, so
the controller safely blocked T-094 but reported a controller error. The
smallest repair admits `ESCALATE` as a typed non-role transition and makes the
controller park it once, release the ticket lease, and record the exact detail.
Focused state-resolver and controller regressions cover this terminal path. The
Narrator commit and screenshots remain immutable failure evidence; no Narrator
rerun is allowed against the unchanged preview. The separate product preview
pairing defect routes directly to Builder before a new deployed-head proof;
only malformed Narrator output consumes the bounded Narrator correction.

Fourth follow-on occurrence: candidate `d7a420f4` correctly resolved T-094's
preserved explicit `NOT APPROVABLE:` bundle to `FIX builder`, then spent more
than 300 seconds reducing authenticated history and materializing the ticket
state. The nested `ticket-state.sh` completed and pushed the exact
`Review -> Building` transition, but the controller's generic 300-second outer
timeout killed the state-machine parent before passport migration and receipt
issuance. The controller failed closed, released the lease, and launched no
provider, while the ticket branch remained clean at `Building`. The smallest
repair removes only this redundant aggregate timeout; resolver, ticket-state,
passport, and Git subprocess bounds remain unchanged. Focused regressions bind
the controller call to those inner bounds and prove replay from the already
committed `Building` state issues `FIX builder` without repeating the state
transition or changing the preserved Narrator evidence.
A 48-case mocked role/state matrix now enumerates all six roles under both
`RUN` and `FIX` from Ready, Planning, Building, and Review. It verifies every
exact multi-hop, same-state, repair, and forbidden-backward edge in seconds
before live role execution.

The broader focused fault pass also exposed a test-harness boundary: the real
oversized-output terminalization regression completes in about 24 seconds when
isolated but exceeded its 30-second caller timeout under parallel suite load.
The production output limit, hashing, conservative accounting, and cleanup are
unchanged. Only the regression harness timeout is raised to 90 seconds so the
same multi-megabyte fail-closed proof remains reliable under bounded parallel
execution.

Static cross-layer review found that ordinary reconciliation trusted the
state-machine's decoded JSON fields independently, while only the maintenance
boundary checked their semantic agreement. A syntactically valid but mutated
schema, ticket, action, detail, receipt, stage, or role could therefore reach a
provider or publication helper. The controller now applies the complete typed
envelope check before any side effect in every mode. A ten-mutation matrix
proves each mismatch blocks and releases the lease before provider or
publication execution; all 64 focused controller tests remain green.

The mocked Linear pass covered 42 cases after adding transient transport
faults. The client previously retried only `429`; a transient `503` deferred
the whole sync cycle, while a malformed `Retry-After` raised during error
handling. It now retries only typed transient statuses (`429`, `500`, `502`,
`503`, `504`) within the existing three-attempt bound and clamps delay to
0–30 seconds, using exponential fallback for missing or malformed headers.
Mocked `503`, malformed, negative, and extreme delay cases pass alongside
illegal-state restoration, full pagination, duplicate refusal, lock
contention, failed writes, approval ingestion, and repeated-cycle idempotence.

## FI-20260801-136 — Full-board Linear lock delayed a ticket transition

Status: Annotated for the next Factory upgrade; current sealed qualification
continues unchanged
Priority: P1
Area: Linear reconciliation concurrency and qualification latency
Owner: Factory
First seen: Nysa sealed qualification candidate
`eda081122a82e628c7f4e663146382155a8f0d29`
Impact: after T-094 Builder completed and the authenticated `Building -> Review`
commit reached both local and remote heads, `ticket-state.sh` waited behind the
canonical `.linear-sync.lock` before it could clear the matching operator
overlay. The controller remained fail-closed and later launched Reviewer, but
the unrelated asynchronous full-board sync added roughly four minutes to the
state transition. No duplicate provider, state rewrite, lost operator update,
certification, promotion, or production activation occurred.
Evidence: launchd job `com.factory.linear-sync.nysa-app` ran the installed
release `4651e544152d65bbb9e18ad6993a633634b79907` while the sealed controller
used candidate `eda0811`. PID `93873` held the canonical lock and PID `97300`
waited in the candidate's exact operator-version compare-and-clear section.
The live sync projected T-094 from Building to Review and completed normally;
the sealed state machine then returned and Reviewer started with receipt
`51d72a0ac421a5bf0f07d2153f3c160f0c66145e718e4f315ad4f6c9184a37f0`.
Finding: global lock serialization preserves map correctness, but the current
sync holds that lock across a slow full-board network cycle. A single ticket's
post-transition compare-and-clear therefore inherits unrelated Linear API and
board traversal latency. Qualification and production intentionally share the
operator overlay, so this is a latency/availability boundary rather than
evidence corruption.
Next-upgrade requirement: first add a deterministic contention regression that
holds a mocked full-board sync at the network boundary while a ticket performs
its exact operator-version compare-and-clear. Then shorten the global critical
section or introduce an equivalent compare-and-swap/per-ticket design that
preserves operator updates, repeated-cycle idempotence, and duplicate refusal.
The sealed `eda0811` release is not modified or restarted for this finding.

## FI-20260801-137 — Stale non-approvable bundle caused a repair/review loop

Status: Focused regression green; live qualification canary pending
Priority: P0
Area: Contract 1.8 Reviewer/Narrator evidence generation
Owner: Factory
First seen: Nysa sealed qualification candidate
`eda081122a82e628c7f4e663146382155a8f0d29`
Impact: T-094 completed a no-change Builder repair and an independent Reviewer
round-5 `APPROVE` after the operator repaired the PR preview pairing. The
sequencer nevertheless read the preserved older Narrator bundle beginning
`NOT APPROVABLE:` and returned `FIX builder` again, committing
`0e4f856df81be8c70cbfffbc638540e4b77dfeac` (`Review -> Building`). The
qualification product entered maintenance as the redundant Builder launch
started; the supported maintenance boundary parked it before any new provider
attempt, role mutation, or charge completed. The earlier Narrator output and
screenshots remain unchanged.
Evidence: successful Narrator run `1785596620-61582` preceded later successful
Builder and Reviewer runs, including Reviewer run `1785606996-44365` at
deployed head `22edcfb1057681a10354bf16978416cf7c733cb5`. Reviewer reconciliation
commit `839db6c02696bbb00878fa8fe527a252ee37412e` recorded `APPROVE`, but
`narrator_bundle_stage` received the lifetime Narrator count and unconditionally
routed any unattested explicit non-approvable bundle to Builder.
Finding: a bundle is evidence for one effective Reviewer generation, not for
the ticket lifetime. Only successful Narrators after the latest non-void
Reviewer may decide that generation. A preserved Narrator at the end of an
unchanged generation must not be replayed; a later effective Reviewer makes
the old bundle and attestation stale and requires a fresh Narrator. A rejected
latest review cannot inherit an older approval.
Smallest repair: reduce the authenticated role sequence (with a ledger fallback
only for older contracts) to the count of Narrators after the latest non-void
Reviewer, and evaluate it lazily only after the planning/build/review gates.
Keep the refresh-generation reducer intact and use the same per-generation
count for valid, explicitly non-approvable, and structurally invalid bundles.
Edge coverage: unchanged explicit failure, repaired and approved generation,
fresh repeated failure, stale valid bundle, stale attestation, authenticated
role evidence, rejected repair review, void duplicate Reviewer, bounded invalid
bundle correction, and early-stage missing-ledger/override behavior. Live proof
must show the repaired T-094 head reaches one new Narrator without another
Builder/Reviewer loop before this entry closes.
The focused state-machine suite passes 27/27, including the nine-case
authenticated generation matrix and the existing 48-case role/state matrix;
shell syntax, Python compilation, and diff-integrity checks are green.

## FI-20260801-138 — Narrator screenshots were misclassified as implementation drift

Status: Ticket-PR live recovery green; downstream attestation follow-up is
FI-20260801-139
Priority: P0
Area: post-review publication lineage
Owner: Factory
First seen: Nysa sealed qualification candidate
`ff75f3301c95457f5a98f5fcf48d8d19e3b2905d`
Impact: the generation-bound reducer correctly launched one new Narrator for
T-094 without replaying Builder or Reviewer. Run `1785613189-45324` completed
successfully at reviewed head `46c3644d97f3ad3ce50f65a475c486841ab1decc`,
committed approvable bundle head `2ef3aa6ebc5bb788b0460caa1b85f951d6703dcd`,
and replaced two obsolete broken-preview captures with six before, after, and
reference PNGs at the two frozen viewports. The commit was clean and pushed,
but the publication reducer blocked with `ticket implementation changed after
the latest successful review` before bundle attestation. T-100 and T-093
remained in dependency wait; no role replay, product certification, promotion,
or production activation occurred.
Finding: `ticket-pr.py` trusted the current ticket bundle Markdown after the
latest Reviewer but omitted the exact raster files that the Narrator contract
requires the same commit to reference. The security boundary therefore treated
valid evidence output as application drift even though the attestation path
binds the exact branch head and bundle blob.
Smallest repair: admit only changed PNGs below
`factory/tickets/<ticket>-evidence/` when the exact path is referenced by the
current bundle, or by the reviewed bundle when the image is deleted. Require an
ordinary `100644` Git blob, exact PNG beginning and terminal chunk, at most 2 MB
per image, and at most 32 changed images. Do not trust sibling-ticket paths,
unreferenced files, symlinks, disguised bytes, other extensions, or any product
path.
Edge coverage: the focused ticket-PR suite passes 19/19. Seven new cases prove
the exact referenced add/delete set succeeds and that unreferenced, fake-PNG,
oversized, excess-count, symlink, and sibling-ticket variants fail before
GitHub access. The repaired validator also accepts the immutable live T-094
`2ef3aa6` history directly.
Live closure reached the repaired ticket-PR boundary without launching
Narrator again. The next independent bundle-attestation validator exposed the
same missing classification and is tracked separately below.

## FI-20260801-139 — Bundle attestation diverged from ticket-PR evidence lineage

Status: Focused regressions green; sealed successor recovery pending
Priority: P0
Area: post-review bundle attestation
Owner: Factory
First seen: Nysa sealed qualification candidate
`1580fa978525fe31f0dc482e54e00da603661721`
Impact: T-094 crossed the repaired ticket-PR boundary at route head
`1096355e271c4fa9355d9e66e7e6e3b9528dde8a` without replaying Narrator, but
the immediately following `ticket-attest --action bundle` rejected the same
lineage as `product or code changed after the reviewed SHA`. T-100 and T-093
remained in dependency wait and all claims were safely released.
Finding: ticket-PR and bundle attestation independently maintained their
post-review allowlists. The first validator admitted exact referenced Narrator
PNGs, while the second still admitted only ticket, bundle, route, and refresh
metadata. A signature/footer-only PNG check also admitted structurally invalid
chunk data.
Smallest repair: move Narrator raster classification into one shared helper
used by both validators. Validate ordinary Git mode, exact current-ticket flat
paths and references, per-file and aggregate bounds, the complete PNG chunk
stream and CRCs, unique IHDR/IEND, at least one IDAT, and valid IHDR fields.
Edge coverage: ticket-PR now covers valid add/delete, in-place replacement,
and the exact 32-file boundary plus unreferenced additions and deletions,
bad signatures, forged signature/footer with invalid chunks, oversized and
excess sets, symlinks, executable blobs, nested paths, and sibling-ticket
paths. Bundle attestation separately proves the live add/delete shape succeeds
and an unreferenced file still refuses.
Live closure requires a sealed successor to create the T-094 bundle
attestation and reach Awaiting Approval without another Reviewer or Narrator.

## FI-20260801-140 — Approval attestation was rejected as post-review implementation drift

Status: Focused regressions and exact live-history validation green; sealed
successor recovery pending
Priority: P0
Area: post-review approval publication lineage
Owner: Factory
First seen: Nysa sealed qualification candidate
`2c087dedd49016dcdf3f4392353fe87caf073556`
Impact: after T-094 received the required human Linear approval, the sealed
controller correctly committed approval head
`7c7ad4f33a9456777eee09baf9d63e7329be547c` as the direct child of bundle
attestation head `3753b6a4cdf0ac7471ceabf178f47a4a66d8d589`. A follow-on reconciliation then
ran the ordinary publication PR gate and rejected the Factory-generated
`approval.json` as `ticket implementation changed after the latest successful
review`. The exact head and all checks remained clean, auto-merge remained
disabled, T-100 and T-093 stayed dependency-gated, and no role replay occurred.
Finding: ticket-PR admitted the current ticket, bundle, route journal, refresh
receipt, and shared Narrator raster evidence after Reviewer, but omitted the
approval receipt that `ticket-attest` itself creates. Adding the path alone
would weaken the boundary because a forged receipt or approval-time ticket
change could then cross readiness before the attestation retry rejected it.
Smallest repair: centralize bundle-commit and approval-commit validation in one
shared helper consumed by both ticket-PR and ticket-attest. The helper binds
exact keys and identities, complete direct-parent topology, exact `M ticket + A
receipt` commit shapes, ordinary blob modes, immutable bundle/route blobs,
ordered timestamps, and the exact Awaiting Approval → Approved plus Linear
approval ticket transformation. For a later sealed successor, it locates the
unique approval-addition commit under the original Kit-SHA and admits only an
exact validated route migration and ticket Kit-SHA replacement while holding
the approval/bundle evidence and all other approved ticket text byte-identical.
Edge coverage: ticket-PR passes 34/34, including the exact approval continuation
and refusals for a tampered receipt, approval-time ticket drift, executable
receipt, duplicate JSON keys, and an extra commit path, plus acceptance of the
exact successor route continuation and refusals for later receipt mutation or
ticket drift. Ticket attestation
passes 60/60 through the same helper. The helper independently validates the
exact live T-094 approval head and reviewed SHA `22edcfb1057681a10354bf16978416cf7c733cb5`.
Live closure requires a sealed successor to cross the approval-head PR gate,
request protected auto-merge, and close T-094 without replaying Reviewer or
Narrator.

## FI-20260801-141 — Projected Linear approval falsely implied GitHub auto-merge

Status: Focused regressions green; sealed successor recovery pending
Priority: P0
Area: two-phase protected publication truth
Owner: Factory
First seen: Nysa sealed qualification candidate
`33f282b9da6a532ce7164f3d2be3e4dbffe3e471`
Impact: T-094 reached exact migrated approval head
`46fc583c045161b0f2aba70e766ff643bb0d6e06` with every required GitHub and
Railway check green. The approval receipt remained valid and the controller
repeatedly acquired the sole publication lease, but GitHub PR #304 still had
no `autoMergeRequest`. T-100 and T-093 correctly remained dependency-gated;
no role replay or implementation mutation occurred.
Finding: Linear sync correctly projected the transient Approved/Linear operator
fields away after the approval commit. `next-stage` used that absence as an
indirect signal that auto-merge had already been requested, even though the
phase-one attest-only operation deliberately had not called GitHub. The
controller's requested-stage branch therefore only renewed publication and
waited. Phase-two attestation also required the already-projected overlay and
assumed the approval commit was the current PR head, which would reject the
sealed successor route commit and later closeout.
Smallest repair: treat the immutable approval receipt as phase-two authority
when the state/approval overlay is wholly projected away, while refusing any
partial overlay. Share exact successor-continuation validation with phase-two
and protected-main closeout. When the requested stage is observed with a ready
PR, the controller now reacquires the exact publication lease, idempotently
requests auto-merge, verifies the exact H2 head and PR number, and only then
waits for merge. The launcher authorizes that recovery only from either the
ordinary request-pending receipt or the exact misleading requested-stage
receipt; all other transition stages remain refused.
Edge coverage: ticket attestation passes 60/60, ticket publication passes
34/34, the Factory controller passes 66/66, and the complete sealed-launcher
contract passes. The new cases prove
projected-overlay phase two after a successor route, partial-overlay refusal,
protected closeout after that route, and the controller's
misleading-requested-stage recovery. Live closure requires PR
#304 to record protected auto-merge, merge, and reach Factory-owned Done under
the sealed successor without replaying Reviewer or Narrator.

## FI-20260801-142 — Post-merge check propagation parked a merged ticket

Status: Focused regressions green; sealed successor recovery pending
Priority: P0
Area: protected-main closeout recovery
Owner: Factory
First seen: Nysa sealed qualification candidate
`bbb441acd90bab0670310c6707fe25475e4bd3a3`
Impact: the repaired controller requested protected auto-merge for T-094 head
`5c1beaf8a5ffda0a9b491d2db4094a2578f61bd5`, and PR #304 merged as
`894d1b6d454f1b6f14134e21153ee4b77c20e6a4` without replaying a role. The
immediate protected-main closeout ran before the required `ci` check appeared,
reported it as missing or unsuccessful, and parked T-094. Main CI and both
Railway statuses later passed, but same-release reconciliation excluded the
blocked claim, so no Done attestation was emitted and T-100 remained gated.
Finding: ticket attestation collapsed three distinct check states—missing,
pending, and completed unsuccessfully—into one refusal. The controller could
therefore not distinguish normal post-merge propagation from a terminal check
failure and applied its generic fail-closed parking behavior.
Smallest repair: preserve the exact check state in ticket-attest errors. The
controller treats only missing or pending post-merge checks as a wait, records
`post_merge_check_wait`, and retries closeout with the same claim; completed
unsuccessful checks remain errors. No role, ticket implementation, approval,
or Narrator evidence is regenerated.
Edge coverage: focused controller and ticket-attestation regressions prove an
in-progress required check waits while an unsuccessful or structurally
ambiguous check still refuses. The Factory controller passes 67/67; ticket
attestation passes 61/61. Live closure requires a sealed successor to retry the
already-merged T-094 and emit Factory-owned Done.

## FI-20260801-143 — Released publication lease hid an already-merged ticket

Status: Focused regression green; sealed successor recovery pending
Priority: P0
Area: protected-main closeout ordering
Owner: Factory
First seen: Nysa sealed qualification candidate
`0b4f5c9c622aba3cb741e362475582a6d5e30061`
Impact: T-094 remained merged as
`894d1b6d454f1b6f14134e21153ee4b77c20e6a4`, with main CI and both Railway
statuses green. Its earlier closeout failure had released the publication
lease. Successor recovery therefore skipped the merged-ticket shortcut,
entered dependency evaluation, and attempted a prepublication dependency
refresh that correctly refused the Approved ticket. No role or implementation
was rerun, and T-100 remained gated.
Finding: the merged-ticket shortcut incorrectly required a live publication
lease. Lease ownership serializes publication; it is not evidence of whether
GitHub already merged the PR.
Smallest repair: an authenticated passport whose publication state is merged
checks authoritative merged-PR truth before dependency refresh, regardless of
lease presence. A lease is released only when present, and protected-main
attestation remains the fail-closed Done authority.
Edge coverage: the controller regression proves a recovered merged passport
with no publication lease bypasses dependency tracking and the ordinary state
machine, then enters closeout without manufacturing a lease release; the full
controller suite passes 68/68. Live closure requires a sealed successor to
emit T-094 Done without role replay.

## FI-20260801-144 — Merged-passport recovery looped after closeout merged

Status: Focused regression green; sealed successor recovery pending
Priority: P0
Area: protected-main terminal transition
Owner: Factory
First seen: Nysa sealed qualification candidate
`d91309deffad5689456a0d33f98117fccc870358`
Impact: the recovered T-094 created closeout commit
`3b33dc41544722142efb41b4631304b85677f2ad` and protected PR #306 merged as
`7afbfebc8c1bf7947b2f4f43758d2a5ce2e418ce`. The controller nevertheless
kept re-entering the merged-passport shortcut, repeatedly validating closeout
instead of evaluating the authoritative terminal transition. The reconcile
process was stopped after exact verification; no role or ticket output changed.
Finding: the merged-passport shortcut ignored the closeout helper's result and
always reported progress. A pending closeout therefore spun within one cycle,
and a merged closeout could never fall through to `COMPLETE`.
Smallest repair: return `waiting` while closeout remains open. When closeout is
merged, continue through the ordinary state-machine boundary, require its exact
`COMPLETE` envelope, emit `ticket_complete`, and release the ticket lease.
Edge coverage: the Factory controller passes 69/69. The new case proves merged
closeout reaches authoritative `COMPLETE`; the prior cases retain pending
closeout waits with and without a publication lease. Live closure requires one
sealed successor to emit and release T-094 terminally without role replay.

## FI-20260801-145 — Branch dependency ordering masked protected-main Done

Status: Focused regression green; sealed successor recovery pending
Priority: P0
Area: protected-main terminal authority
Owner: Factory
First seen: Nysa sealed qualification candidate
`3a7470a9168b8cbafec3e2c56bc3084ae52e0da6`
Impact: T-094's retrying `done` operation validated its exact closeout commit,
Done receipt, ledger, original merge and required checks, and merged closeout PR
#306. The controller then evaluated the stale Approved ticket branch, whose
dependency check ran before branch-stage resolution and requested a
prepublication refresh. That refresh correctly refused, parking T-094 again;
no role or ticket output changed.
Finding: protected-main terminal truth belongs to the `done` attestation and
cannot be rediscovered by the branch-oriented prepublication state machine.
Falling through after exact closeout validation was redundant and reopened an
inapplicable dependency boundary.
Smallest repair: a successful merged `done` retry is the terminal controller
authority. It emits `ticket_complete` and releases the ticket lease immediately;
an open closeout still returns a wait, and all failed or ambiguous attestation
evidence remains fail closed.
Edge coverage: the Factory controller passes 69/69. The terminal case proves
exact merged closeout completes before dependency tracking or branch-stage
evaluation; the pending cases retain waits with and without a publication
lease. Live closure requires one sealed successor to release T-094 without role
replay.

## FI-20260801-146 — Successor route lineage hid valid terminal evidence

Status: Focused regression green; sealed successor recovery pending
Priority: P0
Area: protected-main terminal and dependency truth
Owner: Factory
First seen: Nysa sealed qualification candidate
`3a7470a9168b8cbafec3e2c56bc3084ae52e0da6`
Impact: T-094 is Done on protected main at
`7afbfebc8c1bf7947b2f4f43758d2a5ce2e418ce`, but the shared terminal reader
rejected its normal attestation chain because bundle/approval Kit-SHA
`2c087dedd49016dcdf3f4392353fe87caf073556` differed from Done Kit-SHA
`bbb441acd90bab0670310c6707fe25475e4bd3a3`. T-100 and T-093 therefore waited
on T-094 even though its protected closeout was complete. No role was replayed.
Finding: terminal validation assumed the route blob and Kit-SHA could not change
between bundle and Done, while the publication boundary already permits exact
sealed-successor release migrations that preserve role evidence.
Smallest repair: retain exact bundle/approval identity, then require the
historical attested route journal to be a byte-for-byte prefix of protected
main and validate a hash-linked suffix containing only continuous release
migrations ending at the Done and ticket Kit-SHA. Fallback, tampering, unknown
shape, or discontinuity still refuses.
Edge coverage: the effective-ticket regression closes a normal ticket after a
successor route migration, then retains its existing ledger-append and
prefix-tamper checks. The same validator recognizes live T-094 as
`attested-done`; the focused effective-ticket suite passes 9/9.

## FI-20260801-147 — Historical passport resurrected a completed ticket

Status: Focused regression green; sealed successor cleanup pending
Priority: P0
Area: controller terminal claim recovery
Owner: Factory
First seen: Nysa sealed qualification candidate
`769b8c443daa3042317e2781158174d9fd7da60d`
Impact: the controller emitted `ticket_complete` for T-094 and released its
claim, then the same reconcile recovered a new claim from the retained Approved
passport. The controller was stopped after exact inspection; no role ran and no
ticket or role output changed.
Finding: a passport is retained historical audit evidence after protected-main
Done, not authority to schedule the ticket again.
Smallest repair: sealed product tickets with exactly one `State: Done` are
excluded from passport recovery. Any residual claim is renewed and released
through the normal controller path before workers are scheduled; the passport
remains intact for audit and reduction.
Edge coverage: one regression proves a Done ticket is not recovered from its
passport, and one proves an existing residual claim is released before recovery.
The full controller suite passes 71/71; live successor cleanup remains pending.

## FI-20260801-148 — Terminal target could not cross qualification restart

Status: Focused regression green; sealed successor restart pending
Priority: P0
Area: qualification restart boundary
Owner: Factory
First seen: Nysa sealed qualification candidate
`f769e97a566725645102bfbd5f48694d7859e1d1`
Impact: live cleanup correctly removed T-094's residual claim, leaving T-100
and T-093 runnable, but the restart boundary reported `active=2` and
`waiting_for_target` forever because the manifest still requires three Done
targets. No role ran and no ticket output changed.
Finding: qualification restart equated the target count with runnable claims;
it did not count protected product Done as an already satisfied target.
Smallest repair: both sides of the restart boundary use the union of runnable
claims and exact product Done tickets for cohort accounting and event binding,
while the reported active count and scheduler retain only unfinished claims.
Edge coverage: one regression exercises both pre-restart and post-restart with
one protected Done target and two runnable claims; the existing all-runnable
restart and terminal-claim cleanup regressions remain green.

## FI-20260801-149 — Derived invalid fixture was identical to its valid input

Status: Focused regression green; sealed T-100 canary pending
Priority: P0
Area: frozen-contract and test-author correctness
Owner: Factory
First seen: T-100 under sealed Factory
`752fe7afcd67af693d3d7b6c30e78a7b6f95e7a5`
Impact: T-100's accepted Test-author evidence derived an invalid fixture with
`.toUpperCase()` from a numeric UUID. The transformation was byte-identical to
the valid fixture, so Builder could not satisfy both assertions and spent a
full implementation run before reporting the contradiction.
Smallest repair: Planner now freezes the exact transformed value and verifies
byte distinction; Spec-linter and Test-author independently reject an identity
transformation. Prompt-contract coverage locks all three checks.

## FI-20260801-150 — Pre-block Linear state could impersonate operator resume

Status: Focused regression green; production install deferred
Priority: P0
Area: Linear operator authority
Owner: Factory
First seen: T-100 contract-block recovery on 2026-08-01
Impact: Linear still exposed Building when the local ticket first entered
Blocked-Escalated. Without a post-block observation boundary, that stale state
could be consumed as a new operator resume even though the operator had not
made a later transition.
Smallest repair: record Linear's `updatedAt` only after the reconciler observes
Blocked-Escalated, then accept the declared resume state only with a strictly
newer timestamp. The focused suite proves stale Building is restored, the
Blocked baseline is observed, and only a later Building transition resumes.

## FI-20260801-151 — Planner repair retained superseded downstream evidence

Status: Focused regression green; sealed T-100 canary pending
Priority: P0
Area: test-first role sequencing
Owner: Factory
First seen: T-100 Planner repair commit
`36a8c916` under sealed Factory
`752fe7afcd67af693d3d7b6c30e78a7b6f95e7a5`
Impact: Planner repaired T-100's frozen contract, but the sequencer counted the
older Spec-linter and Test-author evidence and issued Builder receipt
`733d3272` without a new checking-lane run. No provider run or new role output
was accepted under that receipt.
Smallest repair: authenticated completed-role order now treats any Planner run
after Test-author as a new test-first epoch. It requires Spec-linter,
Test-author, then Builder while preserving all earlier evidence. The focused
state-machine suite proves the complete reopened sequence and its prior normal
planning path.

## FI-20260801-152 — Archived Planner repair lost catch-up authority

Status: Focused regression green; sealed T-100 retry pending
Priority: P0
Area: state materialization after contract repair
Owner: Factory
First seen: T-100 sealed successor canary under Factory
`3a90ab040667f9c37c0397b6a086d40e193f6c66`
Impact: authenticated role order correctly selected a fresh Spec-linter after
the completed Planner repair, but the materializer refused the required
Building-to-Planning catch-up. The refusal occurred before provider admission;
no role ran and no output or charge was created.
Smallest repair: only a signed completed Planner repair may retain the narrow
backward override when its immediate resolved stage targets an earlier coarse
state. Ordinary backward transitions remain forbidden.
Edge coverage: the completed-repair regression drives `next_transition` from
Building to `RUN spec-linter` and proves no generic state transition is called.

## FI-20260801-153 — Frozen generated ID contradicted its reset

Status: Focused prompt regression green; sealed T-100 repair pending
Priority: P0
Area: frozen-contract and test-author correctness
Owner: Factory
First seen: T-100 Test-author run `1785657239-55398` under sealed Factory
`7a855d62b556b78ed9233fa3926bb7d6fed8a5bb`
Impact: contract v4 required receipt ID `4001`, while the protected test setup's
identity reset produced `1`; the narrow v4 repair scope also forbade correcting
that setup. Test-author detected and committed the contradiction before Builder,
so no implementation run was admitted.
Smallest repair: Planner, Spec-linter, and Test-author now independently evaluate
exact generated identifiers, sequences, counters, and timestamps from their
initializer/reset and reject a repair scope excluding a required setup fix.
Edge coverage: prompt-contract regression locks all three checks; the sealed mock
role-sequence and complete lifecycle-state matrix remain required before retry.

## FI-20260801-154 — Prior resume overlay survived a repeated block

Status: Focused regression green; sealed T-100 resume pending
Priority: P0
Area: Linear operator authority
Owner: Factory
First seen: T-100 second contract block under Factory
`8e1d0016bfe2e86d1f84daa0c126c9bedb8d863d`
Impact: after the earlier Planner resume, the stored Building overlay already
named `state_base=Blocked-Escalated`. When T-100 later blocked again at that
same coarse state, the reconciler retained the old overlay, so it could not
record the new blocked timestamp baseline. No resume or provider call occurred.
Smallest repair: an exact remote/local Blocked-Escalated observation always
clears a prior state/approval overlay before materializing effective state and
records the latest remote timestamp as the new baseline.
Edge coverage: the Linear regression now proves resume, same-state re-block,
overlay removal, new baseline capture, and only then a later second resume.

## FI-20260801-155 — Backward repair blocker required an impossible state rewind

Status: Focused regression green; sealed T-100 retry pending
Priority: P0
Area: contract-repair state transitions
Owner: Factory
First seen: T-100 Planner repair run `1785661512-9483` under sealed Factory
`4a8abc0eb8d4ac0ebf10604d1db57f0a34ce5dca`
Impact: the signed `FIX planner` correctly ran beneath T-100's unchanged
Building state and committed a new contract blocker. The shared block
transition then required Planning, so it could neither materialize the blocker
nor preserve the Planner output for an authenticated resume. No downstream
role or provider run was admitted.
Smallest repair: the exact active signed backward repair may block at its later
coarse state and records that state as `Resume-State`; block recovery and
resume authenticate the same repair. Every unsigned, mismatched-role, earlier,
or non-phase state remains refused.
Edge coverage: the state-machine regression proves refusal without the signed
repair, direct block without materialization, idempotent blocked recovery, and
resume to the unchanged coarse state.

## FI-20260801-156 — Blocked repair lost authority across a successor migration

Status: Focused regression green; sealed T-100 retry pending
Priority: P0
Area: contract-repair release migration
Owner: Factory
First seen: T-100 recovery under sealed Factory
`0e6f8632cb91e2daefa184ae4fa249aed842b56f`
Impact: T-100's passport and route journal migrated successfully, but the
active signed Planner repair predated the Planner's contract-block commit. The
migration validator required an edge beginning at the repair authorization
head and rejected the authentic edge beginning at the later terminal-block
head. Recovery stopped before block materialization or any provider call.
Smallest repair: reuse the shared exact contract-block terminal validator and
accept the later migration start only when the consumed `FIX` receipt, its
parent blocker, unique terminal manifest, authenticated charge, passport stage,
and Git ancestry all bind the active repair to the successor passport.
Edge coverage: a synthetic repair-owner block plus release migration now
survives, while a passport whose current stage does not match that `FIX` receipt
remains invalid; the preserved live T-100 evidence also resolves to
`FIX planner` without mutation.

## FI-20260801-157 — Prior resume overlay masked a newly committed block

Status: Focused regression green; sealed T-100 baseline pending
Priority: P0
Area: Linear operator authority and contract-block recovery
Owner: Factory
First seen: T-100 block materialization commit
`4650e355c965227d6605d0fb62f3762ba60ddc49`
Impact: the successor correctly committed T-100 as Blocked-Escalated with
`Resume-State: Building`, but the canonical Linear map still held the prior
block's Building overlay. Effective projection therefore masked the new block,
and recovery attempted resume with the older operator receipt. The state
machine refused before any provider call.
Smallest repair: bind every accepted state overlay to the exact committed
ticket text and clear it when that source changes, including legacy unbound
overlays. The controller materializes a retained blocker but calls resume only
when the ticket visibly contains its exact current receipt; the state machine
remains the sole authority for authenticating that directive.
Edge coverage: the Linear regression now re-blocks by changing only committed
ticket content while remote Linear remains at the old resumed state, proves
that Blocked is republished and a new baseline recorded, then accepts only a
later resume. The controller regression proves an older receipt waits and the
current receipt reaches state-machine validation.

## FI-20260802-158 — Epoch gate rejected the Planner's established freeze form

Status: Focused regression and disposable six-role mock lifecycle green;
sealed successor replay pending
Priority: P0
Area: Planner contract and test-immutability epoch classification
Owner: Factory
First seen: T-100 Planner repair commit
`36a8c916a2e68f614a43b683774cf06bc97de53c`
Impact: T-100's authenticated Planner repair changed only its ticket, replaced
the latest v3 heading and established `Freeze result: PASS` line with the
higher matching v4 pair, and then correctly handed the repair to Test-author.
The new epoch gate recognized only a novel exact sentence and append-only text,
although the Planner role required neither. It therefore classified protected
test commit `a4247011fe10114138b94a4563f7167dde7ec994` as late under v3. No
provider, publication, or production action followed the refusal.
Root cause: the Planner producer and the gate/reorder consumers had different
freeze-marker and version-retention contracts.
Smallest repair: both consumers share the established PASS forms and admit only
one exact legacy replacement of the latest heading plus its matching PASS with
one higher pair in a ticket-only commit. Planner v8 requires the canonical
single-line marker and append-only versions going forward. Partial, mismatched,
repeated, lower, mixed, and malformed evidence remains closed.
Validation: the synthetic gate/reorder scenario reproduces T-100's exact v3
comma-terminated marker, one-for-one v4 replacement, and later canonical v5
append; its negative matrix covers eight invalid shapes. The candidate gate
passes T-100's complete real branch with production `TEST_PATHS` while leaving
its head and tree unchanged. Candidate `cdeef046` then completed the disposable
Planner, Spec-linter, Test-author, Builder, Reviewer, and Narrator mock lifecycle
once in 178 seconds and stopped at `AWAIT-OPERATOR` with no replay.

## FI-20260802-159 — Pending operator commit invalidated an active migrated repair

Status: Focused regression, live read-only proof, and disposable six-role mock
lifecycle green; sealed successor replay pending
Priority: P0
Area: contract-repair authentication and passport migration
Owner: Factory
First seen: T-100 controller recovery at operator commit
`2ac344d80706e101b5a53a8cb356f7e57a5d4602` under sealed Factory
`6b350a3fb0d7e8edec2a0f2fda7fe21d26d81003`
Impact: the exact Planner resume directive was the sole ticket-only child of
T-100's authenticated passport head
`4f7bbbf33786ca625e89efdeee1f3ba4ba0eaa2b`. Idempotent block recovery tried
to validate the retained migrated repair against the newer Git head before the
ordinary block transition could migrate the passport, so the controller
emitted `ticket_recovery_failed` and stopped before any provider run or charge.
Root cause: repair migration implicitly treated the working Git head as the
authenticated passport boundary even inside the deliberately narrow pending
operator-commit window.
Smallest repair: reuse the existing strict operator-directive validator when
Git HEAD differs from the passport head, validate the retained repair at that
authenticated head, and let the existing block transition migrate the exact
directive commit before resume. No second resolver or ticket-specific rule is
introduced; arbitrary descendants still fail closed.
Validation: the regression reconstructs a backward Planner repair that blocks,
survives a release migration, then receives one exact receipt-bound operator
commit after its passport head. It proves repair recovery plus one passport
migration. The candidate also passes all 32 focused state-machine cases and
resolves the preserved live T-100 evidence read-only to `FIX planner` without
changing its claim, passport, repair, branch, or charge state.
Candidate `246782f` then ran Planner, Spec-linter, Test-author, Builder,
Reviewer, and Narrator exactly once in disposable lane
`/private/tmp/nysa-sf-dev.amHs9i`, reached `AWAIT-OPERATOR`, and retained a
clean pushed mock branch in 224 seconds.

## FI-20260802-160 — Linear board work serialized ticket authority and repeated incidents

Status: Focused Linear/controller regressions green; protected CI pending
Priority: P1 (#175)
Area: Linear synchronization and admission
Owner: Factory
Impact: the network-length board cycle held the same lock needed to consume a
ticket overlay, and unchanged `unsafe_state` inputs emitted noise every cycle.
Smallest repair: split cycle and short map locks, persist exact operator-clear
intents, and retain one durable input-digested incident with bounded reminders.
Validation: a stale full-board save cannot restore a consumed overlay; active
claims continue while new admission remains closed.

## FI-20260802-161 — Pause and interruption shared ambiguous claim shapes

Status: 78 controller and 33 state-machine regressions green; protected CI pending
Priority: P0 (#164/#184)
Area: controller persistence
Owner: Factory
Impact: a deliberately claim-free passport and an accidentally receipt-free
blocked claim had no distinct authenticated recovery authority.
Smallest repair: add explicit passport- and lifecycle-state-bound
`ticket-control pause|resume` plus one-use pre-provider reconciliation markers.
Neither path scans historical passports or weakens typed blocked claims;
merged/Done truth and capacity refusal leave the pause intent untouched.
Validation: pause/resume, restart, successor passport lineage, exact state,
capacity, merged ticket, blocked restore, two-ticket interruption recovery,
dirty/active/paused/terminal/cross-release refusal, and idempotence are covered.

## FI-20260802-162 — Reviewer-requested late tests were guaranteed CI-red

Status: Focused role/state regressions green; protected CI pending
Priority: P0 (#182/#183)
Area: repair sequencing and Planner scope
Owner: Factory
Impact: Contract 1.8 could push a Test-author commit after Builder, violating
the protected tests-first gate; the preceding Planner repair also ran the full
workspace suite.
Smallest repair: route both single- and dual-owner late test repairs through one
ticket-only higher frozen epoch, authenticate it before Test-author, and block
Planner package-manager entry points. Existing Narrator output is preserved.

## FI-20260802-163 — Certification prerequisites failed after expensive phases

Status: Eight focused runner regressions and caller validation green;
protected CI pending
Priority: P0 (#165/#172/#173)
Area: certification trust boundary
Owner: Factory
Impact: missing reviewed network appeared as opaque npm failure, a known
noncanonical active-product path failed only after the phase graph completed,
and interrupted certification discarded already-complete expensive phases.
Smallest repair: plan v2 pins Node/npm and phase network policy, fails missing
capability before spawn, retains denied phases under reviewed opt-in, preserves
redacted hash-bound failure evidence, and validates the active generation/path/
origin before workspace preparation. Explicit restart-local reuse now binds a
self-hashed phase record to every Factory/product/plan/dependency/runtime/
command/network input and rehashes its retained log plus declared artifacts.
Legacy plans and undeclared side effects never reuse.
Validation: exact repeat, Factory/product/plan/runtime/network invalidation,
artifact drift, interruption, stale evidence, and tamper regressions prove no
false hit. Factory receipt validation accepts only hits carrying a phase-record
digest; full protected certification remains authoritative. Persistent reuse
across separate disposable certification commands remains explicitly deferred
to P1 #198 because it requires authenticated artifact packaging/restoration,
not retention of a prior writable workspace.

## FI-20260802-164 — Successor ticket migrations ran serially

Status: Focused overlap and compact-preview regressions green; protected CI and successor canary pending
Priority: P0 (#181)
Area: migration and resume latency
Owner: Factory
Measured baseline: preserved three-ticket qualification events span 27.8
seconds between first-phase migration completions and 61.7 seconds between
authenticated resume completions because tickets ran one after another.
Smallest repair: overlap only independent per-ticket recovery calls up to the
already-certified capacity. Every ticket retains its existing launcher,
passport, route, lease, repair, and accounting sequence.
Additional occurrence: a 71-revision journal produced a 412,396-byte ordinary
preview in the representative fixture, and live apply repeated the complete
readiness round. Ordinary output now retains only exact source, readiness,
journal-tail, and approval digests; full journal output is explicit diagnostics.
Apply compares one fresh readiness digest to the approved preview instead of
probing twice, while the candidate journal, preview hash, tamper decisions, and
failure reasons remain identical.

## FI-20260802-165 — Frozen test scope omitted fixture cleanup dependencies

Status: Focused role-contract regression green; protected CI pending
Priority: P0 (#192)
Area: planning, specification lint, and protected-test ownership
Owner: Factory
First seen: T-100 Test-author contract block at product commit `d44bb6a4`
under sealed Factory `498dadc36f4c70956a7d25231215b9f11cafb4a8`
Impact: a required serialized suite created a non-cascading child row in one
criterion, then a later reset deleted its parent first. The exact frozen repair
scope omitted the required cleanup edit, so Test-author correctly preserved its
valid committed tests and stopped before Builder.
Smallest repair: Planner and Spec-linter trace setup, reset, and teardown across
the required serialized command, enumerate sibling foreign-key dependencies,
and freeze only required child-first cleanup edits. Exact `ON DELETE CASCADE`
relationships need no redundant cleanup and unrelated protected tests remain
outside Test-author ownership.
Validation: the focused role-contract regression covers child-first cleanup,
sibling dependencies, cascade control, narrow ownership, and preserved
Test-author fail-closed behavior.

## FI-20260802-166 — Sealed launch selected a newer system Node

Status: Focused owner-runtime regression green; protected CI and qualification
canary pending
Priority: P2 (#206)
Area: certification runtime bootstrap
Owner: Factory
Impact: qualification preparation could pass under an explicit Node 22 caller
PATH, then the sealed launcher rebuilt its fixed PATH and selected a newer
system Node before the strict tuple guard stopped the run.
Smallest repair: expose one `factory-kit runtime-pin` operation that reuses the
existing launcher PATH priority. It validates the shared product plan and the
exact Node/npm/npx source versions before atomically replacing owner-local
symlinks in `~/.factory/bin`; it never changes a system-wide Homebrew link.
Validation: a focused regression proves the plan-matching owner pin wins over a
coexisting newer system Node and that source mismatch preserves prior pins.

## FI-20260803-167 — FIX blocker lease recovery required its future repair record

Status: Focused state-machine regression green; protected CI pending
Priority: P0 (#228)
Area: contract-blocker restart recovery
Owner: Factory
First seen: T-100 after a Builder contract block in sealed qualification
Impact: T-100 retained its consumed `FIX builder` receipt, exact terminal
manifest and charge, authenticated blocked passport, current branch ancestry,
operator directive, and newly acquired exact-ticket lease. Recovery still
refused `contract blocker receipt is invalid` before any provider call.
Root cause: the receipt and passport correctly retained the later coarse
`Resume-State: Review`, but validation called `contract_repair_stage()` before
`resume_transition()` could create that signed repair record.
Smallest repair: validate a later coarse resume state directly when the exact
consumed receipt and authenticated blocked passport both name `FIX <role>`.
Every receipt, role, stage, charge, terminal, state, ancestry, directive, and
current-lease check remains fail-closed.
Validation: the focused regression reproduces the FIX Builder/Review boundary,
rotates the lease, commits the exact receipt directive, and proves stage and
passport-state mismatches still fail. Existing controller, Linear-baseline,
lease, receipt, and repair tests retain the remaining negative matrix.

## FI-20260803-168 — Same-release FIX validation shadowed migration proof

Status: Focused state-machine regressions green; protected CI pending
Priority: P0 (#231)
Area: contract-blocker release migration
Owner: Factory
First seen: protected-main run 30823844226
Impact: Linux and macOS deterministically rejected the existing authenticated
release-migration blocker regression with `contract blocker role state
drifted`, blocking qualification of the otherwise passing candidate.
Root cause: the new same-release FIX shortcut treated the expected historical
receipt/current-passport Factory mismatch as malformed before the established
migration validator could authenticate it.
Smallest repair: retain hard refusal for same-release FIX mismatches, but let a
cross-release pair fall through to the existing contract-repair migration
proof. No receipt, passport, role, charge, terminal, ancestry, directive,
lease, or migration-lineage check changes.
Validation: the formerly failing migration case and the rotated-lease FIX
positive/tamper regression pass together; the complete focused state-machine
suite and protected CI remain authoritative.

## FI-20260803-169 — Claimless Done takeover omitted candidate completion proof

Status: Focused successor adoption and reducer regressions green; protected CI pending
Priority: P0 (#233)
Area: qualification restart and reduction
Owner: Factory
Impact: after a Factory candidate changed, the controller counted an exact
protected-main Done target at restart but left its passport on the source
Factory and could not emit a candidate completion because the terminal claim
had already been released. Successor reduction also required a new publication
lease pair for that already-published ticket. No remaining path could satisfy
all three requirements, while set-based checks failed to reject some duplicate
completion and sequential publication evidence.
Smallest repair: authenticate and migrate the source terminal passport through
its one surviving clean ticket cell without a claim, role, publication lease,
or charge. Seal the source/candidate passport digests and protected Done/PR
identity in one candidate marker and typed adoption event, append completion
once, and exempt only that exact adopted ticket from candidate publication
events. Reduction now checks event multiplicity instead of only set membership.
Validation: focused controller and successor-reducer regressions cover a
claimless source terminal, older Done kit in authenticated release history,
candidate migration, marker/event replay, no claim or attempt, no adopted
publication cycle, and rejection of duplicate adoption, completion, acquisition,
and release evidence.

## FI-20260803-170 — Historical terminal passport bypassed successor adoption

Status: Focused historical-lineage adoption regressions green; protected CI pending
Priority: P0 (#235)
Area: qualification terminal adoption
Owner: Factory
First seen: sealed successor candidate `57c304266392348625307848fd6e392d887c7c38`
Impact: T-094's authenticated merged terminal passport was current on an
intermediate Factory after the manifest's active production source. Takeover
preparation accepted that valid ordered suffix, but the controller required
direct source equality, emitted candidate completion, and left no migrated
passport, adoption marker, or adoption event for reduction.
Smallest repair: use the shared ordered successor-lineage reducer before
adoption, preserve-migrate from the passport's immediate pre-candidate Factory,
and bind both the manifest source and immediate predecessor in the marker and
event. Disconnected lineage now fails before migration or candidate evidence.
Validation: the focused controller regression covers source-to-intermediate-to-
candidate adoption, nonterminal refusal, disconnected-lineage refusal,
idempotent replay, and no claim, role, charge, or publication cycle. The
successor reducer regression requires the same complete suffix and exact final
edge.

## FI-20260803-171 — Migrated repair resume required its future record

Status: Focused state-machine regression green; protected CI pending
Priority: P0 (#237)
Area: contract-blocker release recovery
Owner: Factory
First seen: T-100 in sealed successor qualification
Impact: a fully authenticated historical `FIX builder` blocker with a current
blocked passport and exact receipt-bound Planner directive could not resume.
The state machine failed before a provider call with `operator resume lacks
authenticated contract repair state`.
Root cause: the resume-state validator rejected the historical-receipt/current-
passport Factory mismatch and asked for the signed repair record that the same
resume operation creates only after state materialization.
Smallest repair: reuse the existing migrated-blocker proof at that narrow
boundary, additionally matching the exact authenticated passport digest and Git
lineage. Receipt, terminal, charge, release history, role, FIX stage, directive,
state, and ancestry validation remain mandatory; no controller bypass exists.
Validation: the exact regression uses a predecessor `FIX builder` receipt,
current `Blocked-Escalated` passport, `Resume-State: Review`, and receipt-bound
Planner directive with no active or completed repair. A mismatched caller
passport digest remains fail closed.

## FI-20260803-172 — Terminal adoption compared approved and migrated heads

Status: Focused adoption and reducer regressions green; protected CI pending
Priority: P0 (#239)
Area: qualification terminal adoption
Owner: Factory
First seen: Nysa sealed generation-11 qualification
Impact: T-094's authenticated Done receipt bound its merged PR head, while
signed post-merge route migrations advanced the current passport head. The
controller and reducer required those heads to remain equal, so a valid
terminal could not be adopted without rerunning completed work.
Smallest repair: replace equality with one shared exact v2 suffix check from
the approved head through the current authenticated passport Factory, head,
protected base, route, and parent digests. No product state, role evidence,
publication action, or production activation changes.
Validation: focused controller and reducer cases accept the post-merge suffix
and refuse disconnected edges, ambiguous suffixes, and substituted parents.

## Maintenance rule

Record only a systemic failure, backward transition after Spec PASS, sibling
block, or trust/accounting/cancellation divergence. Include immutable evidence,
measured impact, owner, smallest change, and validation. Close an entry only
after a focused regression and one real sandbox canary; then promote the stable
rule into `context/memory.md` and the relevant operating document.

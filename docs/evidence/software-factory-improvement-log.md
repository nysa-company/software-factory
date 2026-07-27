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

## Maintenance rule

Record only a systemic failure, backward transition after Spec PASS, sibling
block, or trust/accounting/cancellation divergence. Include immutable evidence,
measured impact, owner, smallest change, and validation. Close an entry only
after a focused regression and one real sandbox canary; then promote the stable
rule into `context/memory.md` and the relevant operating document.

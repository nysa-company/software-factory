# Memory — software-factory decision log

Entry format: `## YYYY-MM-DD — Short title`, then `Category: Decision | Preference | System change | Context`, then 1–3 sentences. Mark superseded entries clearly and number durable decisions within this repository.

## Current truth

- This repository is a product-agnostic factory kit; product repositories carry only their factory state and CI integration.
- Durable decisions in this repository use their own numbering, beginning at Decision 1.
- Model routing is portfolio policy: the catalog separates transport, gateway, inference provider, family, account route, selection ID, and reported identity; profiles order all-six-role portfolios with distinct production/checking families. Routes are pinned at the ticket boundary and may change mid-ticket only through the Contract 1.4 one-use Linear-approved journal flow; one logical role attempt submits to at most one process.
- Stable product and operating truth lives under `docs/`; executable prompts, copied templates, conformance evidence, and vendored material remain colocated with their consumers.
- The repository adopts Nysa Agents baseline v3 as a toolkit with repository, secret, artifact, Git-flow, CI, config-review, and full local PR gates enabled. The canonical verification command is `bash ci/test-all.sh`.
- Live products resolve sealed exact-SHA kit releases under `~/.factory/kits` through the stable `~/.factory/bin/factory-launch` contract; kit merges are candidates until a product-specific certified activation.
- Install records owner-only, expiring kit-suite evidence for the exact sealed release. Certification reuses it only when every release, physical-tree, host, platform, suite-definition, tool-version, and configured-lifetime binding matches; product certification and product/config/receipt validation always rerun, and receipt expiry cannot outlive suite proof.
- External products require one full `factory/KIT_PIN`, and the first role launch records a durable ticket `Kit-SHA`; only the in-repository conformance test bed has an implicit runtime pin.
- Release activation is maintenance-gated, receipt-bound, and journaled. Failed-cutover recovery keeps `MAINTENANCE`, stops product factory services, reconciles any interrupted transaction, restores the protected previous pin/tree, and calls rollback only for a committed active candidate; automatic pruning is intentionally unavailable.
- The required aggregate `ci` status always reports. A narrow fail-closed inert-metadata diff skips expensive suites while retaining Linux policy and test-immutability checks. Every other change runs Linux; shell/platform-sensitive PRs and every non-lightweight merged SHA also run macOS system-Bash verification. Relay generation 4 runs documentation-only release `35c2e10` with healthy generation 3 on `3b63cc7` retained as its exact current-tree rollback baseline; the five-minute outage target and formal rollback RTO remain unaccepted.
- On macOS hosts where `/usr/bin/python3` is an xcrun shim, the launcher and release sandboxes use the fixed Command Line Tools Python binary when available; this preserves default-deny Seatbelt behavior without xcrun cache writes outside the sandbox.
- The no-record default is `balanced-v2`: Planner uses GPT-5.6 Sol/high; Builder and Narrator use GPT-5.6 Terra/high; Spec-linter and Test-author use Claude Fable 5/medium with Cursor Fable Thinking Medium secondary; Reviewer uses Claude Sonnet 5/high. `legacy-balanced-v1` remains compatibility policy; OpenAI-, Claude-, and Cursor-priority profiles remain explicit alternatives.
- Parallel kit branches, worktrees, PRs, and inert candidate releases are supported. Product activation/rollback remains serialized; contracts 1.1 through 1.5 default to one ticket and permit a configured maximum of four exact-worktree ticket leases with atomic budget reservations and opaque leases confined to trusted helpers. Nysa T-013/T-014 and T-015/T-016 proved initial lease-level concurrency; the product-wide lock still serializes every provider interval, and provider-call concurrency still requires OS-enforced writer isolation, bounded parallel accounting, and crash-recovery evidence.
- Spec-linter and Reviewer escalation overrides accept only an exact authorization for the next semantic round. Test immutability treats `.gitignore` and `context/memory.md` as exact-file bookkeeping exemptions, while documentation remains contract-significant; revert branches use `chore/<slug>-revert`.
- Ticket execution reads Git-authored state from the exact ticket worktree/committed branch and overlays Linear-owned fields from ignored `factory/linear-map.json`. Mutating roles must commit cleanly; the trusted wrapper non-force pushes and verifies them, while Reviewer must leave Git unchanged.
- Trusted ticket and role pushes use only the exact product origin bound by the active certification receipt. Contract 1.2 still stops in Review. Contracts 1.3 and 1.4 provide trusted bundle, exact newer Linear approval/protected auto-merge, and merge/deployment/Done closeout attestations while generic ticket-state keeps refusing evidence-sensitive transitions.
- Runtime costs are authoritative in atomic run manifests and materialized into ignored `factory/runtime-ledger.csv`; tracked `factory/ledger.csv` changes only through deterministic close-out projection, which refuses every active or ambiguous claim and `factory/runs/*.pid` record.
- Backward-compatible ledger reduction collapses a legacy durable reservation followed by its identity-matching terminal row; every other conflicting duplicate run ID fails closed.
- Product and machine runtime configuration is parsed as whitelisted data, never sourced as shell. Budget values are positive and coherent, and an explicit global-ledger path must be absolute before any probe, manifest, or task.
- Provider output and same-UID filesystem state are untrusted: durable GO precedes the adapter gate, the runs root and records are opened without following replacement links, output is captured on a wrapper-held descriptor, and only bounded adapter telemetry is consumed, with full-reservation fallback. A product-level control lock serializes provider intervals; any new or changed sibling manifest, persistent claim, owned manifest, global-ledger, or registered-checkout mutation fails the role. Hostile same-UID prevention requires OS isolation; the portable wrapper promises detection, conservative accounting, and no advancement instead.
- Hermes contract 1.5 retains Contract 1.4 route journals and adds fixed operator snapshots, project-owned policy, bounded envelope overrides, and targeted attempt cancellation while preserving compatibility with active 1.0–1.4 releases.
- Fresh ticket worktrees are created from protected main and pass through trusted materialization before preflight so their exact remote branch exists. Linear Project removal is represented explicitly in the ignored overlay and clears the effective initiative until reassignment.
- Activation, reconciliation, and rollback validate nonterminal `Kit-SHA` affinity from committed exact ticket branches. Plain configuration clears its full allowlist before optional file loading, so inherited environment values cannot become machine policy.
- Contracts 1.2 through 1.5 reject dirty exact ticket worktrees before ordinary ticket helpers. Contract 1.2 treats approval overlays as unsupported stops; contracts 1.3 through 1.5 consume merge approval only through an unchanged evidence-bound approval attestation.
- Operator overlays may materialize only kickoff and declared non-sensitive resume state changes; factory phases remain transition-owned. Git-backed Linear projection uses exact ticket refs then committed HEAD, never uncommitted checkout content.
- Provider-lock owners are bound to wrapper PID, process start, and a private token. Ordinary launch debounces transient owner-liveness misses but never reclaims stale or unsafe ownership; normal release atomically renames an owned lock before cleanup, and only the kill switch may quarantine a provably stale unchanged lock after KILL publication and recorded-process drain.
- Open-source factory frameworks remain references, not replacement control planes: any adopted execution or sandbox component stays behind `factory-launch`, while sequencing, budgets, role separation, Git authority, evidence, and operator approval remain factory-owned. The first justified experiment is a pinned SWE-ReX local-container backend for one non-production role; E2B or Daytona becomes relevant only if that canary proves local isolation insufficient.
- The operator activates model profiles by exact preview hash and may add narrow TTL-bound `credits_exhausted` overrides; subscription quota telemetry is incomplete. Ticket pinning commits and pushes Kit-SHA plus the exact six-role plan atomically. Post-submission retry remains forbidden; an eligible failed GO attempt may instead create one authenticated append-only fallback revision.
- Kimi K2.6 is disabled experimental through Claude CLI/OpenRouter/Moonshot, appears in no profile, and has not had a live or billed pilot. Credential rotation is required before a pilot, and direct same-UID token exposure remains without a broker or OS isolation.
- Route-journal provenance can support future provider/family/model budgets, but none are implemented and the ledger schema is unchanged. Model management, fallback, and evidence-bound ticket attestations are integrated under Hermes contract 1.4.
- The Contract 1.3 cutover has two independent one-time formats: legacy-closeout for the exact authorized Contract 1.2 batch and terminal-backfill for the exact authorized pre-contract terminal-Done batch. Both are separate from normal attestations and route plans, become authoritative only through one manual protected product merge, and use the same fail-closed protected-main terminal reader; plain Done never suffices.
- T-013 through T-016 alone use the audited aggregate-check legacy class because their PRs predate separate policy/app-test jobs; every other reviewed legacy ticket still requires all four authentic app-bound checks.
- The isolated process-group wrapper may wait up to two minutes for the trusted controller's final pre-submission acknowledgement. No adapter starts before that gate, so expensive protected-history validation can finish without weakening kill-switch or orphan prevention.

## Log

## 2026-07-19 — Decision 47: Validated pre-submission checks get a bounded two-minute gate

Category: System change

Protected-history validation can legitimately exceed the original ten-second
process-group acknowledgement window. The isolated wrapper now waits at most
two minutes for the trusted controller's final GO gate; adapters still cannot
start before acknowledgement, and a missing controller remains a hard timeout.

## 2026-07-19 — Decision 45: Local multi-project operator control plane

Category: Decision

Contract 1.5 adds a loopback-only console that selects only registered projects
and reaches product state through fixed launcher grammars. Project model policy,
per-role envelope limits, bounded overrides, and targeted attempt cancellation
are preview-hashed mutations; active budget changes use cancel, conservative
accounting, and same-role restart rather than retroactive manifest edits.
Normal Reviewer family separation remains mandatory, with only an exact
ticket-scoped one-use Linear fallback exception.

## 2026-07-19 — Decision 46: Dispatcher lease capacity expands to four

Category: System change

`MAX_CONCURRENT_TICKETS` accepts only `1` through `4`, defaults to `1`, and
requires one opaque lease per ticket above `1`. Atomic allocation refuses a
fifth lease, stale records continue consuming capacity until owner renewal or
maintenance recovery, and the product-wide provider lock continues to
serialize model-provider intervals.

## 2026-07-18 — Decision 44: Balanced v2 raises default effort and adds Cursor Fable

Category: Decision

The no-record profile becomes `balanced-v2`: production remains Codex Sol for
Planner and Terra for Builder/Narrator at high effort; checking remains Claude
Fable for Spec-linter/Test-author at medium and Sonnet for Reviewer at high.
Cursor secondaries use Sol High, Fable Thinking Medium, and Sonnet Thinking
High respectively. `legacy-balanced-v1` remains immutable compatibility policy,
and historical catalog hashes are accepted during migration only when every
selected route tuple still matches current certified policy.

## 2026-07-18 — Decision 43: Pre-contract terminal evidence is a separate bounded batch

Category: System change

The T-001 through T-012 pre-contract terminal epoch uses an independent
terminal-backfill schema and directory. Authorization binds the exact ticket
batch, product repository, protected-main basis, target kit, cutoff, immutable
source blobs, implementation and closeout PR ancestry, ledger rows, and
authentic app-owned checks. Historically absent bundles and Kit-SHAs remain
null. Only one manual protected product merge makes the complete batch
authoritative, and overlap with normal or first-batch evidence fails closed.

## 2026-07-18 — Decision 42: Earliest legacy PRs bind their authentic check era

Category: System change

T-013 through T-016 predate separate `policy` and `app-tests` jobs, so their
one-time allowlisted migration class binds the authentic app-owned aggregate
`ci` and `test-immutability` checks plus independent criteria-audit and current
combined-test digests. No other ticket may use that class; later reviewed
tickets still require all four exact app-bound checks.

## 2026-07-18 — Decision 41: Legacy closeout preserves the Contract 1.3 trust boundary

Category: System change

The one-time Contract 1.2 migration uses exact authorization and per-ticket
receipts under `factory/migrations/contract-1.3/`, never synthetic normal
attestations. A deterministic local generator binds immutable Git/GitHub,
check-app, ledger, audit, cutoff, old/new kit, and protected-basis evidence but
performs no remote mutation; only the operator's manual merge of the complete
single product PR grants authority. Review is the normal legacy class, while
only T-019/T-020 may carry the separately audited Planning anomaly class.
Shared validation makes plain Done, partial batches, unknown files, conflicting
receipts, and wrong target kits fail activation and terminal sequencing.

## 2026-07-18 — Decision 40: Model portfolios pin exact ticket routes

Category: System change

The operator activates an ordered model profile by exact preview hash.
`legacy-balanced-v1` was the default at this decision and is superseded as the
no-record default by Decision 42; OpenAI-, Claude-, and
Cursor-priority profiles provide explicit alternatives. Ticket-boundary
pinning resolves all six roles with distinct production/checking families and
commits the Kit-SHA plus exact route plan in one verified push; roles never
re-resolve, exact-route re-probe is allowed, and post-submission retry is not.
Kimi remains disabled and unpiloted, and scoped budgets remain future work.

## 2026-07-17 — Decision 39: Exact sealed releases reuse bounded suite proof

Category: System change

Fresh install or certification records atomic owner-only kit-suite evidence bound to the exact SHA/tree/origin/release path, recomputed physical tree, host, OS/architecture, suite definition, tool version, and configured lifetime. Certification treats missing or invalid evidence as a cache miss under the existing install lock, refreshes it only after a successful isolated suite and release revalidation, always reruns product certification and product binding checks, and issues schema-2 receipts bound to the exact suite proof with expiry capped by it.

## 2026-07-17 — Decision 38: One Linear approval enables protected auto-merge

Category: System change

Contract 1.3 makes the operator's Awaiting Approval → Approved Linear transition the sole business approval. Trusted bundle and approval attestations bind the latest non-void approved review, later Narrator lineage, bundle blob, exact PR/head, role runs, kit SHA, configured merge method, and newer Linear observation before requesting ordinary protected auto-merge; Done starts exactly at authoritative main and binds the protected approval blobs, exact merged PR head, collision-free configured merge-commit contexts, and projected accounting. Done then owns one retryable closeout commit and exact factory metadata/accounting PR, requesting protected auto-merge without another approval. At concurrency two every attestation requires the matching opaque lease without recording it; only protected-main terminal evidence produces `COMPLETE` and authorizes release. Contract 1.2 remains fail-closed at Review.

## 2026-07-17 — Decision 37: Required CI gets a documentation fast path

Category: Decision

The required workflow remains reporting and fail-closed. Diffs limited to the explicit inert-metadata allowlist skip expensive suites while retaining repository, secret, artifact, and immutability checks; every other change runs Linux. Pull requests add macOS only for shell/platform-sensitive paths, while every non-lightweight merged kit SHA runs both platforms before release and instantiated products run full verification on every `main` push. Commit size and Markdown suffixes are not safety signals; ambiguous comparisons fail closed.

## 2026-07-17 — Decision 36: Two-lease pilots expose the next latency boundary

Category: Decision

Two Nysa pairs completed with concurrent leases, disjoint worktrees, serialized provider intervals, and protected sequential merges; the second pair satisfies the evidence prerequisite for a non-production provider-concurrency canary, not production parallel calls. The pilots exposed avoidable latency from unconditional product CI and previews, repeated certification, sequential PR creation/checks, operator polling, and rebasing the second PR. They also exposed contract gaps around noninteractive CLI probes, just-in-time ticket-ref verification, deleted remote-tracking refs, ticket-cut approval validation, batch ledger projection, and trusted bundle/merge/deploy/Done attestation; these are tracked in `TODOS.md`.

## 2026-07-16 — Decision 35: Borrow mechanisms, not another factory

Category: Decision

Research across metaswarm, SWE-AF, Flow-Next, Open SWE, OpenHands, SWE-agent, SWE-ReX, E2B, and adjacent infrastructure found no project that combines protected test authorship, enforced cross-family review, hard spend accounting, exact-SHA releases, durable ticket state, and evidence-based human approval. Preserve that control plane; selectively borrow Flow-Next-style requirement-to-evidence traceability, metaswarm-style independent review evidence, Open SWE trigger/idempotency patterns, and in-toto/SLSA-shaped provenance fields, and test SWE-ReX only as a pinned execution transport behind the launcher. Do not add a second orchestrator, tracker, model-budget authority, or observability stack during the walking-factory phase.

## 2026-07-16 — Decision 34: Legacy ledger lifecycle pairs remain readable

Category: System change

Pre-manifest products may retain a durable reservation row followed by its terminal row under one run ID. The effective reducer collapses only that ordered pair when stable run identity matches exactly; reversed, repeated, or identity-conflicting duplicates still fail closed.

## 2026-07-16 — Decision 33: Spec-linter returns to Fable

Category: Decision

The Haiku Spec-linter cost experiment is superseded before product adoption because existing certified product profiles and the approved Nysa concurrency pilot retain Fable routing. Spec-linter and Test-author both use Claude Fable 5/medium; no other role route changes.

## 2026-07-16 — Decision 32: Provider-lock handoff is atomic

Category: System change

Concurrent launch certification exposed transient false-stale observations and an ownerless teardown window at the serialized provider lock. Launch now debounces bounded owner-liveness misses, atomically renames its owned provider lock before cleanup, and uses the configured run timeout for both launch- and provider-lock waits; stale or unsafe locks still require operator recovery.

## 2026-07-16 — Decision 31: Escalation-latency and reservation improvements

Category: System change

Evidence from the first 12 Nysa tickets showed operator waits — not code review — dominate cycle time (50% Blocked-Escalated; 70% of stall minutes in a few multi-hour gaps). Planner v4 now consults and appends a durable `factory/rulings.md` (immutability-exempt path) before escalating, checks cross-ticket file boundaries and deploy topology pre-freeze, and the Linear reconciler assigns Blocked-Escalated issues to the API key's viewer for native push notification. The ticket reservation shrinks to the remaining ticket budget (adapter hard stop and telemetry fallback follow it), so a nearly finished ticket launches instead of stalling on flat-reserve arithmetic; daily/global cap checks keep the flat reserve. Spec-linter moves to Claude Haiku 4.5/medium as a cost pilot; Reviewer and Planner routing are unchanged. Railway runbook adds PR-environment reference variables and a project token for non-interactive redeploys.

## 2026-07-16 — Decision 30: Provider-lock recovery is explicit and evidence preserving

Category: Decision

Ordinary launch never reclaims a stale, malformed, or ambiguous product-level provider lock. After publishing KILL and draining recorded processes while holding the launch lock, the kill switch may atomically quarantine only an unchanged, structurally safe lock whose PID/start identity proves stale; every unsafe case remains for operator reconciliation.

## 2026-07-16 — Decision 29: Provider intervals serialize until OS isolation

Category: Decision

Contract 1.2 may retain two independent ticket leases, but a product-level control lock serializes provider intervals from before manifest creation through provider exit and integrity verification. During that interval every new or changed sibling manifest fails closed; parallel provider execution remains deferred until a separate UID or enforced sandbox prevents providers from authoring launcher control state.

## 2026-07-16 — Decision 28: Contract 1.2 stops at Review (supersedes Decision 23 and the approval portion of Decision 26)

Category: System change

Contract 1.2 has no trusted bundle-attestation boundary. Ticket-state therefore refuses transition or materialization of Awaiting Approval and Approved, sequencing never authorizes `AWAIT-MERGE`, and Done remains unavailable pending a separate merge/deploy attestation path. Deterministic ledger projection also refuses every active or ambiguous claim and every `factory/runs/*.pid` record so close-out cannot omit potentially live work.

## 2026-07-16 — Decision 27: State ownership and projection are committed

Category: System change

Operator state materialization is restricted to Backlog-to-Ready and the declared non-sensitive Blocked resume. Linear projection reads the exact ticket ref or committed HEAD and skips files with no committed source. Approval materialization described here is superseded by Decision 28.

## 2026-07-16 — Decision 26: Ticket evidence must be clean and remotely current

Category: System change

Contract 1.2 refuses tracked and untracked worktree dirt before ticket helpers. Supported pending operator fields must pass through materialization before factory-stage transitions. The approval-readiness portion of this decision is superseded by Decision 28: approval overlays now refuse because contract 1.2 has no trusted bundle-attestation path.

## 2026-07-16 — Decision 25: Cutover and config read authoritative sources

Category: System change

Release cutover checks nonterminal kit affinity from each committed exact ticket branch rather than stale registered-main ticket content. Plain configuration clears every allowlisted key before loading, including when `global.env` is absent, so only file-provided machine policy survives.

## 2026-07-16 — Decision 24: Fresh branches and Project removal are explicit

Category: System change

Dispatch creates an exact clean ticket worktree from protected main and uses trusted materialization to create and verify its remote ref before preflight. Removing every Linear Project writes an explicit initiative tombstone into the ignored overlay, preventing stale membership from being reattached and making the ticket ineligible until reassigned.

## 2026-07-15 — Decision 23: Approval materialization is tip-bound (superseded by Decision 28)

Category: System change

From an already committed Awaiting Approval ticket, trusted Linear materialization records Approved, the exact approval marker, and the materialized operator-field version in one single-ticket commit. Sequencing verifies that version and requires the commit at the remote tip; this is auditable bypass detection within the documented same-UID ceiling, not a cryptographic signature.

## 2026-07-13 — Decision 1: Repository-local decision numbering

Category: Decision

Software Factory decisions are numbered independently from Nysa product decisions. This keeps reusable tooling history separate from product history.

## 2026-07-13 — Decision 2: Family-typed Cursor fallback

Category: System change

Codex remains the production primary and Claude Code the checking primary. Optional Cursor adapters preserve those provider families and may be selected only by non-task probes before reservation and task submission; every post-submission failure stops without automatic retry.

## 2026-07-14 — Decision 3: Baseline v3 and durable documentation routing

Category: System change

Durable documentation was centralized under `docs/` and mechanically checked during PR readiness and CI. Plugin AI review is a pre-publication gate for changes to this kit; the factory's independent Reviewer, Narrator evidence bundle, and human approval remain authoritative for product tickets.

## 2026-07-14 — Decision 4: Exact-SHA product release isolation

Category: Decision

Products activate independently from sealed, read-only `releases/<full-sha>` trees under `~/.factory/kits`. External products fail closed without one exact full `KIT_PIN`; the implicit pin is limited to the in-kit conformance test bed, and ticket `Kit-SHA` affinity prevents silent mid-ticket upgrades.

## 2026-07-14 — Decision 5: Stable versioned Hermes boundary

Category: Decision

Hermes uses the bootstrap-managed `~/.factory/bin/factory-launch` contract instead of mutable checkout scripts. Contract `1.0.0` resolves and validates one physical release per invocation, exposes redacted public JSON and a read-only doctor, and treats unknown or error results as dispatch stops.

## 2026-07-14 — Decision 6: Fail-closed activation and rollback (superseded by Decision 10)

Category: Decision

Activation requires maintenance before the shared launch lock, an unexpired exact-tuple receipt, and a recoverable journal; rollback restores retained previous bits but keeps execution stopped until the protected product pin is reverted and revalidated. The acceptance targets are a control-plane outage of at most 5 minutes and full rollback RTO of at most 30 minutes. No automatic prune is allowed until real-ticket retention and rollback evidence exists.

## 2026-07-14 — Decision 7: Release checks have no bypass identity

Category: System change

The active `main` Ruleset has an empty bypass list and binds the required `ci` and `test-immutability` contexts to the GitHub Actions app (`integration_id` 15368). Release installation rejects bypass actors, unbound contexts, missing PR enforcement, or checks reported by another app.

## 2026-07-15 — Decision 8: Relay first migration accepted with measured exception

Category: System change

The isolated real-Hermes canary passed on release `45008d5`, and Relay generation 1 activated release `3b63cc7` before a hash-verified legacy restore and candidate recutover drill. The activation-to-clear interval was 5m50s, so the five-minute outage target remains unmet; formal `factory-kit rollback` begins with generation 2 because the legacy runtime had no previous activation record.

## 2026-07-15 — Decision 9: Parallel development, serialized production

Category: Decision

Multiple focused kit branches and independently certified merged SHAs may progress concurrently, while each product pin change, activation, rollback, and live dispatcher ticket remains serialized and ticket-boundary gated. Relay T-107 proved the managed lifecycle on one Kit-SHA through planner, spec-linter, test-author, builder, reviewer, Narrator, approval, protected merge, production smoke, and Done closeout; the temporary PR #12 check override was restored immediately and is not accepted as protected evidence, while PRs #13 and #14 passed the restored required checks.

## 2026-07-15 — Decision 10: Protected product restore precedes rollback

Category: System change

Failed-cutover recovery keeps maintenance published while product factory services stop and any interrupted activation is reconciled. The protected previous full pin and product tree are restored before `factory-kit rollback`, which runs only when the candidate generation is committed and still active; health is revalidated before maintenance clears.

## 2026-07-15 — Decision 11: Relay generation 4 activated with a current-tree baseline

Category: System change

Relay generation 3 recertified current product tree `395918c` on retained release `3b63cc7`, then generation 4 activated documentation-only successor `35c2e10` through protected pin PR #15. Green draft revert PR #16 reproduces the baseline tree before rollback; health passed, but the 21m59s maintenance interval missed the five-minute target.

## 2026-07-15 — Decision 12: Bounded dispatcher concurrency candidate

Category: System change

Hermes contract 1.1 preserves one-ticket behavior by default and adds an opt-in maximum of two. Atomic opaque ticket leases gate sequencing and launches; they are never automatically stolen, budget reservations retain the global ledger lock, maintenance operations require a full drain, and stale recovery is an explicit operator action under maintenance.

## 2026-07-15 — Decision 13: Concurrency acceptance boundary

Category: Decision

The initial two-ticket pilot keeps one dispatcher, exact per-ticket branches and linked worktrees, sequential roles within each ticket, and sequential PR merges. Missing, stale, or mismatched leases fail before task submission; opaque lease IDs remain confined to trusted helpers; maintenance blocks claims and renewals while matching lease release remains available for drain; activation and rollback refuse until every lease is gone. A merge queue remains deferred until measured merge contention justifies it.

## 2026-07-16 — Decision 13: Fixed Python inside macOS release sandboxes

Release `71c17f2` passed protected CI but local production installation exposed a host-specific incompatibility: `/usr/bin/python3` invoked xcrun, whose cache write was correctly denied outside the Seatbelt workspace. The launcher now selects the fixed Command Line Tools Python binary when present, and install/certification sandboxes expose that same fixed interpreter through their trusted tool directory. Filesystem permissions remain default-deny; no external cache path is allowlisted.

## 2026-07-15 — Decision 14: Explicit primary role model routing

Category: System change

The factory now passes a fixed model and effort level to each primary role rather than inheriting CLI defaults. Cursor fallback remains family-matched and independently allowlisted.

## 2026-07-15 — Decision 15: Narrow operator and bookkeeping exceptions

Category: System change

Spec-linter now shares Reviewer's exact next-semantic-round operator authorization without adding a generic decision system. The immutability gate and reorder helper exempt only `.gitignore` and `context/memory.md` in addition to existing factory directories; `docs/` remains non-exempt, and protected reversals use `chore/<slug>-revert` branches.

## 2026-07-15 — Decision 16: Ticket worktrees are execution truth

Category: System change

Linear-owned priority, initiative, Ready, approval, and resume decisions live in the ignored sync overlay until the trusted `ticket-state` command materializes them. Preflight, sequencing, roles, and Linear projection use the exact ticket worktree or committed branch; successful mutating roles require a clean commit that the wrapper pushes and verifies, while Reviewer remains read-only.

## 2026-07-15 — Decision 17: Manifest-authoritative runtime accounting

Category: System change

Each run manifest records reservation, GO, terminal state, cost, and cost basis. Pre-GO failures cost zero; unknown post-GO cost retains the full reservation; consumers read the ignored effective runtime ledger, and only launcher-managed close-out projection updates tracked ledger history.

## 2026-07-15 — Decision 18: Reliability contract 1.2

Category: System change

Contract 1.2 binds preflight and sequencing to the exact ticket worktree, adds trusted ticket-state materialization and deterministic close-out ledger projection, and enforces role Git postconditions. The standalone launcher retains active 1.0 and 1.1 compatibility; 1.2 inherits 1.1 lease behavior without adding concurrency or supervision.

## 2026-07-15 — Decision 19: Certified destinations and terminal evidence gates

Category: System change

Automatic ticket and role pushes bind to the active generation's certified product origin and fail closed on remote drift. Generic state transitions cover role stages and escalation only; Awaiting Approval and Done remain unavailable until dedicated bundle and merge/deploy attestations can prove their prerequisites.

## 2026-07-15 — Decision 20: Runtime configuration is data only

Category: System change

`factory/ENVELOPE.env` and `~/.factory/global.env` use one sealed, Bash-3.2-compatible whitelist parser across preflight, adapter contracts, and role launch. Executable content, invalid or incoherent limits, and relative global-ledger paths fail before backend probes, run manifests, or task submission.

## 2026-07-15 — Decision 21: Provider-writable output paths are not accounting truth

Category: System change

The launcher durably publishes GO before opening the adapter gate, binds output to a wrapper-held descriptor, snapshots its manifests and registered checkout, and fails closed on persistent mutation. Runtime-ledger is output-only and is rebuilt from tracked durable history plus manifests read from a real no-follow root; missing or invalid telemetry retains the full reservation.

## 2026-07-15 — Decision 22: Runtime exclusion and machine accounting fail closed

Category: System change

Preflight durably initializes the ignored runs root. Ticket-and-role `mkdir` claims are never reclaimed by launch and cleanup removes only its exact owner; a configured global cap holds and validates its ledger across the complete provider interval, serializing globally capped runs and restoring an owned snapshot after mutation. These are portable integrity checks, not hostile same-UID isolation, which requires an OS boundary.

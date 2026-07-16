# Memory — software-factory decision log

Entry format: `## YYYY-MM-DD — Short title`, then `Category: Decision | Preference | System change | Context`, then 1–3 sentences. Mark superseded entries clearly and number durable decisions within this repository.

## Current truth

- This repository is a product-agnostic factory kit; product repositories carry only their factory state and CI integration.
- Durable decisions in this repository use their own numbering, beginning at Decision 1.
- Backend fallback is pre-execution selection: production stays OpenAI-family, checking stays Anthropic-family, and one logical role run submits its task to at most one agent process.
- Stable product and operating truth lives under `docs/`; executable prompts, copied templates, conformance evidence, and vendored material remain colocated with their consumers.
- The repository adopts Nysa Agents baseline v3 as a toolkit with repository, secret, artifact, Git-flow, CI, config-review, and full local PR gates enabled. The canonical verification command is `bash ci/test-all.sh`.
- Live products resolve sealed exact-SHA kit releases under `~/.factory/kits` through the stable `~/.factory/bin/factory-launch` contract; kit merges are candidates until a product-specific certified activation.
- External products require one full `factory/KIT_PIN`, and the first role launch records a durable ticket `Kit-SHA`; only the in-repository conformance test bed has an implicit runtime pin.
- Release activation is maintenance-gated, receipt-bound, and journaled. Failed-cutover recovery keeps `MAINTENANCE`, stops product factory services, reconciles any interrupted transaction, restores the protected previous pin/tree, and calls rollback only for a committed active candidate; automatic pruning is intentionally unavailable.
- Linux and macOS system-Bash verification both feed the required aggregate `ci` status. Relay generation 4 runs documentation-only release `35c2e10` with healthy generation 3 on `3b63cc7` retained as its exact current-tree rollback baseline; the five-minute outage target and formal rollback RTO remain unaccepted.
- On macOS hosts where `/usr/bin/python3` is an xcrun shim, the launcher and release sandboxes use the fixed Command Line Tools Python binary when available; this preserves default-deny Seatbelt behavior without xcrun cache writes outside the sandbox.
- Primary role routing is explicit: Planner uses GPT-5.6 Sol/high; Builder and Narrator use GPT-5.6 Terra/medium; Spec-linter and Test-author use Claude Fable 5/medium; Reviewer uses Claude Sonnet 5/medium. Cursor remains the family-matched fallback.
- Parallel kit branches, worktrees, PRs, and inert candidate releases are supported. Product activation/rollback remains serialized; contract 1.1 keeps live tickets serialized by default and permits an explicit two-ticket lease pilot with exact worktree isolation, atomic budget reservations, and opaque leases confined to trusted helpers. Relay T-107 completed every managed role on kit `3b63cc7` and reached Done through protected implementation and closeout PRs.
- Spec-linter and Reviewer escalation overrides accept only an exact authorization for the next semantic round. Test immutability treats `.gitignore` and `context/memory.md` as exact-file bookkeeping exemptions, while documentation remains contract-significant; revert branches use `chore/<slug>-revert`.
- Ticket execution reads Git-authored state from the exact ticket worktree/committed branch and overlays Linear-owned fields from ignored `factory/linear-map.json`. Mutating roles must commit cleanly; the trusted wrapper non-force pushes and verifies them, while Reviewer must leave Git unchanged.
- Trusted ticket and role pushes use only the exact product origin bound by the active certification receipt. Contract 1.2 stops in Review: transition and materialization refuse Awaiting Approval, Approved, and Done until dedicated trusted bundle and merge/deploy attestation paths exist, and sequencing does not authorize `AWAIT-MERGE`.
- Runtime costs are authoritative in atomic run manifests and materialized into ignored `factory/runtime-ledger.csv`; tracked `factory/ledger.csv` changes only through deterministic close-out projection, which refuses every active or ambiguous claim and `factory/runs/*.pid` record.
- Backward-compatible ledger reduction collapses a legacy durable reservation followed by its identity-matching terminal row; every other conflicting duplicate run ID fails closed.
- Product and machine runtime configuration is parsed as whitelisted data, never sourced as shell. Budget values are positive and coherent, and an explicit global-ledger path must be absolute before any probe, manifest, or task.
- Provider output and same-UID filesystem state are untrusted: durable GO precedes the adapter gate, the runs root and records are opened without following replacement links, output is captured on a wrapper-held descriptor, and only bounded adapter telemetry is consumed, with full-reservation fallback. A product-level control lock serializes provider intervals; any new or changed sibling manifest, persistent claim, owned manifest, global-ledger, or registered-checkout mutation fails the role. Hostile same-UID prevention requires OS isolation; the portable wrapper promises detection, conservative accounting, and no advancement instead.
- Hermes contract 1.2 requires exact ticket worktrees for preflight and sequencing, exposes trusted ticket-state and ledger projection, and keeps the standalone launcher compatible with active 1.0 and 1.1 releases.
- Fresh ticket worktrees are created from protected main and pass through trusted materialization before preflight so their exact remote branch exists. Linear Project removal is represented explicitly in the ignored overlay and clears the effective initiative until reassignment.
- Activation, reconciliation, and rollback validate nonterminal `Kit-SHA` affinity from committed exact ticket branches. Plain configuration clears its full allowlist before optional file loading, so inherited environment values cannot become machine policy.
- Contract 1.2 rejects dirty exact ticket worktrees before preflight, sequencing, state, reorder, or role helpers. Factory state transitions require supported pending operator fields to materialize first; approval overlays are unsupported stop conditions until a trusted bundle-attestation path exists.
- Operator overlays may materialize only kickoff and declared non-sensitive resume state changes; factory phases remain transition-owned. Git-backed Linear projection uses exact ticket refs then committed HEAD, never uncommitted checkout content.
- Provider-lock owners are bound to wrapper PID, process start, and a private token. Ordinary launch debounces transient owner-liveness misses but never reclaims stale or unsafe ownership; normal release atomically renames an owned lock before cleanup, and only the kill switch may quarantine a provably stale unchanged lock after KILL publication and recorded-process drain.

## Log

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

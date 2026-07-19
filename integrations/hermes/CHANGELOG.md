# Hermes compatibility changelog

The contract version covers only the public launcher commands and arguments,
machine-readable schemas, status categories, exit codes, profile/skill
locations, and supported Hermes versions. Human diagnostics and internal
helper output are not compatibility promises.

## 1.3.0 — 2026-07-17

- Adds exact-grammar `ticket-attest` actions for bundle, Linear approval, and
  post-merge Done closeout.
- Binds approval to immutable run, reviewed-SHA, bundle-blob, PR, head, kit,
  and Linear observation evidence before requesting protected auto-merge.
- Verifies the merge commit on authoritative main and configured exact
  post-merge contexts before projecting accounting and recording Done.
- Retains contract 1.2's fail-closed Review boundary and generic state
  transition refusals.
- Requires the matching opaque lease for attestations at concurrency two,
  selects the receipt-bound configured merge method, and binds Done to the
  exact protected approval chain and collision-free merge-commit checks.
- Makes Done own retryable creation and protected auto-merge of the exact
  factory closeout PR; adds terminal `COMPLETE` sequencing so leases release
  only after attested Done reaches protected main.
- Adds sealed `models profiles`, `status`, `plan`, `activate`, `disable`,
  `enable`, and `pin` grammars.
- Adds operator-approved profile-hash activation, default
  `legacy-balanced-v1`, ordered all-six-role portfolio resolution, and
  TTL-bound `credits_exhausted` overrides.
- Pins Kit-SHA and the exact six-role route plan in one committed and pushed
  ticket-branch transaction. Role launches select only their pinned tuple,
  re-probe only that exact route, and never retry after task submission.
- Binds bundle attestations to the committed route-plan blob and digest, policy
  hash, and exact pinned tuple reported by every successful role-run manifest.
- Catalog provenance separates transport, gateway, inference provider, family,
  account route, selectable ID, and reported identity. Cursor exact models are
  distinct routes. Manifest provenance is ready for future scoped budgets, but
  no provider/family/model limits are implemented and the ledger schema is
  unchanged.
- Catalogs Kimi K2.6 as disabled experimental through Claude
  CLI/OpenRouter/Moonshot. It is absent from profiles and has not had a live or
  billed pilot; credential rotation and resolution of same-UID token exposure
  are prerequisites.
- Adds no launcher command for migration, but strengthens terminal sequencing
  so Done requires either the normal protected attestation chain or the
  separately validated, one-time protected-main Contract 1.2 legacy closeout.
- Adds an independent one-time pre-contract terminal-backfill schema for its
  exact authorized batch; absent historical evidence remains null and only a
  complete manual protected merge can satisfy terminal sequencing.
- Waits for bounded process-group drain after kill-switch escalation before
  classifying survivor state and retaining ownership records.

## 1.2.0 — 2026-07-15

- Uses the fixed Command Line Tools Python binary when available so macOS
  Seatbelt runs do not invoke the `/usr/bin/python3` xcrun shim or write its
  cache outside the certified sandbox.
- Adds validated ticket-worktree selection to `preflight` and `next-stage`.
- Adds `ticket-state` as the trusted path from ignored Linear operator state to
  committed ticket state.
- Adds deterministic `project-ledger` projection from atomic run manifests in
  a dedicated close-out worktree.
- Requires mutating roles to commit, non-force pushes and verifies their exact
  tips, and requires Reviewer to leave Git unchanged.
- Binds every automatic ticket or role push to the exact product origin in the
  active certification receipt and rejects missing, multiple, or drifted push
  destinations.
- Limits generic ticket-state transitions to role stages and escalation;
  Awaiting Approval and Done remain withheld pending dedicated evidence gates.
- Durably initializes and validates the ignored run-manifest root, refuses
  automatic ticket-and-role claim reclamation, binds output capture to the
  wrapper, and retains the full reservation for unusable telemetry.
- Holds a configured machine-cap ledger lock across the provider interval,
  validates and restores owned ledger state after mutation, and fails role
  advancement when control state remains changed. This hardening changes no
  public launcher command, schema, or compatibility category.
- Makes provider-lock handoff atomic, debounces bounded transient liveness
  misses, and derives launch/provider lock waits from the configured run
  timeout. Stale and unsafe locks remain operator-recovery conditions.
- Keeps the standalone launcher compatible with active 1.0 and 1.1 releases.

## 1.1.0 — 2026-07-15

- Adds opt-in two-ticket dispatch through `MAX_CONCURRENT_TICKETS=2`.
- Adds atomic claim, renew, and release commands with opaque per-ticket leases.
- Keeps contract 1.0 and the default contract 1.1 configuration serialized.
- Blocks activation and rollback until dispatcher leases are drained; stale
  leases require explicit operator recovery under maintenance.

## 1.0.0 — 2026-07-14

- Certifies Hermes Agent 0.18.2 (build 2026.7.7.2).
- Defines the version-neutral launcher boundary at
  `~/.factory/bin/factory-launch`.
- Resolves the OS account home through `pwd.getpwuid(os.getuid())`, requires
  the physical installed trust-root path, and hard-codes production kits and
  profile roots beneath that account home. Caller HOME/root/profile values and
  installed-path test overrides cannot redirect release or credential state.
- Provides the canonical Bash 3 launcher source, with one-time `active.json`
  selection, physical release containment, full SHA/tree verification, safe
  `PRODUCT_ROOT` parsing, and maintenance refusal for role launches.
- Exports the exact release SHA, tree, physical path, and contract version to
  every selected helper so sealed releases do not depend on `.git`.
- Executes every selected helper through `env -i` with only HOME, a fixed safe
  PATH, TMPDIR, FACTORY_ROOT, and the exact trusted release tuple; caller
  envelope, ledger, global, test, adapter, probe, Python, and Git controls are
  not inherited.
- Optionally adds only the profile-derived `GH_TOKEN`: the launcher reads one
  assignment as data from the owner-owned mode-0600 profile `.env`, never
  sources it, rejects unsafe or malformed files, and ignores caller tokens.
- Enforces exact run grammar, the contract role allowlist, canonical release
  prompts, same-product distinct linked worktrees, non-detached
  `<TICKET_BRANCH_PREFIX><ticket>` branches (documented default `ticket/`), and
  nonempty tasks.
- Rejects symlinks in every managed kits/profile/state/release path component
  before canonicalization and applies multiline/header/JSON-aware redaction to
  all public diagnostics.
- Wraps the existing preflight and next-stage text/exit behavior in the
  versioned public JSON schemas without changing those scripts.
- Exposes close-out test-fix reordering through the stable launcher, restricted
  by an explicit ticket to a distinct linked, non-detached exact-ticket branch
  sharing the registered product's Git common directory.
- Defines the `nysa.software-factory.hermes-doctor/v1` JSON schema and stable
  `ok`, `warning`, `error`, and `unknown` categories.
- Freezes the factory profile layout, project registry keys, canonical SOUL,
  and `factory-dispatch` skill path.
- Records the factory gateway and machine dashboard as separate LaunchAgents.
- Requires presence-only credential reporting and forbids secrets or
  credential-bearing URLs in public output and LaunchAgent definitions.

## Compatibility policy

- Patch releases may clarify human text without changing public fields or
  behavior.
- Minor releases may add optional JSON fields or launcher commands while
  retaining existing behavior.
- Major releases may remove or reinterpret public fields, arguments,
  categories, or exit codes and require an explicit profile migration.
- Any change to a compatibility-sensitive surface listed in `contract.json`
  must update this changelog and either preserve or bump the contract version.

# Hermes compatibility changelog

The contract version covers only the public launcher commands and arguments,
machine-readable schemas, status categories, exit codes, profile/skill
locations, and supported Hermes versions. Human diagnostics and internal
helper output are not compatibility promises.

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

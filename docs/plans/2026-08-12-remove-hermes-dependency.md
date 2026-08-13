# Remove the Hermes dependency

Status: Audited; ready for implementation after the fast-path transaction lands
Branch: `refactor/remove-hermes`
Base: committed tip of `feat/relay-release-fast-path` at `c5cbc5c`

## Decision

Remove Hermes completely from the active Software Factory architecture. The
Factory will own its launcher, contract, configuration, scheduler, diagnostics,
and qualification boundary directly. No current code, service, configuration,
schema, CI suite, or operator instruction may require or invoke Hermes.

This is a breaking Factory Contract 2.0 migration, not a compatibility shim.
Historical Git evidence may retain the word `Hermes`; immutable evidence is not
an executable dependency and must not be rewritten.

## Goal

After cutover, a machine with no Hermes binary and no `~/.hermes` directory can
install, certify, activate, diagnose, schedule, qualify, run, and roll forward a
Factory release.

```text
launchd (15-second interval + run-evidence watch)
  -> ~/.factory/bin/factory-launch
     -> active sealed release/scripts/factory-controller.py
        -> deterministic helpers and provider CLIs
```

There remains one launcher file, not a bootstrap launcher delegating to a second
release launcher. Its bytes must match the Contract 2 release selected by every
active project on that host.

Hermes gateway, dashboard, profiles, skills, hooks, version checks, and canary
are absent from that flow and from the release.

## Scope boundary

### Remove

- The Hermes executable/version compatibility requirement.
- `~/.hermes/profiles/factory`, its project registry, its optional `.env`, and
  every `HERMES_*` launcher/test override.
- The gateway and dashboard LaunchAgent templates.
- The Hermes profile `SOUL.md`, dispatcher skill, and supervisor skill.
- The real-Hermes canary and its fixtures/evidence contract.
- The `integrations/hermes/` source tree.
- Current `nysa.software-factory.hermes*` contract and doctor schema names.
- Hermes-named CI shards, suite IDs, docs, diagnostics, and operator guidance.

### Preserve

- The stable installed entry point `~/.factory/bin/factory-launch`.
- The native per-product `com.factory.controller.<project>` LaunchAgent.
- The deterministic controller, state machine, receipts, budgets, leases,
  provider isolation, evidence, and approval boundaries.
- Sealed historical releases and authenticated historical evidence. They remain
  readable audit records but are not selectable after the Contract 2.0 cutover.
- Historical documents and run logs whose contents are evidence rather than
  current instructions.

### Not in scope

- Removing or modifying the user's global `~/.hermes` installation; it may
  serve unrelated work and is outside this repository migration.
- Reworking role sequencing, provider routing, accounting, or ticket policy.
- Purging Hermes text from immutable historical tickets, run outputs, decision
  logs, interview transcripts, or archived evidence.

## Affected repositories

1. `software-factory` owns the implementation, Contract 2.0, tests, and current
   operational documentation.
2. `nysa-agents-plugin` has two current production-operation assets that name
   Hermes paths/services. Update them in a separate repository-scoped branch
   after the Factory paths and cutover commands are final.
3. `nysa-app`, `relay-factory`, and `nysa` contain historical migration, ticket,
   or research records only. Do not rewrite them. Product `KIT_PIN` and the
   exact in-flight release authorization still change through their normal
   release workflow before the host-wide Contract 2.0 cutover.

## Preconditions

1. Finish and commit, explicitly discard, or separately land all in-flight
   fast-path transaction work in the original worktree. At audit time this is
   `scripts/release-transaction.py`, `ci/release-transaction-test.py`, plus
   edits to `scripts/factory-kit.sh` and `ci/suite-registry.sh`. This branch
   cannot inherit uncommitted work.
2. Rebase or merge the final committed `feat/relay-release-fast-path` tip before
   implementation. Re-run the scope inventory after that update.
3. Inventory every managed `~/.factory/kits/projects/*/active.json` on the host.
   Contract 2.0 is one machine-wide cutover because all products share
   `~/.factory/bin/factory-launch`; partial product migration is forbidden.
4. Keep every production product on the host in maintenance during the first
   Contract 2.0 cutover. Drain and unload every controller and incident reporter,
   and drain leases, provider attempts, broker tokens, qualification consumers,
   and activation journals first.
5. Before any planned rename or deletion, show the exact file list and obtain
   the repository-required confirmation.

## Implementation plan

### 1. Establish the Factory-owned Contract 2.0 boundary

- Move the launcher source from
  `integrations/hermes/bin/factory-launch` to `scripts/factory-launch`.
- Move the public manifest from `integrations/hermes/contract.json` to the
  repository root as `factory-contract.json`.
- Change the contract identity to `nysa.software-factory`, version `2.0.0`.
- Change the doctor output to `nysa.software-factory.doctor/v2`; remove the
  Hermes check rather than retaining a permanently unknown field.
- Update sealed-release verification, installation, certification,
  qualification, launcher self-location, provider pinning, and artifact checks
  to use the two Factory-owned paths.
- Keep `~/.factory/bin/factory-launch` as the installed bootstrap trust root.
  In steady state it remains byte-identical to the sealed launcher in every
  active release on that host.
- Add one narrowly-scoped first-cutover certification rule: while Contract 1.9
  remains active, certification validates and receipt-binds the Contract 2
  candidate launcher without requiring those bytes to be installed yet. Only
  the host-wide cutover transaction may consume such a receipt.

Contract-version edits must be classified, not blanket-replaced:

- Current active-contract allowlists gain or move to `2.0.0`.
- Historical lineage readers retain the exact 1.8/1.9 versions they already
  authenticate and add 2.0 only where a migration suffix can legitimately span
  the cutover.
- Legacy fixtures stay fixed to their original contract versions.
- No helper may relabel historical evidence as Contract 2.0.

### 2. Make active records the sole project registry

- In production, discover slugs only by enumerating fixed managed paths matching
  `~/.factory/kits/projects/<slug>/active.json`; do not accept product paths from
  filenames, arguments, or a replacement environment registry.
- Securely open each active record from that fixed path with `O_NOFOLLOW`; require
  current UID, a regular single-link file, mode `0600`, bounded size, stable
  inode/bytes, duplicate-key rejection, and the exact slug. Canonicalize its
  `product_path`, verify the physical Git root, then require the existing
  certification receipt to bind that exact path before reading product-owned
  files. The active record is not trusted before this sequence completes.
- Remove the second copy at
  `~/.hermes/profiles/factory/projects/<project>.env` and the equality check
  between that registry and `active.json`.
- Replace `scripts/operator-console.py`'s Hermes `.env` discovery with the same
  slug-only active-record enumeration; delete its environment parser/constants
  and update its focused tests and README.
- Redesign qualification prepare/restore/upgrade publication ordering so it no
  longer creates `root/profile`, a project `.env`, or a registry-last marker.
  Preserve its crash/replay and torn-tree refusal guarantees with `active.json`
  as the last published authority. Add an explicit 1.9-to-2.0 qualification
  path; do not silently relabel an old qualification record.
- Remove `PROFILE_DIR`, `REGISTRY_FILE`, `HERMES_FACTORY_PROFILE`, qualification
  profile overrides, profile fixtures, and profile doctor output.

This deletes duplicate authority while strengthening the remaining fixed-path
record and preserving its receipt binding.

### 3. Use the GitHub CLI credential store directly

- Remove parsing of `GH_TOKEN` from the Hermes profile `.env` and remove the
  launcher's general `GH_TOKEN` propagation.
- Do not call `gh auth token` or materialize a token in shell/Python. For GitHub
  HTTPS Git operations, reuse the existing ownership/mode-validated fixed `gh`
  path as `gh auth git-credential` with `GH_PROMPT_DISABLED=1`, bounded timeouts,
  and captured output. Refactor model fallback/migration to accept that helper
  capability directly instead of a token over file descriptor 9.
- Direct `gh` commands continue using the real account home under the launcher's
  clean environment. They fail closed at their existing action boundary; do not
  make generic ticket preflight depend on live GitHub authentication.
- Doctor may run one bounded, output-suppressed `gh auth status` probe and report
  only `ok`, `warning`, or `error`. Preflight remains warning-only and removes
  only its Hermes-profile fallback.
- Prove both paths from the exact LaunchAgent environment: direct `gh` commands
  and Git's validated `gh auth git-credential` helper. If Keychain access is not
  available there, stop the migration; do not create a second token store.

### 4. Remove Hermes services, prompts, and canary

- Delete the Hermes gateway and dashboard plist templates.
- Delete the Factory Hermes `SOUL.md`, dispatcher skill, supervisor skill, and
  profile fixture.
- Delete `scripts/real-hermes-canary.py` and its CI cases.
- During the host cutover, boot out the installed
  `com.nysa.hermes-factory-gateway` and `com.nysa.hermes-dashboard` jobs and
  prove their labels and processes are absent. After the new controllers are
  healthy, remove only the installed Factory-owned plist/profile files included
  in the reviewed deletion list. Leave unrelated user Hermes state untouched.
- Move the current Railway credential convention from `~/.hermes/secrets` to
  `~/.factory/secrets`. Do not automatically copy, read, or print an existing
  credential; require the operator to provision it at the new owner-only path.
- Do not replace that script with a new framework. The existing native
  controller LaunchAgent, isolated qualification environment, launcher contract
  suite, Doctor controller check, and sandbox smoke already cover the surviving
  production path.
- Add one focused macOS smoke that loads the native controller plist against an
  isolated Factory home with no `.hermes` directory and a sentinel `hermes`
  executable that fails if invoked, observes one reconciliation, then unloads
  it.

### 5. Rename the current contract and CI surface

- Rename `ci/hermes-contract-test.sh` to `ci/factory-contract-test.sh` and keep
  its launcher security, release selection, doctor, concurrency, and command
  coverage.
- Rename the CI suite/shard identifier from `hermes-contract`/`hermes` to
  `factory-contract`/`contract`; update suite registry, group assignment,
  changed-suite selection, workflow matrix, and release-evidence aliases
  atomically.
- Rename `docs/hermes-integration.md` to `docs/factory-runtime.md` and rewrite it
  as the Contract 2.0 release/runtime reference.
- Delete the obsolete `docs/hermes-orchestrator-plan.md`; Git history remains
  the archive.
- Move `integrations/hermes/CHANGELOG.md` to
  `docs/factory-contract-changelog.md`, retaining supported contract history
  without keeping an active Hermes integration surface.
- Update README, architecture, setup, runbooks, roles, operator console docs,
  artifact policy, and current TODO language.

Do not mechanically rewrite archived evidence. The static acceptance check uses
an explicit historical-path allowlist so a new live Hermes reference cannot hide
inside broad exclusions.

### 6. Extend the fast-path transaction for one host-wide cutover

- Rebase this branch after the in-flight `factory-kit release setup/resume`
  transaction lands. Extend that helper and its signed plan/journal schemas;
  do not add a parallel cutover command or second preview-hash mechanism.
- When the candidate is Contract 2.0, `release setup` must enumerate every
  managed active project on the host and refuse a partial set. Its one approval
  hash binds:
  - every current active record, generation, release, product path/tree, and
    certification receipt;
  - the Contract 2 candidate SHA/tree/manifest and launcher bytes;
  - every native controller and optional incident-reporter plist identity;
  - maintenance plus drained controller, lease, provider, broker, qualification,
    and activation-journal state for every product;
  - clean-environment GitHub authentication readiness;
  - the installed launcher bytes and the Contract 1.x no-return boundary.
- `release resume` revalidates that exact basis under the existing global,
  project, and launch locks, then advances one fsynced machine-cutover journal
  through real actions: unload all native/legacy jobs; install the sealed
  launcher; write a durable owner-only `contract-floor` record requiring major
  version 2; switch every active record; reload native jobs; run Doctor for
  every project; and remove the reviewed Factory-owned Hermes state.
- This sequence is crash-safe and convergent, not atomic across files and
  `launchctl`. Inject an interruption after every phase and prove replay reaches
  one coherent state.
- Before the contract-floor phase, failure may restore the old launcher and
  remain in maintenance. At and after that phase, reconcile and explicit
  rollback must reject every Contract 1.x record and fix forward to 2.x. Put one
  shared floor check in activation, interrupted reconciliation, rollback, and
  launcher active-record selection.
- After the first healthy cutover, ordinary rollback is allowed only between
  Contract 2.x generations.

### 7. Update downstream operator guidance

After the Factory implementation and commands are final, update and release
`nysa-agents-plugin` so its production handoff prompt and operator playbook use
`scripts/factory-launch`, `factory-contract.json`, and the native controller
only. Keep this as a separate commit and PR because it is a different repository.

Before the host cutover, every managed product stages the exact Contract 2.0
`KIT_PIN` through its existing protected in-flight release authorization and
certification flow. The single host transaction activates them together.
Historical product documents remain unchanged.

## Verification plan

### Static dependency gate

Add one small CI check that fails when a live path contains any of:

```text
hermes
HERMES_
~/.hermes
.hermes/
nysa.software-factory.hermes
```

Register this check as a normal CI suite and scan tracked text
case-insensitively. Its allowlist is limited to exact immutable historical
paths or exact frozen lines, including dated evidence and this completed plan.
For `context/memory.md`, only prior dated log entries may match; Current truth
and Operating contract receive no exception. Current scripts, manifests, CI,
roles, templates, README, setup docs, runbooks, and non-archived reference docs
receive no exception.

Also assert:

```bash
test ! -e integrations/hermes
test -x scripts/factory-launch
test -f factory-contract.json
```

### Focused behavioral checks

1. Run the renamed Factory contract suite for launcher, Doctor, install,
   certification, and active-record security.
2. Run focused operator-console, qualification-environment,
   release-transaction, model-control/fallback, preflight, and CI-scope suites.
3. Prove install/certify/host-cutover/reconcile in an isolated home where
   `.hermes` never exists and a sentinel `hermes` executable fails the test if
   invoked. Require active-record-only discovery, one native reconciliation per
   project, no Hermes Doctor field, and no Hermes process.
4. Prove active-record discovery rejects symlinks, hard links, group/world
   permissions, oversize input, duplicate keys, slug mismatch, non-Git paths,
   receipt mismatch, and replacement races.
5. Prove direct `gh` and Git's validated `gh auth git-credential` helper work in
   the clean LaunchAgent environment; missing authentication fails at the
   relevant GitHub action, prompting is disabled, and no token appears in argv,
   environment snapshots, stdout/stderr, manifests, journals, or evidence.
6. Prove Contract 1.9-to-2.0 qualification and release lineage preserve old
   evidence exactly while rejecting gaps, duplicate/reordered migrations, and
   Contract 2 relabeling.
7. Inject failure after every host-cutover phase. Before the contract floor,
   replay may restore the complete 1.9 host; afterward it must retain
   maintenance, refuse every 1.x active/rollback target, and converge every
   project to the exact approved 2.0 state.
8. Prove setup refuses live work, a partial project inventory, stale approval,
   changed launcher/active/plist bytes, missing services, or unavailable GitHub
   authentication.

### Repository gates

```bash
bash ci/test-all.sh --changed-or-defer origin/main HEAD
scripts/repo-check
scripts/secret-scan
```

Because this changes the launcher, public contract, suite registry, release
layout, qualification, and CI topology, full Linux and macOS required CI is
mandatory before installation. Local deferral is not a full pass.

### Live qualification gate

Before production:

1. Install the exact protected-main Contract 2.0 release in an isolated home
   with no Hermes installation or state.
2. Complete the native controller macOS smoke.
3. Run a sealed qualification cohort through provider admission and at least
   one terminal ticket boundary.
4. Interrupt and replay activation once.
5. Confirm the measured control-plane outage remains within five minutes.
6. Keep production in maintenance unless every gate passes.

## Acceptance criteria

- A clean machine needs Factory, provider CLIs, Git, Python, Bash, Node/npm as
  declared by the product, GitHub CLI, and native `launchd`; Hermes is absent.
- No current executable, config, service, manifest, schema, CI suite, test
  fixture, or operator instruction references Hermes.
- `~/.factory/bin/factory-launch` resolves only Factory-owned paths and
  `active.json` authority.
- Every active project on a host switches in one transaction, and the durable
  contract floor prevents launcher, reconcile, activation, or rollback from
  selecting Contract 1.x afterward.
- Legacy Hermes LaunchAgent labels, processes, Factory profile files, and
  Factory-owned Hermes credential paths are absent after cutover; unrelated
  user Hermes data is untouched.
- Contract 2.0 preserves all existing security, accounting, evidence, approval,
  concurrency, and crash-recovery invariants.
- Full required CI passes on Linux and macOS at the exact protected-main SHA.
- The first production cutover either makes every managed product healthy on
  Contract 2.0 or keeps the host in maintenance for journaled recovery; after
  the contract floor it never falls back to Hermes implicitly.

## Known cost and risk

- This is intentionally broad: the current tree has 79 tracked files containing
  `Hermes`, 48 files containing Contract 1.9 checks, and 338 runtime contract-
  version references across scripts and the launcher. Most are fixtures or
  historical compatibility, but the active/historical distinction must be
  reviewed line by line.
- The machine-global launcher makes mixed 1.x/2.x activation unsafe. That is the
  main operational risk and the reason for one host-wide approval, full-host
  maintenance, interruption replay at every phase, and a durable contract floor.
- The payoff is one fewer external runtime, one less service pair, no duplicate
  product registry, no profile prompt surface, and no stale Hermes version
  compatibility matrix.

## Audit record

Three read-only subagents audited the draft against the branch:

1. Architecture/data flow found the global-launcher/per-project-record conflict,
   the false two-launcher diagram, incomplete qualification migration, and the
   fact that launcher/plist/pointer changes cannot be atomic. Resolved with one
   host-wide journaled transaction, the single-launcher model, explicit
   qualification redesign, and crash-safe phase semantics.
2. Security/compatibility found executable 1.x rollback paths, weak
   `active.json` bootstrap validation, raw-token extraction risk, missing live
   LaunchAgent cleanup, and a 1.8/1.9-only lineage gate. Resolved with one shared
   contract-floor check, secure fixed-path record parsing plus receipt binding,
   direct GitHub credential-helper use, live-state removal, and cross-version
   lineage tests.
3. Minimal-scope/test review found a duplicate cutover mechanism, omitted
   operator-console migration, an unnecessary generic preflight auth probe, and
   an overbroad static allowlist. Resolved by extending the incoming release
   transaction in place, deleting the old console registry parser, keeping
   preflight warning-only, and registering an exact-allowlist CI gate.

No audit requested an additional runtime dependency or replacement orchestrator.

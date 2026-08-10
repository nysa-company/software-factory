# Factory setup

This file is the checklist for turning the kit into a running factory for one product. Every blank below must be filled before the first ticket runs. The validator section at the end is the pass/fail gate.

Read [architecture.md](architecture.md) first. It defines the kit/product boundary, role flow, trust boundaries, and budget model assumed by this checklist.

## Step 1 — Product repo

- Create the product repo as a **sibling folder** (never nested inside another repo). Do NOT copy kit scripts into it — the engine model in `docs/architecture.md` is the contract.
- Create `factory/` with: `ENVELOPE.md` (filled from `envelope/ENVELOPE.template.md`), `ENVELOPE.env`, `PROJECT.env`, `KIT_PIN`, an executable certification script, and empty `initiatives/` and `tickets/` directories.
- Ignore `factory/linear-map.json`, `factory/.linear-sync.lock`, `factory/.envelope.lock/`, `factory/envelope-overrides/`, and `factory/envelope-override-consumptions/`; they are runtime operator state and must never dirty the registered checkout.
- Ignore `factory/runs/`, `factory/.active-runs/`, `factory/.provider.lock/`, and `factory/runtime-ledger.csv`; preflight or the first normal Linear reconciliation durably initializes the real, no-follow runs root, manifests are atomic local run truth, claims and the provider lock are runtime exclusion state, and the CSV is their rebuildable view over the tracked durable ledger.
- Write exactly one lowercase, full 40-character SHA to `factory/KIT_PIN`. External products never use an abbreviated SHA or the in-kit conformance exception.
- Add one repository-contained executable path to `factory/PROJECT.env`, for example `CERTIFY_SCRIPT=factory/certify.sh`. The script must run the product checks without changing the tracked product tree.
- Declare exactly one `PREVIEW_PROVIDER=railway` or `PREVIEW_PROVIDER=none`. `none` is allowed only with narrow `NONVISUAL_PATHS`; each ready ticket must also declare exact `Builder ownership: path[, path] only` so qualification can prove the work cannot require a missing preview before any provider call.
- When that script uses the managed `scripts/secret-scan`, certification stages its checksum-verified pinned scanner into the disposable product copy before entering the network-denied sandbox.
- Configure exactly one `origin` push URL. Certification records that literal URL as receipt `product_origin`; trusted contract 1.2 writes refuse a different or additional push destination.
- Set exact `GH_REPO=owner/repository`. For contract 1.3, also set nonempty `DONE_REQUIRED_CHECKS=name-one,name-two` to the unique exact GitHub status/check names that must succeed on the merge commit; commas delimit names and surrounding whitespace is invalid. Set `AUTO_MERGE_METHOD=squash`, `merge`, or `rebase` to the repository's protected merge strategy.
- If live preview topology needs a product-specific deterministic check, set `PREVIEW_PREFLIGHT_SCRIPT` to one executable repository-contained path. It receives bounded JSON only after every deployment reports the exact reviewed SHA and must return head-bound pass/wait/fail JSON; omit it when exact deployment identity is sufficient.
  Input is `{"schema":"nysa.software-factory.preview-preflight-input/v1","ticket":"T-NNN","head":"<sha>","previews":[...]}`. Output contains exactly `schema` (`nysa.software-factory.preview-preflight/v1`), the same `head`, `status` (`pass`, `wait`, or `fail`), `reason` (`null` only for pass), and a non-secret `evidence` object.
- Products with repository paths that can never affect a deployable or visual
  surface may set `NONVISUAL_PATHS` to a comma-separated list of distinct,
  non-overlapping directory prefixes ending in `/`. Railway remains required
  by default. The exemption applies only when GitHub reports every semantic PR
  file under those prefixes and every other file is exact current-ticket
  Factory metadata; removed, renamed, copied, mixed, empty, malformed, and
  unknown diffs fail closed. Do not use this for backend, HTTP, UI, shared, or
  otherwise deployable code.
- Leave `MAX_CONCURRENT_TICKETS` absent to use the contract default. Contracts
  1.1 through 1.5 default to `1` and accept integers through `4`; Contracts
  1.6 and 1.7 default to `4` and accept `1` through `6`; Contract 1.8 defaults
  to `4` and accepts `1` through `4`. Set a value above `1` only after a bounded
  concurrency pilot is approved. This is the one coupled worktree/provider
  capacity setting. Contract 1.8 at capacity above one requires exact
  owner-approved subscription concurrency for every enabled Cursor, Claude
  Code, and Codex route; certification and activation refuse instead of
  silently using the legacy provider lock. Older contracts and capacity one
  retain the serialized path. An API route may use isolated parallel admission
  only after exact Contract 1.6 owner activation.
- Copy exactly three CI files (GitHub requires workflows and helpers to live in the repo they run on): `ci/test-immutability-check.sh` and `ci/lightweight-change.sh` → `.github/scripts/`, and `ci/github-actions-ci.template.yml` → `.github/workflows/ci.yml`. Set `TEST_PATHS` from `PROJECT.env` and review the helper's narrow inert-metadata allowlist for the product. Give the workflow one read-only fine-grained `FACTORY_REPOSITORY_TOKEN` for `nysa-company/software-factory`; it is consulted only when the committed diff adds or changes `factory/QUALIFICATION.json`. That path checks out the exact committed `KIT_PIN` with persisted Git credentials disabled and runs its shared manifest parser before merge. Existing product repositories must receive template updates explicitly; kit updates do not rewrite instantiated CI.
- The copied local readiness classifier invokes the same parser from the
  installed exact-pin release and committed product blobs; it does not validate
  dirty checkout bytes:

  ```bash
  bash .github/scripts/lightweight-change.sh origin/main HEAD
  ```

  Exit `0` means inert metadata, exit `1` requires broad product tests, and exit
  `2` is a hard readiness refusal. A present changed manifest must strictly
  match the committed pin before either test classification is returned.
- Write `factory/ENVELOPE.env` from the filled `ENVELOPE.md` — plain `KEY=value` lines for `PER_RUN_BUDGET_USD`, `PER_TICKET_BUDGET_USD`, `PER_RUN_MAX_TURNS`, `PER_RUN_TIMEOUT_MIN`, `DAILY_CAP_USD`. Optional `<ROLE>_PER_RUN_BUDGET_USD`, `<ROLE>_PER_RUN_MAX_TURNS`, and `<ROLE>_PER_RUN_TIMEOUT_MIN` keys override one role's attempt limits; normalize role hyphens to underscores, and omit a key to inherit its default. Money values are capped at $1,000,000 with six decimal places, turns at 1,000, and timeout at 1,440 minutes. The validator checks the two files agree. `ENVELOPE.env` and `~/.factory/global.env` are parsed as whitelisted data and must never contain shell commands or expansions.
- Cursor interprets the selected timeout as a soft inactivity window and keeps
  a nonextendable hard limit at twice that duration. Only normalized structured
  Cursor events extend the soft window; arbitrary log output and timestamps do
  not. Other adapters continue to use the selected timeout as their hard limit.
- If `GLOBAL_DAILY_CAP_USD` is configured, keep its global-ledger parent as a real local directory. The wrapper validates the ledger and holds its exact-owner lock across each complete provider interval, so all globally capped runs on that machine are intentionally serialized.
- Product docs the factory needs (written per product, not in the kit):
  - `docs/engine-spec.md` — data model, durable job model (retries, idempotency, crash recovery), external-action policy, connector safety (sandboxed/allowlisted sends until production).
  - `docs/acceptance/<first-slice>.md` — the vertical-slice acceptance spec the backlog is cut from.
  - `docs/conventions.md` — short; grows from the walking skeleton.

## Step 2 — Envelope

Fill every blank in `factory/ENVELOPE.md`: per-ticket budget (USD and max turns), daily cap, retry ceilings, escalation rules, exit thresholds. Then set matching hard caps in provider consoles and review Cursor usage controls before activating a profile that prioritizes those routes. Subscription quota/credit telemetry is incomplete and is not a safe automatic routing signal. Cursor CLI has no documented per-run dollar stop: the ledger always keeps the full run reservation. Approved token telemetry plus a dated pricing snapshot may add an observational estimate, but never reduce that reservation. Route-plan provenance can support future provider/family/model limits, but none are implemented and the ledger schema is unchanged.

## Step 3 — Keys and secrets

- Keep route/account and product-runtime credentials separate. Cursor uses a one-time local `agent login` (or `CURSOR_API_KEY` for unattended infrastructure); never put credentials in kit or product config.
- Keep both Cursor session files owner-only. Task-free readiness copies them
  into a disposable owner-only home before invoking Cursor, so CLI-generated
  configuration rewrites cannot change the source files used by later roles.
- On macOS, Claude Code may retain an interactive login only in Keychain, which
  cannot be copied into isolated Factory attempts. Run `claude setup-token`,
  save its single token line as `~/.factory/claude-oauth-token` with mode
  `0600`, and never print, commit, or place it in `global.env`. A present token
  is materialized into each disposable Claude config; an absent token preserves
  the legacy owner-only `.claude/.credentials.json` path, and an unsafe token
  fails closed rather than falling back.
- Keep Kimi disabled. No live or billed pilot has run. Before any pilot, rotate its credential and address the residual same-UID token exposure with a credential broker or OS isolation.
- Secrets live only in GitHub Actions secrets and the hosting platform. No `.env` in git, ever.

## Step 4 — Linear

Set up the shared Software Factory team per `docs/workflows/linear.md`: compact phase-level workflow states, the factory issue template, risk/external labels, and one Linear Project per file in `factory/initiatives/`.

Run `scripts/linear-sync.py --factory-root <product-repo> --setup` once to create or verify the team, states, labels, and Projects. After the selected release and stable launcher are active, install and explicitly enable the per-product three-minute job with `bash scripts/factory-kit.sh linear-sync-service enable --project <project> --product <absolute-product-path>`. Use `disable` for a product that must remain inactive; disabled means persistently disabled and unloaded, not merely absent from `launchctl print`. Linear owns operator priority, Ready, approval, unblock, and Project membership; Git owns execution details. The reconciler is asynchronous so Linear never sits in the sequencer control path. Mint the Linear API key (`~/.hermes/secrets/linear-api-key`) from the on-call operator's own account, since that's the account Linear auto-assigns and notifies on Awaiting Approval and Blocked-Escalated tickets.

Fresh-map recovery adopts only Projects with one durable initiative identity and
fails on ambiguity or an unidentified same-name Project. A pre-marker mapped
Project is backfilled only when its exact ID still exists, its team and
Git-owned name match, it has no marker-like line, no other Project carries the
initiative marker, and an immediate re-read is unchanged. The update prepends
the marker without changing prior content and is never retried in the same
sweep; a later sweep observes an update whose response was lost. Once the
mapped marker exists, unmarked same-name duplicates warn in Doctor but are not
mutated or allowed to redirect tickets. Project name and marker content are
Factory-owned; operators retain Project status and target date. For a new selected
ticket after setup, use `scripts/linear-sync.py --factory-root <product-repo>
--ticket T-NNN --initialize`; qualification preparation invokes that bounded
path for every selected ticket against its bound lane-local map. Fresh isolated
preparation requires `--operator-map-seed <absolute-owner-only-linear-map.json>`
(or `FACTORY_QUALIFICATION_OPERATOR_MAP_SEED`) and fails closed if the seed is
absent, ambiguous, unsafe, malformed, or contains secret-bearing fields. It
copies the validated seed into owner-only qualification authority, where the
mutable map, locks, clear intents, and runtime ledger remain outside the sealed
product checkout. Each selected initialization records a bounded create intent
before mutation, persists a returned issue identity before observation, and
never repeats an uncertain create. It confirms the exact intended Project and
complete non-canceled state before persisting the issue's operator observation,
consumes only its matching one-use clear, and does not inventory historical
issues. A Linear rate limit is
persisted canonically as `linear_rate_limited retry_after_seconds=N`, shared by
credential identity, and keeps provider admission closed without API calls
until expiry.

## Step 5 — Hermes release boundary

- Create `~/.factory/bin` and `~/.factory/kits`. Install
  `integrations/hermes/bin/factory-launch` at
  `~/.factory/bin/factory-launch` only through an explicit bootstrap or
  contract migration.
- The launcher intentionally replaces caller `PATH` with its contract
  allowlist, which includes `~/.factory/bin` but not `~/.local/bin`. If
  provider CLIs are installed outside the allowlist, keep every active product
  in maintenance and fully drain controllers, leases, attempts, broker tokens,
  and live qualification consumers. Preview the installed candidate's exact
  three-CLI transaction, review its physical targets and approval hash, then
  apply that same hash:

  ```bash
  bash scripts/factory-kit.sh provider-cli-pin plan --sha "$FACTORY_SHA" \
    --claude-bin "/absolute/versioned/claude" \
    --codex-bin "/absolute/versioned/codex" \
    --cursor-bin "/absolute/versioned/agent" \
    --operator-id "<operator-id>"
  bash scripts/factory-kit.sh provider-cli-pin apply --sha "$FACTORY_SHA" \
    --claude-bin "/absolute/versioned/claude" \
    --codex-bin "/absolute/versioned/codex" \
    --cursor-bin "/absolute/versioned/agent" \
    --operator-id "<operator-id>" --approve-hash "<plan approval_sha256>"
  bash scripts/factory-kit.sh provider-cli-pin check --sha "$FACTORY_SHA"
  ```

  The transaction updates only the three exact global pin keys and the
  `~/.factory/bin/{claude,codex,agent}` links. It binds the sealed candidate,
  the exact compatible active-release allowlist, raw link targets, physical
  executable hashes, parsed versions, and fixed flag contracts. An absent link
  is only an unmanaged warning before any pin or receipt exists; a missing or
  dangling managed target is an error. Do not widen PATH, use `current` or a
  version range, hand-edit the receipt, or resume dispatch until Doctor reports
  the exact pins ready.
- For a product with certification plan v2, pin its exact Node/npm runtime
  without changing the system-wide Homebrew link:

  ```bash
  bash scripts/factory-kit.sh runtime-pin \
    --product "<absolute-product-path>" \
    --runtime-bin "<absolute-node-bin-directory>"
  ```

  The operation reuses the launcher's fixed PATH priority by atomically linking
  verified `node`, `npm`, and `npx` executables into `~/.factory/bin`. It reads
  the shared strict product plan, refuses version or path drift before replacing
  an existing pin set, and verifies the installed tuple under the exact sealed
  launcher PATH. Run it before readiness or qualification preparation; those
  gates, certification, activation, and launch still independently fail closed
  on tuple drift.
- Create the dedicated factory profile at
  `~/.hermes/profiles/factory`. Install the canonical `SOUL.md` and
  `skills/factory-dispatch/SKILL.md` from `integrations/hermes/templates/`.
- For Contract 1.8, instantiate
  `scripts/launchd/com.factory.controller.plist.template` with the exact
  project, home, and product paths and load it as a separate LaunchAgent.
  Keep its `Interactive` process type: macOS background QoS can exhaust the
  unchanged bounded provider-readiness probes before ticket work starts.
- Pre-promotion live qualification uses the owner-only sealed environment
  prepared by `scripts/qualification-environment.py`; it does not replace the
  installed launcher or production activation record. Its generated marker
  and launcher-supplied root binding are required for subscription provider
  attempts; never construct that environment by hand. Use
  `--takeover-project <project>` only for a protected production-successor
  manifest after unloading and draining the installed controller. That mode
  requires the clean activated source checkout to match its authenticated tree
  and current protected main to contain that source commit. It accepts a clean
  local-only linked product worktree based on current protected main, with only
  the candidate pin, successor manifest, and selected-ticket dependency edits.
  Its sealed helper environment binds the canonical live
  Linear map. It validates and reuses canonical authenticated passports
  and provider accounting under their existing lock rather than copying them.
  A fresh isolated worktree may omit ignored runtime directories; the preparer
  alone creates physical owner-only `factory/runs/`. It rejects noncanonical
  selected-ticket freeze metadata and any selected dependency pair before
  sealing, so the restart barrier cannot wait forever. Supply its canonical
  owner-local Linear map with `--operator-map-seed`; the preparer binds a
  lane-local copy and runtime ledger, initializes only the selected cohort,
  and proves the product is still clean before it publishes the environment.
  Preparation is serialized per project. A retry reruns live readiness and
  resumes only a byte-exact pristine write prefix or a completed lost-response
  replay; it never deletes a torn, mismatched, unexpected, or active lane.
  The sealed release is renamed from a same-directory temporary tree, and the
  environment record is durable before the final profile registry. Continue
  to use `--restore` only for its existing signed safe-pause boundary; there is
  no partial-lane cleanup command.
  It also provisions the exact historical run artifacts named by those
  passports from its owner-only retained closure; any absent or altered
  manifest, output, or progress journal stops preparation before a paid role.
  The preparer also fails before admission when the chosen root is too long for Cursor's
  isolated attempt scratch. `--upgrade` is limited to a fresh isolated
  qualification; a takeover binds one frozen candidate. A successor upgrade
  also requires an authenticated source-rooted passport for every selected
  ticket before it changes operator state, initializes Linear, seals the
  release, or replaces activation. If any selected ticket is candidate-native
  or has no source passport, has malformed migration history, or carries
  pre-activation charge/completion evidence from the candidate or evidence from
  a foreign release, use a fresh ordinary qualification instead of retrying or
  rewriting its history. An already-active candidate accepts its own exact
  lineage evidence on an idempotent restart.
  If a failed isolated predecessor stopped after issuing Planner receipts but
  before preflight, commit and protect the successor manifest, candidate pin,
  and exact `preprovider-branch-resets.json`; prepare that unchanged successor
  product; then run the candidate helper once:

  ```bash
  python3 scripts/qualification-environment.py \
    --factory-root "<clean-successor-factory-checkout>" \
    --product-root "<prepared-successor-product-checkout>" \
    --project "<successor-project>" \
    --root "/private/tmp/nysa-sf-qualification.<successor>" \
    --preprovider-source-project "<predecessor-project>" \
    --preprovider-source-root "/private/tmp/nysa-sf-qualification.<predecessor>"
  ```

  Both controllers and provider state must be drained. The executing helper
  must be byte-identical to the sealed successor copy. A completed or partial
  digest journal in the successor's durable controller is the only restart
  authority; do not remove it, edit claims, delete branches, or move worktrees
  by hand.
- Add `~/.hermes/profiles/factory/projects/<project>.env` with
  `PRODUCT_ROOT=<absolute-product-path>`. The stable launcher ignores `KIT_DIR`
  from legacy registry files and resolves the active release itself. Registry
  files contain paths only, never credentials.
- Keep the factory gateway and machine dashboard as separate LaunchAgents.
  Do not embed secret-bearing environment keys in either plist.
- Install the pinned release:

  ```bash
  bash scripts/factory-kit.sh install --repo "$PWD" --sha "<full-sha>"
  ```

- Before certification, compare the installed
  `~/.factory/bin/factory-launch` with the sealed release copy. If they differ,
  drain controller and provider work, retain the current launcher as the
  rollback artifact, and atomically install the exact sealed executable. Only
  after maintenance is published, controller pause has succeeded, and provider
  status proves every attempt and reservation drained, run this fail-fast
  native replacement against the installed release:

  ```bash
  (
    set -eu
    candidate="$HOME/.factory/kits/releases/<full-sha>/integrations/hermes/bin/factory-launch"
    installed="$HOME/.factory/bin/factory-launch"
    test -f "$candidate"
    test ! -L "$candidate"
    test -x "$candidate"
    test -f "$installed"
    test ! -L "$installed"
    rollback="$(mktemp "$HOME/.factory/bin/factory-launch.rollback.XXXXXX")"
    cp -p "$installed" "$rollback"
    printf 'rollback=%s\n' "$rollback"
    temporary="$(mktemp "$HOME/.factory/bin/.factory-launch.XXXXXX")"
    trap 'rm -f "$temporary"' EXIT
    install -m 700 "$candidate" "$temporary"
    cmp -s "$candidate" "$temporary"
    mv -f "$temporary" "$installed"
  )
  ```

  Keep the printed `$rollback` path with the release evidence. Stop if any
  command fails; do not remove the rollback artifact or resume the lane.
  Certification and activation refuse any byte mismatch; never patch the
  installed launcher independently.

- Migrate each legacy release-pinned Linear LaunchAgent exactly once after its
  product has activated this release. Keep maintenance published and run the
  matching `linear-sync-service enable` or `disable` command. The command
  drains both known Linear lock generations, snapshots the prior plist and
  load/disable state, and rolls back automatically
  on any bootout, write, enable/disable, bootstrap, or verification failure,
  and leaves maintenance in place. An absent native override is first
  normalized to verified explicit-enabled ownership and reported as
  `LINEAR SYNC SERVICE OWNERSHIP ADOPTED`; that committed semantic no-op is the
  rollback baseline even if the later migration fails. If adoption cannot be
  verified, no plist or load-state migration is attempted and maintenance must
  remain. Do not hand-render the plist or call
  `launchctl` as part of activation. Verify Doctor reports either an enabled,
  loaded stable route or a disabled, unloaded stable route before resuming.

- For a release migration, merge the protected product PR containing
  `factory/KIT_PIN` and the complete canonical
  `factory/migrations/inflight-release/<target-sha>.json` authorization before
  certification. Fetch canonical protected main, require its exact SHA and
  tracked tree, and certify that exact protected-main tuple. Install the sealed
  release first. On an active host, publish maintenance, recover any named
  stale dispatcher leases, drain controller and provider work, and install the
  sealed launcher before the protected merge. On an inactive replacement,
  keep dispatch disabled while installing the launcher and certifying; also
  publish maintenance on the old active host and drain its controller and
  provider work before the protected merge, then preserve that maintenance
  through cutover. Activate only the exact certified tree. Any later product
  commit invalidates the certification and requires recertification. SSH host
  aliases are not a trusted kit origin; use a clean checkout whose remote
  canonicalizes to
  `github.com/nysa-company/software-factory`.

- Before certifying a Contract 1.8 product with
  `MAX_CONCURRENT_TICKETS` above one, enter maintenance and drain every role,
  lease, provider attempt, and legacy interval. Preview the credential-free
  owner-local policy, review its exact routes and capacity, then apply only its
  exact approval hash:

  ```bash
  FACTORY_KIT="$HOME/.factory/kits/releases/<full-sha>/scripts/factory-kit.sh"
  PLAN="$(
    bash "$FACTORY_KIT" provider-concurrency plan \
      --sha "<full-sha>" --capacity 3
  )"
  APPROVAL="$(
    printf '%s' "$PLAN" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_sha256"])'
  )"
  bash "$FACTORY_KIT" provider-concurrency apply \
    --sha "<full-sha>" --capacity 3 --approve-hash "$APPROVAL"
  ```

  Use the product's exact configured capacity in place of `3`. The apply is
  fail-closed, initializes only owner-local policy/coordinator/runtime state,
  and never copies a credential. Each admitted role later copies only its CLI
  authentication into a mode-0600 attempt-local home and removes that home
  after its process group drains. A policy change while provider work is
  active is refused.
- Certification records the exact policy digest, covered adapters and routes,
  ticket capacity, sealed Factory SHA/tree, and owner-local runtime directory
  identity. Activation and recutover recompute that evidence and refuse drift.
- Certify the pinned release:

  ```bash
  bash scripts/factory-kit.sh certify \
    --project "<project>" --product "<absolute-product-path>" --sha "<full-sha>"
  ```
  A fresh install records 24-hour owner-only kit-suite evidence by default.
  Authenticated successful GitHub Actions evidence for the exact protected-main
  SHA and its full Linux, macOS, aggregate, and immutability jobs is mandatory;
  installation then runs a sandboxed host smoke check. Missing evidence fails
  closed and never launches the complete suite locally. Expired certification
  evidence follows the same remote-proof and local-smoke refresh path.
  Repeated certification of the exact unchanged sealed release may reuse it,
  while product certification and all product/config/receipt checks still run.
  Set `FACTORY_KIT_SUITE_EVIDENCE_TTL_SECONDS` only as explicit machine policy;
  changing it forces a fresh suite and caps the product receipt to that proof.
  A product that has independent validation branches may call the sealed
  `scripts/certification-runner.py` from its certification script with a
  repository-owned `nysa.software-factory.certification-plan/v2` JSON plan.
  The plan pins exact Node and npm versions independently and declares every
  phase's network requirement as `denied`, `optional`, or `required`. A
  required phase fails before spawning when the command-scoped reviewed-network
  opt-in is absent; denied phases remain network-denied when that opt-in is
  present for another phase.
  Before repository readiness tests, call the sealed
  `scripts/certification-preflight.py` with that plan, the exact candidate
  Factory SHA/tree, product root, and Contract version. The command compares
  those values plus the product SHA/tree and observed Node/npm against one
  strict tuple and exits `2` before a plan phase can spawn on missing, unknown,
  malformed, or mismatched input. Qualification preparation and certification
  run the same preflight automatically and persist the tuple for sealed-launch
  revalidation; do not duplicate its plan parser in product scripts.
  Start with two workers; the runner permits at most three, isolates phase logs
  and temporary directories, records timing/CPU/peak-memory/input/artifact
  evidence, cancels siblings after failure, and binds the passing result into
  the Factory receipt. Phase evidence reuse is opt-in: omit `reuse` or set it
  to `never` for commands with undeclared side effects and use `artifacts` only
  when every reusable output is declared and `kind` is exactly `build` or
  `dependencies`. A phase without artifacts cannot opt in; every other kind,
  including application tests and policy/security/configuration checks, must
  use `never` even if it emits report files.
  Same-result-root restarts reuse exact local evidence. A later Factory
  certification command may additionally restore only a plan-authorized
  complete artifact set from the owner-only authenticated store. The Factory
  stages verified entries read-only into the disposable workspace, the runner
  rehashes their complete manifests before restoration, and only independently
  validated disposable outputs can be atomically published back. Raw phase
  logs, application tests, policy, security, configuration, and undeclared side
  effects are never persisted or restored. Tuple, product, plan, dependency,
  command, runner, network, expiry, size, type, mode, containment, or digest
  drift produces a cache miss while full product certification still runs.
  Hit evidence reports saved phase wall time separately from cache lookup,
  manifest rehash, and restoration overhead.

- Review model policy through the sealed launcher. Run `models profiles --json`,
  preview the intended profile with `models plan [--profile <id>] --json`, and
  activate only with that profile's exact returned hash and an operator ID.
  `cursor-opus-v1` is the no-record default.

- Create a separate sandbox product and Hermes canary profile. Do not copy the
  production `.env`, secret files, registry, ledger, board mapping, or
  LaunchAgent. Run the real-Hermes canary in
  [hermes-integration.md](hermes-integration.md) before the first activation.

## Step 6 — CI and hosting

- GitHub: branch protection per `docs/git-flow.md`, with the test-immutability check wired as a required status.
- Hosting: Railway per `docs/operations/railway.md` (staging + preview deploys + Postgres).
- Rehearse the rollback drill (`docs/operations/rollback-drill.md`) once before the pilot.

## Step 7 — Walking skeleton

One trivial end-to-end feature through the full loop before any backlog exists. Gate: the operator opens a working staging URL. See `docs/operations/walking-skeleton.md`.

## Step 8 — Calibration

3 tickets (5 max) through the full loop with sandboxed external actions before cutting the real backlog. Prove: test handoff, pre-merge evidence bundle, one intentional rollback, cost capture in the ledger, one simulated crash recovery.

## Onboarding validator checklist

All boxes checked = the factory may start. Any box unchecked = it may not.

- [ ] Product repo exists, sibling location, `factory/initiatives/` and `factory/tickets/` created (no kit code copied; only the three CI files)
- [ ] `factory/KIT_PIN` contains exactly one lowercase full SHA; `factory/PROJECT.env` names an executable, repository-contained `CERTIFY_SCRIPT`
- [ ] Exact-SHA release exists under `~/.factory/kits/releases/`, is sealed read-only, and has a current, unexpired tuple-bound receipt
- [ ] The active contract 1.2/1.3/1.4 receipt remains owner-only mode `0600`; its certified product origin matches the single configured push destination
- [ ] `~/.factory/bin/factory-launch`, the product-plan Node/npm/npx pins, and the receipt-bound exact provider CLI links are installed; `provider-cli-pin check` is ready, `contract --json` returns the expected version, `contract-test.sh --routes` passes, and `doctor --json` has no error category
- [ ] `models profiles --json` and `models plan --json` were reviewed; the operator approved the exact profile hash, or explicitly retained default `cursor-opus-v1`
- [ ] A clean sample ticket passed `models pin --ticket <T-NNN> --workdir <exact-worktree> --json`, creating one pushed commit containing both `Kit-SHA` and the exact six-role route plan
- [ ] Kimi remains disabled and absent from every profile; no live/billed-pilot claim is recorded, and credential rotation plus broker/OS isolation are prerequisites to a pilot
- [ ] Factory Hermes profile, project registry, and factory gateway LaunchAgent are separate from the dashboard and primary Hermes profile
- [ ] Real-Hermes canary uses a separate profile/product and no copied production secrets; redacted evidence is recorded
- [ ] `ENVELOPE.md` has no unfilled blanks
- [ ] Console spend caps set on the primary providers; Cursor usage controls reviewed before fallback is enabled
- [ ] Provider/account-route, Cursor, and product-runtime credentials are separated; none are committed
- [ ] No secrets in git history (`git log -p | grep -i` for key patterns, or a scanner)
- [ ] Linear board matches `docs/workflows/linear.md`; initiative Projects and ticket template installed; `scripts/linear-sync.py --setup` run; Doctor reports the intended explicit `com.factory.linear-sync` state and stable-launcher arguments; enabled products are loaded; sync health is current
- [ ] Branch protection on; test-immutability check is a required status
- [ ] `GH_REPO`, `DONE_REQUIRED_CHECKS`, and `AUTO_MERGE_METHOD` exactly match the protected repository, required post-merge contexts, and enabled merge strategy; GitHub auto-merge is enabled without bypass permissions
- [ ] Staging deploy works; preview deploys work on PRs
- [ ] Rollback drill performed once, timed, and noted in `factory/`
- [ ] Walking skeleton merged; operator has clicked the staging URL
- [ ] Kill switch tested: `scripts/kill-switch.sh` stops a live run
- [ ] Metrics ledger file exists and the run wrapper writes to it
- [ ] `factory/runs/`, `factory/.active-runs/`, `factory/.provider.lock/`, and `factory/runtime-ledger.csv` are ignored; preflight or the first normal Linear reconciliation creates the durable real runs root; and `project-ledger` can deterministically project it from a clean close-out worktree only after every active or ambiguous claim and `factory/runs/*.pid` record is reconciled
- [ ] A duplicate ticket-and-role launch refuses an existing claim without creating a manifest, and malformed telemetry retains the full reservation
- [ ] Contract 1.8 multi-ticket products report provider concurrency ready in
      doctor, cover Cursor, Claude Code, and Codex at ticket capacity, and use
      distinct attempt-local homes; older and single-ticket products retain the
      product-level serialized provider lock
- [ ] Every declared dependency has normal protected terminal evidence. A
      pre-Contract-1.8 Backlog dependency whose application PR already merged
      uses only an exact `scripts/dependency-fulfillment.py plan` preview and
      matching `apply --approve-hash`; its manual, no-bypass control PR
      atomically installs the target `KIT_PIN` and immutable fulfillment batch
      without marking the legacy ticket Done
- [ ] If a machine cap is configured, its global ledger is a regular non-symlink file in a real directory and a mutation drill fails closed without deleting ledger history
- [ ] Activation/reconcile interruption and fail-closed kit rollback drilled; `MAINTENANCE` remains after rollback
- [ ] Measured control-plane outage is within 5 minutes and full rollback RTO is within 30 minutes, or the factory remains in maintenance until the gap is resolved

Deferred stages live in the root `TODOS.md` until their activation conditions are met.

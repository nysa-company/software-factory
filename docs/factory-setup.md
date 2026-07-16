# Factory setup

This file is the checklist for turning the kit into a running factory for one product. Every blank below must be filled before the first ticket runs. The validator section at the end is the pass/fail gate.

Read [architecture.md](architecture.md) first. It defines the kit/product boundary, role flow, trust boundaries, and budget model assumed by this checklist.

## Step 1 — Product repo

- Create the product repo as a **sibling folder** (never nested inside another repo). Do NOT copy kit scripts into it — the engine model in `docs/architecture.md` is the contract.
- Create `factory/` with: `ENVELOPE.md` (filled from `envelope/ENVELOPE.template.md`), `ENVELOPE.env`, `PROJECT.env`, `KIT_PIN`, an executable certification script, and empty `initiatives/` and `tickets/` directories.
- Ignore `factory/linear-map.json` and `factory/.linear-sync.lock`; they are runtime operator state and must never dirty the registered checkout.
- Ignore `factory/runs/`, `factory/.active-runs/`, `factory/.provider.lock/`, and `factory/runtime-ledger.csv`; preflight or the first normal Linear reconciliation durably initializes the real, no-follow runs root, manifests are atomic local run truth, claims and the provider lock are runtime exclusion state, and the CSV is their rebuildable view over the tracked durable ledger.
- Write exactly one lowercase, full 40-character SHA to `factory/KIT_PIN`. External products never use an abbreviated SHA or the in-kit conformance exception.
- Add one repository-contained executable path to `factory/PROJECT.env`, for example `CERTIFY_SCRIPT=factory/certify.sh`. The script must run the product checks without changing the tracked product tree.
- Configure exactly one `origin` push URL. Certification records that literal URL as receipt `product_origin`; trusted contract 1.2 writes refuse a different or additional push destination.
- Leave `MAX_CONCURRENT_TICKETS` absent (the safe default is `1`). Set it to
  `2` only after contract 1.1 is active and a bounded concurrency pilot is
  approved; no other value is valid. Two ticket leases may progress, but the
  product-level control lock serializes their provider intervals.
- Copy exactly two CI files (GitHub requires workflows to live in the repo they run on): `ci/test-immutability-check.sh` → `.github/scripts/` and `ci/github-actions-ci.template.yml` → `.github/workflows/ci.yml`, with `TEST_PATHS` set from `PROJECT.env`.
- Write `factory/ENVELOPE.env` from the filled `ENVELOPE.md` — plain `KEY=value` lines for `PER_RUN_BUDGET_USD`, `PER_TICKET_BUDGET_USD`, `PER_RUN_MAX_TURNS`, `PER_RUN_TIMEOUT_MIN`, `DAILY_CAP_USD`. Money values are capped at $1,000,000 with six decimal places, turns at 1,000, and timeout at 1,440 minutes. The validator checks the two files agree. `ENVELOPE.env` and `~/.factory/global.env` are parsed as whitelisted data and must never contain shell commands or expansions.
- If `GLOBAL_DAILY_CAP_USD` is configured, keep its global-ledger parent as a real local directory. The wrapper validates the ledger and holds its exact-owner lock across each complete provider interval, so all globally capped runs on that machine are intentionally serialized.
- Product docs the factory needs (written per product, not in the kit):
  - `docs/engine-spec.md` — data model, durable job model (retries, idempotency, crash recovery), external-action policy, connector safety (sandboxed/allowlisted sends until production).
  - `docs/acceptance/<first-slice>.md` — the vertical-slice acceptance spec the backlog is cut from.
  - `docs/conventions.md` — short; grows from the walking skeleton.

## Step 2 — Envelope

Fill every blank in `factory/ENVELOPE.md`: per-ticket budget (USD and max turns), daily cap, retry ceilings, escalation rules, exit thresholds. Then set the matching hard caps in the Anthropic and OpenAI consoles and review Cursor usage controls before enabling fallback. Cursor CLI has no documented per-run dollar stop: the ledger always keeps the full run reservation. Approved token telemetry plus a dated pricing snapshot may add an observational estimate, but never reduce that reservation.

## Step 3 — Keys and secrets

- Keep production CLI, checking CLI, and product-runtime credentials separate. Cursor fallback uses a one-time local `agent login` (or `CURSOR_API_KEY` for unattended infrastructure); never put Cursor credentials in the kit or product config.
- Secrets live only in GitHub Actions secrets and the hosting platform. No `.env` in git, ever.

## Step 4 — Linear

Set up the shared Software Factory team per `docs/workflows/linear.md`: compact phase-level workflow states, the factory issue template, risk/external labels, and one Linear Project per file in `factory/initiatives/`.

Run `scripts/linear-sync.py --factory-root <product-repo> --setup` once to create or verify the team, states, labels, and Projects. Install the per-product job from `scripts/launchd/com.factory.linear-sync.plist.template` to reconcile every three minutes. Linear owns operator priority, Ready, approval, unblock, and Project membership; Git owns execution details. The reconciler is asynchronous so Linear never sits in the sequencer control path.

## Step 5 — Hermes release boundary

- Create `~/.factory/bin` and `~/.factory/kits`. Install
  `integrations/hermes/bin/factory-launch` at
  `~/.factory/bin/factory-launch` only through an explicit bootstrap or
  contract migration.
- The launcher intentionally replaces caller `PATH` with its contract
  allowlist, which includes `~/.factory/bin` but not `~/.local/bin`. If
  provider CLIs are installed outside the allowlist, bootstrap version-pinned
  links for `claude` and `agent` into `~/.factory/bin`; do not widen the
  launcher to an entire user-writable bin directory. Verify the physical link
  targets, pinned versions, and `contract-test.sh --routes` before dispatch.
- Create the dedicated factory profile at
  `~/.hermes/profiles/factory`. Install the canonical `SOUL.md` and
  `skills/factory-dispatch/SKILL.md` from `integrations/hermes/templates/`.
- Add `~/.hermes/profiles/factory/projects/<project>.env` with
  `PRODUCT_ROOT=<absolute-product-path>`. The stable launcher ignores `KIT_DIR`
  from legacy registry files and resolves the active release itself. Registry
  files contain paths only, never credentials.
- Keep the factory gateway and machine dashboard as separate LaunchAgents.
  Do not embed secret-bearing environment keys in either plist.
- Install and certify the pinned release:

  ```bash
  bash scripts/factory-kit.sh install --repo "$PWD" --sha "<full-sha>"
  bash scripts/factory-kit.sh certify \
    --project "<project>" --product "<absolute-product-path>" --sha "<full-sha>"
  ```

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

- [ ] Product repo exists, sibling location, `factory/initiatives/` and `factory/tickets/` created (no kit code copied; only the two CI files)
- [ ] `factory/KIT_PIN` contains exactly one lowercase full SHA; `factory/PROJECT.env` names an executable, repository-contained `CERTIFY_SCRIPT`
- [ ] Exact-SHA release exists under `~/.factory/kits/releases/`, is sealed read-only, and has a current, unexpired tuple-bound receipt
- [ ] The active contract 1.2 receipt remains owner-only mode `0600`; its certified product origin matches the single configured push destination
- [ ] `~/.factory/bin/factory-launch` and any required version-pinned provider CLI links are installed; `contract --json` returns the expected version, `contract-test.sh --routes` passes, and `doctor --json` has no error category
- [ ] Factory Hermes profile, project registry, and factory gateway LaunchAgent are separate from the dashboard and primary Hermes profile
- [ ] Real-Hermes canary uses a separate profile/product and no copied production secrets; redacted evidence is recorded
- [ ] `ENVELOPE.md` has no unfilled blanks
- [ ] Console spend caps set on the primary providers; Cursor usage controls reviewed before fallback is enabled
- [ ] Production, checking, Cursor, and product-runtime credentials are separated; none are committed
- [ ] No secrets in git history (`git log -p | grep -i` for key patterns, or a scanner)
- [ ] Linear board matches `docs/workflows/linear.md`; initiative Projects and ticket template installed; `scripts/linear-sync.py --setup` run; `com.factory.linear-sync` loaded; sync health is current
- [ ] Branch protection on; test-immutability check is a required status
- [ ] Staging deploy works; preview deploys work on PRs
- [ ] Rollback drill performed once, timed, and noted in `factory/`
- [ ] Walking skeleton merged; operator has clicked the staging URL
- [ ] Kill switch tested: `scripts/kill-switch.sh` stops a live run
- [ ] Metrics ledger file exists and the run wrapper writes to it
- [ ] `factory/runs/`, `factory/.active-runs/`, `factory/.provider.lock/`, and `factory/runtime-ledger.csv` are ignored; preflight or the first normal Linear reconciliation creates the durable real runs root; and `project-ledger` can deterministically project it from a clean close-out worktree only after every active or ambiguous claim and `factory/runs/*.pid` record is reconciled
- [ ] A duplicate ticket-and-role launch refuses an existing claim without creating a manifest, and malformed telemetry retains the full reservation
- [ ] Provider intervals serialize under the product-level control lock, and any new or changed sibling manifest during an interval fails closed
- [ ] If a machine cap is configured, its global ledger is a regular non-symlink file in a real directory and a mutation drill fails closed without deleting ledger history
- [ ] Activation/reconcile interruption and fail-closed kit rollback drilled; `MAINTENANCE` remains after rollback
- [ ] Measured control-plane outage is within 5 minutes and full rollback RTO is within 30 minutes, or the factory remains in maintenance until the gap is resolved

Deferred stages live in the root `TODOS.md` until their activation conditions are met.

# Factory setup

This file is the checklist for turning the kit into a running factory for one product. Every blank below must be filled before the first ticket runs. The validator section at the end is the pass/fail gate.

Read [architecture.md](architecture.md) first. It defines the kit/product boundary, role flow, trust boundaries, and budget model assumed by this checklist.

## Step 1 — Product repo

- Create the product repo as a **sibling folder** (never nested inside another repo). Do NOT copy kit scripts into it — the engine model in `docs/architecture.md` is the contract.
- Create `factory/` with: `ENVELOPE.md` (filled from `envelope/ENVELOPE.template.md`), `ENVELOPE.env`, `PROJECT.env`, `KIT_PIN` (current kit SHA), and empty `initiatives/` and `tickets/` directories.
- Copy exactly two CI files (GitHub requires workflows to live in the repo they run on): `ci/test-immutability-check.sh` → `.github/scripts/` and `ci/github-actions-ci.template.yml` → `.github/workflows/ci.yml`, with `TEST_PATHS` set from `PROJECT.env`.
- Write `factory/ENVELOPE.env` from the filled `ENVELOPE.md` — plain `KEY=value` lines for `PER_RUN_BUDGET_USD`, `PER_TICKET_BUDGET_USD`, `PER_RUN_MAX_TURNS`, `PER_RUN_TIMEOUT_MIN`, `DAILY_CAP_USD`. The validator checks the two files agree.
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

## Step 5 — CI and hosting

- GitHub: branch protection per `docs/git-flow.md`, with the test-immutability check wired as a required status.
- Hosting: Railway per `docs/operations/railway.md` (staging + preview deploys + Postgres).
- Rehearse the rollback drill (`docs/operations/rollback-drill.md`) once before the pilot.

## Step 6 — Walking skeleton

One trivial end-to-end feature through the full loop before any backlog exists. Gate: the operator opens a working staging URL. See `docs/operations/walking-skeleton.md`.

## Step 7 — Calibration

3 tickets (5 max) through the full loop with sandboxed external actions before cutting the real backlog. Prove: test handoff, pre-merge evidence bundle, one intentional rollback, cost capture in the ledger, one simulated crash recovery.

## Onboarding validator checklist

All boxes checked = the factory may start. Any box unchecked = it may not.

- [ ] Product repo exists, sibling location, `factory/initiatives/` and `factory/tickets/` created (no kit code copied; only the two CI files)
- [ ] `factory/KIT_PIN` holds the certified kit SHA; `factory/PROJECT.env` filled per the reference copy
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

Deferred stages live in the root `TODOS.md` until their activation conditions are met.

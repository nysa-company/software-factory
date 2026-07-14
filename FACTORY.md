# FACTORY.md — instantiating the kit for a product

This file is the checklist for turning the kit into a running factory for one product. Every blank below must be filled before the first ticket runs. The validator section at the end is the pass/fail gate.

## The engine model (multi-project, 2026-07-13)

The kit is **installed once per machine and shared by every product** — like git itself. Product repos never contain kit code; they contain only their own state. Every script separates the two mechanically: `KIT_DIR` (where the script lives — adapters, sequencer, role prompts) vs `FACTORY_ROOT` (the product repo — tickets, ledger, envelope). A run is always:

```bash
cd <kit clone> && FACTORY_ROOT=<product repo> bash scripts/run-agent.sh --role <role> \
  --ticket <T-NNN> --prompt-file roles/<role>.md [--workdir <worktree>] -- "task text"
```

What lives where:

- **Kit (this repo, one clone per machine):** scripts, adapters + CLI version pins, role contracts, workflow docs, runbooks, CI templates. Fixes land here once, through PRs.
- **Product repo (one per product):** `factory/` (ENVELOPE.env, PROJECT.env, KIT_PIN, initiatives/, tickets/, ledger.csv), product docs, its instantiated CI workflow, its own GitHub ruleset + deploy key. Raw agent output is local-only under `.context/factory-runs/` and never lands in git. All products share the Software Factory Linear team; each initiative gets a Linear Project.
- **`factory/KIT_PIN`** — the kit commit SHA this product is certified against. `scripts/preflight.sh` hard-fails on a mismatch, so a kit upgrade never changes a project's behavior silently; upgrading = run the product's calibration against the new kit, then update the pin. (A product living inside the kit repo, like the Relay conformance app, is implicitly pinned.)
- **`factory/PROJECT.env`** — the project descriptor a dispatcher reads: `PROJECT_NAME`, `GH_REPO` (owner/repo slug), `TEST_PATHS` (for the immutability gate and reorder script), `WORKTREES_DIR`, `TICKET_BRANCH_PREFIX`, and `VERIFY_COMMAND` (the exact project-owned test command agents may run). See `conformance/factory/PROJECT.env` for the reference copy.

Budget note: per-product caps live in each product's `ENVELOPE.env`; the machine-level cap in `~/.factory/global.env` already sums across all products, so adding projects never multiplies the daily budget.

Backend policy is kit-owned and certified by the same `KIT_PIN`. Production roles require the OpenAI family (`codex`, with optional `cursor-openai` fallback); checking roles require the Anthropic family (`claude-code`, with optional `cursor-anthropic` fallback). Fallback is resolved before task submission and is never a retry: one logical role run submits its task to at most one agent process. Machine-specific Cursor model IDs, the approved CLI compatibility version, and `FACTORY_CURSOR_FALLBACK_ENABLED` live in `~/.factory/global.env`. Exact model IDs must be present in `scripts/lib/cursor-model-families.txt`; `auto` is forbidden.

```bash
# ~/.factory/global.env — no credentials in this file
export FACTORY_CURSOR_FALLBACK_ENABLED=0
export CURSOR_AGENT_VERSION="EXACT_VERSION_TOKEN"
export CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
export CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
# Optional observational estimate only; ledger still keeps the full reservation.
export CURSOR_PRICING_SNAPSHOT_DATE=YYYY-MM-DD
export CURSOR_OPENAI_USD_PER_MTOK_IN="RATE"
export CURSOR_OPENAI_USD_PER_MTOK_OUT="RATE"
export CURSOR_ANTHROPIC_USD_PER_MTOK_IN="RATE"
export CURSOR_ANTHROPIC_USD_PER_MTOK_OUT="RATE"
```

Enable fallback only after `agent status --format json`, `agent models`, `scripts/adapters/contract-test.sh --routes`, and both conformance smokes pass. Cursor output is redacted while streaming; the redacted `.out` artifact remains local and ignored, while the manifest and ledger carry durable provenance.

## Step 1 — Product repo

- Create the product repo as a **sibling folder** (never nested inside another repo). Do NOT copy kit scripts into it — the engine model above is the contract.
- Install `nysa-agents` and run `/repo-setup` in the product repo. This owns the generic `AGENTS.md`, memory, repository receipt, `scripts/repo-check`, `scripts/secret-scan`, PR template, and optional Conductor setup; the factory kit continues to own engine code.
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
- `scripts/secret-scan` must pass before preflight, in local verification, and in CI. Autonomous worktrees containing `.env`, key, certificate, or credential files are refused before either model starts.

## Step 4 — Linear

Set up the shared Software Factory team per `workflows/linear.md`: compact phase-level workflow states, the factory issue template, risk/external labels, and one Linear Project per file in `factory/initiatives/`.

Run `scripts/linear-sync.py --factory-root <product-repo> --setup` once to create or verify the team, states, labels, and Projects. Install the per-product job from `scripts/launchd/com.factory.linear-sync.plist.template` to reconcile every three minutes. Linear owns operator priority, Ready, approval, unblock, and Project membership; Git owns execution details. The reconciler is asynchronous so Linear never sits in the sequencer control path.

## Step 5 — CI and hosting

- GitHub: branch protection per `ci/branch-protection.md`, the test-immutability check wired as a required status.
- Hosting: Railway per `ci/railway.md` (staging + preview deploys + Postgres).
- Rehearse the rollback drill (`ci/rollback-drill.md`) once before the pilot.

## Step 6 — Walking skeleton

One trivial end-to-end feature through the full loop before any backlog exists. Gate: the operator opens a working staging URL. See `ci/walking-skeleton.md`.

## Step 7 — Calibration

3 tickets (5 max) through the full loop with sandboxed external actions before cutting the real backlog. Prove: test handoff, pre-merge evidence bundle, one intentional rollback, cost capture in the ledger, one simulated crash recovery.

## Onboarding validator checklist

All boxes checked = the factory may start. Any box unchecked = it may not.

- [ ] Product repo exists, sibling location, `factory/initiatives/` and `factory/tickets/` created (no kit code copied; only the two CI files)
- [ ] `/repo-setup` applied; `scripts/repo-check` and `scripts/secret-scan` pass
- [ ] `factory/KIT_PIN` holds the certified kit SHA; `factory/PROJECT.env` filled per the reference copy
- [ ] `ENVELOPE.md` has no unfilled blanks
- [ ] Console spend caps set on the primary providers; Cursor usage controls reviewed before fallback is enabled
- [ ] Production, checking, Cursor, and product-runtime credentials are separated; none are committed
- [ ] No secrets in git history (`git log -p | grep -i` for key patterns, or a scanner)
- [ ] Linear board matches `workflows/linear.md`; initiative Projects and ticket template installed; `scripts/linear-sync.py --setup` run; `com.factory.linear-sync` loaded; sync health is current
- [ ] Branch protection on; test-immutability check is a required status
- [ ] Staging deploy works; preview deploys work on PRs
- [ ] Rollback drill performed once, timed, and noted in `factory/`
- [ ] Walking skeleton merged; operator has clicked the staging URL
- [ ] Kill switch tested: `scripts/kill-switch.sh` stops a live run
- [ ] Metrics ledger file exists and the run wrapper writes to it

## Deferred stages and ops backlog (activate at first deployed product)

Recorded 2026-07-13 with the spec-kit/gstack tiered adoption; none of these are built until the first product instantiation (Nysa) has a deployed web app. Rationale and full catalog mapping: NYSA `deliverables/2026-07-13-speckit-gstack-evaluation.md`.

- **Verifier stage** — `roles/verifier.md`, report-only browser QA between Reviewer approval and Narrator: vendor gstack's `browse` binary at a pinned version (goto/snapshot/click/console; no preamble), adopt its health-score rubric and issue taxonomy verbatim in the role contract; screenshots + score + regression baseline go into the evidence bundle; findings above threshold route like a REQUEST CHANGES; never commits. Pilot on one ticket before making it standing.
- **Security audit** — periodic gstack `/cso`-style pass, operator-triggered; high value for an LLM product.
- **Post-deploy monitoring** — gstack `/canary` pattern after each staging/production deploy.
- **Perf baselines** — gstack `/benchmark` pattern; regression numbers per ticket once there is a deploy to measure.
- **Code-quality score** — gstack `/health` weighted score as a CI-adjacent metric on the product repo.
- **Spec drift check** — spec-kit `/speckit.converge` pattern between milestones: does the codebase still match the living spec.

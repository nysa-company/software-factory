# FACTORY.md — instantiating the kit for a product

This file is the checklist for turning the kit into a running factory for one product. Every blank below must be filled before the first ticket runs. The validator section at the end is the pass/fail gate.

## Step 1 — Product repo

- Create the product repo as a **sibling folder** (never nested inside another repo).
- Copy from the kit: `envelope/ENVELOPE.template.md` → `factory/ENVELOPE.md`, `roles/` → `factory/roles/`, `workflows/ticket-flow.md` → `factory/`, `ci/test-immutability-check.sh` → `.github/scripts/`.
- Product docs the factory needs (written per product, not in the kit):
  - `docs/engine-spec.md` — data model, durable job model (retries, idempotency, crash recovery), external-action policy, connector safety (sandboxed/allowlisted sends until production).
  - `docs/acceptance/<first-slice>.md` — the vertical-slice acceptance spec the backlog is cut from.
  - `docs/conventions.md` — short; grows from the walking skeleton.

## Step 2 — Envelope

Fill every blank in `factory/ENVELOPE.md`: per-ticket budget (USD and max turns), daily cap, retry ceilings, escalation rules, exit thresholds. Then set the matching hard caps in the Anthropic and OpenAI consoles. The envelope doc states limits; the consoles and the run wrapper enforce them.

## Step 3 — Keys and secrets

- Three separate API keys minimum: factory-builder (family A), factory-reviewer/test-author (family B), product runtime. Named so console dashboards split spend cleanly.
- Secrets live only in GitHub Actions secrets and the hosting platform. No `.env` in git, ever.

## Step 4 — Linear

Set up the board per `workflows/linear.md`: five workflow states plus Done, the ticket template with acceptance-criteria and spec-link checklist fields.

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

- [ ] Product repo exists, sibling location, kit files copied
- [ ] `ENVELOPE.md` has no unfilled blanks
- [ ] Console spend caps set on both providers and screenshot saved in `factory/`
- [ ] Three named API keys exist; none shared across concerns
- [ ] No secrets in git history (`git log -p | grep -i` for key patterns, or a scanner)
- [ ] Linear board matches `workflows/linear.md`; ticket template installed
- [ ] Branch protection on; test-immutability check is a required status
- [ ] Staging deploy works; preview deploys work on PRs
- [ ] Rollback drill performed once, timed, and noted in `factory/`
- [ ] Walking skeleton merged; operator has clicked the staging URL
- [ ] Kill switch tested: `scripts/kill-switch.sh` stops a live run
- [ ] Metrics ledger file exists and the run wrapper writes to it

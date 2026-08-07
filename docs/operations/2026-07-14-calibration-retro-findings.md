# Calibration retro findings — nysa-app pilot (2026-07-14)

Status: open — awaiting kit implementation
Source: first day of real pipeline runs on the `nysa-app` product repo (calibration tickets T-001 trace store, T-002 version endpoint + footer, T-003 DB wrapper skeleton). Evidence lives in `nysa-app`: `factory/ledger.csv`, `factory/tickets/T-001..T-003.md`, PRs #13–#18.

## How to use this document

Each finding below is a candidate kit improvement. For each one you take on: cut a ticket (or branch) scoped to that finding, keep the kit product-agnostic (no Nysa-specific state), and update the relevant `docs/` page plus `context/memory.md` when the behavior changes. Findings are ordered by operational priority. F1, F2, and F4 were flagged to be fixed before the pilot's next ticket build; F4 is product-repo infrastructure (Railway) rather than kit code, and is included for completeness.

## Context: what the pilot measured

- T-001: 14 runs, $14.19 booked ($9.19 real model spend + $5.00 voided reservation, see F6), ~51 min pipeline.
- T-002: 8 runs, $5.26, ~25 min pipeline. Merged same day; staging verified.
- T-003: planning only ($0.71), parked at the spec-lint gate.
- Spec-lint caught 6 real contract gaps across T-001/T-002 before any code existed. Test-immutability gate held on every PR. The evidence bundle honestly reported unverifiable criteria (see F3).

## Findings

### F1 — Runtime state dirties the product repo's main clone (high)

**What happened.** `factory/ledger.csv` rows and ticket `State:` edits accumulate as uncommitted changes in the main clone while runs execute. This broke `preflight.sh` ("working tree not clean") twice and caused a merge conflict on the T-002 PR when a bookkeeping commit on `main` raced the ticket branch's copy of the same files. Earlier the same day, `factory/linear-map.json` had the same problem and was moved to `.gitignore` as runtime state.

**Proposed fix.** Stop mutating shared tracked files during runs:
- Write ledger rows as append-only per-run files (e.g. `factory/runs/<run_id>.ledger` next to the existing `.meta`/`.out`), and roll them into `factory/ledger.csv` via a periodic/explicit bookkeeping commit.
- Have the dispatcher commit ticket `State:` transitions immediately when it makes them, rather than leaving them in the working tree.
- Audit for any other tracked file written mid-run.

### F2 — Planner works in the product repo's main clone (high)

**What happened.** The planner creates the ticket branch inside the main clone and leaves it checked out; on T-003 it also exited leaving its ticket-file edits uncommitted. Consequences: the next ticket's preflight fails (dirty tree / wrong branch), a `git pull` by anyone else lands on the ticket branch, and uncommitted planning work can be lost. The builder already avoids this by running in a dedicated worktree.

**Proposed fix.** Run the planner (and spec-linter) in a dedicated worktree exactly like the builder. Extend the role contract: a planning run must commit and push its ticket-file changes before exiting; the wrapper should verify a clean tree on exit and treat failure to commit as a failed run.

### F3 — Narrator cannot verify UI acceptance criteria (high)

**What happened.** T-002's evidence bundle shipped with 3 of 8 criteria marked "FAIL — not visually verified" because the narrator has no browser and could not resolve the PR preview URL from Railway metadata. An operator-side agent had to verify the footer manually in a browser after the bundle was posted.

**Proposed fix.** Either give the narrator stage a browser-capable verification step (headless check of frozen selectors/text against the PR preview URL), or formalize the split: the bundle template gets an explicit "operator-side visual verification" section listing the exact URL, selector, and expected text for each UI criterion. Also resolve the preview URL mechanically (Railway GraphQL `environment.serviceInstances.domains`) instead of relying on PR metadata.

### F4 — PR preview web bundles bake the staging API URL (medium, product-repo infra)

**What happened.** `VITE_API_URL` is baked at build time and was set to the staging API for all environments, so a PR preview's web bundle calls the staging API. Full-stack changes cannot be demoed end-to-end on a preview: on T-002 the version fetch 404'd against staging and the footer never rendered until fetches were manually redirected to `api-nysa-app-pr-16.up.railway.app`.

**Proposed fix.** Set `VITE_API_URL` per Railway environment so preview environments point at their own API service domain. For the kit: add this to the instantiation checklist (any build-time client config must be environment-scoped before PR previews count as evidence).

### F5 — No operator-override lane for extra lint rounds (medium)

**What happened.** T-001 needed a third spec-lint round, explicitly authorized by the operator. The sequencer only models "max 2 rounds" and output ESCALATE, then REFUSE; the pipeline proceeded on a hand-written `OPERATOR NOTE` on the ticket, outside any contract.

**Proposed fix.** Define an `OPERATOR OVERRIDE` block format on the ticket (who, when, what limit is being extended, by how much). `next-stage.sh` recognizes it, permits exactly one extra round per block, and logs it to the ledger/ticket. Refuse anything not covered by an explicit block.

### F6 — Failed launches book the full conservative reservation (medium)

**What happened.** A builder launch failed instantly (worktree creation race, see below) but still booked its $5.00 conservative reservation into the ledger, inflating T-001's recorded cost by ~50%. The reservation had to be voided by hand with an operator note. Root cause of the launch failure itself: the dispatcher tried to `git worktree add` for a branch still checked out in the main clone.

**Proposed fix.** Two parts: (a) the launch helper validates preconditions (branch not checked out elsewhere, worktree path creatable) *before* reserving budget; (b) runs that die before the adapter produces any turns are auto-voided — ledger row rewritten to $0 with an explicit exit tag (e.g. `launch_failure`) so cost reporting stays honest without manual edits.

### F7 — Test-immutability gate vs bookkeeping commits (medium)

**What happened.** A bookkeeping commit touching only `.gitignore` landed after a test-fix commit on the T-001 branch and tripped the test-immutability gate ("all test commits must precede implementation commits") — the gate classified the `.gitignore` change as implementation. `reorder-test-fixes.sh` reported "nothing to do" and the commits had to be reordered with manual cherry-picks.

**Proposed fix.** Exempt non-code bookkeeping paths (`factory/**`, `.gitignore`, `docs/**`) from the gate's implementation classification. Separately, debug why `reorder-test-fixes.sh` missed the case and add a conformance test for it. Note: merge commits from `main` into a ticket branch passed the gate fine — only the ordering classification needs the fix.

### F8 — `revert/` branch prefix rejected by repo-check (low, documented)

**What happened.** During the rollback drill, a pure-revert branch named `revert/...` was rejected because `revert` is not in the baseline `branch_pattern`. The pilot adopted the convention `chore/<slug>-revert`.

**Proposed fix.** Either add `revert` to the default `branch_pattern` in the baseline, or document `chore/<slug>-revert` as the canonical convention in `docs/git-flow.md`. The convention is already recorded in the product repo's memory; pick one and make the kit consistent.

## Also observed (no kit change proposed yet)

- **Keychain access over SSH.** All provider/Railway/Linear keys on the execution host are readable only from the GUI launchd domain; plain SSH sessions get keychain errors (exit 36). The working pattern is `launchctl submit` of a script that reads the key, then collecting its output file. Worth a documented helper in `docs/operations/` if this keeps recurring.
- **Railway token scoping.** Project tokens cannot run `serviceConnect`/`deploymentTriggerCreate` or query environments created for PRs; an account-scoped token is required for those. Documented in the product repo's memory; consider adding to `docs/operations/railway.md`.
- **What worked and should not regress:** cross-family spec-lint review (6 real gaps caught pre-code), builder/test-author separation (a Postgres 16 regex canonicalization issue was correctly routed to the test-author as a test defect), and the honest evidence bundle. Costs fell from $9.19 real (T-001) to $5.26 (T-002) with pipeline time halved.

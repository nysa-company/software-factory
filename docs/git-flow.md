# Git flow and branch protection

Settings for the product repo's `main` branch. These make the factory's rules mechanical: nobody — agent or human — merges around the gates.

## Branch and pull-request lifecycle

- Create a short-lived branch for one ticket or coherent maintenance change; never work directly on `main`.
- Open a draft PR while implementation or evidence is incomplete. Mark it ready only after deterministic local gates pass.
- Keep the PR focused, update durable docs and memory with changed truth, and include a tested rollback path.
- Squash merge after required checks and operator approval so one ticket produces one durable change on `main`.
- Delete the merged branch. Keep no long-lived integration branches unless concurrency creates a measured need.
- The local plugin AI review is a pre-publication check for this kit repository. It does not replace the factory Reviewer, Narrator evidence bundle, or human approval for product tickets.

## Selective CI

The required `ci` workflow always starts and always reports its aggregate
status. A fail-closed classifier may skip expensive product tests when the
complete diff is non-empty and limited to the documented inert-metadata
allowlist. In this kit that includes the documentation roots plus `AGENTS.md`,
`CLAUDE.md`, the pull-request template, the Hermes changelog, and the
conformance shakedown report. Repository baseline, secret, artifact, and
test-immutability checks still run.

Every behavioral pull request runs fail-closed component selection on Linux.
Applicable shell/platform-sensitive pull requests run the same selection under
macOS system Bash in parallel. Unknown, shared, mixed, dependency, CI, selector,
addition, deletion, and rename changes resolve to full. Every push to `main`
runs both complete suites so release verification remains bound to a fully
tested merged SHA. Instantiated product workflows may skip expensive product
checks for allowlisted PRs, but every product push to `main` runs its full
verification so deployment evidence remains bound to the merged SHA.

Do not infer safety from commit size, commit messages, or a Markdown suffix:
one-line code and executable Markdown prompt changes require full CI. Do not
put `paths-ignore` on a required workflow; GitHub leaves its check pending
instead of reporting success. Mixed changes, renames from outside the
allowlist, and executable Markdown all run at least Linux CI; missing
comparison commits and classifier errors run both platforms. Each product owns
its instantiated helper and must review any allowlist change against paths
that can affect its runtime.

## Parallel kit development

Kit development is parallel; production release selection is serialized.
Developers may keep multiple focused branches and linked worktrees open at the
same time:

```bash
git fetch origin main
git worktree add ../sf-worktrees/<feature> -b feat/<feature> origin/main
```

Each worktree owns one coherent change, runs the managed local selector, and
opens its own protected PR. Broad verification may be explicitly deferred to
required GitHub CI; targeted checks and policy gates are never reported as
deferred. Never share uncommitted files, a branch, or a worktree between
features. Rebase or merge the latest protected `main` before final checks when
another PR lands first. A merge produces one independently addressable
candidate SHA; it does not update any live product.

For every candidate that may reach production:

1. wait for its protected `ci` and `test-immutability` checks;
2. install that exact merged SHA as a sealed release;
3. certify the exact release/product-tree tuple;
4. run an isolated real-Hermes canary when a compatibility-sensitive surface
   changed;
5. open a separate protected product `KIT_PIN` PR; and
6. activate only at a ticket boundary under maintenance and the shared launch
   barrier.

Only one product activation or rollback may run at a time. Do not activate a
second candidate while a ticket has a nonterminal lease, a run is active, or an
activation journal is incomplete. Contract `1.1.0` remains single-ticket by
default and permits an explicit bounded pilot from two through four tickets
when the product sets `MAX_CONCURRENT_TICKETS` accordingly; each ticket keeps
its own exact branch, linked worktree, opaque lease, and sequential role flow.
Those leases may coexist, but a product-level control lock serializes provider
intervals until an OS-enforced writer boundary makes parallel providers safe.

## Kit release lifecycle

A merge to this repository's `main` creates an eligible candidate, not a live
deployment. Release and product changes remain separate protected flows:

1. Merge the focused kit PR after required checks.
2. Install the exact full SHA into
   `~/.factory/kits/releases/<full-sha>/`. Installation verifies that the SHA
   is on fetched `origin/main` and that the required GitHub checks succeeded.
3. Open a product PR changing only the product's full `factory/KIT_PIN` and
   any intentional product compatibility changes.
4. Certify the exact kit SHA/tree plus merged product tree/config tuple.
5. Run the real-Hermes canary for the first cutover and every
   compatibility-sensitive release.
6. Activate only under maintenance, a drained launch barrier, and an
   unexpired matching receipt.

The first role launch records `Kit-SHA:` on the ticket. Never move an in-flight
ticket to a new release by editing that line. Finish it on the leased release,
or use a separately reviewed and tested operator migration.

Changes to the public launcher manifest, command arguments, JSON schemas,
status/exit semantics, supported Hermes version, canonical profile/skill
templates, launcher, doctor, dispatcher contract, preflight, sequencer, run
wrapper, or reorder helper are compatibility-sensitive. Preserve contract
`1.0.0` behavior or bump the contract and include an explicit bootstrap/profile
migration plus retained rollback support.

Contract `1.1.0` preserves 1.0 behavior when `MAX_CONCURRENT_TICKETS` is absent
or `1`; opt-in values `2` through `4` require the 1.1 launcher, profile skill,
and dispatcher role contract to move together. Contract `1.2.0` inherits that
lease behavior unchanged while requiring ticket worktrees for preflight and
sequencing.

**Use a repository ruleset, not classic branch protection.** Verified by live probe (2026-07-12, dispatcher trial setup): a write deploy key pushed straight through classic branch protection to `main`, and also through a ruleset whose bypass list included the repository-admin *role* (deploy keys inherit it). Deploy keys — how agent machines authenticate — are only blocked by a ruleset whose bypass list contains **no repository roles**. Probes to run after any change to these settings: agent key pushes to `main` (must be rejected), agent key pushes a ticket branch (must succeed).

## Ruleset for `main` (Settings → Rules → Rulesets, or CLI below)

- Require a pull request before merging; no direct pushes.
- Required status checks: `ci` and `test-immutability`, strict (branch up to
  date). The `ci` context is an aggregate job that succeeds only when both the
  Linux suite and the macOS system-Bash suite succeed. A separate green Linux
  or macOS job is not sufficient.
- Block force pushes and deletion.
- Bypass list: **empty**. The release verifier fails closed on any bypass actor,
  including organization admins, because the certified commit must be proven to
  have passed the same protected path as every other release.
- Required approving reviews: 1 once the org has ≥2 humans. At single-operator stage set it to 0 — GitHub forbids approving your own PR, so a sole human with a 1-review rule can never merge anything. Merge-by-operator stays mechanical anyway: deploy keys cannot merge PRs (git-only credential), and `main` rejects their direct pushes.
- Concurrent ticket pilots produced measured rebase/recheck contention. Prefer
  the forge's merge queue or auto-merge when it preserves strict required
  checks, squash history, and operator approval; otherwise merge sequentially
  and rebase/recheck the remaining PR.

## CLI

```bash
cat <<'JSON' | gh api repos/OWNER/REPO/rulesets -X POST --input -
{
  "name": "main-pr-only",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [
    { "type": "pull_request", "parameters": { "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false, "require_code_owner_review": false, "require_last_push_approval": false, "required_review_thread_resolution": false, "allowed_merge_methods": ["squash"] } },
    { "type": "required_status_checks", "parameters": { "strict_required_status_checks_policy": true, "required_status_checks": [ { "context": "ci", "integration_id": 15368 }, { "context": "test-immutability", "integration_id": 15368 } ] } },
    { "type": "non_fast_forward" },
    { "type": "deletion" }
  ],
  "bypass_actors": []
}
JSON
```

Requires a paid plan for private repos (GitHub Team or the repo being public).

Merge itself is triggered by the operator's approval on the Narrator bundle (manually at pilot stage: operator clicks merge after approving; automated later).

Release rollback also uses protected Git. Use `chore/<slug>-revert` as the
canonical revert branch name. Keep `MAINTENANCE`, stop the product's
factory services, and reconcile any interrupted activation first. Then merge a
normal protected revert restoring the previous `KIT_PIN` and product tree. Run
`factory-kit rollback` only if the candidate generation is committed and still
active; rollback keeps maintenance in place. Do not add a local pin bypass to
meet the rollback target.

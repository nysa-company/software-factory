# Branch protection

Settings for the product repo's `main` branch. These make the factory's rules mechanical: nobody — agent or human — merges around the gates.

**Use a repository ruleset, not classic branch protection.** Verified by live probe (2026-07-12, dispatcher trial setup): a write deploy key pushed straight through classic branch protection to `main`, and also through a ruleset whose bypass list included the repository-admin *role* (deploy keys inherit it). Deploy keys — how agent machines authenticate — are only blocked by a ruleset whose bypass list contains **no repository roles**. Probes to run after any change to these settings: agent key pushes to `main` (must be rejected), agent key pushes a ticket branch (must succeed).

## Ruleset for `main` (Settings → Rules → Rulesets, or CLI below)

- Require a pull request before merging; no direct pushes.
- Required status checks: `ci` and `test-immutability`, strict (branch up to date).
- Block force pushes and deletion.
- Bypass list: **organization admin only** (the human operator). Never a repository role, never the agents' credentials.
- Required approving reviews: 1 once the org has ≥2 humans. At single-operator stage set it to 0 — GitHub forbids approving your own PR, so a sole human with a 1-review rule can never merge anything. Merge-by-operator stays mechanical anyway: deploy keys cannot merge PRs (git-only credential), and `main` rejects their direct pushes.
- No merge queue at single-builder stage — it queues nothing; add it only when concurrency arrives.

## CLI

```bash
cat <<'JSON' | gh api repos/OWNER/REPO/rulesets -X POST --input -
{
  "name": "main-pr-only",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [
    { "type": "pull_request", "parameters": { "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false, "require_code_owner_review": false, "require_last_push_approval": false, "required_review_thread_resolution": false, "allowed_merge_methods": ["merge", "squash"] } },
    { "type": "required_status_checks", "parameters": { "strict_required_status_checks_policy": true, "required_status_checks": [ { "context": "ci" }, { "context": "test-immutability" } ] } },
    { "type": "non_fast_forward" },
    { "type": "deletion" }
  ],
  "bypass_actors": [ { "actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always" } ]
}
JSON
```

Requires a paid plan for private repos (GitHub Team or the repo being public).

Merge itself is triggered by the operator's approval on the Narrator bundle (manually at pilot stage: operator clicks merge after approving; automated later).

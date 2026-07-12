# Branch protection

Settings for the product repo's `main` branch. These make the factory's rules mechanical: nobody — agent or human — merges around the gates.

## GitHub settings (Settings → Branches → add rule for `main`)

- Require a pull request before merging; no direct pushes.
- Required status checks: `ci` (lint, typecheck, tests, build, snapshots) and `test-immutability`.
- Require branches to be up to date before merging.
- Do not allow bypassing the above (including administrators).
- No merge queue at single-builder stage — it queues nothing; add it only when concurrency arrives.

## Or via CLI

```bash
gh api repos/OWNER/REPO/branches/main/protection -X PUT \
  -f 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=ci' \
  -f 'required_status_checks[contexts][]=test-immutability' \
  -f 'enforce_admins=true' \
  -f 'required_pull_request_reviews[required_approving_review_count]=0' \
  -F 'restrictions=null'
```

Merge itself is triggered by the operator's approval on the Narrator bundle (manually at pilot stage: operator clicks merge after approving; automated later).

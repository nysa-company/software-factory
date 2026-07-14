## Summary

- What changed:
- Why it changed:

## Factory invariants

- [ ] Builder/test-author separation and test-first commit order are preserved.
- [ ] Engine contract changes are reflected in `FACTORY.md` and the relevant runbook.
- [ ] Product-specific state remains outside the reusable kit surface.

## Verification

- [ ] `bash ci/test-all.sh`
- [ ] `scripts/repo-check`
- [ ] `scripts/secret-scan`
- Exact results or reason a check does not apply:

## Safety

- [ ] No secret files, credentials, private data, or raw agent run output were added.
- [ ] Existing raw run history was not removed or rewritten without operator approval.

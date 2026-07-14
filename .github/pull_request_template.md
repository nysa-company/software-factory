## Summary

- Ticket or maintenance scope:
- Frozen spec or contract:
- What changed and why:

## Verification

- [ ] Test-author changes precede Builder implementation, or this change does not use that split.
- [ ] Builder commits do not modify protected tests.
- [ ] Ran `scripts/repo-check`.
- [ ] Ran `scripts/secret-scan`.
- [ ] Ran `scripts/artifact-check`.
- [ ] Ran `bash ci/test-all.sh`.
- [ ] Local AI review passed.
- Checks and results:

## Safety

- [ ] No credentials, private data, unsafe model output, or local artifacts were added.
- [ ] External actions retain their sandbox, destination, idempotency, and approval controls.

## Risk and rollback

- Risk level and likely failure mode:
- Rollback or recovery steps:

## Docs and memory

- [ ] Durable docs were updated when product, architecture, workflow, or operating truth changed.
- [ ] `context/memory.md` was updated when cross-session truth changed.
- Evidence or reason no update was needed:

## Narrator evidence

- Preview, criteria table, screenshots, cost, and final evidence-bundle link when required:

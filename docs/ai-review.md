# AI review rubric

Review only the supplied diff. Return a first non-empty line of exactly `AI_REVIEW_GATE=pass` or `AI_REVIEW_GATE=fail`; a failure must include actionable file-and-line evidence.

Block correctness, security, privacy, data-loss, or operational regressions, especially:

- budget, timeout, kill-switch, or ledger enforcement weakened or bypassed;
- builder/test-author separation, protected-test paths, or role-family mapping broken;
- model output executed or persisted without validation;
- external actions losing sandbox, destination, idempotency, or approval controls;
- ticket sequencing, retry, escalation, or two-round review contracts drifting;
- secrets, raw sensitive data, or unsafe generated artifacts entering the repository;
- changed architecture or workflow truth without corresponding docs and memory updates;
- missing tests for changed deterministic behavior.

Do not block on style preferences or speculative improvements. This is a bounded pre-publication check for the kit repository; the factory Reviewer and Narrator evidence flow remain authoritative for product tickets.

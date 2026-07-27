# Sandbox Factory successor candidate — 2026-07-27

## Executable candidate

- Predecessor: `aeaf260a6e82fb7543ff0b3a87661637a05af8d3`
- Intermediate successors:
  - `d79819d83b0982c201575d3edb49342c08410960`
  - `70ce454d1bd20a86c852dc816db75bdad1bde436`
- Current successor: `655020b610fffe73b005679cba86b91e3cc92469`
- Focused verification:
  - `bash ci/ticket-state-test.sh` — PASS at intermediate successor `70ce454`
  - `bash ci/factory-dev-lane-test.sh` — PASS at the current successor

The successor fixes four Factory-core defects found after the first roles of
the proposed final four had started:

1. fresh one-ticket lanes no longer pass a nonexistent checkpoint path into
   trusted Reviewer reconciliation;
2. authenticated cancellation recovery accepts the observed macOS SIGTERM
   status 143 as well as shell status 130;
3. checkpoint-free trusted Reviewer reconciliation no longer expands an empty
   optional-argument array under macOS Bash 3.2;
4. authenticated `FIX planner` and `FIX spec-linter` contract repairs map to
   their already-authorized roles instead of stopping before submission.

No broad local Factory CI, product CI, Hermes suite, pixel-perfect gate,
Factory promotion, or manual deployment was run.

## Drain evidence

T-079, T-081, T-083, and T-085 each have zero active provider attempts,
dispatcher claims, leases, PID files, dirty worktrees, or matching processes.
T-083's interrupted Builder output is retained only on diagnostic ref
`00d2c50e396c664b26e0650ecc8a09f0008e21fc`; it was not promoted.

## Qualification consequence

The six accepted merges remain valid. The clean-final-four streak resets to
zero because Factory code changed after the proposed final four's first role
starts. Their successful roles remain immutable and cannot be replayed or
relabelled as successor-SHA evidence.

The operator authorized the extended proof: finish the retained four without
successful-role replay, then use T-086 through T-089 as the clean successor-SHA
final four. No new successor role may start until the Generation 9 evidence
binds the current executable SHA above.

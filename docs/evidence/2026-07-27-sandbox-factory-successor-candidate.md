# Sandbox Factory successor candidate — 2026-07-27

## Executable candidate

- Predecessor: `aeaf260a6e82fb7543ff0b3a87661637a05af8d3`
- Successor: `d79819d83b0982c201575d3edb49342c08410960`
- Focused verification: `bash ci/factory-dev-lane-test.sh` — PASS

The successor fixes two Factory-core defects found after the first roles of
the proposed final four had started:

1. fresh one-ticket lanes no longer pass a nonexistent checkpoint path into
   trusted Reviewer reconciliation;
2. authenticated cancellation recovery accepts the observed macOS SIGTERM
   status 143 as well as shell status 130.

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

The canonical handoff therefore requires operator authorization before adding
four new approved tickets for a clean successor-SHA final four. Until that
ruling, no ticket is admitted or published.

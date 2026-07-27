# Sandbox Factory successor candidate — 2026-07-27

## Executable candidate

- Predecessor: `aeaf260a6e82fb7543ff0b3a87661637a05af8d3`
- Intermediate successors:
  - `d79819d83b0982c201575d3edb49342c08410960`
  - `70ce454d1bd20a86c852dc816db75bdad1bde436`
  - `655020b610fffe73b005679cba86b91e3cc92469`
  - `5d611470182614f26fccc61eb751360dfc27c473`
  - `c64a9247d566198803ff429f24535bfd057c2618`
  - `805de58cd6be311cbe2046da3a62a5d73be8ad85`
  - `39240c4fcdd18e4cc274f878d70de6bb14189f51`
  - `4e68e0b11d18c55c24c5a75d6f556727337d37f3`
  - `48478c8f5a9d4181e83d6352c535d6839a34bef5`
  - `592d57f2d2d6e656b6349fe83d5a8726c19b3d59`
- Current successor: `31c56c0ed4204703093ad6dd734b202f792746d9`
- Focused verification:
  - `bash ci/ticket-state-test.sh` — PASS at intermediate successor `805de58`
  - `python3 ci/cursor-stream-test.py` — PASS at intermediate successor
    `4e68e0b`
  - `bash ci/factory-dev-lane-test.sh` — PASS at intermediate successor
    `c64a924`
  - publication-repair parser and portable `FIX test-author` checkpoint
    sequencing fixtures — PASS at intermediate successor `592d57f`
  - exact T-081 zero-attempt failed-plan checkpoint re-export — PASS at the
    current successor

The successor fixes eleven Factory-core defects found after the first roles of
the proposed final four had started:

1. fresh one-ticket lanes no longer pass a nonexistent checkpoint path into
   trusted Reviewer reconciliation;
2. authenticated cancellation recovery accepts the observed macOS SIGTERM
   status 143 as well as shell status 130;
3. checkpoint-free trusted Reviewer reconciliation no longer expands an empty
   optional-argument array under macOS Bash 3.2;
4. authenticated `FIX planner` and `FIX spec-linter` contract repairs map to
   their already-authorized roles instead of stopping before submission;
5. portable Spec-lint evidence accepts the same normal Markdown indentation at
   replay that checkpoint export already normalizes;
6. a selected completed ticket may export after its ticket approval is
   consumed even though the unused compatibility batch approval remains inert;
7. a retained lane's authenticated contract blocker is bound to the immutable
   Factory checkout that executed it, not a later qualification candidate;
8. Reviewer reconciliation normalizes the same named-shell callback form that
   the trusted Cursor role wrapper accepted, preserving one canonical verdict
   and repair owner;
9. unmatched Reviewer evidence remains valid across an ancestor chain confined
   to that ticket's operator-owned documentation, while any source, test, or
   other-file change still fails closed;
10. a protected-CI failure can reopen an authenticated operator-await
    checkpoint for exactly its named Builder or Test-author repair, then
    requires fresh Reviewer and Narrator evidence before returning to
    operator-await;
11. a failed plan with zero provider attempts can re-export its consumed
    accounting lineage even though no runtime ledger was created.

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

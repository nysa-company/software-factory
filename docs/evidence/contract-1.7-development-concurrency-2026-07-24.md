# Contract 1.7 development concurrency evidence — 2026-07-24

This is development-lane evidence only. It does not certify, register, activate,
or promote a Software Factory release.

## Outcome

- A disposable four-ticket subscription-CLI batch completed in 4,239 seconds
  with 26 terminal provider attempts, maximum observed overlap 4, and zero
  successful-role replay.
- A fresh corrected two-ticket batch reached its first terminal boundary in
  3,511 seconds. T-050 completed; T-054 stopped fail-closed at Reviewer
  reconciliation while retaining every successful role.
- T-054 resumed only its Test-author repair and Reviewer in 1,126 seconds.
  Planner, Spec-linter, the original Test-author, and Builder were not replayed.
- T-052, T-053, T-050, and T-054 each reached `AWAIT-OPERATOR`, passed clean
  Node 22 publication gates and protected GitHub checks, and were squash-merged:

  | Ticket | Pull request | Merge commit |
  | --- | --- | --- |
  | T-052 | nysa-app #191 | `553a10b3ba27fa515779256474fe05c7e9a3ff02` |
  | T-053 | nysa-app #190 | `d426294adbb64f4ee2dadc654f281163573a6947` |
  | T-050 | nysa-app #192 | `53d3f7333be47e0660b835c881db9f0471785f49` |
  | T-054 | nysa-app #193 | `bbcc2acaebef86c1ba1414be7ae1f8339b3ec294` |

The user-observed baseline was more than 16 hours for four tickets, or more
than 240 minutes per ticket. The four successful tickets in this exercise each
completed their active Factory lifecycle within an upper bound of 77 minutes
(69 minutes average upper bound), a reduction of at least 71% and at least
3.4x faster per ticket. T-054 recovery took 18 minutes 46 seconds, so the
15-minute recovery target remains missed by 3 minutes 46 seconds.

## Concurrency and control evidence

- The initial four calls submitted within one scheduling wave and overlapped
  through the shared coordinator. Global capacity 4 was observed.
- Cursor remained capped at 2. Codex-native capacity admitted independent
  siblings while both Cursor slots were occupied.
- Capacity waits remained pre-GO and zero-charge. Waiting did not hold the
  launch lock; dispatch leases renewed during provider overlap.
- A stopped T-054 Reviewer reconciliation neither cancelled nor stalled T-050.
- Every provider attempt terminalized. No active reservation, dispatch lease,
  cancellation request, or provider process remained after either run.
- Trusted-host commits owned ticket state and publication. Provider-created
  remote changes were not accepted.
- Publication mailboxes preserved tests-first and implementation-second
  strata. GitHub test-immutability checks passed for all four merged PRs.
- No API key was used. The disposable lane used only its owner-only copied
  subscription-CLI sessions and lane-local homes, profiles, runtime, locks,
  ledgers, worktrees, database environments, and temporary paths.

## Factory defects demonstrated and repaired

1. Development role state diverged from shared Hermes sequencing. Development
   now uses the shared transition mechanism before provider GO:
   `Planning → Planning → Building → Building → Review`.
2. Cursor could append a semantically identical callback restatement after a
   valid Reviewer verdict. The shared parser now accepts only an identical
   later restatement and still refuses conflicts, malformed primary output, or
   extra standalone pairs.
3. Resume planning resolved `next-stage` before deterministic Reviewer
   reconciliation. It now reconciles under the claimed lease, binds the
   post-reconciliation head and stage into the approval, and releases the lease
   on every path.
4. Optional per-ticket envelopes produced warnings and an implicit empty
   binding. Resume basis evidence now explicitly binds the safe ticket override
   or safe global fallback by source and SHA.
5. A fixed export directory prevented a later resumed sibling from exporting.
   `product-export` now supports an optional strict new lane-local output while
   retaining the original default.
6. Product worktrees lacked their required Node 22 dependency bootstrap. Each
   ticket now receives a sandboxed `npm ci`, verified Node 22 toolchain, clean
   tracked tree, and ticket-local dependency projection.
7. Contract-blocked roles could lose their durable reason. Contract 1.7 now
   authenticates an exact committed blocker marker and stops only that ticket.

All changes remain on the unmerged development branch. Contract 1.6 and the
serialized fallback are unchanged.

## Isolation sentinels

The focused suite created these exact replicas under a disposable caller home,
recorded their checksums before execution, and proved them byte-identical
after mock and four-call concurrency runs:

- `.factory/sentinel`
- `.hermes/profiles/factory/sentinel`
- `Library/LaunchAgents/sentinel`
- `Projects/nysa-company/nysa-app/sentinel`

The real protected production paths were not read or mutated:

- `~/.factory/bin/factory-launch`
- `~/.factory` active releases, registry, runtime, ledgers, locks, and services
- `~/.hermes/profiles/factory`
- production LaunchAgents and gateways
- production Factory credentials
- Nysa Linear state and `linear-map.json`
- production Factory ticket lifecycles

Nysa application `main` was intentionally changed only through the four
authorized protected PR merges listed above. Their diffs contained only the
approved backend source and test files; they did not publish lane ticket state,
Factory runtime state, credentials, or development evidence.

## Remaining risks

- Targeted recovery is substantially faster than replay but still exceeded the
  15-minute target by 3 minutes 46 seconds.
- The shared Nysa create-PR helper twice pushed successfully but returned only
  a generic creation failure; explicit `--base main --head <branch>` via the
  documented `gh` fallback succeeded. This needs a fail-loud helper regression.
- Cursor remains capped at 2 because its session/scratch bridge is shared.
  Native Claude remains circuit-broken pending a separate proof.
- No production promotion evidence exists. Promotion requires a separate
  explicit go/no-go, exact-SHA certification, certified disposable canary,
  drained maintenance window, health proof, and rollback rehearsal.

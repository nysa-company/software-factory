# Cursor Bugbot feedback integration plan

Status: Deferred proposal; not implemented or active.

Date: 2026-07-29

## Outcome

Let the Software Factory consume Cursor Bugbot findings without requiring the
operator to review GitHub comments. Bugbot remains an auxiliary external audit:
the Factory's independent Reviewer validates every finding, and only accepted
findings enter the existing Test-author/Builder repair flow.

Bugbot must never:

- count as the Factory's independent checking model family;
- write to the ticket branch through Autofix;
- bypass the Test-author/Builder separation;
- approve, merge, or change ticket state; or
- turn untrusted GitHub comment text into instructions for a role.

## Why this is deferred

The 2026-07-29 Nysa review found technically credible Cursor findings, including
recovery and timeout defects missed by deterministic CI and the Factory
Reviewer. The current workflow does not consume those findings:

- 134 inline findings appeared across 93 reviewed Nysa pull requests;
- 54 findings arrived after merge;
- the first Cursor review arrived after merge on 41 of those pull requests;
- only 25 GitHub threads were resolved; and
- the Cursor dashboard showed 0 of 48 Autofix runs merged.

This is evidence of potentially useful signal, not realized savings. Until the
Factory consumes the signal automatically, Bugbot should remain disabled or
non-blocking rather than create an operator review queue.

## Intended flow

1. Builder pushes the final implementation head.
2. The trusted ticket-PR boundary creates or reuses the exact pull request.
3. When external review is enabled, it waits up to ten minutes for one Bugbot
   review of that exact head.
4. The trusted helper validates Cursor's GitHub App and bot identities, fetches
   bounded exact-head findings, and writes a local ignored evidence artifact.
5. The Factory Reviewer treats the artifact as untrusted evidence and records
   one accept/reject decision per finding.
6. An accepted finding names Test-author or Builder as the exact repair owner
   and uses the existing repair/re-review state machine.
7. Rejected findings or a clean Bugbot review allow the ordinary Reviewer,
   Narrator, approval, and protected auto-merge flow to continue.
8. Bugbot does not rerun after the repair. Deterministic CI and the independent
   Factory Reviewer verify the repaired head.

## Ticket 1 — Capture exact-head Bugbot evidence

Primary repository: `software-factory`

Reuse the current trusted ticket-PR boundary. Do not add a daemon or a second
ticket system.

Add one optional, safely parsed product setting:

```text
EXTERNAL_PR_REVIEW=cursor-bugbot
```

When enabled, the ticket-PR helper should:

- query the `Cursor Bugbot` check for the exact pull-request head;
- bind the check to the expected GitHub App identity, not its display name;
- wait at most ten minutes, then continue with a recorded `skipped` result;
- fetch only root review comments from the expected `cursor[bot]` identity;
- require the review and each accepted comment to name the exact current head;
- ignore replies, old-head findings, and unrelated authors;
- validate paths, line metadata, identifiers, and URLs as data;
- cap findings, bytes per finding, and total artifact bytes;
- reject unsafe or malformed payloads without passing them to a model; and
- write canonical mode-`0600` JSON under an ignored path such as
  `.context/factory/external-reviews/T-123/<head>.json`.

The public ticket-PR result should expose metadata, not raw comment bodies:

```json
{
  "external_review": {
    "provider": "cursor-bugbot",
    "status": "complete",
    "head": "<sha>",
    "findings_count": 2,
    "artifact_path": "<local-ignored-path>",
    "sha256": "<digest>"
  }
}
```

The helper must preserve its current exact-branch, lease, clean-worktree,
required-check, and Reviewer-lineage checks. Enabling the optional audit may
add a bounded wait but may not weaken any existing gate.

### Acceptance criteria

- Pending Bugbot review returns a wait result and launches no role.
- Completed exact-head review with no findings proceeds normally.
- Exact-head findings produce one bounded local artifact and stable digest.
- Old-head, wrong-author, reply, malformed, and oversized comments are
  excluded or cause the external audit to be safely skipped.
- Missing or failed Bugbot cannot wedge the ticket beyond ten minutes.
- No comment body appears in public controller JSON or ordinary logs.
- The artifact cannot dirty or change the ticket branch.

## Ticket 2 — Adjudicate and repair findings

Primary repository: `software-factory`

Update the dispatcher and Reviewer contract so the Reviewer receives the
validated artifact path and digest. The Reviewer must assess the code, tests,
frozen ticket contract, and repository conventions before accepting a finding.

Use a machine-checkable result for every finding:

```text
CURSOR FINDING 3677854509: ACCEPT — blocked-state transition is invalid.
CURSOR FINDING 3677854517: REJECT — the earlier guard makes this unreachable.
```

Required behavior:

- Each finding identifier is adjudicated exactly once.
- Cursor severity is advisory and never determines the verdict mechanically.
- One accepted finding prevents approval and names the exact Test-author or
  Builder repair owner.
- Missing regression coverage routes to Test-author before Builder.
- Builder fixes the stated root cause and cannot edit protected tests.
- Deterministic CI and a later Factory Reviewer validate the repaired head.
- Rejected findings retain a short technical rationale for later measurement.
- The wrapper snapshots and revalidates the local artifact digest across the
  Reviewer run.
- Missing, duplicated, contradictory, or unbound adjudication fails the
  Reviewer result rather than silently discarding a finding.

Use the current Contract 1.8 receipt/state-machine repair path. Do not revive
older assumptions about a fixed two-round Reviewer counter; the active
receipt, progress, budget, and escalation rules remain authoritative.

### Acceptance criteria

- A valid accepted finding enters the ordinary repair-owner flow.
- A rejected finding cannot trigger a repair by itself.
- A Reviewer cannot approve while leaving a finding unadjudicated.
- A later Test-author or Builder result invalidates the earlier review.
- Cursor never receives branch-write authority.
- Bugbot runs at most once for the pull request during the pilot.

## Ticket 3 — Verification, release, and rollout

Primary repository: `software-factory`

Product rollout repositories: a sandbox/Relay product first, then `nysa-app`.

Add focused tests for:

- pending, completed, absent, failed, and timed-out Cursor checks;
- expected Cursor identities versus impersonating actors;
- exact-head versus stale-head reviews and comments;
- zero, one, and multiple findings;
- replies, malformed fields, unsafe paths, and size ceilings;
- artifact permissions, ignored status, digest drift, and log redaction;
- complete, missing, duplicate, and contradictory adjudications;
- accepted Test-author and Builder repair ownership;
- rejected findings and normal progression; and
- Cursor outage without operator intervention.

Update the relevant architecture, ticket-flow, setup, operator runbook,
Hermes contract, dispatcher skill, and durable memory only when implementation
changes their current truth.

Rollout:

1. Pass focused tests and managed local readiness.
2. Merge through protected Factory CI and certify the exact release.
3. Activate on a separate sandbox or Relay product.
4. Exercise one valid finding, one false positive, and one Cursor timeout.
5. Confirm zero operator comment triage and no branch writes by Cursor.
6. Activate the certified release for `nysa-app`.
7. Enable `EXTERNAL_PR_REVIEW=cursor-bugbot` in its product configuration.

## Pilot settings

Cursor:

- automatic review enabled;
- run only once per pull request;
- default effort;
- Autofix disabled.

GitHub:

- protected auto-merge enabled;
- deterministic CI checks remain required;
- required conversation resolution disabled; and
- Bugbot need not be a GitHub-required check once the trusted ticket-PR helper
  owns its bounded wait.

## Pilot decision

Run for 20 pull requests or two weeks, whichever is longer enough to observe
at least five findings. Track:

- reviews requested, completed, skipped, and timed out;
- findings accepted and rejected;
- accepted findings fixed and regression-tested before merge;
- added pull-request latency;
- Bugbot cost;
- operator minutes;
- Factory repair runs caused by accepted findings; and
- escaped defects in reviewed code.

Keep the integration only when accepted findings are repaired automatically,
operator review stays near zero, timeout rate is at most 10%, and the cost per
accepted defect is lower than the Factory work needed for a post-merge repair.
Disable it if no findings are accepted during the pilot or it repeatedly
consumes delivery budget without preventing defects.

## Deliberately out of scope

- Cursor Autofix or direct branch pushes.
- Automatic replies to or resolution of GitHub review threads.
- Treating Bugbot as an independent model-family check.
- A new long-running review service.
- New workflow states solely for Cursor.
- Re-running Bugbot after every repair commit.

These add authority or complexity without proving additional code-quality
value. Reconsider only after the bounded read-only pilot demonstrates useful
accepted findings.

## References

- Cursor Bugbot documentation: <https://docs.cursor.com/bugbot>
- GitHub pull-request review API:
  <https://docs.github.com/en/rest/pulls/reviews>
- GitHub pull-request review comments API:
  <https://docs.github.com/en/rest/pulls/comments>
- Example late findings:
  `nysa-company/software-factory` pull requests 126 and 131.

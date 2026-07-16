# Rollback drill

Use this operational drill before a product factory enters its pilot.

Rehearsed once at instantiation (required by the validator checklist) and available any time a merged change turns out wrong.

## The procedure

1. Find the PR that shipped the problem (the Done ticket links it).
2. `gh pr view N` to confirm, then revert on a canonical `chore/<slug>-revert` branch: use the GitHub UI "Revert" button, or `gh api` / `git revert -m 1 <merge-sha>` and open a PR.
3. The revert PR goes through CI like any change (fast — no review rounds needed for a pure revert; the operator approves directly).
4. Merge; staging redeploys; confirm the staging URL shows the old behavior.
5. Reopen the original ticket as a linked bug ticket (this counts as an escaped defect in metrics).

## Limits — what a revert cannot undo

- **Schema migrations:** a revert PR restores code, not data. Schema-change tickets carry their own tested down-migration per the evidence rubric; run it first, then revert the code.
- **External sends:** an email that went out is gone. That is why external-send bundles carry the "cannot be undone once live" warning and sandbox mode stays on until the operator flips it per connector.

## The drill (at instantiation)

Merge a trivial change (e.g. a visible footer text), revert it, confirm staging shows the original, and time the whole loop. Note the time in `factory/` — that number is the honest answer to "how fast can we undo a mistake."

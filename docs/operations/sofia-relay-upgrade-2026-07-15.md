# Sofia Relay factory upgrade — 2026-07-15

Sofia's Relay factory upgraded through protected Git and immutable releases to
software-factory release `35c2e10aae5d1ea5317bd4cfe5fbbac657a0728d`
(tree `78df41b462da689088f10b2f536497a20d614bf7`, contract `1.0.0`). The
release contains documentation/context changes only; the stable launcher and
integration definitions were unchanged.

Before the candidate cutover, generation 3 recertified the current Relay tree
`395918c324e04e7d98d26b4c5eedf523f07fc581` on retained release
`3b63cc71609676fe5fde30a878032e999df05976`. Contract, doctor, backend
readiness, product tests, service state, Linear freshness, and repeated probes
passed. Generation 4 then activated the successor against protected Relay
commit `5a850f4d2712c3511517eee3886f12962571220d` and product tree
`b2f868f606f3ef860406498c3e22f10bc1aec92e`, retaining generation 3 as its
previous record.

Protected Relay PR #16 remains a green draft rollback path. It restores the
old full pin and exactly reproduces product tree
`395918c324e04e7d98d26b4c5eedf523f07fc581`. On failure, keep maintenance,
stop the Relay services, reconcile any interrupted activation, merge and
deploy that protected revert, and only then call `factory-kit rollback` if
generation 4 is still committed and active.

The maintenance interval ran from `2026-07-15T16:34:18Z` to
`2026-07-15T16:56:17Z` (1,319 seconds). The five-minute target was missed and
remains unaccepted. Health passed before maintenance cleared, and the
post-resume proof at `16:56:41Z` found generation 4 active, the successor
selected by doctor, fresh healthy Linear state, no active run or launch lock,
and a stable gateway. Old/new releases, receipts, consumed copies, activation
journals, generation 3, and the rollback PR remain retained for the evidence
window.

Parallel kit branches, worktrees, PRs, and sealed candidates remain supported.
Relay pin merges, activations, rollbacks, and live dispatcher tickets remain
serialized.

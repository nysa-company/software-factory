# Git flow

- Branch from `main`; never commit directly to it.
- Ticket work: `ticket/T-NNN-<kebab-summary>`.
- Ledger close-out: `bookkeeping/T-NNN-closeout`.
- Maintenance: `<type>/<kebab-summary>` using the types allowed by `.agents/repo-standard.json`.
- Open a reviewed PR and require the repository baseline CI job before merge.
- Never push, merge, or open a PR without explicit user approval.

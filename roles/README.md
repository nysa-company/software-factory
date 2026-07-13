# Roles — versioning convention

Each role file is the system prompt for one factory role. Rules:

- **Version header.** Every file starts with `Version: N` and a one-line changelog entry per bump at the bottom of the file. Bump the version on any change, however small.
- **Regression examples.** Each role file ends with 1–3 worked examples of correct output. When a prompt changes, re-check the examples still hold; they are the prompt's regression test.
- **Cross-family rule.** Production roles (planner, builder, narrator) run on one model family, checking roles (spec-linter, test-author, reviewer) on the other — currently Codex/OpenAI produces and Claude checks (flipped 2026-07-13 to put the token-heavy runs on the cheaper family). Never collapse producers and checkers onto one family — independent judgment is the point.
- **Prompts are not enforcement.** Anything that must be true (budgets, test immutability, merge rights) is enforced by the wrapper, CI, or repo permissions. Prompts describe the job; mechanics enforce the rules.
- **Rollback.** Prompts live in git; a bad prompt change is reverted like any other regression, and the version header makes ticket-level attribution possible (each run logs the prompt versions it used).

# Roles — versioning convention

Each role file is the system prompt for one factory role. Rules:

- **Version header.** Every file starts with `Version: N` and a one-line changelog entry per bump at the bottom of the file. Bump the version on any change, however small.
- **Regression examples.** Each role file ends with 1–3 worked examples of correct output. When a prompt changes, re-check the examples still hold; they are the prompt's regression test.
- **Cross-family rule.** Production roles (planner, builder, narrator) require the OpenAI family; checking roles (spec-linter, test-author, reviewer) require the Anthropic family. Codex and Claude Code are primary. A pre-execution Cursor fallback may use only the matching family-typed adapter. Never collapse producers and checkers onto one family — independent judgment is the point. If both sides use Cursor fallback they share Cursor's agent runtime, so the invariant promises provider-family independence, not full runtime independence.
- **Prompts are not enforcement.** Anything that must be true (budgets, test immutability, merge rights) is enforced by the wrapper, CI, or repo permissions. Prompts describe the job; mechanics enforce the rules.
- **Rollback.** Prompts live in git; a bad prompt change is reverted like any other regression, and the version header makes ticket-level attribution possible (each run logs the prompt versions it used).

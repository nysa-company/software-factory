# Architecture

## Engine model

The kit is installed once per machine and shared by every product. Product repositories contain only their own state. Every run separates `KIT_DIR` (this repository's adapters, sequencer, prompts, and templates) from `FACTORY_ROOT` (the product repository's tickets, ledger, envelope, and configuration):

```bash
cd <kit clone> && FACTORY_ROOT=<product repo> bash scripts/run-agent.sh --role <role> \
  --ticket <T-NNN> --prompt-file roles/<role>.md [--workdir <worktree>] -- "task text"
```

- **Kit:** scripts, adapters and version pins, role contracts, workflows, runbooks, and CI templates. Fixes land once through reviewed PRs.
- **Product repository:** `factory/` state (including initiatives and tickets), product documentation, instantiated CI, GitHub rules, and deploy credentials. All products share the Software Factory Linear team; each initiative gets a Linear Project.
- **`factory/KIT_PIN`:** the certified kit commit. `scripts/preflight.sh` refuses a mismatch so upgrades cannot alter product behavior silently.
- **`factory/PROJECT.env`:** product name, repository slug, protected test paths, worktree location, and ticket branch prefix.

Per-product limits live in each product's `ENVELOPE.env`; the machine limit in `~/.factory/global.env` caps aggregate spend.

Backend policy is kit-owned and certified by the same `KIT_PIN`. Production roles require the OpenAI family (`codex`, with optional `cursor-openai` fallback); checking roles require the Anthropic family (`claude-code`, with optional `cursor-anthropic` fallback). Fallback is resolved before task submission and is never a retry: one logical role run submits its task to at most one agent process. Machine-specific Cursor model IDs, the approved CLI compatibility version, and `FACTORY_CURSOR_FALLBACK_ENABLED` live in `~/.factory/global.env`. Exact model IDs must be present in `scripts/lib/cursor-model-families.txt`; `auto` is forbidden.

```bash
# ~/.factory/global.env — no credentials in this file
export FACTORY_CURSOR_FALLBACK_ENABLED=0
export CURSOR_AGENT_VERSION="EXACT_VERSION_TOKEN"
export CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
export CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
# Optional observational estimate only; ledger still keeps the full reservation.
export CURSOR_PRICING_SNAPSHOT_DATE=YYYY-MM-DD
export CURSOR_OPENAI_USD_PER_MTOK_IN="RATE"
export CURSOR_OPENAI_USD_PER_MTOK_OUT="RATE"
export CURSOR_ANTHROPIC_USD_PER_MTOK_IN="RATE"
export CURSOR_ANTHROPIC_USD_PER_MTOK_OUT="RATE"
```

Enable fallback only after `agent status --format json`, `agent models`, `scripts/adapters/contract-test.sh --routes`, and both conformance smokes pass. Cursor output is redacted while streaming; the redacted `.out` artifact remains local and ignored, while the manifest and ledger carry durable provenance.

## Role and approval flow

Planner, Builder, and Narrator use the production model family. Spec-linter, Test-author, and Reviewer use a separate checking family. Narrator converts verified results into the evidence bundle the operator approves. The exact lifecycle and failure routes live in [workflows/ticket-flow.md](workflows/ticket-flow.md).

## Trust boundaries

- Model output is untrusted data: validate it before persistence and never interpolate it into commands, queries, or HTML.
- The wrapper owns budget and timeout enforcement; role prompts cannot weaken it.
- Builders cannot change protected tests; CI checks commit authorship and paths.
- Product credentials stay in GitHub or the hosting platform, never in repositories or agent output.
- External sends require sandboxing or allowlisting, an explicit destination, and irreversible-action evidence.
- The local plugin AI review is pre-publication hygiene for changes to this kit. It does not replace the factory's independent Reviewer, Narrator bundle, or human approval.

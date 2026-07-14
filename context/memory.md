# Memory — software-factory decision log

Entry format: `## YYYY-MM-DD — Short title`, then `Category: Decision | Preference | System change | Context`, then 1–3 sentences. Mark superseded entries clearly and number durable decisions within this repository.

## Current truth

- This repository is a product-agnostic factory kit; product repositories carry only their factory state and CI integration.
- Durable decisions in this repository use their own numbering, beginning at Decision 1.
- Backend fallback is pre-execution selection: production stays OpenAI-family, checking stays Anthropic-family, and one logical role run submits its task to at most one agent process.
- The shared repository baseline v2 is declared in `.agents/repo-standard.json` and enforced by `scripts/repo-check` plus redacted secret scanning.
- Autonomous Codex runs use the repository permission profile; checking roles receive role-scoped tools. Raw run output stays local under `.context/factory-runs/`.

## Log

## 2026-07-13 — Decision 1: Repository-local decision numbering

Category: Decision

Software Factory decisions are numbered independently from Nysa product decisions. This keeps reusable tooling history separate from product history.

## 2026-07-13 — Decision 2: Family-typed Cursor fallback

Category: System change

Codex remains the production primary and Claude Code the checking primary. Optional Cursor adapters preserve those provider families and may be selected only by non-task probes before reservation and task submission; every post-submission failure stops without automatic retry.

## 2026-07-13 — Decision 3: Repository baseline and agent containment

Category: System change

Adopted the shared repository baseline with secret/history scanning, branch conventions, Conductor/PR setup, and scoped conformance instructions. Agent worktrees reject secret-bearing files, adapters use constrained permissions, and raw run output stays outside git.

## 2026-07-13 — Baseline v2 and shared-skill promotion

Category: System change

Upgraded to baseline v2 while preserving the factory's custom branch, CI, Conductor, PR, and conformance policies. Repeated cross-repository workflows now graduate through the plugin's reviewed `/skill-promote` process; monthly config review is no longer a default.

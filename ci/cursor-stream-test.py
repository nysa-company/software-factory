#!/usr/bin/env python3
"""Focused fail-closed coverage for Cursor's subscription stream."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
from cursor_model_identity import approved_reported_models, listed_reported_model


STREAM = ROOT / "scripts/lib/cursor-stream.py"
ADAPTER = ROOT / "scripts/adapters/cursor-anthropic.sh"
VERDICT = ROOT / "scripts/lib/reviewer-verdict.py"
RECONCILE = ROOT / "scripts/lib/reviewer-reconcile.py"
CATALOG = ROOT / "scripts/model-routing/catalog-v1.json"


class CursorStreamTest(unittest.TestCase):
    def run_verdict(
        self,
        events: list[dict | str],
        contract: str = "1.7.0",
        adapter: str = "cursor-anthropic",
    ) -> subprocess.CompletedProcess:
        if adapter == "codex" and (
            not events or not isinstance(events[0], dict)
            or events[0].get("type") != "thread.started"
        ):
            events = [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                *events,
            ]
        return self.run_verdict_text(
            "".join(json.dumps(event) + "\n" for event in events),
            contract=contract, adapter=adapter,
        )

    def run_verdict_text(
        self, stream_text: str, contract: str = "1.7.0",
        adapter: str = "cursor-anthropic",
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            stream = Path(raw) / "review.out"
            stream.write_text(stream_text)
            return subprocess.run(
                [
                    str(VERDICT),
                    "--adapter",
                    adapter,
                    "--input",
                    str(stream),
                    "--contract-version",
                    contract,
                    "--format",
                    "fields",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_claude_reviewer_decodes_one_terminal_json_result(self) -> None:
        review = (
            "## Verdict\n\n"
            "The fixture needs a test-only repair.\n\n"
            "REQUEST CHANGES\nFIX-OWNER: test-author"
        )
        result = self.run_verdict(
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "result": review,
                }
            ],
            adapter="claude-code",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\ttest-author\n")

    def test_claude_reviewer_refuses_multiple_terminal_results(self) -> None:
        result = self.run_verdict(
            [
                {"type": "result", "subtype": "success", "result": "APPROVE"},
                {"type": "result", "subtype": "success", "result": "APPROVE"},
            ],
            adapter="claude-code",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one successful result", result.stderr)

    def test_codex_reviewer_decodes_only_the_agent_verdict(self) -> None:
        review = "REQUEST CHANGES\nFIX-OWNER: test-author\n\nAdd the missing case."
        result = self.run_verdict(
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-0", "type": "agent_message",
                        "text": "I am inspecting the change.",
                    },
                },
                {
                    "type": "item.updated",
                    "item": {
                        "id": "item-plan", "type": "todo_list",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "command_execution",
                        "aggregated_output": "APPROVE",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-2", "type": "agent_message",
                        "text": "The focused checks are complete.",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-3", "type": "agent_message", "text": review,
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
            adapter="codex",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\ttest-author\n")

    def test_codex_reviewer_accepts_plain_and_bold_approval(self) -> None:
        for verdict in ("APPROVE", "**Approve**"):
            with self.subTest(verdict=verdict):
                result = self.run_verdict(
                    [
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item-1", "type": "agent_message",
                                "text": verdict,
                            },
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    ],
                    adapter="codex",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "APPROVE\t\n")

    def test_codex_reviewer_refuses_ambiguous_or_malformed_messages(self) -> None:
        for events in (
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "agent_message",
                        "text": "APPROVE",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-2", "type": "agent_message",
                        "text": "APPROVE",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "agent_message",
                        "text": "APPROVE\x00",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
            [
                {"type": "item.completed", "item": "invalid"},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "agent_message",
                        "text": "APPROVE\n" + "x" * 131_072,
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
        ):
            with self.subTest(events=events):
                result = self.run_verdict(events, adapter="codex")
                self.assertNotEqual(result.returncode, 0)

    def test_codex_reviewer_returns_bounded_canonical_detail(self) -> None:
        review = "Checks complete.\n\nAPPROVE\n\ntransport tail"
        stream = "\n".join(json.dumps(event) for event in [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1", "type": "agent_message", "text": review,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item-2", "type": "command_execution",
                    "output": "x" * 140_000,
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ])
        spec = importlib.util.spec_from_file_location("reviewer_verdict", VERDICT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            module.review_text(stream, "codex", "2.0.0"),
            review,
        )

    def test_codex_reviewer_accepts_only_the_exact_adapter_metrics_trailer(
        self,
    ) -> None:
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1", "type": "agent_message",
                    "text": "APPROVE",
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ]
        stream = "".join(json.dumps(event) + "\n" for event in events)
        accepted = self.run_verdict_text(
            stream + "turns=1 cost_usd=0.2083\n", adapter="codex",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(accepted.stdout, "APPROVE\t\n")
        refused = self.run_verdict_text(
            stream + "turns=2 cost_usd=0.2083\n", adapter="codex",
        )
        self.assertNotEqual(refused.returncode, 0)

    def test_codex_reviewer_requires_final_message_and_successful_turn(self) -> None:
        for events in (
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "agent_message",
                        "text": "APPROVE",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-2", "type": "agent_message",
                        "text": "Still reviewing.",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ],
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "agent_message",
                        "text": "APPROVE",
                    },
                },
                {"type": "turn.failed"},
            ],
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1", "type": "agent_message",
                        "text": "APPROVE",
                    },
                },
            ],
        ):
            with self.subTest(events=events):
                result = self.run_verdict(events, adapter="codex")
                self.assertNotEqual(result.returncode, 0)

    def test_codex_reviewer_refuses_malformed_json_but_preserves_plain_review(self) -> None:
        for stream in (
            '{"type":"turn.started"}\n{"type":\n',
            '{"type":"turn.started","type":"turn.completed"}\n',
            '{"type":"turn.started"}\n{"type":[]}\n',
            '{"type":"turn.started"}\n{}\n',
            '{"type":"turn.started"}\n[]\n',
            '{"type":"turn.started"}\n{"type":"turn.completed"}\n',
            '{}\n{"type":"turn.started"}\n',
            '{"result":"**APPROVE**"}\n',
            '{"type":"future.completed","result":"**APPROVE**"}\n',
            (
                '{"type":"item.completed","item":'
                '{"id":"item-1","type":"future_agent_message",'
                '"text":"APPROVE"}}\n'
                '{"type":"turn.completed","usage":'
                '{"input_tokens":1,"output_tokens":1}}\n'
            ),
            (
                '{"type":"item.completed","item":'
                '{"id":"item-1","type":"agent_message",'
                '"text":"APPROVE"}}\n'
                '{"type":"turn.completed","usage":'
                '{"input_tokens":1,"output_tokens":1}}\n'
            ),
            (
                '{"type":"thread.started","thread_id":"thread-1"}\n'
                '{"type":"item.completed","item":'
                '{"id":"item-1","type":"agent_message",'
                '"text":"APPROVE"}}\n'
                '{"type":"turn.completed","usage":'
                '{"input_tokens":1,"output_tokens":1}}\n'
            ),
            (
                '{"type":"thread.started","thread_id":"thread-1"}\n'
                '{"type":"turn.started"}\n'
                '{"type":"item.completed","item":'
                '{"id":"item-1","type":"agent_message",'
                '"text":"APPROVE"}}\n'
                '{"type":"turn.completed","usage":'
                '{"input_tokens":1,"output_tokens":1}}\n'
                'trailing plaintext\n'
            ),
        ):
            with self.subTest(stream=stream):
                result = self.run_verdict_text(stream, adapter="codex")
                self.assertNotEqual(result.returncode, 0)
        for stream in (
            '[P1] Review context: {"ok":true}\n\nAPPROVE\n',
            '{"ok":true}\n\nAPPROVE\n',
            '{"ok":true,"ok":false}\n\nAPPROVE\n',
        ):
            with self.subTest(plain=stream):
                plain = self.run_verdict_text(stream, adapter="codex")
                self.assertEqual(plain.returncode, 0, plain.stderr)
                self.assertEqual(plain.stdout, "APPROVE\t\n")

    def test_reviewer_prefers_one_terminal_bound_assistant(self) -> None:
        review = "Review complete.\n\nREQUEST CHANGES\nFIX-OWNER: builder"
        assistants = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": f"Progress {index}"}]
                },
            }
            for index in range(6)
        ]
        assistants.append(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": review}]},
            }
        )
        result = self.run_verdict(
            assistants
            + [
                {
                    "type": "result",
                    "subtype": "success",
                    "result": (
                        "Progress 0\nProgress 1\nProgress 2\nProgress 3\n"
                        f"Progress 4\nProgress 5\n{review}\n\nSummary:\n{review}"
                    ),
                },
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\tbuilder\n")

    def test_reviewer_accepts_explicit_markdown_verdict_heading(self) -> None:
        review = "Checks complete.\n\n## Verdict: APPROVE\n\nNo findings."
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {
                    "type": "result",
                    "subtype": "success",
                    "result": f"Checks finished.{review}",
                },
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "APPROVE\t\n")

    def test_reviewer_normalizes_exact_verdict_label_variants(self) -> None:
        for terminal in (
            "Verdict: APPROVE.",
            "**Verdict: APPROVE.**",
            "**Verdict: APPROVE**.",
            "## Verdict: APPROVE.",
        ):
            with self.subTest(terminal=terminal):
                review = f"Checks complete.\n\n{terminal}"
                result = self.run_verdict([
                    {"type": "assistant", "message": {"content": review}},
                    {"type": "result", "subtype": "success", "result": review},
                ])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "APPROVE\t\n")

        for terminal in (
            "Verdict: APPROVE.\nREQUEST CHANGES\nFIX-OWNER: builder",
            "The verdict is not APPROVE.",
            "I recommend approval.",
        ):
            with self.subTest(invalid=terminal):
                result = self.run_verdict([
                    {"type": "assistant", "message": {"content": terminal}},
                    {"type": "result", "subtype": "success", "result": terminal},
                ])
                self.assertNotEqual(result.returncode, 0)

    def test_reviewer_refuses_multiple_verdict_assistants(self) -> None:
        review = "REQUEST CHANGES\nFIX-OWNER: builder"
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple verdict-bearing assistants", result.stderr)

    def test_reviewer_refuses_duplicate_owner_inside_assistant(self) -> None:
        review = (
            "REQUEST CHANGES\nFIX-OWNER: builder\nFIX-OWNER: builder"
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires exactly one FIX-OWNER", result.stderr)

    def test_reviewer_refuses_terminal_contradiction(self) -> None:
        review = "REQUEST CHANGES\nFIX-OWNER: builder"
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {
                    "type": "result",
                    "subtype": "success",
                    "result": f"{review}\n\nAPPROVE",
                },
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contradicts the successful result", result.stderr)

    def test_reviewer_refuses_unbound_assistant(self) -> None:
        review = "REQUEST CHANGES\nFIX-OWNER: builder"
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "REQUEST CHANGES\nFIX-OWNER: test-author",
                },
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not bound to the successful result", result.stderr)

    def test_reviewer_normalizes_exact_background_callback_pair(self) -> None:
        review = (
            "Findings complete.\n"
            "FIX-OWNER: bothThe background `npm test` run finished with code 0.\n"
            "No follow-up action needed — my review above already stands as "
            "**REQUEST CHANGES / FIX-OWNER: both**."
        )
        result = self.run_verdict(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": review}]},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "result": f"{review}\n\nSummary:\n{review}",
                },
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\tboth\n")

    def test_reviewer_detail_drops_approval_background_callback(self) -> None:
        review = (
            "Review complete.\n\n"
            "APPROVEThat background `find /private/tmp/lane/home/node` "
            "finished with no further output."
        )
        stream = "\n".join(
            json.dumps(event) for event in [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        spec = importlib.util.spec_from_file_location("reviewer_verdict", VERDICT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        detail = module.review_text(stream, "cursor-anthropic", "1.7.0")
        self.assertEqual(detail, "Review complete.\n\nAPPROVE")

    def test_reviewer_normalizes_bold_background_callback_pair(self) -> None:
        review = (
            "REQUEST CHANGES\n\n"
            "FIX-OWNER: test-authorThat background `npm test` run finished "
            "with code 0.\n"
            "**REQUEST CHANGES — FIX-OWNER: test-author**"
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\ttest-author\n")

    def test_reviewer_normalizes_named_shell_callback_pair(self) -> None:
        review = (
            "REQUEST CHANGES\n\n"
            "FIX-OWNER: test-authorThat background shell (the first `npm test` "
            "run) finished with code 0.\n\n"
            "My round‑2 verdict stands: **REQUEST CHANGES**, "
            "`FIX-OWNER: test-author` — the named repair remains."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\ttest-author\n")

    def test_reviewer_normalizes_observed_markdown_callback_shapes(self) -> None:
        reviews = [
            (
                "## REQUEST CHANGES\n\n"
                "FIX-OWNER: test-authorBoth background commands completed.\n"
                "The verdict stands: **REQUEST CHANGES**, "
                "`FIX-OWNER: test-author`."
            ),
            (
                "### REQUEST CHANGES\n\n"
                "`FIX-OWNER: test-author`That background task completed."
            ),
            "## REQUEST CHANGES\n\n**FIX-OWNER: test-author**",
        ]
        for review in reviews:
            with self.subTest(review=review):
                result = self.run_verdict(
                    [
                        {"type": "assistant", "message": {"content": review}},
                        {"type": "result", "subtype": "success", "result": review},
                    ]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout, "REQUEST CHANGES\ttest-author\n"
                )

    def test_reviewer_allows_only_ticket_documentation_after_review(self) -> None:
        spec = importlib.util.spec_from_file_location("reviewer_reconcile", RECONCILE)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            ticket = repo / "factory/tickets/T-1.md"
            source = repo / "app.ts"
            ticket.parent.mkdir(parents=True)
            ticket.write_text("State: Review\n")
            source.write_text("export const value = 1;\n")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            reviewed = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            ticket.write_text("State: Review\n\nOperator note.\n")
            subprocess.run(["git", "commit", "-qam", "docs"], cwd=repo, check=True)
            documented = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertTrue(
                module.review_head_matches(ticket, "T-1", reviewed, documented)
            )
            source.write_text("export const value = 2;\n")
            subprocess.run(["git", "commit", "-qam", "product"], cwd=repo, check=True)
            changed = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertFalse(module.review_head_matches(ticket, "T-1", reviewed, changed))

    def test_reviewer_refuses_background_callback_owner_disagreement(self) -> None:
        review = (
            "FIX-OWNER: builderThe background `npm test` run finished with code 0.\n"
            "No follow-up action needed — my review above already stands as "
            "**REQUEST CHANGES / FIX-OWNER: both**."
        )
        result = self.run_verdict(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": review}]},
                },
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner contradicts its summary", result.stderr)

    def test_reviewer_ignores_identical_late_callback_restatement(self) -> None:
        review = (
            "Review complete.\n\n"
            "REQUEST CHANGES\n\n"
            "FIX-OWNER: test-author\n"
            "That background `npm test` run already completed. No further "
            "action needed — the review verdict stands as posted:\n\n"
            "**REQUEST CHANGES** — `FIX-OWNER: test-author` "
            "(the same two findings)."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REQUEST CHANGES\ttest-author\n")

    def test_reviewer_refuses_conflicting_late_callback_owner(self) -> None:
        review = (
            "REQUEST CHANGES\n"
            "FIX-OWNER: test-author\n\n"
            "**REQUEST CHANGES** — `FIX-OWNER: builder` (callback)."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contradicts its primary verdict", result.stderr)

    def test_reviewer_refuses_callback_without_primary_owner(self) -> None:
        review = (
            "REQUEST CHANGES\n\n"
            "**REQUEST CHANGES** — `FIX-OWNER: test-author` (callback)."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contradicts its primary verdict", result.stderr)

    def test_reviewer_refuses_extra_standalone_pair_before_callback(self) -> None:
        review = (
            "REQUEST CHANGES\n"
            "FIX-OWNER: test-author\n"
            "REQUEST CHANGES\n"
            "FIX-OWNER: test-author\n\n"
            "**REQUEST CHANGES** — `FIX-OWNER: test-author` (callback)."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contradicts its primary verdict", result.stderr)

    def test_reviewer_refuses_callback_after_approval(self) -> None:
        review = (
            "APPROVE\n\n"
            "**REQUEST CHANGES** — `FIX-OWNER: test-author` (callback)."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contradicts its primary verdict", result.stderr)

    def test_reviewer_callback_keeps_contract_16_owner_refusal(self) -> None:
        review = (
            "REQUEST CHANGES\n"
            "FIX-OWNER: test-author\n\n"
            "**REQUEST CHANGES** — `FIX-OWNER: test-author` (callback)."
        )
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
            ],
            contract="1.6.0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FIX-OWNER requires contract 1.7", result.stderr)

    def test_reviewer_keeps_terminal_only_compatibility(self) -> None:
        result = self.run_verdict(
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "Reviewed safely.\n\nAPPROVE",
                }
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "APPROVE\t\n")

    def test_reviewer_requires_one_success_terminal(self) -> None:
        review = "APPROVE"
        result = self.run_verdict(
            [
                {"type": "assistant", "message": {"content": review}},
                {"type": "result", "subtype": "success", "result": review},
                {"type": "result", "subtype": "success", "result": review},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one successful result", result.stderr)

    def run_stream(
        self,
        events: list[dict],
        repeated_error_limit: int,
        model: str = "claude-sonnet-5-thinking-high",
        reported_model: str = "claude-sonnet-5-thinking-high",
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics = root / "metrics"
            progress = root / "progress.jsonl"
            result = subprocess.run(
                [
                    str(STREAM),
                    str(metrics),
                    model,
                    reported_model,
                    str(root),
                    "4",
                    str(repeated_error_limit),
                    str(progress),
                ],
                input="".join(
                    (event if isinstance(event, str) else json.dumps(event)) + "\n"
                    for event in events
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            result.metrics = metrics.read_text()  # type: ignore[attr-defined]
            result.progress = progress.read_text()  # type: ignore[attr-defined]
            return result

    def test_usage_exhaustion_gets_one_typed_parser_diagnostic(self) -> None:
        exhausted = (
            "ActionRequiredError: Increase limits for faster responses. "
            "You're out of usage; increase your limit to continue."
        )
        result = self.run_stream(
            [{"type": "system", "subtype": "init"}, exhausted], 0
        )
        self.assertEqual(result.returncode, 10)
        self.assertIn(exhausted, result.stdout)
        self.assertEqual(
            result.stderr, "cursor stream provider usage exhausted\n"
        )

        for raw in (
            "ActionRequiredError: ordinary provider failure",
            exhausted + "\n" + exhausted,
        ):
            with self.subTest(raw=raw):
                generic = self.run_stream(
                    [{"type": "system", "subtype": "init"}, raw], 0
                )
                self.assertEqual(generic.returncode, 10)
                self.assertEqual(
                    generic.stderr,
                    "cursor stream has no terminal success result\n",
                )

    def test_exact_gpt_context_alias_normalizes_only_its_certified_route(self) -> None:
        events = [
            {
                "type": "system",
                "subtype": "init",
                "model": "GPT-5.6 Sol 272K High",
            },
            {"type": "result", "subtype": "success"},
        ]
        accepted = self.run_stream(
            events,
            0,
            model="gpt-5.6-sol-high",
            reported_model="GPT-5.6 Sol 1M High",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("reported_model=GPT-5.6 Sol 272K High", accepted.metrics)

        for identities in (
            ("GPT-5.6 Sol 1M Medium", "GPT-5.6 Sol 1M High"),
            ("GPT-5.6 Sol 1M High", "GPT-5.6 Sol 1M Medium"),
            ("GPT-5.6 Sol 272K High", "GPT-5.6 Sol 1M High"),
        ):
            with self.subTest(identities=identities):
                refused = self.run_stream(
                    [
                        {"type": "system", "subtype": "init", "model": identity}
                        for identity in identities
                    ]
                    + [{"type": "result", "subtype": "success"}],
                    0,
                    model="gpt-5.6-sol-high",
                    reported_model="GPT-5.6 Sol 1M High",
                )
                self.assertEqual(refused.returncode, 11)

    def test_exact_sonnet_vendor_alias_normalizes_only_its_certified_route(self) -> None:
        accepted = self.run_stream(
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "Claude Sonnet 5 300K High",
                },
                {"type": "result", "subtype": "success"},
            ],
            0,
            model="claude-sonnet-5-thinking-high",
            reported_model="Claude Sonnet 5 1M Thinking",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn(
            "reported_model=Claude Sonnet 5 300K High", accepted.metrics,
        )

        refused = self.run_stream(
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "Claude Sonnet 5 300K High",
                },
                {"type": "result", "subtype": "success"},
            ],
            0,
            model="claude-opus-5-thinking-medium",
            reported_model="Claude Opus 5 1M Medium Thinking",
        )
        self.assertEqual(refused.returncode, 11)

        for wrong_model, wrong_report in (
            ("gpt-5.6-sol-high", "GPT-5.6 Sol 1M Medium"),
            ("gpt-5.6-terra", "GPT-5.6 Sol 1M High"),
            ("claude-opus-5-thinking-medium", "GPT-5.6 Sol 1M High"),
        ):
            with self.subTest(model=wrong_model, report=wrong_report):
                refused = self.run_stream(
                    [
                        {
                            "type": "system",
                            "subtype": "init",
                            "model": wrong_report,
                        },
                        {"type": "result", "subtype": "success"},
                    ],
                    0,
                    model=wrong_model,
                    reported_model=wrong_model,
                )
                self.assertEqual(refused.returncode, 11)
                self.assertIn("unapproved model", refused.stderr)

    def test_enabled_cursor_model_names_follow_provider_contract(self) -> None:
        routes = [
            route
            for route in json.loads(CATALOG.read_text())["routes"]
            if route["enabled"] and route["transport"] == "cursor-cli"
        ]
        self.assertEqual(
            {route["provider_family"] for route in routes},
            {"anthropic", "openai"},
        )
        for route in routes:
            selection = route["selection_id"]
            canonical = route["expected_reported_identity"]
            approved = approved_reported_models(selection, canonical)
            with self.subTest(route=route["route_id"]):
                self.assertIn(selection, approved)
                self.assertIn(canonical, approved)
                if route["provider_family"] == "anthropic":
                    family = selection.removeprefix("claude-").split("-", 1)[0]
                    self.assertEqual(route["adapter"], "cursor-anthropic")
                    self.assertTrue(selection.startswith("claude-"))
                    self.assertTrue(canonical.startswith(
                        (f"{family.title()} ", f"Claude {family.title()} ")
                    ))
                else:
                    self.assertEqual(route["adapter"], "cursor-openai")
                    self.assertTrue(selection.startswith("gpt-"))
                    self.assertTrue(canonical.startswith("GPT-"))

    def test_enabled_cursor_runtime_names_remain_route_bound(self) -> None:
        routes = [
            route
            for route in json.loads(CATALOG.read_text())["routes"]
            if route["enabled"] and route["transport"] == "cursor-cli"
        ]
        identities = {
            route["route_id"]: approved_reported_models(
                route["selection_id"], route["expected_reported_identity"]
            )
            for route in routes
        }
        for route in routes:
            for identity in identities[route["route_id"]]:
                with self.subTest(route=route["route_id"], identity=identity):
                    accepted = self.run_stream(
                        [
                            {"type": "system", "subtype": "init", "model": identity},
                            {"type": "result", "subtype": "success"},
                        ],
                        0,
                        model=route["selection_id"],
                        reported_model=route["expected_reported_identity"],
                    )
                    self.assertEqual(accepted.returncode, 0, accepted.stderr)
            foreign = set().union(*(
                value for key, value in identities.items() if key != route["route_id"]
            ))
            for identity in foreign:
                with self.subTest(route=route["route_id"], foreign=identity):
                    refused = self.run_stream(
                        [
                            {"type": "system", "subtype": "init", "model": identity},
                            {"type": "result", "subtype": "success"},
                        ],
                        0,
                        model=route["selection_id"],
                        reported_model=route["expected_reported_identity"],
                    )
                    self.assertEqual(refused.returncode, 11)

    def test_cursor_inventory_identity_is_exact_and_unambiguous(self) -> None:
        selection = "claude-opus-5-thinking-medium"
        current = "Claude Opus 5 1M Medium Thinking"
        for raw in (
            f"{selection} - {current}\n",
            f"\x1b[32m{selection} - {current}\x1b[0m (current)\n",
            "Available models\n"
            "gpt-5.6-sol-high - GPT-5.6 Sol 1M High (default)\n"
            f"{selection} - {current} (current)\n",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(listed_reported_model(raw, selection), current)

        for raw in (
            f"{selection}\n",
            f"prefix-{selection} - {current}\n",
            f"{selection} - Opus 5 1M Medium Thinking\n",
            f"{selection} - Claude Opus 6 1M Medium Thinking\n",
            f"{selection} - {current}\n{selection} - {current}\n",
        ):
            with self.subTest(raw=raw):
                reported = listed_reported_model(raw, selection)
                self.assertNotIn(
                    reported,
                    approved_reported_models(selection, current),
                )

    def test_structured_events_create_sequenced_progress_evidence(self) -> None:
        result = self.run_stream(
            [
                {"type": "system", "subtype": "init"},
                {"type": "assistant", "message": {"content": "working"}},
                {"type": "tool_call", "subtype": "started"},
                {"type": "tool_call", "subtype": "completed"},
                {"type": "result", "subtype": "success"},
            ],
            0,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.progress.splitlines()]
        self.assertEqual([item["sequence"] for item in records], [1, 2, 3, 4, 5])
        self.assertIn("progress_events=5", result.metrics)
        self.assertRegex(result.metrics, r"progress_sha256=[0-9a-f]{64}")

    def test_development_mode_refuses_second_identical_tool_error(self) -> None:
        error = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "editToolCall": {
                    "result": {
                        "error": {
                            "error": "Expected ',' after property at position 156"
                        }
                    }
                }
            },
        }
        result = self.run_stream(
            [
                {"type": "system", "subtype": "init"},
                error,
                {"type": "retry", "subtype": "starting", "attempt": 1},
                error,
                {"type": "result", "subtype": "success"},
            ],
            2,
        )
        self.assertEqual(result.returncode, 15)
        self.assertIn("identical tool failure limit reached: 2 >= 2", result.stderr)
        self.assertIn("internal_retries=1", result.metrics)
        self.assertIn("repeated_tool_error_count=2", result.metrics)
        self.assertNotIn('"type":"result"', result.stdout)

    def test_legacy_default_can_observe_repeated_error_and_complete(self) -> None:
        error = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "editToolCall": {
                    "result": {"error": {"error": "same deterministic error"}}
                }
            },
        }
        result = self.run_stream(
            [
                error,
                error,
                {"type": "result", "subtype": "success"},
            ],
            0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("repeated_tool_error_count=2", result.metrics)

    def test_different_tool_errors_do_not_trip_fuse(self) -> None:
        def error(message: str) -> dict:
            return {
                "type": "tool_call",
                "subtype": "completed",
                "tool_call": {
                    "editToolCall": {"result": {"error": {"error": message}}}
                },
            }

        result = self.run_stream(
            [
                error("first"),
                error("second"),
                {"type": "result", "subtype": "success"},
            ],
            2,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("repeated_tool_error_count=1", result.metrics)

    def test_adapter_terminates_repeated_error_producer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "agent"
            pid_file = root / "agent.pid"
            fake.write_text(
                """#!/usr/bin/env bash
case "${1:-}" in
  --version) printf '2026.07.test\\n'; exit ;;
  models) printf 'claude-sonnet-5-thinking-high - Claude Sonnet 5 1M Thinking\\n'; exit ;;
esac
printf '%s\\n' "$$" >"$FAKE_CURSOR_PID_FILE"
printf '%s\\n' '{"type":"system","subtype":"init"}'
printf '%s\\n' '{"type":"tool_call","subtype":"completed","tool_call":{"editToolCall":{"result":{"error":{"error":"same deterministic edit error"}}}}}'
printf '%s\\n' '{"type":"retry","subtype":"starting","attempt":1}'
printf '%s\\n' '{"type":"tool_call","subtype":"completed","tool_call":{"editToolCall":{"result":{"error":{"error":"same deterministic edit error"}}}}}'
exec sleep 20
"""
            )
            fake.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "CURSOR_AGENT_BIN": str(fake),
                    "CURSOR_AGENT_VERSION": "2026.07.test",
                    "FACTORY_CURSOR_SESSION_HOME": str(root),
                    "FACTORY_CURSOR_REPEATED_TOOL_ERROR_LIMIT": "2",
                    "FAKE_CURSOR_PID_FILE": str(pid_file),
                    "TMPDIR": str(root),
                }
            )
            started = time.monotonic()
            result = subprocess.run(
                [
                    str(ADAPTER),
                    "--budget",
                    "1",
                    "--max-turns",
                    "4",
                    "--timeout-min",
                    "1",
                    "--prompt-file",
                    "/dev/null",
                    "--workdir",
                    str(root),
                    "--model",
                    "claude-sonnet-5-thinking-high",
                    "--effort",
                    "medium",
                    "--",
                    "test",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 15, result.stderr)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("repeated identical tool failure", result.stderr)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()

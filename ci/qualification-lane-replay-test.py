#!/usr/bin/env python3
"""Fast, credential-free replay of the qualification failure boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_ENV = re.compile(
    r"key|token|secret|password|url|dsn|conn|auth|credential|proxy",
    re.IGNORECASE,
)
SCENARIOS = (
    (
        "terminal_adoption_and_partial_migration_are_once_only",
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_successor_route_migration_is_planned_applied_and_reconciled",
        ),
        (
            "qualification-reducer-test.py",
            "QualificationReducerTest.test_successor_adopts_source_terminal_once_without_publication_replay",
        ),
    ),
    (
        "publication_refresh_and_closeout_survive_restart",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_bundle_refresh_receipt_handoff_survives_restart",
            "FactoryControllerTest.test_pushed_publication_refresh_recovers_without_another_provider",
            "FactoryControllerTest.test_historical_publication_refresh_is_not_replayed",
            "FactoryControllerTest.test_merged_closeout_attestation_completes_before_dependency_refresh",
            "FactoryControllerTest.test_closeout_records_exact_terminal_evidence_once",
        ),
    ),
    (
        "provider_fallback_preserves_handoff",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_successor_readmits_prior_provider_failure",
        ),
        (
            "model-fallback-test.py",
            "FallbackTest.test_qualification_recovers_authenticated_cross_release_output",
        ),
        (
            "failed-attempt-handoff-test.py",
            "HandoffTest.test_committed_narrator_handoff_accepts_only_current_ticket_png_evidence",
        ),
    ),
    (
        "three_ticket_cohort_reduces_green",
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_restart_boundary_then_reduction_is_one_command",
        ),
        (
            "qualification-reducer-test.py",
            "QualificationReducerTest.test_three_ticket_successor_accepts_authenticated_history_and_cap",
        ),
    ),
    (
        "incomplete_three_ticket_cohort_stays_waiting",
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_authenticated_wait_is_not_retried_or_reduced",
            "QualificationRunTest.test_empty_controller_result_is_not_reduced",
        ),
    ),
    (
        "operator_ready_materialization_failure_replays",
        (
            "qualification-environment-test.py",
            "QualificationEnvironmentTest.test_unconsumed_ready_receipt_reaches_materialization_replay",
            "QualificationEnvironmentTest.test_selected_operator_retries_pending_ready_projection",
        ),
    ),
    (
        "missing_codex_companion_blocks_before_controller_mutation",
        (
            "provider-cli-pin-test.py",
            "ProviderCliPinTest.test_codex_companion_is_required_and_receipt_bound",
        ),
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_provider_pin_not_ready_blocks_before_controller_mutation",
        ),
    ),
    (
        "malformed_restart_and_reducer_evidence_fail_closed",
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_malformed_result_and_repeated_restart_fail_closed",
            "QualificationRunTest.test_changed_reducer_digest_fails_closed",
        ),
    ),
    (
        "unbound_fallback_and_handoff_fail_closed",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_bundle_refresh_receipt_handoff_refuses_unbound_evidence",
            "FactoryControllerTest.test_prior_role_failure_accepts_only_exact_route_migration_suffix",
        ),
        (
            "model-fallback-test.py",
            "FallbackTest.test_sealed_successor_refuses_unbound_source_factory",
        ),
    ),
    (
        "unsafe_closeout_never_completes",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_closeout_refuses_merged_without_terminal_evidence",
            "FactoryControllerTest.test_closeout_dirty_retry_remains_fail_closed",
            "FactoryControllerTest.test_closeout_waits_for_post_merge_check_propagation",
            "FactoryControllerTest.test_closeout_defers_while_sibling_claim_is_active",
            "FactoryControllerTest.test_closeout_defers_behind_unmerged_sibling_closeout",
        ),
    ),
)
TIME_LIMIT_SECONDS = 120


def repository_status() -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout


def main() -> int:
    before = repository_status()
    started = time.monotonic()
    local_commands = {
        command: shutil.which(command)
        for command in ("git", "node", "npm", "npx", "python3")
    }
    if any(path is None for path in local_commands.values()):
        raise SystemExit(
            "qualification replay requires git, node, npm, npx, and python3"
        )

    with tempfile.TemporaryDirectory(prefix="qualification-lane-replay.") as raw:
        sandbox = Path(raw)
        binary = sandbox / "bin"
        home = sandbox / "home"
        binary.mkdir()
        home.mkdir()
        for command, path in local_commands.items():
            (binary / command).symlink_to(path)
        calls = sandbox / "external-calls"
        for command in ("claude", "codex", "cursor", "curl", "gh", "scp", "ssh", "wget"):
            path = binary / command
            path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$0\" >> \"$FACTORY_REPLAY_EXTERNAL_CALLS\"\nexit 97\n",
                encoding="utf-8",
            )
            path.chmod(0o700)

        environment = {
            key: value for key, value in os.environ.items()
            if not SENSITIVE_ENV.search(key)
        }
        environment.update({
            "FACTORY_REPLAY_EXTERNAL_CALLS": str(calls),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "HOME": str(home),
            "PATH": str(binary),
            "TMPDIR": str(sandbox),
            "XDG_CONFIG_HOME": str(home / ".config"),
        })

        for name, *commands in SCENARIOS:
            scenario_started = time.monotonic()
            for test_file, *tests in commands:
                remaining = TIME_LIMIT_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    print("FAIL: replay exceeded 120 seconds", file=sys.stderr)
                    return 1
                try:
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "ci" / test_file), *tests],
                        capture_output=True,
                        env=environment,
                        text=True,
                        timeout=remaining,
                    )
                except subprocess.TimeoutExpired:
                    print("FAIL: replay exceeded 120 seconds", file=sys.stderr)
                    return 1
                if result.returncode:
                    sys.stderr.write(result.stdout)
                    sys.stderr.write(result.stderr)
                    print(f"FAIL: {name}", file=sys.stderr)
                    return 1
            print(f"PASS: {name} ({time.monotonic() - scenario_started:.1f}s)")

        if calls.exists():
            print("FAIL: replay attempted an external command", file=sys.stderr)
            return 1
        if repository_status() != before:
            print("FAIL: replay changed the repository", file=sys.stderr)
            return 1

    elapsed = time.monotonic() - started
    if elapsed > TIME_LIMIT_SECONDS:
        print(f"FAIL: replay exceeded 120 seconds ({elapsed:.1f}s)", file=sys.stderr)
        return 1
    print(f"PASS: qualification lane replay ({elapsed:.1f}s, external_calls=0, residual_state=0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

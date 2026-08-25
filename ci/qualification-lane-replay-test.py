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
HISTORICAL_BAD_SHA = "3968fba242c1943f0eda449ce108902c8aa8d176"
HISTORICAL_FIXED_SHA = "441873ddfe7b44f1713bb127e380437aa87b04e9"
HISTORICAL_LIBRARIES = (
    "failed_attempt_handoff.py",
    "narrator_evidence.py",
    "release_lineage.py",
)
HISTORICAL_PROBE = r"""
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from failed_attempt_handoff import HandoffError, RoleBoundaryPolicy, preview_handoff

repo = Path(sys.argv[2])
expected = sys.argv[3]
head = sys.argv[4]
policy = RoleBoundaryPolicy.from_dict({
    "schema": "nysa.software-factory.handoff-boundary/v1",
    "roles": {"builder": ["src/**", ".gitattributes"]},
    "protected_paths": [
        ".git", ".git/**", "factory/tickets/**", "factory/attestations/**",
    ],
    "journal_path": "factory/model-route-journal.json",
    "max_file_bytes": 128,
    "provider_identities": ["provider@example.test"],
})
try:
    preview = preview_handoff(
        repo, role="builder", policy=policy, expected_head=head,
        expected_branch="main", remote="origin", remote_branch="main",
        expected_remote_head=head, provider_scan_base=head,
    )
except HandoffError as error:
    if expected == "reject" and str(error) == "symlinks are forbidden: .claude/skills":
        raise SystemExit(0)
    raise
if expected != "accept" or [entry.path for entry in preview.entries] != ["src/kept.txt"]:
    raise SystemExit("historical tracked-symlink result changed")
"""
SENSITIVE_ENV = re.compile(
    r"key|token|secret|password|url|dsn|conn|auth|credential|proxy",
    re.IGNORECASE,
)
SCENARIOS = (
    (
        "factory_sha_and_successor_authority_are_exact",
        (
            "qualification-manifest-test.py",
            "QualificationManifestTest.test_exact_ordinary_and_successor_manifests_pass",
            "QualificationManifestTest.test_duplicate_raw_fields_refuse_before_last_value_wins",
            "QualificationManifestTest.test_malformed_ticket_variants_refuse_deterministically",
        ),
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_passportless_kit_refusal_recovers_after_exact_route_migration",
            "FactoryControllerTest.test_passportless_kit_refusal_rejects_intermediate_kit_mismatch",
            "FactoryControllerTest.test_factory_upgrade_authenticates_passport_before_route_migration",
        ),
    ),
    (
        "kit_pin_migration_reaches_every_trusted_consumer",
        (
            "ticket-passport-test.py",
            "TicketPassportTest.test_terminal_export_accepts_exact_authenticated_release_migration",
            "TicketPassportTest.test_terminal_export_accepts_only_a_contiguous_migration_suffix",
        ),
        (
            "ticket-pr-test.py",
            "TicketPrTest.test_publication_accepts_approval_then_successor_route_migration",
            "TicketPrTest.test_publication_rejects_wrong_pin_after_successor_migration",
        ),
    ),
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
        (
            "ticket-attest-test.py",
            "TicketAttestTests.test_bundle_accepts_only_exact_post_review_migration_kit_pin",
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
        "internet_loss_waits_and_replays_exact_outputs",
        (
            "ticket-pr-test.py",
            "TicketPrTest.test_git_ls_remote_github_outage_waits",
        ),
        (
            "ticket-attest-test.py",
            "TicketAttestTests.test_bundle_network_outage_waits_without_mutation",
            "TicketAttestTests.test_bundle_recovers_confirmed_push_after_tracking_update_loss",
        ),
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_external_wait_is_typed_and_does_not_latch_qualification",
            "FactoryControllerTest.test_exact_push_accepts_lost_response_and_waits_on_outage",
            "FactoryControllerTest.test_pushed_prepublication_attestations_recover_without_role_replay",
        ),
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_controller_network_wait_stops_before_reduction",
            "QualificationRunTest.test_reducer_network_wait_preserves_completed_controller_evidence",
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
            "FallbackTest.test_qualification_apply_preserves_committed_spec_lint_verdict",
        ),
        (
            "failed-attempt-handoff-test.py",
            "HandoffTest.test_spec_lint_fallback_preserves_one_legal_verdict_append",
            "HandoffTest.test_committed_narrator_handoff_accepts_only_current_ticket_png_evidence",
            "HandoffTest.test_committed_role_validation_allows_only_unchanged_symlinks",
            "HandoffTest.test_preview_and_replay_preserve_only_exact_tracked_symlinks",
            "HandoffTest.test_replay_refuses_deleting_a_tracked_symlink",
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
            "QualificationEnvironmentTest.test_prepare_restarts_after_ready_branch_and_protected_main_advance",
            "QualificationEnvironmentTest.test_unconsumed_ready_receipt_reaches_materialization_replay",
            "QualificationEnvironmentTest.test_selected_operator_retries_pending_ready_projection",
        ),
    ),
    (
        "fresh_qualification_ownership_conflicts_fail_before_provider",
        (
            "qualification-environment-test.py",
            "QualificationEnvironmentTest.test_state_changing_clis_reject_malformed_tickets_before_state",
            "QualificationEnvironmentTest.test_doctor_classifies_authenticated_artifact_tamper_read_only",
            "QualificationEnvironmentTest.test_selected_ticket_authoring_fields_fail_before_lane_creation",
            "QualificationEnvironmentTest.test_rejects_protected_ready_builder_ownership_conflicts",
            "QualificationEnvironmentTest.test_rejects_selected_protected_source_hash_before_lane_creation",
            "QualificationEnvironmentTest.test_takeover_reuses_authenticated_live_state_without_copying_it",
        ),
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_doctor_error_returns_exact_report_before_controller_mutation",
            "QualificationRunTest.test_ticket_readiness_blocks_before_controller_mutation",
        ),
    ),
    (
        "authorization_and_apply_tamper_fail_before_mutation",
        (
            "dispatch-plan-test.py",
            "DispatchPlanTest.test_claim_rechecks_presealed_ticket_blob_before_worktree",
        ),
        (
            "state-machine-test.py",
            "StateMachineTest.test_expected_head_refuses_state_machine_snapshot_drift",
        ),
        (
            "release-transaction-test.py",
            "ReleaseTransactionTest.test_composite_approval_rejects_every_bound_tamper",
            "ReleaseTransactionTest.test_resume_live_basis_refuses_product_and_runtime_drift",
        ),
        (
            "certification-preflight-test.py",
            "CertificationPreflightTest.test_valid_plan_mutation_after_tuple_receipt_fails_before_phase",
        ),
        (
            "envelope-control-test.py",
            "EnvelopeControlTest.test_stale_preview_and_symlink_are_rejected",
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
        "provider_cli_pins_and_reported_models_are_exact",
        (
            "provider-cli-pin-test.py",
            "ProviderCliPinTest.test_healthy_links_exact_receipt_and_idempotent_check",
            "ProviderCliPinTest.test_benign_stderr_warning_does_not_break_the_version_probe",
            "ProviderCliPinTest.test_sensitive_stderr_still_refuses_the_version_probe",
            "ProviderCliPinTest.test_receipt_authority_checks_a_distinct_allowed_release",
        ),
        (
            "qualification-environment-test.py",
            "QualificationEnvironmentTest.test_provider_cli_pin_gate_rejects_ambiguous_or_stale_evidence",
        ),
        (
            "model-router-test.py",
            "ModelRouterTest.test_invalid_and_unknown_hard_stop_without_fallback",
            "ModelRouterTest.test_cursor_reported_identity_mismatch_is_invalid",
            "ModelRouterTest.test_historical_catalog_is_accepted_only_for_compatible_migration",
        ),
    ),
    (
        "fallback_history_generation_and_accounting_are_exact",
        (
            "model-fallback-test.py",
            "FallbackTest.test_qualification_fallback_is_scoped_to_failure_generation",
            "FallbackTest.test_fallback_reduces_authoritative_accounting_not_runtime_view",
            "FallbackTest.test_builder_handoff_accepts_only_its_own_ticket_log",
        ),
        (
            "model-router-test.py",
            "ModelRouterTest.test_history_aware_fallback_excludes_failed_route_and_resolves_future_roles",
            "ModelRouterTest.test_fallback_advances_only_unavailable_and_hard_stops_bad_evidence",
        ),
    ),
    (
        "provider_budget_overflow_fails_closed",
        (
            "provider-production-concurrency-test.py",
            "ProductionConcurrencyTest.test_codex_adapter_uses_the_attempt_local_home",
        ),
    ),
    (
        "provider_cost_precision_stays_authenticatable",
        (
            "ticket-passport-test.py",
            "TicketPassportTest.test_provider_cost_precision_rounds_up_to_micro_usd",
        ),
    ),
    (
        "attempt_charges_and_cancellation_replay_exactly_once",
        (
            "ticket-passport-test.py",
            "TicketPassportTest.test_passport_chains_receipts_without_replay_or_double_charge",
        ),
        (
            "attempt-cancel-test.py",
            "AttemptCancellationTest.test_pre_go_cancel_is_zero_cost_and_replay_safe",
        ),
    ),
    (
        "provider_reservations_overlap_and_drain_independently",
        (
            "provider-production-concurrency-test.py",
            "ProductionConcurrencyTest.test_configuration_lock_serializes_apply_and_reservation",
            "ProductionConcurrencyTest.test_three_distinct_cli_routes_overlap_then_drain_independently",
        ),
    ),
    (
        "cancellation_races_fail_closed_and_replay_once",
        (
            "attempt-cancel-test.py",
            "AttemptCancellationTest.test_term_then_kill_escalation_revalidates_members",
            "AttemptCancellationTest.test_competing_request_is_not_treated_as_replay",
            "AttemptCancellationTest.test_stale_process_converges_without_signalling_or_replay",
        ),
    ),
    (
        "provider_spend_limit_is_typed_and_latched",
        (
            "ticket-passport-test.py",
            "TicketPassportTest.test_claude_spend_limit_reason_is_strict",
        ),
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_qualification_spend_limit_falls_back_without_latching",
        ),
    ),
    (
        "qualification_fail_fast_stops_sibling_launches",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_qualification_controller_error_stops_sibling_next_role_launches",
            "FactoryControllerTest.test_qualification_worker_exception_latches_before_sibling_next_role",
            "FactoryControllerTest.test_qualification_latch_blocks_role_at_atomic_launch_gate",
            "FactoryControllerTest.test_qualification_protected_mutation_latches_before_sibling_launch",
            "FactoryControllerTest.test_qualification_latch_accounts_existing_terminal_before_stopping",
        ),
    ),
    (
        "model_inventory_and_identity_recovery_are_exact",
        (
            "cursor-stream-test.py",
            "CursorStreamTest.test_cursor_inventory_identity_is_exact_and_unambiguous",
            "CursorStreamTest.test_enabled_cursor_runtime_names_remain_route_bound",
        ),
        (
            "model-router-test.py",
            "ModelRouterTest.test_catalog_has_exact_current_routes_and_disabled_experimental_kimi",
            "ModelRouterTest.test_cursor_opus_default_uses_native_sonnet_fallback",
        ),
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_model_identity_success_recovers_before_provider_fallback",
            "FactoryControllerTest.test_first_model_identity_success_observation_never_replays_provider",
        ),
        (
            "ticket-passport-test.py",
            "TicketPassportTest.test_reverted_model_identity_success_is_restored_without_replay",
        ),
    ),
    (
        "qualification_finish_operator_boundaries_fail_closed",
        (
            "qualification-run-test.py",
            "QualificationRunTest.test_finish_projects_exact_approval_and_continues_to_green",
            "QualificationRunTest.test_finish_refuses_dirty_approval_claim",
            "QualificationRunTest.test_finish_refuses_foreign_operator_authority",
            "QualificationRunTest.test_finish_refuses_concurrent_controller",
        ),
    ),
    (
        "terminal_restart_and_publication_evidence_recover_once",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_qualification_missing_terminal_latches_after_process",
            "FactoryControllerTest.test_delayed_terminal_is_finished_without_rerunning_role",
            "FactoryControllerTest.test_qualification_empty_restart_recovers_protected_targets",
            "FactoryControllerTest.test_qualification_restart_surfaces_durable_blocked_claims",
            "FactoryControllerTest.test_publication_events_follow_serialized_lease_order",
            "FactoryControllerTest.test_publication_acquisition_event_recovers_before_claim_save",
        ),
    ),
    (
        "durable_events_and_scheduler_restarts_are_once_only",
        (
            "factory-controller-test.py",
            "FactoryControllerTest.test_terminal_event_is_idempotent_across_restart",
            "FactoryControllerTest.test_concurrent_event_publication_is_monotonic_across_restart",
            "FactoryControllerTest.test_operator_events_backfill_each_durable_crash_boundary_once",
            "FactoryControllerTest.test_scheduler_tracks_each_concurrent_ticket_once",
            "FactoryControllerTest.test_restart_does_not_resubmit_externally_active_role",
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


def historical_tracked_symlink_sensitivity(
    sandbox: Path, environment: dict[str, str], timeout: float,
) -> None:
    """Compare only the unchanged tracked-symlink handoff invariant."""
    fixture = sandbox / "historical-tracked-symlink"
    repo = fixture / "product"
    remote = fixture / "product.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True, env=environment,
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True, env=environment,
    )
    for key, value in (
        ("user.name", "Qualification Replay"),
        ("user.email", "replay@example.invalid"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value],
            check=True, env=environment,
        )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True, env=environment,
    )
    for directory in (repo / "src", repo / "factory", repo / "skills", repo / ".claude"):
        directory.mkdir(parents=True, exist_ok=True)
    (repo / "src/kept.txt").write_text("original\n", encoding="utf-8")
    (repo / "factory/model-route-journal.json").write_text("{}\n", encoding="utf-8")
    (repo / "skills/README.md").write_text("# Skills\n", encoding="utf-8")
    (repo / ".claude/skills").symlink_to("../skills")
    for arguments in (("add", "."), ("commit", "-qm", "tracked symlink baseline")):
        subprocess.run(
            ["git", "-C", str(repo), *arguments], check=True, env=environment,
        )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"],
        check=True, env=environment,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, env=environment, text=True,
    ).stdout.strip()
    (repo / "src/kept.txt").write_text("handoff\n", encoding="utf-8")

    for sha, expected in (
        (HISTORICAL_BAD_SHA, "reject"),
        (HISTORICAL_FIXED_SHA, "accept"),
    ):
        library = fixture / sha / "scripts/lib"
        library.mkdir(parents=True)
        for name in HISTORICAL_LIBRARIES:
            source = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{sha}:scripts/lib/{name}"],
                check=True, capture_output=True, env=environment,
            ).stdout
            (library / name).write_bytes(source)
        subprocess.run(
            [
                sys.executable, "-I", "-S", "-c", HISTORICAL_PROBE,
                str(library), str(repo), expected, head,
            ],
            check=True, env=environment, timeout=timeout,
        )


def main() -> int:
    before = repository_status()
    started = time.monotonic()
    local_commands = {
        command: shutil.which(command)
        for command in (
            "awk", "bash", "cat", "chmod", "dirname", "git", "head", "mkdir",
            "mktemp", "mv", "node", "npm", "npx", "ps", "python3", "rm",
            "sed", "tail", "timeout",
        )
    }
    if any(path is None for path in local_commands.values()):
        raise SystemExit("qualification replay is missing a required local command")

    with tempfile.TemporaryDirectory(
        prefix="qualification-lane-replay.", dir=Path.home()
    ) as raw:
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

        scenario_started = time.monotonic()
        try:
            historical_tracked_symlink_sensitivity(
                sandbox, environment,
                TIME_LIMIT_SECONDS - (time.monotonic() - started),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("FAIL: historical_tracked_symlink_sensitivity", file=sys.stderr)
            return 1
        print(
            "PASS: historical_tracked_symlink_sensitivity "
            f"({time.monotonic() - scenario_started:.1f}s)"
        )

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
    if sandbox.exists():
        print("FAIL: replay left its disposable sandbox", file=sys.stderr)
        return 1
    if elapsed > TIME_LIMIT_SECONDS:
        print(f"FAIL: replay exceeded 120 seconds ({elapsed:.1f}s)", file=sys.stderr)
        return 1
    print(
        f"PASS: qualification lane replay "
        f"({elapsed:.1f}s, blocked_external_cli_calls=0, "
        "repository_changed=0, sandbox_residue=0)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

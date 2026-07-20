#!/usr/bin/env bash
# Canonical suite registry. The caller supplies a callback receiving ID, label, and command.

suite_registry() {
  local callback="$1"
  "$callback" linear "Linear reconciler regression suite" python3 "$ROOT/ci/linear-sync-test.py"
  "$callback" effective-ticket "effective ticket overlay suite" python3 "$ROOT/ci/effective-ticket-test.py"
  "$callback" ledger "runtime ledger regression suite" python3 "$ROOT/ci/ledger-view-test.py"
  "$callback" attempt-cancel "targeted attempt cancellation suite" python3 "$ROOT/ci/attempt-cancel-test.py"
  "$callback" provider-executor "isolated provider executor suite" python3 "$ROOT/ci/provider-executor-test.py"
  "$callback" operator-console "operator console security suite" python3 "$ROOT/ci/operator-console-test.py"
  "$callback" model-router "model router regression suite" python3 "$ROOT/ci/model-router-test.py"
  "$callback" model-manager "model manager regression suite" python3 "$ROOT/ci/model-manager-test.py"
  "$callback" model-control "model control regression suite" python3 "$ROOT/ci/model-control-test.py"
  "$callback" envelope-control "envelope control regression suite" python3 "$ROOT/ci/envelope-control-test.py"
  "$callback" claude-kimi "disabled Claude Kimi adapter suite" python3 "$ROOT/ci/claude-kimi-adapter-test.py"
  "$callback" failed-handoff "failed-attempt handoff suite" python3 "$ROOT/ci/failed-attempt-handoff-test.py"
  "$callback" fallback-approval "model fallback approval suite" python3 "$ROOT/ci/model-fallback-approval-test.py"
  "$callback" model-fallback "model fallback transaction suite" python3 "$ROOT/ci/model-fallback-test.py"
  "$callback" ci-scope "selective CI scope" bash "$ROOT/ci/ci-scope-test.sh"
  "$callback" process-group-readiness "process-group readiness suite" python3 "$ROOT/ci/run-in-process-group-test.py"
  "$callback" factory-scripts "factory script regression suite" bash "$ROOT/ci/test-factory-scripts.sh"
  "$callback" dispatch-leases "dispatcher lease suite" bash "$ROOT/ci/dispatch-leases-test.sh"
  "$callback" reorder-test-fixes "reorder test-fixes suite" bash "$ROOT/ci/reorder-test-fixes-test.sh"
  "$callback" preflight "preflight suite" bash "$ROOT/ci/preflight-test.sh"
  "$callback" ticket-state "ticket-state suite" bash "$ROOT/ci/ticket-state-test.sh"
  "$callback" ticket-attest "ticket attestation suite" python3 "$ROOT/ci/ticket-attest-test.py"
  "$callback" legacy-closeout "legacy closeout suite" python3 "$ROOT/ci/legacy-closeout-test.py"
  "$callback" terminal-backfill "terminal backfill suite" python3 "$ROOT/ci/terminal-backfill-test.py"
  "$callback" hermes-contract "Hermes contract suite" bash "$ROOT/ci/hermes-contract-test.sh"
  "$callback" factory-kit "factory kit release suite" bash "$ROOT/ci/factory-kit-test.sh"
  "$callback" conformance "conformance app suite" run_conformance
  "$callback" immutability "test immutability suite" run_immutability
  "$callback" artifact-policy "artifact policy self-test" "$ROOT/scripts/artifact-check" --self-test
}

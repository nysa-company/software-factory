#!/usr/bin/env bash
# Stable shard and parallel-group assignment for the canonical suite registry.

SUITE_GROUP_COUNT=4

suite_shard_for() {
  case "$1" in
    factory-scripts|provider-executor|provider-activation|provider-artifact-controller)
      printf 'factory\n'
      ;;
    emergency-admit|hermes-contract|preflight|ticket-attest|provider-cli-runtime|provider-coordinator|provider-credential-broker|provider-recovery)
      printf 'hermes\n'
      ;;
    *)
      printf 'release\n'
      ;;
  esac
}

# Group membership is balance, not meaning: each group carries one slow anchor
# suite plus filler, sized from observed hosted CI timings. Rebalance from the
# per-suite durations in recent protected-main runs when a group drifts.
suite_group_for() {
  case "$1" in
    factory-scripts|model-fallback|qualification-environment|protected-merge-reconciliation|terminal-backfill|state-machine)
      printf '1\n'
      ;;
    emergency-admit|hermes-contract|preflight|provider-cli-runtime|provider-coordinator|provider-credential-broker|provider-recovery|provider-executor|factory-dev-lane)
      printf '2\n'
      ;;
    factory-kit|ticket-pr|provider-activation|model-control)
      printf '3\n'
      ;;
    *)
      printf '4\n'
      ;;
  esac
}

suite_group_tmp() {
  printf '%s/group-%s\n' "$1" "$2"
}

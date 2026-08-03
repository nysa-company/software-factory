#!/usr/bin/env bash
# Stable shard and parallel-group assignment for the canonical suite registry.

SUITE_GROUP_COUNT=4

suite_shard_for() {
  case "$1" in
    factory-scripts|provider-executor|provider-activation|provider-artifact-controller)
      printf 'factory\n'
      ;;
    hermes-contract|preflight|ticket-attest|provider-coordinator|provider-credential-broker|provider-recovery)
      printf 'hermes\n'
      ;;
    *)
      printf 'release\n'
      ;;
  esac
}

suite_group_for() {
  case "$1" in
    factory-scripts|model-fallback)
      printf '1\n'
      ;;
    hermes-contract|preflight|ticket-attest|provider-coordinator|provider-credential-broker|provider-recovery|provider-executor)
      printf '2\n'
      ;;
    factory-kit|ticket-pr|terminal-backfill|protected-merge-reconciliation|provider-activation)
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

#!/usr/bin/env bash
# Parse factory configuration as data. Configuration files are never shell.

FACTORY_ENVELOPE_CONFIG_KEYS="PER_RUN_BUDGET_USD PER_TICKET_BUDGET_USD PER_RUN_MAX_TURNS PER_RUN_TIMEOUT_MIN DAILY_CAP_USD"
FACTORY_GLOBAL_CONFIG_KEYS="GLOBAL_DAILY_CAP_USD GLOBAL_LEDGER CLAUDE_CODE_PINNED CODEX_PINNED CODEX_USD_PER_MTOK_IN CODEX_USD_PER_MTOK_OUT FACTORY_CURSOR_FALLBACK_ENABLED CURSOR_AGENT_BIN AGENT_CLI_CREDENTIAL_STORE CURSOR_AGENT_VERSION CURSOR_OPENAI_MODEL CURSOR_ANTHROPIC_MODEL CURSOR_PRICING_SNAPSHOT_DATE CURSOR_OPENAI_USD_PER_MTOK_IN CURSOR_OPENAI_USD_PER_MTOK_OUT CURSOR_OPENAI_USD_PER_MTOK_CACHE CURSOR_ANTHROPIC_USD_PER_MTOK_IN CURSOR_ANTHROPIC_USD_PER_MTOK_OUT CURSOR_ANTHROPIC_USD_PER_MTOK_CACHE FACTORY_PROBE_CODEX FACTORY_PROBE_CLAUDE_CODE FACTORY_PROBE_CURSOR_OPENAI FACTORY_PROBE_CURSOR_ANTHROPIC FACTORY_PROBE_TIMEOUT_SEC FACTORY_OVERRIDE_MODEL"

factory_config_positive_decimal() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] &&
    awk -v value="$1" 'BEGIN { exit !(value > 0) }'
}

factory_validate_envelope_config() {
  factory_config_positive_decimal "$PER_RUN_BUDGET_USD" &&
    factory_config_positive_decimal "$PER_TICKET_BUDGET_USD" &&
    factory_config_positive_decimal "$DAILY_CAP_USD" || {
      echo "envelope config money values must be positive finite decimals" >&2
      return 1
    }
  [[ "$PER_RUN_MAX_TURNS" =~ ^[0-9]+$ ]] &&
    awk -v value="$PER_RUN_MAX_TURNS" 'BEGIN { exit !(value > 0) }' &&
    [[ "$PER_RUN_TIMEOUT_MIN" =~ ^[0-9]+$ ]] &&
    awk -v value="$PER_RUN_TIMEOUT_MIN" 'BEGIN { exit !(value > 0) }' || {
      echo "envelope config turns and timeout must be positive integers" >&2
      return 1
    }
  awk -v run="$PER_RUN_BUDGET_USD" -v ticket="$PER_TICKET_BUDGET_USD" \
    -v daily="$DAILY_CAP_USD" \
    'BEGIN { exit !((run <= ticket) && (run <= daily)) }' || {
      echo "envelope config per-run budget exceeds a ticket or daily cap" >&2
      return 1
    }
}

factory_load_plain_config() {
  local path="$1" kind="$2" allowed="$3" required="$4" export_values="${5:-0}"
  local raw line key value seen=" " required_key
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    [[ "$raw" != *$'\r'* && "$raw" != *$'\n'* && "$raw" != *$'\t'* ]] || {
      echo "$kind config contains control characters" >&2
      return 1
    }
    line="$raw"
    [[ -n "$line" && "$line" != \#* ]] || continue
    case "$line" in export\ *) line="${line#export }" ;; esac
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || {
      echo "$kind config must contain plain KEY=value lines" >&2
      return 1
    }
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
      *\"*|*\'*)
        echo "$kind config contains malformed quoting" >&2
        return 1
        ;;
    esac
    [[ "$value" =~ ^[A-Za-z0-9._:/+@%~-]*$ ]] || {
      echo "$kind config contains an unsafe value" >&2
      return 1
    }
    case "$seen" in
      *" $key "*) echo "$kind config repeats $key" >&2; return 1 ;;
    esac
    case " $allowed " in
      *" $key "*) ;;
      *) echo "$kind config contains unsupported key $key" >&2; return 1 ;;
    esac
    seen="$seen$key "
    if [[ "$export_values" == "1" ]]; then
      export "$key=$value"
    else
      printf -v "$key" '%s' "$value"
    fi
  done < "$path"
  for required_key in $required; do
    case "$seen" in
      *" $required_key "*) ;;
      *) echo "$kind config is missing $required_key" >&2; return 1 ;;
    esac
  done
  if [[ "$kind" == "envelope" ]]; then
    factory_validate_envelope_config || return 1
  fi
  case "$seen" in
    *" GLOBAL_DAILY_CAP_USD "*)
      factory_config_positive_decimal "$GLOBAL_DAILY_CAP_USD" || {
        echo "global config daily cap must be a positive finite decimal" >&2
        return 1
      }
      ;;
  esac
  case "$seen" in
    *" GLOBAL_LEDGER "*)
      case "$GLOBAL_LEDGER" in
        /*) ;;
        *) echo "global config ledger path must be absolute" >&2; return 1 ;;
      esac
      ;;
  esac
}

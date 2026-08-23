#!/usr/bin/env bash
# Parse factory configuration as data. Configuration files are never shell.

FACTORY_ENVELOPE_REQUIRED_KEYS="PER_RUN_BUDGET_USD PER_TICKET_BUDGET_USD PER_RUN_MAX_TURNS PER_RUN_TIMEOUT_MIN DAILY_CAP_USD"
FACTORY_ENVELOPE_ROLE_KEYS="PLANNER_PER_RUN_BUDGET_USD PLANNER_PER_RUN_MAX_TURNS PLANNER_PER_RUN_TIMEOUT_MIN NARRATOR_PER_RUN_BUDGET_USD NARRATOR_PER_RUN_MAX_TURNS NARRATOR_PER_RUN_TIMEOUT_MIN BUILDER_PER_RUN_BUDGET_USD BUILDER_PER_RUN_MAX_TURNS BUILDER_PER_RUN_TIMEOUT_MIN SPEC_LINTER_PER_RUN_BUDGET_USD SPEC_LINTER_PER_RUN_MAX_TURNS SPEC_LINTER_PER_RUN_TIMEOUT_MIN TEST_AUTHOR_PER_RUN_BUDGET_USD TEST_AUTHOR_PER_RUN_MAX_TURNS TEST_AUTHOR_PER_RUN_TIMEOUT_MIN REVIEWER_PER_RUN_BUDGET_USD REVIEWER_PER_RUN_MAX_TURNS REVIEWER_PER_RUN_TIMEOUT_MIN"
FACTORY_ENVELOPE_CONFIG_KEYS="$FACTORY_ENVELOPE_REQUIRED_KEYS $FACTORY_ENVELOPE_ROLE_KEYS"
FACTORY_GLOBAL_CONFIG_KEYS="GLOBAL_DAILY_CAP_USD GLOBAL_LEDGER CLAUDE_CODE_PINNED CODEX_PINNED CODEX_USD_PER_MTOK_IN CODEX_USD_PER_MTOK_OUT CODEX_USD_PER_MTOK_CACHE FACTORY_CURSOR_FALLBACK_ENABLED CURSOR_AGENT_BIN AGENT_CLI_CREDENTIAL_STORE CURSOR_AGENT_VERSION CURSOR_OPENAI_MODEL CURSOR_ANTHROPIC_MODEL CURSOR_PRICING_SNAPSHOT_DATE CURSOR_OPENAI_USD_PER_MTOK_IN CURSOR_OPENAI_USD_PER_MTOK_OUT CURSOR_OPENAI_USD_PER_MTOK_CACHE CURSOR_ANTHROPIC_USD_PER_MTOK_IN CURSOR_ANTHROPIC_USD_PER_MTOK_OUT CURSOR_ANTHROPIC_USD_PER_MTOK_CACHE FACTORY_PROBE_CODEX FACTORY_PROBE_CLAUDE_CODE FACTORY_PROBE_CURSOR_OPENAI FACTORY_PROBE_CURSOR_ANTHROPIC FACTORY_PROBE_TIMEOUT_SEC FACTORY_OVERRIDE_MODEL"
FACTORY_CONFIG_MAX_MONEY_USD=1000000
FACTORY_CONFIG_MAX_TURNS=1000
FACTORY_CONFIG_MAX_TIMEOUT_MIN=1440

factory_config_positive_decimal() {
  [[ "$1" =~ ^[0-9]{1,7}([.][0-9]{1,6})?$ ]] &&
    awk -v value="$1" -v maximum="$FACTORY_CONFIG_MAX_MONEY_USD" \
      'BEGIN { exit !(value > 0 && value <= maximum) }'
}

factory_config_nonnegative_decimal() {
  [[ "$1" =~ ^[0-9]{1,7}([.][0-9]{1,6})?$ ]] &&
    awk -v value="$1" -v maximum="$FACTORY_CONFIG_MAX_MONEY_USD" \
      'BEGIN { exit !(value >= 0 && value <= maximum) }'
}

factory_validate_pricing_config() {
  local seen="$1" key
  case "$seen" in
    *" CODEX_USD_PER_MTOK_IN "*|*" CODEX_USD_PER_MTOK_OUT "*|*" CODEX_USD_PER_MTOK_CACHE "*)
      factory_config_positive_decimal "${CODEX_USD_PER_MTOK_IN:-}" &&
        factory_config_positive_decimal "${CODEX_USD_PER_MTOK_OUT:-}" &&
        factory_config_positive_decimal "${CODEX_USD_PER_MTOK_CACHE:-}" || {
          echo "global config Codex pricing requires bounded positive in/out/cache rates" >&2
          return 1
        }
      ;;
  esac
  case "$seen" in
    *" CURSOR_PRICING_SNAPSHOT_DATE "*|*" CURSOR_OPENAI_USD_PER_MTOK_"*|*" CURSOR_ANTHROPIC_USD_PER_MTOK_"*)
      [[ "${CURSOR_PRICING_SNAPSHOT_DATE:-}" =~ ^20[0-9]{2}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$ ]] &&
        factory_config_positive_decimal "${CURSOR_OPENAI_USD_PER_MTOK_IN:-}" &&
        factory_config_positive_decimal "${CURSOR_OPENAI_USD_PER_MTOK_OUT:-}" &&
        factory_config_positive_decimal "${CURSOR_ANTHROPIC_USD_PER_MTOK_IN:-}" &&
        factory_config_positive_decimal "${CURSOR_ANTHROPIC_USD_PER_MTOK_OUT:-}" || {
          echo "global config Cursor pricing requires a dated complete bounded positive rate bundle" >&2
          return 1
        }
      for key in CURSOR_OPENAI_USD_PER_MTOK_CACHE CURSOR_ANTHROPIC_USD_PER_MTOK_CACHE; do
        if [[ -n "${!key+x}" ]] && ! factory_config_nonnegative_decimal "${!key}"; then
          echo "global config Cursor cache pricing must be bounded and nonnegative" >&2
          return 1
        fi
      done
      ;;
  esac
}

factory_validate_envelope_config() {
  local role prefix budget_key turns_key timeout_key budget
  factory_config_positive_decimal "$PER_RUN_BUDGET_USD" &&
    factory_config_positive_decimal "$PER_TICKET_BUDGET_USD" &&
    factory_config_positive_decimal "$DAILY_CAP_USD" || {
      echo "envelope config money values must be positive finite decimals" >&2
      return 1
    }
  [[ "$PER_RUN_MAX_TURNS" =~ ^[0-9]{1,4}$ ]] &&
    awk -v value="$PER_RUN_MAX_TURNS" -v maximum="$FACTORY_CONFIG_MAX_TURNS" \
      'BEGIN { exit !(value > 0 && value <= maximum) }' &&
    [[ "$PER_RUN_TIMEOUT_MIN" =~ ^[0-9]{1,4}$ ]] &&
    awk -v value="$PER_RUN_TIMEOUT_MIN" -v maximum="$FACTORY_CONFIG_MAX_TIMEOUT_MIN" \
      'BEGIN { exit !(value > 0 && value <= maximum) }' || {
      echo "envelope config turns and timeout must be positive integers" >&2
      return 1
    }
  awk -v run="$PER_RUN_BUDGET_USD" -v ticket="$PER_TICKET_BUDGET_USD" \
    -v daily="$DAILY_CAP_USD" \
    'BEGIN { exit !((run <= ticket) && (run <= daily)) }' || {
      echo "envelope config per-run budget exceeds a ticket or daily cap" >&2
      return 1
    }
  for role in PLANNER NARRATOR BUILDER SPEC_LINTER TEST_AUTHOR REVIEWER; do
    prefix="${role}_"
    budget_key="${prefix}PER_RUN_BUDGET_USD"
    turns_key="${prefix}PER_RUN_MAX_TURNS"
    timeout_key="${prefix}PER_RUN_TIMEOUT_MIN"
    if [[ -n "${!budget_key+x}" ]]; then
      factory_config_positive_decimal "${!budget_key}" || {
        echo "envelope config $budget_key must be a positive finite decimal" >&2
        return 1
      }
      budget="${!budget_key}"
      awk -v run="$budget" -v ticket="$PER_TICKET_BUDGET_USD" \
        -v daily="$DAILY_CAP_USD" \
        'BEGIN { exit !((run <= ticket) && (run <= daily)) }' || {
          echo "envelope config $budget_key exceeds a ticket or daily cap" >&2
          return 1
        }
    fi
    if [[ -n "${!turns_key+x}" ]]; then
      [[ "${!turns_key}" =~ ^[0-9]{1,4}$ ]] &&
        awk -v value="${!turns_key}" -v maximum="$FACTORY_CONFIG_MAX_TURNS" \
          'BEGIN { exit !(value > 0 && value <= maximum) }' || {
            echo "envelope config $turns_key must be a positive integer" >&2
            return 1
          }
    fi
    if [[ -n "${!timeout_key+x}" ]]; then
      [[ "${!timeout_key}" =~ ^[0-9]{1,4}$ ]] &&
        awk -v value="${!timeout_key}" -v maximum="$FACTORY_CONFIG_MAX_TIMEOUT_MIN" \
          'BEGIN { exit !(value > 0 && value <= maximum) }' || {
            echo "envelope config $timeout_key must be a positive integer" >&2
            return 1
          }
    fi
  done
}

# Resolve a role's exact per-attempt values after the complete envelope has
# passed validation. Missing role keys deliberately inherit the legacy defaults.
factory_select_role_envelope() {
  local role_key budget_key turns_key timeout_key
  role_key="$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
  case "$role_key" in
    PLANNER|NARRATOR|BUILDER|SPEC_LINTER|TEST_AUTHOR|REVIEWER) ;;
    *) echo "envelope role is unsupported: $1" >&2; return 1 ;;
  esac
  budget_key="${role_key}_PER_RUN_BUDGET_USD"
  turns_key="${role_key}_PER_RUN_MAX_TURNS"
  timeout_key="${role_key}_PER_RUN_TIMEOUT_MIN"
  PER_RUN_BUDGET_USD="${!budget_key:-$PER_RUN_BUDGET_USD}"
  PER_RUN_MAX_TURNS="${!turns_key:-$PER_RUN_MAX_TURNS}"
  PER_RUN_TIMEOUT_MIN="${!timeout_key:-$PER_RUN_TIMEOUT_MIN}"
}

factory_clear_plain_config_keys() {
  local key
  for key in $1; do
    unset "$key"
  done
}

factory_load_plain_config() {
  local path="$1" kind="$2" allowed="$3" required="$4" export_values="${5:-0}"
  local raw line key value seen=" " required_key
  [[ "$kind" != "global" ]] || required="GLOBAL_DAILY_CAP_USD $required"
  factory_clear_plain_config_keys "$allowed"
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
  if [[ "$kind" == "global" ]]; then
    factory_validate_pricing_config "$seen" || return 1
  fi
  case "$seen" in
    *" GLOBAL_LEDGER "*)
      case "$GLOBAL_LEDGER" in
        /*) ;;
        *) echo "global config ledger path must be absolute" >&2; return 1 ;;
      esac
      ;;
  esac
}

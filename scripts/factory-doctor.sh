#!/usr/bin/env bash
# Read-only Hermes/factory compatibility diagnostics.
# Public interface: factory-doctor.sh [--json] [--project <slug>]
set -u

CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-1.8.0}"
DOCTOR_SCHEMA="nysa.software-factory.hermes-doctor/v1"
SUPPORTED_HERMES_AGENT="0.18.2"
SUPPORTED_HERMES_BUILD="2026.7.7.2"

JSON_MODE=0
PROJECT="${FACTORY_PROJECT:-relay}"
PROFILE_DIR="${HERMES_FACTORY_PROFILE:-$HOME/.hermes/profiles/factory}"
REGISTRY="${HERMES_PROJECT_REGISTRY:-}"
LINEAR_FRESH_SECONDS="${FACTORY_LINEAR_FRESH_SECONDS:-600}"
PROBE_TIMEOUT_SECONDS="${FACTORY_DOCTOR_TIMEOUT_SECONDS:-5}"
KIT_DIR_OVERRIDE=""
PRODUCT_ROOT_OVERRIDE=""
KIT_SHA_OVERRIDE=""

usage() {
  echo "usage: factory-doctor.sh [--json] [--project <slug>] [--profile-dir <path>] [--registry <path>] [--kit-dir <path> --product-root <path> --kit-sha <full-sha>]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)
      JSON_MODE=1
      shift
      ;;
    --project)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PROJECT="$2"
      shift 2
      ;;
    --profile-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PROFILE_DIR="$2"
      shift 2
      ;;
    --registry)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REGISTRY="$2"
      shift 2
      ;;
    --kit-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      KIT_DIR_OVERRIDE="$2"
      shift 2
      ;;
    --product-root)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PRODUCT_ROOT_OVERRIDE="$2"
      shift 2
      ;;
    --kit-sha)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      KIT_SHA_OVERRIDE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! "$PROJECT" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "invalid project slug" >&2
  exit 2
fi
if [[ ! "$LINEAR_FRESH_SECONDS" =~ ^[0-9]+$ ]] || [[ ! "$PROBE_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "freshness and timeout values must be non-negative integers" >&2
  exit 2
fi
if [[ -z "$REGISTRY" ]]; then
  REGISTRY="$PROFILE_DIR/projects/$PROJECT.env"
fi

PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ "$JSON_MODE" -eq 1 ]]; then
    printf '%s\n' "{\"schema\":\"nysa.software-factory.hermes-doctor/v1\",\"schema_version\":1,\"contract_version\":\"$CONTRACT_VERSION\",\"overall_status\":\"error\",\"error\":\"python3 unavailable\"}"
  else
    echo "Factory doctor: ERROR"
    echo "python3 unavailable; safe JSON and timeout handling require python3"
  fi
  exit 1
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/factory-doctor.XXXXXX")" || exit 1
cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT HUP INT TERM
CLI_FILE="$TMP/clis.tsv"
RUN_FILE="$TMP/runs.tsv"
LEASE_FILE="$TMP/leases.tsv"
: > "$CLI_FILE"
: > "$RUN_FILE"
: > "$LEASE_FILE"

sanitize() {
  "$PYTHON_BIN" -c '
import re
import sys
s = sys.stdin.read().replace("\x00", "")
s = re.sub(
    r"(?im)(authorization\s*:\s*)(?:bearer|basic|token)?\s*[^\r\n]*",
    lambda match: match.group(1) + "[redacted]",
    s,
)
s = re.sub(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", "[redacted-url]", s)
sensitive = r"[A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)[A-Za-z0-9_.-]*"
quoted = re.compile(
    rf"(?is)(?P<prefix>[\x22\x27]?{sensitive}[\x22\x27]?\s*[:=]\s*)"
    rf"(?P<quote>[\x22\x27])(?:\\.|(?!(?P=quote)).)*(?P=quote)"
)
s = quoted.sub(lambda match: match.group("prefix") + "[redacted]", s)
key_line = re.compile(
    rf"(?i)^(?P<prefix>.*?[\x22\x27]?{sensitive}[\x22\x27]?\s*[:=]\s*)(?P<value>.*)$"
)
redacted = []
continuation_indent = None
for line in s.splitlines(keepends=True):
    content = line.rstrip("\r\n")
    ending = line[len(content):]
    indent = len(content) - len(content.lstrip(" \t"))
    if continuation_indent is not None:
        if not content.strip() or indent > continuation_indent:
            redacted.append(content[:indent] + "[redacted]" + ending)
            continue
        continuation_indent = None
    match = key_line.match(content)
    if match:
        value = match.group("value").strip()
        redacted.append(match.group("prefix") + "[redacted]" + ending)
        if value in ("", "|", ">", "|-", ">-"):
            continuation_indent = indent
        continue
    redacted.append(line)
s = "".join(redacted)
sys.stdout.write(s)
'
}

first_line() {
  awk 'NR == 1 { gsub(/\r/, ""); print; exit }'
}

registry_value() {
  awk -v wanted="$1" '
    /^[[:space:]]*#/ { next }
    {
      line=$0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      if (line ~ ("^" wanted "[[:space:]]*=")) {
        sub(("^" wanted "[[:space:]]*=[[:space:]]*"), "", line)
        sub(/[[:space:]]+$/, "", line)
        if ((line ~ /^".*"$/) || (line ~ /^'\''.*'\''$/)) {
          line=substr(line, 2, length(line)-2)
        }
        print line
        exit
      }
    }
  ' "$REGISTRY"
}

expand_path() {
  local value="$1"
  case "$value" in
    *'`'*|*'$('*)
      return 1
      ;;
  esac
  case "$value" in
    '${HOME}'/*) value="$HOME/${value#'${HOME}'/}" ;;
    '$HOME'/*) value="$HOME/${value#'$HOME'/}" ;;
    '~'/*) value="$HOME/${value#'~'/}" ;;
  esac
  case "$value" in
    /*) printf '%s\n' "$value" ;;
    *) return 1 ;;
  esac
}

probe_version() {
  "$PYTHON_BIN" - "$PROBE_TIMEOUT_SECONDS" "$@" <<'PY'
import subprocess
import sys

timeout = int(sys.argv[1])
command = sys.argv[2:]
try:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    print((result.stdout or "").strip())
except subprocess.TimeoutExpired:
    print("probe timed out")
except OSError as error:
    print("probe unavailable: " + error.__class__.__name__)
PY
}

REGISTRY_STATUS="ok"
KIT_DIR=""
PRODUCT_ROOT=""
if [[ -n "$KIT_DIR_OVERRIDE" || -n "$PRODUCT_ROOT_OVERRIDE" || -n "$KIT_SHA_OVERRIDE" ]]; then
  if [[ -z "$KIT_DIR_OVERRIDE" || -z "$PRODUCT_ROOT_OVERRIDE" || -z "$KIT_SHA_OVERRIDE" ]]; then
    REGISTRY_STATUS="error"
  else
    KIT_DIR="$(expand_path "$KIT_DIR_OVERRIDE" 2>/dev/null || true)"
    PRODUCT_ROOT="$(expand_path "$PRODUCT_ROOT_OVERRIDE" 2>/dev/null || true)"
    if [[ -z "$KIT_DIR" || -z "$PRODUCT_ROOT" ]]; then
      REGISTRY_STATUS="error"
    fi
  fi
elif [[ ! -f "$REGISTRY" ]]; then
  REGISTRY_STATUS="error"
else
  RAW_KIT_DIR="$(registry_value KIT_DIR 2>/dev/null || true)"
  RAW_PRODUCT_ROOT="$(registry_value PRODUCT_ROOT 2>/dev/null || true)"
  KIT_DIR="$(expand_path "$RAW_KIT_DIR" 2>/dev/null || true)"
  PRODUCT_ROOT="$(expand_path "$RAW_PRODUCT_ROOT" 2>/dev/null || true)"
  if [[ -z "$KIT_DIR" || -z "$PRODUCT_ROOT" ]]; then
    REGISTRY_STATUS="error"
  fi
fi

KIT_STATUS="error"
KIT_SHA=""
if [[ -n "$KIT_DIR" && -d "$KIT_DIR" && -n "$KIT_SHA_OVERRIDE" &&
      "$KIT_SHA_OVERRIDE" =~ ^[0-9a-f]{40}$ ]]; then
  KIT_SHA="$KIT_SHA_OVERRIDE"
  KIT_STATUS="ok"
elif [[ -n "$KIT_DIR" ]] && KIT_SHA="$(git -C "$KIT_DIR" rev-parse --verify HEAD 2>/dev/null)"; then
  if [[ "$KIT_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    KIT_STATUS="ok"
  else
    KIT_STATUS="error"
    KIT_SHA=""
  fi
fi

PIN_STATUS="error"
PIN_SHA=""
PIN_VALID="false"
PIN_MATCHES="false"
PIN_FILE=""
if [[ -n "$PRODUCT_ROOT" ]]; then
  PIN_FILE="$PRODUCT_ROOT/factory/KIT_PIN"
fi
if [[ -n "$PIN_FILE" && -f "$PIN_FILE" ]]; then
  PIN_LINE_COUNT="$(awk 'END { print NR + 0 }' "$PIN_FILE" 2>/dev/null || echo 0)"
  PIN_SHA="$(awk 'NR == 1 { sub(/\r$/, ""); print; exit }' "$PIN_FILE" 2>/dev/null || true)"
  if [[ "$PIN_LINE_COUNT" -eq 1 && "$PIN_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    PIN_VALID="true"
    if [[ -n "$KIT_SHA" && "$PIN_SHA" == "$KIT_SHA" ]]; then
      PIN_MATCHES="true"
      PIN_STATUS="ok"
    fi
  else
    PIN_SHA=""
  fi
fi

FACTORY_DIR=""
MAINTENANCE="false"
LAUNCH_LOCK="false"
LEDGER_LOCK="false"
LINEAR_LOCK="false"
GLOBAL_LEDGER_LOCK="false"
PROVIDER_LOCK="false"
PROVIDER_LOCK_STATE="absent"
ACTIVE_RECORDS=0
ACTIVE_RUNS=0
STALE_RUNS=0
MALFORMED_RUNS=0
MAX_CONCURRENT_TICKETS=1
DISPATCH_LEASES=0
STALE_DISPATCH_LEASES=0
MALFORMED_DISPATCH_LEASES=0
if [[ -n "$PRODUCT_ROOT" ]]; then
  FACTORY_DIR="$PRODUCT_ROOT/factory"
  [[ -e "$FACTORY_DIR/MAINTENANCE" ]] && MAINTENANCE="true"
  [[ -e "$FACTORY_DIR/.launch.lock" ]] && LAUNCH_LOCK="true"
  [[ -e "$FACTORY_DIR/.ledger.lock" ]] && LEDGER_LOCK="true"
  [[ -e "$FACTORY_DIR/.linear-sync.lock" ]] && LINEAR_LOCK="true"
  [[ -e "$HOME/.factory/.ledger.lock" ]] && GLOBAL_LEDGER_LOCK="true"
  if [[ -e "$FACTORY_DIR/.provider.lock" || -L "$FACTORY_DIR/.provider.lock" ]]; then
    PROVIDER_LOCK="true"
    PROVIDER_LOCK_STATE="$("$PYTHON_BIN" - "$FACTORY_DIR/.provider.lock" <<'PY'
import os
import pathlib
import re
import stat
import subprocess
import sys

lock = pathlib.Path(sys.argv[1])
try:
    lock_stat = lock.lstat()
    if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISDIR(lock_stat.st_mode):
        raise ValueError
    owner = lock / "owner"
    owner_stat = owner.lstat()
    if (stat.S_ISLNK(owner_stat.st_mode) or
            not stat.S_ISREG(owner_stat.st_mode) or owner_stat.st_nlink != 1):
        raise ValueError
    if sorted(entry.name for entry in lock.iterdir()) != ["owner"]:
        raise ValueError
    lines = owner.read_text(encoding="utf-8").splitlines()
    if (len(lines) != 3 or not re.fullmatch(r"pid=[1-9][0-9]*", lines[0]) or
            not lines[1].startswith("process_start=") or len(lines[1]) == 14 or
            not re.fullmatch(r"token=[0-9a-f]{32}", lines[2])):
        raise ValueError
    pid = int(lines[0][4:])
    process_start = lines[1][14:]
    try:
        current = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False, capture_output=True, text=True,
        ).stdout
        current = " ".join(current.split())
    except OSError:
        current = ""
    print("active" if current and current == process_start else "stale")
except (OSError, UnicodeError, ValueError):
    print("malformed")
PY
)"
  fi
  if [[ -d "$FACTORY_DIR/runs" ]]; then
    for pid_file in "$FACTORY_DIR"/runs/*.pid; do
      [[ -e "$pid_file" ]] || continue
      ACTIVE_RECORDS=$((ACTIVE_RECORDS + 1))
      run_id="$(printf '%s' "$(basename "$pid_file" .pid)" | sanitize | tr '\t\r\n' '___')"
      pid="$(awk -F= '/^pid=[0-9]+$/ { print $2; exit } NR == 1 && /^[0-9]+$/ { print; exit }' "$pid_file" 2>/dev/null || true)"
      if [[ "$pid" =~ ^[0-9]+$ ]]; then
        if kill -0 "$pid" 2>/dev/null; then
          state="active"
          ACTIVE_RUNS=$((ACTIVE_RUNS + 1))
        else
          state="stale"
          STALE_RUNS=$((STALE_RUNS + 1))
        fi
      else
        state="malformed"
        MALFORMED_RUNS=$((MALFORMED_RUNS + 1))
      fi
      printf '%s\t%s\n' "$run_id" "$state" >> "$RUN_FILE"
    done
  fi
  # shellcheck disable=SC1091
  if source "$KIT_DIR/scripts/lib/dispatch-leases.sh" 2>/dev/null &&
     MAX_CONCURRENT_TICKETS="$(factory_dispatch_max_tickets "$PRODUCT_ROOT" "$CONTRACT_VERSION" 2>/dev/null)"; then
    LEASE_DATA="$(python3 - "$FACTORY_DIR/.dispatch-leases" <<'PY'
import json, pathlib, re, stat, sys, time

root = pathlib.Path(sys.argv[1])
if not root.exists():
    raise SystemExit(0)
if root.is_symlink() or not root.is_dir():
    print("state\tmalformed")
    raise SystemExit(0)
for path in sorted(root.iterdir()):
    ticket, state = path.name, "malformed"
    try:
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode) or path.is_symlink() or not re.fullmatch(r"T-[0-9]+\.json", path.name):
            raise ValueError
        record = json.loads(path.read_text())
        ticket = record["ticket"]
        if ticket + ".json" != path.name or record.get("schema_version") != 1:
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{64}", record.get("lease_id", "")):
            raise ValueError
        state = "active" if int(record["expires_epoch"]) > int(time.time()) else "stale"
    except Exception:
        pass
    print(ticket.replace("\t", "_").replace("\n", "_") + "\t" + state)
PY
)"
    if [[ -n "$LEASE_DATA" ]]; then
      printf '%s\n' "$LEASE_DATA" > "$LEASE_FILE"
      while IFS="$(printf '\t')" read -r _ticket state; do
        case "$state" in
          active) DISPATCH_LEASES=$((DISPATCH_LEASES + 1)) ;;
          stale) DISPATCH_LEASES=$((DISPATCH_LEASES + 1)); STALE_DISPATCH_LEASES=$((STALE_DISPATCH_LEASES + 1)) ;;
          *) MALFORMED_DISPATCH_LEASES=$((MALFORMED_DISPATCH_LEASES + 1)) ;;
        esac
      done < "$LEASE_FILE"
    fi
  else
    RUNTIME_STATUS="error"
    MAX_CONCURRENT_TICKETS=0
  fi
fi

RUNTIME_STATUS="${RUNTIME_STATUS:-ok}"
if [[ "$RUNTIME_STATUS" != "error" ]] &&
   [[ "$MAINTENANCE" == "true" || "$LAUNCH_LOCK" == "true" ||
      "$LEDGER_LOCK" == "true" || "$LINEAR_LOCK" == "true" ||
      "$GLOBAL_LEDGER_LOCK" == "true" || "$PROVIDER_LOCK" == "true" ||
      "$ACTIVE_RECORDS" -gt 0 ||
      "$DISPATCH_LEASES" -gt 0 ]]; then
  RUNTIME_STATUS="warning"
fi
[[ "$MALFORMED_DISPATCH_LEASES" -eq 0 ]] || RUNTIME_STATUS="error"
[[ "$PROVIDER_LOCK_STATE" != "malformed" ]] || RUNTIME_STATUS="error"

HERMES_PATH="$(command -v hermes 2>/dev/null || true)"
HERMES_VERSION=""
HERMES_STATUS="unknown"
if [[ -n "$HERMES_PATH" ]]; then
  HERMES_VERSION="$(probe_version "$HERMES_PATH" --version | sanitize | first_line)"
  if printf '%s' "$HERMES_VERSION" | grep -Fq "$SUPPORTED_HERMES_AGENT" &&
     printf '%s' "$HERMES_VERSION" | grep -Fq "$SUPPORTED_HERMES_BUILD"; then
    HERMES_STATUS="ok"
  else
    HERMES_STATUS="warning"
  fi
fi

CLI_STATUS="ok"
for cli_name in claude codex agent gh; do
  cli_path="$(command -v "$cli_name" 2>/dev/null || true)"
  cli_version=""
  cli_item_status="unknown"
  if [[ -n "$cli_path" ]]; then
    cli_version="$(probe_version "$cli_path" --version | sanitize | first_line)"
    case "$cli_version" in
      "")
        cli_version="version unavailable"
        cli_item_status="warning"
        CLI_STATUS="warning"
        ;;
      "probe timed out"|"probe unavailable:"*)
        cli_item_status="warning"
        CLI_STATUS="warning"
        ;;
      *)
        cli_item_status="ok"
        ;;
    esac
  else
    CLI_STATUS="warning"
  fi
  printf '%s\t%s\t%s\t%s\n' \
    "$cli_name" "$cli_item_status" \
    "$(printf '%s' "$cli_path" | sanitize | tr '\t\r\n' '___')" \
    "$(printf '%s' "$cli_version" | sanitize | tr '\t\r\n' '___')" >> "$CLI_FILE"
done

GH_PRESENT="false"
LINEAR_PRESENT="false"
if [[ ${GH_TOKEN+x} == x ]]; then
  GH_PRESENT="true"
elif [[ -f "$PROFILE_DIR/.env" ]] &&
     grep -qE '^[[:space:]]*(export[[:space:]]+)?GH_TOKEN[[:space:]]*=' "$PROFILE_DIR/.env" 2>/dev/null; then
  GH_PRESENT="true"
fi
if [[ ${LINEAR_API_KEY+x} == x ]]; then
  LINEAR_PRESENT="true"
elif [[ -s "$HOME/.hermes/secrets/linear-api-key" ]]; then
  LINEAR_PRESENT="true"
fi
CREDENTIAL_STATUS="ok"
if [[ "$GH_PRESENT" != "true" || "$LINEAR_PRESENT" != "true" ]]; then
  CREDENTIAL_STATUS="warning"
fi

LINEAR_STATUS="unknown"
LINEAR_LAST_SUCCESS=""
LINEAR_AGE=""
LINEAR_LAST_ERROR=""
LINEAR_MAP=""
if [[ -n "$FACTORY_DIR" ]]; then
  LINEAR_MAP="$FACTORY_DIR/linear-map.json"
fi
if [[ -n "$LINEAR_MAP" && -f "$LINEAR_MAP" ]]; then
  LINEAR_DATA="$("$PYTHON_BIN" - "$LINEAR_MAP" "$LINEAR_FRESH_SECONDS" <<'PY'
import datetime as dt
import json
import re
import sys

path, fresh = sys.argv[1], int(sys.argv[2])
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    sync = data.get("_sync") or {}
    success = sync.get("last_success_at") or ""
    error = str(sync.get("_last_error") or sync.get("last_error") or "")
    error = re.sub(
        r"(?im)(authorization\s*:\s*)(?:bearer|basic|token)?\s*[^\r\n]*",
        lambda match: match.group(1) + "[redacted]",
        error,
    )
    error = re.sub(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", "[redacted-url]", error)
    sensitive = r"[A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)[A-Za-z0-9_.-]*"
    quoted = re.compile(
        rf"(?is)(?P<prefix>[\"']?{sensitive}[\"']?\s*[:=]\s*)"
        rf"(?P<quote>[\"'])(?:\\.|(?!(?P=quote)).)*(?P=quote)"
    )
    error = quoted.sub(lambda match: match.group("prefix") + "[redacted]", error)
    error = re.sub(
        rf"(?im)^(\s*[\"']?{sensitive}[\"']?\s*[:=]\s*).*$",
        lambda match: match.group(1) + "[redacted]",
        error,
    )
    error = error.replace("\r", " ").replace("\n", " ")
    age = ""
    status = "warning" if error else "unknown"
    if success:
        parsed = dt.datetime.fromisoformat(success.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        age = max(0, int((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()))
        status = "ok" if age <= fresh and not error else "warning"
    print(status)
    print(success)
    print(age)
    print(error)
except Exception:
    print("error")
    print("")
    print("")
    print("invalid Linear sync metadata")
PY
)"
  LINEAR_STATUS="$(printf '%s\n' "$LINEAR_DATA" | awk 'NR == 1 { print; exit }')"
  LINEAR_LAST_SUCCESS="$(printf '%s\n' "$LINEAR_DATA" | awk 'NR == 2 { print; exit }')"
  LINEAR_AGE="$(printf '%s\n' "$LINEAR_DATA" | awk 'NR == 3 { print; exit }')"
  LINEAR_LAST_ERROR="$(printf '%s\n' "$LINEAR_DATA" | awk 'NR == 4 { print; exit }' | sanitize)"
fi

PROVIDER_RUNTIME_STATUS="ok"
PROVIDER_ACTIVATED=false
PROVIDER_EXECUTION_MODE=""
PROVIDER_ACTIVE_ATTEMPTS=0
PROVIDER_ACTIVE_TOKENS=0
PROVIDER_UNKNOWN_WORKERS=0
PROVIDER_LEGACY_INTERVALS=0
if [[ ( "$CONTRACT_VERSION" == "1.6.0" || "$CONTRACT_VERSION" == "1.7.0" ||
        "$CONTRACT_VERSION" == "1.8.0" ) &&
      -n "${FACTORY_PROVIDER_ACTIVATION:-}" &&
      -f "${FACTORY_PROVIDER_ACTIVATION:-}" ]]; then
  PROVIDER_ACTIVATED=true
  PROVIDER_ACTIVATION_ARGS=(--config "$FACTORY_PROVIDER_ACTIVATION" \
    --contract-version "$CONTRACT_VERSION")
  [[ -z "${FACTORY_PROVIDER_POLICY:-}" ]] ||
    PROVIDER_ACTIVATION_ARGS+=(--policy "$FACTORY_PROVIDER_POLICY")
  PROVIDER_ACTIVATION_STATUS="$("$PYTHON_BIN" -I -S \
    "$KIT_DIR/scripts/provider-activation.py" \
    "${PROVIDER_ACTIVATION_ARGS[@]}" --status 2>/dev/null || true)"
  PROVIDER_EXECUTION_MODE="$(printf '%s' "$PROVIDER_ACTIVATION_STATUS" | \
    "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
    if value.get("status") != "enabled":
        raise ValueError
    print(value["execution_mode"])
except Exception:
    raise SystemExit(1)
' 2>/dev/null || true)"
  if [[ "$PROVIDER_EXECUTION_MODE" == "api-isolated-v1" &&
        -n "${FACTORY_PROVIDER_DB:-}" && -f "${FACTORY_PROVIDER_DB:-}" &&
        -n "${FACTORY_PROVIDER_BROKER_DB:-}" && -f "${FACTORY_PROVIDER_BROKER_DB:-}" &&
        -n "${FACTORY_PROVIDER_CREDENTIALS:-}" && -f "${FACTORY_PROVIDER_CREDENTIALS:-}" &&
        -n "${FACTORY_PROVIDER_ATTEMPT_ROOT:-}" && -d "${FACTORY_PROVIDER_ATTEMPT_ROOT:-}" &&
        -x "$KIT_DIR/scripts/provider-recovery.py" ]]; then
    PROVIDER_RECOVERY_OUTPUT="$("$PYTHON_BIN" "$KIT_DIR/scripts/provider-recovery.py" \
      --db "$FACTORY_PROVIDER_DB" \
      --broker-db "$FACTORY_PROVIDER_BROKER_DB" \
      --broker-credentials "$FACTORY_PROVIDER_CREDENTIALS" \
      --attempt-root "$FACTORY_PROVIDER_ATTEMPT_ROOT" \
      status 2>/dev/null || true)"
    PROVIDER_RECOVERY_FIELDS="$(printf '%s' "$PROVIDER_RECOVERY_OUTPUT" | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
    print(value["health"])
    print(sum(1 for item in value["attempts"] if item.get("state") in ("reserved", "GO", "submitted")))
    print(value["active_tokens"])
    print(value["unknown_workers"])
    print(len(value["legacy_intervals"]))
except Exception:
    raise SystemExit(1)
' 2>/dev/null || true)"
    if [[ -n "$PROVIDER_RECOVERY_FIELDS" ]]; then
      PROVIDER_RUNTIME_STATUS="$(printf '%s\n' "$PROVIDER_RECOVERY_FIELDS" | awk 'NR==1')"
      PROVIDER_ACTIVE_ATTEMPTS="$(printf '%s\n' "$PROVIDER_RECOVERY_FIELDS" | awk 'NR==2')"
      PROVIDER_ACTIVE_TOKENS="$(printf '%s\n' "$PROVIDER_RECOVERY_FIELDS" | awk 'NR==3')"
      PROVIDER_UNKNOWN_WORKERS="$(printf '%s\n' "$PROVIDER_RECOVERY_FIELDS" | awk 'NR==4')"
      PROVIDER_LEGACY_INTERVALS="$(printf '%s\n' "$PROVIDER_RECOVERY_FIELDS" | awk 'NR==5')"
    else
      PROVIDER_RUNTIME_STATUS="error"
    fi
  elif [[ "$PROVIDER_EXECUTION_MODE" == "cli-concurrent-v1" &&
          -n "${FACTORY_PROVIDER_DB:-}" && -f "${FACTORY_PROVIDER_DB:-}" &&
          -n "${FACTORY_PROVIDER_POLICY:-}" && -f "${FACTORY_PROVIDER_POLICY:-}" &&
          -n "${FACTORY_PROVIDER_ATTEMPT_ROOT:-}" && -d "${FACTORY_PROVIDER_ATTEMPT_ROOT:-}" &&
          -n "${FACTORY_PROVIDER_APPLY_LOCK_ROOT:-}" && -d "${FACTORY_PROVIDER_APPLY_LOCK_ROOT:-}" ]]; then
    PROVIDER_CLI_FIELDS="$("$PYTHON_BIN" -I -S - \
      "$PROVIDER_ACTIVATION_STATUS" "$FACTORY_PROVIDER_POLICY" \
      "$FACTORY_PROVIDER_DB" "$FACTORY_PROVIDER_ATTEMPT_ROOT" \
      "$FACTORY_PROVIDER_APPLY_LOCK_ROOT" <<'PY' 2>/dev/null || true
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

activation_status = json.loads(sys.argv[1])
policy_path, database_path, attempt_root, apply_root = map(Path, sys.argv[2:])

def secure(path, *, directory=False, owner_only=False):
    info = path.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not path.is_absolute() or path.is_symlink() or not expected
            or info.st_uid != os.geteuid() or (not directory and info.st_nlink != 1)
            or info.st_mode & (0o077 if owner_only else 0o022)):
        raise SystemExit(1)

secure(policy_path)
secure(database_path, owner_only=True)
secure(attempt_root, directory=True)
secure(apply_root, directory=True)
policy = json.loads(policy_path.read_text(encoding="utf-8"))
policy_digest = hashlib.sha256(json.dumps(
    policy, ensure_ascii=True, sort_keys=True, separators=(",", ":")
).encode()).hexdigest()
if activation_status.get("policy_sha256") != policy_digest:
    raise SystemExit(1)
uri = "file:" + str(database_path) + "?mode=ro"
connection = sqlite3.connect(uri, uri=True)
try:
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA application_id").fetchone()[0] != 0x4E595343:
        raise SystemExit(1)
    if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
        raise SystemExit(1)
    if connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone() != ("factory-provider-state/v2",):
        raise SystemExit(1)
    active = connection.execute(
        "SELECT count(*) FROM attempts WHERE state IN ('reserved','GO','submitted')"
    ).fetchone()[0]
    legacy = connection.execute("SELECT count(*) FROM legacy_intervals").fetchone()[0]
finally:
    connection.close()
print(active)
print(legacy)
PY
)"
    if [[ -n "$PROVIDER_CLI_FIELDS" ]]; then
      PROVIDER_ACTIVE_ATTEMPTS="$(printf '%s\n' "$PROVIDER_CLI_FIELDS" | awk 'NR==1')"
      PROVIDER_LEGACY_INTERVALS="$(printf '%s\n' "$PROVIDER_CLI_FIELDS" | awk 'NR==2')"
    else
      PROVIDER_RUNTIME_STATUS="error"
    fi
  else
    PROVIDER_RUNTIME_STATUS="error"
  fi
fi

OVERALL_STATUS="ok"
for check_status in "$REGISTRY_STATUS" "$KIT_STATUS" "$PIN_STATUS" "$RUNTIME_STATUS" \
                    "$HERMES_STATUS" "$CLI_STATUS" "$CREDENTIAL_STATUS" "$LINEAR_STATUS" \
                    "$PROVIDER_RUNTIME_STATUS"; do
  if [[ "$check_status" == "error" ]]; then
    OVERALL_STATUS="error"
    break
  fi
  if [[ "$check_status" == "warning" || "$check_status" == "unknown" ]]; then
    OVERALL_STATUS="warning"
  fi
done

OUTPUT_PROFILE_DIR="$(printf '%s' "$PROFILE_DIR" | sanitize)"
OUTPUT_REGISTRY="$(printf '%s' "$REGISTRY" | sanitize)"
OUTPUT_KIT_DIR="$(printf '%s' "$KIT_DIR" | sanitize)"
OUTPUT_PRODUCT_ROOT="$(printf '%s' "$PRODUCT_ROOT" | sanitize)"
OUTPUT_PIN_FILE="$(printf '%s' "$PIN_FILE" | sanitize)"
OUTPUT_FACTORY_DIR="$(printf '%s' "$FACTORY_DIR" | sanitize)"
OUTPUT_LINEAR_MAP="$(printf '%s' "$LINEAR_MAP" | sanitize)"

export CONTRACT_VERSION DOCTOR_SCHEMA PROJECT REGISTRY_STATUS
export OUTPUT_PROFILE_DIR OUTPUT_REGISTRY OUTPUT_KIT_DIR OUTPUT_PRODUCT_ROOT
export KIT_STATUS KIT_SHA PIN_STATUS OUTPUT_PIN_FILE PIN_SHA PIN_VALID PIN_MATCHES
export RUNTIME_STATUS OUTPUT_FACTORY_DIR MAINTENANCE LAUNCH_LOCK LEDGER_LOCK LINEAR_LOCK GLOBAL_LEDGER_LOCK
export PROVIDER_LOCK PROVIDER_LOCK_STATE
export ACTIVE_RECORDS ACTIVE_RUNS STALE_RUNS MALFORMED_RUNS
export MAX_CONCURRENT_TICKETS DISPATCH_LEASES STALE_DISPATCH_LEASES MALFORMED_DISPATCH_LEASES LEASE_FILE
export HERMES_STATUS HERMES_PATH HERMES_VERSION CLI_STATUS CLI_FILE
export CREDENTIAL_STATUS GH_PRESENT LINEAR_PRESENT
export LINEAR_STATUS OUTPUT_LINEAR_MAP LINEAR_LAST_SUCCESS LINEAR_AGE LINEAR_LAST_ERROR
export PROVIDER_RUNTIME_STATUS PROVIDER_ACTIVATED PROVIDER_ACTIVE_ATTEMPTS
export PROVIDER_EXECUTION_MODE
export PROVIDER_ACTIVE_TOKENS PROVIDER_UNKNOWN_WORKERS PROVIDER_LEGACY_INTERVALS
export OVERALL_STATUS RUN_FILE

if [[ "$JSON_MODE" -eq 1 ]]; then
  "$PYTHON_BIN" <<'PY'
import json
import os

def optional(name):
    value = os.environ.get(name, "")
    return value if value else None

def boolean(name):
    return os.environ.get(name) == "true"

def number(name):
    value = os.environ.get(name, "")
    return int(value) if value else None

clis = []
with open(os.environ["CLI_FILE"], encoding="utf-8") as handle:
    for line in handle:
        name, status, path, version = line.rstrip("\n").split("\t", 3)
        clis.append({
            "name": name,
            "status": status,
            "path": path or None,
            "version": version or None,
        })

runs = []
with open(os.environ["RUN_FILE"], encoding="utf-8") as handle:
    for line in handle:
        run_id, state = line.rstrip("\n").split("\t", 1)
        runs.append({"run_id": run_id, "state": state})

leases = []
with open(os.environ["LEASE_FILE"], encoding="utf-8") as handle:
    for line in handle:
        ticket, state = line.rstrip("\n").split("\t", 1)
        leases.append({"ticket": ticket, "state": state})

document = {
    "schema": os.environ["DOCTOR_SCHEMA"],
    "schema_version": 1,
    "contract_version": os.environ["CONTRACT_VERSION"],
    "overall_status": os.environ["OVERALL_STATUS"],
    "project": os.environ["PROJECT"],
    "checks": {
        "registry": {
            "status": os.environ["REGISTRY_STATUS"],
            "path": os.environ["OUTPUT_REGISTRY"],
            "profile_path": os.environ["OUTPUT_PROFILE_DIR"],
            "kit_dir": optional("OUTPUT_KIT_DIR"),
            "product_root": optional("OUTPUT_PRODUCT_ROOT"),
        },
        "kit": {
            "status": os.environ["KIT_STATUS"],
            "full_sha": optional("KIT_SHA"),
        },
        "kit_pin": {
            "status": os.environ["PIN_STATUS"],
            "path": optional("OUTPUT_PIN_FILE"),
            "full_sha": optional("PIN_SHA"),
            "valid_full_sha": boolean("PIN_VALID"),
            "matches_kit": boolean("PIN_MATCHES"),
        },
        "runtime": {
            "status": os.environ["RUNTIME_STATUS"],
            "factory_dir": optional("OUTPUT_FACTORY_DIR"),
            "maintenance": boolean("MAINTENANCE"),
            "locks": {
                "launch": boolean("LAUNCH_LOCK"),
                "ledger": boolean("LEDGER_LOCK"),
                "linear_sync": boolean("LINEAR_LOCK"),
                "global_ledger": boolean("GLOBAL_LEDGER_LOCK"),
                "provider": boolean("PROVIDER_LOCK"),
            },
            "provider_lock_state": os.environ["PROVIDER_LOCK_STATE"],
            "run_records": number("ACTIVE_RECORDS"),
            "active_runs": number("ACTIVE_RUNS"),
            "stale_runs": number("STALE_RUNS"),
            "malformed_runs": number("MALFORMED_RUNS"),
            "runs": runs,
            "max_concurrent_tickets": number("MAX_CONCURRENT_TICKETS"),
            "dispatch_lease_records": number("DISPATCH_LEASES"),
            "stale_dispatch_leases": number("STALE_DISPATCH_LEASES"),
            "malformed_dispatch_leases": number("MALFORMED_DISPATCH_LEASES"),
            "dispatch_leases": leases,
        },
        "hermes": {
            "status": os.environ["HERMES_STATUS"],
            "path": optional("HERMES_PATH"),
            "version": optional("HERMES_VERSION"),
        },
        "clis": {
            "status": os.environ["CLI_STATUS"],
            "items": clis,
        },
        "credentials": {
            "status": os.environ["CREDENTIAL_STATUS"],
            "validated_authentication": False,
            "presence": {
                "github": boolean("GH_PRESENT"),
                "linear": boolean("LINEAR_PRESENT"),
            },
        },
        "linear_sync": {
            "status": os.environ["LINEAR_STATUS"],
            "path": optional("OUTPUT_LINEAR_MAP"),
            "last_success_at": optional("LINEAR_LAST_SUCCESS"),
            "age_seconds": number("LINEAR_AGE"),
            "last_error": optional("LINEAR_LAST_ERROR"),
        },
        "isolated_provider": {
            "status": os.environ["PROVIDER_RUNTIME_STATUS"],
            "activated": boolean("PROVIDER_ACTIVATED"),
            "execution_mode": optional("PROVIDER_EXECUTION_MODE"),
            "active_attempts": number("PROVIDER_ACTIVE_ATTEMPTS"),
            "active_tokens": number("PROVIDER_ACTIVE_TOKENS"),
            "unknown_workers": number("PROVIDER_UNKNOWN_WORKERS"),
            "legacy_intervals": number("PROVIDER_LEGACY_INTERVALS"),
        },
    },
}
print(json.dumps(document, indent=2, sort_keys=True))
PY
else
  echo "Factory doctor: $(printf '%s' "$OVERALL_STATUS" | tr '[:lower:]' '[:upper:]')"
  echo "Contract: $CONTRACT_VERSION"
  echo "Project: $PROJECT"
  echo "Registry [$REGISTRY_STATUS]: $OUTPUT_REGISTRY"
  echo "Kit [$KIT_STATUS]: ${KIT_SHA:-unavailable}"
  echo "KIT_PIN [$PIN_STATUS]: ${PIN_SHA:-missing or invalid}"
  echo "Runtime [$RUNTIME_STATUS]: maintenance=$MAINTENANCE active=$ACTIVE_RUNS stale=$STALE_RUNS malformed=$MALFORMED_RUNS concurrency=$MAX_CONCURRENT_TICKETS leases=$DISPATCH_LEASES"
  echo "Locks: launch=$LAUNCH_LOCK ledger=$LEDGER_LOCK linear_sync=$LINEAR_LOCK global_ledger=$GLOBAL_LEDGER_LOCK provider=$PROVIDER_LOCK provider_state=$PROVIDER_LOCK_STATE"
  echo "Hermes [$HERMES_STATUS]: ${HERMES_VERSION:-unavailable} (${HERMES_PATH:-not found})"
  while IFS="$(printf '\t')" read -r cli_name cli_item_status cli_path cli_version; do
    echo "CLI $cli_name [$cli_item_status]: ${cli_version:-unavailable} (${cli_path:-not found})"
  done < "$CLI_FILE"
  echo "Credentials [$CREDENTIAL_STATUS]: github=$GH_PRESENT linear=$LINEAR_PRESENT (presence only; authentication not validated)"
  echo "Isolated provider [$PROVIDER_RUNTIME_STATUS]: activated=$PROVIDER_ACTIVATED mode=${PROVIDER_EXECUTION_MODE:-none} attempts=$PROVIDER_ACTIVE_ATTEMPTS tokens=$PROVIDER_ACTIVE_TOKENS unknown_workers=$PROVIDER_UNKNOWN_WORKERS legacy=$PROVIDER_LEGACY_INTERVALS"
  echo "Linear sync [$LINEAR_STATUS]: age_seconds=${LINEAR_AGE:-unknown} last_success=${LINEAR_LAST_SUCCESS:-unknown}"
  [[ -z "$LINEAR_LAST_ERROR" ]] || echo "Linear last error: $LINEAR_LAST_ERROR"
fi

[[ "$OVERALL_STATUS" == "error" ]] && exit 1
exit 0

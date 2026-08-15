#!/usr/bin/env bash
# Read-only Software Factory diagnostics.
# Public interface: factory-doctor.sh [--json] [--project <slug>]
set -u

CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-2.0.0}"
DOCTOR_SCHEMA="nysa.software-factory.doctor/v2"

JSON_MODE=0
PROJECT="${FACTORY_PROJECT:-relay}"
PROBE_TIMEOUT_SECONDS="${FACTORY_DOCTOR_TIMEOUT_SECONDS:-5}"
READINESS_TIMEOUT_SECONDS="${FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS:-30}"
KIT_DIR_OVERRIDE=""
PRODUCT_ROOT_OVERRIDE=""
KIT_SHA_OVERRIDE=""

usage() {
  echo "usage: factory-doctor.sh [--json] [--project <slug>] --kit-dir <path> --product-root <path> --kit-sha <full-sha>" >&2
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
if [[ ! "$PROBE_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "timeout value must be a non-negative integer" >&2
  exit 2
fi
if [[ ! "$READINESS_TIMEOUT_SECONDS" =~ ^[0-9]+$ ||
      "$READINESS_TIMEOUT_SECONDS" -lt 1 ||
      "$READINESS_TIMEOUT_SECONDS" -gt 120 ]]; then
  echo "readiness timeout value must be an integer from 1 through 120" >&2
  exit 2
fi
PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ "$JSON_MODE" -eq 1 ]]; then
    printf '%s\n' "{\"schema\":\"nysa.software-factory.doctor/v2\",\"schema_version\":2,\"contract_version\":\"$CONTRACT_VERSION\",\"overall_status\":\"error\",\"error\":\"python3 unavailable\"}"
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
ACTIVE_CLAIM_FILE="$TMP/active-run-claims.tsv"
LEASE_FILE="$TMP/leases.tsv"
CONTRACT_RESUME_FILE="$TMP/contract-resume.json"
TRANSITION_RECEIPT_FILE="$TMP/transition-receipts.json"
: > "$CLI_FILE"
: > "$RUN_FILE"
: > "$ACTIVE_CLAIM_FILE"
: > "$LEASE_FILE"
printf '[]\n' > "$CONTRACT_RESUME_FILE"
printf '[]\n' > "$TRANSITION_RECEIPT_FILE"

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

bounded_command() {
  local timeout="$1"
  local output="$2"
  shift 2
  "$PYTHON_BIN" - "$timeout" "$output" "$@" <<'PY'
import os
import resource
import signal
import subprocess
import sys

timeout = int(sys.argv[1])
path = sys.argv[2]
command = sys.argv[3:]
limit_bytes = 1024 * 1024


def limit_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit_bytes, limit_bytes))


try:
    with open(path, "xb") as stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=limit_output,
        )
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            status = 3
    if os.path.getsize(path) > limit_bytes:
        status = 3
except OSError:
    status = 3
raise SystemExit(status if status in {0, 2} else 3)
PY
}

BINDING_STATUS="ok"
KIT_DIR=""
PRODUCT_ROOT=""
if [[ -z "$KIT_DIR_OVERRIDE" || -z "$PRODUCT_ROOT_OVERRIDE" || -z "$KIT_SHA_OVERRIDE" ]]; then
  BINDING_STATUS="error"
else
  KIT_DIR="$(expand_path "$KIT_DIR_OVERRIDE" 2>/dev/null || true)"
  PRODUCT_ROOT="$(expand_path "$PRODUCT_ROOT_OVERRIDE" 2>/dev/null || true)"
  if [[ -z "$KIT_DIR" || -z "$PRODUCT_ROOT" ]]; then
    BINDING_STATUS="error"
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
GLOBAL_LEDGER_LOCK="false"
PROVIDER_LOCK="false"
PROVIDER_LOCK_STATE="absent"
ACTIVE_RECORDS=0
ACTIVE_RUNS=0
STALE_RUNS=0
MALFORMED_RUNS=0
ACTIVE_RUN_CLAIMS=0
MALFORMED_ACTIVE_RUN_CLAIMS=0
MAX_CONCURRENT_TICKETS=1
DISPATCH_LEASES=0
STALE_DISPATCH_LEASES=0
MALFORMED_DISPATCH_LEASES=0
if [[ -n "$PRODUCT_ROOT" ]]; then
  FACTORY_DIR="$PRODUCT_ROOT/factory"
  [[ -e "$FACTORY_DIR/MAINTENANCE" ]] && MAINTENANCE="true"
  [[ -e "$FACTORY_DIR/.launch.lock" ]] && LAUNCH_LOCK="true"
  [[ -e "$FACTORY_DIR/.ledger.lock" ]] && LEDGER_LOCK="true"
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
  if ! ACTIVE_CLAIM_DATA="$("$PYTHON_BIN" -I -S - "$FACTORY_DIR/.active-runs" <<'PY'
import os, pathlib, re, stat, sys

root = pathlib.Path(sys.argv[1])
try:
    info = root.lstat()
except FileNotFoundError:
    raise SystemExit(0)
try:
    if (
        root.is_symlink() or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid() or info.st_mode & 0o022
    ):
        raise ValueError
    tickets = set()
    for path in sorted(root.iterdir()):
        match = re.fullmatch(
            r"(T-[0-9]+)[.][A-Za-z0-9_-]+[.](lock|pid)", path.name,
        )
        if match is None:
            raise ValueError
        item = path.lstat()
        if (
            path.is_symlink() or item.st_uid != os.geteuid()
            or item.st_mode & 0o022
            or match.group(2) == "lock" and not stat.S_ISDIR(item.st_mode)
            or match.group(2) == "pid" and (
                not stat.S_ISREG(item.st_mode)
                or item.st_nlink != 1 or item.st_size > 10_000
            )
        ):
            raise ValueError
        tickets.add(match.group(1))
except (OSError, ValueError):
    print("state\tmalformed")
    raise SystemExit(0)
for ticket in sorted(tickets):
    print(ticket + "\tactive")
PY
)"; then
    ACTIVE_CLAIM_DATA="$(printf 'state\tmalformed')"
  fi
  if [[ -n "$ACTIVE_CLAIM_DATA" ]]; then
    printf '%s\n' "$ACTIVE_CLAIM_DATA" > "$ACTIVE_CLAIM_FILE"
    while IFS="$(printf '\t')" read -r _ticket state; do
      case "$state" in
        active) ACTIVE_RUN_CLAIMS=$((ACTIVE_RUN_CLAIMS + 1)) ;;
        *) MALFORMED_ACTIVE_RUN_CLAIMS=$((MALFORMED_ACTIVE_RUN_CLAIMS + 1)) ;;
      esac
    done < "$ACTIVE_CLAIM_FILE"
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
      "$LEDGER_LOCK" == "true" ||
      "$GLOBAL_LEDGER_LOCK" == "true" || "$PROVIDER_LOCK" == "true" ||
      "$ACTIVE_RECORDS" -gt 0 ||
      "$ACTIVE_RUN_CLAIMS" -gt 0 ||
      "$DISPATCH_LEASES" -gt 0 ]]; then
  RUNTIME_STATUS="warning"
fi
[[ "$MALFORMED_DISPATCH_LEASES" -eq 0 ]] || RUNTIME_STATUS="error"
[[ "$MALFORMED_ACTIVE_RUN_CLAIMS" -eq 0 ]] || RUNTIME_STATUS="error"
[[ "$PROVIDER_LOCK_STATE" != "malformed" ]] || RUNTIME_STATUS="error"

CONTRACT_RESUME_STATUS="ok"
TRANSITION_RECEIPT_STATUS="ok"
if [[ -n "${FACTORY_CONTROLLER_STATE_DIR:-}" ]]; then
  if ! "$PYTHON_BIN" -I -S - "$FACTORY_CONTROLLER_STATE_DIR" \
      "$TRANSITION_RECEIPT_FILE" \
      > "$CONTRACT_RESUME_FILE" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1]).resolve()
transition_file = Path(sys.argv[2])
resolved = {"contract_blocker_recovered", "recorded_contract_repair_prepared"}
transition_terminal = {"ticket_complete", "ticket_released", "ticket_retired"}
transition_prior_migrated = {
    "passportless_route_migration_recovered", "route_migration_cleared",
    "upgraded_bundle_refresh_recovered", "upgraded_claim_recovered",
    "upgraded_merged_claim_recovered",
}
transition_resolved = transition_terminal | transition_prior_migrated

def secure(path, *, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not path.is_absolute() or path.resolve() != path or path.is_symlink()
            or not kind or info.st_uid != os.geteuid()
            or (not directory and info.st_nlink != 1)
            or info.st_mode & (0o022 if directory else 0o077)):
        raise ValueError

try:
    secure(root, directory=True)
    claims = root / "claims"
    if claims.exists() or claims.is_symlink():
        secure(claims, directory=True)
    events = root / "events"
    if not events.exists():
        transition_file.write_text("[]\n", encoding="utf-8")
        print("[]")
        raise SystemExit(0)
    secure(events, directory=True)
    latest = {}
    transition_latest = {}
    transition_terminal_epoch = {}
    transition_migration_epoch = {}
    for path in sorted(events.iterdir()):
        if path.suffix != ".json":
            continue
        secure(path)
        if path.stat().st_size > 1_048_576:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        digest = value.pop("event_sha256", "")
        canonical = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        if (digest != hashlib.sha256(canonical).hexdigest()
                or value.get("schema") != "nysa.software-factory.controller-event/v1"
                or not isinstance(value.get("observed_at_epoch_ns"), int)
                or isinstance(value.get("observed_at_epoch_ns"), bool)
                or value["observed_at_epoch_ns"] < 0):
            raise ValueError
        event = value.get("event")
        if (
            event != "contract_resume_refused"
            and event not in resolved
            and event not in {
                "prior_kit_transition_receipt_observed",
                "transition_receipt_invalid",
            }
            and event not in transition_resolved
        ):
            continue
        ticket = value.get("ticket")
        if not isinstance(ticket, str) or not re.fullmatch(r"T-[0-9]+", ticket):
            raise ValueError
        factory_sha = value.get("factory_sha")
        base_fields = {
            "event", "factory_sha", "observed_at_epoch_ns", "schema", "ticket",
        }
        upgrade_fields = base_fields | {"from_factory_sha"}
        qualification_fields = {
            "qualification_generation", "qualification_manifest_sha256",
        }
        if (
            factory_sha is None
            and event == "contract_blocker_recovered"
            and set(value) == base_fields | {"failed_run_id"}
            and isinstance(value.get("failed_run_id"), str)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value["failed_run_id"])
        ):
            continue
        if (
            factory_sha is None
            and event == "ticket_released"
            and set(value) == base_fields
        ):
            continue
        if (
            event == "upgraded_claim_recovered"
            and set(value) in (
                upgrade_fields, upgrade_fields | qualification_fields,
            )
            and isinstance(value.get("from_factory_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", value.get("from_factory_sha", ""))
            and (
                set(value) == upgrade_fields
                or isinstance(value.get("qualification_generation"), int)
                and not isinstance(value["qualification_generation"], bool)
                and value["qualification_generation"] > 0
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    value.get("qualification_manifest_sha256", ""),
                )
            )
            and (
                factory_sha is None
                or (
                    isinstance(factory_sha, str)
                    and re.fullmatch(r"[0-9a-f]{40}", factory_sha)
                    and value["from_factory_sha"] == factory_sha
                )
            )
        ):
            continue
        if (
            not isinstance(factory_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", factory_sha)
        ):
            raise ValueError
        if event == "contract_resume_refused":
            reason = value.get("reason_code")
            if (not isinstance(reason, str)
                    or not re.fullmatch(r"resume_[a-z0-9_]{1,120}", reason)
                    or not re.fullmatch(
                        r"[0-9a-f]{64}", value.get("blocked_receipt_sha256", "")
                    )):
                raise ValueError
            incident = {
                key: value[key]
                for key in (
                    "actual_bytes", "blocked_receipt_sha256",
                    "changed_path_count", "expected_bytes", "first_differing_line",
                    "local_head", "observed_at_epoch_ns", "offending_parent", "reason_code",
                    "remote_head", "ticket",
                )
                if key in value
            }
            for key in (
                "actual_bytes", "changed_path_count", "expected_bytes",
                "first_differing_line",
            ):
                if key in incident and incident[key] is not None and (
                    isinstance(incident[key], bool)
                    or not isinstance(incident[key], int)
                    or incident[key] < 0
                ):
                    raise ValueError
            for key in ("local_head", "offending_parent", "remote_head"):
                if (key in incident and incident[key] not in {None, ""}
                        and not re.fullmatch(r"[0-9a-f]{40}", incident[key])):
                    raise ValueError
        elif event in resolved:
            incident = None
        elif event == "prior_kit_transition_receipt_observed":
            active = value.get("active_factory_sha")
            prior = value.get("receipt_factory_sha")
            receipt = value.get("transition_receipt_sha256")
            if (
                not isinstance(active, str)
                or not re.fullmatch(r"[0-9a-f]{40}", active)
                or active != value.get("factory_sha")
                or not isinstance(prior, str)
                or not re.fullmatch(r"[0-9a-f]{40}", prior)
                or prior == active
                or not isinstance(receipt, str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt)
            ):
                raise ValueError
            transition_incident = {
                "active_factory_sha": active,
                "observed_at_epoch_ns": value["observed_at_epoch_ns"],
                "reason_code": "prior_kit_receipt",
                "receipt_factory_sha": prior,
                "ticket": ticket,
                "transition_receipt_sha256": receipt,
            }
        elif event == "transition_receipt_invalid":
            reason = value.get("reason_code")
            if reason not in {
                "receipt_digest_invalid", "receipt_identity_invalid",
                "receipt_unreadable",
            }:
                raise ValueError
            transition_incident = {
                "observed_at_epoch_ns": value["observed_at_epoch_ns"],
                "reason_code": reason,
                "ticket": ticket,
            }
        elif event == "passportless_route_migration_recovered":
            if not re.fullmatch(
                r"[0-9a-f]{64}", value.get("refused_receipt_sha256", "")
            ):
                raise ValueError
        elif event == "upgraded_claim_recovered":
            prior = value.get("from_factory_sha", "")
            if (
                not re.fullmatch(r"[0-9a-f]{40}", prior)
                or prior == value["factory_sha"]
            ):
                raise ValueError
        observed = value["observed_at_epoch_ns"]
        if event == "contract_resume_refused" or event in resolved:
            if ticket not in latest or observed > latest[ticket][0]:
                latest[ticket] = (observed, incident)
        if (
            event in {
                "prior_kit_transition_receipt_observed",
                "transition_receipt_invalid",
            }
        ) and (
            ticket not in transition_latest
            or observed > transition_latest[ticket][0]
        ):
            transition_latest[ticket] = (observed, transition_incident)
        if event in transition_terminal:
            transition_terminal_epoch[ticket] = max(
                observed, transition_terminal_epoch.get(ticket, -1)
            )
        if event in transition_prior_migrated:
            previous = transition_migration_epoch.get(ticket, (-1, ""))
            if observed > previous[0]:
                transition_migration_epoch[ticket] = (
                    observed, value["factory_sha"]
                )
    def terminally_resolved(ticket, observed):
        claim = claims / f"{ticket}.json"
        return (
            transition_terminal_epoch.get(ticket, -1) > observed
            and not claim.exists()
            and not claim.is_symlink()
        )

    transition_file.write_text(json.dumps([
        incident
        for observed, incident in sorted(
            transition_latest.values(), key=lambda item: item[0]
        )
        if not terminally_resolved(incident["ticket"], observed)
        and (
            incident["reason_code"] != "prior_kit_receipt"
            or transition_migration_epoch.get(
                incident["ticket"], (-1, "")
            )[0] <= observed
            or transition_migration_epoch[incident["ticket"]][1]
            != incident["active_factory_sha"]
        )
    ], sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps([
        incident
        for _, incident in sorted(latest.values(), key=lambda item: item[0])
        if incident is not None
    ], sort_keys=True))
except (
    FileNotFoundError, json.JSONDecodeError, OSError, TypeError, UnicodeError,
    ValueError,
):
    raise SystemExit(1)
PY
  then
    CONTRACT_RESUME_STATUS="error"
    TRANSITION_RECEIPT_STATUS="error"
    printf '[]\n' > "$CONTRACT_RESUME_FILE"
    printf '[]\n' > "$TRANSITION_RECEIPT_FILE"
  elif [[ "$(tr -d '[:space:]' < "$CONTRACT_RESUME_FILE")" != "[]" ]]; then
    CONTRACT_RESUME_STATUS="warning"
  fi
  if [[ "$TRANSITION_RECEIPT_STATUS" != "error" ]] &&
     [[ "$(tr -d '[:space:]' < "$TRANSITION_RECEIPT_FILE")" != "[]" ]]; then
    TRANSITION_RECEIPT_STATUS="warning"
  fi
fi

CONTROLLER_STATUS="not_applicable"
CONTROLLER_SERVICE_STATE="not_applicable"
CONTROLLER_LAST_EXIT_STATUS=""
CONTROLLER_PLATFORM="$(/usr/bin/uname -s 2>/dev/null || true)"
CONTROLLER_LAUNCHCTL="/bin/launchctl"
if [[ "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == "1" ]]; then
  [[ -z "${FACTORY_DOCTOR_PLATFORM:-}" ]] ||
    CONTROLLER_PLATFORM="$FACTORY_DOCTOR_PLATFORM"
  [[ -z "${FACTORY_DOCTOR_LAUNCHCTL:-}" ]] ||
    CONTROLLER_LAUNCHCTL="$FACTORY_DOCTOR_LAUNCHCTL"
fi
if [[ ( "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) &&
      "${FACTORY_KIT_TRUST_SCOPE:-}" == "production-certified" &&
      "${FACTORY_TEST_MODE:-0}" != "1" &&
      "$CONTROLLER_PLATFORM" == "Darwin" ]]; then
  CONTROLLER_STATUS="error"
  CONTROLLER_SERVICE_STATE="unavailable"
  CONTROLLER_RESULT="$("$PYTHON_BIN" -I -S - \
      "$HOME" "$PRODUCT_ROOT" "$PROJECT" "$CONTROLLER_LAUNCHCTL" <<'PY'
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

home = Path(sys.argv[1]).resolve()
product = Path(sys.argv[2]).resolve()
project = sys.argv[3]
launchctl = Path(sys.argv[4])
label = f"com.factory.controller.{project}"
expected_program = str(home / ".factory/bin/factory-launch")
expected_arguments = [expected_program, project, "reconcile", "--json"]
expected_job = {
    "Label": label,
    "ProcessType": "Interactive",
    "ProgramArguments": expected_arguments,
    "RunAtLoad": True,
    "StandardErrorPath": str(
        home / f".factory/logs/{project}-controller.error.log"
    ),
    "StandardOutPath": str(
        home / f".factory/logs/{project}-controller.log"
    ),
    "StartInterval": 15,
    "WatchPaths": [str(product / "factory/runs")],
}


def line(state, last=None):
    print(state)
    print("" if last is None else last)


def regular(path, maximum):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or not 0 < before.st_size <= maximum
        ):
            raise ValueError
        raw = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError
        return raw
    finally:
        os.close(descriptor)


def native(*arguments):
    result = subprocess.run(
        [
            str(launchctl), "asuser", str(os.getuid()), str(launchctl),
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    if (
        result.returncode != 0
        or len(result.stdout) > 65_536
        or len(result.stderr) > 65_536
        or b"\0" in result.stdout
        or b"\0" in result.stderr
    ):
        raise ValueError
    return result.stdout.decode("utf-8")


try:
    if launchctl != Path("/bin/launchctl"):
        if os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") != "1":
            raise ValueError
        info = launchctl.lstat()
        if (
            not launchctl.is_absolute()
            or launchctl.resolve() != launchctl
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
            or not os.access(launchctl, os.X_OK)
        ):
            raise ValueError
    job_path = home / "Library/LaunchAgents" / f"{label}.plist"
    try:
        job = plistlib.loads(regular(job_path, 65_536))
    except FileNotFoundError:
        line("unavailable")
        raise SystemExit(0)
    if job != expected_job:
        line("route_mismatch")
        raise SystemExit(0)
    disabled = {}
    opened = False
    closed = False
    for item in native("print-disabled", f"gui/{os.getuid()}").splitlines():
        if not item.strip():
            continue
        if not opened:
            if not re.fullmatch(r"\s*disabled services\s*=\s*\{\s*", item):
                raise ValueError
            opened = True
            continue
        if re.fullmatch(r"\s*\}\s*", item):
            if closed:
                raise ValueError
            closed = True
            continue
        if closed:
            raise ValueError
        match = re.fullmatch(
            r'\s*"([^"\r\n]+)"\s*=>\s*(enabled|disabled|true|false)\s*',
            item,
        )
        if match is None or match.group(1) in disabled:
            raise ValueError
        disabled[match.group(1)] = match.group(2)
    if not opened or not closed:
        raise ValueError
    if disabled.get(label) in {"disabled", "true"}:
        line("disabled")
        raise SystemExit(0)
    output = native("list", label)
    strings = {}
    integers = {}
    arguments = None
    argument_values = []
    in_arguments = False
    for item in output.splitlines():
        if in_arguments:
            if re.fullmatch(r"\s*\);\s*", item):
                arguments = argument_values
                in_arguments = False
                continue
            match = re.fullmatch(r'\s*"([^"\r\n]*)";\s*', item)
            if match is None:
                raise ValueError
            argument_values.append(match.group(1))
            continue
        match = re.fullmatch(r'\s*"ProgramArguments"\s*=\s*\(\s*', item)
        if match:
            if arguments is not None or argument_values:
                raise ValueError
            in_arguments = True
            continue
        match = re.fullmatch(
            r'\s*"(Label|Program)"\s*=\s*"([^"\r\n]*)";\s*', item
        )
        if match:
            if match.group(1) in strings:
                raise ValueError
            strings[match.group(1)] = match.group(2)
            continue
        match = re.fullmatch(
            r'\s*"(PID|LastExitStatus)"\s*=\s*(-?[0-9]+);\s*', item
        )
        if match:
            if match.group(1) in integers:
                raise ValueError
            value = int(match.group(2))
            if not -(2**31) <= value < 2**31:
                raise ValueError
            integers[match.group(1)] = value
    if in_arguments:
        raise ValueError
    if (
        strings.get("Label") != label
        or strings.get("Program") != expected_program
        or arguments != expected_arguments
    ):
        line("route_mismatch")
    elif "PID" in integers:
        if integers["PID"] <= 0:
            raise ValueError
        line("running", integers.get("LastExitStatus"))
    elif integers.get("LastExitStatus") == 0:
        line("idle_clean", 0)
    elif "LastExitStatus" in integers:
        line("last_exit_nonzero", integers["LastExitStatus"])
    else:
        line("unavailable")
except (
    OSError, RuntimeError, UnicodeError, ValueError, plistlib.InvalidFileException,
    subprocess.SubprocessError,
):
    raise SystemExit(1)
PY
  )" || CONTROLLER_RESULT=""
  if [[ -n "$CONTROLLER_RESULT" ]]; then
    CONTROLLER_SERVICE_STATE="$(printf '%s\n' "$CONTROLLER_RESULT" | sed -n '1p')"
    CONTROLLER_LAST_EXIT_STATUS="$(printf '%s\n' "$CONTROLLER_RESULT" | sed -n '2p')"
  fi
  case "$CONTROLLER_SERVICE_STATE" in
    running|idle_clean) CONTROLLER_STATUS="ok" ;;
    disabled|unavailable|route_mismatch|last_exit_nonzero) CONTROLLER_STATUS="error" ;;
    *)
      CONTROLLER_STATUS="error"
      CONTROLLER_SERVICE_STATE="unavailable"
      CONTROLLER_LAST_EXIT_STATUS=""
      ;;
  esac
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

PROVIDER_CLI_PIN_STATUS="not_applicable"
PROVIDER_CLI_PIN_JSON="null"
if [[ "${FACTORY_KIT_TRUST_SCOPE:-}" == "production-certified" ]]; then
  # Legacy releases without the pin helper warn. Modern releases delegate to
  # Factory-kit so the receipt-selected sealed authority can check this release.
  PROVIDER_CLI_PIN_STATUS="warning"
  if [[ "${FACTORY_RELEASE_TREE:-}" =~ ^[0-9a-f]{40}$ &&
        -f "$KIT_DIR/scripts/owner-provider-cli-pin.py" &&
        ! -L "$KIT_DIR/scripts/owner-provider-cli-pin.py" ]]; then
    PROVIDER_CLI_PIN_STATUS="error"
    PROVIDER_CLI_PIN_RAW="$(bash "$KIT_DIR/scripts/factory-kit.sh" \
      provider-cli-pin check --sha "$KIT_SHA" 2>/dev/null || true)"
    if PROVIDER_CLI_PIN_FIELDS="$(printf '%s' "$PROVIDER_CLI_PIN_RAW" | \
        "$PYTHON_BIN" -c '
import json, sys
value = json.load(sys.stdin)
assert value.get("schema") == "nysa.software-factory.provider-cli-pin-status/v1"
assert value.get("status") in {"ready", "unready"}
items = value.get("items")
assert isinstance(items, list) and {item.get("name") for item in items} == {"claude", "codex", "agent"}
assert all(item.get("status") in {"ok", "warning", "error"} for item in items)
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
print("warning" if items and all(item["status"] == "warning" for item in items) else
      ("ok" if value["status"] == "ready" else "error"))
' 2>/dev/null)"; then
      PROVIDER_CLI_PIN_JSON="$(printf '%s\n' "$PROVIDER_CLI_PIN_FIELDS" | sed -n '1p')"
      PROVIDER_CLI_PIN_STATUS="$(printf '%s\n' "$PROVIDER_CLI_PIN_FIELDS" | sed -n '2p')"
    fi
  fi
fi

FALLBACK_READINESS_STATUS="not_applicable"
FALLBACK_READINESS_JSON="null"
if [[ "${FACTORY_KIT_TRUST_SCOPE:-}" == "qualification-candidate" ]]; then
  FALLBACK_READINESS_STATUS="error"
  FALLBACK_READINESS_FILE="$TMP/fallback-readiness.raw"
  if bounded_command "$READINESS_TIMEOUT_SECONDS" "$FALLBACK_READINESS_FILE" \
      /bin/bash "$KIT_DIR/scripts/model-control.sh" qualification-readiness; then
    FALLBACK_READINESS_EXIT=0
  else
    FALLBACK_READINESS_EXIT=$?
  fi
  FALLBACK_READINESS_RAW="$(cat "$FALLBACK_READINESS_FILE" 2>/dev/null || true)"
  if FALLBACK_READINESS_PARSED="$($PYTHON_BIN - "$FALLBACK_READINESS_RAW" 2>/dev/null <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value.get("schema") == "nysa.software-factory.qualification-fallback-readiness/v1"
assert value.get("status") in {"ready", "invalid"}
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
)"; then
    FALLBACK_READINESS_JSON="$FALLBACK_READINESS_PARSED"
    [[ "$FALLBACK_READINESS_EXIT" -eq 0 ]] && FALLBACK_READINESS_STATUS="ok"
  fi
fi

MODEL_READINESS_STATUS="not_applicable"
MODEL_READINESS_JSON="null"
if [[ "${FACTORY_KIT_TRUST_SCOPE:-}" == "repository-test" ]]; then
  MODEL_READINESS_STATUS="error"
  if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
        "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == "1" &&
        "${FACTORY_ADAPTER_OVERRIDE:-}" == "mock" ]]; then
    MODEL_READINESS_STATUS="ok"
    MODEL_READINESS_JSON='{"adapter":"mock","schema":"nysa.software-factory.doctor-repository-test-readiness/v1","status":"ready","trust_scope":"repository-test"}'
  fi
elif [[ "${FACTORY_KIT_TRUST_SCOPE:-}" == "production-certified" &&
      "${FACTORY_MODEL_STATE_ROOT:-}" == /* &&
      -d "${FACTORY_MODEL_STATE_ROOT:-}" ]]; then
  MODEL_READINESS_STATUS="error"
  MODEL_READINESS_FILE="$TMP/model-readiness.raw"
  if bounded_command "$READINESS_TIMEOUT_SECONDS" "$MODEL_READINESS_FILE" \
      /bin/bash "$KIT_DIR/scripts/model-control.sh" plan; then
    MODEL_READINESS_EXIT=0
  else
    MODEL_READINESS_EXIT=$?
  fi
  MODEL_READINESS_RAW="$(cat "$MODEL_READINESS_FILE" 2>/dev/null || true)"
  if MODEL_READINESS_PARSED="$($PYTHON_BIN - "$MODEL_READINESS_RAW" \
      "$MODEL_READINESS_EXIT" 2>/dev/null <<'PY'
import json
import re
import sys

safe_id = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
safe_text = re.compile(r"[^\x00-\x1f\x7f]{0,500}\Z")
digest = re.compile(r"[0-9a-f]{64}\Z")
fields = {"adapter_version", "reason", "reported_identity", "state"}
states = {"READY", "UNAVAILABLE", "INVALID", "UNKNOWN"}
roles = {"planner", "builder", "narrator", "spec-linter", "test-author", "reviewer"}


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


value = json.loads(sys.argv[1], object_pairs_hook=no_duplicates)
exit_code = int(sys.argv[2])
if not isinstance(value, dict):
    raise ValueError
if exit_code == 0:
    if (
        value.get("schema") not in {
            "model-resolution-plan/v1", "model-resolution-plan/v2",
        }
        or not isinstance(value.get("profile_id"), str)
        or not safe_id.fullmatch(value["profile_id"])
        or not isinstance(value.get("portfolio_id"), str)
        or not safe_id.fullmatch(value["portfolio_id"])
        or not isinstance(value.get("profile_hash"), str)
        or not digest.fullmatch(value["profile_hash"])
        or not isinstance(value.get("selections"), dict)
        or set(value["selections"]) != roles
    ):
        raise ValueError
    report = {
        "portfolio_id": value["portfolio_id"],
        "profile_hash": value["profile_hash"],
        "profile_id": value["profile_id"],
        "schema": "nysa.software-factory.doctor-model-readiness/v1",
        "status": "ready",
    }
elif exit_code == 2:
    if set(value) != {
        "error", "profile_id", "readiness", "reason_code", "schema", "status",
    }:
        raise ValueError
    reason = value.get("reason_code")
    readiness = value.get("readiness")
    if (
        value.get("schema")
        != "nysa.software-factory.model-resolution-error/v1"
        or value.get("status") != "error"
        or not isinstance(reason, str)
        or not safe_id.fullmatch(reason)
        or not isinstance(value.get("profile_id"), str)
        or not safe_id.fullmatch(value["profile_id"])
        or value.get("error") != f"model plan failed: {reason}"
        or not isinstance(readiness, dict)
        or len(readiness) > 64
        or reason in {
            "profile_resolution_failed", "profile_temporarily_unavailable",
        } and not readiness
    ):
        raise ValueError
    for route_id, evidence in readiness.items():
        if (
            not isinstance(route_id, str)
            or not safe_id.fullmatch(route_id)
            or not isinstance(evidence, dict)
            or set(evidence) != fields
            or evidence.get("state") not in states
            or not isinstance(evidence.get("reason"), str)
            or not safe_id.fullmatch(evidence["reason"])
        ):
            raise ValueError
        for name in ("adapter_version", "reported_identity"):
            text = evidence.get(name)
            if (
                not isinstance(text, str)
                or not safe_text.fullmatch(text)
                or re.search(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://", text)
                or re.search(
                    r"(?i)[A-Za-z0-9_.-]*"
                    r"(?:key|token|secret|password|url|dsn|conn|auth)"
                    r"[A-Za-z0-9_.-]*\s*[:=]", text,
                )
            ):
                raise ValueError
    report = value
else:
    raise ValueError
print(json.dumps(report, sort_keys=True, separators=(",", ":")))
PY
)"; then
    MODEL_READINESS_JSON="$MODEL_READINESS_PARSED"
    if [[ "$MODEL_READINESS_EXIT" -eq 0 ]]; then
      MODEL_READINESS_STATUS="ok"
    fi
  fi
fi

GH_AUTH_READY="false"
CREDENTIAL_STATUS="warning"
GH_PATH="$(command -v gh 2>/dev/null || true)"
if [[ -n "$GH_PATH" ]] && "$PYTHON_BIN" - "$PROBE_TIMEOUT_SECONDS" "$GH_PATH" <<'PY'
import os
import subprocess
import sys

environment = os.environ.copy()
environment["GH_PROMPT_DISABLED"] = "1"
try:
    result = subprocess.run(
        [sys.argv[2], "auth", "status", "--active", "--hostname", "github.com"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        timeout=int(sys.argv[1]),
        check=False,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
raise SystemExit(result.returncode)
PY
then
  GH_AUTH_READY="true"
  CREDENTIAL_STATUS="ok"
fi

PROVIDER_RUNTIME_STATUS="ok"
PROVIDER_ACTIVATED=false
PROVIDER_EXECUTION_MODE=""
PROVIDER_ACTIVE_ATTEMPTS=0
PROVIDER_ACTIVE_TOKENS=0
PROVIDER_UNKNOWN_WORKERS=0
PROVIDER_LEGACY_INTERVALS=0
PROVIDER_CONCURRENCY_REQUIRED=false
PROVIDER_CONCURRENCY_READY=false
if [[ ( "$CONTRACT_VERSION" == "1.6.0" || "$CONTRACT_VERSION" == "1.7.0" ||
        "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) &&
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
if [[ ( "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) &&
      "${FACTORY_KIT_TRUST_SCOPE:-}" != "repository-test" &&
      "$MAX_CONCURRENT_TICKETS" =~ ^[0-9]+$ &&
      "$MAX_CONCURRENT_TICKETS" -gt 1 ]]; then
  PROVIDER_CONCURRENCY_REQUIRED=true
  PROVIDER_CONCURRENCY_ARGS=(
    --release "$KIT_DIR"
    --root "$(dirname "${FACTORY_PROVIDER_POLICY:-}")"
    --capacity "$MAX_CONCURRENT_TICKETS"
    check
    --activation "${FACTORY_PROVIDER_ACTIVATION:-}"
  )
  [[ -z "${FACTORY_CLI_RUNTIME_ROOT:-}" ]] ||
    PROVIDER_CONCURRENCY_ARGS+=(--cli-root "$FACTORY_CLI_RUNTIME_ROOT")
  if [[ -n "${FACTORY_PROVIDER_POLICY:-}" &&
        "${FACTORY_PROVIDER_POLICY:-}" == */provider-policy.json ]] &&
     "$PYTHON_BIN" -I -S "$KIT_DIR/scripts/provider-concurrency-config.py" \
       "${PROVIDER_CONCURRENCY_ARGS[@]}" >/dev/null 2>&1; then
    PROVIDER_CONCURRENCY_READY=true
  else
    PROVIDER_RUNTIME_STATUS="error"
  fi
fi

OVERALL_STATUS="ok"
for check_status in "$BINDING_STATUS" "$KIT_STATUS" "$PIN_STATUS" "$RUNTIME_STATUS" \
                    "$CLI_STATUS" "$CREDENTIAL_STATUS" \
                    "$PROVIDER_RUNTIME_STATUS" "$CONTRACT_RESUME_STATUS" \
                    "$TRANSITION_RECEIPT_STATUS" "$CONTROLLER_STATUS" \
                    "$FALLBACK_READINESS_STATUS" "$MODEL_READINESS_STATUS" \
                    "$PROVIDER_CLI_PIN_STATUS"; do
  if [[ "$check_status" == "error" ]]; then
    OVERALL_STATUS="error"
    break
  fi
  if [[ "$check_status" == "warning" || "$check_status" == "unknown" ]]; then
    OVERALL_STATUS="warning"
  fi
done

OUTPUT_KIT_DIR="$(printf '%s' "$KIT_DIR" | sanitize)"
OUTPUT_PRODUCT_ROOT="$(printf '%s' "$PRODUCT_ROOT" | sanitize)"
OUTPUT_PIN_FILE="$(printf '%s' "$PIN_FILE" | sanitize)"
OUTPUT_FACTORY_DIR="$(printf '%s' "$FACTORY_DIR" | sanitize)"

export CONTRACT_VERSION DOCTOR_SCHEMA PROJECT BINDING_STATUS
export OUTPUT_KIT_DIR OUTPUT_PRODUCT_ROOT
export KIT_STATUS KIT_SHA PIN_STATUS OUTPUT_PIN_FILE PIN_SHA PIN_VALID PIN_MATCHES
export RUNTIME_STATUS OUTPUT_FACTORY_DIR MAINTENANCE LAUNCH_LOCK LEDGER_LOCK GLOBAL_LEDGER_LOCK
export PROVIDER_LOCK PROVIDER_LOCK_STATE
export ACTIVE_RECORDS ACTIVE_RUNS STALE_RUNS MALFORMED_RUNS
export ACTIVE_RUN_CLAIMS MALFORMED_ACTIVE_RUN_CLAIMS ACTIVE_CLAIM_FILE
export MAX_CONCURRENT_TICKETS DISPATCH_LEASES STALE_DISPATCH_LEASES MALFORMED_DISPATCH_LEASES LEASE_FILE
export CLI_STATUS CLI_FILE
export CREDENTIAL_STATUS GH_AUTH_READY
export PROVIDER_RUNTIME_STATUS PROVIDER_ACTIVATED PROVIDER_ACTIVE_ATTEMPTS
export PROVIDER_EXECUTION_MODE
export PROVIDER_ACTIVE_TOKENS PROVIDER_UNKNOWN_WORKERS PROVIDER_LEGACY_INTERVALS
export PROVIDER_CONCURRENCY_REQUIRED PROVIDER_CONCURRENCY_READY
export CONTRACT_RESUME_STATUS CONTRACT_RESUME_FILE OVERALL_STATUS RUN_FILE
export TRANSITION_RECEIPT_STATUS TRANSITION_RECEIPT_FILE
export CONTROLLER_STATUS CONTROLLER_SERVICE_STATE CONTROLLER_LAST_EXIT_STATUS
export FALLBACK_READINESS_STATUS FALLBACK_READINESS_JSON
export MODEL_READINESS_STATUS MODEL_READINESS_JSON
export PROVIDER_CLI_PIN_STATUS PROVIDER_CLI_PIN_JSON

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

active_run_tickets = []
with open(os.environ["ACTIVE_CLAIM_FILE"], encoding="utf-8") as handle:
    for line in handle:
        ticket, state = line.rstrip("\n").split("\t", 1)
        if state == "active":
            active_run_tickets.append(ticket)

leases = []
with open(os.environ["LEASE_FILE"], encoding="utf-8") as handle:
    for line in handle:
        ticket, state = line.rstrip("\n").split("\t", 1)
        leases.append({"ticket": ticket, "state": state})

with open(os.environ["CONTRACT_RESUME_FILE"], encoding="utf-8") as handle:
    contract_resume_incidents = json.load(handle)
with open(os.environ["TRANSITION_RECEIPT_FILE"], encoding="utf-8") as handle:
    transition_receipt_incidents = json.load(handle)

document = {
    "schema": os.environ["DOCTOR_SCHEMA"],
    "schema_version": 2,
    "contract_version": os.environ["CONTRACT_VERSION"],
    "overall_status": os.environ["OVERALL_STATUS"],
    "project": os.environ["PROJECT"],
    "checks": {
        "active_binding": {
            "status": os.environ["BINDING_STATUS"],
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
                "global_ledger": boolean("GLOBAL_LEDGER_LOCK"),
                "provider": boolean("PROVIDER_LOCK"),
            },
            "provider_lock_state": os.environ["PROVIDER_LOCK_STATE"],
            "run_records": number("ACTIVE_RECORDS"),
            "active_runs": number("ACTIVE_RUNS"),
            "stale_runs": number("STALE_RUNS"),
            "malformed_runs": number("MALFORMED_RUNS"),
            "runs": runs,
            "active_run_claims": number("ACTIVE_RUN_CLAIMS"),
            "malformed_active_run_claims": number("MALFORMED_ACTIVE_RUN_CLAIMS"),
            "active_run_tickets": active_run_tickets,
            "max_concurrent_tickets": number("MAX_CONCURRENT_TICKETS"),
            "dispatch_lease_records": number("DISPATCH_LEASES"),
            "stale_dispatch_leases": number("STALE_DISPATCH_LEASES"),
            "malformed_dispatch_leases": number("MALFORMED_DISPATCH_LEASES"),
            "dispatch_leases": leases,
        },
        "clis": {
            "status": os.environ["CLI_STATUS"],
            "items": clis,
        },
        "provider_cli_pins": {
            "status": os.environ["PROVIDER_CLI_PIN_STATUS"],
            "report": json.loads(os.environ["PROVIDER_CLI_PIN_JSON"]),
        },
        "fallback_readiness": {
            "status": os.environ["FALLBACK_READINESS_STATUS"],
            "report": json.loads(os.environ["FALLBACK_READINESS_JSON"]),
        },
        "model_readiness": {
            "status": os.environ["MODEL_READINESS_STATUS"],
            "report": json.loads(os.environ["MODEL_READINESS_JSON"]),
        },
        "credentials": {
            "status": os.environ["CREDENTIAL_STATUS"],
            "validated_authentication": boolean("GH_AUTH_READY"),
        },
        "contract_resume": {
            "status": os.environ["CONTRACT_RESUME_STATUS"],
            "incidents": contract_resume_incidents,
        },
        "transition_receipts": {
            "status": os.environ["TRANSITION_RECEIPT_STATUS"],
            "incidents": transition_receipt_incidents,
        },
        "controller": {
            "status": os.environ["CONTROLLER_STATUS"],
            "state": os.environ["CONTROLLER_SERVICE_STATE"],
            "last_exit_status": number("CONTROLLER_LAST_EXIT_STATUS"),
        },
        "isolated_provider": {
            "status": os.environ["PROVIDER_RUNTIME_STATUS"],
            "activated": boolean("PROVIDER_ACTIVATED"),
            "concurrency_required": boolean("PROVIDER_CONCURRENCY_REQUIRED"),
            "concurrency_ready": boolean("PROVIDER_CONCURRENCY_READY"),
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
  echo "Active binding [$BINDING_STATUS]: kit=$OUTPUT_KIT_DIR product=$OUTPUT_PRODUCT_ROOT"
  echo "Kit [$KIT_STATUS]: ${KIT_SHA:-unavailable}"
  echo "KIT_PIN [$PIN_STATUS]: ${PIN_SHA:-missing or invalid}"
  echo "Runtime [$RUNTIME_STATUS]: maintenance=$MAINTENANCE active=$ACTIVE_RUNS claims=$ACTIVE_RUN_CLAIMS stale=$STALE_RUNS malformed=$MALFORMED_RUNS concurrency=$MAX_CONCURRENT_TICKETS leases=$DISPATCH_LEASES"
  echo "Locks: launch=$LAUNCH_LOCK ledger=$LEDGER_LOCK global_ledger=$GLOBAL_LEDGER_LOCK provider=$PROVIDER_LOCK provider_state=$PROVIDER_LOCK_STATE"
  while IFS="$(printf '\t')" read -r cli_name cli_item_status cli_path cli_version; do
    echo "CLI $cli_name [$cli_item_status]: ${cli_version:-unavailable} (${cli_path:-not found})"
  done < "$CLI_FILE"
  echo "Provider CLI pins [$PROVIDER_CLI_PIN_STATUS]"
  echo "Credentials [$CREDENTIAL_STATUS]: github_authenticated=$GH_AUTH_READY"
  echo "Isolated provider [$PROVIDER_RUNTIME_STATUS]: activated=$PROVIDER_ACTIVATED concurrency_required=$PROVIDER_CONCURRENCY_REQUIRED concurrency_ready=$PROVIDER_CONCURRENCY_READY mode=${PROVIDER_EXECUTION_MODE:-none} attempts=$PROVIDER_ACTIVE_ATTEMPTS tokens=$PROVIDER_ACTIVE_TOKENS unknown_workers=$PROVIDER_UNKNOWN_WORKERS legacy=$PROVIDER_LEGACY_INTERVALS"
  echo "Contract resume [$CONTRACT_RESUME_STATUS]: incidents=$("$PYTHON_BIN" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$CONTRACT_RESUME_FILE")"
  echo "Transition receipts [$TRANSITION_RECEIPT_STATUS]: incidents=$("$PYTHON_BIN" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$TRANSITION_RECEIPT_FILE")"
  echo "Controller [$CONTROLLER_STATUS]: state=$CONTROLLER_SERVICE_STATE last_exit=${CONTROLLER_LAST_EXIT_STATUS:-none}"
  echo "Model readiness [$MODEL_READINESS_STATUS]"
fi

[[ "$OVERALL_STATUS" == "error" ]] && exit 1
exit 0

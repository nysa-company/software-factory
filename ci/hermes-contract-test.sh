#!/usr/bin/env bash
# Sandboxed contract tests for the public Hermes integration boundary.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCTOR="$ROOT/scripts/factory-doctor.sh"
CONTRACT="$ROOT/integrations/hermes/contract.json"
LAUNCHER="$ROOT/integrations/hermes/bin/factory-launch"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/hermes-contract-test.XXXXXX")"
TEST_HOME="$TMP/home"
PROFILE="$TEST_HOME/.hermes/profiles/factory"
PRODUCT="$TMP/product"
LAUNCH_PRODUCT="$TMP/launch-product"
KITS_ROOT="$TEST_HOME/.factory/kits"
STUB_BIN="$TMP/bin"
JSON_OUT="$TMP/doctor.json"
HUMAN_OUT="$TMP/doctor.txt"
BACKGROUND_PIDS=""

cleanup() {
  local pid
  if [[ -d "$LAUNCH_PRODUCT/factory" ]]; then
    touch "$LAUNCH_PRODUCT/factory/test-adapter-gate" 2>/dev/null || true
  fi
  for pid in $BACKGROUND_PIDS; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in $BACKGROUND_PIDS; do
    wait "$pid" 2>/dev/null || true
  done
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

if [[ "${FACTORY_KIT_OUTER_SANDBOX:-0}" == "1" ]]; then
  # The launcher deliberately replaces PATH with its fixed production value.
  # Preserve only the tools selected by the enclosing release sandbox so
  # contract tests do not fall back to macOS xcrun shims or system ps.
  mkdir -p "$TEST_HOME/.factory/bin"
  for tool in git python3 ps; do
    tool_path="$(command -v "$tool")"
    [[ "$tool_path" == /* && -x "$tool_path" ]] ||
      fail "outer sandbox did not provide $tool"
    ln -s "$tool_path" "$TEST_HOME/.factory/bin/$tool"
  done
fi

assert_release_metadata() {
  local file="$1" sha="$2" tree="$3" release="$4" physical contract
  physical="$(cd "$release" && pwd -P)"
  contract="$(python3 - "$release/integrations/hermes/contract.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["contract_version"])
PY
)"
  grep -qF "FACTORY_RELEASE_SHA=$sha" "$file" || fail "helper did not receive release SHA"
  grep -qF "FACTORY_RELEASE_TREE=$tree" "$file" || fail "helper did not receive release tree"
  grep -qF "FACTORY_RELEASE_PATH=$physical" "$file" ||
    fail "helper did not receive physical release path"
  grep -qF "FACTORY_RELEASE_CONTRACT_VERSION=$contract" "$file" ||
    fail "helper did not receive release contract"
}

assert_helper_confinement() {
  local file="$1" credential_expectation="${2:-present}" \
    origin_expectation="${3:-absent}" safe_tmp safe_home expected_cksum
  safe_tmp="$(cd "$TMP/launcher-tmp" && pwd -P)"
  safe_home="$(cd "$TEST_HOME" && pwd -P)"
  grep -qF "HOME=$safe_home" "$file" || fail "helper HOME was not explicitly passed"
  grep -qF "PATH=$safe_home/.factory/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    "$file" || fail "helper PATH was not the fixed safe path"
  grep -qF "TMPDIR=$safe_tmp" "$file" || fail "helper TMPDIR was not physical"
  grep -qFx "FACTORY_TEST_MODE=1" "$file" ||
    fail "isolated launcher did not fix test mode"
  grep -qFx "FACTORY_ADAPTER_OVERRIDE=mock" "$file" ||
    fail "isolated launcher did not fix the mock adapter"
  grep -qFx "FACTORY_TRUSTED_TEST_HARNESS=1" "$file" ||
    fail "isolated launcher did not authenticate its test harness"
  grep -qF "FACTORY_ROOT=$(cd "$LAUNCH_PRODUCT" && pwd -P)" "$file" ||
    fail "helper FACTORY_ROOT was not canonical"
  grep -qF "FACTORY_MODEL_STATE_ROOT=$(cd "$KITS_ROOT/projects" && pwd -P)" "$file" ||
    fail "helper model state root was not canonical"
  grep -qFx "FACTORY_PROJECT=launchtest" "$file" ||
    fail "helper project context was not explicit"
  if grep -Fq "$GH_SECRET" "$file" || grep -Fq "$CALLER_GH_SECRET" "$file"; then
    fail "helper environment snapshot stored a credential value"
  fi
  if [[ "$credential_expectation" == "present" ]]; then
    expected_cksum="$(printf '%s' "$GH_SECRET" | cksum)"
    grep -qF "GH_TOKEN_PRESENT=true" "$file" ||
      fail "profile GH_TOKEN did not reach selected helper"
    grep -qF "GH_TOKEN_CKSUM=$expected_cksum" "$file" ||
      fail "selected helper did not receive the profile-derived GH_TOKEN"
  else
    if grep -q '^GH_TOKEN_' "$file"; then
      fail "caller GH_TOKEN reached helper without a profile credential"
    fi
  fi
  if [[ "$origin_expectation" == "present" ]]; then
    grep -qFx "FACTORY_CERTIFIED_PRODUCT_ORIGIN=$LAUNCH_PRODUCT_REMOTE" "$file" ||
      fail "pin helper did not receive the receipt-bound certified origin"
  fi
  local variable
  for variable in \
    FACTORY_LAUNCH_TEST_MODE FACTORY_LAUNCH_TEST_HOME \
    FACTORY_LAUNCH_TEST_ACCOUNT_HOME FACTORY_KITS_ROOT HERMES_FACTORY_PROFILE \
    FACTORY_ENVELOPE FACTORY_LEDGER FACTORY_GLOBAL_ENV \
    FACTORY_MODEL_MANAGER FACTORY_MODEL_CATALOG FACTORY_MODEL_PROFILES \
    FACTORY_DISPATCH_LEASE_ID \
    FACTORY_PROBE_CODEX FACTORY_PROBE_CLAUDE_CODE \
    FACTORY_CURSOR_FALLBACK_ENABLED CURSOR_AGENT_BIN CODEX_PINNED MOCK_STATUS \
    PROJECTED_TICKET_USD PYTHONHOME PYTHONPATH PYTHONWARNINGS GIT_DIR GIT_WORK_TREE \
    GIT_INDEX_FILE GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_COUNT \
    GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 \
    BASH_ENV ENV; do
    if grep -q "^$variable=" "$file"; then
      fail "caller control propagated to helper: $variable"
    fi
  done
  if [[ "$origin_expectation" != "present" ]] &&
     grep -q "^FACTORY_CERTIFIED_PRODUCT_ORIGIN=" "$file"; then
    fail "certified product origin reached a helper that does not perform trusted writes"
  fi
}

tree_for_directory() {
  local directory="$1" object_dir index tree
  object_dir="$(mktemp -d "$TMP/tree.XXXXXX")"
  index="$object_dir/index"
  git init --bare -q "$object_dir/repo.git"
  git --git-dir="$object_dir/repo.git" config core.bare false
  GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" read-tree --empty
  GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" add -f -A -- .
  tree="$(GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" write-tree)"
  rm -rf "$object_dir"
  printf '%s\n' "$tree"
}

write_active() {
  local sha="$1" tree="$2" release="$3" product="${4:-$LAUNCH_PRODUCT}"
  local active="$KITS_ROOT/projects/launchtest/active.json" temporary contract
  local origin="" product_tree="" receipt_id=""
  release="$(cd "$release" && pwd -P)"
  product="$(cd "$product" && pwd -P)"
  contract="$(python3 - "$release/integrations/hermes/contract.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["contract_version"])
PY
)"
  if [[ "$contract" == "1.2.0" || "$contract" == "1.3.0" ||
        "$contract" == "1.4.0" ]]; then
    origin="$(git -C "$product" remote get-url --push origin)"
    product_tree="$(git -C "$product" rev-parse 'HEAD^{tree}')"
    receipt_id="$(printf '%s' "$sha|$tree|$product|$origin" | shasum -a 256 | awk '{print $1}')"
    mkdir -p "$KITS_ROOT/receipts"
    python3 - "$KITS_ROOT/receipts/$receipt_id.json" "$receipt_id" "$sha" \
      "$tree" "$product" "$product_tree" "$origin" "$contract" <<'PY'
import json, sys
path, receipt_id, sha, tree, product, product_tree, origin, contract = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "receipt_id": receipt_id,
        "status": "pass",
        "project": "launchtest",
        "kit_sha": sha,
        "kit_tree": tree,
        "product_path": product,
        "product_origin": origin,
        "product_tree": product_tree,
        "contract_version": contract,
    }, handle)
    handle.write("\n")
PY
    chmod 600 "$KITS_ROOT/receipts/$receipt_id.json"
  fi
  temporary="$active.tmp"
  python3 - "$temporary" "$sha" "$tree" "$release" "$product" \
    "$contract" "$receipt_id" "$product_tree" <<'PY'
import json
import sys

path, sha, tree, release, product, contract, receipt_id, product_tree = sys.argv[1:]
value = {
    "generation": 1,
    "project": "launchtest",
    "kit_sha": sha,
    "kit_tree": tree,
    "contract_version": contract,
    "product_path": product,
    "release_path": release,
}
if receipt_id:
    value["receipt_id"] = receipt_id
    value["product_tree"] = product_tree
with open(path, "w", encoding="utf-8") as handle:
    json.dump(value, handle)
    handle.write("\n")
PY
  mv "$temporary" "$active"
}

run_launcher() {
  local kits profile launcher_tmp
  kits="${LAUNCHER_KITS_ROOT_OVERRIDE:-$(cd "$KITS_ROOT" && pwd -P)}"
  profile="${LAUNCHER_PROFILE_OVERRIDE:-$(cd "$PROFILE" && pwd -P)}"
  launcher_tmp="$TMP/launcher-tmp"
  mkdir -p "$launcher_tmp"
  HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$launcher_tmp" \
    FACTORY_LAUNCH_TEST_MODE=1 FACTORY_LAUNCH_TEST_HOME="$TEST_HOME" \
    FACTORY_KITS_ROOT="$kits" HERMES_FACTORY_PROFILE="$profile" \
    FACTORY_ENVELOPE="$TMP/bypass-envelope.env" \
    FACTORY_LEDGER="$TMP/bypass-ledger.csv" \
    FACTORY_GLOBAL_ENV="$TMP/bypass-global.env" \
    FACTORY_TEST_MODE=caller-bypass FACTORY_ADAPTER_OVERRIDE=caller-bypass \
    FACTORY_PROBE_CODEX=INVALID:bypass \
    FACTORY_PROBE_CLAUDE_CODE=INVALID:bypass \
    FACTORY_CURSOR_FALLBACK_ENABLED=1 CURSOR_AGENT_BIN="$TMP/agent-bypass" \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$TMP/caller-origin-bypass.git" \
    FACTORY_MODEL_STATE_ROOT="$TMP/caller-model-state-bypass" \
    FACTORY_PROJECT=caller-model-project \
    FACTORY_MODEL_MANAGER="$TMP/caller-model-manager.py" \
    FACTORY_MODEL_CATALOG="$TMP/caller-model-catalog.json" \
    FACTORY_MODEL_PROFILES="$TMP/caller-model-profiles.json" \
    CODEX_PINNED=bypass MOCK_STATUS=0 PROJECTED_TICKET_USD=999999 \
    PYTHONHOME="$TMP/python-home-bypass" PYTHONPATH="$TMP/python-path-bypass" \
    PYTHONWARNINGS=error GIT_DIR="$TMP/git-dir-bypass" \
    GIT_WORK_TREE="$TMP/git-work-tree-bypass" GIT_INDEX_FILE="$TMP/git-index-bypass" \
    GIT_CONFIG_GLOBAL="$TMP/git-global-bypass" GIT_CONFIG_SYSTEM="$TMP/git-system-bypass" \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$TMP/hooks" \
    BASH_ENV=/dev/null ENV=/dev/null GH_TOKEN="$CALLER_GH_SECRET" \
    bash "$LAUNCHER" "$@"
}

create_test_release() {
  local release="$1" label="$2" action="$3" contract="${4:-1.4.0}"
  mkdir -p "$release/integrations/hermes" "$release/scripts/lib" "$release/roles"
  printf '*.out\n' > "$release/.gitignore"
  printf 'tracked ignored release evidence\n' > "$release/tracked.out"
  cp "$CONTRACT" "$release/integrations/hermes/contract.json"
  python3 - "$release/integrations/hermes/contract.json" "$contract" <<'PY'
import json, pathlib, sys
path, contract = pathlib.Path(sys.argv[1]), sys.argv[2]
value = json.loads(path.read_text())
value["contract_version"] = contract
path.write_text(json.dumps(value, indent=2) + "\n")
PY
  cp "$DOCTOR" "$release/scripts/factory-doctor-real.sh"
  python3 - "$release/scripts/factory-doctor-real.sh" "$contract" <<'PY'
import pathlib, sys
path, contract = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
old = 'CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-1.4.0}"'
new = f'CONTRACT_VERSION="${{FACTORY_RELEASE_CONTRACT_VERSION:-{contract}}}"'
if text.count(old) != 1:
    raise SystemExit("factory-doctor contract fixture is ambiguous")
path.write_text(text.replace(old, new))
PY
  cp "$ROOT/scripts/dispatch-lease.sh" "$release/scripts/dispatch-lease.sh"
  cp "$ROOT/scripts/lib/dispatch-leases.sh" "$release/scripts/lib/dispatch-leases.sh"
  cp "$ROOT/scripts/model-control.sh" "$release/scripts/model-control-real.sh"
  cp "$ROOT/scripts/model-manager.py" "$release/scripts/model-manager.py"
  cp "$ROOT/scripts/model-router.py" "$release/scripts/model-router.py"
  cp -R "$ROOT/scripts/model-routing" "$release/scripts/model-routing"
  cp "$ROOT/scripts/lib/backend-policy.sh" "$release/scripts/lib/backend-policy.sh"
  cp "$ROOT/scripts/lib/kit-pin.sh" "$release/scripts/lib/kit-pin.sh"
  cp "$ROOT/scripts/lib/plain-config.sh" "$release/scripts/lib/plain-config.sh"
  cp "$ROOT/scripts/lib/product-remote.sh" "$release/scripts/lib/product-remote.sh"
  for role in planner spec-linter test-author builder reviewer narrator; do
    printf '# %s prompt\n' "$role" > "$release/roles/$role.md"
  done
  ln -s builder.md "$release/roles/builder-link.md"
  cat > "$release/scripts/factory-doctor.sh" <<'EOF'
#!/usr/bin/env bash
ENV_OUT="$FACTORY_ROOT/factory/doctor-helper.env"
env | awk -F= '$1 != "GH_TOKEN"' | LC_ALL=C sort > "$ENV_OUT"
if [[ ${GH_TOKEN+x} == x ]]; then
  printf 'GH_TOKEN_PRESENT=true\nGH_TOKEN_CKSUM=%s\n' \
    "$(printf '%s' "$GH_TOKEN" | cksum)" >> "$ENV_OUT"
fi
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/factory-doctor-real.sh" "$@"
EOF
  cat > "$release/scripts/preflight.sh" <<EOF
#!/usr/bin/env bash
ENV_OUT="\$FACTORY_ROOT/factory/preflight-helper.env"
env | awk -F= '\$1 != "GH_TOKEN"' | LC_ALL=C sort > "\$ENV_OUT"
if [[ \${GH_TOKEN+x} == x ]]; then
  printf 'GH_TOKEN_PRESENT=true\nGH_TOKEN_CKSUM=%s\n' \
    "\$(printf '%s' "\$GH_TOKEN" | cksum)" >> "\$ENV_OUT"
fi
if [[ -e "\$FACTORY_ROOT/factory/test-preflight-block" ]]; then
  printf '%s\n' started > "\$FACTORY_ROOT/factory/test-preflight-started"
  while [[ ! -e "\$FACTORY_ROOT/factory/test-preflight-gate" ]]; do sleep 0.02; done
fi
echo "PREFLIGHT $label"
if [[ -e "\$FACTORY_ROOT/factory/test-preflight-fail" ]]; then
  printf '%s\n' \
    "Authorization: Bearer authorization-secret-value" \
    '{"api_token": "json secret value with spaces"}' \
    "password: |" \
    "  multiline-secret-one" \
    "  multiline-secret-two" \
    "failure at https://user:url-secret-value@example.invalid/path"
  exit 7
fi
if [[ -e "\$FACTORY_ROOT/factory/test-preflight-signal" ]]; then
  printf '%s\n' "Authorization: Bearer signal-secret-value"
  printf '%s\n' started > "\$FACTORY_ROOT/factory/test-preflight-signal-started"
  sleep 2
fi
exit 0
EOF
  cat > "$release/scripts/next-stage.sh" <<EOF
#!/usr/bin/env bash
ENV_OUT="\$FACTORY_ROOT/factory/next-stage-helper.env"
env | awk -F= '\$1 != "GH_TOKEN"' | LC_ALL=C sort > "\$ENV_OUT"
if [[ \${GH_TOKEN+x} == x ]]; then
  printf 'GH_TOKEN_PRESENT=true\nGH_TOKEN_CKSUM=%s\n' \
    "\$(printf '%s' "\$GH_TOKEN" | cksum)" >> "\$ENV_OUT"
fi
echo "$action"
EOF
  cat > "$release/scripts/run-agent.sh" <<EOF
#!/usr/bin/env bash
ENV_OUT="\$FACTORY_ROOT/factory/run-helper.env"
env | awk -F= '\$1 != "GH_TOKEN"' | LC_ALL=C sort > "\$ENV_OUT"
if [[ \${GH_TOKEN+x} == x ]]; then
  printf 'GH_TOKEN_PRESENT=true\nGH_TOKEN_CKSUM=%s\n' \
    "\$(printf '%s' "\$GH_TOKEN" | cksum)" >> "\$ENV_OUT"
fi
echo "RUN $label"
echo "FACTORY_ROOT=\$FACTORY_ROOT"
printf 'ARG=%s\n' "\$@"
EOF
  cat > "$release/scripts/reorder-test-fixes.sh" <<EOF
#!/usr/bin/env bash
ENV_OUT="\$FACTORY_ROOT/factory/reorder-helper.env"
env | awk -F= '\$1 != "GH_TOKEN"' | LC_ALL=C sort > "\$ENV_OUT"
if [[ \${GH_TOKEN+x} == x ]]; then
  printf 'GH_TOKEN_PRESENT=true\nGH_TOKEN_CKSUM=%s\n' \
    "\$(printf '%s' "\$GH_TOKEN" | cksum)" >> "\$ENV_OUT"
fi
echo "REORDER $label"
echo "WORKDIR=\$(pwd -P)"
printf 'ARG=%s\n' "\$@"
EOF
  cat > "$release/scripts/model-control.sh" <<'EOF'
#!/usr/bin/env bash
ENV_OUT="$FACTORY_ROOT/factory/model-helper.env"
env | awk -F= '$1 != "GH_TOKEN"' | LC_ALL=C sort > "$ENV_OUT"
if [[ ${GH_TOKEN+x} == x ]]; then
  printf 'GH_TOKEN_PRESENT=true\n' >> "$ENV_OUT"
fi
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/model-control-real.sh" "$@"
EOF
  chmod +x "$release/scripts/factory-doctor.sh" "$release/scripts/factory-doctor-real.sh" \
    "$release/scripts/preflight.sh" \
    "$release/scripts/next-stage.sh" "$release/scripts/run-agent.sh" \
    "$release/scripts/reorder-test-fixes.sh" "$release/scripts/model-control.sh" \
    "$release/scripts/model-control-real.sh" \
    "$release/scripts/dispatch-lease.sh"
}

mkdir -p "$PROFILE/projects" "$TEST_HOME/.hermes/secrets" "$TEST_HOME/.factory/.ledger.lock"
mkdir -p "$PRODUCT/factory/runs" "$PRODUCT/factory/.launch.lock" "$STUB_BIN"
printf '%s\n' 'THIS_IS_NOT_A_VALID_ENVELOPE=1' > "$TMP/bypass-envelope.env"
cat > "$TMP/bypass-global.env" <<'EOF'
GLOBAL_DAILY_CAP_USD=0
FACTORY_TEST_MODE=1
FACTORY_ADAPTER_OVERRIDE=mock
EOF
printf '%s\n' "caller bypass ledger must remain untouched" > "$TMP/bypass-ledger.csv"
BYPASS_ENVELOPE_BEFORE="$(cksum "$TMP/bypass-envelope.env")"
BYPASS_GLOBAL_BEFORE="$(cksum "$TMP/bypass-global.env")"
BYPASS_LEDGER_BEFORE="$(cksum "$TMP/bypass-ledger.csv")"

KIT_SHA="$(git -C "$ROOT" rev-parse --verify HEAD)"
printf '%s\n' "$KIT_SHA" > "$PRODUCT/factory/KIT_PIN"
touch "$PRODUCT/factory/MAINTENANCE"
printf 'pid=%s\n' "$$" > "$PRODUCT/factory/runs/run-active.pid"
PROVIDER_OWNER_TOKEN="00000000000000000000000000000000"
mkdir "$PRODUCT/factory/.provider.lock"
printf 'pid=%s\nprocess_start=%s\ntoken=%s\n' "$$" \
  "$(ps -o lstart= -p "$$" | awk '{$1=$1; print; exit}')" "$PROVIDER_OWNER_TOKEN" > \
  "$PRODUCT/factory/.provider.lock/owner"

cat > "$PROFILE/projects/relay.env" <<EOF
KIT_DIR=$ROOT
PRODUCT_ROOT=$PRODUCT
EOF

GH_SECRET="ghp_contract_test_value_never_print"
CALLER_GH_SECRET="ghp_caller_value_must_be_dropped"
LINEAR_SECRET="lin_contract_test_value_never_print"
URL_SECRET="url-password-never-print"
CLI_SECRET="cli-password-never-print"
AUTH_SECRET="doctor-authorization-never-print"
JSON_SECRET="doctor json secret with spaces"
MULTILINE_SECRET="doctor-multiline-never-print"
printf 'GH_TOKEN=%s\n' "$GH_SECRET" > "$PROFILE/.env"
printf '%s\n' "$LINEAR_SECRET" > "$TEST_HOME/.hermes/secrets/linear-api-key"
chmod 600 "$PROFILE/.env" "$TEST_HOME/.hermes/secrets/linear-api-key"
cat > "$TEST_HOME/.factory/global.env" <<'EOF'
CODEX_PINNED=0.144.1
CLAUDE_CODE_PINNED=2.1.207
FACTORY_CURSOR_FALLBACK_ENABLED=1
CURSOR_AGENT_VERSION=2026.07.test
CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
FACTORY_PROBE_CODEX=READY:test
FACTORY_PROBE_CLAUDE_CODE=READY:test
FACTORY_PROBE_CURSOR_OPENAI=READY:test
FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test
EOF
ENV_BEFORE="$(cksum "$PROFILE/.env")"
KEY_BEFORE="$(cksum "$TEST_HOME/.hermes/secrets/linear-api-key")"

python3 - "$PRODUCT/factory/linear-map.json" "$URL_SECRET" <<'PY'
import datetime as dt
import json
import sys

path, password = sys.argv[1:]
document = {
    "_sync": {
        "last_success_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "last_error": (
            f"Authorization: Bearer doctor-authorization-never-print\n"
            f'{{"api_token": "doctor json secret with spaces"}}\n'
            f"password: |\n  doctor-multiline-never-print\n"
            f"sync failed at https://user:{password}@example.invalid/path?token=also-secret"
        ),
    }
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle)
PY

cat > "$STUB_BIN/hermes" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "Hermes Agent v0.18.2 (2026.7.7.2)"
fi
STUB
cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
echo "Claude Code 2.1.207"
STUB
cat > "$STUB_BIN/codex" <<'STUB'
#!/usr/bin/env bash
echo "codex-cli 0.144.1"
STUB
cat > "$STUB_BIN/agent" <<'STUB'
#!/usr/bin/env bash
echo "Cursor Agent 2026.07.test"
STUB
cat > "$STUB_BIN/gh" <<EOF
#!/usr/bin/env bash
echo "gh version test https://cli-user:$CLI_SECRET@example.invalid/version"
EOF
chmod +x "$STUB_BIN/hermes" "$STUB_BIN/claude" "$STUB_BIN/codex" "$STUB_BIN/agent" "$STUB_BIN/gh"

HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  bash "$DOCTOR" --json --project relay > "$JSON_OUT"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  bash "$DOCTOR" --project relay > "$HUMAN_OUT"

assert_no_secret() {
  local output="$1"
  local secret
  for secret in "$GH_SECRET" "$CALLER_GH_SECRET" "$LINEAR_SECRET" "$URL_SECRET" "$CLI_SECRET" \
                "$AUTH_SECRET" "$JSON_SECRET" "$MULTILINE_SECRET" "$PROVIDER_OWNER_TOKEN" "also-secret" \
                "authorization-secret-value" "json secret value with spaces" \
                "multiline-secret-one" "multiline-secret-two" "url-secret-value" \
                "signal-secret-value"; do
    if LC_ALL=C grep -Fq "$secret" "$output"; then
      fail "doctor output leaked a seeded secret"
    fi
  done
  python3 - "$output" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
if re.search(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:]+:[^@\s]+@", text):
    raise SystemExit("credential-bearing URL remained in doctor output")
PY
}

assert_no_secret "$JSON_OUT"
assert_no_secret "$HUMAN_OUT"
[[ "$(cksum "$PROFILE/.env")" == "$ENV_BEFORE" ]] || fail "doctor changed the profile environment"
[[ "$(cksum "$TEST_HOME/.hermes/secrets/linear-api-key")" == "$KEY_BEFORE" ]] ||
  fail "doctor changed the Linear credential file"

python3 - "$JSON_OUT" "$KIT_SHA" "$ROOT" "$PRODUCT" <<'PY'
import json
import sys

path, sha, kit_dir, product_root = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)

assert data["schema"] == "nysa.software-factory.hermes-doctor/v1"
assert data["schema_version"] == 1
assert data["contract_version"] == "1.4.0"
assert data["overall_status"] == "warning"
assert data["project"] == "relay"
checks = data["checks"]
assert checks["registry"]["status"] == "ok"
assert checks["registry"]["kit_dir"] == kit_dir
assert checks["registry"]["product_root"] == product_root
assert checks["kit"] == {"status": "ok", "full_sha": sha}
assert checks["kit_pin"]["status"] == "ok"
assert checks["kit_pin"]["full_sha"] == sha
assert checks["kit_pin"]["valid_full_sha"] is True
assert checks["kit_pin"]["matches_kit"] is True
assert checks["runtime"]["status"] == "warning"
assert checks["runtime"]["maintenance"] is True
assert checks["runtime"]["locks"]["launch"] is True
assert checks["runtime"]["locks"]["global_ledger"] is True
assert checks["runtime"]["locks"]["provider"] is True
assert checks["runtime"]["provider_lock_state"] == "active"
assert checks["runtime"]["active_runs"] == 1
assert checks["runtime"]["runs"] == [{"run_id": "run-active", "state": "active"}], checks["runtime"]
assert checks["hermes"]["status"] == "ok"
assert "0.18.2" in checks["hermes"]["version"]
assert "2026.7.7.2" in checks["hermes"]["version"]
assert [item["name"] for item in checks["clis"]["items"]] == ["claude", "codex", "agent", "gh"]
assert checks["credentials"]["presence"] == {"github": True, "linear": True}
assert checks["credentials"]["validated_authentication"] is False
assert isinstance(checks["linear_sync"]["age_seconds"], int)
assert checks["linear_sync"]["status"] == "warning"
assert "[redacted]" in checks["linear_sync"]["last_error"]
allowed = {"ok", "warning", "error", "unknown"}
assert data["overall_status"] in allowed
assert all(check["status"] in allowed for check in checks.values())
PY

sed -i.bak 's/^process_start=.*/process_start=stale/' "$PRODUCT/factory/.provider.lock/owner"
rm -f "$PRODUCT/factory/.provider.lock/owner.bak"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" bash "$DOCTOR" --json --project relay > "$TMP/provider-stale.json"
python3 - "$TMP/provider-stale.json" <<'PY'
import json, sys
runtime = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]["runtime"]
assert runtime["provider_lock_state"] == "stale"
assert runtime["status"] == "warning"
PY
rm "$PRODUCT/factory/.provider.lock/owner"
ln -s "$TMP/missing-provider-owner" "$PRODUCT/factory/.provider.lock/owner"
MALFORMED_PROVIDER_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" bash "$DOCTOR" --json --project relay \
  > "$TMP/provider-malformed.json" || MALFORMED_PROVIDER_RC=$?
[[ "$MALFORMED_PROVIDER_RC" -eq 1 ]] || fail "malformed provider lock did not fail doctor"
python3 - "$TMP/provider-malformed.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["overall_status"] == "error"
assert data["checks"]["runtime"]["provider_lock_state"] == "malformed"
PY
rm -rf "$PRODUCT/factory/.provider.lock"

printf '%s\n' "0123456789abcdef0123456789abcdef0123456" > "$PRODUCT/factory/KIT_PIN"
BAD_PIN_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" bash "$DOCTOR" --json --project relay \
  > "$TMP/bad-pin.json" || BAD_PIN_RC=$?
[[ "$BAD_PIN_RC" -eq 1 ]] || fail "invalid full KIT_PIN did not return exit 1"
python3 - "$TMP/bad-pin.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["overall_status"] == "error"
assert data["checks"]["kit_pin"]["status"] == "error"
assert data["checks"]["kit_pin"]["valid_full_sha"] is False
assert data["checks"]["kit_pin"]["matches_kit"] is False
PY
assert_no_secret "$TMP/bad-pin.json"

BAD_REGISTRY_PASSWORD="registry-password-never-print"
BAD_REGISTRY_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" bash "$DOCTOR" --json --project relay \
  --registry "https://registry-user:$BAD_REGISTRY_PASSWORD@example.invalid/relay.env" \
  > "$TMP/bad-registry.json" || BAD_REGISTRY_RC=$?
[[ "$BAD_REGISTRY_RC" -eq 1 ]] || fail "invalid registry did not return exit 1"
if LC_ALL=C grep -Fq "$BAD_REGISTRY_PASSWORD" "$TMP/bad-registry.json"; then
  fail "doctor leaked a credential-bearing registry URL"
fi
python3 - "$TMP/bad-registry.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["checks"]["registry"]["status"] == "error"
assert data["checks"]["registry"]["path"] == "[redacted-url]"
PY

# Stable launcher: two physically separate releases and one mutable active record.
SHA_A="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_B="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHA_V11="dddddddddddddddddddddddddddddddddddddddd"
SHA_MODELS="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
RELEASE_A="$KITS_ROOT/releases/$SHA_A"
RELEASE_B="$KITS_ROOT/releases/$SHA_B"
RELEASE_V11="$KITS_ROOT/releases/$SHA_V11"
RELEASE_MODELS="$KITS_ROOT/releases/$SHA_MODELS"
mkdir -p "$KITS_ROOT/projects/launchtest" "$LAUNCH_PRODUCT/factory"
create_test_release "$RELEASE_A" "RELEASE-A" "RUN planner" "1.0.0"
create_test_release "$RELEASE_B" "RELEASE-B" "AWAIT-OPERATOR" "1.1.0"
create_test_release "$RELEASE_V11" "RELEASE-V11" "RUN planner" "1.1.0"
create_test_release "$RELEASE_MODELS" "RELEASE-MODELS" "RUN planner" "1.2.0"
TREE_A="$(tree_for_directory "$RELEASE_A")"
TREE_B="$(tree_for_directory "$RELEASE_B")"
TREE_V11="$(tree_for_directory "$RELEASE_V11")"
TREE_MODELS="$(tree_for_directory "$RELEASE_MODELS")"
printf '%s\n' "$SHA_A" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
REGISTRY_SENTINEL="$TMP/registry-was-sourced"
cat > "$PROFILE/projects/launchtest.env" <<EOF
KIT_DIR=\$(touch "$REGISTRY_SENTINEL")
PRODUCT_ROOT=$LAUNCH_PRODUCT
EOF
write_active "$SHA_A" "$TREE_A" "$RELEASE_A"

chmod +x "$LAUNCHER"
run_launcher launchtest contract --json > "$TMP/launcher-contract.json"
python3 - "$TMP/launcher-contract.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["contract"] == "nysa.software-factory.hermes"
assert data["contract_version"] == "1.0.0"
assert data["launcher"]["source"] == "integrations/hermes/bin/factory-launch", data["launcher"]
PY
CLAIM_V1_RC=0
run_launcher launchtest claim --ticket T-123 > "$TMP/claim-v1.out" 2>&1 || CLAIM_V1_RC=$?
[[ "$CLAIM_V1_RC" -eq 1 ]] || fail "contract 1.0 unexpectedly exposed dispatcher leases"
MODELS_V1_RC=0
run_launcher launchtest models profiles --json > "$TMP/models-v1.out" 2>&1 || MODELS_V1_RC=$?
[[ "$MODELS_V1_RC" -eq 1 ]] || fail "contract 1.0 unexpectedly exposed model control"
TICKET_STATE_V1_RC=0
run_launcher launchtest ticket-state --ticket T-123 --workdir "$LAUNCH_PRODUCT" \
  --action materialize --json > "$TMP/ticket-state-v1.out" 2>&1 || TICKET_STATE_V1_RC=$?
[[ "$TICKET_STATE_V1_RC" -eq 1 ]] || fail "contract 1.0 unexpectedly exposed ticket-state"
[[ ! -e "$REGISTRY_SENTINEL" ]] || fail "launcher sourced arbitrary registry content"

run_launcher launchtest preflight --ticket T-123 --json > "$TMP/preflight-a.json"
run_launcher launchtest next-stage --ticket T-123 --json > "$TMP/next-a.json"
PREFLIGHT_HELPER_ENV="$LAUNCH_PRODUCT/factory/preflight-helper.env"
NEXT_HELPER_ENV="$LAUNCH_PRODUCT/factory/next-stage-helper.env"
assert_release_metadata "$PREFLIGHT_HELPER_ENV" "$SHA_A" "$TREE_A" "$RELEASE_A"
assert_release_metadata "$NEXT_HELPER_ENV" "$SHA_A" "$TREE_A" "$RELEASE_A"
assert_helper_confinement "$PREFLIGHT_HELPER_ENV"
assert_helper_confinement "$NEXT_HELPER_ENV"
assert_no_secret "$TMP/preflight-a.json"
assert_no_secret "$TMP/next-a.json"

PROFILE_ENV_BACKUP="$TMP/profile.env.backup"
cp -p "$PROFILE/.env" "$PROFILE_ENV_BACKUP"
mv "$PROFILE/.env" "$PROFILE/.env.absent"
run_launcher launchtest next-stage --ticket T-123 --json > "$TMP/next-caller-token-only.json"
assert_helper_confinement "$NEXT_HELPER_ENV" absent
assert_no_secret "$TMP/next-caller-token-only.json"
mv "$PROFILE/.env.absent" "$PROFILE/.env"

expect_profile_env_refusal() {
  local label="$1" rc=0 output
  output="$TMP/profile-env-$label.out"
  run_launcher launchtest contract --json > "$output" 2>&1 || rc=$?
  [[ "$rc" -eq 1 ]] || fail "unsafe profile environment was accepted: $label"
  assert_no_secret "$output"
}

printf 'GH_TOKEN=%s\nGH_TOKEN=%s\n' "$GH_SECRET" "$CALLER_GH_SECRET" > "$PROFILE/.env"
chmod 600 "$PROFILE/.env"
expect_profile_env_refusal duplicate
MALFORMED_PROFILE_SENTINEL="$TMP/malformed-profile-was-sourced"
printf 'GH_TOKEN=$(touch %s)\n' "$MALFORMED_PROFILE_SENTINEL" > "$PROFILE/.env"
chmod 600 "$PROFILE/.env"
expect_profile_env_refusal malformed
[[ ! -e "$MALFORMED_PROFILE_SENTINEL" ]] || fail "profile environment was sourced"
printf 'GH_TOKEN=%s\n' "$GH_SECRET" > "$PROFILE/.env"
chmod 644 "$PROFILE/.env"
expect_profile_env_refusal broad-mode
rm -f "$PROFILE/.env"
ln -s "$PROFILE_ENV_BACKUP" "$PROFILE/.env"
expect_profile_env_refusal symlink
rm -f "$PROFILE/.env"
cp -p "$PROFILE_ENV_BACKUP" "$PROFILE/.env"

python3 - "$TMP/preflight-a.json" "$TMP/next-a.json" <<'PY'
import json
import sys

preflight = json.load(open(sys.argv[1], encoding="utf-8"))
stage = json.load(open(sys.argv[2], encoding="utf-8"))
assert preflight == {
    "command": "preflight",
    "contract_version": "1.0.0",
    "exit_code": 0,
    "output": "PREFLIGHT RELEASE-A\n",
    "project": "launchtest",
    "schema": "nysa.software-factory.preflight/v1",
    "schema_version": 1,
    "status": "ok",
    "ticket": "T-123",
}
assert stage["schema"] == "nysa.software-factory.next-stage/v1"
assert stage["status"] == "ok"
assert stage["exit_code"] == 0
assert stage["action"] == "RUN"
assert stage["detail"] == "planner"
assert stage["output"] == "RUN planner\n"
PY

PREFLIGHT_FAIL_RC=0
touch "$LAUNCH_PRODUCT/factory/test-preflight-fail"
run_launcher launchtest preflight --ticket T-123 --json \
  > "$TMP/preflight-fail.json" || PREFLIGHT_FAIL_RC=$?
rm -f "$LAUNCH_PRODUCT/factory/test-preflight-fail"
[[ "$PREFLIGHT_FAIL_RC" -eq 7 ]] || fail "preflight wrapper did not preserve exit code"
python3 - "$TMP/preflight-fail.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["status"] == "error"
assert data["exit_code"] == 7
assert "PREFLIGHT RELEASE-A" in data["output"]
assert "[redacted-url]" in data["output"], "preflight output lacked URL redaction marker"
assert "Authorization: [redacted]" in data["output"]
assert '"api_token": [redacted]' in data["output"]
assert "password: [redacted]" in data["output"]
PY
assert_no_secret "$TMP/preflight-fail.json"

touch "$LAUNCH_PRODUCT/factory/test-preflight-signal"
SIGNAL_MARKER="$LAUNCH_PRODUCT/factory/test-preflight-signal-started"
SIGNAL_KITS_ROOT="$(cd "$KITS_ROOT" && pwd -P)"
SIGNAL_PROFILE="$(cd "$PROFILE" && pwd -P)"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_LAUNCH_TEST_MODE=1 FACTORY_LAUNCH_TEST_HOME="$TEST_HOME" \
  FACTORY_KITS_ROOT="$SIGNAL_KITS_ROOT" HERMES_FACTORY_PROFILE="$SIGNAL_PROFILE" \
  bash "$LAUNCHER" launchtest preflight --ticket T-124 --json \
  > "$TMP/signal-wrapper.json" &
SIGNAL_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $SIGNAL_PID"
for _try in $(seq 1 200); do
  [[ -e "$SIGNAL_MARKER" ]] && break
  sleep 0.02
done
[[ -e "$SIGNAL_MARKER" ]] || fail "signal cleanup fixture never reached helper"
kill -TERM "$SIGNAL_PID"
wait "$SIGNAL_PID" 2>/dev/null || true
BACKGROUND_PIDS=""
rm -f "$LAUNCH_PRODUCT/factory/test-preflight-signal" "$SIGNAL_MARKER"
if compgen -G "$TMP/launcher-tmp/factory-launch-tree.*" >/dev/null; then
  fail "signal cleanup retained the raw wrapper workspace"
fi

run_launcher launchtest doctor --json > "$TMP/launcher-doctor.json"
python3 - "$TMP/launcher-doctor.json" "$SHA_A" "$RELEASE_A" "$LAUNCH_PRODUCT" <<'PY'
import json
import os
import sys

path, sha, release, product = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
assert data["schema"] == "nysa.software-factory.hermes-doctor/v1"
assert data["checks"]["kit"] == {"status": "ok", "full_sha": sha}
assert data["checks"]["registry"]["kit_dir"] == os.path.realpath(release), "doctor reported wrong resolved release"
assert data["checks"]["registry"]["product_root"] == os.path.realpath(product), "doctor reported wrong product"
assert data["checks"]["kit_pin"]["matches_kit"] is True
PY
assert_no_secret "$TMP/launcher-doctor.json"
DOCTOR_HELPER_ENV="$LAUNCH_PRODUCT/factory/doctor-helper.env"
assert_release_metadata "$DOCTOR_HELPER_ENV" "$SHA_A" "$TREE_A" "$RELEASE_A"
assert_helper_confinement "$DOCTOR_HELPER_ENV"

# The upgraded standalone launcher must continue selecting an inherited 1.1
# release without rewriting its public contract.
printf '%s\n' "$SHA_V11" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_V11" "$TREE_V11" "$RELEASE_V11"
run_launcher launchtest contract --json > "$TMP/launcher-contract-v11.json"
run_launcher launchtest doctor --json > "$TMP/launcher-doctor-v11.json"
run_launcher launchtest preflight --ticket T-123 --json > "$TMP/preflight-v11.json"
run_launcher launchtest next-stage --ticket T-123 --json > "$TMP/next-v11.json"
TICKET_STATE_V11_RC=0
run_launcher launchtest ticket-state --ticket T-123 --workdir "$LAUNCH_PRODUCT" \
  --action materialize --json > "$TMP/ticket-state-v11.out" 2>&1 || TICKET_STATE_V11_RC=$?
[[ "$TICKET_STATE_V11_RC" -eq 1 ]] || fail "contract 1.1 unexpectedly exposed ticket-state"
PROJECT_LEDGER_V11_RC=0
run_launcher launchtest project-ledger --ticket T-123 --workdir "$LAUNCH_PRODUCT" \
  --json > "$TMP/project-ledger-v11.out" 2>&1 || PROJECT_LEDGER_V11_RC=$?
[[ "$PROJECT_LEDGER_V11_RC" -eq 1 ]] || fail "contract 1.1 unexpectedly exposed project-ledger"
MODELS_V11_RC=0
run_launcher launchtest models status --json > "$TMP/models-v11.out" 2>&1 || MODELS_V11_RC=$?
[[ "$MODELS_V11_RC" -eq 1 ]] || fail "contract 1.1 unexpectedly exposed model control"
python3 - "$TMP/launcher-contract-v11.json" "$TMP/launcher-doctor-v11.json" \
  "$TMP/preflight-v11.json" "$TMP/next-v11.json" <<'PY'
import json, sys
for path in sys.argv[1:]:
    assert json.load(open(path, encoding="utf-8"))["contract_version"] == "1.1.0"
PY

printf '%s\n' "$SHA_B" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_B" "$TREE_B" "$RELEASE_B"
run_launcher launchtest next-stage --ticket T-123 --json > "$TMP/next-b.json"
python3 - "$TMP/next-b.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["action"] == "AWAIT-OPERATOR"
assert data["detail"] is None
assert data["output"] == "AWAIT-OPERATOR\n"
PY

# Contract 1.1 exposes bounded, opaque ticket-lease operations through the
# same release-selected launcher. Contract 1.0 above remained usable without them.
mkdir -p "$LAUNCH_PRODUCT/factory/tickets"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=2' > "$LAUNCH_PRODUCT/factory/PROJECT.env"
for ticket in T-201 T-202 T-203; do
  printf '# %s\n\nState: Ready\n' "$ticket" > "$LAUNCH_PRODUCT/factory/tickets/$ticket.md"
done
run_launcher launchtest claim --ticket T-201 > "$TMP/claim-201.json"
run_launcher launchtest claim --ticket T-202 > "$TMP/claim-202.json"
CLAIM_201_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/claim-201.json")"
CLAIM_202_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/claim-202.json")"
CLAIM_203_RC=0
run_launcher launchtest claim --ticket T-203 > "$TMP/claim-203.out" 2>&1 || CLAIM_203_RC=$?
[[ "$CLAIM_203_RC" -eq 1 ]] || fail "launcher accepted a third concurrent ticket"
run_launcher launchtest renew --ticket T-201 --lease "$CLAIM_201_ID" > "$TMP/renew-201.json"
run_launcher launchtest release --ticket T-201 --lease "$CLAIM_201_ID" > "$TMP/release-201.json"
run_launcher launchtest release --ticket T-202 --lease "$CLAIM_202_ID" > "$TMP/release-202.json"
[[ ! -n "$(find "$LAUNCH_PRODUCT/factory/.dispatch-leases" -type f -print -quit)" ]] ||
  fail "launcher lease release left state behind"
rm -f "$LAUNCH_PRODUCT/factory/tickets/T-201.md" \
  "$LAUNCH_PRODUCT/factory/tickets/T-202.md" "$LAUNCH_PRODUCT/factory/tickets/T-203.md"
rm -rf "$LAUNCH_PRODUCT/factory/.dispatch-leases"
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' > "$LAUNCH_PRODUCT/factory/PROJECT.env"

KITS_ROOT_PHYS="$(cd "$KITS_ROOT" && pwd -P)"
PROFILE_PHYS="$(cd "$PROFILE" && pwd -P)"
ln -s "$KITS_ROOT_PHYS" "$TMP/kits-root-link"
ROOT_LINK_RC=0
LAUNCHER_KITS_ROOT_OVERRIDE="$TMP/kits-root-link" \
  run_launcher launchtest contract --json > "$TMP/root-link.out" 2>&1 || ROOT_LINK_RC=$?
[[ "$ROOT_LINK_RC" -eq 1 ]] || fail "symlink FACTORY_KITS_ROOT was accepted"
ln -s "$PROFILE_PHYS" "$TMP/profile-link"
PROFILE_LINK_RC=0
LAUNCHER_PROFILE_OVERRIDE="$TMP/profile-link" \
  run_launcher launchtest contract --json > "$TMP/profile-link.out" 2>&1 || PROFILE_LINK_RC=$?
[[ "$PROFILE_LINK_RC" -eq 1 ]] || fail "symlink Hermes profile was accepted"
FORGED_HOME="$TMP/forged-home"
FORGED_KITS="$FORGED_HOME/.factory/kits"
FORGED_PROFILE="$FORGED_HOME/.hermes/profiles/factory"
mkdir -p "$FORGED_KITS" "$FORGED_PROFILE"
printf 'GH_TOKEN=%s\n' "$CALLER_GH_SECRET" > "$FORGED_PROFILE/.env"
chmod 600 "$FORGED_PROFILE/.env"
FORGED_ROOT_RC=0
HOME="$FORGED_HOME" FACTORY_KITS_ROOT="$FORGED_KITS" \
  HERMES_FACTORY_PROFILE="$FORGED_PROFILE" \
  bash "$LAUNCHER" launchtest contract --json \
  > "$TMP/forged-root.out" 2>&1 || FORGED_ROOT_RC=$?
[[ "$FORGED_ROOT_RC" -eq 1 ]] ||
  fail "repository launcher accepted forged production roots"
assert_no_secret "$TMP/forged-root.out"

SIMULATED_ACCOUNT="$TMP/simulated-account"
SIMULATED_INSTALL="$SIMULATED_ACCOUNT/.factory/bin/factory-launch"
mkdir -p "$(dirname "$SIMULATED_INSTALL")"
cp "$LAUNCHER" "$SIMULATED_INSTALL"
chmod +x "$SIMULATED_INSTALL"
SIMULATED_OVERRIDE_RC=0
HOME="$FORGED_HOME" FACTORY_LAUNCH_TEST_MODE=1 \
  FACTORY_LAUNCH_TEST_ACCOUNT_HOME="$SIMULATED_ACCOUNT" \
  FACTORY_LAUNCH_TEST_HOME="$TEST_HOME" FACTORY_KITS_ROOT="$KITS_ROOT_PHYS" \
  HERMES_FACTORY_PROFILE="$PROFILE_PHYS" \
  bash "$SIMULATED_INSTALL" launchtest contract --json \
  > "$TMP/simulated-installed-override.out" 2>&1 || SIMULATED_OVERRIDE_RC=$?
[[ "$SIMULATED_OVERRIDE_RC" -eq 1 ]] ||
  fail "installed trust-root path accepted test root overrides"
assert_no_secret "$TMP/simulated-installed-override.out"

mv "$KITS_ROOT_PHYS/projects" "$KITS_ROOT_PHYS/projects-real"
ln -s "$KITS_ROOT_PHYS/projects-real" "$KITS_ROOT_PHYS/projects"
PROJECTS_LINK_RC=0
run_launcher launchtest contract --json > "$TMP/projects-link.out" 2>&1 || PROJECTS_LINK_RC=$?
[[ "$PROJECTS_LINK_RC" -eq 1 ]] || fail "symlink projects directory was accepted"
rm "$KITS_ROOT_PHYS/projects"
mv "$KITS_ROOT_PHYS/projects-real" "$KITS_ROOT_PHYS/projects"

mv "$KITS_ROOT_PHYS/releases" "$KITS_ROOT_PHYS/releases-real"
ln -s "$KITS_ROOT_PHYS/releases-real" "$KITS_ROOT_PHYS/releases"
RELEASES_LINK_RC=0
run_launcher launchtest contract --json > "$TMP/releases-link.out" 2>&1 || RELEASES_LINK_RC=$?
[[ "$RELEASES_LINK_RC" -eq 1 ]] || fail "symlink releases directory was accepted"
rm "$KITS_ROOT_PHYS/releases"
mv "$KITS_ROOT_PHYS/releases-real" "$KITS_ROOT_PHYS/releases"

mv "$KITS_ROOT_PHYS/projects/launchtest" "$KITS_ROOT_PHYS/projects/launchtest-real"
ln -s "$KITS_ROOT_PHYS/projects/launchtest-real" "$KITS_ROOT_PHYS/projects/launchtest"
STATE_LINK_RC=0
run_launcher launchtest contract --json > "$TMP/state-link.out" 2>&1 || STATE_LINK_RC=$?
[[ "$STATE_LINK_RC" -eq 1 ]] || fail "symlink project state directory was accepted"
rm "$KITS_ROOT_PHYS/projects/launchtest"
mv "$KITS_ROOT_PHYS/projects/launchtest-real" "$KITS_ROOT_PHYS/projects/launchtest"

mv "$KITS_ROOT_PHYS/projects/launchtest/active.json" \
  "$KITS_ROOT_PHYS/projects/launchtest/active-real.json"
ln -s "$KITS_ROOT_PHYS/projects/launchtest/active-real.json" \
  "$KITS_ROOT_PHYS/projects/launchtest/active.json"
ACTIVE_LINK_RC=0
run_launcher launchtest contract --json > "$TMP/active-link.out" 2>&1 || ACTIVE_LINK_RC=$?
[[ "$ACTIVE_LINK_RC" -eq 1 ]] || fail "symlink active record was accepted"
rm "$KITS_ROOT_PHYS/projects/launchtest/active.json"
mv "$KITS_ROOT_PHYS/projects/launchtest/active-real.json" \
  "$KITS_ROOT_PHYS/projects/launchtest/active.json"

mv "$RELEASE_B" "$RELEASE_B-real"
ln -s "$RELEASE_B-real" "$RELEASE_B"
RELEASE_LINK_RC=0
run_launcher launchtest contract --json > "$TMP/release-link.out" 2>&1 || RELEASE_LINK_RC=$?
[[ "$RELEASE_LINK_RC" -eq 1 ]] || fail "symlink selected release was accepted"
rm "$RELEASE_B"
mv "$RELEASE_B-real" "$RELEASE_B"

# Refuse slug traversal, release containment escape, product drift, and tree tampering.
TRAVERSAL_RC=0
run_launcher "../launchtest" contract --json > "$TMP/traversal.out" 2>&1 || TRAVERSAL_RC=$?
[[ "$TRAVERSAL_RC" -eq 1 ]] || fail "project slug traversal was accepted"

OUTSIDE_RELEASE="$TMP/outside-release"
cp -R "$RELEASE_B" "$OUTSIDE_RELEASE"
write_active "$SHA_B" "$TREE_B" "$OUTSIDE_RELEASE"
OUTSIDE_RC=0
run_launcher launchtest contract --json > "$TMP/outside.out" 2>&1 || OUTSIDE_RC=$?
[[ "$OUTSIDE_RC" -eq 1 ]] || fail "release outside FACTORY_KITS_ROOT was accepted"

write_active "$SHA_B" "$TREE_B" "$RELEASE_B" "$PRODUCT"
PRODUCT_DRIFT_RC=0
run_launcher launchtest contract --json > "$TMP/product-drift.out" 2>&1 || PRODUCT_DRIFT_RC=$?
[[ "$PRODUCT_DRIFT_RC" -eq 1 ]] || fail "active product path drift was accepted"

write_active "$SHA_B" "$TREE_B" "$RELEASE_B"
printf 'tampered\n' > "$RELEASE_B/untracked-tamper"
TREE_TAMPER_RC=0
run_launcher launchtest contract --json > "$TMP/tree-tamper.out" 2>&1 || TREE_TAMPER_RC=$?
[[ "$TREE_TAMPER_RC" -eq 1 ]] || fail "release tree tampering was accepted"
rm -f "$RELEASE_B/untracked-tamper"

python3 - "$KITS_ROOT/projects/launchtest/active.json" <<'PY'
import json
import sys

path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["contract_version"] = "9.0.0"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(value, handle)
    handle.write("\n")
PY
CONTRACT_DRIFT_RC=0
run_launcher launchtest contract --json > "$TMP/contract-drift.out" 2>&1 || CONTRACT_DRIFT_RC=$?
[[ "$CONTRACT_DRIFT_RC" -eq 1 ]] || fail "incompatible active contract was accepted"

# An active-record switch after helper start cannot change the selected release.
printf '%s\n' "$SHA_A" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_A" "$TREE_A" "$RELEASE_A"
RACE_MARKER="$LAUNCH_PRODUCT/factory/test-preflight-started"
RACE_GATE="$LAUNCH_PRODUCT/factory/test-preflight-gate"
touch "$LAUNCH_PRODUCT/factory/test-preflight-block"
run_launcher launchtest preflight --ticket T-999 --json > "$TMP/race.json" &
RACE_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $RACE_PID"
for _try in $(seq 1 200); do
  [[ -e "$RACE_MARKER" ]] && break
  sleep 0.02
done
[[ -e "$RACE_MARKER" ]] || fail "race fixture never started selected helper"
printf '%s\n' "$SHA_B" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_B" "$TREE_B" "$RELEASE_B"
touch "$RACE_GATE"
wait "$RACE_PID"
BACKGROUND_PIDS=""
rm -f "$LAUNCH_PRODUCT/factory/test-preflight-block" "$RACE_MARKER" "$RACE_GATE"
python3 - "$TMP/race.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["output"] == "PREFLIGHT RELEASE-A\n"
PY

# Close-out reorder runs only in a physical worktree for the registered product.
git -C "$LAUNCH_PRODUCT" init -b main >/dev/null 2>&1
git -C "$LAUNCH_PRODUCT" config user.email "hermes-contract@test.local"
git -C "$LAUNCH_PRODUCT" config user.name "hermes-contract-test"
printf 'launcher worktree fixture\n' > "$LAUNCH_PRODUCT/README.md"
mkdir -p "$LAUNCH_PRODUCT/factory/tickets"
printf '# T-123\n\nState: Ready\n' > "$LAUNCH_PRODUCT/factory/tickets/T-123.md"
cat > "$LAUNCH_PRODUCT/.gitignore" <<'EOF'
factory/*-helper.env
factory/runs/
factory/runtime-ledger.csv
factory/.active-runs/
factory/.provider.lock/
factory/.dispatch-leases/
factory/test-adapter-gate
EOF
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' > "$LAUNCH_PRODUCT/factory/PROJECT.env"
git -C "$LAUNCH_PRODUCT" add -A
git -C "$LAUNCH_PRODUCT" commit -qm "seed launcher worktree"
LAUNCH_PRODUCT_REMOTE="$TMP/launch-product.git"
git init --bare -q "$LAUNCH_PRODUCT_REMOTE"
git -C "$LAUNCH_PRODUCT" remote add origin "$LAUNCH_PRODUCT_REMOTE"
git -C "$LAUNCH_PRODUCT" push -q -u origin main
REORDER_WORKTREE="$TMP/reorder-worktree"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-456 "$REORDER_WORKTREE"
REORDER_WORKTREE_PHYS="$(cd "$REORDER_WORKTREE" && pwd -P)"
RUN_WORKTREE="$TMP/run-worktree"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-123 "$RUN_WORKTREE"
RUN_WORKTREE_PHYS="$(cd "$RUN_WORKTREE" && pwd -P)"
WRONG_TICKET_WORKTREE="$TMP/wrong-ticket-worktree"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-999 "$WRONG_TICKET_WORKTREE"
WRONG_TICKET_WORKTREE_PHYS="$(cd "$WRONG_TICKET_WORKTREE" && pwd -P)"
RELEASE_B_PHYS="$(cd "$RELEASE_B" && pwd -P)"
RELEASE_A_PHYS="$(cd "$RELEASE_A" && pwd -P)"
LAUNCH_PRODUCT_PHYS="$(cd "$LAUNCH_PRODUCT" && pwd -P)"

# Contract 1.2 exposes only the task-free, release-selected model-control
# grammar. The launcher supplies project isolation and validates pin worktrees.
printf '%s\n' "$SHA_MODELS" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_MODELS" "$TREE_MODELS" "$RELEASE_MODELS"
MODEL_STATE_ROOT_PHYS="$(cd "$KITS_ROOT/projects" && pwd -P)"
MODEL_HELPER_ENV="$LAUNCH_PRODUCT/factory/model-helper.env"

assert_model_call() {
  local label="$1"
  shift
  local origin_expectation=absent
  [[ "$1" != "pin" ]] || origin_expectation=present
  local action="$1"
  local output="$TMP/models-$label.json"
  if ! run_launcher launchtest models "$@" > "$output" 2>"$TMP/models-$label.err"; then
    awk '{print}' "$TMP/models-$label.err" >&2
    fail "valid model-control invocation was refused: $label"
  fi
  python3 - "$output" "$action" <<'PY'
import json, sys
path, action = sys.argv[1:]
value = json.load(open(path, encoding="utf-8"))
expected = {
    "profiles": "model-manager-profiles/v1",
    "status": "model-manager-status/v1",
    "plan": "model-resolution-plan/v1",
    "activate": "model-routing-active/v1",
    "disable": "model-routing-overrides/v1",
    "enable": "model-routing-overrides/v1",
}[action]
assert value["schema"] == expected, value
if action in {"status", "activate", "disable", "enable"}:
    assert value["project"] == "launchtest", value
PY
  assert_no_secret "$output"
  assert_helper_confinement "$MODEL_HELPER_ENV" absent "$origin_expectation"
  ! grep -qFx "GH_TOKEN_PRESENT=true" "$MODEL_HELPER_ENV" ||
    fail "task-free model helper received GH_TOKEN"
}

assert_model_call profiles profiles --json
assert_model_call status status --json
assert_model_call plan plan --json
assert_model_call plan-profile plan --profile legacy-balanced-v1 --json
MODEL_PROFILE_HASH="$(python3 "$ROOT/scripts/model-router.py" profile-hash \
  legacy-balanced-v1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["profile_hash"])')"
assert_model_call activate activate --profile legacy-balanced-v1 \
  --approve-hash "$MODEL_PROFILE_HASH" \
  --approved-by operator-1 --json
assert_model_call disable disable --scope-type route --scope-id codex-gpt-5.6-sol \
  --reason credits_exhausted --ttl-seconds 60 --operator-id operator-1 --json
assert_model_call enable enable --scope-type route --scope-id codex-gpt-5.6-sol --json

PIN_HEAD_BEFORE="$(git -C "$RUN_WORKTREE_PHYS" rev-parse HEAD)"
if ! run_launcher launchtest models pin --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" \
  --json > "$TMP/models-pin.json"; then
  cat "$TMP/models-pin.json" >&2
  fail "valid model pin invocation failed"
fi
python3 - "$TMP/models-pin.json" "$RUN_WORKTREE_PHYS" "$SHA_MODELS" <<'PY'
import json, pathlib, subprocess, sys
path, workdir, kit_sha = sys.argv[1:]
value = json.load(open(path, encoding="utf-8"))
assert value["schema"] == "ticket-model-route-plan/v1", value
assert value["commit_created"] is True, value
assert len(value["commit_sha"]) == 40 and len(value["pin_hash"]) == 64, value
ticket = pathlib.Path(workdir) / "factory/tickets/T-123.md"
plan = pathlib.Path(workdir) / "factory/route-plans/T-123.json"
assert ticket.read_text().count("Kit-SHA:") == 1
assert f"Kit-SHA: {kit_sha}" in ticket.read_text()
assert json.loads(plan.read_text())["ticket"] == "T-123"
changed = subprocess.check_output(
    ["git", "-C", workdir, "diff-tree", "--no-commit-id", "--name-only", "-r",
     value["commit_sha"]], text=True
).splitlines()
assert sorted(changed) == [
    "factory/route-plans/T-123.json", "factory/tickets/T-123.md"
], changed
PY
[[ "$(git -C "$RUN_WORKTREE_PHYS" rev-list --count "$PIN_HEAD_BEFORE..HEAD")" == "1" ]] ||
  fail "model pin did not create exactly one commit"
[[ -z "$(git -C "$RUN_WORKTREE_PHYS" status --porcelain --untracked-files=all)" ]] ||
  fail "model pin left staged or dirty state"
PIN_REMOTE_HEAD="$(git -C "$RUN_WORKTREE_PHYS" ls-remote --heads \
  "$LAUNCH_PRODUCT_REMOTE" refs/heads/ticket/T-123 | awk 'NR==1 {print $1; exit}')"
[[ "$PIN_REMOTE_HEAD" == "$(git -C "$RUN_WORKTREE_PHYS" rev-parse HEAD)" ]] ||
  fail "model pin did not push the exact ticket branch"
assert_helper_confinement "$MODEL_HELPER_ENV" absent present
assert_no_secret "$TMP/models-pin.json"
if ! run_launcher launchtest models pin --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" \
  --json > "$TMP/models-pin-again.json"; then
  cat "$TMP/models-pin-again.json" >&2
  fail "idempotent model pin invocation failed"
fi
python3 - "$TMP/models-pin.json" "$TMP/models-pin-again.json" <<'PY'
import json, sys
first, second = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
assert second["commit_created"] is False, second
assert second["commit_sha"] == first["commit_sha"], (first, second)
assert second["pin_hash"] == first["pin_hash"], (first, second)
PY

expect_bad_model() {
  local label="$1" rc=0
  shift
  run_launcher launchtest models "$@" > "$TMP/bad-model-$label.out" 2>&1 || rc=$?
  [[ "$rc" -ne 0 ]] || fail "invalid model-control invocation was accepted: $label"
  assert_no_secret "$TMP/bad-model-$label.out"
}

expect_bad_model reordered-json --json profiles
expect_bad_model extra profiles --json extra
expect_bad_model reordered-profile plan --json --profile legacy-balanced-v1
expect_bad_model malformed-profile plan --profile ../unsafe --json
expect_bad_model malformed-hash activate --profile legacy-balanced-v1 \
  --approve-hash ABC --approved-by operator-1 --json
expect_bad_model wrong-hash activate --profile legacy-balanced-v1 \
  --approve-hash bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --approved-by operator-1 --json
expect_bad_model malformed-scope enable --scope-type route --scope-id ../unsafe --json
expect_bad_model bad-reason disable --scope-type route --scope-id codex-gpt-5.6-sol \
  --reason operator_request --ttl-seconds 60 --operator-id operator-1 --json
expect_bad_model zero-ttl disable --scope-type route --scope-id codex-gpt-5.6-sol \
  --reason credits_exhausted --ttl-seconds 0 --operator-id operator-1 --json
expect_bad_model large-ttl disable --scope-type route --scope-id codex-gpt-5.6-sol \
  --reason credits_exhausted --ttl-seconds 604801 --operator-id operator-1 --json
expect_bad_model pin-main pin --ticket T-123 --workdir "$LAUNCH_PRODUCT_PHYS" --json
expect_bad_model pin-wrong-ticket pin --ticket T-123 \
  --workdir "$WRONG_TICKET_WORKTREE_PHYS" --json

mv "$KITS_ROOT/projects/launchtest/routing" \
  "$KITS_ROOT/projects/launchtest/routing-real"
ln -s "$KITS_ROOT/projects/launchtest/routing-real" \
  "$KITS_ROOT/projects/launchtest/routing"
expect_bad_model symlinked-state status --json
rm "$KITS_ROOT/projects/launchtest/routing"
mv "$KITS_ROOT/projects/launchtest/routing-real" \
  "$KITS_ROOT/projects/launchtest/routing"

touch "$LAUNCH_PRODUCT/factory/MAINTENANCE"
assert_model_call maintenance-profiles profiles --json
assert_model_call maintenance-status status --json
assert_model_call maintenance-plan plan --json
expect_bad_model maintenance-activate activate --profile legacy-balanced-v1 \
  --approve-hash aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --approved-by operator-1 --json
expect_bad_model maintenance-disable disable --scope-type route \
  --scope-id codex-gpt-5.6-sol --reason credits_exhausted --ttl-seconds 60 \
  --operator-id operator-1 --json
expect_bad_model maintenance-enable enable --scope-type route \
  --scope-id codex-gpt-5.6-sol --json
expect_bad_model maintenance-pin pin --ticket T-123 \
  --workdir "$RUN_WORKTREE_PHYS" --json
rm -f "$LAUNCH_PRODUCT/factory/MAINTENANCE"
# Keep later launcher/run accounting fixtures independent from the model-state
# mutation coverage above.
rm -rf "$KITS_ROOT/projects/launchtest/routing"
rm -f "$TEST_HOME/.factory/global.env"

# Compatibility smoke: the new launcher can still run a mock role selected
# from an active 1.0 release.
printf '%s\n' "$SHA_A" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_A" "$TREE_A" "$RELEASE_A"
run_launcher launchtest run \
  --role builder \
  --ticket T-123 \
  --prompt-file "$RELEASE_A_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" \
  -- "1.0 compatibility smoke" > "$TMP/run-v1.txt"
grep -qF "RUN RELEASE-A" "$TMP/run-v1.txt" || fail "active contract 1.0 mock role failed"
printf '%s\n' "$SHA_B" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_B" "$TREE_B" "$RELEASE_B"
run_launcher launchtest run \
  --role builder \
  --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" \
  -- "build safely" > "$TMP/run-b.txt"
RUN_HELPER_ENV="$LAUNCH_PRODUCT/factory/run-helper.env"
assert_release_metadata "$RUN_HELPER_ENV" "$SHA_B" "$TREE_B" "$RELEASE_B"
assert_helper_confinement "$RUN_HELPER_ENV"
grep -qF "RUN RELEASE-B" "$TMP/run-b.txt" || fail "run did not use selected release"
grep -qF "FACTORY_ROOT=$LAUNCH_PRODUCT_PHYS" "$TMP/run-b.txt" ||
  fail "run did not bind the registered product root"
grep -qF "ARG=$RELEASE_B_PHYS/roles/builder.md" "$TMP/run-b.txt" ||
  fail "run did not pass the canonical release prompt"
grep -qF "ARG=$RUN_WORKTREE_PHYS" "$TMP/run-b.txt" ||
  fail "run did not pass the physical product worktree"
grep -qF "ARG=build safely" "$TMP/run-b.txt" || fail "run changed the task"
assert_no_secret "$TMP/run-b.txt"

expect_bad_run() {
  local label="$1"
  shift
  local rc=0
  run_launcher launchtest run "$@" > "$TMP/bad-run-$label.out" 2>&1 || rc=$?
  [[ "$rc" -ne 0 ]] || fail "invalid run was accepted: $label"
}

expect_bad_run role \
  --role dispatcher --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
expect_bad_run ticket \
  --role builder --ticket ../T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
expect_bad_run adapter \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" --adapter mock -- task
expect_bad_run prompt-escape \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/../roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
expect_bad_run prompt-symlink \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder-link.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
printf 'do not read\n' > "$LAUNCH_PRODUCT_PHYS/secret-prompt.txt"
expect_bad_run prompt-secret-file \
  --role builder --ticket T-123 \
  --prompt-file "$LAUNCH_PRODUCT_PHYS/secret-prompt.txt" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
rm -f "$LAUNCH_PRODUCT_PHYS/secret-prompt.txt"
expect_bad_run empty-task \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- ""
expect_bad_run missing-separator \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" task

expect_bad_run main-checkout \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$LAUNCH_PRODUCT_PHYS" -- task
expect_bad_run wrong-ticket-branch \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$WRONG_TICKET_WORKTREE_PHYS" -- task
DETACHED_WORKTREE="$TMP/detached-worktree"
git -C "$LAUNCH_PRODUCT" worktree add -q --detach "$DETACHED_WORKTREE" HEAD
DETACHED_WORKTREE_PHYS="$(cd "$DETACHED_WORKTREE" && pwd -P)"
expect_bad_run detached-worktree \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$DETACHED_WORKTREE_PHYS" -- task
PREFIX_SENTINEL="$TMP/prefix-injection-executed"
printf 'TICKET_BRANCH_PREFIX=$(touch %s)\n' "$PREFIX_SENTINEL" \
  > "$LAUNCH_PRODUCT/factory/PROJECT.env"
expect_bad_run unsafe-branch-prefix \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
[[ ! -e "$PREFIX_SENTINEL" ]] || fail "ticket branch prefix was evaluated"
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' 'TICKET_BRANCH_PREFIX=other/' \
  > "$LAUNCH_PRODUCT/factory/PROJECT.env"
expect_bad_run duplicate-branch-prefix \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- task
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' > "$LAUNCH_PRODUCT/factory/PROJECT.env"

touch "$LAUNCH_PRODUCT/factory/MAINTENANCE"
expect_bad_run maintenance \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$RUN_WORKTREE_PHYS" -- blocked
rm -f "$LAUNCH_PRODUCT/factory/MAINTENANCE"

REORDER_ARG_SENTINEL="$TMP/reorder-argument-executed"
REORDER_INJECTION_ARG="\$(touch $REORDER_ARG_SENTINEL)"
run_launcher launchtest reorder-test-fixes \
  --ticket T-456 \
  --workdir "$REORDER_WORKTREE_PHYS" \
  -- --base main --test-paths "tests/" "$REORDER_INJECTION_ARG" \
  > "$TMP/reorder.txt"
REORDER_HELPER_ENV="$LAUNCH_PRODUCT/factory/reorder-helper.env"
assert_release_metadata "$REORDER_HELPER_ENV" "$SHA_B" "$TREE_B" "$RELEASE_B"
assert_helper_confinement "$REORDER_HELPER_ENV"
grep -qF "REORDER RELEASE-B" "$TMP/reorder.txt" ||
  fail "reorder did not use the selected release"
grep -qF "WORKDIR=$REORDER_WORKTREE_PHYS" "$TMP/reorder.txt" ||
  fail "reorder did not execute from the resolved worktree"
grep -qF "ARG=--base" "$TMP/reorder.txt" || fail "reorder dropped helper arguments"
grep -qF "ARG=$REORDER_INJECTION_ARG" "$TMP/reorder.txt" ||
  fail "reorder changed a passthrough argument"
[[ ! -e "$REORDER_ARG_SENTINEL" ]] || fail "reorder evaluated a passthrough argument"
assert_no_secret "$TMP/reorder.txt"

expect_bad_reorder() {
  local label="$1" rc=0
  shift
  run_launcher launchtest reorder-test-fixes "$@" \
    > "$TMP/bad-reorder-$label.out" 2>&1 || rc=$?
  [[ "$rc" -ne 0 ]] || fail "invalid reorder worktree was accepted: $label"
}

expect_bad_reorder main-checkout \
  --ticket T-456 --workdir "$LAUNCH_PRODUCT_PHYS" -- --base main
expect_bad_reorder wrong-ticket-branch \
  --ticket T-456 --workdir "$WRONG_TICKET_WORKTREE_PHYS" -- --base main
expect_bad_reorder detached-worktree \
  --ticket T-456 --workdir "$DETACHED_WORKTREE_PHYS" -- --base main
expect_bad_reorder malformed-ticket \
  --ticket ../T-456 --workdir "$REORDER_WORKTREE_PHYS" -- --base main

REORDER_LINK="$(cd "$TMP" && pwd -P)/reorder-worktree-link"
ln -s "$REORDER_WORKTREE_PHYS" "$REORDER_LINK"
expect_bad_run symlink-workdir \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$REORDER_LINK" -- task
REORDER_LINK_RC=0
run_launcher launchtest reorder-test-fixes --ticket T-456 \
  --workdir "$REORDER_LINK" -- --base main \
  > "$TMP/reorder-link.out" 2>&1 || REORDER_LINK_RC=$?
[[ "$REORDER_LINK_RC" -eq 1 ]] || fail "symlink reorder workdir was accepted"

REORDER_TRAVERSAL_RC=0
run_launcher launchtest reorder-test-fixes \
  --ticket T-456 \
  --workdir "$REORDER_WORKTREE_PHYS/../$(basename "$REORDER_WORKTREE_PHYS")" -- \
  --base main > "$TMP/reorder-traversal.out" 2>&1 || REORDER_TRAVERSAL_RC=$?
[[ "$REORDER_TRAVERSAL_RC" -eq 1 ]] || fail "reorder workdir traversal was accepted"

mkdir -p "$REORDER_WORKTREE_PHYS/nested"
REORDER_SUBDIR_RC=0
run_launcher launchtest reorder-test-fixes --ticket T-456 \
  --workdir "$REORDER_WORKTREE_PHYS/nested" -- \
  --base main > "$TMP/reorder-subdir.out" 2>&1 || REORDER_SUBDIR_RC=$?
[[ "$REORDER_SUBDIR_RC" -eq 1 ]] || fail "non-root reorder workdir was accepted"

UNRELATED_REPO="$TMP/unrelated-repo"
mkdir -p "$UNRELATED_REPO"
git -C "$UNRELATED_REPO" init -q
UNRELATED_REPO_PHYS="$(cd "$UNRELATED_REPO" && pwd -P)"
expect_bad_run foreign-workdir \
  --role builder --ticket T-123 \
  --prompt-file "$RELEASE_B_PHYS/roles/builder.md" \
  --workdir "$UNRELATED_REPO_PHYS" -- task
REORDER_UNRELATED_RC=0
run_launcher launchtest reorder-test-fixes --ticket T-456 \
  --workdir "$UNRELATED_REPO_PHYS" -- \
  --base main > "$TMP/reorder-unrelated.out" 2>&1 || REORDER_UNRELATED_RC=$?
[[ "$REORDER_UNRELATED_RC" -eq 1 ]] || fail "unrelated Git worktree was accepted"

touch "$LAUNCH_PRODUCT/factory/MAINTENANCE"
REORDER_MAINTENANCE_RC=0
run_launcher launchtest reorder-test-fixes --ticket T-456 \
  --workdir "$REORDER_WORKTREE_PHYS" -- \
  --base main > "$TMP/reorder-maintenance.out" 2>&1 || REORDER_MAINTENANCE_RC=$?
[[ "$REORDER_MAINTENANCE_RC" -eq 1 ]] || fail "reorder did not refuse maintenance"
rm -f "$LAUNCH_PRODUCT/factory/MAINTENANCE"

# Real sealed-runtime smoke: copied production helpers, no .git, trusted CLI stub.
SHA_C="cccccccccccccccccccccccccccccccccccccccc"
RELEASE_C="$KITS_ROOT/releases/$SHA_C"
mkdir -p "$RELEASE_C/integrations/hermes" "$RELEASE_C/scripts/model-routing"
cp "$CONTRACT" "$RELEASE_C/integrations/hermes/contract.json"
cp -R "$ROOT/roles" "$RELEASE_C/"
cp -R "$ROOT/scripts/lib" "$RELEASE_C/scripts/"
cp -R "$ROOT/scripts/adapters" "$RELEASE_C/scripts/"
for helper in preflight.sh next-stage.sh run-agent.sh ticket-state.sh ledger-view.py reorder-test-fixes.sh dispatch-lease.sh model-control.sh model-manager.py model-router.py; do
  cp -p "$ROOT/scripts/$helper" "$RELEASE_C/scripts/$helper"
done
cp -p "$ROOT/scripts/model-routing/catalog-v1.json" \
  "$ROOT/scripts/model-routing/profiles-v1.json" "$RELEASE_C/scripts/model-routing/"
# Keep the production mock adapter's confidentiality assertion, but wrap it with
# a deterministic gate so concurrent launcher runs can be observed in flight.
mv "$RELEASE_C/scripts/adapters/mock.sh" "$RELEASE_C/scripts/adapters/mock-real.sh"
cat > "$RELEASE_C/scripts/adapters/mock.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
args=("$@")
workdir="" task=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2 ;;
    --) shift; task="${*:-}"; break ;;
    *) shift ;;
  esac
done
case "$task" in
  overlap-*|budget-*)
    touch "$workdir/.factory-test-adapter-started"
    gate_open=0
    for _try in $(seq 1 1000); do
      if [[ -e "$FACTORY_ROOT/factory/test-adapter-gate" ]]; then
        gate_open=1
        break
      fi
      sleep 0.02
    done
    [[ "$gate_open" -eq 1 ]] || { echo "test adapter gate timed out" >&2; exit 98; }
    ;;
esac
[[ "$task" != "overlap-fail" ]] || export MOCK_STATUS=42
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/mock-real.sh" "${args[@]}"
STUB
chmod +x "$RELEASE_C/scripts/adapters/mock.sh" "$RELEASE_C/scripts/adapters/mock-real.sh"
[[ ! -e "$RELEASE_C/.git" ]] || fail "real sealed fixture unexpectedly has Git metadata"
cmp "$ROOT/scripts/next-stage.sh" "$RELEASE_C/scripts/next-stage.sh" >/dev/null ||
  fail "real next-stage helper was not copied exactly"
cmp "$ROOT/scripts/run-agent.sh" "$RELEASE_C/scripts/run-agent.sh" >/dev/null ||
  fail "real run-agent helper was not copied exactly"
REAL_TREE="$(tree_for_directory "$RELEASE_C")"
chmod -R a-w "$RELEASE_C"
printf '%s\n' "$SHA_C" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
mkdir -p "$LAUNCH_PRODUCT/factory/tickets" "$LAUNCH_PRODUCT/factory/initiatives"
for ticket in T-777 T-778 T-779 T-780; do
  cat > "$LAUNCH_PRODUCT/factory/tickets/$ticket.md" <<TICKET
# $ticket — sealed runtime concurrency smoke

State: Ready
Initiative: I-777
Priority: normal

## Log
TICKET
done
cat > "$LAUNCH_PRODUCT/factory/initiatives/I-777.md" <<'INITIATIVE'
# Sealed runtime smoke

Status: planned
INITIATIVE
cat > "$LAUNCH_PRODUCT/factory/ENVELOPE.env" <<'ENVELOPE'
PER_RUN_BUDGET_USD=1.00
PER_TICKET_BUDGET_USD=2.00
PER_RUN_MAX_TURNS=5
PER_RUN_TIMEOUT_MIN=1
DAILY_CAP_USD=10.00
ENVELOPE
printf '%s\n' \
  "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version" \
  > "$LAUNCH_PRODUCT/factory/ledger.csv"
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' 'MAX_CONCURRENT_TICKETS=2' \
  > "$LAUNCH_PRODUCT/factory/PROJECT.env"
git -C "$LAUNCH_PRODUCT" add factory/tickets/T-77{7,8,9}.md \
  factory/tickets/T-780.md factory/initiatives/I-777.md \
  factory/ENVELOPE.env factory/ledger.csv factory/PROJECT.env factory/KIT_PIN
git -C "$LAUNCH_PRODUCT" commit -qm "seed contract 1.2 ticket"
git -C "$LAUNCH_PRODUCT" push -q origin main
write_active "$SHA_C" "$REAL_TREE" "$RELEASE_C"
ACTIVE_SNAPSHOT_TMP="$(cd "$TMP/launcher-tmp" && pwd -P)"
ACTIVE_SNAPSHOT_MARKER="$ACTIVE_SNAPSHOT_TMP/active-parsed.marker"
ACTIVE_SNAPSHOT_GATE="$ACTIVE_SNAPSHOT_TMP/active-parsed.gate"
export FACTORY_LAUNCH_TEST_ACTIVE_PARSED_MARKER="$ACTIVE_SNAPSHOT_MARKER"
export FACTORY_LAUNCH_TEST_ACTIVE_PARSED_GATE="$ACTIVE_SNAPSHOT_GATE"
run_launcher launchtest contract --json > "$TMP/active-snapshot.json" &
ACTIVE_SNAPSHOT_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $ACTIVE_SNAPSHOT_PID"
for _try in $(seq 1 200); do
  [[ -e "$ACTIVE_SNAPSHOT_MARKER" ]] && break
  sleep 0.02
done
[[ -e "$ACTIVE_SNAPSHOT_MARKER" ]] || fail "active snapshot race never reached the parse gate"
python3 - "$KITS_ROOT/projects/launchtest/active.json" <<'PY'
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["receipt_id"] = "0" * 64
value["product_tree"] = "0" * 40
temporary = path.with_suffix(".race")
temporary.write_text(json.dumps(value) + "\n")
os.replace(temporary, path)
PY
touch "$ACTIVE_SNAPSHOT_GATE"
ACTIVE_SNAPSHOT_RC=0
wait "$ACTIVE_SNAPSHOT_PID" || ACTIVE_SNAPSHOT_RC=$?
BACKGROUND_PIDS=""
unset FACTORY_LAUNCH_TEST_ACTIVE_PARSED_MARKER FACTORY_LAUNCH_TEST_ACTIVE_PARSED_GATE
rm -f "$ACTIVE_SNAPSHOT_MARKER" "$ACTIVE_SNAPSHOT_GATE"
[[ "$ACTIVE_SNAPSHOT_RC" -eq 0 ]] || fail "active snapshot changed after its single parse"
write_active "$SHA_C" "$REAL_TREE" "$RELEASE_C"
ACTIVE_RECEIPT_ID="$(python3 - "$KITS_ROOT/projects/launchtest/active.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["receipt_id"])
PY
)"
ACTIVE_RECEIPT="$KITS_ROOT/receipts/$ACTIVE_RECEIPT_ID.json"
cp "$ACTIVE_RECEIPT" "$ACTIVE_RECEIPT.saved"
python3 - "$ACTIVE_RECEIPT" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["project"] = "tampered-project"
path.write_text(json.dumps(value) + "\n")
PY
chmod 600 "$ACTIVE_RECEIPT"
TAMPERED_RECEIPT_RC=0
run_launcher launchtest contract --json > "$TMP/tampered-receipt.out" 2>&1 ||
  TAMPERED_RECEIPT_RC=$?
[[ "$TAMPERED_RECEIPT_RC" -eq 1 ]] || fail "launcher accepted a mismatched active receipt binding"
mv "$ACTIVE_RECEIPT.saved" "$ACTIVE_RECEIPT"
cp "$ACTIVE_RECEIPT" "$ACTIVE_RECEIPT.saved"
python3 - "$ACTIVE_RECEIPT" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["product_tree"] = "0" * 40
path.write_text(json.dumps(value) + "\n")
PY
chmod 600 "$ACTIVE_RECEIPT"
TAMPERED_PRODUCT_TREE_RC=0
run_launcher launchtest contract --json > "$TMP/tampered-product-tree.out" 2>&1 ||
  TAMPERED_PRODUCT_TREE_RC=$?
[[ "$TAMPERED_PRODUCT_TREE_RC" -eq 1 ]] || fail "launcher accepted a receipt for a different product tree"
mv "$ACTIVE_RECEIPT.saved" "$ACTIVE_RECEIPT"
NO_WORKDIR_V12_RC=0
run_launcher launchtest next-stage --ticket T-777 --json \
  > "$TMP/next-stage-v12-no-workdir.out" 2>&1 || NO_WORKDIR_V12_RC=$?
[[ "$NO_WORKDIR_V12_RC" -eq 1 ]] || fail "contract 1.2 accepted next-stage without workdir"
RELEASE_C_PHYS="$(cd "$RELEASE_C" && pwd -P)"
REAL_RUN_WORKTREE="$TMP/real-run-worktree"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-777 "$REAL_RUN_WORKTREE"
REAL_RUN_WORKTREE_PHYS="$(cd "$REAL_RUN_WORKTREE" && pwd -P)"
[[ -z "$(git -C "$REAL_RUN_WORKTREE_PHYS" ls-remote --heads origin \
  refs/heads/ticket/T-777)" ]] || fail "fresh ticket branch already existed remotely"
REAL_RUN_WORKTREE_778="$TMP/real-run-worktree-778"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-778 "$REAL_RUN_WORKTREE_778"
REAL_RUN_WORKTREE_778_PHYS="$(cd "$REAL_RUN_WORKTREE_778" && pwd -P)"
REAL_RUN_WORKTREE_779="$TMP/real-run-worktree-779"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-779 "$REAL_RUN_WORKTREE_779"
REAL_RUN_WORKTREE_779_PHYS="$(cd "$REAL_RUN_WORKTREE_779" && pwd -P)"
REAL_RUN_WORKTREE_780="$TMP/real-run-worktree-780"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-780 "$REAL_RUN_WORKTREE_780"
REAL_RUN_WORKTREE_780_PHYS="$(cd "$REAL_RUN_WORKTREE_780" && pwd -P)"

printf '\nUncommitted-Evidence: forged\n' >> \
  "$REAL_RUN_WORKTREE_PHYS/factory/tickets/T-777.md"
DIRTY_PREFLIGHT_RC=0
run_launcher launchtest preflight --ticket T-777 \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json >/dev/null 2>&1 ||
  DIRTY_PREFLIGHT_RC=$?
DIRTY_NEXT_STAGE_RC=0
run_launcher launchtest next-stage --ticket T-777 \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json >/dev/null 2>&1 ||
  DIRTY_NEXT_STAGE_RC=$?
[[ "$DIRTY_PREFLIGHT_RC" -eq 1 && "$DIRTY_NEXT_STAGE_RC" -eq 1 ]] ||
  fail "contract 1.2 accepted dirty tracked ticket evidence"
git -C "$REAL_RUN_WORKTREE_PHYS" restore factory/tickets/T-777.md
git -C "$REAL_RUN_WORKTREE_PHYS" config status.showUntrackedFiles no
touch "$REAL_RUN_WORKTREE_PHYS/untracked-evidence.txt"
DIRTY_UNTRACKED_RC=0
run_launcher launchtest next-stage --ticket T-777 \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json >/dev/null 2>&1 ||
  DIRTY_UNTRACKED_RC=$?
[[ "$DIRTY_UNTRACKED_RC" -eq 1 ]] ||
  fail "contract 1.2 accepted hidden untracked ticket evidence"
rm "$REAL_RUN_WORKTREE_PHYS/untracked-evidence.txt"
git -C "$REAL_RUN_WORKTREE_PHYS" config --unset status.showUntrackedFiles
WORKTREE_INDEX="$(git -C "$REAL_RUN_WORKTREE_PHYS" rev-parse --path-format=absolute \
  --git-path index)"
cp "$WORKTREE_INDEX" "$TMP/ticket-index.saved"
printf 'corrupt index\n' > "$WORKTREE_INDEX"
CORRUPT_INDEX_RC=0
run_launcher launchtest next-stage --ticket T-777 \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json >/dev/null 2>&1 ||
  CORRUPT_INDEX_RC=$?
[[ "$CORRUPT_INDEX_RC" -eq 1 ]] ||
  fail "contract 1.2 accepted an uninspectable ticket worktree"
mv "$TMP/ticket-index.saved" "$WORKTREE_INDEX"

run_launcher launchtest ticket-state --ticket T-777 --workdir "$REAL_RUN_WORKTREE_PHYS" \
  --action materialize --json > "$TMP/real-ticket-state.json"
BOOTSTRAP_HEAD="$(git -C "$REAL_RUN_WORKTREE_PHYS" rev-parse HEAD)"
[[ "$(git --git-dir="$LAUNCH_PRODUCT_REMOTE" rev-parse refs/heads/ticket/T-777)" == \
     "$BOOTSTRAP_HEAD" &&
   "$(git -C "$REAL_RUN_WORKTREE_PHYS" rev-parse \
     refs/remotes/origin/ticket/T-777)" == "$BOOTSTRAP_HEAD" ]] ||
  fail "ticket-state did not create and verify the fresh remote ticket branch"
run_launcher launchtest ticket-state --ticket T-778 --workdir "$REAL_RUN_WORKTREE_778_PHYS" \
  --action materialize --json > "$TMP/real-ticket-state-778.json"
run_launcher launchtest ticket-state --ticket T-779 --workdir "$REAL_RUN_WORKTREE_779_PHYS" \
  --action materialize --json > "$TMP/real-ticket-state-779.json"
run_launcher launchtest ticket-state --ticket T-780 --workdir "$REAL_RUN_WORKTREE_780_PHYS" \
  --action materialize --json > "$TMP/real-ticket-state-780.json"

assert_bad_real_preflight() {
  local label="$1" rc=0
  shift
  run_launcher launchtest preflight --ticket T-777 "$@" \
    --workdir "$REAL_RUN_WORKTREE_PHYS" --json \
    > "$TMP/real-preflight-$label.json" || rc=$?
  [[ "$rc" -eq 1 ]] || fail "real preflight accepted $label dispatcher lease"
  python3 - "$TMP/real-preflight-$label.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["status"] == "error"
assert "dispatcher lease" in data["output"]
PY
}
assert_bad_real_preflight missing
assert_bad_real_preflight wrong \
  --lease 0000000000000000000000000000000000000000000000000000000000000000

mkdir -p "$TEST_HOME/.factory/bin"
cat > "$TEST_HOME/.factory/bin/timeout" <<'STUB'
#!/usr/bin/env bash
shift
exec "$@"
STUB
cat > "$TEST_HOME/.factory/bin/codex" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version)
    echo "codex-cli 0.144.1"
    ;;
  login)
    [[ "${2:-}" == "status" ]]
    ;;
  exec)
    if [[ "${2:-}" == "--help" ]]; then
      echo "usage: codex exec --json --model"
    else
      echo '{"type":"turn.completed","input_tokens":10,"output_tokens":5,"message":"trusted sealed test stub"}'
    fi
    ;;
  *)
    exit 2
    ;;
esac
STUB
chmod +x "$TEST_HOME/.factory/bin/timeout" "$TEST_HOME/.factory/bin/codex"

assert_bad_real_run_lease() {
  local label="$1" rc=0
  shift
  run_launcher launchtest run \
    --role planner --ticket T-777 "$@" \
    --prompt-file "$RELEASE_C_PHYS/roles/planner.md" \
    --workdir "$REAL_RUN_WORKTREE_PHYS" -- "must not reach adapter" \
    > "$TMP/real-run-$label.out" 2>&1 || rc=$?
  [[ "$rc" -eq 7 ]] || fail "real run accepted $label dispatcher lease"
  ! grep -qF "mock adapter ran task" "$TMP/real-run-$label.out" ||
    fail "real run reached adapter with $label dispatcher lease"
}

# First prove that two concurrent launch requests serialize provider execution
# and cannot exceed one dollar of capacity. Runtime manifests, not the
# projection-only durable ledger, are authoritative under contract 1.2.
run_launcher launchtest claim --ticket T-779 > "$TMP/real-claim-779.json"
run_launcher launchtest claim --ticket T-780 > "$TMP/real-claim-780.json"
REAL_LEASE_779="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/real-claim-779.json")"
REAL_LEASE_780="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/real-claim-780.json")"
run_launcher launchtest preflight --ticket T-779 --lease "$REAL_LEASE_779" \
  --workdir "$REAL_RUN_WORKTREE_779_PHYS" --json > "$TMP/real-preflight-779.json"
run_launcher launchtest preflight --ticket T-780 --lease "$REAL_LEASE_780" \
  --workdir "$REAL_RUN_WORKTREE_780_PHYS" --json > "$TMP/real-preflight-780.json"
run_launcher launchtest ticket-state --ticket T-779 --workdir "$REAL_RUN_WORKTREE_779_PHYS" \
  --action transition --state Planning --json > "$TMP/real-ticket-transition-779.json"
run_launcher launchtest ticket-state --ticket T-780 --workdir "$REAL_RUN_WORKTREE_780_PHYS" \
  --action transition --state Planning --json > "$TMP/real-ticket-transition-780.json"
cp "$LAUNCH_PRODUCT/factory/ENVELOPE.env" "$TMP/envelope-ten.env"
sed 's/DAILY_CAP_USD=10.00/DAILY_CAP_USD=1.00/' \
  "$TMP/envelope-ten.env" > "$LAUNCH_PRODUCT/factory/ENVELOPE.env"
rm -f "$LAUNCH_PRODUCT/factory/test-adapter-gate"
run_launcher launchtest run --role planner --ticket T-779 --lease "$REAL_LEASE_779" \
  --prompt-file "$RELEASE_C_PHYS/roles/planner.md" --workdir "$REAL_RUN_WORKTREE_779_PHYS" \
  -- budget-779 > "$TMP/budget-779.out" 2>&1 &
BUDGET_779_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $BUDGET_779_PID"
run_launcher launchtest run --role planner --ticket T-780 --lease "$REAL_LEASE_780" \
  --prompt-file "$RELEASE_C_PHYS/roles/planner.md" --workdir "$REAL_RUN_WORKTREE_780_PHYS" \
  -- budget-780 > "$TMP/budget-780.out" 2>&1 &
BUDGET_780_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $BUDGET_780_PID"
for _try in $(seq 1 1000); do
  started=0
  [[ -e "$REAL_RUN_WORKTREE_779_PHYS/.factory-test-adapter-started" ]] && started=$((started + 1))
  [[ -e "$REAL_RUN_WORKTREE_780_PHYS/.factory-test-adapter-started" ]] && started=$((started + 1))
  [[ "$started" -eq 1 ]] && break
  sleep 0.02
done
if [[ "$started" -ne 1 ]]; then
  sed 's/^/T-779: /' "$TMP/budget-779.out" >&2
  sed 's/^/T-780: /' "$TMP/budget-780.out" >&2
  fail "near-cap fixture did not reach exactly one task adapter"
fi
BUDGET_779_RC=0 BUDGET_780_RC=0
touch "$LAUNCH_PRODUCT/factory/test-adapter-gate"
wait "$BUDGET_779_PID" || BUDGET_779_RC=$?
wait "$BUDGET_780_PID" || BUDGET_780_RC=$?
BACKGROUND_PIDS=""
rm -f "$LAUNCH_PRODUCT/factory/test-adapter-gate"
started=0
[[ -e "$REAL_RUN_WORKTREE_779_PHYS/.factory-test-adapter-started" ]] && started=$((started + 1))
[[ -e "$REAL_RUN_WORKTREE_780_PHYS/.factory-test-adapter-started" ]] && started=$((started + 1))
[[ "$started" -eq 1 ]] || fail "near-cap loser reached the serialized provider interval"
if [[ ! ( "$BUDGET_779_RC" -eq 0 && "$BUDGET_780_RC" -eq 5 ) &&
      ! ( "$BUDGET_779_RC" -eq 5 && "$BUDGET_780_RC" -eq 0 ) ]]; then
  printf 'near-cap statuses: T-779=%s T-780=%s\n' "$BUDGET_779_RC" "$BUDGET_780_RC" >&2
  sed 's/^/T-779: /' "$TMP/budget-779.out" >&2
  sed 's/^/T-780: /' "$TMP/budget-780.out" >&2
  fail "concurrent near-cap reservations were not atomic"
fi
python3 - "$LAUNCH_PRODUCT/factory/runs" <<'PY'
import sys
from pathlib import Path

records = []
for path in Path(sys.argv[1]).glob("*.meta"):
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    if values.get("ticket") in {"T-779", "T-780"}:
        records.append(values)
assert len(records) == 2, records
assert sum(value.get("go_issued") == "1" for value in records) == 1, records
assert sorted(value.get("accounting_state") for value in records) == ["completed", "launch_void"], records
PY
run_launcher launchtest release --ticket T-779 --lease "$REAL_LEASE_779" >/dev/null
run_launcher launchtest release --ticket T-780 --lease "$REAL_LEASE_780" >/dev/null
cp "$TMP/envelope-ten.env" "$LAUNCH_PRODUCT/factory/ENVELOPE.env"

# Then prove inherited two-ticket execution and failure isolation with the
# normal cap restored and the exact contract 1.2 worktrees supplied.
run_launcher launchtest claim --ticket T-777 > "$TMP/real-claim.json"
REAL_LEASE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/real-claim.json")"
run_launcher launchtest claim --ticket T-778 > "$TMP/real-claim-778.json"
REAL_LEASE_778="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/real-claim-778.json")"
if ! run_launcher launchtest preflight --ticket T-777 --lease "$REAL_LEASE_ID" \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json > "$TMP/real-preflight.json"; then
  awk '{print}' "$TMP/real-preflight.json" >&2
  git -C "$LAUNCH_PRODUCT" status --short >&2
  git -C "$REAL_RUN_WORKTREE_PHYS" status --short >&2
  fail "contract 1.2 sealed preflight failed"
fi
run_launcher launchtest preflight --ticket T-778 --lease "$REAL_LEASE_778" \
  --workdir "$REAL_RUN_WORKTREE_778_PHYS" --json > "$TMP/real-preflight-778.json"
run_launcher launchtest ticket-state --ticket T-777 --workdir "$REAL_RUN_WORKTREE_PHYS" \
  --action transition --state Planning --json > "$TMP/real-ticket-transition.json"
run_launcher launchtest ticket-state --ticket T-778 --workdir "$REAL_RUN_WORKTREE_778_PHYS" \
  --action transition --state Planning --json > "$TMP/real-ticket-transition-778.json"
run_launcher launchtest next-stage --ticket T-777 --lease "$REAL_LEASE_ID" \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json > "$TMP/real-next-stage.json"
python3 - "$TMP/real-ticket-transition.json" "$TMP/real-preflight.json" \
  "$TMP/real-next-stage.json" <<'PY'
import json, sys
state, preflight, stage = [json.load(open(path, encoding="utf-8")) for path in sys.argv[1:]]
assert state["ticket"] == "T-777" and state["state"] == "Planning"
assert preflight["status"] == "ok"
assert stage["status"] == "ok"
assert stage["action"] == "RUN"
assert stage["detail"] == "planner"
assert stage["output"] == "RUN planner\n"
PY
assert_bad_real_run_lease missing
assert_bad_real_run_lease wrong \
  --lease 0000000000000000000000000000000000000000000000000000000000000000

rm -f "$LAUNCH_PRODUCT/factory/test-adapter-gate"
run_launcher launchtest run \
  --role planner --ticket T-777 --lease "$REAL_LEASE_ID" \
  --prompt-file "$RELEASE_C_PHYS/roles/planner.md" \
  --workdir "$REAL_RUN_WORKTREE_PHYS" -- overlap-success \
  > "$TMP/real-run.txt" 2>&1 &
REAL_RUN_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $REAL_RUN_PID"
run_launcher launchtest run \
  --role planner --ticket T-778 --lease "$REAL_LEASE_778" \
  --prompt-file "$RELEASE_C_PHYS/roles/planner.md" \
  --workdir "$REAL_RUN_WORKTREE_778_PHYS" -- overlap-fail \
  > "$TMP/real-run-778.txt" 2>&1 &
REAL_RUN_778_PID=$!
BACKGROUND_PIDS="$BACKGROUND_PIDS $REAL_RUN_778_PID"
for _try in $(seq 1 1000); do
  started=0
  [[ -e "$REAL_RUN_WORKTREE_PHYS/.factory-test-adapter-started" ]] && started=$((started + 1))
  [[ -e "$REAL_RUN_WORKTREE_778_PHYS/.factory-test-adapter-started" ]] && started=$((started + 1))
  [[ "$started" -eq 1 ]] && break
  sleep 0.02
done
if [[ "$started" -ne 1 ]] ||
   ! kill -0 "$REAL_RUN_PID" 2>/dev/null || ! kill -0 "$REAL_RUN_778_PID" 2>/dev/null; then
  fail "serialized role runs did not keep one provider active and one queued"
fi
[[ -f "$LAUNCH_PRODUCT/factory/.dispatch-leases/T-777.json" &&
   -f "$LAUNCH_PRODUCT/factory/.dispatch-leases/T-778.json" ]] ||
  fail "serialized providers did not preserve both dispatcher leases"
touch "$LAUNCH_PRODUCT/factory/test-adapter-gate"
REAL_RUN_RC=0 REAL_RUN_778_RC=0
wait "$REAL_RUN_PID" || REAL_RUN_RC=$?
wait "$REAL_RUN_778_PID" || REAL_RUN_778_RC=$?
BACKGROUND_PIDS=""
rm -f "$LAUNCH_PRODUCT/factory/test-adapter-gate"
[[ -e "$REAL_RUN_WORKTREE_PHYS/.factory-test-adapter-started" &&
   -e "$REAL_RUN_WORKTREE_778_PHYS/.factory-test-adapter-started" ]] ||
  fail "queued role did not run after the active provider exited"
[[ "$REAL_RUN_RC" -eq 0 && "$REAL_RUN_778_RC" -eq 42 ]] ||
  fail "one failed serialized role run affected its peer"
[[ "$(git -C "$REAL_RUN_WORKTREE_PHYS" branch --show-current)" == "ticket/T-777" &&
   "$(git -C "$REAL_RUN_WORKTREE_778_PHYS" branch --show-current)" == "ticket/T-778" ]] ||
  fail "serialized role runs did not retain exact ticket branches"
[[ -f "$LAUNCH_PRODUCT/factory/.dispatch-leases/T-777.json" &&
   -f "$LAUNCH_PRODUCT/factory/.dispatch-leases/T-778.json" ]] ||
  fail "one failed role run invalidated a dispatcher lease"
grep -qF "mock adapter ran task: overlap-success" "$TMP/real-run.txt" ||
  fail "successful serialized role did not complete"

REAL_MANIFEST="$(awk -F= '$1=="ticket" && $2=="T-777" {print FILENAME}' \
  "$LAUNCH_PRODUCT/factory/runs/"*.meta | tail -1)"
grep -qF "kit_sha=$SHA_C" "$REAL_MANIFEST" || fail "real sealed run manifest omitted release SHA"
grep -qF "kit_tree=$REAL_TREE" "$REAL_MANIFEST" || fail "real sealed run manifest omitted release tree"
grep -qF "contract_version=1.4.0" "$REAL_MANIFEST" || fail "real sealed run manifest omitted release contract"
grep -qF "physical_kit_path=$RELEASE_C_PHYS" "$REAL_MANIFEST" || fail "real sealed run manifest omitted physical release path"
grep -qF "role=planner" "$REAL_MANIFEST" || fail "real sealed run did not use the sequencer-authorized role"
grep -qF "adapter=mock" "$REAL_MANIFEST" || fail "isolated launcher did not enforce the mock adapter"
assert_no_secret "$TMP/real-run.txt"
assert_no_secret "$TMP/real-run-778.txt"
if grep -Fq "$REAL_LEASE_ID" "$REAL_MANIFEST" "$TMP/real-run.txt" \
     "$LAUNCH_PRODUCT/factory/ledger.csv" "$LAUNCH_PRODUCT/factory/tickets/T-777.md" \
     "$RELEASE_C_PHYS/roles/planner.md" ||
   grep -Fq "$REAL_LEASE_778" "$LAUNCH_PRODUCT/factory/runs/"*.meta \
     "$TMP/real-run-778.txt" "$LAUNCH_PRODUCT/factory/ledger.csv" \
     "$LAUNCH_PRODUCT/factory/tickets/T-778.md" "$RELEASE_C_PHYS/roles/planner.md"; then
  fail "opaque dispatcher lease reached a prompt, manifest, output, ledger, or ticket artifact"
fi
run_launcher launchtest release --ticket T-777 --lease "$REAL_LEASE_ID" >/dev/null
run_launcher launchtest release --ticket T-778 --lease "$REAL_LEASE_778" >/dev/null

REAL_CLOSEOUT_WORKTREE="$TMP/real-closeout-worktree"
git -C "$LAUNCH_PRODUCT" worktree add -q -b chore/t777-closeout \
  "$REAL_CLOSEOUT_WORKTREE" origin/main
REAL_CLOSEOUT_WORKTREE_PHYS="$(cd "$REAL_CLOSEOUT_WORKTREE" && pwd -P)"
run_launcher launchtest project-ledger --ticket T-777 \
  --workdir "$REAL_CLOSEOUT_WORKTREE_PHYS" --json > "$TMP/real-project-ledger.json"
python3 - "$TMP/real-project-ledger.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["schema"] == "nysa.software-factory.ledger-projection/v1"
assert data["status"] == "ok" and data["ticket"] == "T-777"
assert data["row_count"] >= 1
PY
PROJECT_WRONG_BRANCH_RC=0
run_launcher launchtest project-ledger --ticket T-777 \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json \
  > "$TMP/project-ledger-wrong-branch.out" 2>&1 || PROJECT_WRONG_BRANCH_RC=$?
[[ "$PROJECT_WRONG_BRANCH_RC" -eq 1 ]] || fail "project-ledger accepted a ticket branch"
PROJECT_DIRTY_RC=0
run_launcher launchtest project-ledger --ticket T-777 \
  --workdir "$REAL_CLOSEOUT_WORKTREE_PHYS" --json \
  > "$TMP/project-ledger-dirty.out" 2>&1 || PROJECT_DIRTY_RC=$?
[[ "$PROJECT_DIRTY_RC" -eq 1 ]] || fail "project-ledger accepted a dirty close-out worktree"
[[ "$(cksum "$TMP/bypass-envelope.env")" == "$BYPASS_ENVELOPE_BEFORE" ]] ||
  fail "caller FACTORY_ENVELOPE bypass was consumed or modified"
[[ "$(cksum "$TMP/bypass-global.env")" == "$BYPASS_GLOBAL_BEFORE" ]] ||
  fail "caller FACTORY_GLOBAL_ENV bypass was consumed or modified"
[[ "$(cksum "$TMP/bypass-ledger.csv")" == "$BYPASS_LEDGER_BEFORE" ]] ||
  fail "caller FACTORY_LEDGER bypass was consumed or modified"

python3 - "$ROOT" "$CONTRACT" <<'PY'
import json
import os
import plistlib
import re
import sys

root, contract_path = sys.argv[1:]
with open(contract_path, encoding="utf-8") as handle:
    contract = json.load(handle)

assert contract["contract"] == "nysa.software-factory.hermes"
assert contract["contract_version"] == "1.4.0"
assert contract["doctor_schema"] == "nysa.software-factory.hermes-doctor/v1"
assert contract["preflight_schema"] == "nysa.software-factory.preflight/v1"
assert contract["next_stage_schema"] == "nysa.software-factory.next-stage/v1"
assert contract["status_categories"] == ["ok", "warning", "error", "unknown"]
assert contract["exit_codes"] == {
    "0": "no error-category checks",
    "1": "one or more error-category checks",
    "2": "invalid invocation",
}
assert contract["supported_hermes"] == [{
    "agent_version": "0.18.2",
    "build_version": "2026.7.7.2",
    "compatibility": "certified",
}]
assert contract["launcher"]["path"] == "~/.factory/bin/factory-launch"
assert contract["launcher"]["source"] == "integrations/hermes/bin/factory-launch"
commands = contract["launcher"]["commands"]
assert commands["contract"]["arguments"] == ["--json"]
assert commands["doctor"]["output_schema"] == contract["doctor_schema"]
assert commands["models"]["minimum_contract_version"] == "1.2.0"
assert commands["models"]["helper"] == "scripts/model-control.sh"
assert commands["models"]["grammars"] == [
    "profiles --json",
    "status --json",
    "plan --json",
    "plan --profile <safe-id> --json",
    "activate --profile <safe-id> --approve-hash <lowercase-sha256> --approved-by <safe-id> --json",
    "disable --scope-type <account-route|provider-family|model|route> --scope-id <safe-selection-or-id> --reason credits_exhausted --ttl-seconds <1..604800> --operator-id <safe-id> --json",
    "enable --scope-type <account-route|provider-family|model|route> --scope-id <safe-selection-or-id> --json",
    "pin --ticket <T-NNN> --workdir <exact-ticket-worktree> --json",
    "migrate-plan --ticket <T-NNN> --workdir <exact-clean-ticket-worktree> --json",
    "migrate --ticket <T-NNN> --workdir <exact-clean-ticket-worktree> --approve-hash <lowercase-sha256> --approved-by <safe-id> --json",
    "fallback-plan --ticket <T-NNN> --failed-run <safe-run-id> --workdir <exact-ticket-worktree> --reason <credits_exhausted|provider_unavailable> --json",
    "fallback --ticket <T-NNN> --failed-run <safe-run-id> --workdir <exact-ticket-worktree> --reason <credits_exhausted|provider_unavailable> --json",
]
assert commands["models"]["state"] == {
    "root": "$FACTORY_KITS_ROOT/projects",
    "project": "<project>",
    "active_profile": "$FACTORY_KITS_ROOT/projects/<project>/routing/active.json",
    "temporary_overrides": "$FACTORY_KITS_ROOT/projects/<project>/routing/overrides.json",
    "ticket_route_plan": "<ticket-worktree>/factory/route-plans/<T-NNN>.json (v1 immutable plan or v2 append-only journal)",
    "isolation": "active profiles and temporary overrides are selected only from the validated launcher project",
}
assert commands["models"]["output_schemas"]["activate"] == "model-routing-active/v1"
assert commands["models"]["output_schemas"]["pin"].endswith(
    "commit_created, commit_sha, and pin_hash"
)
assert commands["models"]["pin_transaction"]["result_fields"] == [
    "commit_created", "commit_sha", "pin_hash"
]
assert commands["models"]["maintenance"] == {
    "allowed": ["profiles", "status", "plan", "migrate-plan", "fallback-plan"],
    "refused": ["activate", "disable", "enable", "pin", "migrate", "fallback"],
}
assert commands["preflight"]["arguments"][-1] == "--json"
assert commands["next-stage"]["arguments"][-1] == "--json"
assert commands["preflight"]["arguments"][-3:] == [
    "--workdir", "<absolute-product-worktree>", "--json"
]
assert commands["next-stage"]["arguments"][-3:] == [
    "--workdir", "<absolute-product-worktree>", "--json"
]
assert commands["next-stage"]["contract_1_3_terminal_action"].startswith(
    "COMPLETE means"
)
assert commands["ticket-state"]["arguments"] == [
    "--ticket", "<T-NNN>", "--workdir", "<absolute-product-worktree>",
    "--action", "<materialize|transition>", "[--state <ticket-state>]", "--json"
]
assert commands["ticket-state"]["transition_states"] == [
    "Planning", "Building", "Review", "Blocked-Escalated"
]
assert commands["ticket-attest"]["arguments"] == [
    "--ticket", "<T-NNN>", "[--lease <opaque-lease-id>]",
    "--workdir", "<absolute-worktree>",
    "--action", "<bundle|approval|done>", "--json"
]
assert any("closeout PR" in item and "protected auto-merge" in item
           for item in commands["ticket-attest"]["validation"])
assert commands["project-ledger"]["arguments"] == [
    "--ticket", "<T-NNN>", "--workdir", "<absolute-closeout-worktree>", "--json"
]
assert commands["project-ledger"]["output_schema"] == \
    "nysa.software-factory.ledger-projection/v1"
assert commands["claim"]["arguments"] == ["--ticket", "<T-NNN>"]
assert commands["renew"]["arguments"][-2:] == ["--lease", "<opaque-lease-id>"]
assert commands["release"]["arguments"][-2:] == ["--lease", "<opaque-lease-id>"]
assert contract["concurrency"]["default"] == 1
assert contract["concurrency"]["maximum"] == 2
assert commands["reorder-test-fixes"]["arguments"] == [
    "--ticket",
    "<T-NNN>",
    "--workdir",
    "<absolute-product-worktree>",
    "--",
    "<arguments for reorder-test-fixes.sh>",
]
assert commands["reorder-test-fixes"]["ticket_branch"] == \
    "same policy as launcher.commands.run.ticket_branch"
assert contract["launcher"]["trust_root"] == {
    "account_home_source": "pwd.getpwuid(os.getuid()).pw_dir",
    "installed_physical_path": "<account-home>/.factory/bin/factory-launch",
    "production_kits_root": "<account-home>/.factory/kits",
    "production_profile_root": "<account-home>/.hermes/profiles/factory",
    "caller_home_and_root_overrides": "ignored or refused",
    "repository_test_mode": "explicit isolated roots allowed only from a non-installed launcher path",
}
assert commands["run"]["role_whitelist"] == [
    "planner",
    "spec-linter",
    "test-author",
    "builder",
    "reviewer",
    "narrator",
]
assert commands["run"]["ticket_branch"] == {
    "descriptor": "PRODUCT_ROOT/factory/PROJECT.env",
    "key": "TICKET_BRANCH_PREFIX",
    "default_prefix": "ticket/",
    "expected": "<prefix><T-NNN>",
}
assert contract["launcher"]["helper_environment"] == {
    "FACTORY_RELEASE_SHA": "active record kit_sha",
    "FACTORY_RELEASE_TREE": "active record kit_tree",
    "FACTORY_RELEASE_PATH": "resolved physical release path",
    "FACTORY_RELEASE_CONTRACT_VERSION": "active record contract_version",
    "FACTORY_MODEL_STATE_ROOT": "resolved production kits projects directory",
    "FACTORY_PROJECT": "validated launcher project slug",
    "FACTORY_CERTIFIED_PRODUCT_ORIGIN": "contract 1.2+ certification receipt product_origin; consumed by trusted write helpers and never exposed to adapters",
    "FACTORY_DISPATCH_LEASE_ID": "validated optional ticket lease supplied by the dispatcher",
}
assert contract["launcher"]["helper_environment_allowlist"] == [
    "HOME",
    "PATH",
    "TMPDIR",
    "FACTORY_ROOT",
    "FACTORY_RELEASE_SHA",
    "FACTORY_RELEASE_TREE",
    "FACTORY_RELEASE_PATH",
    "FACTORY_RELEASE_CONTRACT_VERSION",
    "FACTORY_MODEL_STATE_ROOT",
    "FACTORY_PROJECT",
    "FACTORY_CERTIFIED_PRODUCT_ORIGIN",
    "FACTORY_DISPATCH_LEASE_ID",
    "GH_TOKEN",
]
assert contract["launcher"]["helper_optional_credentials"]["GH_TOKEN"] == {
    "source": "$HERMES_FACTORY_PROFILE/.env",
    "assignment": "exactly one optional GH_TOKEN assignment parsed as data",
    "file_policy": "owner-owned regular non-symlink file with mode 0600",
    "caller_value_ignored": True,
}
assert contract["launcher"]["helper_safe_path"] == \
    "$HOME/.factory/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
assert contract["launcher"]["managed_path_policy"] == \
    "reject every symlink component before physical resolution"
assert contract["profile"]["required_registry_keys"] == ["PRODUCT_ROOT"]
assert contract["profile"]["observed_ignored_registry_keys"] == ["KIT_DIR"]
assert contract["launcher"]["active_record"]["required_fields"] == [
    "project",
    "kit_sha",
    "kit_tree",
    "contract_version",
    "product_path",
    "release_path",
]
assert contract["launcher"]["active_record"]["contract_1_2_required_fields"] == [
    "receipt_id",
    "product_tree",
]
assert "receipt_id" in contract["launcher"]["active_record"]["contract_1_2_receipt_binding"]
assert "product path/tree" in contract["launcher"]["active_record"]["contract_1_2_receipt_binding"]
for surface in [
    "scripts/model-control.sh",
    "scripts/model-manager.py",
    "scripts/model-router.py",
    "scripts/lib/backend-policy.sh",
    "scripts/lib/kit-pin.sh",
    "scripts/lib/plain-config.sh",
    "scripts/lib/product-remote.sh",
    "scripts/lib/legacy_closeout.py",
    "scripts/lib/terminal_backfill.py",
    "scripts/legacy-closeout.py",
    "scripts/terminal-backfill.py",
    "scripts/model-routing/catalog-v1.json",
    "scripts/model-routing/profiles-v1.json",
    "factory/route-plans/<T-NNN>.json",
]:
    assert surface in contract["compatibility_sensitive_surfaces"], surface

integration = os.path.join(root, "integrations", "hermes")
required = [
    "contract.json",
    "CHANGELOG.md",
    "bin/factory-launch",
    "fixtures/factory-profile.json",
    "fixtures/projects/relay.env",
    "templates/profile/SOUL.md",
    "templates/profile/skills/factory-dispatch/SKILL.md",
    "templates/launchd/com.nysa.hermes-factory-gateway.plist",
    "templates/launchd/com.nysa.hermes-dashboard.plist",
]
for relative in required:
    assert os.path.isfile(os.path.join(integration, relative)), relative
assert os.access(os.path.join(integration, "bin/factory-launch"), os.X_OK)

changelog = open(os.path.join(integration, "CHANGELOG.md"), encoding="utf-8").read()
assert "## 1.4.0" in changelog and "## 1.3.0" in changelog and "## 1.2.0" in changelog and "## 1.1.0" in changelog and "## 1.0.0" in changelog
assert "0.18.2" in changelog and "2026.7.7.2" in changelog

for relative in [
    "templates/profile/SOUL.md",
    "templates/profile/skills/factory-dispatch/SKILL.md",
]:
    text = open(os.path.join(integration, relative), encoding="utf-8").read()
    assert "~/.factory/bin/factory-launch" in text
    assert " contract --json" in text
    assert " doctor --json" in text
    assert not re.search(r"(?:^|\s)(?:~/[^ ]*/)?scripts/(?:run-agent|preflight|next-stage)\.sh", text)
for relative in (
    "README.md",
    "docs/runbooks/operator.md",
    "conformance/SHAKEDOWN-REPORT.md",
):
    text = open(os.path.join(root, relative), encoding="utf-8").read()
    assert "~/.factory/bin/factory-launch" in text
    assert not re.search(
        r"`(?:scripts/)?(?:run-agent|preflight|next-stage)\.sh(?:`|\s)", text,
    )
skill = open(
    os.path.join(integration, "templates/profile/skills/factory-dispatch/SKILL.md"),
    encoding="utf-8",
).read()
assert "factory-launch <project> reorder-test-fixes" in skill
assert "version: 1.4.0" in skill
assert "Contracts `1.2.0` through `1.4.0` inherit `1.1.0` lease behavior unchanged" in skill
assert "factory-launch <project> ticket-state" in skill
assert "factory-launch <project> ticket-attest" in skill
assert "factory-launch <project> project-ledger" in skill
assert "copy, reconstruct, reorder, or hand-edit ledger rows" in skill
assert "--ticket <T-NNN>" in skill
assert "--workdir <absolute-product-worktree>" in skill

soul = open(
    os.path.join(integration, "templates/profile/SOUL.md"), encoding="utf-8"
).read()
assert "Contracts `1.2.0` through `1.4.0` inherit contract `1.1.0` lease behavior unchanged" in soul
assert "preflight --ticket <T-NNN> --workdir <ticket-worktree> --json" in soul
assert "next-stage --ticket <T-NNN> --workdir <ticket-worktree> --json" in soul
assert "factory-launch <project> ticket-state" in soul
assert "factory-launch <project> ticket-attest" in soul
assert "factory-launch <project> project-ledger" in soul
assert "never hand-edit ticket state or" in soul

fixture = json.load(open(os.path.join(integration, "fixtures/factory-profile.json"), encoding="utf-8"))
assert fixture["redacted"] is True
assert fixture["secret_values_included"] is False
assert fixture["profile"]["environment_key_presence"] == {"GH_TOKEN": True}
assert fixture["services"] == [
    {"kind": "gateway", "launch_agent": "separate"},
    {"kind": "dashboard", "launch_agent": "separate"},
]

labels = []
for relative in [
    "templates/launchd/com.nysa.hermes-factory-gateway.plist",
    "templates/launchd/com.nysa.hermes-dashboard.plist",
]:
    with open(os.path.join(integration, relative), "rb") as handle:
        plist = plistlib.load(handle)
    labels.append(plist["Label"])
    env = plist.get("EnvironmentVariables", {})
    assert not any(re.search(r"key|token|secret|password|url|dsn|conn|auth", key, re.I) for key in env)
assert labels == ["com.nysa.hermes-factory-gateway", "com.nysa.hermes-dashboard"]
assert len(set(labels)) == 2
PY

echo "hermes-contract-test: all cases passed"

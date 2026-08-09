#!/usr/bin/env bash
# Sandboxed contract tests for the public Hermes integration boundary.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCTOR="$ROOT/scripts/factory-doctor.sh"
CONTRACT="$ROOT/integrations/hermes/contract.json"
LAUNCHER="$ROOT/integrations/hermes/bin/factory-launch"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/hermes-contract-test.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
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
trap 'status=$?; echo "FAIL: unexpected command at line ${BASH_LINENO[0]:-$LINENO} (exit $status)" >&2; exit "$status"' ERR
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
  grep -qFx "FACTORY_KIT_TRUST_SCOPE=production-certified" "$file" ||
    fail "installed helper did not receive certified production trust scope"
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
    FACTORY_QUALIFICATION_MANIFEST FACTORY_QUALIFICATION_PRODUCT_SHA \
    FACTORY_QUALIFICATION_PRODUCT_TREE \
    FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256 \
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
        "$contract" == "1.4.0" || "$contract" == "1.5.0" ||
        "$contract" == "1.6.0" || "$contract" == "1.7.0" ||
        "$contract" == "1.8.0" ]]; then
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
    FACTORY_QUALIFICATION_MANIFEST="$TMP/caller-qualification-bypass.json" \
    FACTORY_QUALIFICATION_PRODUCT_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    FACTORY_QUALIFICATION_PRODUCT_TREE=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
    FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
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
    LINEAR_API_KEY="$LINEAR_SECRET" \
    bash "$LAUNCHER" "$@"
}

create_test_release() {
  local release="$1" label="$2" action="$3" contract="${4:-1.6.0}"
  mkdir -p "$release/integrations/hermes/bin" "$release/scripts/lib" "$release/roles"
  printf '*.out\n' > "$release/.gitignore"
  printf 'tracked ignored release evidence\n' > "$release/tracked.out"
  cp "$CONTRACT" "$release/integrations/hermes/contract.json"
  cp "$LAUNCHER" "$release/integrations/hermes/bin/factory-launch"
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
old = 'CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-1.8.0}"'
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
  cp "$ROOT/scripts/envelope-control.py" "$release/scripts/envelope-control.py"
  cp "$ROOT/scripts/attempt-cancel.py" "$release/scripts/attempt-cancel.py"
  cp "$ROOT/scripts/operator-state.py" "$release/scripts/operator-state.py"
  cp "$ROOT/scripts/operator-event-watch.py" "$release/scripts/operator-event-watch.py"
  cp "$ROOT/scripts/ticket-attest.sh" "$release/scripts/ticket-attest.sh"
  cat > "$release/scripts/linear-sync.py" <<EOF
#!/usr/bin/env python3
import os
from pathlib import Path

root = Path(os.environ["FACTORY_ROOT"])
(root / "factory/linear-release.txt").write_text("$label\n", encoding="utf-8")
(root / "factory/linear-helper.env").write_text(
    "\n".join(sorted(os.environ)) + "\n", encoding="utf-8"
)
EOF
  cp -R "$ROOT/scripts/model-routing" "$release/scripts/model-routing"
  cp "$ROOT/scripts/lib/backend-policy.sh" "$release/scripts/lib/backend-policy.sh"
  cp "$ROOT/scripts/lib/provider-cli-version.sh" \
    "$release/scripts/lib/provider-cli-version.sh"
  cp "$ROOT/scripts/lib/cursor-model-families.txt" \
    "$release/scripts/lib/cursor-model-families.txt"
  cp "$ROOT/scripts/lib/kit-pin.sh" "$release/scripts/lib/kit-pin.sh"
  cp "$ROOT/scripts/lib/plain-config.sh" "$release/scripts/lib/plain-config.sh"
  cp "$ROOT/scripts/lib/product-remote.sh" "$release/scripts/lib/product-remote.sh"
  cp "$ROOT/scripts/lib/process-identity.py" "$release/scripts/lib/process-identity.py"
  cat > "$release/scripts/ticket-passport.py" <<'PY'
import json
import sys

print(json.dumps({"arguments": sys.argv[1:]}))
PY
  cat > "$release/scripts/ticket-attest.py" <<'PY'
import json
import sys

action = sys.argv[sys.argv.index("--action") + 1]
print(json.dumps({"action": action, "status": "ok"}))
PY
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
if [[ -z "${FACTORY_INTERNAL_BATCH_RESOLUTION:-}" ]]; then
  env | awk -F= '$1 != "GH_TOKEN"' | LC_ALL=C sort > "$ENV_OUT"
  if [[ ${GH_TOKEN+x} == x ]]; then
    printf 'GH_TOKEN_PRESENT=true\nGH_TOKEN_CKSUM=%s\n' \
      "$(printf '%s' "$GH_TOKEN" | cksum)" >> "$ENV_OUT"
  fi
fi
if [[ -e "$FACTORY_ROOT/factory/test-model-args-only" ]]; then
  if [[ "${FACTORY_GITHUB_TOKEN_FD:-}" == "9" ]]; then
    IFS= read -r PROFILE_TOKEN <&9
    printf 'GITHUB_TOKEN_FD_PRESENT=true\nGITHUB_TOKEN_FD_CKSUM=%s\n' \
      "$(printf '%s' "$PROFILE_TOKEN" | cksum)" >> "$ENV_OUT"
    unset PROFILE_TOKEN
  fi
  printf 'ARG=%s\n' "$@"
  exit 0
fi
if [[ "${1:-}" == "qualification-readiness" &&
      -e "$FACTORY_ROOT/factory/test-model-readiness-invalid" ]]; then
  printf '%s\n' '{"checks":[{"cursor_route_id":"cursor-gpt-5.6-sol-high","expected_version":"0.147.0","fallback_route_id":"codex-gpt-5.6-sol-high","installed_version":"0.148.0","reason":"version_mismatch","role":"planner","state":"INVALID"}],"profile_id":"cursor-opus-v1","readiness_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema":"nysa.software-factory.qualification-fallback-readiness/v1","status":"invalid"}'
  exit 1
fi
if [[ "${1:-}" == "qualification-readiness" &&
      -e "$FACTORY_ROOT/factory/test-model-readiness-ready" ]]; then
  printf '%s\n' '{"checks":[],"profile_id":"cursor-opus-v1","readiness_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","schema":"nysa.software-factory.qualification-fallback-readiness/v1","status":"ready"}'
  exit 0
fi
if [[ "${1:-}" == "plan" &&
      -e "$FACTORY_ROOT/factory/test-production-model-readiness-invalid" ]]; then
  printf '%s\n' '{"error":"model plan failed: profile_resolution_failed","profile_id":"cursor-opus-v1","readiness":{"codex-gpt-5.6-sol":{"adapter_version":"0.148.0","reason":"version_mismatch","reported_identity":"","state":"INVALID"}},"reason_code":"profile_resolution_failed","schema":"nysa.software-factory.model-resolution-error/v1","status":"error"}'
  exit 2
fi
if [[ "${1:-}" == "plan" &&
      -e "$FACTORY_ROOT/factory/test-production-model-readiness-unsafe" ]]; then
  printf '%s\n' '{"error":"model plan failed: profile_resolution_failed","profile_id":"cursor-opus-v1","readiness":{"codex-gpt-5.6-sol":{"adapter_version":"Authorization: Bearer DO-NOT-LEAK-A","reason":"version_mismatch","reported_identity":"connection:DO-NOT-LEAK-B","state":"INVALID"}},"reason_code":"profile_resolution_failed","schema":"nysa.software-factory.model-resolution-error/v1","status":"error"}'
  exit 2
fi
if [[ "${1:-}" == "plan" &&
      -e "$FACTORY_ROOT/factory/test-production-model-readiness-temporary" ]]; then
  printf '%s\n' '{"error":"model plan failed: profile_temporarily_unavailable","profile_id":"cursor-opus-v1","readiness":{"codex-gpt-5.6-sol":{"adapter_version":"0.147.0","reason":"authentication_unavailable","reported_identity":"","state":"UNAVAILABLE"}},"reason_code":"profile_temporarily_unavailable","schema":"nysa.software-factory.model-resolution-error/v1","status":"error"}'
  exit 2
fi
if [[ "${1:-}" == "plan" &&
      -e "$FACTORY_ROOT/factory/test-production-model-readiness-ready" ]]; then
  printf '%s\n' '{"portfolio_id":"cursor-openai-production","profile_hash":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","profile_id":"cursor-opus-v1","schema":"model-resolution-plan/v1","selections":{"builder":{},"narrator":{},"planner":{},"reviewer":{},"spec-linter":{},"test-author":{}}}'
  exit 0
fi
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/model-control-real.sh" "$@"
EOF
  chmod +x "$release/scripts/factory-doctor.sh" "$release/scripts/factory-doctor-real.sh" \
    "$release/scripts/preflight.sh" \
    "$release/scripts/next-stage.sh" "$release/scripts/run-agent.sh" \
    "$release/scripts/reorder-test-fixes.sh" "$release/scripts/model-control.sh" \
    "$release/scripts/model-control-real.sh" \
    "$release/scripts/dispatch-lease.sh" "$release/scripts/operator-event-watch.py"
  chmod +x "$release/scripts/ticket-attest.sh" "$release/scripts/linear-sync.py"
}

mkdir -p "$PROFILE/projects" "$TEST_HOME/.hermes/secrets" \
  "$TEST_HOME/.factory/.ledger.lock" "$TEST_HOME/.factory/bin"
mkdir -p "$PRODUCT/factory/runs" "$PRODUCT/factory/.launch.lock" "$STUB_BIN" \
  "$TEST_HOME/Library/LaunchAgents"
cp "$LAUNCHER" "$TEST_HOME/.factory/bin/factory-launch"
chmod 700 "$TEST_HOME/.factory/bin/factory-launch"
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
printf '%s\n' 'MAX_CONCURRENT_TICKETS=1' > "$PRODUCT/factory/PROJECT.env"
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
        "project_identity_conflict": {
            "schema": "nysa.software-factory.linear-project-identity-conflict/v1",
            "initiative": "I-001",
            "reason": "conflicting_project_identity",
            "candidates": [
                {
                    "project_id": "project-canonical",
                    "project_url": "https://linear.app/test/project/project-canonical",
                },
                {
                    "project_id": "project-duplicate",
                    "project_url": "https://linear.app/test/project/project-duplicate",
                },
            ],
            "observed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        },
        "project_identity_warnings": [{
            "schema": "nysa.software-factory.linear-project-identity-conflict/v1",
            "initiative": "I-001",
            "reason": "unmarked_same_name_project",
            "candidates": [
                {
                    "project_id": "project-duplicate",
                    "project_url": "https://linear.app/test/project/project-duplicate",
                },
                {
                    "project_id": "project-canonical",
                    "project_url": "https://linear.app/test/project/project-canonical",
                },
            ],
            "observed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        }],
    },
    "initiatives": {
        "I-001": {
            "project_id": "project-canonical",
            "project_url": "https://linear.app/test/project/project-canonical",
        }
    },
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
cat > "$STUB_BIN/launchctl" <<'STUB'
#!/usr/bin/env bash
set -u
service_state() {
  local label="$1" field="$2" file="$HOME/.factory/$label.test-state"
  if [[ -f "$file" ]]; then
    awk -v line="$field" 'NR == line { print; exit }' "$file"
  elif [[ "$field" == "1" ]]; then
    printf 'enabled\n'
  else
    printf 'loaded\n'
  fi
}
case "${1:-}" in
  print-disabled)
    printf '\tdisabled services = {\n'
    for label in com.factory.linear-sync.relay com.factory.linear-sync.launchtest; do
      printf '\t\t"%s" => %s\n' "$label" "$(service_state "$label" 1)"
    done
    printf '\t}\n'
    ;;
  print)
    label="${2##*/}"
    [[ "$(service_state "$label" 2)" != "error" ]] || exit 5
    [[ "$(service_state "$label" 2)" == "loaded" ]] || exit 113
    plist="$HOME/Library/LaunchAgents/$label.plist"
    [[ -f "$plist" ]] || exit 113
    python3 - "$2" "$plist" <<'PY'
import plistlib, sys
target, path = sys.argv[1:]
with open(path, "rb") as stream:
    value = plistlib.load(stream)
print(target + " = {")
print("\targuments = {")
for item in value["ProgramArguments"]:
    print("\t\t" + item)
print("\t}")
print("}")
PY
    ;;
  *) exit 2 ;;
esac
STUB
chmod +x "$STUB_BIN/hermes" "$STUB_BIN/claude" "$STUB_BIN/codex" "$STUB_BIN/agent" \
  "$STUB_BIN/gh" "$STUB_BIN/launchctl"
export FACTORY_DOCTOR_TEST_LAUNCHCTL="$STUB_BIN/launchctl"
export FACTORY_TEST_MODE=1
export FACTORY_TRUSTED_TEST_HARNESS=1

render_linear_plist() {
  local project="$1" product="$2" home="${3:-$TEST_HOME}" destination
  destination="$TEST_HOME/Library/LaunchAgents/com.factory.linear-sync.$project.plist"
  python3 - "$ROOT/scripts/launchd/com.factory.linear-sync.plist.template" \
    "$destination" "$home" "$project" "$product" <<'PY'
import pathlib, sys
source, destination, home, project, product = sys.argv[1:]
text = pathlib.Path(source).read_text(encoding="utf-8")
text = text.replace("__HOME__", home).replace("__PROJECT_SLUG__", project)
text = text.replace("__FACTORY_ROOT__", product)
pathlib.Path(destination).write_text(text, encoding="utf-8")
PY
}
render_linear_plist relay "$PRODUCT"

CONTROLLER_STATE="$TMP/controller-state"
mkdir -m 700 "$CONTROLLER_STATE" "$CONTROLLER_STATE/events" \
  "$CONTROLLER_STATE/claims"
python3 - "$CONTROLLER_STATE/events" "$KIT_SHA" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

events, factory_sha = Path(sys.argv[1]), sys.argv[2]

def write(name, ticket, observed, **details):
    value = {
        "event": name,
        "factory_sha": factory_sha,
        "observed_at_epoch_ns": observed,
        "schema": "nysa.software-factory.controller-event/v1",
        "ticket": ticket,
        **details,
    }
    canonical = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    value["event_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = events / f"{observed}-{ticket}-{name}.json"
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)

write(
    "contract_resume_refused", "T-110", 1,
    actual_bytes=120, expected_bytes=80, first_differing_line=5,
    blocked_receipt_sha256="b" * 64,
    reason_code="resume_commit_content_mismatch",
)
write(
    "contract_resume_refused", "T-111", 2,
    blocked_receipt_sha256="c" * 64,
    local_head="b" * 40, reason_code="resume_commit_not_pushed",
    remote_head="a" * 40,
)
write("contract_blocker_recovered", "T-111", 3)
write(
    "contract_resume_refused", "T-112", 4,
    blocked_receipt_sha256="d" * 64,
    offending_parent="9" * 40, reason_code="resume_parent_not_migrated",
)
# Doctor validates the stable reason-code grammar, not a controller allowlist,
# so a future typed refusal stays visible without suppressing known siblings.
write(
    "contract_resume_refused", "T-113", 5,
    blocked_receipt_sha256="e" * 64,
    reason_code="resume_future_contract_guard",
)
# Authenticated legacy events outside Doctor's two projections may lack the
# Factory identity that current relevant events require.
write("controller_started", "T-109", 0, factory_sha=None)
write(
    "prior_kit_transition_receipt_observed", "T-114", 6,
    active_factory_sha=factory_sha,
    receipt_factory_sha="9" * 40,
    transition_receipt_sha256="f" * 64,
)
write(
    "transition_receipt_invalid", "T-115", 7,
    reason_code="receipt_digest_invalid",
)
write("upgraded_claim_recovered", "T-115", 10, from_factory_sha="7" * 40)
write(
    "prior_kit_transition_receipt_observed", "T-116", 8,
    active_factory_sha=factory_sha,
    receipt_factory_sha="8" * 40,
    transition_receipt_sha256="a" * 64,
)
write("upgraded_claim_recovered", "T-116", 9, from_factory_sha="8" * 40)
write(
    "transition_receipt_invalid", "T-117", 11,
    reason_code="receipt_identity_invalid",
)
write("ticket_retired", "T-117", 12)
claim = events.parent / "claims/T-117.json"
claim.write_text("{}\n", encoding="utf-8")
os.chmod(claim, 0o600)
write(
    "transition_receipt_invalid", "T-118", 13,
    reason_code="receipt_unreadable",
)
write("ticket_retired", "T-118", 14)
write(
    "prior_kit_transition_receipt_observed", "T-119", 15,
    active_factory_sha=factory_sha,
    receipt_factory_sha="7" * 40,
    transition_receipt_sha256="b" * 64,
)
# Retained legacy resolution events authenticate, but lack usable Factory
# lineage. Doctor ignores only these exact observed shapes so they cannot erase
# current incident reports or turn an append-only history into a permanent
# scanner error.
write(
    "contract_blocker_recovered", "T-110", 31,
    factory_sha=None, failed_run_id="1785352139-78426",
)
write(
    "contract_blocker_recovered", "T-112", 32,
    factory_sha=None, failed_run_id="1785371676-11405",
)
write("ticket_released", "T-114", 33, factory_sha=None)
write(
    "upgraded_claim_recovered", "T-119", 34,
    factory_sha=None, from_factory_sha="7" * 40,
)
write(
    "upgraded_claim_recovered", "T-119", 57,
    from_factory_sha=factory_sha,
)
PY

HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$JSON_OUT"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
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

python3 - "$JSON_OUT" "$KIT_SHA" "$ROOT" "$PRODUCT" "$TEST_HOME" <<'PY'
import json
import sys

path, sha, kit_dir, product_root, home = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)

assert data["schema"] == "nysa.software-factory.hermes-doctor/v1"
assert data["schema_version"] == 1
assert data["contract_version"] == "1.8.0"
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
assert checks["linear_sync"]["projects"] == [{
    "initiative": "I-001",
    "project_id": "project-canonical",
    "project_url": "https://linear.app/test/project/project-canonical",
}]
assert checks["linear_sync"]["project_identity_conflict"]["reason"] == "conflicting_project_identity"
assert checks["linear_sync"]["service"] == {
    "arguments_match": True,
    "loaded": True,
    "state": "enabled",
    "status": "ok",
}
assert [
    item["project_id"]
    for item in checks["linear_sync"]["project_identity_conflict"]["candidates"]
] == ["project-canonical", "project-duplicate"]
assert checks["linear_sync"]["project_identity_warnings"][0]["reason"] == "unmarked_same_name_project"
assert [
    item["project_id"]
    for item in checks["linear_sync"]["project_identity_warnings"][0]["candidates"]
] == ["project-canonical", "project-duplicate"]
assert checks["contract_resume"] == {
    "incidents": [{
        "actual_bytes": 120,
        "blocked_receipt_sha256": "b" * 64,
        "expected_bytes": 80,
        "first_differing_line": 5,
        "observed_at_epoch_ns": 1,
        "reason_code": "resume_commit_content_mismatch",
        "ticket": "T-110",
    }, {
        "blocked_receipt_sha256": "d" * 64,
        "observed_at_epoch_ns": 4,
        "offending_parent": "9" * 40,
        "reason_code": "resume_parent_not_migrated",
        "ticket": "T-112",
    }, {
        "blocked_receipt_sha256": "e" * 64,
        "observed_at_epoch_ns": 5,
        "reason_code": "resume_future_contract_guard",
        "ticket": "T-113",
    }],
    "status": "warning",
}
assert checks["transition_receipts"] == {
    "incidents": [{
        "active_factory_sha": sha,
        "observed_at_epoch_ns": 6,
        "reason_code": "prior_kit_receipt",
        "receipt_factory_sha": "9" * 40,
        "ticket": "T-114",
        "transition_receipt_sha256": "f" * 64,
    }, {
        "observed_at_epoch_ns": 7,
        "reason_code": "receipt_digest_invalid",
        "ticket": "T-115",
    }, {
        "observed_at_epoch_ns": 11,
        "reason_code": "receipt_identity_invalid",
        "ticket": "T-117",
    }, {
        "active_factory_sha": sha,
        "observed_at_epoch_ns": 15,
        "reason_code": "prior_kit_receipt",
        "receipt_factory_sha": "7" * 40,
        "ticket": "T-119",
        "transition_receipt_sha256": "b" * 64,
    }],
    "status": "warning",
}
assert checks["controller"] == {
    "last_exit_status": None,
    "state": "not_applicable",
    "status": "not_applicable",
}
allowed = {"ok", "warning", "error", "unknown"}
assert data["overall_status"] in allowed
assert all(
    check["status"] in allowed | {"not_applicable"} for check in checks.values()
)
PY

# Installed Contract 1.8 production Doctor reads the exact managed LaunchAgent
# and launchd's label-specific legacy dictionary. The production parser stays
# native and fixed; these authenticated overrides exist only for this fixture.
CONTROLLER_PLIST="$TEST_HOME/Library/LaunchAgents/com.factory.controller.relay.plist"
CONTROLLER_LAUNCHCTL="$(cd "$TMP" && pwd -P)/launchctl-stub"
CONTROLLER_LAUNCHCTL_MARKER="$TMP/launchctl-invoked"
CONTROLLER_TEST_HOME="$(cd "$TEST_HOME" && pwd -P)"
CONTROLLER_QUALIFICATION_KIT="$TMP/controller-qualification-kit"
mkdir -m 700 -p "$(dirname "$CONTROLLER_PLIST")"
mkdir -m 700 -p "$CONTROLLER_QUALIFICATION_KIT/scripts"
cat > "$CONTROLLER_QUALIFICATION_KIT/scripts/model-control.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' '{"checks":[],"profile_id":"fixture","readiness_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema":"nysa.software-factory.qualification-fallback-readiness/v1","status":"ready"}'
STUB
chmod 700 "$CONTROLLER_QUALIFICATION_KIT/scripts/model-control.sh"
python3 - "$CONTROLLER_PLIST" "$TEST_HOME" "$PRODUCT" <<'PY'
from pathlib import Path
import os
import plistlib
import sys

path = Path(sys.argv[1])
home = Path(sys.argv[2]).resolve()
product = Path(sys.argv[3]).resolve()
program = str(home / ".factory/bin/factory-launch")
value = {
    "Label": "com.factory.controller.relay",
    "ProcessType": "Interactive",
    "ProgramArguments": [program, "relay", "reconcile", "--json"],
    "RunAtLoad": True,
    "StandardErrorPath": str(home / ".factory/logs/relay-controller.error.log"),
    "StandardOutPath": str(home / ".factory/logs/relay-controller.log"),
    "StartInterval": 15,
    "WatchPaths": [str(product / "factory/runs")],
}
with path.open("wb") as handle:
    plistlib.dump(value, handle, sort_keys=True)
os.chmod(path, 0o600)
PY
cat > "$CONTROLLER_LAUNCHCTL" <<'STUB'
#!/usr/bin/env bash
set -eu

[[ "$#" -eq 5 && "$1" == "asuser" && "$3" == "$0" &&
   ( "$4" == "print-disabled" || "$4" == "list" ) ]] || exit 64
printf '%s\n' "$4" >> "$FACTORY_LAUNCHCTL_MARKER"

if [[ "$4" == "print-disabled" ]]; then
  [[ "$5" == "gui/$2" ]] || exit 64
  case "$FACTORY_LAUNCHCTL_SCENARIO" in
    disabled-oversize)
      python3 - <<'PY'
import sys
sys.stdout.write("launchctl-secret-never-print" + "x" * 65_537)
PY
      exit 0
      ;;
  esac
  printf '\tdisabled services = {\n'
  case "$FACTORY_LAUNCHCTL_SCENARIO" in
    disabled-loaded)
      printf '\t\t"com.factory.controller.relay" => disabled\n'
      ;;
    disabled-true-loaded)
      printf '\t\t"com.factory.controller.relay" => true\n'
      ;;
    enabled-running)
      printf '\t\t"com.factory.controller.relay" => enabled\n'
      ;;
    false-running)
      printf '\t\t"com.factory.controller.relay" => false\n'
      ;;
    disabled-duplicate)
      printf '\t\t"com.factory.controller.relay" => enabled\n'
      printf '\t\t"com.factory.controller.relay" => disabled\n'
      ;;
    disabled-unknown)
      printf '\t\t"com.factory.controller.relay" => maybe\n'
      ;;
    *)
      printf '\t\t"com.factory.unrelated" => enabled\n'
      ;;
  esac
  printf '\t}\n'
  exit 0
fi

[[ "$5" == "com.factory.controller.relay" ]] || exit 64

case "$FACTORY_LAUNCHCTL_SCENARIO" in
  missing)
    printf 'launchctl-secret-never-print\n' >&2
    exit 113
    ;;
  oversize)
    python3 - <<'PY'
import sys
sys.stdout.write("launchctl-secret-never-print" + "x" * 65_537)
PY
    exit 0
    ;;
  malformed)
    printf 'launchctl-secret-never-print\n' >&2
    printf '{\n    "Label" = "com.factory.controller.relay";\n'
    printf '    "Label" = "com.factory.controller.relay";\n}\n'
    exit 0
    ;;
esac

printf '{\n'
if [[ "$FACTORY_LAUNCHCTL_SCENARIO" == "running" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "disabled-loaded" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "disabled-true-loaded" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "enabled-running" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "false-running" ]]; then
  printf '    "PID" = 123;\n'
fi
if [[ "$FACTORY_LAUNCHCTL_SCENARIO" == "nonzero" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "running" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "disabled-loaded" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "disabled-true-loaded" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "enabled-running" ||
      "$FACTORY_LAUNCHCTL_SCENARIO" == "false-running" ]]; then
  printf '    "LastExitStatus" = 7;\n'
else
  printf '    "LastExitStatus" = 0;\n'
fi
printf '    "Label" = "com.factory.controller.relay";\n'
printf '    "Program" = "%s/.factory/bin/factory-launch";\n' "$HOME"
printf '    "ProgramArguments" = (\n'
printf '        "%s/.factory/bin/factory-launch";\n' "$HOME"
printf '        "relay";\n        "reconcile";\n        "--json";\n'
printf '    );\n}\n'
STUB
chmod 700 "$CONTROLLER_LAUNCHCTL"

run_controller_doctor() {
  local scope="$1" platform="$2" test_mode="$3" scenario="$4" output="$5"
  local -a doctor_args
  doctor_args=(--json --project relay)
  if [[ "$scope" == "qualification-candidate" ]]; then
    doctor_args+=(
      --kit-dir "$CONTROLLER_QUALIFICATION_KIT"
      --product-root "$PRODUCT"
      --kit-sha "$KIT_SHA"
    )
  fi
  rm -f "$CONTROLLER_LAUNCHCTL_MARKER"
  CONTROLLER_DOCTOR_RC=0
  HOME="$CONTROLLER_TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
    FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
    FACTORY_KIT_TRUST_SCOPE="$scope" FACTORY_TEST_MODE="$test_mode" \
    FACTORY_TRUSTED_TEST_HARNESS=1 FACTORY_DOCTOR_PLATFORM="$platform" \
    FACTORY_DOCTOR_LAUNCHCTL="$CONTROLLER_LAUNCHCTL" \
    FACTORY_LAUNCHCTL_SCENARIO="$scenario" \
    FACTORY_LAUNCHCTL_MARKER="$CONTROLLER_LAUNCHCTL_MARKER" \
    bash "$DOCTOR" "${doctor_args[@]}" > "$output" \
    2> "${output%.json}.err" || CONTROLLER_DOCTOR_RC=$?
}

assert_controller_check() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json
import sys

path, status, state, last = sys.argv[1:]
expected_last = None if last == "null" else int(last)
value = json.load(open(path, encoding="utf-8"))
assert value["checks"]["controller"] == {
    "last_exit_status": expected_last,
    "state": state,
    "status": status,
}
PY
}

while IFS='|' read -r scenario expected_rc status state last commands; do
  output="$TMP/controller-$scenario.json"
  run_controller_doctor production-certified Darwin 0 "$scenario" "$output"
  [[ "$CONTROLLER_DOCTOR_RC" -eq "$expected_rc" ]] ||
    fail "Doctor returned the wrong status for controller scenario $scenario"
  assert_controller_check "$output" "$status" "$state" "$last"
  observed_commands="$(paste -sd, "$CONTROLLER_LAUNCHCTL_MARKER")"
  [[ "$observed_commands" == "$commands" ]] ||
    fail "Doctor used the wrong launchctl sequence for controller scenario $scenario"
done <<'EOF'
running|0|ok|running|7|print-disabled,list
idle|0|ok|idle_clean|0|print-disabled,list
nonzero|1|error|last_exit_nonzero|7|print-disabled,list
missing|1|error|unavailable|null|print-disabled,list
malformed|1|error|unavailable|null|print-disabled,list
oversize|1|error|unavailable|null|print-disabled,list
disabled-loaded|1|error|disabled|null|print-disabled
disabled-true-loaded|1|error|disabled|null|print-disabled
enabled-running|0|ok|running|7|print-disabled,list
false-running|0|ok|running|7|print-disabled,list
disabled-duplicate|1|error|unavailable|null|print-disabled
disabled-unknown|1|error|unavailable|null|print-disabled
disabled-oversize|1|error|unavailable|null|print-disabled
EOF
if grep -R -Fq "launchctl-secret-never-print" "$TMP"/controller-*.json \
    "$TMP"/controller-*.err; then
  fail "Doctor exposed launchctl output"
fi

python3 - "$CONTROLLER_PLIST" <<'PY'
import plistlib
import sys

path = sys.argv[1]
with open(path, "rb") as handle:
    value = plistlib.load(handle)
value["StartInterval"] = 16
with open(path, "wb") as handle:
    plistlib.dump(value, handle, sort_keys=True)
PY
run_controller_doctor production-certified Darwin 0 idle \
  "$TMP/controller-route-mismatch.json"
[[ "$CONTROLLER_DOCTOR_RC" -eq 1 ]] || fail "Doctor accepted a mismatched controller route"
assert_controller_check "$TMP/controller-route-mismatch.json" error route_mismatch null
[[ ! -e "$CONTROLLER_LAUNCHCTL_MARKER" ]] || \
  fail "Doctor queried launchd before rejecting its managed plist"
python3 - "$CONTROLLER_PLIST" <<'PY'
import plistlib
import sys

path = sys.argv[1]
with open(path, "rb") as handle:
    value = plistlib.load(handle)
value["StartInterval"] = 15
with open(path, "wb") as handle:
    plistlib.dump(value, handle, sort_keys=True)
PY

run_controller_doctor qualification-candidate Darwin 0 idle \
  "$TMP/controller-qualification.json"
assert_controller_check "$TMP/controller-qualification.json" not_applicable not_applicable null
[[ ! -e "$CONTROLLER_LAUNCHCTL_MARKER" ]] || fail "qualification Doctor queried launchd"

run_controller_doctor production-certified Linux 0 idle \
  "$TMP/controller-linux.json"
[[ "$CONTROLLER_DOCTOR_RC" -eq 0 ]] || fail "Linux controller check was not neutral"
assert_controller_check "$TMP/controller-linux.json" not_applicable not_applicable null
[[ ! -e "$CONTROLLER_LAUNCHCTL_MARKER" ]] || fail "Linux Doctor queried launchd"

run_controller_doctor production-certified Darwin 1 idle \
  "$TMP/controller-disposable.json"
[[ "$CONTROLLER_DOCTOR_RC" -eq 0 ]] || fail "disposable controller check was not neutral"
assert_controller_check "$TMP/controller-disposable.json" not_applicable not_applicable null
[[ ! -e "$CONTROLLER_LAUNCHCTL_MARKER" ]] || fail "disposable Doctor queried launchd"
RELAY_SERVICE_STATE="$TEST_HOME/.factory/com.factory.linear-sync.relay.test-state"
printf 'disabled\nunloaded\n' > "$RELAY_SERVICE_STATE"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-linear-disabled.json"
python3 - "$TMP/doctor-linear-disabled.json" <<'PY'
import json, sys
service = json.load(open(sys.argv[1]))["checks"]["linear_sync"]["service"]
assert service["status"] == "ok"
assert service["state"] == "disabled"
assert service["loaded"] is False
PY
printf 'disabled\nloaded\n' > "$RELAY_SERVICE_STATE"
LINEAR_DISABLED_LOADED_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-linear-disabled-loaded.json" || \
  LINEAR_DISABLED_LOADED_RC=$?
[[ "$LINEAR_DISABLED_LOADED_RC" -eq 1 ]] || fail "doctor accepted disabled-but-loaded Linear service"
python3 - "$TMP/doctor-linear-disabled-loaded.json" <<'PY'
import json, sys
service = json.load(open(sys.argv[1]))["checks"]["linear_sync"]["service"]
assert service["status"] == "error"
assert service["state"] == "disabled"
assert service["loaded"] is True
assert service["arguments_match"] is True
PY
printf 'disabled\nerror\n' > "$RELAY_SERVICE_STATE"
LINEAR_QUERY_ERROR_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-linear-query-error.json" || \
  LINEAR_QUERY_ERROR_RC=$?
[[ "$LINEAR_QUERY_ERROR_RC" -eq 1 ]] || fail "doctor treated a launchctl failure as unloaded"
python3 - "$TMP/doctor-linear-query-error.json" <<'PY'
import json, sys
service = json.load(open(sys.argv[1]))["checks"]["linear_sync"]["service"]
assert service["status"] == "error"
assert service["state"] == "disabled"
assert service["loaded"] is False
PY
printf 'enabled\nunloaded\n' > "$RELAY_SERVICE_STATE"
LINEAR_UNLOADED_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-linear-unloaded.json" || \
  LINEAR_UNLOADED_RC=$?
[[ "$LINEAR_UNLOADED_RC" -eq 1 ]] || fail "doctor accepted enabled-but-unloaded Linear service"
python3 - "$TMP/doctor-linear-unloaded.json" <<'PY'
import json, sys
service = json.load(open(sys.argv[1]))["checks"]["linear_sync"]["service"]
assert service["status"] == "error"
assert service["state"] == "enabled"
assert service["loaded"] is False
PY
rm "$RELAY_SERVICE_STATE"
cp "$TEST_HOME/Library/LaunchAgents/com.factory.linear-sync.relay.plist" \
  "$TMP/linear-service.plist"
python3 - "$TEST_HOME/Library/LaunchAgents/com.factory.linear-sync.relay.plist" <<'PY'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as stream:
    value = plistlib.load(stream)
value["ProgramArguments"] = ["/legacy/release/scripts/linear-sync.py"]
with open(path, "wb") as stream:
    plistlib.dump(value, stream)
PY
LINEAR_LEGACY_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-linear-legacy.json" || \
  LINEAR_LEGACY_RC=$?
[[ "$LINEAR_LEGACY_RC" -eq 1 ]] || fail "doctor accepted a release-pinned Linear plist"
mv "$TMP/linear-service.plist" \
  "$TEST_HOME/Library/LaunchAgents/com.factory.linear-sync.relay.plist"

# A relevant event still requires Factory identity even when its digest is valid.
INVALID_RESUME_EVENT="$CONTROLLER_STATE/events/6-invalid.json"
python3 - "$INVALID_RESUME_EVENT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
value = {
    "blocked_receipt_sha256": "f" * 64,
    "event": "contract_resume_refused",
    "factory_sha": None,
    "observed_at_epoch_ns": 6,
    "reason_code": "resume_future_contract_guard",
    "schema": "nysa.software-factory.controller-event/v1",
    "ticket": "T-114",
}
canonical = json.dumps(
    value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
).encode()
value["event_sha256"] = hashlib.sha256(canonical).hexdigest()
path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
INVALID_RESUME_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-invalid-resume.json" \
  2> "$TMP/doctor-invalid-resume.err" || \
  INVALID_RESUME_RC=$?
[[ "$INVALID_RESUME_RC" -eq 1 ]] || fail "doctor accepted invalid resume event state"
[[ ! -s "$TMP/doctor-invalid-resume.err" ]] || \
  fail "doctor exposed an interpreter error for invalid resume state"
python3 - "$TMP/doctor-invalid-resume.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["overall_status"] == "error"
assert data["checks"]["contract_resume"] == {"incidents": [], "status": "error"}
assert data["checks"]["transition_receipts"] == {
    "incidents": [], "status": "error",
}
PY
rm "$INVALID_RESUME_EVENT"

INVALID_MIGRATION_EVENT="$CONTROLLER_STATE/events/16-invalid.json"
python3 - "$INVALID_MIGRATION_EVENT" "$KIT_SHA" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

path, factory_sha = Path(sys.argv[1]), sys.argv[2]
value = {
    "event": "upgraded_claim_recovered",
    "factory_sha": factory_sha,
    "observed_at_epoch_ns": 16,
    "schema": "nysa.software-factory.controller-event/v1",
    "ticket": "T-120",
}
canonical = json.dumps(
    value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
).encode()
value["event_sha256"] = hashlib.sha256(canonical).hexdigest()
path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
INVALID_MIGRATION_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_CONTROLLER_STATE_DIR="$CONTROLLER_STATE" \
  bash "$DOCTOR" --json --project relay > "$TMP/doctor-invalid-migration.json" || \
  INVALID_MIGRATION_RC=$?
[[ "$INVALID_MIGRATION_RC" -eq 1 ]] || fail "doctor accepted invalid migration event state"
python3 - "$TMP/doctor-invalid-migration.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["overall_status"] == "error"
assert data["checks"]["transition_receipts"] == {
    "incidents": [], "status": "error",
}
PY
rm "$INVALID_MIGRATION_EVENT"

PROVIDER_TEST_ROOT="$(cd "$TMP" && pwd -P)/provider-v2"
mkdir -m 700 "$PROVIDER_TEST_ROOT" "$PROVIDER_TEST_ROOT/attempts" \
  "$PROVIDER_TEST_ROOT/apply-locks"
python3 - "$PROVIDER_TEST_ROOT/policy.json" "$PROVIDER_TEST_ROOT/activation.json" <<'PY'
import hashlib
import json
import os
import sys

policy_path, activation_path = sys.argv[1:]
limit = {"max_concurrent": 4, "max_starts": 20, "window_seconds": 60}
account_limit = {"max_concurrent": 2, "max_starts": 20, "window_seconds": 60}
policy = {
    "schema": "factory-provider-concurrency-policy/v1",
    "coupled_max_concurrent": 4,
    "global": limit,
    "provider_families": {"openai": limit},
    "account_routes": {"codex-native": account_limit},
}
canonical_policy = json.dumps(policy, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
with open(policy_path, "w", encoding="utf-8") as handle:
    handle.write(canonical_policy + "\n")
activation = {
    "enabled": True,
    "mode": "cli-concurrent-v1",
    "policy_sha256": hashlib.sha256(canonical_policy.encode()).hexdigest(),
    "routes": {
        "codex-gpt-5.6-sol": {
            "account_route": "codex-native",
            "adapter": "codex",
            "model": "gpt-5.6-sol",
            "provider_family": "openai",
        }
    },
    "schema": "nysa.software-factory.provider-activation/v2",
}
with open(activation_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(activation, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
os.chmod(activation_path, 0o600)
PY
python3 "$ROOT/scripts/provider-coordinator.py" \
  --db "$PROVIDER_TEST_ROOT/state.sqlite3" status >/dev/null
PROVIDER_DB_BEFORE="$(cksum "$PROVIDER_TEST_ROOT/state.sqlite3")"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" FACTORY_LINEAR_FRESH_SECONDS=600 \
  FACTORY_PROVIDER_ACTIVATION="$PROVIDER_TEST_ROOT/activation.json" \
  FACTORY_PROVIDER_POLICY="$PROVIDER_TEST_ROOT/policy.json" \
  FACTORY_PROVIDER_DB="$PROVIDER_TEST_ROOT/state.sqlite3" \
  FACTORY_PROVIDER_ATTEMPT_ROOT="$PROVIDER_TEST_ROOT/attempts" \
  FACTORY_PROVIDER_APPLY_LOCK_ROOT="$PROVIDER_TEST_ROOT/apply-locks" \
  bash "$DOCTOR" --json --project relay > "$TMP/provider-v2-doctor.json"
[[ "$(cksum "$PROVIDER_TEST_ROOT/state.sqlite3")" == "$PROVIDER_DB_BEFORE" ]] ||
  fail "Contract 1.7 doctor mutated provider coordinator state"
python3 - "$TMP/provider-v2-doctor.json" <<'PY'
import json
import sys

provider = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]["isolated_provider"]
assert provider == {
    "status": "ok",
    "activated": True,
    "concurrency_required": False,
    "concurrency_ready": False,
    "execution_mode": "cli-concurrent-v1",
    "active_attempts": 0,
    "active_tokens": 0,
    "unknown_workers": 0,
    "legacy_intervals": 0,
}
PY
python3 - "$PROVIDER_TEST_ROOT/activation.json" <<'PY'
import json
import sys

path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["policy_sha256"] = "0" * 64
with open(path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
PY
PROVIDER_MISMATCH_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" \
  FACTORY_PROVIDER_ACTIVATION="$PROVIDER_TEST_ROOT/activation.json" \
  FACTORY_PROVIDER_POLICY="$PROVIDER_TEST_ROOT/policy.json" \
  FACTORY_PROVIDER_DB="$PROVIDER_TEST_ROOT/state.sqlite3" \
  FACTORY_PROVIDER_ATTEMPT_ROOT="$PROVIDER_TEST_ROOT/attempts" \
  FACTORY_PROVIDER_APPLY_LOCK_ROOT="$PROVIDER_TEST_ROOT/apply-locks" \
  bash "$DOCTOR" --json --project relay > "$TMP/provider-v2-mismatch.json" ||
  PROVIDER_MISMATCH_RC=$?
[[ "$PROVIDER_MISMATCH_RC" -eq 1 ]] ||
  fail "Contract 1.7 doctor accepted a mismatched provider policy digest"
python3 - "$TMP/provider-v2-mismatch.json" <<'PY'
import json
import sys

provider = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]["isolated_provider"]
assert provider["status"] == "error"
assert provider["activated"] is True
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
SHA_MODELS_V18="9999999999999999999999999999999999999999"
SHA_FALLBACK="ffffffffffffffffffffffffffffffffffffffff"
SHA_LINEAR_A="7777777777777777777777777777777777777777"
SHA_LINEAR_B="8888888888888888888888888888888888888888"
RELEASE_A="$KITS_ROOT/releases/$SHA_A"
RELEASE_B="$KITS_ROOT/releases/$SHA_B"
RELEASE_V11="$KITS_ROOT/releases/$SHA_V11"
RELEASE_MODELS="$KITS_ROOT/releases/$SHA_MODELS"
RELEASE_MODELS_V18="$KITS_ROOT/releases/$SHA_MODELS_V18"
RELEASE_FALLBACK="$KITS_ROOT/releases/$SHA_FALLBACK"
RELEASE_LINEAR_A="$KITS_ROOT/releases/$SHA_LINEAR_A"
RELEASE_LINEAR_B="$KITS_ROOT/releases/$SHA_LINEAR_B"
mkdir -p "$KITS_ROOT/projects/launchtest" "$LAUNCH_PRODUCT/factory"
git -C "$LAUNCH_PRODUCT" init -q -b main
git -C "$LAUNCH_PRODUCT" config user.email "hermes-contract@test.local"
git -C "$LAUNCH_PRODUCT" config user.name "hermes-contract-test"
printf 'initial launcher product\n' > "$LAUNCH_PRODUCT/README.md"
git -C "$LAUNCH_PRODUCT" add README.md
git -C "$LAUNCH_PRODUCT" commit -qm "initialize launcher product"
LAUNCH_PRODUCT_REMOTE="$TMP/launch-product.git"
git init --bare -q "$LAUNCH_PRODUCT_REMOTE"
git -C "$LAUNCH_PRODUCT" remote add origin "$LAUNCH_PRODUCT_REMOTE"
git -C "$LAUNCH_PRODUCT" push -q -u origin main
render_linear_plist launchtest "$(cd "$LAUNCH_PRODUCT" && pwd -P)" \
  "$(cd "$TEST_HOME" && pwd -P)"
LAUNCHTEST_LINEAR_PLIST="$TEST_HOME/Library/LaunchAgents/com.factory.linear-sync.launchtest.plist"
LAUNCHTEST_LINEAR_PLIST_BEFORE="$(cksum "$LAUNCHTEST_LINEAR_PLIST")"
create_test_release "$RELEASE_A" "RELEASE-A" "RUN planner" "1.0.0"
create_test_release "$RELEASE_B" "RELEASE-B" "AWAIT-OPERATOR" "1.1.0"
create_test_release "$RELEASE_V11" "RELEASE-V11" "RUN planner" "1.1.0"
create_test_release "$RELEASE_MODELS" "RELEASE-MODELS" "RUN planner" "1.2.0"
create_test_release "$RELEASE_MODELS_V18" "RELEASE-MODELS-V18" "RUN planner" "1.8.0"
create_test_release "$RELEASE_FALLBACK" "RELEASE-FALLBACK" "RUN planner" "1.6.0"
create_test_release "$RELEASE_LINEAR_A" "RELEASE-LINEAR-A" "RUN planner" "1.8.0"
create_test_release "$RELEASE_LINEAR_B" "RELEASE-LINEAR-B" "RUN planner" "1.8.0"
TREE_A="$(tree_for_directory "$RELEASE_A")"
TREE_B="$(tree_for_directory "$RELEASE_B")"
TREE_V11="$(tree_for_directory "$RELEASE_V11")"
TREE_MODELS="$(tree_for_directory "$RELEASE_MODELS")"
TREE_MODELS_V18="$(tree_for_directory "$RELEASE_MODELS_V18")"
TREE_FALLBACK="$(tree_for_directory "$RELEASE_FALLBACK")"
TREE_LINEAR_A="$(tree_for_directory "$RELEASE_LINEAR_A")"
TREE_LINEAR_B="$(tree_for_directory "$RELEASE_LINEAR_B")"
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

LINEAR_V1_RC=0
run_launcher launchtest linear-sync > "$TMP/linear-v1.out" 2>&1 || LINEAR_V1_RC=$?
[[ "$LINEAR_V1_RC" -eq 1 ]] || fail "contract 1.0 unexpectedly exposed scheduled Linear sync"
printf '%s\n' "$SHA_LINEAR_A" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_LINEAR_A" "$TREE_LINEAR_A" "$RELEASE_LINEAR_A"
run_launcher launchtest linear-sync
[[ "$(<"$LAUNCH_PRODUCT/factory/linear-release.txt")" == "RELEASE-LINEAR-A" ]] ||
  fail "scheduled Linear sync did not select Linear release A"
run_launcher launchtest doctor --json > "$TMP/linear-doctor.json"
python3 - "$TMP/linear-doctor.json" <<'PY'
import json, sys
service = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]["linear_sync"]["service"]
assert service["status"] == "ok"
assert service["state"] == "enabled"
assert service["loaded"] is True
assert service["arguments_match"] is True
PY
python3 - "$LAUNCH_PRODUCT/factory/linear-helper.env" <<'PY'
import pathlib, sys
names = set(pathlib.Path(sys.argv[1]).read_text().splitlines())
required = {"FACTORY_ROOT", "HOME", "PATH", "TMPDIR"}
assert required <= names, names
platform = {
    "CPATH", "LC_CTYPE", "LIBRARY_PATH", "MANPATH", "SDKROOT",
    "__CF_USER_TEXT_ENCODING",
}
assert not names - required - platform, names
PY
LINEAR_ARGUMENT_RC=0
run_launcher launchtest linear-sync --factory-root "$TMP/bypass" \
  > "$TMP/linear-arguments.out" 2>&1 || LINEAR_ARGUMENT_RC=$?
[[ "$LINEAR_ARGUMENT_RC" -eq 2 ]] || fail "scheduled Linear sync accepted caller arguments"
printf '%s\n' "$SHA_LINEAR_B" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_LINEAR_B" "$TREE_LINEAR_B" "$RELEASE_LINEAR_B"
run_launcher launchtest linear-sync
[[ "$(<"$LAUNCH_PRODUCT/factory/linear-release.txt")" == "RELEASE-LINEAR-B" ]] ||
  fail "scheduled Linear sync did not follow the active Linear release switch"
printf '%s\n' "$SHA_LINEAR_A" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_LINEAR_A" "$TREE_LINEAR_A" "$RELEASE_LINEAR_A"
run_launcher launchtest linear-sync
[[ "$(<"$LAUNCH_PRODUCT/factory/linear-release.txt")" == "RELEASE-LINEAR-A" ]] ||
  fail "scheduled Linear sync did not follow the active Linear release rollback"
[[ "$(cksum "$LAUNCHTEST_LINEAR_PLIST")" == "$LAUNCHTEST_LINEAR_PLIST_BEFORE" ]] ||
  fail "active release switches rewrote the stable Linear plist"
printf '%s\n' "$SHA_A" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_A" "$TREE_A" "$RELEASE_A"

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
for _try in $(seq 1 1500); do
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

mkdir -m 700 -p "$KITS_ROOT/projects/launchtest/controller/events"
chmod 700 "$KITS_ROOT/projects/launchtest/controller"
if ! run_launcher launchtest doctor --json > "$TMP/launcher-doctor.json"; then
  cat "$TMP/launcher-doctor.json" >&2
  fail "launcher doctor rejected controller-state diagnostics"
fi
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
assert data["checks"]["controller"] == {
    "last_exit_status": None,
    "state": "not_applicable",
    "status": "not_applicable",
}
assert data["checks"]["fallback_readiness"] == {
    "status": "not_applicable", "report": None,
}
model_readiness = data["checks"]["model_readiness"]
assert model_readiness["status"] == "ok"
assert model_readiness["report"]["status"] == "ready"
assert model_readiness["report"]["profile_id"] == "cursor-opus-v1"
assert data["checks"]["linear_sync"]["service"]["status"] == "not_applicable"
assert data["checks"]["linear_sync"]["service"]["arguments_match"] is False
PY
assert_no_secret "$TMP/launcher-doctor.json"
DOCTOR_HELPER_ENV="$LAUNCH_PRODUCT/factory/doctor-helper.env"
assert_release_metadata "$DOCTOR_HELPER_ENV" "$SHA_A" "$TREE_A" "$RELEASE_A"
assert_helper_confinement "$DOCTOR_HELPER_ENV"
EXPECTED_CONTROLLER_STATE="$(cd "$KITS_ROOT/projects/launchtest/controller" && pwd -P)"
grep -Fx "FACTORY_CONTROLLER_STATE_DIR=$EXPECTED_CONTROLLER_STATE" \
  "$DOCTOR_HELPER_ENV" >/dev/null || fail "doctor did not receive controller state"

touch "$LAUNCH_PRODUCT/factory/test-model-readiness-invalid"
DOCTOR_READINESS_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_RELEASE_CONTRACT_VERSION=1.8.0 FACTORY_PROJECT=launchtest \
  FACTORY_ROOT="$LAUNCH_PRODUCT" FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  bash "$RELEASE_A/scripts/factory-doctor-real.sh" --json \
    --profile-dir "$PROFILE" --kit-dir "$RELEASE_A" \
    --product-root "$LAUNCH_PRODUCT" --kit-sha "$SHA_A" \
    > "$TMP/doctor-readiness-invalid.json" || DOCTOR_READINESS_RC=$?
[[ "$DOCTOR_READINESS_RC" -eq 1 ]] || fail "Doctor accepted invalid fallback readiness"
python3 - "$TMP/doctor-readiness-invalid.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
check = value["checks"]["fallback_readiness"]
assert check["status"] == "error"
assert check["report"]["readiness_sha256"] == "a" * 64
route = check["report"]["checks"][0]
assert route == {
    "cursor_route_id": "cursor-gpt-5.6-sol-high",
    "expected_version": "0.147.0",
    "fallback_route_id": "codex-gpt-5.6-sol-high",
    "installed_version": "0.148.0",
    "reason": "version_mismatch",
    "role": "planner",
    "state": "INVALID",
}
PY
rm -f "$LAUNCH_PRODUCT/factory/test-model-readiness-invalid"
touch "$LAUNCH_PRODUCT/factory/test-model-readiness-ready"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_RELEASE_CONTRACT_VERSION=1.8.0 FACTORY_PROJECT=launchtest \
  FACTORY_ROOT="$LAUNCH_PRODUCT" FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  bash "$RELEASE_A/scripts/factory-doctor-real.sh" --json \
    --profile-dir "$PROFILE" --kit-dir "$RELEASE_A" \
    --product-root "$LAUNCH_PRODUCT" --kit-sha "$SHA_A" \
    > "$TMP/doctor-readiness-ready.json" || true
python3 - "$TMP/doctor-readiness-ready.json" <<'PY'
import json, sys
check = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]["fallback_readiness"]
assert check == {"status": "ok", "report": {
    "checks": [], "profile_id": "cursor-opus-v1",
    "readiness_sha256": "b" * 64,
    "schema": "nysa.software-factory.qualification-fallback-readiness/v1",
    "status": "ready",
}}
PY
rm -f "$LAUNCH_PRODUCT/factory/test-model-readiness-ready"

touch "$LAUNCH_PRODUCT/factory/test-production-model-readiness-invalid"
DOCTOR_MODEL_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_KIT_TRUST_SCOPE=production-certified \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_RELEASE_CONTRACT_VERSION=1.8.0 FACTORY_PROJECT=launchtest \
  FACTORY_ROOT="$LAUNCH_PRODUCT" FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  FACTORY_MODEL_MANAGER="$RELEASE_A/scripts/model-manager.py" \
  bash "$RELEASE_A/scripts/factory-doctor-real.sh" --json \
    --profile-dir "$PROFILE" --kit-dir "$RELEASE_A" \
    --product-root "$LAUNCH_PRODUCT" --kit-sha "$SHA_A" \
    > "$TMP/doctor-production-readiness-invalid.json" || DOCTOR_MODEL_RC=$?
[[ "$DOCTOR_MODEL_RC" -eq 1 ]] || fail "Doctor accepted an unusable active profile"
python3 - "$TMP/doctor-production-readiness-invalid.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
check = value["checks"]["model_readiness"]
assert check["status"] == "error"
assert check["report"]["profile_id"] == "cursor-opus-v1"
assert check["report"]["reason_code"] == "profile_resolution_failed"
assert check["report"]["readiness"]["codex-gpt-5.6-sol"] == {
    "adapter_version": "0.148.0",
    "reason": "version_mismatch",
    "reported_identity": "",
    "state": "INVALID",
}
PY
rm -f "$LAUNCH_PRODUCT/factory/test-production-model-readiness-invalid"

touch "$LAUNCH_PRODUCT/factory/test-production-model-readiness-unsafe"
DOCTOR_MODEL_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_KIT_TRUST_SCOPE=production-certified \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_RELEASE_CONTRACT_VERSION=1.8.0 FACTORY_PROJECT=launchtest \
  FACTORY_ROOT="$LAUNCH_PRODUCT" FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  FACTORY_MODEL_MANAGER="$RELEASE_A/scripts/model-manager.py" \
  bash "$RELEASE_A/scripts/factory-doctor-real.sh" --json \
    --profile-dir "$PROFILE" --kit-dir "$RELEASE_A" \
    --product-root "$LAUNCH_PRODUCT" --kit-sha "$SHA_A" \
    > "$TMP/doctor-production-readiness-unsafe.json" || DOCTOR_MODEL_RC=$?
[[ "$DOCTOR_MODEL_RC" -eq 1 ]] || fail "Doctor accepted unsafe readiness evidence"
python3 - "$TMP/doctor-production-readiness-unsafe.json" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
assert "DO-NOT-LEAK" not in raw
check = json.loads(raw)["checks"]["model_readiness"]
assert check == {"report": None, "status": "error"}
PY
rm -f "$LAUNCH_PRODUCT/factory/test-production-model-readiness-unsafe"

touch "$LAUNCH_PRODUCT/factory/test-production-model-readiness-temporary"
DOCTOR_MODEL_RC=0
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_KIT_TRUST_SCOPE=production-certified \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_RELEASE_CONTRACT_VERSION=1.8.0 FACTORY_PROJECT=launchtest \
  FACTORY_ROOT="$LAUNCH_PRODUCT" FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  FACTORY_MODEL_MANAGER="$RELEASE_A/scripts/model-manager.py" \
  bash "$RELEASE_A/scripts/factory-doctor-real.sh" --json \
    --profile-dir "$PROFILE" --kit-dir "$RELEASE_A" \
    --product-root "$LAUNCH_PRODUCT" --kit-sha "$SHA_A" \
    > "$TMP/doctor-production-readiness-temporary.json" || DOCTOR_MODEL_RC=$?
[[ "$DOCTOR_MODEL_RC" -eq 1 ]] || fail "Doctor accepted a temporarily unusable profile"
python3 - "$TMP/doctor-production-readiness-temporary.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
check = value["checks"]["model_readiness"]
assert check["status"] == "error"
assert check["report"]["reason_code"] == "profile_temporarily_unavailable"
assert value["overall_status"] == "error"
PY
rm -f "$LAUNCH_PRODUCT/factory/test-production-model-readiness-temporary"

touch "$LAUNCH_PRODUCT/factory/test-production-model-readiness-ready"
HOME="$TEST_HOME" PATH="$STUB_BIN:$PATH" TMPDIR="$TMP/launcher-tmp" \
  FACTORY_KIT_TRUST_SCOPE=production-certified \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_RELEASE_CONTRACT_VERSION=1.8.0 FACTORY_PROJECT=launchtest \
  FACTORY_ROOT="$LAUNCH_PRODUCT" FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  FACTORY_MODEL_MANAGER="$RELEASE_A/scripts/model-manager.py" \
  bash "$RELEASE_A/scripts/factory-doctor-real.sh" --json \
    --profile-dir "$PROFILE" --kit-dir "$RELEASE_A" \
    --product-root "$LAUNCH_PRODUCT" --kit-sha "$SHA_A" \
    > "$TMP/doctor-production-readiness-ready.json"
python3 - "$TMP/doctor-production-readiness-ready.json" <<'PY'
import json, sys
check = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]["model_readiness"]
assert check == {"status": "ok", "report": {
    "portfolio_id": "cursor-openai-production",
    "profile_hash": "c" * 64,
    "profile_id": "cursor-opus-v1",
    "schema": "nysa.software-factory.doctor-model-readiness/v1",
    "status": "ready",
}}
PY
rm -f "$LAUNCH_PRODUCT/factory/test-production-model-readiness-ready"

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
doctor = json.load(open(sys.argv[2], encoding="utf-8"))
assert doctor["checks"]["linear_sync"]["service"]["status"] == "not_applicable"
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
printf '%s\n' 'MAX_CONCURRENT_TICKETS=4' > "$LAUNCH_PRODUCT/factory/PROJECT.env"
for ticket in T-201 T-202 T-203 T-204 T-205; do
  printf '# %s\n\nState: Ready\n' "$ticket" > "$LAUNCH_PRODUCT/factory/tickets/$ticket.md"
done
run_launcher launchtest claim --ticket T-201 > "$TMP/claim-201.json"
run_launcher launchtest claim --ticket T-202 > "$TMP/claim-202.json"
run_launcher launchtest claim --ticket T-203 > "$TMP/claim-203.json"
run_launcher launchtest claim --ticket T-204 > "$TMP/claim-204.json"
CLAIM_201_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/claim-201.json")"
CLAIM_202_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/claim-202.json")"
CLAIM_203_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/claim-203.json")"
CLAIM_204_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' "$TMP/claim-204.json")"
CLAIM_205_RC=0
run_launcher launchtest claim --ticket T-205 > "$TMP/claim-205.out" 2>&1 || CLAIM_205_RC=$?
[[ "$CLAIM_205_RC" -eq 1 ]] || fail "launcher accepted a fifth concurrent ticket"
grep -Fqx "dispatcher capacity is full" "$TMP/claim-205.out" ||
  fail "launcher fifth-ticket capacity refusal was not deterministic"
run_launcher launchtest renew --ticket T-201 --lease "$CLAIM_201_ID" > "$TMP/renew-201.json"
run_launcher launchtest release --ticket T-201 --lease "$CLAIM_201_ID" > "$TMP/release-201.json"
run_launcher launchtest release --ticket T-202 --lease "$CLAIM_202_ID" > "$TMP/release-202.json"
run_launcher launchtest release --ticket T-203 --lease "$CLAIM_203_ID" > "$TMP/release-203.json"
run_launcher launchtest release --ticket T-204 --lease "$CLAIM_204_ID" > "$TMP/release-204.json"
[[ ! -n "$(find "$LAUNCH_PRODUCT/factory/.dispatch-leases" -type f -print -quit)" ]] ||
  fail "launcher lease release left state behind"
rm -f "$LAUNCH_PRODUCT/factory/tickets/T-201.md" \
  "$LAUNCH_PRODUCT/factory/tickets/T-202.md" "$LAUNCH_PRODUCT/factory/tickets/T-203.md" \
  "$LAUNCH_PRODUCT/factory/tickets/T-204.md" "$LAUNCH_PRODUCT/factory/tickets/T-205.md"
rm -rf "$LAUNCH_PRODUCT/factory/.dispatch-leases"
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' > "$LAUNCH_PRODUCT/factory/PROJECT.env"

KITS_ROOT_PHYS="$(cd "$KITS_ROOT" && pwd -P)"
PROFILE_PHYS="$(cd "$PROFILE" && pwd -P)"
LAUNCHER_KITS_ROOT_OVERRIDE="$KITS_ROOT_PHYS//" \
  LAUNCHER_PROFILE_OVERRIDE="$PROFILE_PHYS/./" \
  run_launcher launchtest contract --json > "$TMP/component-normalization.json"
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
for _try in $(seq 1 1500); do
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
git -C "$LAUNCH_PRODUCT" config user.email "hermes-contract@test.local"
git -C "$LAUNCH_PRODUCT" config user.name "hermes-contract-test"
printf 'launcher worktree fixture\n' > "$LAUNCH_PRODUCT/README.md"
mkdir -p "$LAUNCH_PRODUCT/factory/tickets"
printf '# T-123\n\nState: Ready\n' > "$LAUNCH_PRODUCT/factory/tickets/T-123.md"
cat > "$LAUNCH_PRODUCT/.gitignore" <<'EOF'
factory/*-helper.env
factory/runs/
factory/runtime-ledger.csv
factory/linear-map.json
factory/.linear-sync.lock
factory/.linear-sync-cycle.lock
factory/.linear-operator-clears/
factory/.active-runs/
factory/.provider.lock/
factory/.dispatch-leases/
factory/test-adapter-gate
factory/test-model-args-only
EOF
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' > "$LAUNCH_PRODUCT/factory/PROJECT.env"
git -C "$LAUNCH_PRODUCT" add -A
git -C "$LAUNCH_PRODUCT" commit -qm "seed launcher worktree"
git -C "$LAUNCH_PRODUCT" push -q -u origin main

# Contract 1.8 exposes a read-only watch over only this project's production
# controller state. Its cursor must bind that exact path and project.
printf '%s\n' "$SHA_MODELS_V18" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_MODELS_V18" "$TREE_MODELS_V18" "$RELEASE_MODELS_V18"
python3 - "$EXPECTED_CONTROLLER_STATE/events/9000001-0000000000000001.json" \
  "$SHA_MODELS_V18" <<'PY'
import hashlib, json, os, sys
path, factory_sha = sys.argv[1:]
value = {
    "event": "state_machine_escalated",
    "factory_sha": factory_sha,
    "observed_at_epoch_ns": 9000001,
    "schema": "nysa.software-factory.controller-event/v1",
    "ticket": "T-901",
    "detail": "token=operator-watch-secret https://user:password@example.invalid/path",
}
raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
value["event_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
with open(path, "w", encoding="utf-8") as stream:
    stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
os.chmod(path, 0o600)
PY
run_launcher launchtest watch --json --limit 1 --idle-timeout-seconds 1 \
  > "$TMP/launcher-watch.jsonl"
python3 - "$TMP/launcher-watch.jsonl" "$EXPECTED_CONTROLLER_STATE" \
  "$ROOT/scripts/operator-event-watch.py" <<'PY'
import importlib.util, json, pathlib, sys
output, state, helper = sys.argv[1:]
value = json.loads(pathlib.Path(output).read_text())
assert value["schema"] == "nysa.software-factory.operator-watch-event/v1"
assert value["project"] == "launchtest"
assert value["ticket"] == "T-901"
assert value["action"] == "blocked_escalated"
spec = importlib.util.spec_from_file_location("operator_event_watch", helper)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.decode_cursor(pathlib.Path(state), "launchtest", value["cursor"])[0] == \
    "9000001-0000000000000001.json"
PY
assert_no_secret "$TMP/launcher-watch.jsonl"
! grep -Fq "operator-watch-secret" "$TMP/launcher-watch.jsonl" ||
  fail "operator watch leaked source detail"
rm -f "$EXPECTED_CONTROLLER_STATE/events/9000001-0000000000000001.json"
python3 - "$EXPECTED_CONTROLLER_STATE/events/9000002-0000000000000002.json" \
  "$EXPECTED_CONTROLLER_STATE/events/9000003-0000000000000003.json" \
  "$SHA_MODELS_V18" <<'PY'
import hashlib, json, os, sys
for path, epoch, ticket, factory_sha in (
    (sys.argv[1], 9000002, "T-902", None),
    (sys.argv[2], 9000003, "T-903", sys.argv[3]),
):
    value = {
        "event": "budget_wait",
        "factory_sha": factory_sha,
        "observed_at_epoch_ns": epoch,
        "schema": "nysa.software-factory.controller-event/v1",
        "ticket": ticket,
    }
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    value["event_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)
PY
run_launcher launchtest watch --json --limit 1 --idle-timeout-seconds 1 \
  > "$TMP/launcher-watch-diagnostic.json"
python3 - "$TMP/launcher-watch-diagnostic.json" > "$TMP/launcher-watch.cursor" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["schema"] == "nysa.software-factory.operator-watch-diagnostic/v1"
assert value["action"] == "invalid_action_context"
assert value["reason"] == "factory_identity_unavailable"
assert value["factory_sha"] is None
assert value["ticket"] == "T-902"
print(value["cursor"])
PY
IFS= read -r WATCH_DIAGNOSTIC_CURSOR < "$TMP/launcher-watch.cursor"
[[ "${#WATCH_DIAGNOSTIC_CURSOR}" -le 1024 &&
   "$WATCH_DIAGNOSTIC_CURSOR" =~ ^[A-Za-z0-9_-]+$ ]] ||
  fail "sealed operator watch emitted an invalid diagnostic cursor"
run_launcher launchtest watch --json \
  --cursor "$WATCH_DIAGNOSTIC_CURSOR" \
  --limit 1 --idle-timeout-seconds 1 > "$TMP/launcher-watch-after.json"
python3 - "$TMP/launcher-watch-after.json" > "$TMP/launcher-watch-after.cursor" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["schema"] == "nysa.software-factory.operator-watch-event/v1"
assert value["action"] == "budget_halt"
assert value["ticket"] == "T-903"
assert isinstance(value["factory_sha"], str) and len(value["factory_sha"]) == 40
print(value["cursor"])
PY
IFS= read -r WATCH_ACTION_CURSOR < "$TMP/launcher-watch-after.cursor"
[[ "${#WATCH_ACTION_CURSOR}" -le 1024 &&
   "$WATCH_ACTION_CURSOR" =~ ^[A-Za-z0-9_-]+$ ]] ||
  fail "sealed operator watch emitted an invalid action cursor"
run_launcher launchtest watch --json \
  --cursor "$WATCH_ACTION_CURSOR" \
  --idle-timeout-seconds 1 > "$TMP/launcher-watch-idle.out"
[[ ! -s "$TMP/launcher-watch-idle.out" ]] ||
  fail "sealed operator watch repeated a handled record after idle restart"
rm -f "$EXPECTED_CONTROLLER_STATE/events/9000002-0000000000000002.json" \
  "$EXPECTED_CONTROLLER_STATE/events/9000003-0000000000000003.json"

# The sealed watcher skips intermediate recovery failures, projects both
# terminal recovery stops, and resumes to a later sibling action exactly once.
python3 - "$EXPECTED_CONTROLLER_STATE/events" "$SHA_MODELS_V18" <<'PY'
import hashlib, json, os, pathlib, sys
events, factory_sha = pathlib.Path(sys.argv[1]), sys.argv[2]
values = (
    (9000004, "0000000000000004", {
        "event": "role_blocked",
        "factory_sha": factory_sha,
        "role": "builder",
        "role_exit": "provider_failed",
        "run_id": "run-timeout",
        "terminal_reason_code": "soft_timeout",
        "ticket": "T-904",
    }),
    (9000005, "0000000000000005", {
        "event": "typed_recovery_refused",
        "factory_sha": factory_sha,
        "reason": "manifest",
        "recovery_kind": "qualification_fallback",
        "ticket": "T-905",
    }),
    (9000006, "0000000000000006", {
        "error": "token=intermediate-recovery-secret",
        "event": "ticket_recovery_failed",
        "factory_sha": factory_sha,
        "recovery": "release-upgrade",
        "ticket": "T-905",
    }),
    (9000007, "0000000000000007", {
        "attempts": 3,
        "event": "ticket_recovery_abandoned",
        "factory_sha": factory_sha,
        "input_sha256": "d" * 64,
        "outcome_sha256": "e" * 64,
        "recovery": "release-upgrade",
        "ticket": "T-906",
    }),
    (9000008, "0000000000000008", {
        "event": "awaiting_approval",
        "factory_sha": factory_sha,
        "passport_sha256": "f" * 64,
        "ticket": "T-907",
    }),
)
for epoch, token, value in values:
    value.update(
        observed_at_epoch_ns=epoch,
        schema="nysa.software-factory.controller-event/v1",
    )
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    value["event_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    path = events / f"{epoch}-{token}.json"
    path.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
PY
run_launcher launchtest watch --json --limit 1 --idle-timeout-seconds 1 \
  > "$TMP/launcher-watch-timeout.json"
python3 - "$TMP/launcher-watch-timeout.json" \
  > "$TMP/launcher-watch-timeout.cursor" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["action"] == "progress_timeout"
assert value["reason"] == "soft_timeout"
assert value["ticket"] == "T-904"
print(value["cursor"])
PY
IFS= read -r WATCH_TIMEOUT_CURSOR < "$TMP/launcher-watch-timeout.cursor"
run_launcher launchtest watch --json --cursor "$WATCH_TIMEOUT_CURSOR" \
  --limit 1 --idle-timeout-seconds 1 \
  > "$TMP/launcher-watch-recovery-refused.json"
python3 - "$TMP/launcher-watch-recovery-refused.json" \
  > "$TMP/launcher-watch-recovery-refused.cursor" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["action"] == "blocked_escalated"
assert value["reason"] == "qualification_fallback:manifest"
assert value["ticket"] == "T-905"
print(value["cursor"])
PY
IFS= read -r WATCH_RECOVERY_CURSOR \
  < "$TMP/launcher-watch-recovery-refused.cursor"
run_launcher launchtest watch --json --cursor "$WATCH_RECOVERY_CURSOR" \
  --limit 1 --idle-timeout-seconds 1 \
  > "$TMP/launcher-watch-recovery-abandoned.json"
python3 - "$TMP/launcher-watch-recovery-abandoned.json" \
  > "$TMP/launcher-watch-recovery-abandoned.cursor" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["action"] == "blocked_escalated"
assert value["ticket"] == "T-906"
assert value["reason"] == (
    "recovery_abandoned:release-upgrade:attempts=3:"
    + "input_sha256=" + "d" * 64 + ":outcome_sha256=" + "e" * 64
)
print(value["cursor"])
PY
assert_no_secret "$TMP/launcher-watch-recovery-refused.json"
assert_no_secret "$TMP/launcher-watch-recovery-abandoned.json"
! grep -Fq "intermediate-recovery-secret" \
  "$TMP/launcher-watch-recovery-abandoned.json" ||
  fail "sealed operator watch projected an intermediate recovery failure"
IFS= read -r WATCH_ABANDONED_CURSOR \
  < "$TMP/launcher-watch-recovery-abandoned.cursor"
run_launcher launchtest watch --json --cursor "$WATCH_ABANDONED_CURSOR" \
  --limit 1 --idle-timeout-seconds 1 \
  > "$TMP/launcher-watch-recovery-sibling.json"
python3 - "$TMP/launcher-watch-recovery-sibling.json" \
  > "$TMP/launcher-watch-recovery-sibling.cursor" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["action"] == "awaiting_approval"
assert value["ticket"] == "T-907"
print(value["cursor"])
PY
IFS= read -r WATCH_RECOVERY_SIBLING_CURSOR \
  < "$TMP/launcher-watch-recovery-sibling.cursor"
run_launcher launchtest watch --json --cursor "$WATCH_RECOVERY_SIBLING_CURSOR" \
  --idle-timeout-seconds 1 > "$TMP/launcher-watch-recovery-idle.out"
[[ ! -s "$TMP/launcher-watch-recovery-idle.out" ]] ||
  fail "sealed operator watch repeated a handled recovery record"
rm -f "$EXPECTED_CONTROLLER_STATE/events/900000"{4,5,6,7,8}-*.json

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

printf '%s\n' "$SHA_MODELS_V18" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_MODELS_V18" "$TREE_MODELS_V18" "$RELEASE_MODELS_V18"
PIN_HEAD_BEFORE="$(git -C "$RUN_WORKTREE_PHYS" rev-parse HEAD)"
if ! run_launcher launchtest models pin-batch \
  --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" \
  --json > "$TMP/models-pin.json"; then
  cat "$TMP/models-pin.json" >&2
  fail "valid batch model pin invocation failed"
fi
python3 - "$TMP/models-pin.json" "$RUN_WORKTREE_PHYS" "$SHA_MODELS_V18" <<'PY'
import json, pathlib, subprocess, sys
path, workdir, kit_sha = sys.argv[1:]
batch = json.load(open(path, encoding="utf-8"))
assert batch["schema"] == "model-pin-batch/v1", batch
assert batch["status"] == "ok" and len(batch["pins"]) == 1, batch
value = batch["pins"][0]
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

# A post-push dependency replay is capability-bound by the exact dispatcher
# lease and does not reopen the already-consumed transition receipt grammar.
printf '%s\n' 'TICKET_BRANCH_PREFIX=ticket/' 'MAX_CONCURRENT_TICKETS=4' > \
  "$LAUNCH_PRODUCT/factory/PROJECT.env"
git -C "$LAUNCH_PRODUCT" add factory/PROJECT.env
git -C "$LAUNCH_PRODUCT" commit -qm "enable replay lease fixture"
git -C "$LAUNCH_PRODUCT" push -q origin main
write_active "$SHA_MODELS_V18" "$TREE_MODELS_V18" "$RELEASE_MODELS_V18"
run_launcher launchtest claim --ticket T-123 > "$TMP/replay-claim.json"
REPLAY_LEASE="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["lease_id"])' \
  "$TMP/replay-claim.json")"
run_launcher launchtest ticket-attest --ticket T-123 --lease "$REPLAY_LEASE" \
  --workdir "$RUN_WORKTREE_PHYS" --action dependency-refresh-replay --json \
  > "$TMP/dependency-replay.json"
python3 - "$TMP/dependency-replay.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value == {"action": "dependency-refresh-replay", "status": "ok"}, value
PY
REPLAY_MISSING_RC=0
run_launcher launchtest ticket-attest --ticket T-123 \
  --workdir "$RUN_WORKTREE_PHYS" --action dependency-refresh-replay --json \
  > "$TMP/dependency-replay-missing.out" 2>&1 || REPLAY_MISSING_RC=$?
[[ "$REPLAY_MISSING_RC" -eq 1 ]] || fail "receipt replay accepted no dispatcher lease"
REPLAY_WRONG_RC=0
run_launcher launchtest ticket-attest --ticket T-123 --lease \
  "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff" \
  --workdir "$RUN_WORKTREE_PHYS" --action dependency-refresh-replay --json \
  > "$TMP/dependency-replay-wrong.out" 2>&1 || REPLAY_WRONG_RC=$?
[[ "$REPLAY_WRONG_RC" -eq 1 ]] || fail "receipt replay accepted the wrong dispatcher lease"
ORDINARY_NO_RECEIPT_RC=0
run_launcher launchtest ticket-attest --ticket T-123 --lease "$REPLAY_LEASE" \
  --workdir "$RUN_WORKTREE_PHYS" --action dependency-refresh --json \
  > "$TMP/dependency-refresh-no-receipt.out" 2>&1 || ORDINARY_NO_RECEIPT_RC=$?
[[ "$ORDINARY_NO_RECEIPT_RC" -eq 1 ]] ||
  fail "ordinary dependency refresh stopped requiring its one-use receipt"
run_launcher launchtest release --ticket T-123 --lease "$REPLAY_LEASE" \
  > "$TMP/replay-release.json"
assert_helper_confinement "$MODEL_HELPER_ENV" absent present
assert_no_secret "$TMP/models-pin.json"
if ! run_launcher launchtest models pin --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" \
  --json > "$TMP/models-pin-again.json"; then
  cat "$TMP/models-pin-again.json" >&2
  fail "idempotent model pin invocation failed"
fi
python3 - "$TMP/models-pin.json" "$TMP/models-pin-again.json" <<'PY'
import json, sys
first = json.load(open(sys.argv[1], encoding="utf-8"))["pins"][0]
second = json.load(open(sys.argv[2], encoding="utf-8"))
assert second["commit_created"] is False, second
assert second["commit_sha"] == first["commit_sha"], (first, second)
assert second["pin_hash"] == first["pin_hash"], (first, second)
PY

run_launcher launchtest passport verify-model-identity-success \
  --ticket T-123 \
  --receipt aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --run-id failed-run-1 --workdir "$RUN_WORKTREE_PHYS" --json \
  > "$TMP/passport-model-identity.json"
python3 - "$TMP/passport-model-identity.json" <<'PY'
import json
import sys

arguments = json.load(open(sys.argv[1], encoding="utf-8"))["arguments"]
assert arguments[0] == "verify-model-identity-success", arguments
assert arguments[arguments.index("--receipt") + 1] == "a" * 64, arguments
assert arguments[arguments.index("--run-id") + 1] == "failed-run-1", arguments
PY
BAD_PASSPORT_IDENTITY_RC=0
run_launcher launchtest passport verify-model-identity-success \
  --ticket T-123 \
  --receipt aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --workdir "$RUN_WORKTREE_PHYS" --json \
  > "$TMP/bad-passport-model-identity.out" 2>&1 || BAD_PASSPORT_IDENTITY_RC=$?
[[ "$BAD_PASSPORT_IDENTITY_RC" -eq 2 ]] ||
  fail "model-identity passport verification accepted a missing run identifier"

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
expect_bad_model missing-migration-readiness migrate --ticket T-123 \
  --workdir "$RUN_WORKTREE_PHYS" \
  --approve-hash aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --approved-by operator-1 --json
expect_bad_model pin-batch-duplicate pin-batch \
  --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" \
  --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" --json

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
expect_bad_model maintenance-pin-batch pin-batch \
  --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" --json
rm -f "$LAUNCH_PRODUCT/factory/MAINTENANCE"
# Keep later launcher/run accounting fixtures independent from the model-state
# mutation coverage above.
rm -rf "$KITS_ROOT/projects/launchtest/routing"
rm -f "$TEST_HOME/.factory/global.env"

# Bash 3.2 with nounset rejects expansion of an empty array. A fallback without
# a Reviewer exception must still reach the selected helper with only base args.
printf '%s\n' "$SHA_FALLBACK" > "$LAUNCH_PRODUCT/factory/KIT_PIN"
write_active "$SHA_FALLBACK" "$TREE_FALLBACK" "$RELEASE_FALLBACK"
touch "$LAUNCH_PRODUCT/factory/test-model-args-only"
run_launcher launchtest models inventory --json > "$TMP/models-inventory.out"
grep -qFx 'ARG=inventory' "$TMP/models-inventory.out" ||
  fail "sealed model inventory did not reach the selected helper"
run_launcher launchtest models migrate \
  --ticket T-123 --workdir "$RUN_WORKTREE_PHYS" \
  --approve-hash aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --readiness-hash bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --approved-by tester --json > "$TMP/models-migrate-auth.out"
grep -qFx 'ARG=migrate' "$TMP/models-migrate-auth.out" ||
  fail "sealed route migration did not reach the selected helper"
grep -qFx 'GITHUB_TOKEN_FD_PRESENT=true' "$MODEL_HELPER_ENV" ||
  fail "route migration did not receive the profile credential pipe"
grep -qFx "GITHUB_TOKEN_FD_CKSUM=$(printf '%s' "$GH_SECRET" | cksum)" "$MODEL_HELPER_ENV" ||
  fail "route migration received a caller credential instead of the profile credential"
assert_no_secret "$MODEL_HELPER_ENV"
run_launcher launchtest models fallback-plan \
  --ticket T-123 --failed-run failed-run-1 --workdir "$RUN_WORKTREE_PHYS" \
  --reason provider_unavailable --json > "$TMP/models-fallback-no-exception.out"
for expected in fallback-plan T-123 failed-run-1 "$RUN_WORKTREE_PHYS" provider_unavailable; do
  grep -qFx "ARG=$expected" "$TMP/models-fallback-no-exception.out" ||
    fail "fallback without Reviewer exception omitted base argument: $expected"
done
! grep -qF 'allow-reviewer-family' "$TMP/models-fallback-no-exception.out" ||
  fail "fallback without Reviewer exception invented one"
grep -qFx 'GITHUB_TOKEN_FD_PRESENT=true' "$MODEL_HELPER_ENV" ||
  fail "fallback did not receive the profile credential pipe"
grep -qFx "GITHUB_TOKEN_FD_CKSUM=$(printf '%s' "$GH_SECRET" | cksum)" "$MODEL_HELPER_ENV" ||
  fail "fallback received a caller credential instead of the profile credential"
assert_no_secret "$MODEL_HELPER_ENV"
rm -f "$LAUNCH_PRODUCT/factory/test-model-args-only"

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
python3 - "$RELEASE_C/integrations/hermes/contract.json" <<'PY'
import json, pathlib, sys
path=pathlib.Path(sys.argv[1])
value=json.loads(path.read_text())
value["contract_version"]="1.7.0"
path.write_text(json.dumps(value,indent=2)+"\n")
PY
cp -R "$ROOT/roles" "$RELEASE_C/"
cp -R "$ROOT/scripts/lib" "$RELEASE_C/scripts/"
cp -R "$ROOT/scripts/adapters" "$RELEASE_C/scripts/"
for helper in preflight.sh next-stage.sh run-agent.sh ticket-state.sh ticket-pr.py ledger-view.py envelope-control.py reorder-test-fixes.sh dispatch-lease.sh dispatch-lease-heartbeat.py dispatch-plan.py model-control.sh model-manager.py model-router.py; do
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
for ticket in T-777 T-778 T-779 T-780 T-781 T-782; do
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
  'PREVIEW_PROVIDER=railway' \
  > "$LAUNCH_PRODUCT/factory/PROJECT.env"
git -C "$LAUNCH_PRODUCT" add factory/tickets/T-77{7,8,9}.md \
  factory/tickets/T-78{0,1,2}.md factory/initiatives/I-777.md \
  factory/ENVELOPE.env factory/ledger.csv factory/PROJECT.env factory/KIT_PIN
git -C "$LAUNCH_PRODUCT" commit -qm "seed contract 1.2 ticket"
git -C "$LAUNCH_PRODUCT" push -q origin main
write_active "$SHA_C" "$REAL_TREE" "$RELEASE_C"
python3 - "$LAUNCH_PRODUCT/factory/linear-map.json" <<'PY'
import datetime
import json
import sys
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"_sync": {"last_success_at": now}, "tickets": {}}, handle)
    handle.write("\n")
PY
run_launcher launchtest dispatch-plan --shadow --json > "$TMP/dispatch-shadow.json"
python3 - "$TMP/dispatch-shadow.json" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["action"] == "SHADOW"
assert value["status"] == "SHADOW"
assert value["ticket"] == "T-777"
PY
[[ ! -e "$TEST_HOME/.factory/worktrees" ]] ||
  fail "dispatch shadow created the trusted worktree root"

expect_bad_dispatch() {
  local label="$1" rc=0
  shift
  run_launcher launchtest dispatch-plan "$@" \
    > "$TMP/bad-dispatch-$label.out" 2>&1 || rc=$?
  [[ "$rc" -ne 0 ]] || fail "invalid dispatch override was accepted: $label"
}
expect_bad_dispatch zero --shadow --max-linear-age 0 --json
expect_bad_dispatch over --shadow --max-linear-age 601 --json
expect_bad_dispatch noninteger --shadow --max-linear-age 3.0 --json
expect_bad_dispatch duplicate --shadow --max-linear-age 300 \
  --max-linear-age 300 --json
expect_bad_dispatch claim-override --claim --max-linear-age 300 --json

python3 - "$LAUNCH_PRODUCT/factory/linear-map.json" <<'PY'
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["_sync"]["last_success_at"] = (
    datetime.datetime.now(datetime.timezone.utc)
    - datetime.timedelta(seconds=290)
).isoformat()
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
dispatch_state_sha256() {
  python3 - "$LAUNCH_PRODUCT/factory" <<'PY'
import hashlib
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    info = path.lstat()
    digest.update(str(path.relative_to(root)).encode() + b"\0")
    digest.update(f"{stat.S_IMODE(info.st_mode):04o}".encode() + b"\0")
    if path.is_file() and not path.is_symlink():
        digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}
dispatch_state_sha256 > "$TMP/dispatch-before.sha256"
run_launcher launchtest dispatch-plan --shadow --max-linear-age 300 --json \
  > "$TMP/dispatch-near-ttl.json"
python3 - "$TMP/dispatch-near-ttl.json" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
assert (value["status"], value["action"], value["ticket"]) == (
    "SHADOW", "SHADOW", "T-777",
)
PY
dispatch_state_sha256 > "$TMP/dispatch-after.sha256"
cmp "$TMP/dispatch-before.sha256" "$TMP/dispatch-after.sha256" >/dev/null ||
  fail "near-TTL dispatch shadow mutated Factory state"
[[ ! -e "$TEST_HOME/.factory/worktrees" ]] ||
  fail "near-TTL dispatch shadow created the trusted worktree root"
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
REAL_RUN_WORKTREE_781="$TMP/real-run-worktree-781"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-781 "$REAL_RUN_WORKTREE_781"
REAL_RUN_WORKTREE_781_PHYS="$(cd "$REAL_RUN_WORKTREE_781" && pwd -P)"
REAL_RUN_WORKTREE_782="$TMP/real-run-worktree-782"
git -C "$LAUNCH_PRODUCT" worktree add -q -b ticket/T-782 "$REAL_RUN_WORKTREE_782"
REAL_RUN_WORKTREE_782_PHYS="$(cd "$REAL_RUN_WORKTREE_782" && pwd -P)"

ROLELESS_PREFLIGHT_RC=0
run_launcher launchtest preflight --ticket T-777 \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json >/dev/null 2>&1 ||
  ROLELESS_PREFLIGHT_RC=$?
[[ "$ROLELESS_PREFLIGHT_RC" -eq 2 ]] ||
  fail "contract 1.5 accepted preflight without an exact role"

printf '\nUncommitted-Evidence: forged\n' >> \
  "$REAL_RUN_WORKTREE_PHYS/factory/tickets/T-777.md"
DIRTY_PREFLIGHT_RC=0
run_launcher launchtest preflight --ticket T-777 --role planner \
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
run_launcher launchtest ticket-state --ticket T-781 --workdir "$REAL_RUN_WORKTREE_781_PHYS" \
  --action materialize --json > "$TMP/real-ticket-state-781.json"
run_launcher launchtest ticket-state --ticket T-782 --workdir "$REAL_RUN_WORKTREE_782_PHYS" \
  --action materialize --json > "$TMP/real-ticket-state-782.json"

# Development and Hermes consume the same trusted Reviewer reconciliation.
mkdir -p "$LAUNCH_PRODUCT/factory/runs"
for transition in Planning Building Review; do
  run_launcher launchtest ticket-state --ticket T-781 \
    --workdir "$REAL_RUN_WORKTREE_781_PHYS" \
    --action transition --state "$transition" --json >/dev/null
  run_launcher launchtest ticket-state --ticket T-782 \
    --workdir "$REAL_RUN_WORKTREE_782_PHYS" \
    --action transition --state "$transition" --json >/dev/null
done
for ticket in T-781 T-782; do
  worktree="$REAL_RUN_WORKTREE_781_PHYS"
  [[ "$ticket" == T-781 ]] || worktree="$REAL_RUN_WORKTREE_782_PHYS"
  output="$LAUNCH_PRODUCT/factory/runs/reviewer-$ticket.out"
  manifest="$LAUNCH_PRODUCT/factory/runs/reviewer-$ticket.meta"
  printf '%s\n' 'Review complete.' 'REQUEST CHANGES' 'FIX-OWNER: both' > "$output"
  head="$(git -C "$worktree" rev-parse HEAD)"
  digest="$(shasum -a 256 "$output" | awk '{print $1}')"
  printf '%s\n' \
    "run_id=reviewer-$ticket" "ticket=$ticket" 'role=reviewer' 'adapter=codex' \
    'contract_version=1.7.0' 'role_exit=ok' \
    "role_head_before=$head" "role_remote_before=$head" "output_sha256=$digest" \
    'accounting_state=completed' 'exit_status=0' 'started_at=2026-07-23T00:00:00Z' \
    > "$manifest"
done
FACTORY_ROOT="$LAUNCH_PRODUCT" \
FACTORY_CERTIFIED_PRODUCT_ORIGIN="$LAUNCH_PRODUCT_REMOTE" \
FACTORY_HERMES_CONTRACT_VERSION=1.7.0 \
  "$RELEASE_C_PHYS/scripts/ticket-state.sh" \
    --ticket T-781 --workdir "$REAL_RUN_WORKTREE_781_PHYS" \
    --action reviewer-reconcile > "$TMP/direct-reviewer-reconcile.json"
run_launcher launchtest ticket-state --ticket T-782 \
  --workdir "$REAL_RUN_WORKTREE_782_PHYS" \
  --action reviewer-reconcile --json > "$TMP/launcher-reviewer-reconcile.json"
python3 - \
  "$REAL_RUN_WORKTREE_781_PHYS/factory/tickets/T-781.md" \
  "$REAL_RUN_WORKTREE_782_PHYS/factory/tickets/T-782.md" <<'PY'
import re
import sys

left = open(sys.argv[1], encoding="utf-8").read().replace("T-781", "T-TEST")
right = open(sys.argv[2], encoding="utf-8").read().replace("T-782", "T-TEST")
assert left == right
assert re.search(r"^State: Building$", left, re.M)
assert left.count("reviewer round 1: REQUEST CHANGES") == 1
assert left.count("reviewer round 1 FIX-OWNER: both") == 1
PY
run_launcher launchtest ticket-state --ticket T-782 \
  --workdir "$REAL_RUN_WORKTREE_782_PHYS" \
  --action reviewer-reconcile --json > "$TMP/launcher-reviewer-reconcile-replay.json"
printf '%s\n' 'tampered output' >> "$LAUNCH_PRODUCT/factory/runs/reviewer-T-782.out"
REVIEWER_OUTPUT_DRIFT_RC=0
run_launcher launchtest ticket-state --ticket T-782 \
  --workdir "$REAL_RUN_WORKTREE_782_PHYS" \
  --action reviewer-reconcile --json >/dev/null 2>&1 || REVIEWER_OUTPUT_DRIFT_RC=$?
[[ "$REVIEWER_OUTPUT_DRIFT_RC" -eq 1 ]] ||
  fail "reviewer reconciliation accepted output drift"
rm -f "$LAUNCH_PRODUCT/factory/runs/reviewer-T-78"{1,2}.{meta,out}

assert_bad_real_preflight() {
  local label="$1" rc=0
  shift
  run_launcher launchtest preflight --ticket T-777 --role planner "$@" \
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
if ! run_launcher launchtest preflight --ticket T-779 --role planner \
  --lease "$REAL_LEASE_779" \
  --workdir "$REAL_RUN_WORKTREE_779_PHYS" --json \
  > "$TMP/real-preflight-779.json" 2>&1; then
  sed 's/^/T-779 preflight: /' "$TMP/real-preflight-779.json" >&2
  fail "first concurrent-capacity preflight failed"
fi
if ! run_launcher launchtest preflight --ticket T-780 --role planner \
  --lease "$REAL_LEASE_780" \
  --workdir "$REAL_RUN_WORKTREE_780_PHYS" --json \
  > "$TMP/real-preflight-780.json" 2>&1; then
  sed 's/^/T-780 preflight: /' "$TMP/real-preflight-780.json" >&2
  fail "second concurrent-capacity preflight failed"
fi
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
if ! run_launcher launchtest preflight --ticket T-777 --role planner \
  --lease "$REAL_LEASE_ID" \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json > "$TMP/real-preflight.json"; then
  awk '{print}' "$TMP/real-preflight.json" >&2
  git -C "$LAUNCH_PRODUCT" status --short >&2
  git -C "$REAL_RUN_WORKTREE_PHYS" status --short >&2
  fail "contract 1.2 sealed preflight failed"
fi
run_launcher launchtest preflight --ticket T-778 --role planner \
  --lease "$REAL_LEASE_778" \
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
assert "planner attempt envelope:" in preflight["output"]
assert stage["status"] == "ok"
assert stage["action"] == "RUN"
assert stage["detail"] == "planner"
assert stage["output"] == "RUN planner\n"
PY
TICKET_PR_STAGE_RC=0
run_launcher launchtest ticket-pr --ticket T-777 --lease "$REAL_LEASE_ID" \
  --workdir "$REAL_RUN_WORKTREE_PHYS" --json > "$TMP/real-ticket-pr-stage.json" ||
  TICKET_PR_STAGE_RC=$?
[[ "$TICKET_PR_STAGE_RC" -eq 2 ]] ||
  fail "ticket-pr accepted a ticket before the reviewer boundary"
python3 - "$TMP/real-ticket-pr-stage.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
assert result["schema"] == "nysa.software-factory.ticket-pr/v1"
assert result["status"] == "error"
assert "reviewer or narrator stage" in result.get("error", "")
assert "dispatcher lease" not in result["error"]
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
grep -qF "contract_version=1.7.0" "$REAL_MANIFEST" || fail "real sealed run manifest omitted release contract"
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
import pathlib
import plistlib
import re
import subprocess
import sys
import tempfile
import textwrap

root, contract_path = sys.argv[1:]
with open(contract_path, encoding="utf-8") as handle:
    contract = json.load(handle)

assert contract["contract"] == "nysa.software-factory.hermes"
assert contract["contract_version"] == "1.8.0"
assert contract["doctor_schema"] == "nysa.software-factory.hermes-doctor/v1"
assert contract["preflight_schema"] == "nysa.software-factory.preflight/v1"
assert contract["next_stage_schema"] == "nysa.software-factory.next-stage/v1"
assert contract["state_machine_schema"] == "nysa.software-factory.state-machine/v1"
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
assert commands["reconcile"]["output_schema"] == \
    "nysa.software-factory.controller/v1"
assert commands["watch"]["output_schema"] == \
    "nysa.software-factory.operator-watch-event/v1"
assert commands["watch"]["diagnostic_schema"] == \
    "nysa.software-factory.operator-watch-diagnostic/v1"
assert commands["qualification"]["output_schema"] == \
    "nysa.software-factory.qualification-report/v1"
assert commands["state-machine"]["output_schema"] == \
    "nysa.software-factory.state-machine/v1"
assert commands["passport"]["output_schema"] == \
    "nysa.software-factory.ticket-passport/v1"
assert commands["publication"]["output_schema"] == \
    "nysa.software-factory.publication-lease/v1"
assert commands["publication-repair"]["output_schema"] == \
    "nysa.software-factory.publication-repair/v1"
assert commands["ci-rerun"]["output_schema"] == \
    "nysa.software-factory.ci-rerun/v1"
dispatch = commands["dispatch-plan"]
assert dispatch["minimum_contract_version"] == "1.6.0"
assert dispatch["grammars"] == [
    "--shadow --json",
    "--claim --json",
    "--claim [--exclude-ticket <T-NNN> ...] --json",
]
assert dispatch["output_schema"] == "nysa.software-factory.dispatch-plan/v1"
ticket_pr = commands["ticket-pr"]
assert ticket_pr["minimum_contract_version"] == "1.6.0"
assert ticket_pr["output_schema"] == "nysa.software-factory.ticket-pr/v1"
assert commands["contract"]["arguments"] == ["--json"]
assert commands["doctor"]["output_schema"] == contract["doctor_schema"]
assert commands["linear-sync"]["arguments"] == []
assert commands["linear-sync"]["contract_version"] == "1.8.0"
assert commands["linear-sync"]["helper"] == "scripts/linear-sync.py"
assert commands["models"]["minimum_contract_version"] == "1.2.0"
assert commands["models"]["helper"] == "scripts/model-control.sh"
assert commands["models"]["grammars"] == [
    "profiles --json",
    "status --json",
    "inventory --json",
    "policy-candidates --json",
    "policy-preview --policy <canonical-json> --json",
    "policy-apply --policy <canonical-json> --expected-current-hash <lowercase-sha256> --approve-hash <lowercase-sha256> --json",
    "reviewer-exception-contract --json",
    "ticket-status --ticket <T-NNN> --json",
    "plan --json",
    "plan --profile <safe-id> --json",
    "activate --profile <safe-id> --approve-hash <lowercase-sha256> --approved-by <safe-id> --json",
    "disable --scope-type <account-route|provider-family|model|route> --scope-id <safe-selection-or-id> --reason credits_exhausted --ttl-seconds <1..604800> --operator-id <safe-id> --json",
    "enable --scope-type <account-route|provider-family|model|route> --scope-id <safe-selection-or-id> --json",
    "pin-batch [--ticket <T-NNN> --workdir <exact-ticket-worktree>]... --json (one to four unique tickets)",
    "pin --ticket <T-NNN> --workdir <exact-ticket-worktree> --json",
    "migrate-plan --ticket <T-NNN> --workdir <exact-clean-ticket-worktree> [--include-journal] --json",
    "migrate --ticket <T-NNN> --workdir <exact-clean-ticket-worktree> --approve-hash <lowercase-sha256> --readiness-hash <lowercase-sha256> --approved-by <safe-id> --json",
    "qualification-readiness --json (qualification launcher, Contract 1.8)",
    "fallback-plan --ticket <T-NNN> --failed-run <safe-run-id> --workdir <exact-ticket-worktree> --reason <credits_exhausted|provider_unavailable> --json",
    "fallback-auto --ticket <T-NNN> --failed-run <safe-run-id> --workdir <exact-ticket-worktree> --reason <credits_exhausted|provider_unavailable> --json",
    "fallback --ticket <T-NNN> --failed-run <safe-run-id> --workdir <exact-ticket-worktree> --reason <credits_exhausted|provider_unavailable> [--allow-reviewer-family <safe-id>] --json",
]
assert commands["models"]["output_schemas"]["inventory"] == "factory-cursor-model-inventory/v1"
assert commands["models"]["output_schemas"]["qualification-readiness"] == \
    "nysa.software-factory.qualification-fallback-readiness/v1"
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
    "allowed": [
        "profiles", "status", "inventory", "policy-candidates", "policy-preview",
        "reviewer-exception-contract", "ticket-status", "plan",
        "migrate-plan", "qualification-readiness", "fallback-plan",
    ],
    "refused": [
        "activate", "disable", "enable", "policy-apply", "pin", "migrate",
        "fallback-auto", "fallback",
    ],
}
assert commands["preflight"]["arguments"] == [
    "--ticket", "<T-NNN>", "--role", "<next-stage-role>",
    "[--lease <opaque-lease-id>]", "--receipt", "<lowercase-sha256>", "--workdir",
    "<absolute-product-worktree>", "--json",
]
assert commands["next-stage"]["arguments"] == [
    "--ticket", "<T-NNN>", "[--lease <opaque-lease-id>]", "--workdir",
    "<absolute-product-worktree>", "--json",
]
assert commands["next-stage"]["contract_1_3_terminal_action"].startswith(
    "COMPLETE means"
)
assert commands["ticket-state"]["arguments"] == [
    "--ticket", "<T-NNN>", "--workdir", "<absolute-product-worktree>",
    "--action", "<materialize|transition|reviewer-reconcile|qualification-backlog>",
    "[--state <ticket-state>]", "--json"
]
assert commands["ticket-state"]["transition_states"] == [
    "Planning", "Building", "Review", "Blocked-Escalated"
]
assert commands["ticket-attest"]["arguments"] == [
    "--ticket", "<T-NNN>", "[--lease <opaque-lease-id>]",
    "[--receipt <lowercase-sha256> (required for Contract 1.8 non-done, non-dependency-refresh-replay actions)]",
    "--workdir", "<absolute-worktree>",
    "--action", "<bundle|approval|dependency-refresh|dependency-refresh-replay|refresh|done|emergency-plan|emergency-apply>",
    "[--request <absolute-owner-request.json> --approve-hash <lowercase-sha256> (emergency only)]",
    "--json"
]
assert any("fresh review" in item and "stale bundle" in item
           for item in commands["ticket-attest"]["validation"])
assert any("dependency-refresh-replay" in item and "exact dispatcher lease" in item
           for item in commands["ticket-attest"]["validation"])
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
assert contract["concurrency"]["default"] == 4
assert contract["concurrency"]["maximum"] == 4
assert contract["concurrency"]["enabled_value"] == 4
assert contract["concurrency"]["enabled_values"] == [2, 3, 4]
assert "disposable per-ticket execution cells" in \
    contract["concurrency"]["capacity_scope"]
assert "provider lock" in contract["concurrency"]["parallel_execution_gate"]
assert contract["concurrency"]["lease_required_when_greater_than"] == 1
provider_execution = contract["provider_execution"]
assert provider_execution["contract_1_6_mode"] == "isolated-v1"
assert provider_execution["contract_1_7_mode"] == "cli-concurrent-v1"
assert provider_execution["contract_1_8_mode"] == "cli-concurrent-v1"
assert set(provider_execution["activation_schemas"]) == {
    "nysa.software-factory.provider-activation/v1",
    "nysa.software-factory.provider-activation/v2",
}
assert provider_execution["runtime_authority"] == "scripts/provider-runtime.py"
assert provider_execution["state_machine"] == [
    "prepared", "reserved", "GO", "submitted", "terminal",
]
assert "owner-only canonical activation" in provider_execution["activation_gate"]
assert "1.0.0 through 1.5.0" in provider_execution["legacy_contracts"]
assert provider_execution["coordinator"]["transaction"] == "BEGIN IMMEDIATE"
assert provider_execution["coordinator"]["database"].endswith("state-v2.sqlite3")
assert "integer micro-USD" in \
    provider_execution["coordinator"]["financial_accounting"]
broker = provider_execution["credential_broker"]
assert broker["authority"] == "scripts/provider-credential-broker.py"
assert broker["database"].endswith("credential-broker.sqlite3")
assert broker["token_binding"] == [
    "attempt_id", "route_id", "model", "reserve_micro_usd", "expires_at",
    "max_requests",
]
assert "TLS endpoint" in broker["transport"]
assert "no redirects" in broker["proxy_policy"]
assert "revokes" in broker["revocation"]
assert provider_execution["worker"]["request_schema"] == \
    "nysa.software-factory.provider-execution-request/v3"
assert provider_execution["worker"]["image_lock"] == "worker/image-lock.json"
assert provider_execution["worker"]["image_lock_schema"].endswith(
    "provider-worker-image-lock/v1"
)
assert provider_execution["worker"]["identity_binding"] == [
    "ticket", "role", "attempt_id", "base_sha", "input_sha256", "route_id",
    "policy_sha256", "image_digest", "source_sha256", "worker_sha256",
    "command",
]
assert provider_execution["worker"]["network"].startswith("none;")
assert "trusted host runtime" in provider_execution["worker"]["network"]
assert provider_execution["controller"]["artifact_authority"] == \
    "scripts/provider-artifact-controller.py"
assert provider_execution["controller"]["artifact_schema"].endswith(
    "provider-patch-artifact/v1"
)
assert provider_execution["recovery"]["authority"] == "scripts/provider-recovery.py"
assert "persist request before signaling" in \
    provider_execution["recovery"]["cancellation"]
assert "no isolated-v1 or cli-concurrent-v1 admission" in provider_execution["legacy_barrier"]
assert provider_execution["subscription_cli"]["authority"] == \
    "scripts/provider-cli-runtime.py"
assert provider_execution["subscription_cli"]["allowed_adapters"] == [
    "codex", "claude-code", "cursor-openai", "cursor-anthropic",
]
assert "ticket capacity" in provider_execution["subscription_cli"]["account_limits"]
assert "~/.factory/cli-runtimes" in provider_execution["subscription_cli"]["runtime_root"]
assert "policy digest" in provider_execution["subscription_cli"]["certification_binding"]
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
    "FACTORY_OPERATOR_MAP": "sealed qualification operator overlay path: owner-local lane state for isolated qualification or the canonical live map for takeover",
    "FACTORY_QUALIFICATION_MANIFEST": "sealed qualification manifest path supplied only by a qualification launcher",
    "FACTORY_QUALIFICATION_PRODUCT_SHA": "qualification receipt-bound protected product commit",
    "FACTORY_QUALIFICATION_PRODUCT_TREE": "qualification receipt-bound protected product tree",
    "FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256": "qualification receipt-bound native fallback readiness digest",
    "FACTORY_CERTIFIED_PRODUCT_ORIGIN": "contract 1.2+ certification receipt product_origin; consumed by trusted write helpers and never exposed to adapters",
    "FACTORY_DISPATCH_LEASE_ID": "validated optional ticket lease supplied by the dispatcher",
    "FACTORY_TRANSITION_RECEIPT_SHA256": "Contract 1.8 consumed one-use state-machine receipt",
    "FACTORY_TRANSITION_STATE_DIR": "Contract 1.8 owner-only controller state directory",
    "FACTORY_AUTHENTICATED_ROLE_EVIDENCE": "Contract 1.8 owner-only ephemeral completed-role sequence created only by the state machine for its next-stage child",
    "FACTORY_PROVIDER_DB": "fixed Contract 1.6+ owner-local transactional database",
    "FACTORY_PROVIDER_POLICY": "fixed Contract 1.6+ owner-local admission policy",
    "FACTORY_PROVIDER_BROKER_DB": "fixed Contract 1.6 owner-local broker database",
    "FACTORY_PROVIDER_CREDENTIALS": "fixed Contract 1.6 owner-local credential configuration",
    "FACTORY_PROVIDER_ARTIFACT_POLICY": "fixed Contract 1.6 owner-local artifact policy",
    "FACTORY_PROVIDER_ATTEMPT_ROOT": "fixed Contract 1.6+ owner-local attempt directory",
    "FACTORY_PROVIDER_APPLY_LOCK_ROOT": "fixed Contract 1.6+ owner-local apply-lock directory",
    "FACTORY_PROVIDER_CONFIGURATION_LOCK": "fixed Contract 1.8 owner-local provider-configuration lock",
    "FACTORY_PROVIDER_ACTIVATION": "fixed owner-local activation gate: Contract 1.6 API-only v1; Contract 1.7/1.8 API v1 or subscription-CLI v2",
    "FACTORY_CLI_RUNTIME_ROOT": "fixed Contract 1.7+ owner-local per-attempt subscription CLI runtime directory",
    "FACTORY_CURSOR_ACCOUNT_DB": "fixed machine-local owner-only Cursor account-route admission database shared by production and qualification",
    "FACTORY_PROVIDER_BROKER_URL": "fixed Contract 1.6 loopback TLS broker endpoint",
    "FACTORY_PROVIDER_BROKER_CA": "fixed Contract 1.6 broker trust anchor",
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
    "FACTORY_OPERATOR_MAP",
    "FACTORY_QUALIFICATION_MANIFEST",
    "FACTORY_QUALIFICATION_PRODUCT_SHA",
    "FACTORY_QUALIFICATION_PRODUCT_TREE",
    "FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256",
    "FACTORY_CERTIFIED_PRODUCT_ORIGIN",
    "FACTORY_DISPATCH_LEASE_ID",
    "FACTORY_TRANSITION_RECEIPT_SHA256",
    "FACTORY_TRANSITION_STATE_DIR",
    "FACTORY_AUTHENTICATED_ROLE_EVIDENCE",
    "FACTORY_PROVIDER_DB",
    "FACTORY_PROVIDER_POLICY",
    "FACTORY_PROVIDER_BROKER_DB",
    "FACTORY_PROVIDER_CREDENTIALS",
    "FACTORY_PROVIDER_ARTIFACT_POLICY",
    "FACTORY_PROVIDER_ATTEMPT_ROOT",
    "FACTORY_PROVIDER_APPLY_LOCK_ROOT",
    "FACTORY_PROVIDER_CONFIGURATION_LOCK",
    "FACTORY_PROVIDER_ACTIVATION",
    "FACTORY_CLI_RUNTIME_ROOT",
    "FACTORY_CURSOR_ACCOUNT_DB",
    "FACTORY_PROVIDER_BROKER_URL",
    "FACTORY_PROVIDER_BROKER_CA",
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
launcher = open(
    os.path.join(root, "integrations", "hermes", "bin", "factory-launch"),
    encoding="utf-8",
).read()
path_guard = launcher.split("reject_symlink_components() {", 1)[1].split("\n}", 1)[0]
assert "$PYTHON_BIN" not in path_guard
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
    "scripts/factory-controller.py",
    "scripts/qualification-environment.py",
    "scripts/state-machine.py",
    "scripts/emergency-admit.py",
    "scripts/ticket-passport.py",
    "scripts/publication-lease.py",
    "scripts/publication-conflict-policy.py",
    "scripts/ci-rerun.py",
    "scripts/ticket-readiness.py",
    "scripts/launchd/com.factory.controller.plist.template",
    "scripts/launchd/com.factory.linear-sync.plist.template",
    "scripts/provider-coordinator.py",
    "scripts/provider-cli-runtime.py",
    "scripts/provider-credential-broker.py",
    "scripts/provider-activation.py",
    "scripts/provider-concurrency-config.py",
    "scripts/provider-artifact-controller.py",
    "scripts/provider-executor.py",
    "scripts/provider-isolated-run.py",
    "scripts/provider-recovery.py",
    "scripts/provider-runtime.py",
    "scripts/provider-worker-image.py",
    "worker/Dockerfile",
    "worker/image-lock.json",
    "worker/provider-worker.mjs",
    "scripts/model-control.sh",
    "scripts/linear-sync-service.py",
    "scripts/model-manager.py",
    "scripts/model-router.py",
    "scripts/lib/backend-policy.sh",
    "scripts/lib/kit-pin.sh",
    "scripts/lib/plain-config.sh",
    "scripts/lib/product-remote.sh",
    "scripts/lib/legacy_closeout.py",
    "scripts/lib/terminal_backfill.py",
    "scripts/lib/protected_merge_reconciliation.py",
    "scripts/lib/qualification_manifest.py",
    "scripts/lib/inflight_release.py",
    "scripts/legacy-closeout.py",
    "scripts/protected-merge-reconciliation.py",
    "scripts/terminal-backfill.py",
    "scripts/model-routing/catalog-v1.json",
    "scripts/model-routing/profiles-v1.json",
    "factory/route-plans/<T-NNN>.json",
]:
    assert surface in contract["compatibility_sensitive_surfaces"], surface

launcher_text = open(
    os.path.join(root, "integrations", "hermes", "bin", "factory-launch"),
    encoding="utf-8",
).read()
assert "/private/tmp/nysa-sf-qualification" in launcher_text
assert 'optional = ("", "", "", "", "", "", "", "", "", "")' in launcher_text
assert 'WORKTREE_PARENT="$KITS_ROOT/worktrees"' in launcher_text
assert '"FACTORY_CLI_LANE_ROOT=$QUALIFICATION_ROOT"' in launcher_text
assert 'KIT_TRUST_SCOPE="qualification-candidate"' in launcher_text
assert '"FACTORY_KIT_TRUST_SCOPE=$KIT_TRUST_SCOPE"' in launcher_text
assert '"FACTORY_ROOT=$PRODUCT_ROOT"' in launcher_text
assert '"$PYTHON_BIN" -I -S "$LINEAR_SYNC"' in launcher_text
assert '"FACTORY_QUALIFICATION_MANIFEST=$PRODUCT_ROOT/factory/QUALIFICATION.json"' in launcher_text
assert '"FACTORY_QUALIFICATION_PRODUCT_SHA=$ACTIVE_PRODUCT_SHA"' in launcher_text
assert '"FACTORY_QUALIFICATION_PRODUCT_TREE=$ACTIVE_PRODUCT_TREE"' in launcher_text
assert '"FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256=$ACTIVE_FALLBACK_READINESS_SHA256"' in launcher_text
assert '"FACTORY_LEDGER=$ACTIVE_RUNTIME_LEDGER"' in launcher_text
assert '"FACTORY_DURABLE_LEDGER=$PRODUCT_ROOT/factory/ledger.csv"' in launcher_text
assert '"FACTORY_REFRESH_RUNTIME_LEDGER=1"' in launcher_text
assert 'CLI_RUNTIME_ROOT="$QUALIFICATION_ROOT"' in launcher_text
assert '"FACTORY_CLI_RUNTIME_ROOT=$CLI_RUNTIME_ROOT"' in launcher_text
assert 'CONTROLLER_STATE_DIR="$ACTIVE_CONTROLLER_STATE"' in launcher_text
assert 'CONTROLLER_STATE_DIR="$KITS_ROOT/projects/$PROJECT/controller"' in launcher_text
assert '--state-dir "$CONTROLLER_STATE_DIR" --project "$PROJECT"' in launcher_text
assert 'exec /usr/bin/env -i "HOME=$HOME" "PATH=$SAFE_PATH" "TMPDIR=$SAFE_TMPDIR"' in launcher_text
assert 'CURSOR_ACCOUNT_DB="$HOME/.factory/accounting/cursor-account-admission-v1.sqlite3"' in launcher_text
assert '"FACTORY_CURSOR_ACCOUNT_DB=$CURSOR_ACCOUNT_DB"' in launcher_text
assert '"FACTORY_ADAPTER_OVERRIDE=mock"' in launcher_text
assert 'if ! transition_receipt consume "$RUN_ROLE"; then' in launcher_text
assert '"$ATTEST_ACTION" != "dependency-refresh-replay" ||' in launcher_text
assert '"$EMERGENCY_HELPER" consume' in launcher_text
runner_text = open(
    os.path.join(root, "scripts", "run-agent.sh"), encoding="utf-8"
).read()
assert 'nysa-sf-qualification.*' in runner_text
assert "including this Narrator attempt's conservative reservation" in runner_text

integration = os.path.join(root, "integrations", "hermes")
required = [
    "contract.json",
    "CHANGELOG.md",
    "bin/factory-launch",
    "fixtures/factory-profile.json",
    "fixtures/projects/relay.env",
    "templates/profile/SOUL.md",
    "templates/profile/skills/factory-dispatch/SKILL.md",
    "templates/profile/skills/factory-supervisor/SKILL.md",
    "templates/launchd/com.nysa.hermes-factory-gateway.plist",
    "templates/launchd/com.nysa.hermes-dashboard.plist",
]
for relative in required:
    assert os.path.isfile(os.path.join(integration, relative)), relative
assert os.access(os.path.join(integration, "bin/factory-launch"), os.X_OK)

changelog = open(os.path.join(integration, "CHANGELOG.md"), encoding="utf-8").read()
assert "## 1.8.0" in changelog and "## 1.7.0" in changelog and "## 1.6.0" in changelog and "## 1.5.0" in changelog and "## 1.4.0" in changelog and "## 1.3.0" in changelog and "## 1.2.0" in changelog and "## 1.1.0" in changelog and "## 1.0.0" in changelog
assert "0.18.2" in changelog and "2026.7.7.2" in changelog

for relative in [
    "templates/profile/SOUL.md",
    "templates/profile/skills/factory-dispatch/SKILL.md",
    "templates/profile/skills/factory-supervisor/SKILL.md",
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
setup = open(os.path.join(root, "docs/factory-setup.md"), encoding="utf-8").read()
operator_full = open(
    os.path.join(root, "docs/runbooks/operator.md"), encoding="utf-8",
).read()
migration = open(
    os.path.join(root, "docs/runbooks/release-migration-prompt.md"),
    encoding="utf-8",
).read()
stuck = operator_full[operator_full.index("## Stuck ticket"):]
stuck = stuck[:stuck.index("\n## ", 1)]
assert "watch --json" in stuck and "progress_timeout" in stuck
assert "factory-launch <project> run" not in stuck
assert "Blocked-Escalated" not in stuck
operator = operator_full[operator_full.index("## Preparing and activating a release"):]
operator = operator[:operator.index("\n## ", 1)]
migration = migration[migration.index("this exact order:"):]

def numbered_steps(text):
    matches = list(re.finditer(r"(?m)^(\d+)\. ", text))
    return {
        int(match.group(1)): re.sub(
            r"\s+",
            " ",
            text[match.start():(next_match.start() if next_match else len(text))],
        )
        for match, next_match in zip(matches, matches[1:] + [None])
    }


operator_steps = numbered_steps(operator)
assert "SSH host aliases" in operator_steps[1]
operator_drain = operator_steps[4]
assert operator_drain.index("factory-kit.sh pause") < operator_drain.index(
    "factory-kit.sh recover-lease"
) < operator_drain.index("prove every remaining provider") < operator_drain.index(
    "install the exact sealed"
)
assert operator_drain.index("publish maintenance on the old active host") < \
    operator_drain.index("prove its controller and provider work are drained") < \
    operator_drain.index("keep it stopped through cutover")
assert operator_steps[7].index("merge the protected product PR") < \
    operator_steps[7].index("certify that exact protected-main SHA and tree")
operator_release = " ".join(operator_steps[index] for index in range(1, 8))
assert operator_release.index("publish maintenance on the old active host") < \
    operator_release.index("prove its controller and provider work are drained") < \
    operator_release.index("merge the protected product PR")
operator_cutover = operator_steps[11]
assert "re-confirm the old host remains in maintenance" in operator_cutover
assert "provider work drained" in operator_cutover
assert "publish maintenance" not in operator_cutover
assert "wait for" not in operator_cutover

migration_steps = numbered_steps(migration)
assert "SSH host aliases" in migration_steps[1]
migration_drain = migration_steps[3]
assert migration_drain.index("factory-kit.sh pause") < migration_drain.index(
    "factory-kit.sh recover-lease"
) < migration_drain.index("prove every remaining provider") < \
    migration_drain.index("install the sealed")
assert migration_drain.index("publish maintenance on the old active host") < \
    migration_drain.index("prove its controller and provider work are drained") < \
    migration_drain.index("keep it stopped through cutover")
assert "manual protected merge" in migration_steps[8]
assert "certify that exact" in migration_steps[9]
migration_release = " ".join(migration_steps[index] for index in range(1, 10))
assert migration_release.index("publish maintenance on the old active host") < \
    migration_release.index("prove its controller and provider work are drained") < \
    migration_release.index("manual protected merge")
migration_cutover = migration_steps[11]
assert "re-confirm the old host remains in maintenance" in migration_cutover
assert "controller and provider work drained" in migration_cutover
assert "publish maintenance" not in migration_cutover
assert "drain it" not in migration_cutover

block_match = re.search(
    r"```bash\n(?P<block>  \(\n    set -eu.*?\n  \))\n  ```",
    setup,
    re.S,
)
assert block_match
launcher_install = textwrap.dedent(block_match.group("block"))

def exercise_launcher_install(failure=None):
    with tempfile.TemporaryDirectory() as root:
        home = pathlib.Path(root)
        installed = home / ".factory/bin/factory-launch"
        candidate = home / (
            ".factory/kits/releases/<full-sha>/integrations/hermes/bin/"
            "factory-launch"
        )
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"installed-old\n")
        installed.chmod(0o700)
        if failure != "candidate":
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"sealed-new\n")
            candidate.chmod(0o700)
        env = dict(os.environ)
        env["HOME"] = str(home)
        if failure in {"install", "cmp"}:
            stub = home / "stub"
            stub.mkdir()
            command = stub / failure
            command.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
            command.chmod(0o700)
            env["PATH"] = f"{stub}{os.pathsep}{env['PATH']}"
        result = subprocess.run(
            ["bash", "-c", launcher_install],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        rollbacks = list(installed.parent.glob("factory-launch.rollback.*"))
        assert not list(installed.parent.glob(".factory-launch.*"))
        if failure:
            assert result.returncode != 0
            assert installed.read_bytes() == b"installed-old\n"
            assert len(rollbacks) == (0 if failure == "candidate" else 1)
            if rollbacks:
                assert rollbacks[0].read_bytes() == b"installed-old\n"
        else:
            assert result.returncode == 0, result.stderr
            assert installed.read_bytes() == b"sealed-new\n"
            assert len(rollbacks) == 1
            assert rollbacks[0].read_bytes() == b"installed-old\n"


for failure in ("candidate", "install", "cmp", None):
    exercise_launcher_install(failure)
skill = open(
    os.path.join(integration, "templates/profile/skills/factory-dispatch/SKILL.md"),
    encoding="utf-8",
).read()
assert "version: 1.8.0" in skill
assert "reconcile --json" in skill
assert "cannot spawn an agentic dispatcher" in skill
assert "four disposable execution cells" in skill

supervisor = open(
    os.path.join(integration, "templates/profile/skills/factory-supervisor/SKILL.md"),
    encoding="utf-8",
).read()
assert "version: 1.8.0" in supervisor
assert "dispatch-plan --claim --json" in supervisor
assert "no agentic supervisor" in supervisor
assert "reconcile --json" in supervisor

soul = open(
    os.path.join(integration, "templates/profile/SOUL.md"), encoding="utf-8"
).read()
assert "no agentic dispatcher or supervisor" in soul
assert "reconcile --json" in soul
assert "every 15 seconds" in soul
assert "exactly one product publication lease" in soul

fixture = json.load(open(os.path.join(integration, "fixtures/factory-profile.json"), encoding="utf-8"))
assert fixture["redacted"] is True
assert fixture["secret_values_included"] is False
assert fixture["profile"]["environment_key_presence"] == {"GH_TOKEN": True}
assert "skills/factory-supervisor/SKILL.md" in fixture["profile"]["files"]
assert fixture["services"] == [
    {"kind": "controller", "launch_agent": "per-product"},
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

linear_template = pathlib.Path(root, "scripts/launchd/com.factory.linear-sync.plist.template")
linear_text = linear_template.read_text(encoding="utf-8")
linear = plistlib.loads(
    linear_text.replace("__HOME__", "/Users/test")
    .replace("__PROJECT_SLUG__", "alpha")
    .replace("__FACTORY_ROOT__", "/product")
    .encode()
)
assert linear["ProgramArguments"] == [
    "/Users/test/.factory/bin/factory-launch", "alpha", "linear-sync"
]
assert "__KIT_DIR__" not in linear_text
PY

python3 "$ROOT/ci/linear-sync-service-test.py"

echo "hermes-contract-test: all cases passed"

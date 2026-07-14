#!/usr/bin/env bash
# Prove the production adapters fail closed without dangerous bypass flags.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sf-adapter-policy.XXXXXX")"
BIN="$TMP/bin"
FAIL=0
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$BIN" "$TMP/repo"
git -C "$TMP/repo" init -q

cat > "$BIN/timeout" <<'STUB'
#!/usr/bin/env bash
shift
exec "$@"
STUB

cat > "$BIN/codex" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version) echo "codex-cli 0.144.1"; exit 0 ;;
  exec)
    if [[ "${2:-}" == "--help" ]]; then
      echo "--json --strict-config --ephemeral --add-dir --ask-for-approval"
      exit 0
    fi
    ;;
esac
printf '<%s>\n' "$@" > "$CAPTURE"
echo '{"input_tokens":10,"output_tokens":5}'
STUB

cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version) echo "2.1.207 (Claude Code)"; exit 0 ;;
  --help)
    echo "--max-budget-usd --output-format --append-system-prompt --permission-mode --allowedTools --disallowedTools --add-dir --no-session-persistence"
    exit 0
    ;;
esac
printf 'SCARY_TOKEN=%s\n' "${SCARY_TOKEN-unset}" > "$CAPTURE"
printf '<%s>\n' "$@" >> "$CAPTURE"
echo '{"total_cost_usd":0.01,"num_turns":1}'
STUB
chmod +x "$BIN/timeout" "$BIN/codex" "$BIN/claude"

assert_has() { grep -qF -- "$2" "$1" || { echo "FAIL: missing $2" >&2; FAIL=1; }; }
assert_lacks() { ! grep -qF -- "$2" "$1" || { echo "FAIL: forbidden $2" >&2; FAIL=1; }; }

CODEX_ARGS="$TMP/codex.args"
PATH="$BIN:$PATH" CAPTURE="$CODEX_ARGS" \
  "$ROOT/scripts/adapters/codex.sh" --role builder --budget 1 --max-turns 3 \
    --timeout-min 1 --prompt-file /dev/null --verify-command "bash ci/test-all.sh" \
    --workdir "$TMP/repo" -- "test" >/dev/null
assert_has "$CODEX_ARGS" "<--strict-config>"
assert_has "$CODEX_ARGS" "<--ephemeral>"
assert_has "$CODEX_ARGS" "<--add-dir>"
assert_has "$CODEX_ARGS" "permissions.factory="
assert_has "$CODEX_ARGS" "shell_environment_policy="
assert_has "$CODEX_ARGS" "<-a>"
assert_has "$CODEX_ARGS" "<never>"
assert_lacks "$CODEX_ARGS" "dangerously-bypass"

CLAUDE_ARGS="$TMP/claude.args"
PATH="$BIN:$PATH" CAPTURE="$CLAUDE_ARGS" SCARY_TOKEN="test-only" \
  "$ROOT/scripts/adapters/claude-code.sh" --role test-author --budget 1 --max-turns 3 \
    --timeout-min 1 --prompt-file /dev/null --verify-command "bash ci/test-all.sh" \
    --workdir "$TMP/repo" -- "test" >/dev/null
assert_has "$CLAUDE_ARGS" "<dontAsk>"
assert_has "$CLAUDE_ARGS" "Bash(git commit *)"
assert_has "$CLAUDE_ARGS" "<--disallowedTools>"
assert_has "$CLAUDE_ARGS" "<--no-session-persistence>"
assert_lacks "$CLAUDE_ARGS" "dangerously-skip"
assert_has "$CLAUDE_ARGS" "SCARY_TOKEN=unset"

REVIEW_ARGS="$TMP/review.args"
PATH="$BIN:$PATH" CAPTURE="$REVIEW_ARGS" \
  "$ROOT/scripts/adapters/claude-code.sh" --role reviewer --budget 1 --max-turns 3 \
    --timeout-min 1 --prompt-file /dev/null --verify-command "bash ci/test-all.sh" \
    --workdir "$TMP/repo" -- "test" >/dev/null
assert_lacks "$REVIEW_ARGS" "Bash(git commit *)"

if [[ "$FAIL" -eq 0 ]]; then
  echo "PASS: adapter permission policies"
else
  exit 1
fi

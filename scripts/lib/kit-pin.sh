#!/usr/bin/env bash
# Shared physical-kit provenance, strict product pinning, and ticket affinity.
# Functions report failures through *_ERROR variables and never print values.

factory_physical_path() {
  (cd "$1" 2>/dev/null && pwd -P)
}

factory_validate_runtime_overrides() {
  local override_names
  FACTORY_RUNTIME_OVERRIDE_ERROR=""
  override_names="$(compgen -v | awk '
    /^(FACTORY_TEST_MODE|FACTORY_TRUSTED_TEST_HARNESS|FACTORY_TEST_|FACTORY_ADAPTER_OVERRIDE$|FACTORY_OVERRIDE_MODEL$|FACTORY_PROBE_|FACTORY_KIMI_|MOCK_|STUB_)/ {
      print
    }
  ')"
  [[ -z "$override_names" ]] && return 0
  if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
        "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == "1" ]]; then
    return 0
  fi
  FACTORY_RUNTIME_OVERRIDE_ERROR="test, adapter, and probe overrides require the trusted internal test harness"
  return 1
}

factory_directory_tree() {
  local root tmp tree rc=0
  root="$(factory_physical_path "$1")" || return 1
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/factory-runtime-tree.XXXXXX")" || return 1
  git init --bare -q "$tmp/repo.git" >/dev/null 2>&1 || rc=1
  if [[ "$rc" -eq 0 ]]; then
    git --git-dir="$tmp/repo.git" config core.bare false >/dev/null 2>&1 || rc=1
  fi
  if [[ "$rc" -eq 0 ]]; then
    GIT_INDEX_FILE="$tmp/index" git --git-dir="$tmp/repo.git" \
      --work-tree="$root" read-tree --empty >/dev/null 2>&1 || rc=1
  fi
  if [[ "$rc" -eq 0 ]]; then
    (cd "$root" &&
      GIT_INDEX_FILE="$tmp/index" git --git-dir="$tmp/repo.git" \
        --work-tree="$root" add -f -A -- . >/dev/null 2>&1) || rc=1
  fi
  if [[ "$rc" -eq 0 ]]; then
    tree="$(GIT_INDEX_FILE="$tmp/index" git --git-dir="$tmp/repo.git" \
      --work-tree="$root" write-tree 2>/dev/null)" || rc=1
  fi
  rm -rf "$tmp"
  [[ "$rc" -eq 0 ]] || return 1
  printf '%s\n' "$tree"
}

factory_git_common_dir() {
  local root="$1" common
  common="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$common" in
    /*) factory_physical_path "$common" ;;
    *) factory_physical_path "$root/$common" ;;
  esac
}

factory_is_in_repo_conformance() {
  local kit_dir="$1" product_root="$2"
  local kit_top product_top kit_common product_common product_relative
  local kit_head product_head

  kit_top="$(git -C "$kit_dir" rev-parse --show-toplevel 2>/dev/null)" || return 1
  product_top="$(git -C "$product_root" rev-parse --show-toplevel 2>/dev/null)" || return 1
  kit_top="$(factory_physical_path "$kit_top")" || return 1
  product_top="$(factory_physical_path "$product_top")" || return 1
  kit_common="$(factory_git_common_dir "$kit_dir")" || return 1
  product_common="$(factory_git_common_dir "$product_root")" || return 1
  [[ "$kit_common" == "$product_common" ]] || return 1

  if [[ "$product_root" == "$product_top/conformance" ]]; then
    product_relative="conformance"
  elif [[ "$product_root" == "$product_top" && "$product_top" == "$kit_top/conformance" ]]; then
    product_relative="conformance"
  else
    return 1
  fi
  [[ "$product_relative" == "conformance" ]] || return 1

  kit_head="$(git -C "$kit_dir" rev-parse HEAD 2>/dev/null)" || return 1
  product_head="$(git -C "$product_root" rev-parse HEAD 2>/dev/null)" || return 1
  [[ "$kit_head" == "$product_head" ]]
}

factory_product_tree() {
  local product_root="$1" product_top relative
  product_top="$(git -C "$product_root" rev-parse --show-toplevel 2>/dev/null)" || return 1
  product_top="$(factory_physical_path "$product_top")" || return 1
  if [[ "$product_root" == "$product_top" ]]; then
    git -C "$product_root" rev-parse 'HEAD^{tree}' 2>/dev/null
    return
  fi
  case "$product_root" in
    "$product_top"/*)
      relative="${product_root#"$product_top/"}"
      git -C "$product_root" rev-parse "HEAD:$relative" 2>/dev/null
      ;;
    *) return 1 ;;
  esac
}

factory_contract_version_from_directory() {
  local kit_dir="$1" file value
  for file in "$kit_dir/factory-contract.json"; do
    [[ -f "$file" ]] || continue
    value="$(sed -n \
      -e 's/^[[:space:]]*"contract_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*$/\1/p' \
      -e 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*$/\1/p' \
      "$file" | awk 'NR==1 {print; exit}')"
    if [[ -n "$value" ]]; then
      printf '%s\n' "$value"
      return 0
    fi
  done
  return 1
}

factory_contract_version() {
  local kit_dir="$1"
  FACTORY_CONTRACT_VERSION="${FACTORY_CONTRACT_VERSION:-}"
  [[ -z "$FACTORY_CONTRACT_VERSION" ]] || return 0
  FACTORY_CONTRACT_VERSION="$(factory_contract_version_from_directory "$kit_dir" 2>/dev/null || true)"
  return 0
}

factory_load_kit_provenance() {
  local kit_dir="$1" product_root="$2" release_count=0 release_tree release_contract
  local mutable_top requested_scope
  FACTORY_KIT_PIN_ERROR=""
  FACTORY_KIT_PROVENANCE_MODE=""
  FACTORY_KIT_PROVENANCE_SCOPE=""
  FACTORY_KIT_PATH="$(factory_physical_path "$kit_dir")" || {
    FACTORY_KIT_PIN_ERROR="physical kit path is unavailable"
    return 1
  }
  FACTORY_PRODUCT_PATH="$(factory_physical_path "$product_root")" || {
    FACTORY_KIT_PIN_ERROR="physical product path is unavailable"
    return 1
  }

  [[ "${FACTORY_RELEASE_SHA+x}" == "x" ]] && release_count=$((release_count + 1))
  [[ "${FACTORY_RELEASE_TREE+x}" == "x" ]] && release_count=$((release_count + 1))
  [[ "${FACTORY_RELEASE_PATH+x}" == "x" ]] && release_count=$((release_count + 1))
  [[ "${FACTORY_RELEASE_CONTRACT_VERSION+x}" == "x" ]] && release_count=$((release_count + 1))
  if [[ "$release_count" -ne 0 && "$release_count" -ne 4 ]]; then
    FACTORY_KIT_PIN_ERROR="trusted release provenance is partial; all four FACTORY_RELEASE variables are required"
    return 1
  fi

  if [[ "$release_count" -eq 4 ]]; then
    if ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"${FACTORY_RELEASE_SHA:-}" ||
       ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"${FACTORY_RELEASE_TREE:-}" ||
       ! LC_ALL=C grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' <<<"${FACTORY_RELEASE_CONTRACT_VERSION:-}"; then
      FACTORY_KIT_PIN_ERROR="trusted release provenance contains a noncanonical SHA, tree, or contract version"
      return 1
    fi
    case "${FACTORY_RELEASE_PATH:-}" in
      *$'\n'*|*$'\r'*|*$'\t'*)
        FACTORY_KIT_PIN_ERROR="trusted release path contains control characters"
        return 1
        ;;
      /*) ;;
      *)
        FACTORY_KIT_PIN_ERROR="trusted release path must be an absolute physical path"
        return 1
        ;;
    esac
    release_tree="$(factory_physical_path "$FACTORY_RELEASE_PATH")" || {
      FACTORY_KIT_PIN_ERROR="trusted release path is unavailable"
      return 1
    }
    if [[ "$release_tree" != "$FACTORY_RELEASE_PATH" ||
          "$FACTORY_KIT_PATH" != "$FACTORY_RELEASE_PATH" ]]; then
      FACTORY_KIT_PIN_ERROR="physical kit path does not match trusted release path"
      return 1
    fi
    if [[ -e "$FACTORY_KIT_PATH/.git" || -L "$FACTORY_KIT_PATH/.git" ]]; then
      FACTORY_KIT_PIN_ERROR="trusted sealed release unexpectedly contains Git metadata"
      return 1
    fi
    release_tree="$(factory_directory_tree "$FACTORY_KIT_PATH")" || {
      FACTORY_KIT_PIN_ERROR="could not recompute trusted release tree"
      return 1
    }
    if [[ "$release_tree" != "$FACTORY_RELEASE_TREE" ]]; then
      FACTORY_KIT_PIN_ERROR="physical release tree does not match trusted release provenance"
      return 1
    fi
    release_contract="$(factory_contract_version_from_directory "$FACTORY_KIT_PATH" 2>/dev/null || true)"
    if [[ -z "$release_contract" ||
          "$release_contract" != "$FACTORY_RELEASE_CONTRACT_VERSION" ]]; then
      FACTORY_KIT_PIN_ERROR="physical release contract does not match trusted release provenance"
      return 1
    fi
    FACTORY_KIT_SHA="$FACTORY_RELEASE_SHA"
    FACTORY_KIT_TREE="$FACTORY_RELEASE_TREE"
    FACTORY_CONTRACT_VERSION="$FACTORY_RELEASE_CONTRACT_VERSION"
    FACTORY_KIT_PROVENANCE_MODE="sealed"
  else
    mutable_top="$(git -C "$FACTORY_KIT_PATH" rev-parse --show-toplevel 2>/dev/null)" || {
      FACTORY_KIT_PIN_ERROR="physical kit is not a mutable git checkout"
      return 1
    }
    mutable_top="$(factory_physical_path "$mutable_top")" || {
      FACTORY_KIT_PIN_ERROR="mutable kit root is unavailable"
      return 1
    }
    if [[ "$mutable_top" != "$FACTORY_KIT_PATH" ]]; then
      FACTORY_KIT_PIN_ERROR="physical kit is not the root of a mutable git checkout"
      return 1
    fi
    FACTORY_KIT_SHA="$(git -C "$FACTORY_KIT_PATH" rev-parse HEAD 2>/dev/null)" || {
      FACTORY_KIT_PIN_ERROR="physical kit is not a mutable git checkout"
      return 1
    }
    if ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"$FACTORY_KIT_SHA"; then
      FACTORY_KIT_PIN_ERROR="physical kit HEAD is not a canonical full SHA"
      return 1
    fi
    FACTORY_KIT_TREE="$(git -C "$FACTORY_KIT_PATH" rev-parse 'HEAD^{tree}' 2>/dev/null)" || {
      FACTORY_KIT_PIN_ERROR="physical kit tree is unavailable"
      return 1
    }
    factory_contract_version "$FACTORY_KIT_PATH"
    FACTORY_KIT_PROVENANCE_MODE="git"
  fi

  requested_scope="${FACTORY_KIT_TRUST_SCOPE:-}"
  case "$requested_scope" in
    production-certified|qualification-candidate)
      if [[ "$FACTORY_KIT_PROVENANCE_MODE" != "sealed" ]]; then
        FACTORY_KIT_PIN_ERROR="$requested_scope requires a sealed release"
        return 1
      fi
      FACTORY_KIT_PROVENANCE_SCOPE="$requested_scope"
      ;;
    "")
      if [[ "$FACTORY_KIT_PROVENANCE_MODE" == "git" ]]; then
        FACTORY_KIT_PROVENANCE_SCOPE="development-local"
      else
        FACTORY_KIT_PROVENANCE_SCOPE="standalone-sealed"
      fi
      ;;
    *)
      FACTORY_KIT_PIN_ERROR="kit trust scope is invalid"
      return 1
      ;;
  esac

  FACTORY_PRODUCT_TREE="$(factory_product_tree "$FACTORY_PRODUCT_PATH" 2>/dev/null)" || {
    FACTORY_KIT_PIN_ERROR="product git tree is unavailable"
    return 1
  }
  if ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"$FACTORY_KIT_TREE" ||
     ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"$FACTORY_PRODUCT_TREE"; then
    FACTORY_KIT_PIN_ERROR="kit or product tree is not a canonical full object ID"
    return 1
  fi
  return 0
}

factory_validate_kit_pin() {
  local kit_dir="$1" product_root="$2" pin_file="$2/factory/KIT_PIN"
  local line_count pinned_sha

  FACTORY_KIT_PIN=""
  FACTORY_KIT_PIN_IMPLICIT=0
  factory_load_kit_provenance "$kit_dir" "$product_root" || return 1

  if [[ "$FACTORY_KIT_PROVENANCE_MODE" == "git" ]] &&
     factory_is_in_repo_conformance "$FACTORY_KIT_PATH" "$FACTORY_PRODUCT_PATH"; then
    FACTORY_KIT_PIN="$FACTORY_KIT_SHA"
    FACTORY_KIT_PIN_IMPLICIT=1
    return 0
  fi

  if [[ ! -f "$pin_file" ]]; then
    FACTORY_KIT_PIN_ERROR="external product requires factory/KIT_PIN"
    return 1
  fi
  line_count="$(awk 'END {print NR+0}' "$pin_file" 2>/dev/null || true)"
  pinned_sha="$(awk 'NR==1 {print; exit}' "$pin_file" 2>/dev/null || true)"
  if [[ "$line_count" != "1" ]] ||
     ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"$pinned_sha"; then
    FACTORY_KIT_PIN_ERROR="factory/KIT_PIN must contain exactly one lowercase full 40-hex SHA"
    return 1
  fi
  FACTORY_KIT_PIN="$pinned_sha"
  if [[ "$FACTORY_KIT_PIN" != "$FACTORY_KIT_SHA" ]]; then
    FACTORY_KIT_PIN_ERROR="factory/KIT_PIN does not match the selected kit SHA"
    return 1
  fi
  return 0
}

factory_validate_ticket_kit_sha() {
  local ticket_file="$1" expected_sha="$2" lease_count lease
  FACTORY_TICKET_KIT_SHA=""
  FACTORY_TICKET_KIT_ERROR=""

  if [[ ! -f "$ticket_file" ]]; then
    FACTORY_TICKET_KIT_ERROR="canonical ticket file is missing"
    return 1
  fi
  lease_count="$(awk '/^[[:space:]]*Kit-SHA:/ {count++} END {print count+0}' "$ticket_file")"
  if [[ "$lease_count" == "0" ]]; then
    return 0
  fi
  if [[ "$lease_count" != "1" ]]; then
    FACTORY_TICKET_KIT_ERROR="ticket must contain at most one Kit-SHA lease"
    return 1
  fi
  lease="$(sed -n 's/^[[:space:]]*Kit-SHA:[[:space:]]*//p' "$ticket_file" | awk 'NR==1 {print; exit}')"
  if ! LC_ALL=C grep -Eq '^[0-9a-f]{40}$' <<<"$lease"; then
    FACTORY_TICKET_KIT_ERROR="ticket Kit-SHA lease must be a lowercase full 40-hex SHA"
    return 1
  fi
  FACTORY_TICKET_KIT_SHA="$lease"
  if [[ "$FACTORY_TICKET_KIT_SHA" != "$expected_sha" ]]; then
    FACTORY_TICKET_KIT_ERROR="ticket Kit-SHA lease does not match the selected kit SHA"
    return 1
  fi
  return 0
}

factory_record_ticket_kit_sha() {
  local ticket_file="$1" expected_sha="$2"
  factory_validate_ticket_kit_sha "$ticket_file" "$expected_sha" || return 1
  [[ -z "$FACTORY_TICKET_KIT_SHA" ]] || return 0

  printf '\nKit-SHA: %s\n' "$expected_sha" >> "$ticket_file" || {
    FACTORY_TICKET_KIT_ERROR="could not record ticket Kit-SHA lease"
    return 1
  }
  FACTORY_TICKET_KIT_SHA="$expected_sha"
  return 0
}

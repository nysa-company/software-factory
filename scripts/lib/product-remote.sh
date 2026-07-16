#!/usr/bin/env bash
# Bind trusted writes to the product origin recorded by release certification.

factory_capture_product_remote() {
  local root="$1" expected="$2" urls count
  FACTORY_PRODUCT_REMOTE_ERROR=""
  if [[ -z "$expected" || "$expected" == *$'\n'* ||
        "$expected" == *$'\r'* || "$expected" == *$'\t'* ]]; then
    FACTORY_PRODUCT_REMOTE_ERROR="certified product origin is missing or unsafe"
    return 1
  fi
  urls="$(git -C "$root" remote get-url --push --all origin 2>/dev/null || true)"
  count="$(printf '%s\n' "$urls" | awk 'NF {count++} END {print count+0}')"
  if [[ "$count" != "1" || "$urls" != "$expected" ]]; then
    FACTORY_PRODUCT_REMOTE_ERROR="product push destination does not match certification"
    return 1
  fi
  printf '%s\n' "$urls"
}

factory_product_remote_matches() {
  local root="$1" expected="$2" current count
  current="$(git -C "$root" remote get-url --push --all origin 2>/dev/null || true)"
  count="$(printf '%s\n' "$current" | awk 'NF {count++} END {print count+0}')"
  [[ "$count" == "1" && "$current" == "$expected" ]] || {
    FACTORY_PRODUCT_REMOTE_ERROR="product push destination changed during execution"
    return 1
  }
}

factory_remote_tracking_tip() {
  git -C "$1" rev-parse --verify --quiet "refs/remotes/origin/$2" 2>/dev/null || true
}

factory_update_tracking_ref() {
  local root="$1" branch="$2" new="$3" old="$4"
  git -C "$root" update-ref "refs/remotes/origin/$branch" "$new" "$old"
}

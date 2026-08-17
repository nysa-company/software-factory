"""Authenticate a legacy committed operator-approval audit as metadata only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import operator_receipt


def _git(workdir: Path, *arguments: str, raw: bool = False) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), *arguments],
        text=True, capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise ValueError("legacy approval audit Git evidence is unavailable")
    return result.stdout if raw else result.stdout.strip()


def trusted_legacy_approval_audit_paths(
    workdir: Path, ticket: str, head: str, changed: set[str],
    refresh_metadata: set[str],
) -> set[str]:
    """Return one exact receipt audit path, or no trusted metadata."""
    prefix = f"factory/receipts/{ticket}/approve-"
    candidates = sorted(path for path in changed if path.startswith(prefix))
    if len(candidates) != 1:
        return set()
    relative = candidates[0]
    match = re.fullmatch(
        rf"factory/receipts/{re.escape(ticket)}/approve-([1-9][0-9]*)\.json",
        relative,
    )
    state_dir = Path(os.environ.get("FACTORY_CONTROLLER_STATE_DIR", ""))
    if (
        match is None
        or not state_dir.is_absolute()
        or not state_dir.is_dir()
        or not (state_dir / "operator-receipts").is_dir()
    ):
        return set()
    try:
        if not re.fullmatch(
            rf"100644 blob [0-9a-f]{{40}}\t{re.escape(relative)}",
            _git(workdir, "ls-tree", head, "--", relative),
        ):
            return set()
        audit_text = _git(workdir, "show", f"{head}:{relative}", raw=True)
        audit = json.loads(audit_text)
        if not isinstance(audit, dict):
            return set()
        bundle_path = f"factory/attestations/{ticket}/bundle.json"
        try:
            bundle_blob = _git(workdir, "rev-parse", f"{head}:{bundle_path}")
        except ValueError:
            refresh_path = f"factory/attestations/{ticket}/refresh.json"
            if refresh_path not in refresh_metadata:
                return set()
            refresh = json.loads(
                _git(workdir, "show", f"{head}:{refresh_path}", raw=True)
            )
            if not isinstance(refresh, dict):
                return set()
            bundle_blob = refresh.get("prior_bundle_blob", "")
            if (
                not re.fullmatch(r"[0-9a-f]{40}", bundle_blob)
                or _git(
                    workdir, "rev-parse",
                    f"{refresh.get('old_head', '')}:{bundle_path}",
                ) != bundle_blob
            ):
                return set()
        receipt = operator_receipt.read_exact(
            state_dir, ticket, "approve", audit.get("receipt_sha256", ""),
            {"bundle_attestation_blob": bundle_blob},
        )
    except (
        json.JSONDecodeError, OSError, TypeError, ValueError,
        operator_receipt.OperatorReceiptError,
    ):
        return set()
    if receipt is None or receipt.get("sequence") != int(match.group(1)):
        return set()
    expected = {
        key: value for key, value in receipt.items()
        if key not in {"nonce", "consumed_at_epoch"}
    }
    expected["consumed"] = False
    expected["audit"] = "no-authority"
    canonical = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    return {relative} if audit_text == canonical else set()

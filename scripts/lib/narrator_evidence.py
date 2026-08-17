"""Fail-closed classification for Narrator-owned PNG evidence."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import struct
import subprocess
import zlib

from release_lineage import successor_release_lineage, valid_v2_migration


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
MAX_NARRATOR_EVIDENCE_FILES = 32
MAX_NARRATOR_EVIDENCE_BYTES = 2_000_000


def authenticated_narrator_parent(
    state_dir: Path, ticket: str, project: str, branch: str,
    factory_sha: str, contract_version: str, head: str,
) -> str:
    if not state_dir.is_absolute():
        return ""
    module_path = Path(__file__).resolve().parents[1] / "ticket-passport.py"
    spec = importlib.util.spec_from_file_location(
        "narrator_evidence_ticket_passport", module_path,
    )
    if spec is None or spec.loader is None:
        return ""
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        state_dir = module.safe_directory(state_dir)
        secret = module.read_regular(state_dir / "passport.key", 0o600, 32)
        if len(secret) != 32:
            return ""
        passport, _ = module.load_passport(
            state_dir / "passports" / f"{ticket}.json", secret,
        )
    except (FileNotFoundError, OSError, ValueError):
        return ""
    evidence = passport.get("completed_role_evidence")
    latest = evidence[-1] if isinstance(evidence, list) and evidence else {}
    source_factory = (
        latest.get("factory_sha", "") if isinstance(latest, dict) else ""
    )
    source_contract = (
        latest.get("contract_version", "") if isinstance(latest, dict) else ""
    )
    history = passport.get("factory_release_history")
    migrations = passport.get("migration_history")
    if (
        passport.get("ticket") != ticket
        or passport.get("project") != project
        or passport.get("contract_version") != contract_version
        or passport.get("factory_sha") != factory_sha
        or passport.get("branch") != branch
        or passport.get("head_sha") != head
        or not isinstance(latest, dict)
        or latest.get("role") != "narrator"
        or not re.fullmatch(r"[0-9a-f]{40}", latest.get("head_before", ""))
        or {"contract_version": source_contract, "factory_sha": source_factory}
        not in (history if isinstance(history, list) else [])
        or {"contract_version": contract_version, "factory_sha": factory_sha}
        not in (history if isinstance(history, list) else [])
        or not successor_release_lineage(
            history, migrations, source_factory, factory_sha, valid_v2_migration,
        )
    ):
        return ""
    return latest["head_before"]


def git_text(workdir: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), *arguments],
        text=True, capture_output=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def bundle_png_paths(workdir: Path, commit: str, ticket: str) -> set[str]:
    bundle = f"factory/tickets/{ticket}-bundle.md"
    result = subprocess.run(
        ["git", "-C", str(workdir), "show", f"{commit}:{bundle}"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        return set()
    target = re.compile(
        rf"!\[[^\]\r\n]*\]\("
        rf"({re.escape(ticket)}-evidence/[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}\.png)"
        rf"(?:[ \t]+[\"'][^\"')\r\n]*[\"'])?\)"
    )
    return {
        f"factory/tickets/{match.group(1)}"
        for match in target.finditer(result.stdout)
    }


def valid_png(blob: bytes) -> bool:
    if not blob.startswith(PNG_SIGNATURE):
        return False
    offset = len(PNG_SIGNATURE)
    chunks = []
    while offset + 12 <= len(blob):
        length = struct.unpack(">I", blob[offset:offset + 4])[0]
        kind = blob[offset + 4:offset + 8]
        end = offset + 12 + length
        if (
            end > len(blob)
            or not re.fullmatch(rb"[A-Za-z]{4}", kind)
            or not 65 <= kind[2] <= 90
        ):
            return False
        payload = blob[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", blob[offset + 8 + length:end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return False
        chunks.append((kind, payload))
        offset = end
        if kind == b"IEND":
            break
    if (
        offset != len(blob)
        or len(chunks) < 3
        or chunks[0][0] != b"IHDR"
        or len(chunks[0][1]) != 13
        or not any(kind == b"IDAT" for kind, _ in chunks[1:-1])
        or chunks[-1] != (b"IEND", b"")
        or sum(kind == b"IHDR" for kind, _ in chunks) != 1
        or sum(kind == b"IEND" for kind, _ in chunks) != 1
    ):
        return False
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", chunks[0][1],
    )
    depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    return (
        width > 0
        and height > 0
        and color in depths
        and depth in depths[color]
        and compression == 0
        and filtering == 0
        and interlace in {0, 1}
    )


def png_blob(workdir: Path, entry: str, path: str) -> bytes | None:
    match = re.fullmatch(
        rf"100644 blob ([0-9a-f]{{40}})\t{re.escape(path)}", entry,
    )
    if not match:
        return None
    oid = match.group(1)
    raw_size = git_text(workdir, "cat-file", "-s", oid)
    if not raw_size.isdigit():
        return None
    size = int(raw_size)
    if size < len(PNG_SIGNATURE) + len(PNG_END) or size > MAX_NARRATOR_EVIDENCE_BYTES:
        return None
    result = subprocess.run(
        ["git", "-C", str(workdir), "cat-file", "blob", oid],
        capture_output=True, check=False,
    )
    if (
        result.returncode
        or len(result.stdout) != size
        or not valid_png(result.stdout)
    ):
        return None
    return result.stdout


def trusted_narrator_evidence_paths(
    workdir: Path, ticket: str, reviewed: str, head: str, changed: set[str],
    retained_parent: str = "",
) -> set[str]:
    prefix = f"factory/tickets/{ticket}-evidence/"
    candidates = {path for path in changed if path.startswith(prefix)}
    if not candidates or len(candidates) > MAX_NARRATOR_EVIDENCE_FILES:
        return set()
    prior_references = bundle_png_paths(workdir, reviewed, ticket)
    current_references = bundle_png_paths(workdir, head, ticket)
    retained_references = set()
    if retained_parent and all(
        subprocess.run(
            ["git", "-C", str(workdir), "merge-base", "--is-ancestor", *edge],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
        for edge in ((reviewed, retained_parent), (retained_parent, head))
    ):
        retained_references = bundle_png_paths(workdir, retained_parent, ticket)
    trusted = set()
    for path in candidates:
        current_entry = git_text(workdir, "ls-tree", head, "--", path)
        if current_entry:
            blob = png_blob(workdir, current_entry, path)
            if path in current_references and blob is not None:
                trusted.add(path)
            elif (
                path in retained_references
                and current_entry
                == git_text(workdir, "ls-tree", retained_parent, "--", path)
                and blob is not None
            ):
                trusted.add(path)
            continue
        prior_entry = git_text(workdir, "ls-tree", reviewed, "--", path)
        blob = png_blob(workdir, prior_entry, path) if prior_entry else None
        if path in prior_references and blob is not None:
            trusted.add(path)
    return trusted

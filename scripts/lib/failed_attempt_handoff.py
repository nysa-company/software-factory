#!/usr/bin/env python3
"""Race-resistant failed-attempt snapshots and temporary-index handoff commits.

This module deliberately has no launcher or model-control integration.  Callers
must supply the expected Git state and a sealed role-boundary policy.
"""

import base64
import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple

from narrator_evidence import valid_png


GITHUB_AUTH_ENVIRONMENT = (
    "FACTORY_GITHUB_TOKEN_FD",
    "GH_CONFIG_DIR",
    "GH_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_TOKEN",
)


class HandoffError(ValueError):
    """The attempted handoff is unsafe or no longer matches its preview."""


@dataclasses.dataclass(frozen=True)
class GitHubHTTPSCredential:
    """Validated GitHub CLI credential-helper capability; never serialized."""

    helper: str

    def __post_init__(self):
        helper = Path(self.helper)
        try:
            metadata = helper.stat()
        except OSError as error:
            raise HandoffError("github credential helper is unavailable") from error
        if (
            not helper.is_absolute()
            or helper.is_symlink()
            or helper.resolve() != helper
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not os.access(helper, os.X_OK)
            or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", self.helper)
        ):
            raise HandoffError("github credential helper is unsafe")
        try:
            parent = helper.parent.stat()
        except OSError as error:
            raise HandoffError("github credential helper is unsafe") from error
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise HandoffError("github credential helper is unsafe")


def github_https_remote(url):
    """Return true only for the certified GitHub HTTPS repository shape."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)
        is not None
    )


DEFAULT_PROTECTED = (
    ".git",
    ".git/**",
    "factory/attestations/**",
    "factory/ledger.csv",
    "factory/runtime-ledger.csv",
    "factory/runs/**",
    "factory/tickets/**",
)
DEFAULT_SECRETS = (
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b",
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
)
ZERO_OID_RE = re.compile(r"^0+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
NARRATOR_PNG_RE = re.compile(
    r"^factory/tickets/T-[0-9]+-evidence/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.png$"
)


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise HandoffError(f"journal contains duplicate key: {key}")
        value[key] = item
    return value


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _matches(path, patterns):
    return any(path == pattern.rstrip("/") or _path_glob(path, pattern) for pattern in patterns)


def _path_glob(path, pattern):
    expression = []
    index = 0
    while index < len(pattern):
        if pattern[index:index + 3] == "**/":
            expression.append("(?:.*/)?")
            index += 3
        elif pattern[index:index + 2] == "**":
            expression.append(".*")
            index += 2
        elif pattern[index] == "*":
            expression.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            expression.append("[^/]")
            index += 1
        else:
            expression.append(re.escape(pattern[index]))
            index += 1
    return re.fullmatch("".join(expression), path) is not None


@dataclasses.dataclass(frozen=True)
class RoleBoundaryPolicy:
    """Sealed path/content policy used by both preview and revalidation."""

    roles: Tuple[Tuple[str, Tuple[str, ...]], ...]
    role_forbidden_paths: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    role_forbidden_exceptions: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    protected_paths: Tuple[str, ...] = DEFAULT_PROTECTED
    journal_path: str = "factory/model-route-journal.json"
    max_file_bytes: int = 1024 * 1024
    secret_patterns: Tuple[str, ...] = DEFAULT_SECRETS
    provider_identities: Tuple[str, ...] = ()
    schema: str = "nysa.software-factory.handoff-boundary/v1"

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get("schema") != (
            "nysa.software-factory.handoff-boundary/v1"
        ):
            raise HandoffError("role-boundary policy schema is invalid")
        roles = value.get("roles")
        if not isinstance(roles, dict) or not roles:
            raise HandoffError("role-boundary policy must define roles")
        normalized_roles = []
        for role, patterns in sorted(roles.items()):
            if not isinstance(role, str) or not role or not isinstance(patterns, list):
                raise HandoffError("role-boundary policy roles are invalid")
            normalized_roles.append((role, _patterns(patterns, "role path")))
        forbidden_value = value.get("role_forbidden_paths", {})
        if not isinstance(forbidden_value, dict):
            raise HandoffError("role forbidden paths must be an object")
        unknown_forbidden = set(forbidden_value) - set(roles)
        if unknown_forbidden:
            raise HandoffError("role forbidden paths reference an unknown role")
        normalized_forbidden = [
            (role, _patterns(patterns, "role forbidden path"))
            for role, patterns in sorted(forbidden_value.items())
        ]
        exception_value = value.get("role_forbidden_exceptions", {})
        if not isinstance(exception_value, dict):
            raise HandoffError("role forbidden exceptions must be an object")
        unknown_exceptions = set(exception_value) - set(roles)
        if unknown_exceptions:
            raise HandoffError(
                "role forbidden exceptions reference an unknown role"
            )
        normalized_exceptions = [
            (role, _patterns(patterns, "role forbidden exception"))
            for role, patterns in sorted(exception_value.items())
        ]
        protected = _patterns(
            value.get("protected_paths", list(DEFAULT_PROTECTED)), "protected path"
        )
        secrets = _patterns(
            value.get("secret_patterns", list(DEFAULT_SECRETS)), "secret"
        )
        identities = value.get("provider_identities", [])
        if (
            not isinstance(identities, list)
            or any(not isinstance(item, str) or not item.strip() for item in identities)
        ):
            raise HandoffError("provider identities are invalid")
        journal = value.get("journal_path", "factory/model-route-journal.json")
        _validate_path_text(journal)
        maximum = value.get("max_file_bytes", 1024 * 1024)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
            raise HandoffError("max_file_bytes must be a positive integer")
        return cls(
            tuple(normalized_roles),
            tuple(normalized_forbidden),
            tuple(normalized_exceptions),
            protected,
            journal,
            maximum,
            secrets,
            tuple(sorted({item.casefold() for item in identities})),
        )

    def paths_for(self, role):
        mapping = dict(self.roles)
        if role not in mapping:
            raise HandoffError(f"role is not present in boundary policy: {role}")
        return mapping[role]

    def forbidden_for(self, role):
        return dict(self.role_forbidden_paths).get(role, ())

    def forbidden_exceptions_for(self, role):
        return dict(self.role_forbidden_exceptions).get(role, ())

    def canonical(self):
        return {
            "journal_path": self.journal_path,
            "max_file_bytes": self.max_file_bytes,
            "protected_paths": list(self.protected_paths),
            "provider_identities": list(self.provider_identities),
            "role_forbidden_paths": {
                role: list(patterns) for role, patterns in self.role_forbidden_paths
            },
            "role_forbidden_exceptions": {
                role: list(patterns)
                for role, patterns in self.role_forbidden_exceptions
            },
            "roles": {role: list(patterns) for role, patterns in self.roles},
            "schema": self.schema,
            "secret_patterns": list(self.secret_patterns),
        }

    @property
    def digest(self):
        return _sha256(_canonical_json(self.canonical()).encode())


def _patterns(value, label):
    if (
        not isinstance(value, (list, tuple))
        or any(
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or item.startswith("/")
            for item in value
        )
    ):
        raise HandoffError(f"{label} patterns are invalid")
    return tuple(value)


@dataclasses.dataclass(frozen=True)
class SnapshotEntry:
    path: str
    state: str
    mode: Optional[str] = None
    blob_oid: Optional[str] = None
    content_sha256: Optional[str] = None
    size: Optional[int] = None

    def canonical_fields(self):
        return (
            self.path.encode("utf-8"),
            self.state.encode("ascii"),
            (self.mode or "").encode("ascii"),
            (self.blob_oid or "").encode("ascii"),
            (self.content_sha256 or "").encode("ascii"),
            (str(self.size) if self.size is not None else "").encode("ascii"),
        )


@dataclasses.dataclass(frozen=True)
class HandoffPreview:
    schema: str
    repo: str
    role: str
    head: str
    branch: str
    remote: str
    remote_destination: Optional[str]
    remote_url: str
    remote_branch: str
    remote_head: str
    provider_scan_base: Optional[str]
    index_digest: str
    policy_digest: str
    snapshot_digest: str
    entries: Tuple[SnapshotEntry, ...]
    preview_digest: str


@dataclasses.dataclass(frozen=True)
class HandoffCommit:
    commit: str
    tree: str
    parent: str
    snapshot_digest: str
    revision_hash: str


def _git_env(extra=None):
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
        }
    )
    if extra:
        env.update(extra)
    for name in GITHUB_AUTH_ENVIRONMENT:
        env.pop(name, None)
    return env


def _git(repo, arguments, *, input_bytes=None, env=None, git_auth=None):
    credential_args = []
    credential_env = _git_env(env)
    if git_auth is not None:
        home = Path(credential_env.get("HOME", ""))
        if not home.is_absolute():
            raise HandoffError("github credential helper is unavailable")
        credential_args = [
            "-c",
            "credential.https://github.com.helper="
            f"!{git_auth.helper} auth git-credential",
        ]
        credential_env["GH_CONFIG_DIR"] = str(home / ".config" / "gh")
        credential_env["GH_PROMPT_DISABLED"] = "1"
    command = [
        "git",
        "-C",
        str(repo),
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        *credential_args,
        "-c",
        "diff.external=",
        "-c",
        "interactive.diffFilter=",
        *arguments,
    ]
    result = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=credential_env,
    )
    if result.returncode:
        if git_auth is not None:
            raise HandoffError("github_https_authentication_failed")
        message = result.stderr.decode("utf-8", "replace").strip()
        raise HandoffError(f"sanitized git command failed: {message}")
    return result.stdout


def _validate_path_text(path):
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise HandoffError("unsafe repository path")
    if "\\" in path or path.endswith("/") or "//" in path:
        raise HandoffError(f"unsafe repository path: {path!r}")
    components = path.split("/")
    if any(
        item in ("", ".", "..")
        or item.casefold() == ".git"
        or any(ord(character) < 32 or ord(character) == 127 for character in item)
        for item in components
    ):
        raise HandoffError(f"unsafe repository path: {path!r}")
    return path


def _decode_path(raw):
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HandoffError("repository path is not valid UTF-8") from error
    return _validate_path_text(path)


def _parse_tree(raw, *, allow_symlinks=False):
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, path_raw = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise HandoffError("Git tree output is malformed")
        mode, kind, oid = (item.decode("ascii") for item in fields)
        path = _decode_path(path_raw)
        if kind == "commit" or mode == "160000":
            raise HandoffError(f"submodules are forbidden: {path}")
        if mode == "120000" and not allow_symlinks:
            raise HandoffError(f"symlinks are forbidden: {path}")
        allowed_modes = (
            ("100644", "100755", "120000")
            if allow_symlinks else ("100644", "100755")
        )
        if kind != "blob" or mode not in allowed_modes:
            raise HandoffError(f"unsupported tracked entry: {path}")
        entries[path] = (mode, oid)
    return entries


def _parse_index(raw):
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, path_raw = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise HandoffError("Git index output is malformed")
        mode, oid, stage = (item.decode("ascii") for item in fields)
        path = _decode_path(path_raw)
        if stage != "0":
            raise HandoffError(f"unmerged index entry is forbidden: {path}")
        if mode == "160000":
            raise HandoffError(f"submodules are forbidden: {path}")
        if mode == "120000":
            raise HandoffError(f"symlinks are forbidden: {path}")
        if mode not in ("100644", "100755") or ZERO_OID_RE.fullmatch(oid):
            raise HandoffError(f"unsupported index entry: {path}")
        entries[path] = (mode, oid)
    return entries


def _filesystem_hazard_check(root, ignored):
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        relative = os.path.relpath(directory, root)
        if relative == ".":
            names[:] = [name for name in names if name != ".git"]
        elif ".git" in names or ".git" in files:
            raise HandoffError(
                f"nested repository is forbidden: {Path(relative).as_posix()}"
            )
        names[:] = [
            name for name in names
            if (Path(relative) / name).as_posix().removeprefix("./") not in ignored
        ]
        for name in names + files:
            path = Path(directory) / name
            item_relative = path.relative_to(root).as_posix()
            if item_relative in ignored:
                continue
            try:
                metadata = path.lstat()
            except OSError as error:
                raise HandoffError(
                    f"unsafe filesystem entry at {item_relative}: {error}"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise HandoffError(f"symlink is forbidden: {item_relative}")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise HandoffError(
                    f"non-regular file is forbidden: {item_relative}"
                )
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise HandoffError(f"hardlinked file is forbidden: {item_relative}")


def _read_regular(root_fd, path, maximum):
    descriptors = []
    try:
        current = os.dup(root_fd)
        descriptors.append(current)
        components = path.split("/")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        for component in components[:-1]:
            current = os.open(component, directory_flags | nofollow, dir_fd=current)
            descriptors.append(current)
        try:
            descriptor = os.open(
                components[-1], os.O_RDONLY | nofollow, dir_fd=current
            )
        except FileNotFoundError:
            return None
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise HandoffError(f"non-regular file is forbidden: {path}")
        if before.st_nlink != 1:
            raise HandoffError(f"hardlinked file is forbidden: {path}")
        if before.st_mode & (stat.S_ISUID | stat.S_ISGID):
            raise HandoffError(f"privileged file mode is forbidden: {path}")
        if before.st_size > maximum:
            raise HandoffError(f"oversized file is forbidden: {path}")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(content) > maximum:
            raise HandoffError(f"oversized file is forbidden: {path}")
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise HandoffError(f"file changed while being snapshotted: {path}")
        executable = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        return content, ("100755" if before.st_mode & executable else "100644")
    except OSError as error:
        raise HandoffError(f"unsafe filesystem entry at {path}: {error}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_content(path, content, policy, mode="100644"):
    if NARRATOR_PNG_RE.fullmatch(path):
        if mode != "100644" or not valid_png(content):
            raise HandoffError(f"Narrator PNG evidence is invalid: {path}")
        return
    if b"\0" in content:
        raise HandoffError(f"binary file is forbidden: {path}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HandoffError(f"binary file is forbidden: {path}") from error
    disallowed = sum(
        1
        for character in text
        if (
            unicodedata.category(character) in ("Cc", "Cf")
            and character not in "\n\r\t"
        )
    )
    if disallowed:
        raise HandoffError(f"binary control data is forbidden: {path}")
    for pattern in policy.secret_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            raise HandoffError(f"secret-like content is forbidden: {path}")


def _ticket_evidence(content):
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise HandoffError("ticket content is not UTF-8") from error
    patterns = (
        re.compile(
            r"^\s*(?:State|Operator-Approval|Priority|Initiative|Kit-SHA|Resume-State):",
            re.I,
        ),
        re.compile(r"^\s*OPERATOR AUTHORIZATION:", re.I),
        re.compile(r"^\s*reviewer round\s+\d+:", re.I),
        re.compile(r"^\s*OPERATOR NOTE:\s*reviewer run", re.I),
        re.compile(r"^\s*SPEC-LINT:", re.I),
    )
    return tuple(
        line for line in lines
        if any(pattern.match(line) for pattern in patterns)
    )


def _ticket_evidence_is_legal(before, after, role):
    if role != "spec-linter":
        return before == after
    spec_lint = re.compile(
        r"^\s*SPEC-LINT:\s*(?:PASS|FAIL(?:\s+—\s+.*)?)\s*$", re.I
    )
    prior = tuple(line for line in before if spec_lint.fullmatch(line))
    current = tuple(line for line in after if spec_lint.fullmatch(line))
    return (
        tuple(line for line in before if not spec_lint.fullmatch(line))
        == tuple(line for line in after if not spec_lint.fullmatch(line))
        and len(current) == len(prior) + 1
        and current[:-1] == prior
    )


def _snapshot_digest(entries):
    digest = hashlib.sha256()
    digest.update(b"nysa-failed-attempt-snapshot-v1\0")
    for entry in entries:
        for field in entry.canonical_fields():
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest()


def validate_handoff_commit(
    repo, *, commit, role, provider_scan_base, policy,
    expected_snapshot_digest, expected_revision_hash, expected_subject,
):
    """Re-authenticate the exact tree produced by build_handoff_commit."""
    if not HEX64_RE.fullmatch(expected_snapshot_digest or "") or not HEX64_RE.fullmatch(
        expected_revision_hash or ""
    ):
        raise HandoffError("handoff commit digests are invalid")
    parents = _git(repo, ["rev-list", "--parents", "-n", "1", commit]).decode().split()
    if len(parents) != 2 or parents[0] != commit:
        raise HandoffError("handoff commit must be one direct commit")
    parent = parents[1]
    message = _git(repo, ["show", "-s", "--format=%B", commit]).decode().splitlines()
    if (
        not message
        or message[0] != expected_subject
        or message.count(f"Failed-Attempt-Snapshot: {expected_snapshot_digest}") != 1
        or message.count(f"Model-Route-Revision: {expected_revision_hash}") != 1
    ):
        raise HandoffError("handoff commit message is invalid")
    _reject_provider_commits(
        repo, provider_scan_base, parent, policy.provider_identities,
    )
    _validate_committed_changes(repo, provider_scan_base, parent, role, policy)
    parent_tree = _parse_tree(_git(repo, ["ls-tree", "-rz", "--full-tree", parent]))
    commit_tree = _parse_tree(_git(repo, ["ls-tree", "-rz", "--full-tree", commit]))
    changed = sorted(
        _decode_path(item)
        for item in _git(
            repo,
            ["-c", "diff.renames=false", "diff", "--name-only", "-z", parent, commit, "--"],
        ).split(b"\0")
        if item
    )
    if policy.journal_path not in changed:
        raise HandoffError("handoff commit omitted its route journal")
    entries = []
    allowed = policy.paths_for(role)
    for path in changed:
        _validate_path_text(path)
        previous = parent_tree.get(path)
        current = commit_tree.get(path)
        if path == policy.journal_path:
            if current is None or current[0] != "100644":
                raise HandoffError("handoff route journal mode is unsafe")
            continue
        if _matches(path, policy.protected_paths):
            raise HandoffError(f"protected path changed: {path}")
        if not _matches(path, allowed):
            raise HandoffError(f"path is outside the {role} boundary: {path}")
        if (
            _matches(path, policy.forbidden_for(role))
            and not _matches(path, policy.forbidden_exceptions_for(role))
        ):
            raise HandoffError(f"path is forbidden for {role}: {path}")
        if current is None:
            entry = SnapshotEntry(path=path, state="deleted")
        else:
            mode, oid = current
            if mode not in {"100644", "100755"}:
                raise HandoffError(f"handoff path has an unsafe mode: {path}")
            content = _git(repo, ["cat-file", "blob", oid])
            if len(content) > policy.max_file_bytes:
                raise HandoffError(f"handoff path is oversized: {path}")
            _validate_content(path, content, policy, mode)
            entry = SnapshotEntry(
                path=path, state="file", mode=mode, blob_oid=oid,
                content_sha256=_sha256(content), size=len(content),
            )
        if re.fullmatch(r"factory/tickets/T-[0-9]+\.md", path):
            if previous is None or current is None:
                raise HandoffError("ticket file creation or deletion is forbidden")
            before = _ticket_evidence(_git(repo, ["cat-file", "blob", previous[1]]))
            after = _ticket_evidence(_git(repo, ["cat-file", "blob", current[1]]))
            if not _ticket_evidence_is_legal(before, after, role):
                raise HandoffError("protected ticket evidence changed")
        entries.append(entry)
    if _snapshot_digest(tuple(entries)) != expected_snapshot_digest:
        raise HandoffError("handoff snapshot digest is invalid")


def _remote_state(repo, remote, branch, destination=None, git_auth=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise HandoffError("remote name is invalid")
    _validate_path_text(branch)
    _git(repo, ["check-ref-format", f"refs/heads/{branch}"])
    url = (
        destination
        if destination is not None
        else _git(repo, ["remote", "get-url", "--", remote]).decode().strip()
    )
    if (
        not isinstance(url, str)
        or not url
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*@", url)
    ):
        raise HandoffError("remote URL is invalid")
    if github_https_remote(url) != (git_auth is not None):
        reason = (
            "github_credential_unavailable"
            if git_auth is None
            else "github credential supplied for a non-GitHub remote"
        )
        raise HandoffError(reason)
    output = _git(
        repo,
        [
            "ls-remote",
            "--heads",
            "--",
            destination or remote,
            f"refs/heads/{branch}",
        ],
        git_auth=git_auth,
    )
    records = [record for record in output.splitlines() if record]
    if len(records) != 1:
        raise HandoffError("remote branch is missing or ambiguous")
    fields = records[0].split(b"\t")
    if len(fields) != 2:
        raise HandoffError("remote branch response is malformed")
    return url, fields[0].decode("ascii")


def _reject_provider_commits(repo, baseline, head, identities):
    if not identities:
        return
    _git(repo, ["merge-base", "--is-ancestor", baseline, head])
    records = _git(
        repo,
        ["log", "-z", "--format=%an%x00%ae%x00%cn%x00%ce", f"{baseline}..{head}"],
    ).split(b"\0")
    forbidden = {identity.casefold() for identity in identities}
    for value in records:
        if value and value.decode("utf-8", "replace").strip().casefold() in forbidden:
            raise HandoffError("provider-authored commit is forbidden")


def _validate_committed_changes(
    repo, baseline, head, role, policy, *, allow_spec_lint_append=False
):
    if baseline == head:
        return ()
    _git(repo, ["merge-base", "--is-ancestor", baseline, head])
    baseline_tree = _parse_tree(
        _git(repo, ["ls-tree", "-rz", "--full-tree", baseline]),
        allow_symlinks=True,
    )
    head_tree = _parse_tree(
        _git(repo, ["ls-tree", "-rz", "--full-tree", head]),
        allow_symlinks=True,
    )
    changed = _git(
        repo,
        [
            "-c",
            "diff.renames=false",
            "diff",
            "--name-only",
            "-z",
            baseline,
            head,
            "--",
        ],
    )
    allowed = policy.paths_for(role)
    entries = []
    for raw_path in changed.split(b"\0"):
        if not raw_path:
            continue
        path = _decode_path(raw_path)
        _validate_path_text(path)
        if _matches(path, policy.protected_paths):
            raise HandoffError(f"protected path changed in committed work: {path}")
        if not _matches(path, allowed):
            raise HandoffError(
                f"committed path is outside the {role} boundary: {path}"
            )
        if (
            _matches(path, policy.forbidden_for(role))
            and not _matches(path, policy.forbidden_exceptions_for(role))
        ):
            raise HandoffError(f"committed path is forbidden for {role}: {path}")
        previous = baseline_tree.get(path)
        current = head_tree.get(path)
        if any(item is not None and item[0] == "120000" for item in (previous, current)):
            raise HandoffError(f"committed path has an unsafe mode: {path}")
        if current is not None:
            mode, oid = current
            if mode not in ("100644", "100755"):
                raise HandoffError(f"committed path has an unsafe mode: {path}")
            content = _git(repo, ["cat-file", "blob", oid])
            if len(content) > policy.max_file_bytes:
                raise HandoffError(f"committed file exceeds size limit: {path}")
            _validate_content(path, content, policy, mode)
        if re.fullmatch(r"factory/tickets/T-[0-9]+\.md", path):
            if previous is None or current is None:
                raise HandoffError("ticket file creation or deletion is forbidden")
            prior_content = _git(repo, ["cat-file", "blob", previous[1]])
            current_content = _git(repo, ["cat-file", "blob", current[1]])
            before = _ticket_evidence(prior_content)
            after = _ticket_evidence(current_content)
            if (
                before != after
                and not (
                    allow_spec_lint_append
                    and _ticket_evidence_is_legal(before, after, role)
                )
            ):
                raise HandoffError("protected ticket evidence changed")
        entries.append(
            SnapshotEntry(path=path, state="deleted")
            if current is None else SnapshotEntry(
                path=path, state="file", mode=mode, blob_oid=oid,
                content_sha256=_sha256(content), size=len(content),
            )
        )
    return tuple(entries)


def validate_committed_output(repo, *, baseline, head, role, policy):
    """Re-run the committed-output checks used by handoff preview."""
    _reject_provider_commits(repo, baseline, head, policy.provider_identities)
    return _snapshot_digest(
        _validate_committed_changes(repo, baseline, head, role, policy)
    )


def _preview_payload(preview):
    return {
        "branch": preview.branch,
        "entries": [
            {
                "blob_oid": entry.blob_oid,
                "content_sha256": entry.content_sha256,
                "mode": entry.mode,
                "path_b64": base64.b64encode(entry.path.encode()).decode(),
                "size": entry.size,
                "state": entry.state,
            }
            for entry in preview.entries
        ],
        "head": preview.head,
        "index_digest": preview.index_digest,
        "policy_digest": preview.policy_digest,
        "remote": preview.remote,
        "remote_branch": preview.remote_branch,
        "remote_head": preview.remote_head,
        "remote_url": preview.remote_url,
        "provider_scan_base": preview.provider_scan_base,
        "repo": preview.repo,
        "role": preview.role,
        "schema": preview.schema,
        "snapshot_digest": preview.snapshot_digest,
    }


def preview_handoff(
    repo,
    *,
    role,
    policy,
    expected_head,
    expected_branch,
    remote,
    remote_branch,
    expected_remote_head,
    remote_destination=None,
    provider_scan_base=None,
    git_auth=None,
):
    """Create a bound preview, refusing unsafe content and any expected-state drift."""
    root = Path(
        _git(repo, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()
    ).resolve()
    supplied = Path(repo).resolve()
    if root != supplied:
        raise HandoffError("repo must be the worktree root")
    head = _git(root, ["rev-parse", "--verify", "HEAD"]).decode().strip()
    branch = _git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"]).decode().strip()
    if head != expected_head:
        raise HandoffError("HEAD drifted from the expected commit")
    if branch != expected_branch:
        raise HandoffError("branch drifted from the expected branch")
    remote_url, remote_head = _remote_state(
        root, remote, remote_branch, remote_destination, git_auth
    )
    if remote_head != expected_remote_head:
        raise HandoffError("remote branch drifted from the expected commit")
    validate_committed_output(
        root, baseline=provider_scan_base or expected_head, head=head,
        role=role, policy=policy,
    )
    tree = _parse_tree(_git(root, ["ls-tree", "-rz", "--full-tree", "HEAD"]))
    index_raw = _git(root, ["ls-files", "-z", "--stage"])
    index = _parse_index(index_raw)
    untracked_raw = _git(
        root, ["ls-files", "-z", "--others", "--exclude-standard", "--"]
    )
    untracked = set()
    for item in untracked_raw.split(b"\0"):
        if not item:
            continue
        if item.endswith(b"/"):
            nested = _decode_path(item[:-1])
            raise HandoffError(f"nested repository is forbidden: {nested}")
        untracked.add(_decode_path(item))
    ignored_raw = _git(
        root,
        [
            "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
            "--directory", "--",
        ],
    )
    ignored = {
        _decode_path(item[:-1] if item.endswith(b"/") else item)
        for item in ignored_raw.split(b"\0") if item
    }
    _filesystem_hazard_check(root, ignored)
    candidates = sorted(set(tree) | set(index) | untracked, key=lambda item: item.encode())
    allowed = policy.paths_for(role)
    entries = []
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for path in candidates:
            if path == policy.journal_path:
                continue
            current = _read_regular(root_fd, path, policy.max_file_bytes)
            previous = tree.get(path)
            if current is None:
                if previous is not None:
                    entry = SnapshotEntry(path=path, state="deleted")
                else:
                    continue
            else:
                content, mode = current
                oid = _git(root, ["hash-object", "--stdin"], input_bytes=content).decode().strip()
                if previous == (mode, oid):
                    continue
                _validate_content(path, content, policy, mode)
                entry = SnapshotEntry(
                    path=path,
                    state="file",
                    mode=mode,
                    blob_oid=oid,
                    content_sha256=_sha256(content),
                    size=len(content),
                )
            if _matches(path, policy.protected_paths):
                raise HandoffError(f"protected path changed: {path}")
            if not _matches(path, allowed):
                raise HandoffError(f"path is outside the {role} boundary: {path}")
            if (
                _matches(path, policy.forbidden_for(role))
                and not _matches(path, policy.forbidden_exceptions_for(role))
            ):
                raise HandoffError(f"path is forbidden for {role}: {path}")
            if re.fullmatch(r"factory/tickets/T-[0-9]+\.md", path):
                if current is None or previous is None:
                    raise HandoffError("ticket file creation or deletion is forbidden")
                prior_content = _git(root, ["show", f"HEAD:{path}"])
                if _ticket_evidence(prior_content) != _ticket_evidence(current[0]):
                    raise HandoffError("protected ticket evidence changed")
            entries.append(entry)
    finally:
        os.close(root_fd)
    entries = tuple(entries)
    snapshot_digest = _snapshot_digest(entries)
    partial = HandoffPreview(
        schema="nysa.software-factory.failed-attempt-handoff-preview/v1",
        repo=str(root),
        role=role,
        head=head,
        branch=branch,
        remote=remote,
        remote_destination=remote_destination,
        remote_url=remote_url,
        remote_branch=remote_branch,
        remote_head=remote_head,
        provider_scan_base=provider_scan_base,
        index_digest=_sha256(index_raw),
        policy_digest=policy.digest,
        snapshot_digest=snapshot_digest,
        entries=entries,
        preview_digest="",
    )
    return dataclasses.replace(
        partial,
        preview_digest=_sha256(_canonical_json(_preview_payload(partial)).encode()),
    )


def revalidate_handoff(preview, policy, git_auth=None):
    """Recompute every Git/index/filesystem input and require byte-for-byte equality."""
    if policy.digest != preview.policy_digest:
        raise HandoffError("role-boundary policy drifted")
    current = preview_handoff(
        preview.repo,
        role=preview.role,
        policy=policy,
        expected_head=preview.head,
        expected_branch=preview.branch,
        remote=preview.remote,
        remote_branch=preview.remote_branch,
        expected_remote_head=preview.remote_head,
        remote_destination=preview.remote_destination,
        provider_scan_base=preview.provider_scan_base,
        git_auth=git_auth,
    )
    if current != preview:
        raise HandoffError("handoff snapshot drifted after preview")
    return current


def build_handoff_commit(
    preview,
    policy,
    *,
    revision_hash,
    commit_timestamp,
    journal_content,
    author_name="Nysa Failed Attempt Handoff",
    author_email="handoff@nysa.invalid",
    subject="Preserve failed attempt for trusted handoff",
    git_auth=None,
):
    """Revalidate and prepare an unreferenced commit through a temporary index."""
    revalidate_handoff(preview, policy, git_auth)
    if not HEX64_RE.fullmatch(revision_hash):
        raise HandoffError("revision hash must be a lowercase SHA-256 digest")
    if (
        not isinstance(commit_timestamp, str)
        or not re.fullmatch(r"[0-9]+ [+-][0-9]{4}", commit_timestamp)
    ):
        raise HandoffError("commit timestamp must use Git internal date format")
    if (
        not isinstance(journal_content, bytes)
        or not journal_content
        or len(journal_content) > policy.max_file_bytes
    ):
        raise HandoffError("journal content is invalid or oversized")
    _validate_content(policy.journal_path, journal_content, policy)
    try:
        journal_value = json.loads(
            journal_content.decode("utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffError("journal content is not valid JSON") from error
    if journal_content != (_canonical_json(journal_value) + "\n").encode():
        raise HandoffError("journal content is not canonical JSON")
    for label, value in (
        ("author name", author_name),
        ("author email", author_email),
        ("subject", subject),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or "\n" in value
            or "\r" in value
            or "\x00" in value
        ):
            raise HandoffError(f"{label} is invalid")
    repo = Path(preview.repo)
    with tempfile.TemporaryDirectory(prefix="nysa-handoff-index-") as temporary:
        index = str(Path(temporary) / "index")
        commit_env = {
            "GIT_INDEX_FILE": index,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": commit_timestamp,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_COMMITTER_DATE": commit_timestamp,
        }
        _git(repo, ["read-tree", preview.head], env=commit_env)
        root_fd = os.open(repo, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            for entry in preview.entries:
                if entry.state == "deleted":
                    if _read_regular(root_fd, entry.path, policy.max_file_bytes) is not None:
                        raise HandoffError(f"deleted handoff file reappeared: {entry.path}")
                    _git(
                        repo,
                        ["update-index", "--force-remove", "--", entry.path],
                        env=commit_env,
                    )
                    continue
                content = _read_regular(
                    root_fd, entry.path, policy.max_file_bytes
                )
                if content is None:
                    raise HandoffError(f"handoff file disappeared: {entry.path}")
                raw, mode = content
                oid = _git(
                    repo, ["hash-object", "-w", "--stdin"], input_bytes=raw, env=commit_env
                ).decode().strip()
                if (
                    mode != entry.mode
                    or oid != entry.blob_oid
                    or _sha256(raw) != entry.content_sha256
                ):
                    raise HandoffError(f"handoff file drifted: {entry.path}")
                _git(
                    repo,
                    ["update-index", "--add", "--cacheinfo", mode, oid, entry.path],
                    env=commit_env,
                )
        finally:
            os.close(root_fd)
        journal_oid = _git(
            repo, ["hash-object", "-w", "--stdin"],
            input_bytes=journal_content, env=commit_env,
        ).decode().strip()
        _git(
            repo,
            ["update-index", "--add", "--cacheinfo", "100644", journal_oid,
             policy.journal_path],
            env=commit_env,
        )
        tree = _git(repo, ["write-tree"], env=commit_env).decode().strip()
        # Seal only after a final check of paths omitted from the fixed tree, too.
        revalidate_handoff(preview, policy, git_auth)
        message = (
            f"{subject}\n\n"
            f"Failed-Attempt-Snapshot: {preview.snapshot_digest}\n"
            f"Model-Route-Revision: {revision_hash}\n"
        ).encode()
        commit = _git(
            repo,
            ["commit-tree", tree, "-p", preview.head],
            input_bytes=message,
            env=commit_env,
        ).decode().strip()
    return HandoffCommit(
        commit=commit,
        tree=tree,
        parent=preview.head,
        snapshot_digest=preview.snapshot_digest,
        revision_hash=revision_hash,
    )

#!/usr/bin/python3
"""Small human interface for exact Factory launcher targets."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata


TARGET = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TICKET = re.compile(r"T-[0-9]{1,12}\Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}
STATES = {
    "Approved", "Awaiting Approval", "Backlog", "Blocked-Escalated",
    "Building", "Canceled", "Done", "Planning", "Ready", "Review",
}
MAX_OUTPUT = 4_000_000
QUALIFICATION_LAUNCHER = re.compile(
    r"(?P<root>/private/tmp/nysa-sf-qualification\.[A-Za-z0-9._-]+)/releases/"
    r"[0-9a-f]{40}/scripts/factory-launch\Z"
)


class CliError(ValueError):
    pass


class LauncherRefused(CliError):
    def __init__(self, message: str, value: dict | None = None):
        super().__init__(message)
        self.value = value


class ExactLauncher:
    def __init__(self, path: Path, descriptor: int, identity: tuple[int, ...]):
        self.path = path
        self.descriptor = descriptor
        self.identity = identity
        self.lock_path = None

    def check(self) -> None:
        try:
            path_info = self.path.lstat()
            open_info = os.fstat(self.descriptor)
        except OSError as error:
            raise CliError("target launcher changed; run factory use") from error
        observed = (
            open_info.st_dev, open_info.st_ino, open_info.st_size,
            open_info.st_mtime_ns, stat.S_IMODE(open_info.st_mode),
        )
        if (
            self.path.is_symlink()
            or (path_info.st_dev, path_info.st_ino) != (open_info.st_dev, open_info.st_ino)
            or observed != self.identity
        ):
            raise CliError("target launcher changed; run factory use")

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def acquire_lock(self) -> int:
        if self.lock_path is None:
            return -1
        descriptor = -1
        try:
            probe = self.lock_path.lstat()
            descriptor = os.open(
                self.lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or (probe.st_dev, probe.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise CliError("Factory launcher lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            current = self.lock_path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise CliError("Factory launcher lock changed")
            return descriptor
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise CliError("Factory launcher lock is unavailable") from error
        except CliError:
            if descriptor >= 0:
                os.close(descriptor)
            raise


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CliError("duplicate JSON key")
        value[key] = item
    return value


def _regular(path: Path, label: str, maximum: int) -> bytes:
    try:
        probe = path.lstat()
    except OSError as error:
        raise CliError(f"{label} is unavailable; run factory use") from error
    if stat.S_ISLNK(probe.st_mode):
        raise CliError(f"{label} is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise CliError(f"{label} is unavailable; run factory use") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum
            or (probe.st_dev, probe.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise CliError(f"{label} is unsafe")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CliError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _directory(path: Path, label: str, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise CliError(f"{label} is unavailable") from error
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.resolve(strict=True) != path
    ):
        raise CliError(f"{label} is unsafe")
    return path


def _atomic(path: Path, raw: bytes) -> None:
    _directory(path.parent, "Factory preference directory", create=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _account_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as error:
        raise CliError("account home is unavailable") from error


def _secure_parent(path: Path, label: str, *, owner: bool = True) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise CliError(f"{label} is unavailable") from error
    if (
        path.is_symlink() or not stat.S_ISDIR(info.st_mode)
        or path.resolve(strict=True) != path
        or owner and info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CliError(f"{label} is unsafe")


def _launcher(path: object) -> ExactLauncher:
    if not isinstance(path, str):
        raise CliError("target launcher is invalid")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise CliError("target launcher is invalid")
    try:
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise CliError("target launcher is unavailable; run factory use") from error
    if (
        candidate.is_symlink()
        or resolved != candidate
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or not stat.S_IMODE(before.st_mode) & 0o100
    ):
        raise CliError("target launcher is unsafe")
    _secure_parent(candidate.parent, "target launcher directory")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise CliError("target launcher is unavailable; run factory use") from error
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
        or opened.st_nlink != 1 or not stat.S_IMODE(opened.st_mode) & 0o100
        or stat.S_IMODE(opened.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise CliError("target launcher is unsafe")
    return ExactLauncher(candidate, descriptor, (
        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
        stat.S_IMODE(opened.st_mode),
    ))


def _target(targets: Path, target_id: str, trusted: bool = False) -> tuple[ExactLauncher, str]:
    if not TARGET.fullmatch(target_id):
        raise CliError("selected target is invalid; run factory use")
    try:
        raw = _regular(targets / f"{target_id}.json", "target", 4096)
        value = json.loads(raw, object_pairs_hook=_unique)
    except CliError as error:
        if "unavailable" in str(error):
            raise CliError("selected target is unavailable; run factory use") from error
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CliError("target is invalid; run factory use") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"launcher", "project"}
        or not isinstance(value.get("project"), str)
        or not PROJECT.fullmatch(value["project"])
    ):
        raise CliError("target is invalid; run factory use")
    launcher = _launcher(value["launcher"])
    try:
        if trusted:
            _trusted_launcher(launcher, value["project"])
    except Exception:
        launcher.close()
        raise
    return launcher, value["project"]


def _selected(targets: Path, selection: Path, trusted: bool = False) -> tuple[ExactLauncher, str]:
    try:
        target_id = _regular(selection, "selection", 256).decode("ascii").strip()
    except UnicodeError as error:
        raise CliError("selection is invalid; run factory use") from error
    return _target(targets, target_id, trusted)


def _trusted_launcher(launcher: ExactLauncher, project: str) -> None:
    factory = _account_home() / ".factory"
    installed = factory / "bin/factory-launch"
    releases = factory / "kits/releases"
    try:
        production = launcher.path.relative_to(releases).parts
    except ValueError:
        production = ()
    qualification = QUALIFICATION_LAUNCHER.fullmatch(str(launcher.path))
    if launcher.path == installed:
        _secure_parent(installed.parent.parent, "Factory state directory")
        _secure_parent(installed.parent, "Factory command directory")
        launcher.lock_path = installed.parent.parent / ".launcher-pin.lock"
        descriptor = launcher.acquire_lock()
        try:
            launcher.check()
        finally:
            os.close(descriptor)
    elif (
        len(production) == 3 and re.fullmatch(r"[0-9a-f]{40}", production[0])
        and production[1:] == ("scripts", "factory-launch")
    ):
        sha = production[0]
        release = releases / sha
        _secure_parent(factory, "Factory state directory")
        _secure_parent(factory / "kits", "Factory kits directory")
        _secure_parent(releases, "Factory release directory")
        _secure_parent(release, "sealed production release")
        active_path = factory / "kits/projects" / project / "active.json"
        manifest_path = factory / "kits/manifests" / f"{sha}.json"
        try:
            active = json.loads(_regular(active_path, "active release", 64_000), object_pairs_hook=_unique)
            manifest = json.loads(_regular(manifest_path, "install manifest", 64_000), object_pairs_hook=_unique)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CliError("production target trust evidence is invalid") from error
        raw = os.pread(launcher.descriptor, launcher.identity[2], 0)
        if (
            not isinstance(active, dict) or active.get("project") != project
            or active.get("kit_sha") != sha
            or not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("kit_sha") != sha
            or manifest.get("sealed_release_path") != str(release)
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_tree", "")))
            or manifest.get("launcher_sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise CliError("production target trust evidence is invalid")
        launcher.lock_path = factory / ".launcher-pin.lock"
        descriptor = launcher.acquire_lock()
        try:
            launcher.check()
        finally:
            os.close(descriptor)
    elif qualification is not None:
        root = launcher.path.parents[3]
        _secure_parent(root, "qualification root")
        _secure_parent(root / "releases", "qualification release directory")
        _secure_parent(launcher.path.parents[1], "sealed qualification release")
    else:
        raise CliError("target launcher is outside a Factory trust root")


def _invoke(launcher: ExactLauncher, project: str, arguments: list[str]) -> tuple[dict, int]:
    lock = launcher.acquire_lock()
    try:
        launcher.check()
        try:
            result = subprocess.run(
                [str(launcher.path), project, *arguments],
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=720,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliError("selected target is unavailable; run factory use") from error
        launcher.check()
    finally:
        if lock >= 0:
            os.close(lock)
    if len(result.stdout.encode()) > MAX_OUTPUT:
        raise CliError("launcher output is too large")
    try:
        value = json.loads(result.stdout, object_pairs_hook=_unique)
    except json.JSONDecodeError as error:
        if result.returncode:
            message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "launcher refused"
            raise LauncherRefused(_safe_title(message[:500])) from error
        raise CliError("launcher returned invalid JSON") from error
    if not isinstance(value, dict):
        raise CliError("launcher returned invalid JSON")
    return value, result.returncode


def _call(launcher: ExactLauncher, project: str, arguments: list[str]) -> dict:
    value, code = _invoke(launcher, project, arguments)
    if code:
        message = value.get("reason") or value.get("error") or value.get("status") or "launcher refused"
        raise LauncherRefused(_safe_title(str(message)[:500]), value)
    return value


def _safe_title(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise CliError("ticket title is invalid")
    value = ANSI.sub("", value)
    value = "".join(" " if unicodedata.category(character).startswith("C") else character for character in value)
    value = " ".join(value.split())
    if not value:
        raise CliError("ticket title is invalid")
    return value


def _workflow(launcher: ExactLauncher, project: str) -> dict:
    value = _call(launcher, project, ["operator-snapshot", "workflow", "--json"])
    tickets = value.get("tickets")
    if (
        value.get("schema") != "factory-operator-workflow/v1"
        or value.get("project") != project
        or value.get("mode") not in {"production", "qualification"}
        or not isinstance(value.get("label"), str)
        or not value["label"].strip()
        or not isinstance(tickets, list)
        or len(tickets) > 10_000
    ):
        raise CliError("workflow snapshot is invalid")
    seen = set()
    normalized = []
    for item in tickets:
        if not isinstance(item, dict) or not TICKET.fullmatch(str(item.get("ticket", ""))):
            raise CliError("ticket identifier is invalid")
        dependencies = item.get("depends_on")
        if (
            item["ticket"] in seen
            or item.get("priority") not in PRIORITY
            or item.get("state") not in STATES
            or not isinstance(dependencies, list)
            or any(not isinstance(entry, str) or not TICKET.fullmatch(entry) for entry in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise CliError("ticket snapshot is invalid")
        seen.add(item["ticket"])
        normalized.append({**item, "title": _safe_title(item.get("title"))})
    return {**value, "label": _safe_title(value["label"]), "tickets": normalized}


def _rank(ticket: dict, states: dict[str, str] | None = None) -> tuple[int, bool, int]:
    blocked = bool(ticket["depends_on"]) if states is None else any(
        states.get(dependency) != "Done" for dependency in ticket["depends_on"]
    )
    return PRIORITY[ticket["priority"]], blocked, int(ticket["ticket"][2:])


def _choose(stdin, stdout, count: int) -> int:
    stdout.write("Select: ")
    answer = stdin.readline().strip()
    if not answer.isdigit() or not 1 <= int(answer) <= count:
        raise CliError("selection is invalid")
    return int(answer) - 1


def _confirm(stdin, stdout) -> None:
    stdout.write("Proceed? ")
    if stdin.readline().strip().lower() != "yes":
        raise CliError("action canceled")


def _use(targets: Path, selection: Path, stdin, stdout, trusted: bool) -> None:
    _directory(targets, "target directory")
    choices = []
    for path in sorted(targets.glob("*.json")):
        if not TARGET.fullmatch(path.stem):
            raise CliError("target registry is unsafe")
        launcher = None
        try:
            launcher, project = _target(targets, path.stem, trusted)
            workflow = _workflow(launcher, project)
        except CliError as error:
            if "unavailable" in str(error) or isinstance(error, LauncherRefused):
                continue
            raise
        finally:
            if launcher is not None:
                launcher.close()
        suffix = "Production" if workflow["mode"] == "production" else f"Qualification · {len(workflow['tickets'])} tickets"
        choices.append((path.stem, f"{workflow['label']} · {suffix}"))
    if not choices:
        raise CliError("no available targets")
    stdout.write("Choose a project:\n\n")
    for number, (_, label) in enumerate(choices, 1):
        stdout.write(f"{number}  {label}\n")
    selected = choices[_choose(stdin, stdout, len(choices))][0]
    _atomic(selection, f"{selected}\n".encode("ascii"))


def _backlog(workflow: dict, stdout) -> None:
    states = {item["ticket"]: item["state"] for item in workflow["tickets"]}
    tickets = sorted(
        (item for item in workflow["tickets"] if item["state"] == "Backlog"),
        key=lambda item: _rank(item, states),
    )
    stdout.write(f"{workflow['label']} · Backlog\n\nRank  Ticket  Title  Priority  State  Depends on\n")
    for number, ticket in enumerate(tickets, 1):
        dependencies = ",".join(ticket["depends_on"]) or "none"
        stdout.write(
            f"{number}  {ticket['ticket']}  {ticket['title']}  "
            f"{ticket['priority']}  {ticket['state']}  {dependencies}\n"
        )


def _doctor(launcher: ExactLauncher, project: str, stdout) -> None:
    value, code = _invoke(launcher, project, ["doctor", "--json"])
    if (
        value.get("schema") != "nysa.software-factory.doctor/v2"
        or "project" in value and value.get("project") != project
    ):
        raise CliError("Doctor report is invalid")
    if code or value.get("overall_status") != "ok":
        status = _safe_title(str(value.get("overall_status", "failed")))
        detail = value.get("error")
        raise CliError(
            f"Doctor {status}" + (f": {_safe_title(str(detail))}" if detail else "")
        )
    checks = value.get("checks", {})
    isolated = checks.get("isolated_provider", {}) if isinstance(checks, dict) else {}
    runtime = checks.get("runtime", {}) if isinstance(checks, dict) else {}
    details = []
    if isinstance(isolated, dict):
        details.append(f"{isolated.get('unknown_workers', 0)} unknown workers")
    if isinstance(runtime, dict) and isinstance(runtime.get("max_concurrent_tickets"), int):
        details.append(f"capacity {runtime['max_concurrent_tickets']}")
    stdout.write("Doctor passed" + (" · " + " · ".join(details) if details else "") + "\n")


def _next(
    launcher: ExactLauncher, project: str, workflow: dict, stdin, stdout,
    trusted: bool,
) -> None:
    tickets = workflow["tickets"]
    states = {item["ticket"]: item["state"] for item in tickets}
    if workflow["mode"] == "qualification":
        cohort = sorted((item for item in tickets if item["state"] != "Done"), key=_rank)
        if not cohort:
            stdout.write("No action needs you.\n")
            return
        ready_to_close = all(item["state"] == "Awaiting Approval" for item in cohort)
        verb = "Close" if ready_to_close else "Continue"
        stdout.write(f"{workflow['label']} · Qualification\n\n1  {verb} {len(cohort)} tickets\n")
        for ticket in cohort:
            stdout.write(f"   {ticket['ticket']} · {ticket['title']}\n")
        _choose(stdin, stdout, 1)
        _confirm(stdin, stdout)
        result, code = _invoke(launcher, project, ["qualification-finish", "--json"])
        status = result.get("status")
        project_matches = result.get("project") == project or (
            trusted and status == "error" and "project" not in result
        )
        if (
            not project_matches
            or trusted and result.get("schema")
            != "nysa.software-factory.qualification-run/v1"
            or status not in {"green", "waiting", "blocked", "error"}
        ):
            raise CliError("qualification result is invalid")
        if code or status != "green":
            reason = _safe_title(str(
                result.get("reason", result.get("error", status))
            ))
            raise CliError(f"Qualification is not ready: {reason}")
        stdout.write("Qualification closed.\n")
        return
    approvals = sorted((item for item in tickets if item["state"] == "Awaiting Approval"), key=_rank)
    if approvals:
        choices = approvals
        action = "approve"
        verb = "Approve"
    else:
        choices = sorted(
            (
                item for item in tickets
                if item["state"] == "Backlog" and not _rank(item, states)[1]
            ),
            key=lambda item: _rank(item, states),
        )
        action = "ready"
        verb = "Mark ready"
    if not choices:
        stdout.write("No action needs you.\n")
        return
    stdout.write(f"{workflow['label']} · Next\n\n")
    for number, ticket in enumerate(choices, 1):
        stdout.write(f"{number}  {verb} {ticket['ticket']} · {ticket['title']}\n")
    ticket = choices[_choose(stdin, stdout, len(choices))]["ticket"]
    _confirm(stdin, stdout)
    result = _call(launcher, project, ["operator", action, "--ticket", ticket, "--json"])
    valid = (
        result.get("schema") == "nysa.software-factory.operator-receipt/v1"
        and result.get("ticket") == ticket and result.get("action") == action
        if trusted else result.get("project") == project
        and result.get("ticket") == ticket and result.get("status") == "pass"
    )
    if not valid:
        raise CliError("operator result is invalid")
    stdout.write(f"{ticket} updated.\n")


def register(target_id: str, launcher: str, project: str, targets_dir: Path) -> None:
    if not TARGET.fullmatch(target_id) or not PROJECT.fullmatch(project):
        raise CliError("target identity is invalid")
    candidate = _launcher(launcher)
    try:
        _trusted_launcher(candidate, project)
        _atomic(
            targets_dir / f"{target_id}.json",
            (json.dumps({"launcher": str(candidate.path), "project": project}, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        qualification = QUALIFICATION_LAUNCHER.fullmatch(str(candidate.path))
        if qualification is not None:
            for path in sorted(targets_dir.glob("qualification-*.json")):
                if path.name == f"{target_id}.json":
                    continue
                try:
                    value = json.loads(
                        _regular(path, "qualification target", 4096),
                        object_pairs_hook=_unique,
                    )
                except json.JSONDecodeError as error:
                    raise CliError("qualification target is invalid") from error
                old = (
                    QUALIFICATION_LAUNCHER.fullmatch(str(value.get("launcher", "")))
                    if isinstance(value, dict) and set(value) == {"launcher", "project"}
                    else None
                )
                if old is not None and (
                    old.group("root") == qualification.group("root")
                    or value.get("project") == project
                ):
                    path.unlink()
            _sync_directory(targets_dir)
    finally:
        candidate.close()


def run(
    arguments: list[str], *, targets_dir: Path | None = None,
    selection_file: Path | None = None, stdin=sys.stdin, stdout=sys.stdout,
    stderr=sys.stderr,
) -> int:
    home = _account_home() if targets_dir is None or selection_file is None else None
    trusted = targets_dir is None
    targets = targets_dir or home / ".factory/targets"
    selection = selection_file or home / ".factory/current-target"
    launcher = None
    try:
        _directory(targets, "target directory")
        _directory(selection.parent, "Factory preference directory")
        command = arguments[0] if arguments else "next"
        if len(arguments) > 1 or command not in {"backlog", "doctor", "next", "use"}:
            raise CliError("usage: factory [use|backlog|doctor|next]")
        if command == "use":
            _use(targets, selection, stdin, stdout, trusted)
            return 0
        launcher, project = _selected(targets, selection, trusted)
        if command == "doctor":
            _doctor(launcher, project, stdout)
            return 0
        workflow = _workflow(launcher, project)
        if command == "backlog":
            _backlog(workflow, stdout)
        else:
            _next(launcher, project, workflow, stdin, stdout, trusted)
        return 0
    except (CliError, OSError, UnicodeError) as error:
        print(f"factory: {error}", file=stderr)
        return 2
    finally:
        if launcher is not None:
            launcher.close()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        if len(sys.argv) != 5 or os.environ.get("FACTORY_INTERNAL_REGISTER") != "1":
            print("factory: internal registration is invalid", file=sys.stderr)
            return 2
        try:
            register(sys.argv[2], sys.argv[3], sys.argv[4], _account_home() / ".factory/targets")
            return 0
        except (CliError, OSError) as error:
            print(f"factory: {error}", file=sys.stderr)
            return 2
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Loopback-only, zero-dependency multi-project operator console.

Security model: one random bootstrap URL creates one random in-memory session;
all later API access requires its HttpOnly SameSite cookie.  Mutations also
require the session CSRF token and an exact same-origin request.  Registry
files are parsed as data, and browser input can select only a validated slug.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "integrations" / "operator-console"
SNAPSHOT_PATH = Path(__file__).with_name("operator-snapshot.py")
SPEC = importlib.util.spec_from_file_location("operator_snapshot", SNAPSHOT_PATH)
SNAPSHOT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SNAPSHOT)

PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ASSIGNMENT_RE = re.compile(
    r"(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.*?)[ \t]*"
)
REGISTRY_KEYS = {"KIT_DIR", "PRODUCT_ROOT"}
MAX_REGISTRY_BYTES = 65_536
MAX_BODY_BYTES = 16_384
SESSION_SECONDS = 8 * 60 * 60
COOKIE_NAME = "factory_operator_session"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SNAPSHOT_ROUTES = {
    f"/api/snapshots/{name}": name for name in SNAPSHOT.SNAPSHOT_COMMANDS
}
ACTION_ROUTES = {
    "/api/actions/model-activate": "model-activate",
    "/api/actions/model-disable": "model-disable",
    "/api/actions/model-enable": "model-enable",
    "/api/actions/model-policy-preview": "model-policy-preview",
    "/api/actions/model-policy-apply": "model-policy-apply",
    "/api/actions/envelope-plan": "envelope-plan",
    "/api/actions/envelope-apply": "envelope-apply",
    "/api/actions/envelope-override-plan": "envelope-override-plan",
    "/api/actions/envelope-override-apply": "envelope-override-apply",
    "/api/actions/attempt-cancel-plan": "attempt-cancel-plan",
    "/api/actions/attempt-cancel": "attempt-cancel",
}


class RegistryError(ValueError):
    pass


def loopback_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("bind address must be a numeric loopback") from error
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("bind address must be loopback-only")
    return value


def _open_regular(path: Path, maximum: int) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise RegistryError("registry file is unavailable") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or before.st_size > maximum
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise RegistryError("registry file is not an owner-controlled regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RegistryError("registry file changed while opening")
        with os.fdopen(descriptor, encoding="utf-8", errors="strict") as handle:
            descriptor = -1
            return handle.read(maximum + 1)
    except (OSError, UnicodeError) as error:
        raise RegistryError("registry file cannot be read safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _registry_path(value: str, home: Path) -> Path:
    if value[:1] in {'"', "'"}:
        if len(value) < 2 or value[-1] != value[0] or value[0] in value[1:-1]:
            raise RegistryError("registry contains malformed quoting")
        value = value[1:-1]
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise RegistryError("registry path contains unsafe characters")
    if any(fragment in value for fragment in ("`", "$(", "\\", "\x7f")):
        raise RegistryError("registry path contains unsafe expansion")
    for prefix in ("$HOME/", "${HOME}/", "~/"):
        if value.startswith(prefix):
            value = str(home / value[len(prefix):])
            break
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RegistryError("registry paths must be absolute and traversal-free")
    return path


def validate_registry_file(path: Path, home: Path) -> None:
    content = _open_regular(path, MAX_REGISTRY_BYTES)
    values: dict[str, Path] = {}
    for raw in content.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT_RE.fullmatch(raw)
        if match is None:
            raise RegistryError(f"malformed registry: {path.name}")
        key, encoded = match.groups()
        if key not in REGISTRY_KEYS or key in values:
            raise RegistryError(f"unsupported registry key: {path.name}")
        values[key] = _registry_path(encoded, home)
    if "PRODUCT_ROOT" not in values:
        raise RegistryError(f"registry lacks PRODUCT_ROOT: {path.name}")


class ProjectRegistry:
    """Discover project slugs without exposing or accepting product paths."""

    def __init__(self, directory: Path, home: Path | None = None):
        if not directory.is_absolute():
            raise RegistryError("registry directory must be absolute")
        try:
            before = directory.lstat()
            resolved = directory.resolve(strict=True)
        except OSError as error:
            raise RegistryError("registry directory is unavailable") from error
        if not stat.S_ISDIR(before.st_mode) or directory.is_symlink() or resolved != directory:
            raise RegistryError("registry directory must be a real absolute directory")
        self.directory = directory
        self.home = (home or Path.home()).resolve()

    def projects(self) -> list[str]:
        projects = []
        try:
            entries = list(os.scandir(self.directory))
        except OSError as error:
            raise RegistryError("registry directory cannot be read") from error
        for entry in sorted(entries, key=lambda item: item.name):
            if not entry.name.endswith(".env"):
                continue
            slug = entry.name[:-4]
            if not PROJECT_RE.fullmatch(slug):
                raise RegistryError("registry contains an invalid project filename")
            validate_registry_file(self.directory / entry.name, self.home)
            projects.append(slug)
        if not projects:
            raise RegistryError("registry contains no valid projects")
        return projects

    def require(self, project: Any) -> str:
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
            raise RegistryError("invalid project selector")
        # Revalidate on each request so removed or replaced registry entries
        # cannot retain authority through a stale in-memory path mapping.
        if project not in self.projects():
            raise RegistryError("unknown project selector")
        return project


class ConsoleState:
    def __init__(self, registry: ProjectRegistry, launcher: Path):
        self.registry = registry
        self.launcher = SNAPSHOT.LauncherClient(launcher)
        self.bootstrap = secrets.token_urlsafe(32)
        self.session: str | None = None
        self.csrf: str | None = None
        self.expires_at = 0.0
        self.lock = threading.Lock()

    def establish(self, token: str) -> str | None:
        with self.lock:
            if self.bootstrap is None or not secrets.compare_digest(token, self.bootstrap):
                return None
            self.bootstrap = None
            self.session = secrets.token_urlsafe(32)
            self.csrf = secrets.token_urlsafe(32)
            self.expires_at = time.monotonic() + SESSION_SECONDS
            return self.session

    def authenticated(self, token: str | None) -> bool:
        with self.lock:
            if time.monotonic() >= self.expires_at:
                self.session = self.csrf = None
                return False
            return (
                token is not None
                and self.session is not None
                and secrets.compare_digest(token, self.session)
            )


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self, address: tuple[str, int], state: ConsoleState, public_host: str
    ):
        self.state = state
        self.public_host = public_host
        self.address_family = (
            socket.AF_INET6 if ipaddress.ip_address(public_host).version == 6
            else socket.AF_INET
        )
        super().__init__(address, ConsoleHandler)
        host = f"[{public_host}]" if ":" in public_host else public_host
        self.authority = f"{host}:{self.server_address[1]}"
        self.origin = f"http://{self.authority}"


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_arguments: Any) -> None:
        # Bootstrap URLs and session activity must not enter default access logs.
        return

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, status: int, value: Any) -> None:
        self._send(
            status,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _request_target(self) -> tuple[str, dict[str, list[str]]] | None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return None
        if not parsed.query:
            return parsed.path, {}
        try:
            query = parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=True,
                max_num_fields=4,
            )
        except ValueError:
            return None
        return parsed.path, query

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == self.server.authority

    def _valid_origin(self, required: bool) -> bool:
        origin = self.headers.get("Origin")
        return (not required and origin is None) or origin == self.server.origin

    def _session_cookie(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw or len(raw) > 4096:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        item = cookie.get(COOKIE_NAME)
        return item.value if item is not None else None

    def _authorize(self) -> bool:
        if not self.server.state.authenticated(self._session_cookie()):
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "session required")
            return False
        return True

    def _project_query(self, query: dict[str, list[str]]) -> str | None:
        if set(query) != {"project"} or len(query["project"]) != 1:
            self._error(
                HTTPStatus.BAD_REQUEST, "invalid_request", "one project selector is required"
            )
            return None
        try:
            return self.server.state.registry.require(query["project"][0])
        except RegistryError:
            self._error(
                HTTPStatus.BAD_REQUEST, "invalid_project", "invalid project selector"
            )
            return None

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._valid_host():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "invalid_host", "invalid Host")
            return
        if not self._valid_origin(required=False):
            self._error(HTTPStatus.FORBIDDEN, "invalid_origin", "invalid Origin")
            return
        target = self._request_target()
        if target is None:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid request target")
            return
        path, query = target
        if path.startswith("/bootstrap/"):
            if query or path.count("/") != 2:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
                return
            token = path.removeprefix("/bootstrap/")
            session = self.server.state.establish(token)
            if session is None:
                self._error(HTTPStatus.GONE, "bootstrap_consumed", "bootstrap is unavailable")
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self._security_headers()
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={session}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={SESSION_SECONDS}",
            )
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._authorize():
            return
        if path in STATIC_ROUTES:
            if query:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "query not allowed")
                return
            filename, content_type = STATIC_ROUTES[path]
            try:
                content = (ASSET_ROOT / filename).read_bytes()
            except OSError:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "asset_unavailable",
                    "console asset unavailable",
                )
                return
            self._send(HTTPStatus.OK, content, content_type)
            return
        if path == "/api/session":
            if query:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "query not allowed")
                return
            self._json(HTTPStatus.OK, {"csrf": self.server.state.csrf})
            return
        if path == "/api/projects":
            if query:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "query not allowed")
                return
            try:
                projects = self.server.state.registry.projects()
            except RegistryError:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "registry_unavailable",
                    "project registry is unavailable",
                )
                return
            self._json(HTTPStatus.OK, {"projects": projects})
            return
        view = SNAPSHOT_ROUTES.get(path)
        if view is not None:
            project = self._project_query(query)
            if project is None:
                return
            try:
                value = self.server.state.launcher.snapshot(project, view)
            except SNAPSHOT.SnapshotError as error:
                self._error(HTTPStatus.BAD_GATEWAY, error.code, str(error))
                return
            self._json(
                HTTPStatus.OK, {"project": project, "view": view, "snapshot": value}
            )
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def _body(self) -> dict[str, Any] | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(
                HTTPStatus.BAD_REQUEST, "invalid_body", "transfer encoding is not allowed"
            )
            return None
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type",
                "application/json is required",
            )
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 2 or length > MAX_BODY_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_body",
                "request body has an invalid size",
            )
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_body", "invalid JSON body")
            return None
        if not isinstance(value, dict):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_body", "JSON object required")
            return None
        return value

    def do_POST(self) -> None:
        if not self._valid_host():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "invalid_host", "invalid Host")
            return
        if not self._valid_origin(required=True):
            self._error(HTTPStatus.FORBIDDEN, "invalid_origin", "same Origin required")
            return
        target = self._request_target()
        if target is None:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid request target")
            return
        path, query = target
        if query or path not in ACTION_ROUTES:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        if not self._authorize():
            return
        csrf = self.headers.get("X-CSRF-Token")
        expected = self.server.state.csrf
        if csrf is None or expected is None or not secrets.compare_digest(csrf, expected):
            self._error(HTTPStatus.FORBIDDEN, "invalid_csrf", "valid CSRF token required")
            return
        body = self._body()
        if body is None:
            return
        try:
            project = self.server.state.registry.require(body.pop("project", None))
            value = self.server.state.launcher.mutate(
                project, ACTION_ROUTES[path], body
            )
        except RegistryError:
            self._error(
                HTTPStatus.BAD_REQUEST, "invalid_project", "invalid project selector"
            )
            return
        except SNAPSHOT.SnapshotError as error:
            status = (
                HTTPStatus.BAD_REQUEST
                if error.code in {"invalid_action", "unknown_action"}
                else HTTPStatus.BAD_GATEWAY
            )
            self._error(status, error.code, str(error))
            return
        self._json(HTTPStatus.OK, {"project": project, "result": value})

    def _unsupported_method(self) -> None:
        if not self._valid_host():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "invalid_host", "invalid Host")
            return
        if not self._valid_origin(required=False):
            self._error(HTTPStatus.FORBIDDEN, "invalid_origin", "invalid Origin")
            return
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed",
            "request method is not allowed",
        )

    do_CONNECT = _unsupported_method
    do_DELETE = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_PATCH = _unsupported_method
    do_PUT = _unsupported_method
    do_TRACE = _unsupported_method


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", type=loopback_address, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--registry-dir",
        type=Path,
        default=Path.home() / ".hermes" / "profiles" / "factory" / "projects",
    )
    parser.add_argument(
        "--launcher",
        type=Path,
        default=Path.home() / ".factory" / "bin" / "factory-launch",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be from 0 through 65535")
    try:
        registry = ProjectRegistry(args.registry_dir)
        state = ConsoleState(registry, args.launcher)
        server = ConsoleServer((args.bind, args.port), state, args.bind)
    except (RegistryError, SNAPSHOT.SnapshotError, OSError) as error:
        parser.error(str(error))
    bootstrap_url = f"{server.origin}/bootstrap/{quote(state.bootstrap, safe='')}"
    print(f"Operator console: {bootstrap_url}", flush=True)
    print("The bootstrap URL works once and is not logged.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

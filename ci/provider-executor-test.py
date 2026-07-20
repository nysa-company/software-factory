#!/usr/bin/env python3
"""Security and compatibility tests for the provider executor transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "scripts/provider-executor.py"
REQUEST_SCHEMA = "nysa.software-factory.provider-execution-request/v1"
IMAGE = "registry.example/worker@sha256:" + "a" * 64

FAKE_RUNTIME = r"""#!/usr/bin/env python3
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile

args = sys.argv[1:]
with Path(os.environ["FAKE_RUNTIME_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args, separators=(",", ":")) + "\n")
if args[0] == "create":
    print("fake-container-id")
elif args[0] == "start":
    print("fake-container-id")
elif args[0] == "exec" and "-i" in args:
    pass
elif args[0] == "exec" and "tar" in args and "-c" in args:
    output = io.BytesIO()
    mode = os.environ.get("FAKE_ARTIFACT_MODE", "normal")
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo("artifacts")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        archive.addfile(directory)
        if mode == "symlink":
            linked = tarfile.TarInfo("artifacts/escape")
            linked.type = tarfile.SYMTYPE
            linked.linkname = "/etc/passwd"
            archive.addfile(linked)
        else:
            raw = b"x" * 1024 if mode == "oversized" else b'{"ok":true}\n'
            item = tarfile.TarInfo(
                "artifacts/large" if mode == "oversized" else "artifacts/answer.json"
            )
            item.size = len(raw)
            item.mode = 0o600
            archive.addfile(item, io.BytesIO(raw))
    sys.stdout.buffer.write(output.getvalue())
elif args[0] == "exec":
    sys.stdout.write("O" * int(os.environ.get("FAKE_STDOUT_BYTES", "2")))
    sys.stderr.write("E" * int(os.environ.get("FAKE_STDERR_BYTES", "2")))
    raise SystemExit(int(os.environ.get("FAKE_RETURN_CODE", "0")))
elif args[0] == "rm":
    pass
else:
    raise SystemExit("unsupported fake runtime invocation")
"""


class ProviderExecutorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.txt").write_text("safe source\n")
        (self.source / ".git").mkdir()
        (self.source / ".git/config").write_text("must not copy\n")
        self.input = self.root / "input.json"
        self.input.write_text('{"prompt":"test"}\n')
        self.attempts = self.root / "attempts"
        self.runtime = self.root / "fake-runtime"
        self.runtime.write_text(FAKE_RUNTIME)
        self.runtime.chmod(0o755)
        self.log = self.root / "runtime.log"
        self.environment = {
            **os.environ,
            "FAKE_RUNTIME_LOG": str(self.log),
        }

    def tearDown(self):
        self.temp.cleanup()

    def request(self, **changes):
        value = {
            "attempt_id": "attempt-1",
            "base_sha": "b" * 40,
            "command": ["provider-worker", "--input", "../input"],
            "image": IMAGE,
            "input": str(self.input),
            "policy_sha256": "c" * 64,
            "role": "builder",
            "route_id": "route-1",
            "schema": REQUEST_SCHEMA,
            "source": str(self.source),
            "ticket": "T-123",
        }
        value.update(changes)
        path = self.root / f"request-{len(list(self.root.glob('request-*')))}.json"
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        return path

    def execute(self, request=None, *, mode="isolated-v1", extra=(), env=None):
        command = [
            sys.executable,
            str(EXECUTOR),
            "--runtime",
            str(self.runtime),
            "--attempt-root",
            str(self.attempts),
            *extra,
            "execute",
            "--mode",
            mode,
            "--request",
            str(request or self.request()),
        ]
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=env or self.environment,
            timeout=10,
        )

    def calls(self):
        return [
            json.loads(line)
            for line in self.log.read_text().splitlines()
        ] if self.log.exists() else []

    def test_isolated_runtime_is_unprivileged_mountless_and_identity_bound(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["mode"], "isolated-v1")
        self.assertEqual(value["return_code"], 0)
        self.assertEqual(value["image_digest"], "a" * 64)
        self.assertEqual(value["base_sha"], "b" * 40)
        self.assertEqual(value["policy_sha256"], "c" * 64)
        self.assertEqual(value["ticket"], "T-123")
        self.assertEqual(value["role"], "builder")
        self.assertEqual(value["route_id"], "route-1")
        self.assertEqual(value["artifact_bytes"], len('{"ok":true}\n'))
        self.assertRegex(value["binding_sha256"], r"^[0-9a-f]{64}$")

        calls = self.calls()
        self.assertEqual(
            [call[0] for call in calls],
            ["create", "start", "exec", "exec", "exec", "rm"],
        )
        create = calls[0]
        self.assertIn("none", create)
        self.assertIn("--read-only", create)
        self.assertIn("65532:65532", create)
        self.assertIn("ALL", create)
        self.assertIn("no-new-privileges", create)
        self.assertIn("--pids-limit", create)
        self.assertIn("--memory", create)
        self.assertIn("--cpus", create)
        forbidden = ("--privileged", "--mount", "-v", "/var/run/docker.sock", ".git")
        self.assertFalse(any(item in create for item in forbidden), create)
        self.assertFalse(any("/home/" in item or "/factory/" in item for item in create))
        self.assertEqual(calls[1], ["start", value["container_name"]])
        self.assertEqual(calls[2][:4], ["exec", "-i", value["container_name"], "tar"])
        self.assertEqual(calls[4][:4], ["exec", value["container_name"], "tar", "-c"])
        self.assertTrue((self.attempts / "attempt-1/artifacts/answer.json").is_file())
        self.assertFalse((self.attempts / "attempt-1/payload/source/.git").exists())

    def test_digest_pin_is_required_before_runtime_launch(self):
        result = self.execute(self.request(image="registry.example/worker:latest"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned by sha256", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_source_and_input_symlinks_are_rejected(self):
        outside = self.root / "outside"
        outside.write_text("outside\n")
        (self.source / "escape").symlink_to(outside)
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe file entry", result.stderr)
        self.assertEqual(self.calls(), [])

        (self.source / "escape").unlink()
        linked_input = self.root / "linked-input"
        linked_input.symlink_to(self.input)
        result = self.execute(self.request(attempt_id="attempt-2", input=str(linked_input)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe or oversized", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_artifact_symlinks_and_oversized_output_are_rejected(self):
        environment = {**self.environment, "FAKE_ARTIFACT_MODE": "symlink"}
        result = self.execute(env=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-regular entry", result.stderr)
        self.assertEqual(self.calls()[-1][0], "rm")
        self.assertFalse((self.attempts / "attempt-1/result.json").exists())

        environment = {**self.environment, "FAKE_ARTIFACT_MODE": "oversized"}
        request = self.request(attempt_id="attempt-2")
        command = [
            sys.executable, str(EXECUTOR),
            "--runtime", str(self.runtime),
            "--attempt-root", str(self.attempts),
            "execute", "--mode", "isolated-v1", "--request", str(request),
            "--artifact-bytes", "32",
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, env=environment, timeout=10
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact output exceeds", result.stderr)
        self.assertEqual(self.calls()[-1][0], "rm")

    def test_replay_requires_same_bound_attempt_identity(self):
        first = self.execute()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_calls = len(self.calls())
        replay = self.execute(self.request())
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), json.loads(first.stdout))
        self.assertEqual(len(self.calls()), first_calls)

        for field, value in (
            ("base_sha", "d" * 40),
            ("policy_sha256", "e" * 64),
            ("route_id", "route-2"),
            ("image", "registry.example/other@sha256:" + "b" * 64),
        ):
            mismatch = self.execute(self.request(**{field: value}))
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("replay identity mismatch", mismatch.stderr)
        self.input.write_text('{"prompt":"changed"}\n')
        mismatch = self.execute(self.request())
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("replay identity mismatch", mismatch.stderr)
        self.input.write_text('{"prompt":"test"}\n')
        (self.source / "main.txt").write_text("changed source\n")
        mismatch = self.execute(self.request())
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("replay identity mismatch", mismatch.stderr)
        (self.source / "main.txt").write_text("safe source\n")
        (self.attempts / "attempt-1/artifacts/answer.json").write_text("tampered\n")
        mismatch = self.execute(self.request())
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("replay result mismatch", mismatch.stderr)
        self.assertEqual(len(self.calls()), first_calls)

    def test_concurrent_identical_execution_creates_only_one_container(self):
        request = self.request()
        barrier = threading.Barrier(3)
        results = []

        def run():
            barrier.wait()
            results.append(self.execute(request))

        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

        self.assertEqual(sorted(result.returncode for result in results), [0, 0])
        self.assertEqual(json.loads(results[0].stdout), json.loads(results[1].stdout))
        self.assertEqual(
            [call[0] for call in self.calls()],
            ["create", "start", "exec", "exec", "exec", "rm"],
        )

    def test_stdout_stderr_are_bounded_and_legacy_behavior_remains_available(self):
        environment = {
            **self.environment,
            "FAKE_STDOUT_BYTES": "1000",
            "FAKE_STDERR_BYTES": "1000",
            "FAKE_RETURN_CODE": "7",
        }
        result = self.execute(extra=("--output-bytes", "32"), env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual((len(value["stdout"]), len(value["stderr"])), (32, 32))
        self.assertTrue(value["stdout_truncated"])
        self.assertTrue(value["stderr_truncated"])
        self.assertEqual(value["return_code"], 7)

        legacy = self.request(
            attempt_id="legacy-1",
            image=None,
            command=[
                sys.executable,
                "-c",
                "import pathlib; print(pathlib.Path('main.txt').read_text().strip())",
            ],
        )
        result = self.execute(legacy, mode="legacy-serialized")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["mode"], "legacy-serialized")
        self.assertEqual(value["stdout"], "safe source\n")

    def test_bound_container_identity_supports_targeted_cancellation(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = json.loads(
            (self.attempts / "attempt-1/identity.json").read_text()
        )
        before = len(self.calls())
        command = [
            sys.executable, str(EXECUTOR),
            "--runtime", str(self.runtime),
            "--attempt-root", str(self.attempts),
            "cancel", "--attempt-id", "attempt-1",
            "--binding-sha256", "0" * 64,
        ]
        cancelled = subprocess.run(
            command, text=True, capture_output=True,
            env=self.environment, timeout=10,
        )
        self.assertNotEqual(cancelled.returncode, 0)
        self.assertIn("cancellation binding mismatch", cancelled.stderr)
        self.assertEqual(len(self.calls()), before)

        command[-1] = identity["binding_sha256"]
        cancelled = subprocess.run(
            command, text=True, capture_output=True,
            env=self.environment, timeout=10,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        value = json.loads(cancelled.stdout)
        self.assertEqual(value["container_name"], identity["container_name"])
        self.assertEqual(self.calls()[-1], ["rm", "--force", identity["container_name"]])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "model-control.sh"


class ModelControlTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir()
        self.product = self.base / "product"
        self.remote = self.base / "product.git"
        self.workdir = self.base / "ticket-T-901"
        (self.product / "factory" / "tickets").mkdir(parents=True)
        self.kit_sha = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        (self.product / "factory" / "KIT_PIN").write_text(self.kit_sha + "\n")
        (self.product / "factory" / "PROJECT.env").write_text(
            "TICKET_BRANCH_PREFIX=ticket/\n"
        )
        (self.product / "factory" / "tickets" / "T-901.md").write_text(
            "# T-901\n\nState: Ready\n"
        )
        subprocess.run(
            ["git", "-C", str(self.product), "init", "-q", "-b", "main"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.name", "test"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.product), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "fixture"],
            check=True,
        )
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "remote", "add", "origin", str(self.remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "push", "-q", "-u", "origin", "main"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.product), "worktree", "add", "-q", "-b",
                "ticket/T-901", str(self.workdir),
            ],
            check=True,
        )
        self.global_env = self.base / "global.env"
        self.global_env.write_text(
            "\n".join(
                (
                    "CODEX_PINNED=0.144.1",
                    "CLAUDE_CODE_PINNED=2.1.207",
                    "FACTORY_CURSOR_FALLBACK_ENABLED=1",
                    "CURSOR_AGENT_VERSION=2026.07.23-e383d2b",
                    "CURSOR_OPENAI_MODEL=gpt-5.6-sol-high",
                    "CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high",
                    "FACTORY_PROBE_CODEX=READY:test",
                    "FACTORY_PROBE_CLAUDE_CODE=READY:test",
                    "FACTORY_PROBE_CURSOR_OPENAI=READY:test",
                    "FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test",
                    "",
                )
            )
        )
        self.environment = {
            **os.environ,
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_MODEL_STATE_ROOT": str(self.state),
            "FACTORY_PROJECT": "model-control-test",
            "FACTORY_ROOT": str(self.product),
            "FACTORY_GLOBAL_ENV": str(self.global_env),
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *args, check=True):
        result = subprocess.run(
            [str(CONTROL), *args],
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and result.returncode:
            self.fail("model-control failed: %s %s" % (result.stdout, result.stderr))
        return result

    def model_policy(self):
        return {
            "checking_family": "anthropic",
            "production_family": "openai",
            "roles": {
                "planner": {
                    "effort": "high",
                    "primary_route_id": "codex-gpt-5.6-sol",
                    "secondary_route_id": "cursor-gpt-5.6-sol-high",
                },
                **{
                    role: {
                        "effort": "medium",
                        "primary_route_id": "codex-gpt-5.6-terra",
                        "secondary_route_id": "cursor-gpt-5.6-sol-high",
                    }
                    for role in ("builder", "narrator")
                },
                **{
                    role: {
                        "effort": "medium",
                        "primary_route_id": "claude-fable",
                        "secondary_route_id": "cursor-claude-fable-5-thinking-medium",
                    }
                    for role in ("spec-linter", "test-author")
                },
                "reviewer": {
                    "effort": "high",
                    "primary_route_id": "claude-sonnet",
                    "secondary_route_id": "cursor-claude-sonnet-5-thinking-high",
                },
            },
            "schema": "factory-model-policy/v1",
            "version": 1,
        }

    def test_plan_resolves_all_six_roles_and_honors_profile(self):
        default = json.loads(self.command("plan").stdout)
        selected = json.loads(
            self.command("plan", "--profile", "claude-priority-v1").stdout
        )
        self.assertEqual(len(default["selections"]), 6)
        self.assertEqual(default["profile_id"], "cursor-opus-v1")
        self.assertEqual(selected["profile_id"], "claude-priority-v1")
        self.assertEqual(selected["selections"]["planner"]["adapter"], "claude-code")

    def test_qualification_requires_ready_native_fallbacks_for_cursor_routes(self):
        ready = json.loads(self.command("qualification-readiness").stdout)
        self.assertEqual(ready["status"], "ready")
        self.assertRegex(ready["readiness_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(ready["checks"])
        self.assertTrue(all(item["state"] == "READY" for item in ready["checks"]))

        self.global_env.write_text(
            self.global_env.read_text().replace(
                "FACTORY_PROBE_CODEX=READY:test",
                "FACTORY_PROBE_CODEX=INVALID:version_mismatch",
            )
        )
        refused = self.command("qualification-readiness", check=False)
        self.assertNotEqual(refused.returncode, 0)
        value = json.loads(refused.stdout)
        self.assertEqual(value["status"], "invalid")
        self.assertIn("version_mismatch", {item["reason"] for item in value["checks"]})
        mismatch = next(item for item in value["checks"] if item["reason"] == "version_mismatch")
        self.assertEqual(mismatch["expected_version"], "0.144.1")
        self.assertEqual(mismatch["installed_version"], "test")

    def test_fallback_refusals_are_typed_without_leaking_detail(self):
        helper = ROOT / "scripts/lib/fallback_refusal.py"
        cases = (
            ("readiness", "native CLI version mismatch token=DO-NOT-LEAK"),
            ("manifest", "qualification fallback authority changed token=DO-NOT-LEAK"),
            ("attempt_count", "failed run still has a process record token=DO-NOT-LEAK"),
            ("handoff", "remote branch is missing or ambiguous token=DO-NOT-LEAK"),
            ("route_policy", "provider family violates route policy token=DO-NOT-LEAK"),
            ("provenance", "Linear approval does not match current evidence token=DO-NOT-LEAK"),
            ("route_policy", "role-boundary policy is invalid token=DO-NOT-LEAK"),
            ("route_policy", "provider identities are invalid token=DO-NOT-LEAK"),
            ("handoff", "ticket content is not UTF-8 token=DO-NOT-LEAK"),
            ("handoff", "existing fallback has a non-migration suffix token=DO-NOT-LEAK"),
            ("handoff", "role forbidden exceptions must be an object token=DO-NOT-LEAK"),
            ("handoff", "role forbidden exceptions reference an unknown role token=DO-NOT-LEAK"),
        )
        for expected, detail in cases:
            with self.subTest(expected=expected):
                source = self.base / f"{expected}.error"
                source.write_text(detail)
                result = subprocess.run(
                    [sys.executable, str(helper), str(source)],
                    text=True, capture_output=True, check=True,
                )
                self.assertEqual(result.stdout.strip(), expected)
                self.assertNotIn("DO-NOT-LEAK", result.stdout + result.stderr)

    def test_cursor_inventory_uses_disposable_credentials_and_refuses_unsafe_source(self):
        source = self.base / "cursor-home"
        cursor = source / ".cursor"
        cursor.mkdir(parents=True)
        auth = cursor / "auth.json"
        config = cursor / "cli-config.json"
        auth.write_text('{"token":"fixture"}\n')
        config.write_text('{"model":"fixture"}\n')
        auth.chmod(0o600)
        config.chmod(0o600)
        tools = self.base / "bin"
        tools.mkdir()
        inventory_tmp = self.base / "inventory-tmp"
        inventory_tmp.mkdir()
        trace = self.base / "inventory.trace"
        mode_trace = self.base / "inventory-mode.trace"
        fixture = self.base / "models.txt"
        version = self.base / "version.txt"
        current_fixture = (
            ROOT / "ci/fixtures/cursor-models-2026.07.23-e383d2b.txt"
        ).read_bytes()
        fixture.write_bytes(current_fixture)
        version.write_text("Cursor Agent 2026.07.23-e383d2b\n")
        agent = tools / "agent"
        agent.write_text(
            "#!/bin/sh\n"
            "credential_state=empty\n"
            "test ! -e \"$HOME/.cursor/auth.json\" || credential_state=copied\n"
            "printf '%s|%s|%s\\n' \"$1\" \"$HOME\" \"$credential_state\" "
            ">> \"$MODEL_INVENTORY_TRACE\"\n"
            "case \"$1\" in\n"
            "  --version) cat \"$MODEL_INVENTORY_VERSION\" ;;\n"
            "  models)\n"
            "    printf '%s\\n' changed > \"$HOME/.cursor/cli-config.json\"\n"
            "    python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "
            "\"$HOME\"/.models.* >> \"$MODEL_INVENTORY_MODE_TRACE\"\n"
            "    cat \"$MODEL_INVENTORY_FIXTURE\"\n"
            "    test \"${MODEL_INVENTORY_UNSAFE_OUTPUT:-}\" != mode || "
            "chmod 0644 \"$HOME\"/.models.*\n"
            "    ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        agent.chmod(0o755)
        timeout = tools / "timeout"
        timeout.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
        timeout.chmod(0o755)
        with self.global_env.open("a") as stream:
            stream.write(f"CURSOR_AGENT_BIN={agent}\n")
        self.environment.update(
            FACTORY_CURSOR_SESSION_HOME=str(source),
            MODEL_INVENTORY_FIXTURE=str(fixture),
            MODEL_INVENTORY_MODE_TRACE=str(mode_trace),
            MODEL_INVENTORY_TRACE=str(trace),
            MODEL_INVENTORY_VERSION=str(version),
            PATH=f"{tools}:{self.environment['PATH']}",
            TMPDIR=str(inventory_tmp),
        )
        production_state = self.base / "production-controller"
        production_state.mkdir()
        production_marker = production_state / "active.json"
        production_marker.write_text("production sentinel\n")
        before = {
            path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mode,
                   path.stat().st_mtime_ns)
            for path in (auth, config, production_marker)
        }

        value = json.loads(self.command("inventory").stdout)

        self.assertEqual(value, {
            "count": 2,
            "models": [
                "claude-opus-5-thinking-medium", "gpt-5.6-sol-high",
            ],
            "schema": "factory-cursor-model-inventory/v1",
            "status": "ok",
        })
        initial_trace = [line.split("|") for line in trace.read_text().splitlines()]
        self.assertEqual([line[0] for line in initial_trace], ["--version", "models"])
        self.assertEqual([line[2] for line in initial_trace], ["empty", "copied"])
        self.assertEqual(mode_trace.read_text().splitlines(), ["0o600"])
        fixture.write_bytes(
            current_fixture.replace(
                b"Available models", b"\x1b[2mAvailable models\x1b[22m"
            ).replace(
                b"gpt-5.6-sol-high", b"\x1b[36mgpt-5.6-sol-high\x1b[39m"
            )
        )
        self.assertEqual(
            json.loads(self.command("inventory").stdout)["models"], value["models"]
        )

        for name, output in {
            "malformed": b"Available models\n\ngpt-5.6-sol-high\n",
            "unknown": current_fixture + b"unknown-field=value\n",
            "unknown-flag": current_fixture.replace(
                b" (default)", b" (future)"
            ),
            "repeated-flag": current_fixture.replace(
                b" (default)", b" (current) (default)"
            ),
            "duplicate": current_fixture.replace(
                b"\n\nTip:",
                b"\ngpt-5.6-sol-high - Duplicate\n\nTip:",
            ),
            "secret": current_fixture.replace(
                b"GPT-5.6 Sol 272K High", b"API to\x1b[31mken\x1b[0m material"
            ),
            "github-token": current_fixture.replace(
                b"gpt-5.6-sol-high", b"gh" + b"p_abcdefghijklmnopqrstuvwxyz012345"
            ),
            "openai-token": current_fixture.replace(
                b"gpt-5.6-sol-high", b"s" + b"k-proj-abcdefghijklmnopqrstuvwxyz012345"
            ),
            "slack-token": current_fixture.replace(
                b"gpt-5.6-sol-high", b"x" + b"oxb-1234567890-abcdefghijklmnopqrstuv"
            ),
            "jwt-token": current_fixture.replace(
                b"GPT-5.6 Sol 272K High",
                b"eyJabcdefghijk.eyJabcdefghijkl.mnopqrstuvwxyz",
            ),
            "private-key": current_fixture.replace(
                b"GPT-5.6 Sol 272K High", b"-----BEGIN " + b"PRIVATE KEY-----"
            ),
            "oversized": b"x" * 1_000_001,
        }.items():
            with self.subTest(refusal=name):
                fixture.write_bytes(output)
                refused = self.command("inventory", check=False)
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(json.loads(refused.stdout), {
                    "error": "cursor model inventory returned unsafe or invalid output",
                    "status": "error",
                })
                self.assertEqual(list(inventory_tmp.iterdir()), [])

        fixture.write_bytes(current_fixture)
        for output in (
            "Malicious Shim 2026.07.23-e383d2b\n",
            "Cursor Agent 2026.07.23-e383d2b\nextra\n",
            "Cursor Agent 2026.07.23-e383d2b",
            "x" * 257,
        ):
            with self.subTest(version_output=output[:40]):
                version.write_text(output)
                calls = len(trace.read_text().splitlines())
                refused = self.command("inventory", check=False)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("version is not approved", refused.stdout)
                self.assertEqual(len(trace.read_text().splitlines()), calls + 1)
                self.assertEqual(
                    trace.read_text().splitlines()[-1].split("|")[2], "empty"
                )
                self.assertEqual(list(inventory_tmp.iterdir()), [])
        version.write_text("Cursor Agent 2026.07.23-e383d2b\n")

        self.environment["MODEL_INVENTORY_UNSAFE_OUTPUT"] = "mode"
        unsafe_output = self.command("inventory", check=False)
        self.assertEqual(unsafe_output.returncode, 2)
        self.assertIn("unsafe or invalid output", unsafe_output.stdout)
        self.assertEqual(list(inventory_tmp.iterdir()), [])
        del self.environment["MODEL_INVENTORY_UNSAFE_OUTPUT"]
        control_text = CONTROL.read_text()
        self.assertIn("before.st_mtime_ns", control_text)
        self.assertIn("after.st_mtime_ns", control_text)

        approved = self.global_env.read_text()
        self.global_env.write_text(approved.replace(
            "CURSOR_AGENT_VERSION=2026.07.23-e383d2b",
            "CURSOR_AGENT_VERSION=unsupported",
        ))
        calls = len(trace.read_text().splitlines())
        refused = self.command("inventory", check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("version is not approved", refused.stdout)
        self.assertEqual(len(trace.read_text().splitlines()), calls + 1)
        self.assertEqual(list(inventory_tmp.iterdir()), [])
        self.global_env.write_text(approved)

        calls = len(trace.read_text().splitlines())
        config.chmod(0o644)
        refused = self.command("inventory", check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("cursor_cli_config_mode_0644", refused.stdout)
        self.assertEqual(len(trace.read_text().splitlines()), calls + 1)
        self.assertEqual(trace.read_text().splitlines()[-1].split("|")[2], "empty")
        self.assertEqual(list(inventory_tmp.iterdir()), [])
        config.chmod(0o600)

        probe_homes = {
            Path(line.split("|")[1]) for line in trace.read_text().splitlines()
        }
        self.assertNotIn(source, probe_homes)
        self.assertTrue(all(not path.exists() for path in probe_homes))
        for path, expected in before.items():
            self.assertEqual(
                (path.read_bytes(), path.stat().st_ino, path.stat().st_mode,
                 path.stat().st_mtime_ns),
                expected,
            )

    def test_policy_candidates_preview_cas_apply_and_ticket_status_api(self):
        candidates = json.loads(self.command("policy-candidates").stdout)
        self.assertTrue(candidates["reviewer_exception"]["supported"])
        policy = json.dumps(self.model_policy(), sort_keys=True, separators=(",", ":"))
        preview = json.loads(
            self.command("policy-preview", "--policy", policy).stdout
        )
        stale = self.command(
            "policy-apply",
            "--policy", policy,
            "--expected-current-hash", "0" * 64,
            "--approve-hash", preview["preview_hash"],
            check=False,
        )
        self.assertEqual(stale.returncode, 2)
        applied = json.loads(
            self.command(
                "policy-apply",
                "--policy", policy,
                "--expected-current-hash", preview["current_policy_hash"],
                "--approve-hash", preview["preview_hash"],
            ).stdout
        )
        self.assertRegex(applied["policy_hash"], r"^[0-9a-f]{64}$")
        plan = json.loads(self.command("plan").stdout)
        self.assertEqual(plan["schema"], "model-resolution-plan/v2")
        self.assertEqual(plan["profile_id"], "project-policy")

        status = json.loads(
            self.command("ticket-status", "--ticket", "T-901").stdout
        )
        self.assertEqual(status["state"], "Ready")
        self.assertEqual(status["route_plan_status"], "absent")

    def test_pin_records_affinity_and_plan_in_one_commit_pushes_and_is_idempotent(self):
        before = subprocess.check_output(
            ["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True
        ).strip()
        value = json.loads(
            self.command(
                "pin", "--ticket", "T-901", "--workdir", str(self.workdir)
            ).stdout
        )
        route_plan = self.workdir / "factory" / "route-plans" / "T-901.json"
        ticket = self.workdir / "factory" / "tickets" / "T-901.md"
        self.assertTrue(value["commit_created"])
        self.assertRegex(value["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(value["pin_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(ticket.read_text().count("Kit-SHA:"), 1)
        self.assertIn("Kit-SHA: %s" % self.kit_sha, ticket.read_text())
        self.assertEqual(json.loads(route_plan.read_text())["ticket"], "T-901")
        changed = subprocess.check_output(
            [
                "git", "-C", str(self.workdir), "diff-tree", "--no-commit-id",
                "--name-only", "-r", value["commit_sha"],
            ],
            text=True,
        ).splitlines()
        self.assertEqual(
            sorted(changed),
            ["factory/route-plans/T-901.json", "factory/tickets/T-901.md"],
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "rev-list", "--count", f"{before}..HEAD"],
                text=True,
            ).strip(),
            "1",
        )
        remote_head = subprocess.check_output(
            [
                "git", "-C", str(self.workdir), "ls-remote", "--heads",
                str(self.remote), "refs/heads/ticket/T-901",
            ],
            text=True,
        ).split()[0]
        self.assertEqual(remote_head, value["commit_sha"])
        status = subprocess.check_output(
            ["git", "-C", str(self.workdir), "status", "--porcelain"], text=True
        )
        self.assertEqual(status, "")

        again = json.loads(
            self.command(
                "pin", "--ticket", "T-901", "--workdir", str(self.workdir)
            ).stdout
        )
        self.assertFalse(again["commit_created"])
        self.assertEqual(again["commit_sha"], value["commit_sha"])
        self.assertEqual(again["pin_hash"], value["pin_hash"])

    def test_pin_rejects_dirty_wrong_branch_and_wrong_remote(self):
        ticket = self.workdir / "factory" / "tickets" / "T-901.md"
        ticket.write_text(ticket.read_text() + "\nlocal change\n")
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir),
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("clean", result.stdout)
        subprocess.run(
            ["git", "-C", str(self.workdir), "restore", str(ticket)], check=True
        )

        subprocess.run(
            ["git", "-C", str(self.workdir), "branch", "-m", "ticket/T-902"],
            check=True,
        )
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir), check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match", result.stdout)
        subprocess.run(
            ["git", "-C", str(self.workdir), "branch", "-m", "ticket/T-901"],
            check=True,
        )

        self.environment["FACTORY_CERTIFIED_PRODUCT_ORIGIN"] = str(
            self.base / "wrong.git"
        )
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir), check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("certified product origin", result.stdout)

    def test_precommit_failure_restores_ticket_and_leaves_no_staged_state(self):
        self.global_env.write_text("UNSAFE MODEL CONFIG\n")
        before = (self.workdir / "factory" / "tickets" / "T-901.md").read_text()
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir), check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe or malformed", result.stdout)
        self.assertEqual(
            (self.workdir / "factory" / "tickets" / "T-901.md").read_text(), before
        )
        self.assertFalse(
            (self.workdir / "factory" / "route-plans" / "T-901.json").exists()
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "status", "--porcelain"], text=True
            ),
            "",
        )

    def test_migration_preview_binds_one_current_readiness_probe_per_command(self):
        self.command("pin", "--ticket", "T-901", "--workdir", str(self.workdir))
        release = self.base / "release"
        shutil.copytree(ROOT / "scripts", release / "scripts")
        backend = release / "scripts" / "lib" / "backend-policy.sh"
        backend.write_text(backend.read_text().replace(
            "factory_resolve_model_profile() {\n",
            '''factory_resolve_model_profile() {
  if [[ ${GH_TOKEN+x} == x ]] || /bin/bash -c ': <&9' 2>/dev/null; then
    FACTORY_RESOLVE_ERROR="github_credential_leaked_to_readiness"
    return 2
  fi
''',
            1,
        ).replace(
            '  if [[ -n "$readiness_output" ]]; then\n',
            '''  if [[ -n "${FACTORY_TEST_MIGRATION_READINESS_COUNTER:-}" ]]; then
    count="$(awk 'NR==1 {print; exit}' "$FACTORY_TEST_MIGRATION_READINESS_COUNTER" 2>/dev/null || true)"
    count=$((${count:-0} + 1))
    printf '%s\\n' "$count" > "$FACTORY_TEST_MIGRATION_READINESS_COUNTER"
    if [[ "$count" -eq 1 ]]; then
      python3 - "$tmp/readiness.json" <<'PY'
import json, sys
path = sys.argv[1]
value = json.load(open(path))
value[sorted(value)[0]]["reason"] = "drift"
with open(path, "w") as handle:
    json.dump(value, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\\n")
PY
    fi
  fi
  if [[ -n "$readiness_output" ]]; then
''',
            1,
        ))
        (release / "integrations" / "hermes").mkdir(parents=True)
        shutil.copy2(
            ROOT / "integrations" / "hermes" / "contract.json",
            release / "integrations" / "hermes" / "contract.json",
        )
        release_tree = subprocess.check_output(
            [
                "bash", "-c", 'source "$1"; factory_directory_tree "$2"', "_",
                str(ROOT / "scripts" / "lib" / "kit-pin.sh"), str(release),
            ],
            text=True,
        ).strip()
        trace = self.base / "migration-probes.trace"
        network_trace = self.base / "migration-network.trace"
        network_url = "https://github.com/nysa-company/model-control-test.git"
        subprocess.run(
            [
                "git", "-C", str(self.workdir), "config",
                "remote.origin.pushurl", network_url,
            ],
            check=True,
        )
        tools = self.base / "migration-tools"
        tools.mkdir()
        git_wrapper = tools / "git"
        git_wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "url = os.environ['TEST_GITHUB_URL']\n"
            "network = url in args and ('push' in args or 'ls-remote' in args)\n"
            "if network:\n"
            "    assert os.environ.get('GH_TOKEN') == 'fixture-token'\n"
            "    assert any('credential.https://github.com.helper=!' in x for x in args)\n"
            "    args = [os.environ['TEST_LOCAL_REMOTE'] if x == url else x for x in args]\n"
            "    with open(os.environ['TEST_GIT_TRACE'], 'a') as handle:\n"
            "        handle.write('authenticated-git-network\\n')\n"
            "else:\n"
            "    assert 'GH_TOKEN' not in os.environ\n"
            "os.execv('/usr/bin/git', ['/usr/bin/git', *args])\n"
        )
        git_wrapper.chmod(0o700)
        environment = {
            **self.environment,
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": network_url,
            "FACTORY_PROBE_TRACE": str(trace),
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_RELEASE_SHA": self.kit_sha,
            "FACTORY_RELEASE_TREE": release_tree,
            "PATH": f"{tools}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "TEST_GITHUB_URL": network_url,
            "TEST_GIT_TRACE": str(network_trace),
            "TEST_LOCAL_REMOTE": str(self.remote),
        }

        def migrate(*args, run_environment=None, check=True):
            run_environment = run_environment or environment
            command = [str(release / "scripts" / "model-control.sh"), *args]
            input_text = None
            if args[0] == "migrate":
                command = [
                    "/bin/bash", "-c", 'exec 9<&0; exec "$@"', "_", *command,
                ]
                input_text = "fixture-token\n"
                run_environment = {
                    **run_environment,
                    "FACTORY_GITHUB_TOKEN_FD": "9",
                }
            result = subprocess.run(
                command,
                env=run_environment,
                text=True,
                input=input_text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode and check:
                self.fail("sealed model-control failed: %s %s" % (
                    result.stdout, result.stderr
                ))
            return json.loads(result.stdout) if not result.returncode else result

        preview = migrate(
            "migrate-plan", "--ticket", "T-901", "--workdir", str(self.workdir)
        )
        preview_probes = trace.read_text().splitlines()
        self.assertNotIn("journal", preview)
        self.assertRegex(preview["readiness_sha256"], r"^[0-9a-f]{64}$")
        route_plan = self.workdir / "factory" / "route-plans" / "T-901.json"
        before_head = subprocess.check_output(
            ["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True
        )
        before_plan = route_plan.read_bytes()
        drift_environment = {
            **environment,
            "FACTORY_TEST_MIGRATION_READINESS_COUNTER": str(
                self.base / "migration-readiness-counter"
            ),
        }
        drift = migrate(
            "migrate", "--ticket", "T-901", "--workdir", str(self.workdir),
            "--approve-hash", preview["preview_hash"],
            "--readiness-hash", preview["readiness_sha256"],
            "--approved-by", "tester",
            run_environment=drift_environment,
            check=False,
        )
        self.assertEqual(drift.returncode, 2)
        self.assertIn("readiness changed after approval", drift.stdout)
        self.assertEqual(route_plan.read_bytes(), before_plan)
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True
            ),
            before_head,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "status", "--porcelain"],
                text=True,
            ),
            "",
        )
        after_drift_probes = trace.read_text().splitlines()
        self.assertEqual(
            len(after_drift_probes) - len(preview_probes), len(preview_probes)
        )
        self.assertEqual(
            (self.base / "migration-readiness-counter").read_text().strip(), "1"
        )
        applied = migrate(
            "migrate", "--ticket", "T-901", "--workdir", str(self.workdir),
            "--approve-hash", preview["preview_hash"],
            "--readiness-hash", preview["readiness_sha256"],
            "--approved-by", "tester",
        )
        self.assertGreaterEqual(len(preview_probes), 6)
        self.assertEqual(
            len(trace.read_text().splitlines()) - len(after_drift_probes),
            len(preview_probes),
        )
        self.assertEqual(applied["preview_hash"], preview["preview_hash"])
        self.assertEqual(
            network_trace.read_text().splitlines(),
            ["authenticated-git-network", "authenticated-git-network"],
        )
        self.assertEqual(
            json.loads(
                route_plan.read_text()
            )["schema"],
            "ticket-model-route-journal/v2",
        )
        bundle = self.workdir / "factory/attestations/T-901/bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(json.dumps({"kit_sha": "b" * 40}) + "\n")
        subprocess.run(
            ["git", "-C", str(self.workdir), "add", str(bundle)], check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.workdir), "-c", "user.name=test",
                "-c", "user.email=test@example.com", "commit", "-qm",
                "stale bundle fixture",
            ],
            check=True,
        )
        stale = subprocess.run(
            [
                str(release / "scripts/model-control.sh"), "migrate-plan",
                "--ticket", "T-901", "--workdir", str(self.workdir),
            ],
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn(
            "bundle attestation must be invalidated before route migration",
            stale.stdout,
        )


if __name__ == "__main__":
    unittest.main()

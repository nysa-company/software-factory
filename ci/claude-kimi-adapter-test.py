#!/usr/bin/env python3
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts" / "adapters" / "claude-kimi.sh"
SECRET_VALIDATOR = ROOT / "scripts" / "lib" / "claude-kimi-secret.py"
POLICY = ROOT / "scripts" / "lib" / "backend-policy.sh"
MODEL = "moonshotai/kimi-k2.6"


class ClaudeKimiAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.workdir = self.base / "work"
        self.trace = self.base / "trace.json"
        self.home.mkdir()
        self.bin.mkdir()
        self.workdir.mkdir()
        self.prompt = self.base / "prompt.txt"
        self.prompt.write_text("system prompt\n")
        self.secret = self.base / "pilot.key"
        self.token = "pilot-token-that-must-never-appear"
        self.secret.write_text(self.token + "\n")
        self.secret.chmod(0o600)
        self.write_timeout_stub()
        self.write_claude_stub()

    def tearDown(self):
        self.temporary.cleanup()

    def write_timeout_stub(self):
        path = self.bin / "timeout"
        path.write_text(
            "#!/bin/bash\n"
            'case "$*" in *timeout-case*) exit 124;; esac\n'
            'shift\nexec "$@"\n'
        )
        path.chmod(0o755)

    def write_claude_stub(self, version="2.1.207", missing_flag=""):
        flags = [
            "--max-turns",
            "--max-budget-usd",
            "--output-format",
            "--model",
            "--append-system-prompt-file",
            "--dangerously-skip-permissions",
        ]
        if missing_flag:
            flags.remove(missing_flag)
        script = f"""#!/bin/bash
if [[ "${{1:-}}" == "--version" ]]; then echo "{version} (Claude Code)"; exit 0; fi
if [[ "${{1:-}}" == "--help" ]]; then printf '%s\\n' {' '.join(flags)}; exit 0; fi
python3 - {json.dumps(str(self.trace))} "$@" <<'PY'
import json, os, sys
path = sys.argv[1]
args = sys.argv[2:]
selected = {{key: os.environ.get(key) for key in (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CONFIG_DIR", "UNRELATED_SECRET",
)}}
with open(path, "w", encoding="utf-8") as handle:
    json.dump({{"args": args, "env": selected}}, handle)
task = args[args.index("-p") + 1]
token = os.environ["ANTHROPIC_AUTH_TOKEN"]
if task == "missing-identity":
    result = {{"num_turns": 2, "usage": {{"input_tokens": 3}}}}
elif task == "identity-mismatch":
    result = {{"num_turns": 2, "modelUsage": {{"other/model": {{}}}}}}
elif task == "multiple-identity":
    result = {{"num_turns": 2, "modelUsage": {{
        "moonshotai/kimi-k2.6": {{}}, "other/model": {{}}
    }}}}
elif task == "too-many-turns":
    result = {{"num_turns": 6, "modelUsage": {{"moonshotai/kimi-k2.6": {{}}}}}}
else:
    result = {{
        "num_turns": 2,
        "modelUsage": {{"moonshotai/kimi-k2.6": {{
            "inputTokens": 10, "outputTokens": 4, "cacheReadTokens": 2
        }}}},
        "result": "response containing " + token,
        "total_cost_usd": 999,
    }}
print(json.dumps(result))
print("stderr containing " + token, file=sys.stderr)
PY
"""
        path = self.bin / "claude"
        path.write_text(script)
        path.chmod(0o755)

    def environment(self, trusted=True):
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "FACTORY_KIMI_SECRET_FILE": str(self.secret),
            "UNRELATED_SECRET": "must-not-be-in-minimal-env",
        }
        if trusted:
            environment.update(
                FACTORY_TEST_MODE="1", FACTORY_TRUSTED_TEST_HARNESS="1"
            )
        else:
            environment.pop("FACTORY_TEST_MODE", None)
            environment.pop("FACTORY_TRUSTED_TEST_HARNESS", None)
        return environment

    def run_adapter(self, task="valid", model=MODEL, max_turns="5"):
        return subprocess.run(
            [
                str(ADAPTER),
                "--budget", "1.25",
                "--max-turns", max_turns,
                "--timeout-min", "1",
                "--prompt-file", str(self.prompt),
                "--workdir", str(self.workdir),
                "--model", model,
                "--effort", "medium",
                "--", task,
            ],
            env=self.environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_minimal_environment_exact_route_and_conservative_metrics(self):
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = json.loads(self.trace.read_text())
        expected = {
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        }
        self.assertTrue(all(trace["env"][key] == MODEL for key in expected))
        self.assertEqual(trace["env"]["ANTHROPIC_BASE_URL"], "https://openrouter.ai/api")
        self.assertEqual(trace["env"]["ANTHROPIC_API_KEY"], "")
        self.assertIsNone(trace["env"]["UNRELATED_SECRET"])
        self.assertEqual(trace["env"]["ANTHROPIC_AUTH_TOKEN"], self.token)
        self.assertIn("--max-turns", trace["args"])
        self.assertEqual(trace["args"][trace["args"].index("--model") + 1], MODEL)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        self.assertIn("[REDACTED]", result.stdout + result.stderr)
        self.assertNotIn("cost_usd", result.stdout)
        self.assertIn("turns=2 input_tokens=10 output_tokens=4", result.stdout)
        self.assertIn("cost_basis=conservative_reservation", result.stdout)

    def test_exact_model_version_and_help_are_hard_requirements(self):
        self.assertNotEqual(self.run_adapter(model="kimi").returncode, 0)
        self.write_claude_stub(version="2.1.208")
        self.assertNotEqual(self.run_adapter().returncode, 0)
        self.write_claude_stub(missing_flag="--max-turns")
        self.assertNotEqual(self.run_adapter().returncode, 0)

    def test_identity_failures_and_max_turns_keep_reservation(self):
        for task in (
            "missing-identity",
            "identity-mismatch",
            "multiple-identity",
            "too-many-turns",
        ):
            with self.subTest(task=task):
                result = self.run_adapter(task)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(self.token, result.stdout + result.stderr)
                self.assertNotIn("cost_usd", result.stdout)
                self.assertIn("turns=0", result.stdout)
                self.assertIn("cost_basis=conservative_reservation", result.stdout)

    def test_timeout_is_enforced_without_emitting_secret(self):
        result = self.run_adapter("timeout-case")
        self.assertEqual(result.returncode, 124)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        self.assertIn("cost_basis=conservative_reservation", result.stdout)

    def test_secret_file_validation_and_override_guard(self):
        untrusted = subprocess.run(
            [
                str(ADAPTER), "--budget", "1", "--max-turns", "1",
                "--timeout-min", "1", "--prompt-file", str(self.prompt),
                "--workdir", str(self.workdir), "--model", MODEL,
                "--effort", "medium", "--", "valid",
            ],
            env=self.environment(trusted=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(untrusted.returncode, 0)
        self.assertNotIn(str(self.secret), untrusted.stdout + untrusted.stderr)

        self.secret.chmod(0o644)
        invalid = subprocess.run(
            [str(SECRET_VALIDATOR), "--check", str(self.secret)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn(str(self.secret), invalid.stdout + invalid.stderr)
        self.secret.chmod(0o600)

        link = self.base / "secret-link"
        link.symlink_to(self.secret)
        linked = subprocess.run(
            [str(SECRET_VALIDATOR), "--check", str(link)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(linked.returncode, 0)

        hardlink = self.base / "secret-hardlink"
        os.link(self.secret, hardlink)
        linked = subprocess.run(
            [str(SECRET_VALIDATOR), "--check", str(self.secret)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(linked.returncode, 0)

    def probe(self, pilot=False):
        environment = self.environment()
        if pilot:
            environment["FACTORY_KIMI_PILOT_TEST"] = "1"
        command = (
            f'source "{POLICY}"; '
            'factory_probe_adapter claude-kimi "moonshotai/kimi-k2.6"; '
            'printf "%s:%s\\n" "$PROBE_STATE" "$PROBE_REASON"'
        )
        return subprocess.run(
            ["bash", "-c", command],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_probe_is_task_free_and_disabled_except_trusted_pilot(self):
        disabled = self.probe()
        self.assertEqual(
            disabled.stdout.strip(), "UNAVAILABLE:experimental_route_disabled"
        )
        self.assertFalse(self.trace.exists())
        pilot = self.probe(pilot=True)
        self.assertEqual(pilot.stdout.strip(), "READY:trusted_pilot_contract_ready")
        self.assertFalse(self.trace.exists())

    def test_profiles_do_not_reference_kimi(self):
        profiles = json.loads(
            (ROOT / "scripts" / "model-routing" / "profiles-v1.json").read_text()
        )
        self.assertNotIn("claude-kimi", json.dumps(profiles))
        self.assertNotIn(MODEL, json.dumps(profiles))


if __name__ == "__main__":
    unittest.main()

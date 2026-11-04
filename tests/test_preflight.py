"""Host-only checks for secret-safe inspection and setup before an .env exists."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import preflight

SYNTHETIC_SECRET = "synthetic-credential-for-redaction-tests-only"

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_configuration_reports_missing(self):
        self.assertEqual(preflight.provider_status(self.root, {}), {"status": "missing", "location": None})

    def test_dotenv_key_presence_handles_quotes_and_comments_without_disclosure(self):
        (self.root / ".env").write_text(f'OPENAI_API_KEY="{SYNTHETIC_SECRET}" # private\n')
        result = preflight.provider_status(self.root, {})
        self.assertEqual(result["status"], "present_unverified")
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(result))

    def test_empty_environment_value_overrides_dotenv(self):
        (self.root / ".env").write_text(f"OPENAI_API_KEY={SYNTHETIC_SECRET}\n")
        result = preflight.provider_status(self.root, {"OPENAI_API_KEY": ""})
        self.assertEqual(result, {"status": "missing", "location": "environment:OPENAI_API_KEY"})

    def test_unresolved_dotenv_syntax_is_not_reported_as_a_valid_key(self):
        for value in ['"unterminated', '${PROVIDER_SECRET}', 'replace_me']:
            with self.subTest(value=value):
                (self.root / ".env").write_text(f"OPENAI_API_KEY={value}\n")
                self.assertEqual(preflight.provider_status(self.root, {})["status"], "requires_resolution")

    def test_report_hides_provider_and_memseek_secrets_and_does_not_run_missing_tools(self):
        state = self.root / ".state"
        state.mkdir()
        (state / "api_key").write_text("synthetic-workspace-secret")
        with patch.object(preflight.shutil, "which", return_value=None), patch.object(preflight, "command_status") as command:
            report = preflight.inspect_environment(self.root, {"OPENAI_API_KEY": SYNTHETIC_SECRET})
        command.assert_not_called()
        text = json.dumps(report)
        self.assertNotIn(SYNTHETIC_SECRET, text)
        self.assertNotIn("synthetic-workspace-secret", text)
        self.assertEqual(report["docker_engine"], "not_checked")
        self.assertEqual(report["credentials"]["memseek"], "key_file_present_unverified")
        self.assertFalse((self.root / ".env").exists())

    def test_docker_timeout_is_bounded_and_output_is_suppressed(self):
        with patch.object(preflight.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 8)) as run:
            self.assertEqual(preflight.command_status(["docker", "info"]), "timed_out")
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_launcher_inspect_works_before_init_and_creates_no_configuration(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ["launch.py", "preflight.py"]:
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        env = {**os.environ, "PATH": "", "OPENAI_API_KEY": SYNTHETIC_SECRET, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(scripts / "launch.py"), "inspect"],
                                env=env, capture_output=True, text=True, check=True, timeout=5)
        report = json.loads(result.stdout)
        self.assertEqual(report["credentials"]["provider_OPENAI_API_KEY"]["status"], "present_unverified")
        self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / ".state").exists())

if __name__ == "__main__":
    unittest.main()

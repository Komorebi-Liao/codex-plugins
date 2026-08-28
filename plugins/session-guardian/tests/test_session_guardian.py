import importlib.util
import re
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_guardian.py"
SPEC = importlib.util.spec_from_file_location("session_guardian", SCRIPT)
guardian = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guardian)


def make_transcript(path, byte_count):
    with path.open("wb") as handle:
        if byte_count:
            handle.seek(byte_count - 1)
            handle.write(b"\0")


def payload(event, transcript, session="session-test", cwd=None):
    return {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": cwd or str(transcript.parent),
        "hook_event_name": event,
        "model": "test-model",
        "prompt": "Continue the original work",
    }


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.env = {"PLUGIN_DATA": str(self.data)}
        self.transcript = self.root / "session.jsonl"
        config = dict(guardian.DEFAULT_CONFIG, notifications=False)
        guardian.write_json_atomic(self.data / "config.json", config)

    def tearDown(self):
        self.temporary.cleanup()

    def test_warning_uses_size_without_reading_contents(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["warning_bytes"])
        result = guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        self.assertIn(guardian.format_mib(guardian.DEFAULT_CONFIG["warning_bytes"]), result["systemMessage"])
        self.assertEqual("UserPromptSubmit", result["hookSpecificOutput"]["hookEventName"])
        state = guardian.load_state(self.data, "session-test")
        self.assertEqual(1, state["prompt_count"])
        self.assertTrue(state["warned"])

    def test_rollover_notice_requires_size_and_prompt_count(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["rollover_bytes"])
        for _ in range(guardian.DEFAULT_CONFIG["min_prompts"] - 1):
            guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        self.assertIsNone(guardian.handle_hook(payload("Stop", self.transcript), env=self.env))

        guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        result = guardian.handle_hook(payload("Stop", self.transcript), env=self.env)
        self.assertTrue(result["continue"])
        self.assertIn("继续交接", result["systemMessage"])
        state = guardian.load_state(self.data, "session-test")
        self.assertEqual(guardian.ROLLOVER_REQUIRED, state["status"])
        self.assertEqual("size", state["trigger"])

    def test_hard_limit_blocks_request_with_visible_confirmation_message(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["hard_limit_bytes"])
        result = guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        self.assertEqual("block", result["decision"])
        self.assertEqual(result["systemMessage"], result["reason"])
        self.assertIn("intercepted this prompt", result["systemMessage"])
        self.assertIn("继续交接", result["reason"])
        self.assertEqual(guardian.ROLLOVER_REQUIRED, guardian.load_state(self.data, "session-test")["status"])

    def test_explicit_confirmation_starts_single_agent_rollover_request(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["hard_limit_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED, prompt_count=10)
        confirmation = payload("UserPromptSubmit", self.transcript)
        confirmation["prompt"] = "  继续交接  "
        result = guardian.handle_hook(confirmation, env=self.env)
        self.assertTrue(result["continue"])
        self.assertNotIn("decision", result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("before any tool call", context)
        self.assertIn("explicitly confirmed rollover", context)
        self.assertIn("do not make a separate summary request", context)
        self.assertIn("replacement task is ready before archiving", context.replace("\n", " "))
        self.assertEqual(guardian.ROLLOVER_ACTIVE, guardian.load_state(self.data, "session-test")["status"])

    def test_pending_rollover_blocks_non_confirmation_prompt_again(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["rollover_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED, prompt_count=10)
        result = guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        self.assertEqual("block", result["decision"])
        self.assertIn("waiting for explicit", result["reason"])

    def test_stop_after_rollover_instruction_does_not_recurse(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["hard_limit_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_ACTIVE, prompt_count=10)
        self.assertIsNone(guardian.handle_hook(payload("Stop", self.transcript), env=self.env))

    def test_archive_disabled_is_carried_into_agent_instruction(self):
        config = dict(guardian.DEFAULT_CONFIG, notifications=False, archive_original=False)
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["hard_limit_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED, prompt_count=10)
        confirmation = payload("UserPromptSubmit", self.transcript)
        confirmation["prompt"] = "continue rollover"
        result = guardian.handle_hook(confirmation, env=self.env)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Leave this current task unarchived", context)

    def test_warning_mode_never_requests_rollover(self):
        config = dict(guardian.DEFAULT_CONFIG, mode="warn", notifications=False)
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["hard_limit_bytes"])
        result = guardian.handle_hook(payload("Stop", self.transcript), env=self.env)
        self.assertTrue(result["continue"])
        self.assertIn("warning-only", result["systemMessage"])
        self.assertNotEqual(guardian.ROLLOVER_REQUIRED, guardian.load_state(self.data, "session-test")["status"])

    def test_manual_arm_requests_rollover_once(self):
        make_transcript(self.transcript, 10)
        guardian.write_json_atomic(
            guardian.arm_path(self.data),
            {"cwd": guardian.canonical_path(str(self.root)), "armed_at": guardian.utc_now()},
        )
        result = guardian.handle_hook(payload("Stop", self.transcript, cwd=str(self.root)), env=self.env)
        self.assertTrue(result["continue"])
        self.assertIn("manual rollover", result["systemMessage"])
        self.assertFalse(guardian.arm_path(self.data).exists())

    def test_manual_arm_works_when_monitoring_is_off(self):
        config = dict(guardian.DEFAULT_CONFIG, mode="off", notifications=False)
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, 10)
        guardian.write_json_atomic(
            guardian.arm_path(self.data),
            {"cwd": guardian.canonical_path(str(self.root)), "armed_at": guardian.utc_now()},
        )
        result = guardian.handle_hook(payload("Stop", self.transcript, cwd=str(self.root)), env=self.env)
        self.assertTrue(result["continue"])
        confirmation = payload("UserPromptSubmit", self.transcript, cwd=str(self.root))
        confirmation["prompt"] = "继续交接"
        confirmed = guardian.handle_hook(confirmation, env=self.env)
        self.assertTrue(confirmed["continue"])
        self.assertIn("explicitly confirmed", confirmed["hookSpecificOutput"]["additionalContext"])


class ConfigurationTests(unittest.TestCase):
    def test_invalid_config_is_rejected(self):
        config = dict(guardian.DEFAULT_CONFIG)
        config["warning_bytes"] = config["rollover_bytes"]
        with self.assertRaises(ValueError):
            guardian.validate_config(config)

    def test_default_thresholds_match_measured_safety_policy(self):
        self.assertEqual(64 * guardian.MIB, guardian.DEFAULT_CONFIG["warning_bytes"])
        self.assertEqual(96 * guardian.MIB, guardian.DEFAULT_CONFIG["rollover_bytes"])
        self.assertEqual(128 * guardian.MIB, guardian.DEFAULT_CONFIG["hard_limit_bytes"])


class RepositoryPrivacyTests(unittest.TestCase):
    def test_repository_contains_no_machine_specific_paths_or_private_keys(self):
        repository = Path(__file__).resolve().parents[3]
        patterns = [
            re.compile(re.escape("/") + "Users" + r"/[^/\s]+/"),
            re.compile(r"[A-Za-z]:\\" + "Users" + r"\\[^\\\s]+\\", re.IGNORECASE),
            re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
        ]
        local_user = Path.home().name.lower()
        findings = []
        for path in repository.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in patterns:
                if pattern.search(text):
                    findings.append("%s matched %s" % (path.relative_to(repository), pattern.pattern))
            if local_user not in ("root", "runner") and len(local_user) >= 4 and local_user in text.lower():
                findings.append("%s contains the local account name" % path.relative_to(repository))
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()

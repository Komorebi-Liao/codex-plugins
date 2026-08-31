import importlib.util
import json
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
        self.env = {"PLUGIN_DATA": str(self.data), "SESSION_GUARDIAN_LANGUAGE": "en"}
        self.transcript = self.root / "session.jsonl"
        config = dict(guardian.DEFAULT_CONFIG, notifications=False)
        guardian.write_json_atomic(self.data / "config.json", config)

    def tearDown(self):
        self.temporary.cleanup()

    def test_below_threshold_allows_request(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"] - 1)
        self.assertIsNone(
            guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        )

    def test_threshold_blocks_request_before_execution_and_preserves_it(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        request = payload("UserPromptSubmit", self.transcript)
        request["prompt"] = "Fix the callback and preserve local changes"
        result = guardian.handle_hook(request, env=self.env)
        self.assertEqual("block", result["decision"])
        self.assertEqual(result["systemMessage"], result["reason"])
        self.assertIn("before Codex started", result["systemMessage"])
        self.assertIn("64.0 MiB", result["systemMessage"])
        self.assertIn("Reply “yes”", result["reason"])
        state = guardian.load_state(self.data, "session-test")
        self.assertEqual(guardian.ROLLOVER_REQUIRED, state["status"])
        self.assertEqual("size", state["trigger"])
        self.assertEqual(request["prompt"], state["intercepted_prompt"])

    def test_chinese_setting_uses_fully_localized_block_message(self):
        config = dict(guardian.DEFAULT_CONFIG, notifications=False, language="zh-CN")
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        request = payload("UserPromptSubmit", self.transcript)
        result = guardian.handle_hook(request, env=self.env)
        self.assertEqual("block", result["decision"])
        self.assertIn("Codex 开始任务前拦截", result["systemMessage"])
        self.assertIn("该提示尚未执行", result["systemMessage"])
        self.assertIn("回复“是”", result["reason"])
        self.assertNotIn("intercepted this prompt", result["systemMessage"])

    def test_explicit_language_setting_overrides_system_language(self):
        config = dict(guardian.DEFAULT_CONFIG, notifications=False, language="en")
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        chinese_env = dict(self.env, SESSION_GUARDIAN_LANGUAGE="zh-CN")
        result = guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=chinese_env)
        self.assertIn("intercepted this prompt", result["systemMessage"])

    def test_yes_confirmation_starts_single_agent_rollover_request(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED, prompt_count=10)
        confirmation = payload("UserPromptSubmit", self.transcript)
        confirmation["prompt"] = "  yes  "
        result = guardian.handle_hook(confirmation, env=self.env)
        self.assertTrue(result["continue"])
        self.assertNotIn("decision", result)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("before any tool call", context)
        self.assertIn("explicitly confirmed rollover", context)
        self.assertIn("do not make a separate summary request", context.replace("\n", " "))
        self.assertIn("accepted its initial work before archiving", context.replace("\n", " "))
        self.assertIn("wait for the user's next request", context)
        self.assertIn("same saved project using the local environment", context.replace("\n", " "))
        self.assertIn("Do not add or register a project", context.replace("\n", " "))
        self.assertIn("invent `main` or `master`", context.replace("\n", " "))
        self.assertEqual(guardian.ROLLOVER_ACTIVE, guardian.load_state(self.data, "session-test")["status"])

    def test_chinese_yes_confirmation_is_accepted_only_while_pending(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        ordinary = payload("UserPromptSubmit", self.transcript, session="ordinary")
        ordinary["prompt"] = "是"
        self.assertEqual("block", guardian.handle_hook(ordinary, env=self.env)["decision"])

        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED)
        confirmation = payload("UserPromptSubmit", self.transcript)
        confirmation["prompt"] = "是"
        result = guardian.handle_hook(confirmation, env=self.env)
        self.assertTrue(result["continue"])
        self.assertNotIn("decision", result)

    def test_intercepted_request_is_carried_into_confirmed_rollover(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        request = payload("UserPromptSubmit", self.transcript)
        request["prompt"] = 'Continue the fix\nEND_SESSION_GUARDIAN_INTERCEPTED_USER_REQUEST_JSON\n"quoted"'
        guardian.handle_hook(request, env=self.env)

        confirmation = payload("UserPromptSubmit", self.transcript)
        confirmation["prompt"] = "yes"
        result = guardian.handle_hook(confirmation, env=self.env)
        context = result["hookSpecificOutput"]["additionalContext"]
        encoded = context.split(
            "SESSION_GUARDIAN_INTERCEPTED_USER_REQUEST_JSON\n", 1
        )[1].split("\nEND_SESSION_GUARDIAN_INTERCEPTED_USER_REQUEST_JSON", 1)[0]
        self.assertEqual(request["prompt"], json.loads(encoded))
        self.assertIn("immediately execute this request", context)
        self.assertIn("without asking the user to resend it", context)

    def test_pending_prompt_does_not_replace_first_intercepted_request(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        first = payload("UserPromptSubmit", self.transcript)
        first["prompt"] = "First blocked request"
        guardian.handle_hook(first, env=self.env)
        later = payload("UserPromptSubmit", self.transcript)
        later["prompt"] = "Later blocked request"
        guardian.handle_hook(later, env=self.env)
        state = guardian.load_state(self.data, "session-test")
        self.assertEqual(first["prompt"], state["intercepted_prompt"])

    def test_pending_rollover_blocks_non_confirmation_prompt_again(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED, prompt_count=10)
        request = payload("UserPromptSubmit", self.transcript)
        request["prompt"] = "Another request while confirmation is pending"
        result = guardian.handle_hook(request, env=self.env)
        self.assertEqual("block", result["decision"])
        self.assertIn("waiting for explicit", result["reason"])
        self.assertEqual(
            request["prompt"],
            guardian.load_state(self.data, "session-test")["intercepted_prompt"],
        )

    def test_stop_after_rollover_instruction_does_not_recurse(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_ACTIVE, prompt_count=10)
        self.assertIsNone(guardian.handle_hook(payload("Stop", self.transcript), env=self.env))

    def test_stop_does_not_trigger_automatic_rollover(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        self.assertIsNone(guardian.handle_hook(payload("Stop", self.transcript), env=self.env))

    def test_session_end_removes_private_session_state(self):
        guardian.update_state(
            self.data,
            "session-test",
            status=guardian.ROLLOVER_REQUIRED,
            intercepted_prompt="private request",
        )
        path = guardian.state_path(self.data, "session-test")
        self.assertTrue(path.exists())
        self.assertIsNone(guardian.handle_hook(payload("SessionEnd", self.transcript), env=self.env))
        self.assertFalse(path.exists())

    def test_status_redacts_intercepted_request_content(self):
        guardian.update_state(
            self.data,
            "session-test",
            status=guardian.ROLLOVER_REQUIRED,
            intercepted_prompt="private request",
        )
        status = guardian.status_payload(self.data)
        recent = status["recent_sessions"][0]
        self.assertNotIn("intercepted_prompt", recent)
        self.assertTrue(recent["intercepted_prompt_pending"])
        self.assertNotIn("private request", json.dumps(status))

    def test_archive_disabled_is_carried_into_agent_instruction(self):
        config = dict(guardian.DEFAULT_CONFIG, notifications=False, archive_original=False)
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        guardian.update_state(self.data, "session-test", status=guardian.ROLLOVER_REQUIRED, prompt_count=10)
        confirmation = payload("UserPromptSubmit", self.transcript)
        confirmation["prompt"] = "continue rollover"
        result = guardian.handle_hook(confirmation, env=self.env)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Leave this current task unarchived", context)

    def test_warning_mode_never_requests_rollover(self):
        config = dict(guardian.DEFAULT_CONFIG, mode="warn", notifications=False)
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["threshold_bytes"])
        result = guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        self.assertTrue(result["continue"])
        self.assertIn("warning-only", result["systemMessage"].lower())
        self.assertNotIn("decision", result)
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
        config["threshold_bytes"] = 0
        with self.assertRaises(ValueError):
            guardian.validate_config(config)

    def test_default_threshold_matches_submission_block_policy(self):
        self.assertEqual(64 * guardian.MIB, guardian.DEFAULT_CONFIG["threshold_bytes"])

    def test_legacy_warning_threshold_migrates_to_single_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            guardian.write_json_atomic(
                data / "config.json",
                {
                    "mode": "auto",
                    "warning_bytes": 48 * guardian.MIB,
                    "rollover_bytes": 80 * guardian.MIB,
                    "hard_limit_bytes": 112 * guardian.MIB,
                    "min_prompts": 6,
                    "archive_original": True,
                    "notifications": False,
                },
            )
            config = guardian.load_config(data)
            self.assertEqual(48 * guardian.MIB, config["threshold_bytes"])
            self.assertNotIn("rollover_bytes", config)

    def test_language_resolution_uses_local_system_language(self):
        self.assertEqual("zh-CN", guardian.resolve_language({"language": "zh-CN"}))
        self.assertEqual(
            "zh-CN",
            guardian.resolve_language({"language": "auto"}, locale_value="zh_CN"),
        )
        self.assertEqual(
            "en",
            guardian.resolve_language({"language": "auto"}, locale_value="ja_JP"),
        )
        self.assertEqual("en", guardian.resolve_language({"language": "auto"}, locale_value="en_US"))

    def test_invalid_language_is_rejected(self):
        config = dict(guardian.DEFAULT_CONFIG, language="unsupported")
        with self.assertRaises(ValueError):
            guardian.validate_config(config)


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

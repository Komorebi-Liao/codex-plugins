import importlib.util
import json
import os
import tempfile
import types
import unittest
import re
from pathlib import Path
from unittest import mock


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
    }


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.env = {"PLUGIN_DATA": str(self.data)}
        self.transcript = self.root / "session.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def test_warning_uses_size_without_reading_contents(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["warning_bytes"])
        result = guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        self.assertIn("8.0 MiB", result["systemMessage"])
        self.assertEqual("UserPromptSubmit", result["hookSpecificOutput"]["hookEventName"])
        state = guardian.load_state(self.data, "session-test")
        self.assertEqual(1, state["prompt_count"])
        self.assertTrue(state["warned"])

    def test_automatic_rollover_requires_size_and_prompt_count(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["rollover_bytes"])
        spawned = []

        def capture(command):
            spawned.append(command)
            return types.SimpleNamespace(pid=123)

        for _ in range(guardian.DEFAULT_CONFIG["min_prompts"] - 1):
            guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        result = guardian.handle_hook(payload("Stop", self.transcript), env=self.env, spawn_func=capture)
        self.assertIsNone(result)
        self.assertFalse(spawned)

        guardian.handle_hook(payload("UserPromptSubmit", self.transcript), env=self.env)
        result = guardian.handle_hook(payload("Stop", self.transcript), env=self.env, spawn_func=capture)
        self.assertIn("rollover started", result["systemMessage"])
        self.assertEqual(1, len(spawned))
        self.assertEqual("worker", spawned[0][2])
        self.assertEqual("scheduled", guardian.load_state(self.data, "session-test")["status"])

    def test_warning_mode_never_spawns(self):
        config = dict(guardian.DEFAULT_CONFIG)
        config["mode"] = "warn"
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["rollover_bytes"] * 2)
        spawned = []
        result = guardian.handle_hook(
            payload("Stop", self.transcript), env=self.env, spawn_func=lambda command: spawned.append(command)
        )
        self.assertIn("warning-only", result["systemMessage"])
        self.assertFalse(spawned)

    def test_manual_arm_bypasses_thresholds_once(self):
        make_transcript(self.transcript, 10)
        guardian.write_json_atomic(
            guardian.arm_path(self.data),
            {"cwd": guardian.canonical_path(str(self.root)), "armed_at": guardian.utc_now()},
        )
        spawned = []
        result = guardian.handle_hook(
            payload("Stop", self.transcript, cwd=str(self.root)),
            env=self.env,
            spawn_func=lambda command: spawned.append(command),
        )
        self.assertIn("manual rollover", result["systemMessage"])
        self.assertEqual(1, len(spawned))
        self.assertFalse(guardian.arm_path(self.data).exists())

    def test_manual_arm_works_when_automatic_monitoring_is_off(self):
        config = dict(guardian.DEFAULT_CONFIG)
        config["mode"] = "off"
        guardian.write_json_atomic(self.data / "config.json", config)
        make_transcript(self.transcript, 10)
        guardian.write_json_atomic(
            guardian.arm_path(self.data),
            {"cwd": guardian.canonical_path(str(self.root)), "armed_at": guardian.utc_now()},
        )
        spawned = []
        result = guardian.handle_hook(
            payload("Stop", self.transcript, cwd=str(self.root)),
            env=self.env,
            spawn_func=lambda command: spawned.append(command),
        )
        self.assertIn("manual rollover", result["systemMessage"])
        self.assertEqual(1, len(spawned))

    def test_in_progress_session_does_not_recurse(self):
        make_transcript(self.transcript, guardian.DEFAULT_CONFIG["rollover_bytes"] * 2)
        guardian.update_state(self.data, "session-test", status="summarizing", prompt_count=99)
        spawned = []
        result = guardian.handle_hook(
            payload("Stop", self.transcript), env=self.env, spawn_func=lambda command: spawned.append(command)
        )
        self.assertIsNone(result)
        self.assertFalse(spawned)


class SummaryTests(unittest.TestCase):
    def summary(self):
        return {
            "title": "Fix the build",
            "user_goal": "Make CI pass",
            "completed": ["Fixed parsing"],
            "current_state": "One test remains",
            "decisions": ["Keep Python 3.9"],
            "files_changed": ["parser.py"],
            "verification": ["Unit tests: 20/21"],
            "pending": ["Fix Windows path test"],
            "next_step": "Run the failing test on Windows",
            "constraints": ["Do not change the public API"],
            "warnings": [],
        }

    def test_parse_and_format_handoff(self):
        parsed = guardian.parse_summary("```json\n%s\n```" % json.dumps(self.summary()))
        handoff = guardian.format_handoff(parsed)
        self.assertIn("Make CI pass", handoff)
        self.assertIn("- Fixed parsing", handoff)
        self.assertIn("Do not act on it", handoff)

    def test_safe_title_is_bounded(self):
        title = guardian.safe_title("x" * 500, None)
        self.assertLessEqual(len(title), 120)
        self.assertTrue(title.endswith("continued"))

    def test_invalid_config_is_rejected(self):
        config = dict(guardian.DEFAULT_CONFIG)
        config["warning_bytes"] = config["rollover_bytes"]
        with self.assertRaises(ValueError):
            guardian.validate_config(config)


class FakeClient:
    instances = []
    fail_seed = False
    pinned = False
    active_goal = False
    fail_goal_copy = False

    def __init__(self, binary, timeout=900):
        self.calls = []
        self.turn_count = 0
        FakeClient.instances.append(self)

    def request(self, method, params=None, allow_error=False):
        self.calls.append((method, params, allow_error))
        if method == "thread/read":
            return {"thread": {"id": "old", "name": "Original", "isPinned": self.pinned}}
        if method == "thread/goal/get":
            if self.active_goal:
                return {"goal": {"objective": "Finish safely", "status": "active", "tokenBudget": 1000}}
            return None
        if method == "thread/goal/set" and self.fail_goal_copy:
            raise guardian.AppServerError("goal copy failed")
        if method == "turn/start":
            self.turn_count += 1
            return {"turn": {"id": "turn-%d" % self.turn_count}}
        if method == "thread/start":
            return {"thread": {"id": "new"}}
        return {}

    def notify(self, method, params=None):
        self.calls.append((method, params, False))

    def wait_for_turn(self, thread_id, turn_id):
        if thread_id == "new":
            if self.fail_seed:
                raise guardian.AppServerError("seed failed")
            return "Handoff loaded."
        return json.dumps(SummaryTests().summary())

    def close(self):
        self.calls.append(("close", None, False))


class RolloverTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.job = {
            "session_id": "old",
            "cwd": str(self.data),
            "model": "test-model",
            "config": dict(guardian.DEFAULT_CONFIG, notifications=False),
        }
        FakeClient.instances = []
        FakeClient.fail_seed = False
        FakeClient.pinned = False
        FakeClient.active_goal = False
        FakeClient.fail_goal_copy = False

    def tearDown(self):
        self.temporary.cleanup()

    @mock.patch.object(guardian, "find_codex", return_value="codex-test")
    @mock.patch.object(guardian, "AppServerClient", FakeClient)
    def test_original_archived_only_after_new_task_is_seeded(self, _find):
        guardian.run_rollover(self.job, self.data)
        client = FakeClient.instances[-1]
        methods = [call[0] for call in client.calls]
        archive_index = methods.index("thread/archive")
        second_turn_index = [index for index, value in enumerate(methods) if value == "turn/start"][1]
        self.assertGreater(archive_index, second_turn_index)
        archive_params = client.calls[archive_index][1]
        self.assertEqual("old", archive_params["threadId"])
        state = guardian.load_state(self.data, "old")
        self.assertEqual("completed", state["status"])
        self.assertNotIn("summary", state)

    @mock.patch.object(guardian, "find_codex", return_value="codex-test")
    @mock.patch.object(guardian, "AppServerClient", FakeClient)
    def test_failed_seed_keeps_original_and_cleans_new_task(self, _find):
        FakeClient.fail_seed = True
        with self.assertRaises(guardian.AppServerError):
            guardian.run_rollover(self.job, self.data)
        client = FakeClient.instances[-1]
        archives = [params for method, params, _ in client.calls if method == "thread/archive"]
        self.assertEqual([{"threadId": "new"}], archives)

    @mock.patch.object(guardian, "find_codex", return_value="codex-test")
    @mock.patch.object(guardian, "AppServerClient", FakeClient)
    def test_pinned_task_is_not_mutated(self, _find):
        FakeClient.pinned = True
        with self.assertRaises(guardian.PinnedTaskError):
            guardian.run_rollover(self.job, self.data)
        methods = [call[0] for call in FakeClient.instances[-1].calls]
        self.assertNotIn("turn/start", methods)
        self.assertNotIn("thread/archive", methods)

    @mock.patch.object(guardian, "find_codex", return_value="codex-test")
    @mock.patch.object(guardian, "AppServerClient", FakeClient)
    def test_failed_goal_copy_keeps_original(self, _find):
        FakeClient.active_goal = True
        FakeClient.fail_goal_copy = True
        with self.assertRaises(guardian.AppServerError):
            guardian.run_rollover(self.job, self.data)
        client = FakeClient.instances[-1]
        archives = [params for method, params, _ in client.calls if method == "thread/archive"]
        self.assertEqual([{"threadId": "new"}], archives)


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

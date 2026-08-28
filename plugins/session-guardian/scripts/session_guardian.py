#!/usr/bin/env python3
"""Local lifecycle monitor and failure-safe task rollover for Codex."""

from __future__ import print_function

import argparse
import collections
import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path


MIB = 1024 * 1024
STATE_VERSION = 1
DEFAULT_CONFIG = {
    "mode": "auto",
    "warning_bytes": 64 * MIB,
    "rollover_bytes": 96 * MIB,
    "hard_limit_bytes": 128 * MIB,
    "min_prompts": 6,
    "archive_original": True,
    "notifications": True,
    "retry_cooldown_seconds": 15 * 60,
    "worker_timeout_seconds": 15 * 60,
}
ACTIVE_STATES = {"scheduled", "summarizing", "creating", "seeding", "archiving"}

HANDOFF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "user_goal": {"type": "string"},
        "completed": {"type": "array", "items": {"type": "string"}},
        "current_state": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "array", "items": {"type": "string"}},
        "pending": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title",
        "user_goal",
        "completed",
        "current_state",
        "decisions",
        "files_changed",
        "verification",
        "pending",
        "next_step",
        "constraints",
        "warnings",
    ],
}

SUMMARY_PROMPT = """Create a compact handoff for this Codex task.

Return only the JSON object required by the supplied output schema. Do not call tools, edit files,
continue the implementation, or ask the user a question. Preserve concrete facts needed to resume:
the user's goal, work completed, current state, important decisions, changed files, verification,
pending work, the exact next step, constraints, and unresolved warnings. Do not include secrets,
credentials, hidden reasoning, redundant discussion, or local machine paths unless a path is itself
necessary to continue the task. Keep the complete JSON concise enough for a fresh task.
"""

CONTINUATION_INSTRUCTIONS = """This task was created by Session Guardian as a compact continuation
of an archived Codex task. Treat the first user message as handoff context, not as a request to do
more work. Acknowledge that the handoff is loaded in one short sentence, then wait for the user's
next request. On later turns, continue from the handoff without claiming access to omitted history.
"""


class AppServerError(RuntimeError):
    pass


class PinnedTaskError(RuntimeError):
    pass


def utc_now():
    return int(time.time())


def canonical_path(value):
    return os.path.normcase(os.path.realpath(os.path.abspath(value or os.getcwd())))


def private_data_dir(env=None):
    env = env or os.environ
    configured = env.get("PLUGIN_DATA") or env.get("SESSION_GUARDIAN_DATA")
    if configured:
        path = Path(configured)
    else:
        state_home = env.get("XDG_STATE_HOME")
        path = Path(state_home) / "session-guardian" if state_home else Path.home() / ".session-guardian"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def read_json(path, default=None):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return {} if default is None else default


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, str(path))
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def load_config(data_dir):
    raw = read_json(Path(data_dir) / "config.json", {})
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        config.update({key: raw[key] for key in DEFAULT_CONFIG if key in raw})
    validate_config(config)
    return config


def validate_config(config):
    if config.get("mode") not in ("auto", "warn", "off"):
        raise ValueError("mode must be auto, warn, or off")
    for key in (
        "warning_bytes",
        "rollover_bytes",
        "hard_limit_bytes",
        "min_prompts",
    ):
        if not isinstance(config.get(key), int) or config[key] < 1:
            raise ValueError("%s must be a positive integer" % key)
    if config["warning_bytes"] >= config["rollover_bytes"]:
        raise ValueError("warning threshold must be smaller than rollover threshold")
    if config["rollover_bytes"] >= config["hard_limit_bytes"]:
        raise ValueError("rollover threshold must be smaller than hard limit")
    for key in ("archive_original", "notifications"):
        if not isinstance(config.get(key), bool):
            raise ValueError("%s must be boolean" % key)


def session_key(session_id):
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def state_path(data_dir, session_id):
    return Path(data_dir) / "sessions" / (session_key(session_id) + ".json")


def load_state(data_dir, session_id):
    state = read_json(state_path(data_dir, session_id), {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", STATE_VERSION)
    state.setdefault("prompt_count", 0)
    state.setdefault("status", "monitoring")
    return state


def update_state(data_dir, session_id, **fields):
    state = load_state(data_dir, session_id)
    state.update(fields)
    state["updated_at"] = utc_now()
    write_json_atomic(state_path(data_dir, session_id), state)
    return state


def transcript_size(path_value):
    if not path_value:
        return 0
    try:
        return Path(path_value).stat().st_size
    except OSError:
        return 0


def format_mib(byte_count):
    return "%.1f MiB" % (float(byte_count) / MIB)


def warning_output(message, event_name):
    result = {"continue": True, "systemMessage": message}
    if event_name == "UserPromptSubmit":
        result["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "Session Guardian detected a large task. Complete the user's current request "
                "normally; automatic rollover, if enabled, happens only after the turn stops."
            ),
        }
    return result


def block_output(message):
    return {
        "continue": False,
        "stopReason": message,
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": message,
        },
    }


def arm_path(data_dir):
    return Path(data_dir) / "armed.json"


def consume_arm(data_dir, cwd):
    path = arm_path(data_dir)
    value = read_json(path, {})
    if not isinstance(value, dict) or not value:
        return False
    if canonical_path(value.get("cwd")) != canonical_path(cwd):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def acquire_rollover_lock(data_dir, session_id):
    lock_dir = Path(data_dir) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / (session_key(session_id) + ".lock")
    if path.exists():
        try:
            if utc_now() - int(path.stat().st_mtime) > 3600:
                path.unlink()
        except OSError:
            return None
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        return path
    except OSError:
        return None


def release_lock(path):
    if not path:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def detached_spawn(command):
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def schedule_rollover(payload, data_dir, config, byte_count, lock, spawn_func=detached_spawn, trigger="size"):
    session_id = payload["session_id"]
    job_dir = Path(data_dir) / "jobs"
    job_dir.mkdir(parents=True, exist_ok=True)
    job_path = job_dir / (str(uuid.uuid4()) + ".json")
    job = {
        "version": STATE_VERSION,
        "session_id": session_id,
        "cwd": payload.get("cwd") or os.getcwd(),
        "model": payload.get("model"),
        "transcript_bytes": byte_count,
        "trigger": trigger,
        "lock_path": str(lock),
        "created_at": utc_now(),
        "config": config,
    }
    write_json_atomic(job_path, job)
    update_state(
        data_dir,
        session_id,
        status="scheduled",
        transcript_bytes=byte_count,
        trigger=trigger,
        scheduled_at=utc_now(),
        last_error=None,
    )
    try:
        spawn_func([sys.executable, str(Path(__file__).resolve()), "worker", "--job", str(job_path)])
    except Exception:
        update_state(data_dir, session_id, status="failed", scheduled_at=None, last_error="worker_start_failed")
        release_lock(lock)
        try:
            job_path.unlink()
        except OSError:
            pass
        raise


def handle_hook(payload, env=None, spawn_func=detached_spawn):
    env = env or os.environ
    data_dir = private_data_dir(env)
    config = load_config(data_dir)
    event_name = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    if not session_id:
        return None
    if config["mode"] == "off" and event_name != "Stop":
        return None

    size = transcript_size(payload.get("transcript_path"))
    state = load_state(data_dir, session_id)

    if event_name == "UserPromptSubmit":
        prompt_count = int(state.get("prompt_count", 0)) + 1
        fields = {"prompt_count": prompt_count, "transcript_bytes": size, "status": state.get("status", "monitoring")}
        should_warn = size >= config["warning_bytes"] and not state.get("warned")
        if should_warn:
            fields.update({"warned": True, "warning_at": utc_now()})
        update_state(data_dir, session_id, **fields)
        if size >= config["hard_limit_bytes"] and config["mode"] == "auto":
            summary_prompt_allowed = (
                state.get("status") == "summarizing"
                and state.get("summary_prompt_pending") is True
                and ("prompt" not in payload or payload.get("prompt") == SUMMARY_PROMPT)
            )
            if summary_prompt_allowed:
                update_state(data_dir, session_id, summary_prompt_pending=False)
            else:
                if state.get("status") not in ACTIVE_STATES:
                    lock = acquire_rollover_lock(data_dir, session_id)
                    if lock:
                        try:
                            schedule_rollover(
                                payload,
                                data_dir,
                                config,
                                size,
                                lock,
                                spawn_func=spawn_func,
                                trigger="hard_limit",
                            )
                        except Exception:
                            return block_output(
                                "Session Guardian blocked this prompt at %s, but could not start the replacement worker. "
                                "The original task remains available." % format_mib(size)
                            )
                return block_output(
                    "Session Guardian blocked this prompt because the task reached the %s hard safety limit. "
                    "A summarized replacement task is being prepared." % format_mib(config["hard_limit_bytes"])
                )
        if should_warn:
            action = "Automatic rollover will run after a completed turn at %s." % format_mib(
                config["rollover_bytes"]
            )
            if config["mode"] == "warn":
                action = "Warning-only mode is enabled; no task will be archived automatically."
            return warning_output(
                "Session Guardian: this task is %s. %s" % (format_mib(size), action), event_name
            )
        return None

    if event_name != "Stop":
        return None
    if state.get("status") in ACTIVE_STATES:
        return None

    forced = consume_arm(data_dir, payload.get("cwd") or os.getcwd())
    if config["mode"] == "off" and not forced:
        return None
    prompt_count = int(state.get("prompt_count", 0))
    threshold_reached = size >= config["rollover_bytes"]
    enough_prompts = prompt_count >= config["min_prompts"]
    hard_limit_reached = size >= config["hard_limit_bytes"]
    size_rollover = threshold_reached and (enough_prompts or hard_limit_reached)
    if not forced and not size_rollover:
        return None

    last_attempt = int(state.get("last_attempt_at") or 0)
    if not forced and utc_now() - last_attempt < config["retry_cooldown_seconds"]:
        return None
    if config["mode"] == "warn" and not forced:
        update_state(data_dir, session_id, status="warning", transcript_bytes=size)
        return warning_output(
            "Session Guardian: rollover threshold reached at %s; warning-only mode left this task unchanged."
            % format_mib(size),
            event_name,
        )

    lock = acquire_rollover_lock(data_dir, session_id)
    if not lock:
        return None
    trigger = "manual" if forced else "size"
    try:
        schedule_rollover(payload, data_dir, config, size, lock, spawn_func=spawn_func, trigger=trigger)
    except Exception:
        return warning_output(
            "Session Guardian could not start the rollover worker; the current task was left unchanged.",
            event_name,
        )
    return warning_output(
        "Session Guardian: %srollover started. A compact replacement task will be created before this task is archived."
        % ("manual " if forced else ""),
        event_name,
    )


def find_codex(env=None):
    env = env or os.environ
    requested = env.get("SESSION_GUARDIAN_CODEX_BIN")
    if requested and Path(requested).is_file():
        return requested
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    candidates = [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
    ]
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "Programs" / "Codex" / "codex.exe"))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


class AppServerClient(object):
    def __init__(self, codex_binary, timeout=900):
        self.timeout = timeout
        self.next_id = 1
        self.messages = queue.Queue()
        self.backlog = collections.deque()
        self.process = subprocess.Popen(
            [codex_binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _read_output(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put({"_reader_closed": True})

    def send(self, message):
        if self.process.poll() is not None:
            raise AppServerError("Codex App Server exited unexpectedly")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def _handle_server_request(self, message):
        method = message.get("method", "")
        request_id = message.get("id")
        if request_id is None:
            return False
        if "requestApproval" in method:
            result = {"decision": "decline"}
        elif method in ("tool/requestUserInput", "mcpServer/elicitation/request"):
            result = {"action": "cancel", "content": None}
        else:
            self.send({"id": request_id, "error": {"code": -32601, "message": "Unsupported request"}})
            return True
        self.send({"id": request_id, "result": result})
        return True

    def _get_message(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError("Timed out waiting for Codex App Server")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty:
            raise AppServerError("Timed out waiting for Codex App Server")
        if message.get("_reader_closed"):
            raise AppServerError("Codex App Server closed its output stream")
        if message.get("method") and "id" in message and self._handle_server_request(message):
            return self._get_message(deadline)
        return message

    def request(self, method, params=None, allow_error=False):
        request_id = self.next_id
        self.next_id += 1
        message = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            incoming = self._get_message(deadline)
            if incoming.get("id") == request_id:
                if incoming.get("error"):
                    if allow_error:
                        return None
                    raise AppServerError("%s failed: %s" % (method, incoming["error"].get("message", "unknown")))
                return incoming.get("result", {})
            self.backlog.append(incoming)

    def wait_for_turn(self, thread_id, turn_id):
        deadline = time.monotonic() + self.timeout
        final_messages = []
        while True:
            incoming = self.backlog.popleft() if self.backlog else self._get_message(deadline)
            method = incoming.get("method")
            params = incoming.get("params") or {}
            incoming_thread = params.get("threadId")
            incoming_turn = params.get("turnId")
            if method == "item/completed" and (not incoming_thread or incoming_thread == thread_id):
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and (not incoming_turn or incoming_turn == turn_id):
                    text = item.get("text")
                    if text:
                        final_messages.append((item.get("phase"), text))
            if method == "turn/completed":
                turn = params.get("turn") or {}
                completed_id = turn.get("id") or incoming_turn
                if (not incoming_thread or incoming_thread == thread_id) and completed_id == turn_id:
                    status = turn.get("status")
                    if status != "completed":
                        error = (turn.get("error") or {}).get("message", status or "unknown")
                        raise AppServerError("Codex turn did not complete: %s" % error)
                    finals = [text for phase, text in final_messages if phase == "final_answer"]
                    return finals[-1] if finals else (final_messages[-1][1] if final_messages else "")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except Exception:
                pass


def parse_summary(text):
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        value = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise AppServerError("Summary response was not valid JSON")
        value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise AppServerError("Summary response was not an object")
    for field in HANDOFF_SCHEMA["required"]:
        if field not in value:
            raise AppServerError("Summary response omitted %s" % field)
    return value


def markdown_list(values):
    cleaned = [str(value).strip() for value in values or [] if str(value).strip()]
    return "\n".join("- " + value for value in cleaned) if cleaned else "- None recorded"


def format_handoff(summary):
    return """# Automatic task handoff

This message is continuity context from a task archived by Session Guardian. Do not act on it until
the user sends the next request.

## Goal
{goal}

## Completed
{completed}

## Current state
{state}

## Decisions
{decisions}

## Files changed
{files}

## Verification
{verification}

## Pending work
{pending}

## Exact next step
{next_step}

## Constraints
{constraints}

## Warnings
{warnings}
""".format(
        goal=str(summary.get("user_goal", "")).strip(),
        completed=markdown_list(summary.get("completed")),
        state=str(summary.get("current_state", "")).strip(),
        decisions=markdown_list(summary.get("decisions")),
        files=markdown_list(summary.get("files_changed")),
        verification=markdown_list(summary.get("verification")),
        pending=markdown_list(summary.get("pending")),
        next_step=str(summary.get("next_step", "")).strip(),
        constraints=markdown_list(summary.get("constraints")),
        warnings=markdown_list(summary.get("warnings")),
    )


def response_turn_id(result):
    turn = (result or {}).get("turn") or {}
    turn_id = turn.get("id")
    if not turn_id:
        raise AppServerError("Codex App Server did not return a turn id")
    return turn_id


def safe_title(original_title, summary_title):
    base = (original_title or summary_title or "Continued task").strip()
    suffix = " · continued"
    if base.endswith(suffix):
        return base[:120]
    return (base[: max(1, 120 - len(suffix))] + suffix).strip()


def notify_desktop(title, message):
    try:
        system = platform.system()
        if system == "Darwin" and shutil.which("osascript"):
            escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
            escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", 'display notification "%s" with title "%s"' % (escaped_message, escaped_title)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except Exception:
        pass


def run_rollover(job, data_dir):
    session_id = job["session_id"]
    config = job["config"]
    codex_binary = find_codex()
    if not codex_binary:
        raise AppServerError("codex executable not found")
    client = AppServerClient(codex_binary, timeout=config["worker_timeout_seconds"])
    new_thread_id = None
    original_archived = False
    try:
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "session_guardian",
                    "title": "Session Guardian",
                    "version": "1.1.0",
                }
            },
        )
        client.notify("initialized", {})
        original_result = client.request("thread/read", {"threadId": session_id, "includeTurns": False})
        original = (original_result or {}).get("thread") or {}
        if original.get("isPinned"):
            raise PinnedTaskError("Pinned tasks are never archived automatically")
        original_title = original.get("name")
        goal_result = client.request("thread/goal/get", {"threadId": session_id})

        update_state(
            data_dir,
            session_id,
            status="summarizing",
            summary_prompt_pending=True,
            last_attempt_at=utc_now(),
        )
        client.request("thread/resume", {"threadId": session_id})
        summary_turn = client.request(
            "turn/start",
            {
                "threadId": session_id,
                "input": [{"type": "text", "text": SUMMARY_PROMPT}],
                "outputSchema": HANDOFF_SCHEMA,
            },
        )
        summary_text = client.wait_for_turn(session_id, response_turn_id(summary_turn))
        summary = parse_summary(summary_text)

        update_state(data_dir, session_id, status="creating", summary_prompt_pending=False)
        thread_params = {
            "cwd": job.get("cwd"),
            "developerInstructions": CONTINUATION_INSTRUCTIONS,
            "serviceName": "session_guardian",
        }
        if job.get("model"):
            thread_params["model"] = job["model"]
        new_result = client.request("thread/start", thread_params)
        new_thread = (new_result or {}).get("thread") or {}
        new_thread_id = new_thread.get("id")
        if not new_thread_id:
            raise AppServerError("Codex App Server did not return a replacement task id")
        client.request(
            "thread/name/set",
            {"threadId": new_thread_id, "name": safe_title(original_title, summary.get("title"))},
            allow_error=True,
        )

        update_state(data_dir, session_id, status="seeding", new_thread_id=new_thread_id)
        seed_turn = client.request(
            "turn/start",
            {"threadId": new_thread_id, "input": [{"type": "text", "text": format_handoff(summary)}]},
        )
        client.wait_for_turn(new_thread_id, response_turn_id(seed_turn))

        goal = (goal_result or {}).get("goal") if isinstance(goal_result, dict) else None
        if isinstance(goal, dict) and goal.get("objective") and goal.get("status") == "active":
            goal_params = {
                "threadId": new_thread_id,
                "objective": goal["objective"],
                "status": "active",
            }
            if goal.get("tokenBudget"):
                goal_params["tokenBudget"] = goal["tokenBudget"]
            client.request("thread/goal/set", goal_params)

        archived = False
        if config["archive_original"]:
            update_state(data_dir, session_id, status="archiving")
            client.request("thread/archive", {"threadId": session_id})
            archived = True
            original_archived = True
        update_state(
            data_dir,
            session_id,
            status="completed",
            completed_at=utc_now(),
            archived=archived,
            new_thread_id=new_thread_id,
            last_error=None,
        )
        if config["notifications"]:
            message = "A compact replacement task is ready."
            if archived:
                message += " The original was archived."
            notify_desktop("Session Guardian", message)
    except Exception:
        if original_archived:
            if config["notifications"]:
                notify_desktop("Session Guardian", "A compact replacement task is ready. The original was archived.")
            return
        if new_thread_id:
            client.request("thread/archive", {"threadId": new_thread_id}, allow_error=True)
        raise
    finally:
        client.close()


def worker_main(job_path_value):
    job_path = Path(job_path_value)
    job = read_json(job_path, {})
    data_dir = private_data_dir()
    session_id = job.get("session_id")
    lock = job.get("lock_path")
    try:
        if not session_id or not isinstance(job.get("config"), dict):
            raise ValueError("invalid rollover job")
        run_rollover(job, data_dir)
    except PinnedTaskError:
        if session_id:
            update_state(data_dir, session_id, status="skipped_pinned", last_attempt_at=utc_now(), last_error=None)
        if job.get("config", {}).get("notifications"):
            notify_desktop("Session Guardian", "The task is pinned, so automatic rollover was skipped.")
    except Exception as error:
        if session_id:
            update_state(
                data_dir,
                session_id,
                status="failed",
                last_attempt_at=utc_now(),
                last_error=type(error).__name__,
            )
        if job.get("config", {}).get("notifications"):
            notify_desktop("Session Guardian", "Rollover failed; the original task was left available.")
    finally:
        release_lock(lock)
        try:
            job_path.unlink()
        except OSError:
            pass


def yes_no(value):
    lowered = value.lower()
    if lowered in ("yes", "true", "1", "on"):
        return True
    if lowered in ("no", "false", "0", "off"):
        return False
    raise argparse.ArgumentTypeError("expected yes or no")


def configure(args, data_dir):
    config = load_config(data_dir)
    if args.mode is not None:
        config["mode"] = args.mode
    if args.warning_mib is not None:
        config["warning_bytes"] = int(args.warning_mib * MIB)
    if args.rollover_mib is not None:
        config["rollover_bytes"] = int(args.rollover_mib * MIB)
    if args.hard_limit_mib is not None:
        config["hard_limit_bytes"] = int(args.hard_limit_mib * MIB)
    if args.min_prompts is not None:
        config["min_prompts"] = args.min_prompts
    if args.archive_original is not None:
        config["archive_original"] = args.archive_original
    if args.notifications is not None:
        config["notifications"] = args.notifications
    validate_config(config)
    write_json_atomic(Path(data_dir) / "config.json", config)
    return config


def status_payload(data_dir):
    sessions = []
    session_dir = Path(data_dir) / "sessions"
    if session_dir.exists():
        states = []
        for path in session_dir.glob("*.json"):
            state = read_json(path, {})
            if isinstance(state, dict):
                state = dict(state)
                state.pop("new_thread_id", None)
                states.append(state)
        sessions = sorted(states, key=lambda item: item.get("updated_at", 0), reverse=True)[:10]
    return {
        "config": load_config(data_dir),
        "forced_rollover_armed": arm_path(data_dir).exists(),
        "codex_available": bool(find_codex()),
        "recent_sessions": sessions,
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Monitor and roll over oversized Codex tasks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook", help=argparse.SUPPRESS)
    worker = subparsers.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--job", required=True)
    subparsers.add_parser("status", help="Show configuration and recent local state")
    subparsers.add_parser("doctor", help="Check local prerequisites")
    arm = subparsers.add_parser("arm", help="Force rollover after the next completed turn in this directory")
    arm.add_argument("--cwd", default=os.getcwd())
    disarm = subparsers.add_parser("disarm", help="Cancel a forced rollover before it starts")
    disarm.add_argument("--cwd", default=os.getcwd())
    config_parser = subparsers.add_parser("configure", help="Update persistent local settings")
    config_parser.add_argument("--mode", choices=("auto", "warn", "off"))
    config_parser.add_argument("--warning-mib", type=float)
    config_parser.add_argument("--rollover-mib", type=float)
    config_parser.add_argument("--hard-limit-mib", type=float)
    config_parser.add_argument("--min-prompts", type=int)
    config_parser.add_argument("--archive-original", type=yes_no)
    config_parser.add_argument("--notifications", type=yes_no)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    data_dir = private_data_dir()
    if args.command == "hook":
        try:
            payload = json.load(sys.stdin)
            result = handle_hook(payload)
            if result:
                json.dump(result, sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
        except Exception:
            json.dump(
                {
                    "continue": True,
                    "systemMessage": "Session Guardian could not inspect this task; no rollover action was taken.",
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        return 0
    if args.command == "worker":
        worker_main(args.job)
        return 0
    if args.command == "status":
        print(json.dumps(status_payload(data_dir), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "doctor":
        codex_binary = find_codex()
        result = {
            "codex_available": bool(codex_binary),
            "data_directory_writable": os.access(str(data_dir), os.W_OK),
            "configuration_valid": True,
            "python": platform.python_version(),
        }
        if codex_binary:
            try:
                completed = subprocess.run(
                    [codex_binary, "app-server", "--help"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                result["app_server_available"] = completed.returncode == 0
            except Exception:
                result["app_server_available"] = False
        else:
            result["app_server_available"] = False
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "arm":
        write_json_atomic(arm_path(data_dir), {"cwd": canonical_path(args.cwd), "armed_at": utc_now()})
        print("Forced rollover armed for the next completed turn in this working directory.")
        return 0
    if args.command == "disarm":
        value = read_json(arm_path(data_dir), {})
        if value and canonical_path(value.get("cwd")) == canonical_path(args.cwd):
            try:
                arm_path(data_dir).unlink()
                print("Forced rollover cancelled.")
            except OSError:
                print("No forced rollover was armed.")
        else:
            print("No forced rollover was armed for this working directory.")
        return 0
    if args.command == "configure":
        print(json.dumps(configure(args, data_dir), indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

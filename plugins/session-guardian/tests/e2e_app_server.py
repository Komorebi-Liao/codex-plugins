#!/usr/bin/env python3
"""Opt-in live smoke test. Creates model turns and archives all temporary tasks."""

import importlib.util
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_guardian.py"
SPEC = importlib.util.spec_from_file_location("session_guardian", SCRIPT)
guardian = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guardian)


def initialized_client():
    client = guardian.AppServerClient(guardian.find_codex(), timeout=180)
    client.request(
        "initialize",
        {
            "clientInfo": {
                "name": "session_guardian_e2e",
                "title": "Session Guardian E2E",
                "version": "1.0.0",
            }
        },
    )
    client.notify("initialized", {})
    return client


def main():
    original_id = None
    replacement_id = None
    client = initialized_client()
    try:
        started = client.request(
            "thread/start",
            {
                "cwd": str(Path(__file__).resolve().parents[3]),
                "developerInstructions": "This is a temporary integration test. Do not call tools.",
                "serviceName": "session_guardian_e2e",
            },
        )
        original_id = started["thread"]["id"]
        client.request(
            "thread/name/set",
            {"threadId": original_id, "name": "Session Guardian E2E (temporary)"},
            allow_error=True,
        )
        turn = client.request(
            "turn/start",
            {
                "threadId": original_id,
                "input": [
                    {
                        "type": "text",
                        "text": "Remember that the test goal is to preserve the phrase cobalt bridge. Reply briefly.",
                    }
                ],
            },
        )
        client.wait_for_turn(original_id, guardian.response_turn_id(turn))
    finally:
        client.close()

    try:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            job = {
                "session_id": original_id,
                "cwd": str(Path(__file__).resolve().parents[3]),
                "model": None,
                "config": dict(guardian.DEFAULT_CONFIG, notifications=False),
            }
            guardian.run_rollover(job, data_dir)
            state = guardian.load_state(data_dir, original_id)
            if state.get("status") != "completed" or not state.get("archived"):
                raise RuntimeError("rollover transaction did not commit")
            replacement_id = state.get("new_thread_id")
            if not replacement_id:
                raise RuntimeError("replacement task id was not recorded")
    finally:
        cleanup = initialized_client()
        try:
            for thread_id in (replacement_id, original_id):
                if thread_id:
                    cleanup.request("thread/archive", {"threadId": thread_id}, allow_error=True)
        finally:
            cleanup.close()
    print("Session Guardian live rollover: OK (temporary tasks archived)")


if __name__ == "__main__":
    main()

# Session Guardian architecture

## Why a plugin with hooks and a skill

A skill supplies repeatable workflow instructions but cannot wake itself. An MCP server exposes callable tools but is also passive until the model calls it. Codex lifecycle hooks are the component that can observe every prompt submission and completed turn. Session Guardian therefore combines:

- `hooks/hooks.json` for automatic, local observation;
- `scripts/session_guardian.py` for deterministic thresholds, state, App Server orchestration, and notifications;
- `skills/session-rollover/SKILL.md` for manual inspection and configuration.

No MCP server is needed because all required operations are local Codex operations.

## Detection signal

Codex supplies `session_id`, `transcript_path`, `cwd`, event name, and model to command hooks. The transcript format is not a stable public interface, so Session Guardian deliberately does not parse it. It uses only:

- transcript file size in bytes;
- the number of `UserPromptSubmit` events observed for the task;
- a one-shot manual rollover flag, when explicitly requested.

The default size policy is 64 MiB for warning, 96 MiB for automatic rollover, and 128 MiB for the hard safety limit. The 96 MiB path also requires six observed prompts so that one large attachment does not immediately archive a new task. At 128 MiB, a new user prompt is blocked and rollover starts regardless of the prompt count.

Transcript byte size is the sole automatic risk signal. Session Guardian does not inspect Clash/Mihomo connections, proxy traffic, packets, or operating-system network counters. It measures a strong proxy for repeated context-upload cost rather than network traffic itself.

## Rollover transaction

The rollover is an ordered, failure-safe transaction:

1. Atomically acquire a per-task lock in the plugin data directory.
2. Spawn a detached worker after a normal 96 MiB `Stop` trigger, or immediately when the 128 MiB guard blocks a user prompt.
3. Connect to `codex app-server` over local stdio.
4. Read and resume the original task.
5. Request a structured handoff summary with a JSON output schema.
6. Create a fresh task in the same working directory and with the same model.
7. Seed the new task with the handoff and wait for its acknowledgement.
8. Copy an active goal when one exists.
9. Archive the original task only after all preceding steps succeed.
10. Record status without retaining the summary and emit a best-effort desktop notification.

If the original task is pinned, automatic rollover stops before mutation. If summary generation or task creation fails, the original remains untouched.

## Recursion and concurrency controls

Summary generation itself creates another Codex turn. The per-task state marks rollover as in progress, allowing this one intentional full-context request through the hard guard while preventing another worker from being scheduled. Per-task lock files prevent duplicate workers. Locks older than one hour are treated as stale after an interrupted process.

## Privacy model

Monitoring never reads transcript contents. The only content-processing step is a normal request through the user's local Codex App Server. No third-party endpoint, telemetry client, analytics SDK, or separate API credential is present. The generated summary is sent directly into the replacement task and is not written into Session Guardian's state.

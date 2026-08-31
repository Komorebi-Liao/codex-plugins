# Session Guardian architecture

## Why a plugin with hooks and a skill

A skill supplies repeatable workflow instructions but cannot wake itself. An MCP server exposes callable tools but is also passive until the model calls it. Codex lifecycle hooks are the component that can observe every prompt submission and completed turn. Session Guardian therefore combines:

- `hooks/hooks.json` for automatic, local observation;
- `scripts/session_guardian.py` for deterministic thresholds, state, enforcement instructions, and notifications;
- `skills/session-rollover/SKILL.md` for the failure-safe app-tool transaction, manual inspection, and configuration.

No MCP server is needed because all required operations are local Codex operations.

## Detection signal

Codex supplies `session_id`, `transcript_path`, `cwd`, event name, and model to command hooks. The transcript format is not a stable public interface, so Session Guardian deliberately does not parse it. It uses only:

- transcript file size in bytes;
- the number of `UserPromptSubmit` events observed for the task;
- a one-shot manual rollover flag, when explicitly requested.

The default size policy is 64 MiB for warning, 96 MiB for rollover-required notification, and 128 MiB for the hard safety limit. The 96 MiB path also requires six observed prompts so that one large attachment does not immediately archive a new task. At 128 MiB, the submitted business request is locally blocked regardless of prompt count. The hook asks for the exact control prompt `继续交接` or `continue rollover`; only that explicit confirmation starts the final full-context rollover response.

Transcript byte size is the sole automatic risk signal. Session Guardian does not inspect Clash/Mihomo connections, proxy traffic, packets, or operating-system network counters. It measures a strong proxy for repeated context-upload cost rather than network traffic itself.

## Rollover transaction

The rollover is an ordered, failure-safe transaction performed by the current Codex turn:

1. The hook records `rollover_required` and locally blocks business prompts with a visible reason.
2. The user sends the explicit control prompt, authorizing task creation and archival.
3. The hook records `agent_rollover` and injects mandatory rollover instructions into that response.
4. Codex first tells the user that rollover is starting and the intercepted request will resume in the replacement.
5. Codex derives one compact handoff from context already loaded in that turn. No separate summary request is made.
6. Codex creates exactly one replacement with the app's task-management tools.
7. Codex waits until the replacement is ready, has acknowledged the handoff, and has accepted the intercepted request when one exists.
8. Codex leaves a known pinned original or an original with automatic archival disabled available.
9. Otherwise, the calling task archives itself only after all preceding steps succeed.

If task creation, setup, acknowledgement, or archival tooling fails, Codex reports the concrete error and the original remains available.

This ownership model is intentional. Codex Desktop holds an active writer on an open task. A detached second App Server can fork it, but cannot archive it and would also require another full-context summary request. The current task already owns the writer and has the host task-management tools, so it is the only safe place to commit the rollover.

## Recursion and concurrency controls

For a 96 MiB `Stop` trigger, the hook records that rollover is required and surfaces the confirmation instruction without starting another model response. For a 128 MiB prompt trigger, the hook uses the event-specific `block` decision and visible `reason`, so the business prompt never reaches the model. While rollover is pending, every non-confirmation prompt is blocked. The confirmation response records `agent_rollover`; its later `Stop` event is ignored to prevent recursion. A later confirmation retries the same protected path if the previous rollover did not archive the task.

## Privacy model

Monitoring never reads transcript contents. A prompt blocked during rollover enforcement is retained in private plugin state until the original task ends so it can resume in the replacement. No third-party endpoint, telemetry client, analytics SDK, detached App Server, or separate API credential is present. The generated handoff is sent directly into the replacement task and is not written into Session Guardian's state.

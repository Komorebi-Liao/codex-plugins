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
- a one-shot manual rollover flag, when explicitly requested.

The default size policy is one 64 MiB threshold. After the task reaches it, the synchronous `UserPromptSubmit` hook blocks the next submitted business request before Codex starts work. The hook asks for `是` or `yes`; the legacy prompts `继续交接` and `continue rollover` remain accepted. Only an explicit confirmation while rollover is pending starts the final full-context rollover response.

Transcript byte size is the sole automatic risk signal. Session Guardian does not inspect Clash/Mihomo connections, proxy traffic, packets, or operating-system network counters. It measures a strong proxy for repeated context-upload cost rather than network traffic itself.

## Rollover transaction

The rollover is an ordered, failure-safe transaction performed by the current Codex turn:

1. The hook records `rollover_required` and locally blocks business prompts with a visible reason.
2. The user sends the explicit control prompt, authorizing task creation and archival.
3. The hook records `agent_rollover` and injects mandatory rollover instructions into that response.
4. Codex first tells the user that rollover is starting and the intercepted request will resume in the replacement.
5. Codex derives one compact handoff from context already loaded in that turn. No separate summary request is made.
6. Codex creates exactly one replacement in the same saved project with the Local environment using the app's task-management tools.
7. Codex waits until the replacement is ready, has acknowledged the handoff, and has accepted the intercepted request when one exists.
8. Codex navigates to the ready replacement task with `navigate_to_codex_page` using its actual `threadId`, so the user lands in the resumed task rather than the generic new-conversation page.
9. Codex leaves a known pinned original or an original with automatic archival disabled available.
10. Otherwise, the calling task archives itself only after all preceding steps succeed.

If task creation, setup, acknowledgement, or archival tooling fails, Codex reports the concrete error and the original remains available.

This ownership model is intentional. Codex Desktop holds an active writer on an open task. A detached second App Server can fork it, but cannot archive it and would also require another full-context summary request. The current task already owns the writer and has the host task-management tools, so it is the only safe place to commit the rollover.

## Recursion and concurrency controls

At 64 MiB, the `UserPromptSubmit` hook uses the event-specific `block` decision and visible `reason`, so the business prompt never reaches the model. While rollover is pending, every non-confirmation prompt is blocked and the first intercepted request remains preserved. The confirmation response records `agent_rollover`; its later `Stop` event is ignored to prevent recursion. A later confirmation retries the same protected path if the previous rollover did not archive the task. The `Stop` hook remains only for explicitly armed manual rollover.

## Privacy model

Monitoring never reads transcript contents. A prompt blocked during rollover enforcement is retained in private plugin state until the original task ends so it can resume in the replacement. No third-party endpoint, telemetry client, analytics SDK, detached App Server, or separate API credential is present. The generated handoff is sent directly into the replacement task and is not written into Session Guardian's state.

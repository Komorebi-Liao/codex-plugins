---
name: session-rollover
description: Inspect, configure, force, or cancel Session Guardian rollover for oversized Codex tasks. Use when the user mentions long conversations, abnormal repeated upload traffic, context growth, starting a summarized replacement task, or archiving an old task after handoff. Do not use for ordinary task creation or archival unrelated to context size.
---

# Session rollover

Use the Session Guardian script for deterministic measurement and configuration, and use Codex app task-management tools for the rollover itself. Resolve the plugin root as three parent directories above this `SKILL.md`; its entrypoint is `scripts/session_guardian.py`.

## Choose the operation

- For inspection, run `python3 <plugin-root>/scripts/session_guardian.py status` and explain the current mode, threshold, recent rollover state, and whether Codex Desktop task tools were detected.
- To force rollover now, follow the rollover procedure below in the current turn. Use `arm --cwd "$PWD"` only when the user explicitly wants rollover deferred until the current turn tries to stop.
- To cancel a forced rollover that has not started, run `python3 <plugin-root>/scripts/session_guardian.py disarm --cwd "$PWD"`.
- To update settings, run `configure` with only the values requested by the user. Supported options are `--mode auto|warn|off`, `--threshold-mib`, `--archive-original yes|no`, and `--notifications yes|no`. `--warning-mib` remains a compatibility alias for `--threshold-mib`.
- For diagnostics, run `python3 <plugin-root>/scripts/session_guardian.py doctor`.

Do not manipulate transcript contents, plugin state JSON, Codex configuration, or task files directly. Do not start another `codex app-server`: Codex Desktop holds an active writer on the current task, so only the current task's app tools can archive it safely.

## Rollover procedure

Automatic rollover reaches this procedure only after Session Guardian has blocked an oversized-task request and the user explicitly replies `是` or `yes`. The legacy confirmations `继续交接` and `continue rollover` remain accepted. Treat the confirmation as a control prompt authorizing the transaction, not as a business request. The earlier business prompt was locally blocked and must not be executed in the current task.

1. Before calling any tool, send a concise commentary update stating that Session Guardian is preparing a compact replacement task. If a business prompt was intercepted, tell the user that it will resume in the replacement.
2. Build one concise handoff from context already available in this turn. Include the user goal, completed work, current state, decisions, changed files, verification, pending work, exact next step, constraints, and unresolved warnings. Exclude secrets, credentials, hidden reasoning, and redundant discussion. Do not make a second model request merely to summarize.
3. Use the Codex app project and task tools to create exactly one replacement task. List saved projects, identify the project backing the current task, and reuse that same saved project with the Local environment. Include the active working directory or actual code subdirectory in the replacement prompt. Do not add or register a project, initialize or repair Git, move files, change branches, or invent `main` or `master` during rollover. If the current saved project cannot be identified, report the mismatch and leave the current task unarchived. Keep the current model/settings unless the user requested an override.
4. The new task's initial prompt must identify itself as a Session Guardian handoff and contain the compact handoff. When the hook context includes an intercepted request, preserve it as user-provided content and instruct the replacement to acknowledge the handoff briefly, then execute that request immediately without asking the user to resend it. Otherwise, instruct the replacement to acknowledge and wait.
5. Wait for the replacement task to become ready and accept its initial work. If creation, setup, or acknowledgement fails or needs user input, report the concrete problem and leave the current task unarchived.
6. If the current task is known to be pinned, leave it unarchived and tell the user. If `archive_original` is disabled, also leave it unarchived.
7. Otherwise, tell the user the replacement is ready and that this task will now be archived, then archive the calling task with the Codex app archival tool. Target the calling task by omitting a task id; never guess an id.

Treat creation plus readiness as the transaction's prepare phase and archival as its commit. Never archive first. Never archive a partially created replacement instead of the original unless cleaning up a failed replacement is clearly safe.

## Interpret results

The default is one 64 MiB threshold. In `auto` mode, when a user submits a prompt after the task reaches that size, the synchronous `UserPromptSubmit` hook blocks it before Codex starts work, preserves the first blocked business request in private state, and asks the user to reply `是` or `yes`. Confirmation authorizes one final full-context rollover turn; the preserved request resumes automatically in the replacement task. In `warn` mode the request is allowed and a warning is shown. In `off` mode automatic detection is disabled.

The detector measures transcript byte size only. It is a proxy for repeated context-upload cost, not a network measurement. Do not add or imply Clash/Mihomo inspection, proxy monitoring, packet capture, or operating-system network-counter sampling. The transcript wire format is unstable, so the monitor intentionally does not parse its contents.

Rollover runs inside the current Codex turn so the same model request can both derive the handoff and use the app's task tools. This is the one final full-context request retained to preserve handoff quality. Starting a detached App Server would require another full-context request and cannot archive the desktop-owned original because of the active-writer lock.

Automatic rollover must leave the original task available whenever handoff generation, replacement-task creation, or acknowledgement fails. A pinned original is reported but not archived automatically.

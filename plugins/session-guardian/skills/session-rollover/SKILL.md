---
name: session-rollover
description: Inspect, configure, force, or cancel Session Guardian rollover for oversized Codex tasks. Use when the user mentions long conversations, abnormal repeated upload traffic, context growth, starting a summarized replacement task, or archiving an old task after handoff. Do not use for ordinary task creation or archival unrelated to context size.
---

# Session rollover

Use the Session Guardian script for deterministic measurement and configuration, and use Codex app task-management tools for the rollover itself. Resolve the plugin root as three parent directories above this `SKILL.md`; its entrypoint is `scripts/session_guardian.py`.

## Choose the operation

- For inspection, run `python3 <plugin-root>/scripts/session_guardian.py status` and explain the current mode, thresholds, recent rollover state, and whether Codex Desktop task tools were detected.
- To force rollover now, follow the rollover procedure below in the current turn. Use `arm --cwd "$PWD"` only when the user explicitly wants rollover deferred until the current turn tries to stop.
- To cancel a forced rollover that has not started, run `python3 <plugin-root>/scripts/session_guardian.py disarm --cwd "$PWD"`.
- To update settings, run `configure` with only the values requested by the user. Supported options are `--mode auto|warn|off`, `--warning-mib`, `--rollover-mib`, `--hard-limit-mib`, `--min-prompts`, `--archive-original yes|no`, and `--notifications yes|no`.
- For diagnostics, run `python3 <plugin-root>/scripts/session_guardian.py doctor`.

Do not manipulate transcript contents, plugin state JSON, Codex configuration, or task files directly. Do not start another `codex app-server`: Codex Desktop holds an active writer on the current task, so only the current task's app tools can archive it safely.

## Rollover procedure

Automatic rollover reaches this procedure only after the user explicitly sends `继续交接` or `continue rollover`. Treat that message as a control prompt authorizing the transaction, not as a business request. Any earlier hard-limit business prompt was locally blocked and must not be executed in the current task.

1. Before calling any tool, send a concise commentary update stating that Session Guardian is preparing a compact replacement task. If a business prompt was intercepted, tell the user that it will resume in the replacement.
2. Build one concise handoff from context already available in this turn. Include the user goal, completed work, current state, decisions, changed files, verification, pending work, exact next step, constraints, and unresolved warnings. Exclude secrets, credentials, hidden reasoning, and redundant discussion. Do not make a second model request merely to summarize.
3. Use the Codex app project and task tools to create exactly one replacement task. List saved projects and identify the repository that actually contains the active files or changes; do not infer it only from the task's outer workspace folder. Before selecting Worktree, verify the saved project itself has a commit with `git -C <project-path> rev-parse --verify HEAD^{commit}`, and verify any explicit starting ref at that same path. Never assume `main` or `master`. Prefer an exact saved-project/repository-root match. If the current saved project is an unborn repository, points at the wrong Git root, or has no verified starting ref, do not add a project or modify Git as a workaround: create the replacement in that same saved project using the Local environment and include the actual code subdirectory in its prompt. If no safe saved project can be selected, report the mismatch and leave the current task unarchived. Preserve the working tree when repository state matters, and keep the current model/settings unless the user requested an override.
4. The new task's initial prompt must identify itself as a Session Guardian handoff and contain the compact handoff. When the hook context includes an intercepted request, preserve it as user-provided content and instruct the replacement to acknowledge the handoff briefly, then execute that request immediately without asking the user to resend it. Otherwise, instruct the replacement to acknowledge and wait.
5. Wait for the replacement task to become ready and accept its initial work. If creation, setup, or acknowledgement fails or needs user input, report the concrete problem and leave the current task unarchived.
6. If the current task is known to be pinned, leave it unarchived and tell the user. If `archive_original` is disabled, also leave it unarchived.
7. Otherwise, tell the user the replacement is ready and that this task will now be archived, then archive the calling task with the Codex app archival tool. Target the calling task by omitting a task id; never guess an id.

Treat creation plus readiness as the transaction's prepare phase and archival as its commit. Never archive first. Never archive a partially created replacement instead of the original unless cleaning up a failed replacement is clearly safe.

## Interpret results

The defaults are 64 MiB for warning, 96 MiB for a rollover-required notice after a completed turn, and 128 MiB for the hard safety limit. The 96 MiB trigger also requires six observed prompts. Both paths require an explicit rollover control prompt. At the hard limit, the business request is blocked with a visible reason and resumes automatically in the replacement task after confirmation.

The detector measures transcript byte size plus observed prompt count. It is a proxy for repeated context-upload cost, not a network measurement. Do not add or imply Clash/Mihomo inspection, proxy monitoring, packet capture, or operating-system network-counter sampling. The transcript wire format is unstable, so the monitor intentionally does not parse its contents.

Rollover runs inside the current Codex turn so the same model request can both derive the handoff and use the app's task tools. This is the one final full-context request retained to preserve handoff quality. Starting a detached App Server would require another full-context request and cannot archive the desktop-owned original because of the active-writer lock.

Automatic rollover must leave the original task available whenever handoff generation, replacement-task creation, or acknowledgement fails. A pinned original is reported but not archived automatically.

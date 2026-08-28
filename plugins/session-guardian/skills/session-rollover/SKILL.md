---
name: session-rollover
description: Inspect, configure, force, or cancel Session Guardian rollover for oversized Codex tasks. Use when the user mentions long conversations, abnormal repeated upload traffic, context growth, starting a summarized replacement task, or archiving an old task after handoff. Do not use for ordinary task creation or archival unrelated to context size.
---

# Session rollover

Use the Session Guardian script in this plugin for deterministic state changes. Resolve the plugin root as three parent directories above this `SKILL.md`; its entrypoint is `scripts/session_guardian.py`.

## Choose the operation

- For inspection, run `python3 <plugin-root>/scripts/session_guardian.py status` and explain the current mode, thresholds, recent rollover state, and whether the Codex executable is available.
- To force the current task to roll over after this response, run `python3 <plugin-root>/scripts/session_guardian.py arm --cwd "$PWD"`. Tell the user that the current response will finish first and that the original is archived only after the replacement is ready.
- To cancel a forced rollover that has not started, run `python3 <plugin-root>/scripts/session_guardian.py disarm --cwd "$PWD"`.
- To update settings, run `configure` with only the values requested by the user. Supported options are `--mode auto|warn|off`, `--warning-mib`, `--rollover-mib`, `--hard-limit-mib`, `--min-prompts`, `--archive-original yes|no`, and `--notifications yes|no`.
- For diagnostics, run `python3 <plugin-root>/scripts/session_guardian.py doctor`.

Do not manipulate transcript contents, plugin state JSON, Codex configuration, or task files directly. Do not manually archive the current task as part of this skill: the worker owns the failure-safe order.

## Interpret results

The defaults are 64 MiB for warning, 96 MiB for automatic rollover after a completed turn, and 128 MiB for the hard safety limit. Normal automatic rollover also requires six observed prompts. At the hard limit, a new user prompt is blocked while rollover starts; the user must resend that prompt in the replacement task.

The detector measures transcript byte size plus observed prompt count. It is a proxy for repeated context-upload cost, not a network measurement. Do not add or imply Clash/Mihomo inspection, proxy monitoring, packet capture, or operating-system network-counter sampling. The transcript wire format is unstable, so the monitor intentionally does not parse its contents.

The original task intentionally generates the structured summary through the local Codex App Server. This is the one final full-context request, retained to preserve handoff quality, and the in-progress state allows it through the hard guard.

Automatic rollover must leave the original task available whenever summary generation, replacement-task creation, goal transfer, or acknowledgement fails. A pinned original is reported but not archived automatically.

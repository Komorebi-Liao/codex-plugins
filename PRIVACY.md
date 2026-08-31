# Privacy

Session Guardian processes task metadata locally on the device where Codex runs.

It reads only the lifecycle fields supplied by Codex hooks, including the current task identifier, working directory, model name, transcript file size, and a prompt blocked during rollover enforcement. It does not parse or retain transcript contents for monitoring. A blocked prompt is kept in private local state until the original task ends so the replacement can resume it. When rollover is triggered, the current Codex turn builds a compact handoff from context already loaded and uses Codex Desktop's task-management tools to create the replacement and archive the calling task after the replacement is ready.

Session Guardian does not operate an external server, add analytics, transmit telemetry, or require a separate API key. The user-confirmed Codex rollover response remains subject to the user's OpenAI account, workspace settings, and OpenAI data controls.

Session Guardian does not inspect Clash/Mihomo connections, proxy traffic, packets, or operating-system network counters.

Local state contains configuration, task identifiers, byte counts, timestamps, rollover status, and a pending blocked prompt when applicable. It does not store the generated handoff summary. Session state is removed when the task ends; all state can also be removed by uninstalling the plugin and deleting its plugin data directory.

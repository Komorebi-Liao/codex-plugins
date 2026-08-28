# Privacy

Session Guardian processes task metadata locally on the device where Codex runs.

It reads only the lifecycle fields supplied by Codex hooks, including the current task identifier, working directory, model name, and transcript file size. It does not parse or retain transcript contents for monitoring. When rollover is triggered, it asks the user's existing Codex installation to create a handoff summary and a replacement task through the local Codex App Server.

Session Guardian does not operate an external server, add analytics, transmit telemetry, or require a separate API key. The normal Codex model request used to create the summary remains subject to the user's OpenAI account, workspace settings, and OpenAI data controls.

Local state contains configuration, task identifiers, byte counts, timestamps, and rollover status. It does not store the generated handoff summary. State can be removed by uninstalling the plugin and deleting its plugin data directory.

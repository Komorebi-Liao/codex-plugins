# Codex Plugins

Open-source Codex plugins maintained in this repository.

## Session Guardian

Session Guardian watches the size of a Codex task locally. When a task becomes large enough to make every subsequent request expensive, it creates a compact handoff in a new task and archives the old task.

- Local lifecycle monitoring; no third-party analytics or telemetry.
- A single 64 MiB submission-time protection threshold.
- Failure-safe: the original task is archived only after the handoff task is ready.
- Manual rollover, warning-only mode, a configurable threshold, and desktop notifications.
- No API key and no MCP server required.

### Install from GitHub

```bash
codex plugin marketplace add Komorebi-Liao/codex-plugins --ref main
codex plugin add session-guardian@komorebi-codex-plugins
```

Restart Codex or start a new task after installation. Review and trust the bundled hooks when Codex prompts you, or open `/hooks` in the CLI.

By default, once a task reaches 64 MiB, the next submitted prompt is blocked locally before Codex
starts work. Session Guardian preserves that business request and asks the user to reply `yes` or
`是`. The confirmation authorizes one final full-context response that creates a compact replacement
in the same saved project, resumes the preserved request there, and archives the original only after
the replacement is ready. Codex then navigates to the ready replacement task so the user lands in the
resumed session. Native desktop notifications are used when supported.

Transcript file size is the sole automatic risk signal. Session Guardian does not inspect Clash/Mihomo connections, proxy traffic, packets, or operating-system network counters.

After an interception, reply `是` or `yes` (`继续交接` and `continue rollover` remain accepted). That explicit control prompt
authorizes one final full-context Codex response. It announces the rollover first, derives one compact
handoff from context already loaded, creates and verifies the replacement with the app's task tools,
and only then archives the calling task. Codex Desktop keeps an active writer on an open task, so this
current-task transaction avoids both the write-lock failure and a second large summary request. The
intercepted business request resumes in the replacement and the UI navigates to that task. A failed
rollover leaves the original available.

Invoke `$session-rollover` to inspect, configure, force, or cancel a rollover. See the [Chinese guide](docs/README.zh-CN.md) and [architecture notes](docs/architecture.md) for details.

### Local development

```bash
codex plugin marketplace add .
codex plugin add session-guardian@komorebi-codex-plugins
python3 -m unittest discover -s plugins/session-guardian/tests -v
```

Validate the package with the built-in `plugin-creator` and `skill-creator` validators before release.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

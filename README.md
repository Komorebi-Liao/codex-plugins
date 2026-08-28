# Codex Plugins

Open-source Codex plugins maintained in this repository.

## Session Guardian

Session Guardian watches the size of a Codex task locally. When a task becomes large enough to make every subsequent request expensive, it creates a compact handoff in a new task and archives the old task.

- Local lifecycle monitoring; no third-party analytics or telemetry.
- Automatic rollover only after both size and prompt-count thresholds are reached.
- Failure-safe: the original task is archived only after the handoff task is ready.
- Manual rollover, warning-only mode, configurable thresholds, and desktop notifications.
- No API key and no MCP server required.

### Install from GitHub

```bash
codex plugin marketplace add Komorebi-Liao/codex-plugins --ref main
codex plugin add session-guardian@komorebi-codex-plugins
```

Restart Codex or start a new task after installation. Review and trust the bundled hooks when Codex prompts you, or open `/hooks` in the CLI.

The defaults are:

- warning at 8 MiB;
- automatic rollover at 16 MiB;
- at least 6 submitted prompts before automatic rollover;
- archive the original only after the new task is seeded successfully;
- native desktop notifications when supported.

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

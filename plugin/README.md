# Atlas ADE

Sets up Atlas ADE, the meta-harness for your agentic workflows, in your project.

ADE runs Claude Code, Codex, OpenCode or Pi on your repo one small task (a card) at a time: its own git worktree, a separate reviewer, tests, then merge. Shows what each card cost, so your usage goes on building. Free, MIT, no account.

This plugin holds one skill, `get-started`. Ask Claude to set up ADE, and the skill checks for the `ade` command, installs it with your OK, runs `ade init`, and hands over to the guide that ADE writes into your project.

## Install

```text
/plugin install atlas-ade --marketplace The-Anthropocene/ade
```

From a shell, the same line starts with `claude plugin install`.

## What it runs

- `ade --version`, to see whether ADE is already installed.
- `uv tool install atlas-ade` (or `pipx install atlas-ade`), only after asking you. This fetches the atlas-ade package from PyPI.
- `ade init --dry-run` in a repository you already have, so you see what would change before anything is written.
- `ade init`, which writes `ade.toml`, ADE's hooks and a first graph of your code, and commits them. It also rebuilds the allow and deny lists in `.claude/settings.json` from `ade.toml`. In a repository you already have, the dry run names every rule that would go, and Claude asks you which to keep before anything is written.
- `ade rehearse`, if you want a full loop that spends no usage.

The plugin has no hooks, no server and no scripts, and it sends nothing anywhere. ADE itself needs no account, and nothing phones home.

## Try it

- "Set up Atlas ADE in this repo."
- "Install ADE in a new folder and walk me through the setup."
- "Show me an ADE rehearsal before I spend anything."

## Help

Install page: https://atlas-ade.dev/docs/install/
Source and issues: https://github.com/The-Anthropocene/ade
Email: support@atlas-ade.dev
Licence: MIT

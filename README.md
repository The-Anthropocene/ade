# ade

A small agentic meta-harness, so that your usage goes on building.

Atlas ADE drives Claude Code, Codex, OpenCode or Pi through one loop per card (lane, build, review, test, land), keeps a generated graph of your code, and records what every card cost.

<!-- GIF: 20 to 30 seconds, under 1 MB, recorded by the demo thread -->

```sh
uv tool install git+https://github.com/The-Anthropocene/ade    # or: pipx install git+https://github.com/The-Anthropocene/ade
```

```sh
ade init                                          # in your repo: settings, hooks, a first graph
ade card add "say hello" --path hello.py --done "it prints hello"
ade loop c1                                       # lane, build, review, test, land
```

You need git and one of Claude Code, Codex, OpenCode or Pi, installed and signed in. To try it without spending anything, run `ade rehearse`.

No account, no dependencies, nothing phones home. Python 3.11+ and the standard library.

ADE runs each vendor's own unmodified CLI under your login. It never reads or stores that login, and it can run on an API key instead.

## Where this came from

ADE was built in private first, by Connor Roberts at Practice Atlas Ltd, using ADE itself. Public history starts here, and from now on every landed card is one public commit.

## More

- Docs: https://atlas-ade.dev/docs/ (install, quickstart, the verbs)
- How the loop works: [ARCHITECTURE.md](ARCHITECTURE.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Licence: MIT, see [LICENSE](LICENSE). The Atlas ADE name and logo are not part of the licence.

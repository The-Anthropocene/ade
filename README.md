# ade

A small agentic meta-harness, so that your usage goes on building.

For anyone already building a real project with an agent who keeps hitting usage limits and losing context.

Atlas ADE drives Claude Code, Codex, OpenCode or Pi through one loop per card (lane, build, review, test, land), keeps a generated graph of your code, and records what every card cost.

![You ask in Claude Code; your pilot cards it and flies it to landed with ade](https://raw.githubusercontent.com/The-Anthropocene/ade/main/demo.gif)

<table>
<tr>
<td width="40%"><b>Watch every card fly.</b> A band above the Claude Code prompt ticks each card through lane, build, review, test and land.</td>
<td><img src="https://raw.githubusercontent.com/The-Anthropocene/ade/main/assets/flight-band.gif" alt="flight-band"></td>
</tr>
<tr>
<td width="40%"><b>A cockpit beside your agent.</b> <code>ade cockpit</code> opens next to Claude Code and shows your open cards and the decisions waiting on you.</td>
<td><img src="https://raw.githubusercontent.com/The-Anthropocene/ade/main/assets/cockpit.gif" alt="cockpit"></td>
</tr>
<tr>
<td width="40%"><b>Works where you already work.</b> Run your pilot inside Orca or herdr, and ADE's status line comes with it.</td>
<td><img src="https://raw.githubusercontent.com/The-Anthropocene/ade/main/assets/orca.gif" alt="orca"></td>
</tr>
</table>

![How a card flies through ADE: you set the work, the pilot (the agent driving ADE) files cards, and the conductor flies each card through lane, build, review, test, land and push, with six hooks on every agent call](https://atlas-ade.dev/assets/flight-deck.png)

```sh
uv tool install atlas-ade    # or: pipx install atlas-ade
```

```sh
ade init                                          # in your repo: settings, hooks, a first graph
ade card add "say hello" --path hello.py --done "it prints hello"   # prints the card's id
ade loop c1                                       # that id: lane, build, review, test, land
```

In an empty folder, `ade init` writes a starter card c1 of its own, so your "say hello" card is c2 there.

You need git and one of Claude Code, Codex, OpenCode or Pi, installed and signed in. To try it without spending anything, run `ade rehearse`.

By default each landing pushes your trunk and an `ade-records` branch (cards and decisions) to `origin`. On a shared or protected repo, set `[git] push = "pr"` in ade.toml to get a pull request instead, or `"manual"` to push only when you run `ade push`.

No account, no dependencies, nothing phones home. Python 3.11+ and the standard library.

ADE runs each vendor's own unmodified CLI under your login. It never reads or stores that login, and it can run on an API key instead.

## What makes it ADE

- **Cards, not chats.** Each change is a card with its files and a done line. ade card add writes one; `ade cockpit` shows them all.
- **One loop per card.** Lane, build, review, test, land, each card in its own worktree. A separate reviewer seat can send the work back.
- **Built for long-horizon work.** Scripts run the routine steps, so model turns are spent on building, reviewing and the stops that need judgement. The same stop twice goes to you, so a model never loops on its own mistake.
- **A graph of your code.** Each lane gets a briefing on just the files its card touches, so agents stop re-reading the whole repo.
- **Usage per card.** `ade telemetry` shows what each card cost, per engine, every week.
- **Small on purpose.** The core is capped at 20,000 lines and 8 hooks, and `ade budget` prints where it stands. Small cards keep each diff, review and fix small.

## Where this came from

ADE was built in private first, by Connor Roberts at Practice Atlas Ltd, using ADE itself. Public history starts here, and each release lands as one public commit.

## More

- Any issues? [Open an issue on GitHub](https://github.com/The-Anthropocene/ade/issues), or email support@atlas-ade.dev.
- Work with me: I can set ADE up in your repo, port your existing agent workflow onto it, or work contract days on agent tooling. Email support@atlas-ade.dev.
- Docs: https://atlas-ade.dev/docs/ (install, quickstart, the verbs)
- How the loop works: [ARCHITECTURE.md](https://github.com/The-Anthropocene/ade/blob/main/ARCHITECTURE.md)
- Contributing: [CONTRIBUTING.md](https://github.com/The-Anthropocene/ade/blob/main/CONTRIBUTING.md)
- Licence: MIT, see [LICENSE](https://github.com/The-Anthropocene/ade/blob/main/LICENSE). The Atlas ADE name and logo are not part of the licence.

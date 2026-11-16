# id: ade.lib.agent
# what: tell whether a command runs inside a coding agent, from the engine's own environment markers
# cmd: -
# reads: the process environment, whether stdin is a terminal
# writes: -
import os

# Most specific engine first: nested engines inherit the outer one's markers. Exact names only, never a CLAUDE_CODE_*
# prefix (Codex runs commands through a login shell, so profile variables leak in) and never a bare AGENT.
# This catches the careless case, not a determined agent: it can unset a variable (docs/gaps.md).
MARKERS = (("codex", ("CODEX_THREAD_ID", "CODEX_SESSION_ID")), ("opencode", ("OPENCODE",)),
           ("claude-code", ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")))


def answerer(env=None):
    """{"by": "agent", "engine": ...} when an engine marker is set, else {"by": "person"} at a terminal, else "unknown"."""
    env = os.environ if env is None else env
    engine = next((e for e, names in MARKERS if any(env.get(n) for n in names)), None)
    return {"by": "agent", "engine": engine} if engine else {"by": "person" if os.isatty(0) else "unknown"}

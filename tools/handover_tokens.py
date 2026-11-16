# id: tools.handover_tokens
# what: each role's handover in real tokens: one provider call with the handover and one without; the difference
# cmd: python3 tools/handover_tokens.py [--provider claude|codex] [REPO]   (spends two small calls per role)
# reads: REPO's ade.toml and records (through ade handover), the provider's own usage report
# writes: a temporary empty git repository, removed afterwards
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ade import config, host, providers, telemetry  # noqa: E402
from ade.verbs import handover  # noqa: E402

ASK = "Reply with the single word ok."


def tokens(ctx, name, prompt, cwd):
    # The provider's own count is the real tokenizer; len // 4 is only ade handover's estimate.
    spec, model = ctx.cfg["providers"][name], ctx.cfg["roles"]["builder"]["models"][name]
    mode = ctx.cfg["permissions"]["lane_mode"]
    argv = [a.replace("{prompt}", prompt).replace("{model}", model).replace("{mode}", mode) for a in spec["cmd"]]
    r = subprocess.run(host.which(argv), cwd=cwd, env=providers._env(spec), capture_output=True, text=True,
                       timeout=spec.get("timeout", 600))
    _, u = telemetry.parse(r.stdout)
    if "tokens_in" not in u:
        sys.exit(f"{name} reported no usage (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    # Claude counts cache reads and writes apart from input_tokens; Codex's input_tokens already includes its cache.
    extra = u.get("cache_read", 0) + u.get("cache_write", 0) if name == "claude" else 0
    return u["tokens_in"] + extra


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--provider", default="claude", choices=["claude", "codex"])
    p.add_argument("repo", nargs="?", default=".")
    args = p.parse_args()
    ctx = config.load(args.repo)
    with tempfile.TemporaryDirectory() as empty:
        # An empty repository: no CLAUDE.md or AGENTS.md is read, and codex exec runs only inside a git repo.
        subprocess.run(["git", "init", "-q", empty], check=True)
        base = tokens(ctx, args.provider, ASK, empty)
        for role in ctx.cfg["roles"]:
            body = handover.text(ctx, role)
            real = tokens(ctx, args.provider, f"{body}\n\n{ASK}", empty) - base
            print(f"{role:<10} {real:>7} tokens ({args.provider})   estimate {len(body) // 4:>7}   "
                  f"budget {ctx.cfg['budgets']['handover_tokens']}")


if __name__ == "__main__":
    main()

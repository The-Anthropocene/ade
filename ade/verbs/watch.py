# id: ade.verbs.watch
# what: follow a card's lane log live (any platform, no tmux) until its loop stops or the log goes quiet; --once prints it
# cmd: ade watch CARD [--once | --until-landed]
# reads: .ade/lanes/<card>.log, .ade/records.jsonl journal rows
# writes: -
import json
import sys
import time
from datetime import datetime, timezone

from ade import config, records, stages, wait


def add(p):
    p.add_argument("card")
    p.add_argument("--once", action="store_true", help="print the log so far and exit")
    p.add_argument("--until-landed", action="store_true", help="keep following across stops until the card lands")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between looks")


def _hint(inp):
    # A short hint of a tool call's input: its first string value, one line.
    v = next((x for x in inp.values() if isinstance(x, str)), "") if isinstance(inp, dict) else ""
    return " ".join(v.split())[:80]


def _lines(o):
    # What an engine's stream-json row says, as plain lines; rows that say nothing (hooks, usage, tool output) give none.
    out = []
    kind = o.get("type")
    if kind == "result" and isinstance(o.get("result"), str):
        out.append(o["result"])
    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
    if kind == "assistant" or (kind == "message_end" and msg.get("role") == "assistant"):  # claude, Pi
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                out.append(c.get("text", ""))
            elif isinstance(c, dict) and c.get("type") in ("tool_use", "toolCall"):
                out.append(f"[{c.get('name', 'tool')}] {_hint(c.get('input') or c.get('arguments'))}".rstrip())
    for node in (o.get("item"), o.get("part")):  # codex items, opencode parts
        if isinstance(node, dict):
            if node.get("type") in ("agent_message", "text") and isinstance(node.get("text"), str):
                out.append(node["text"])
            elif node.get("type") == "command_execution":
                out.append(f"[shell] {_hint({'c': node.get('command')})}")
            elif node.get("type") == "tool" and kind != "item.updated":
                out.append(f"[{node.get('tool', 'tool')}] {_hint((node.get('state') or {}).get('input'))}".rstrip())
    return out


def _render(line, last):
    # One log line as readable lines: JSON rows become their words, anything else passes through.
    # `last` holds the final assistant text seen; claude repeats it as the result row, which is dropped.
    if not line.lstrip().startswith("{"):
        return [line]
    try:
        o = json.loads(line)
    except ValueError:
        return [line]
    if not isinstance(o, dict):
        return [line]
    out = [t.rstrip("\n") for t in _lines(o) if t.strip()]
    if o.get("type") == "result" and out and out == last[-1:]:
        return []
    if o.get("type") == "assistant" and out:
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        texts = [c.get("text", "") for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        last[:] = [t.rstrip("\n") for t in texts if t.strip()][-1:]
    return out


def _stopped(ctx, card_id, since):
    # The loop journals every stage; any row after we began that is not an advance means it stopped.
    rows = [r for r in records.read(ctx, "journal") if r["card"] == card_id and r["at"] >= since]
    return any(r["outcome"] != "advance" for r in rows)


def run(args, cwd):
    ctx = config.load(cwd)
    c = stages.card(ctx, args.card)
    log = ctx.state / "lanes" / f"{c['id']}.log"
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pos = 0
    last = []  # the last assistant words printed
    rest = b""  # the unfinished last line of what was read
    done = records.merged(ctx, "card")[c["id"]]
    if done["status"] == "landed" and not log.is_file():
        print(f"{c['id']} landed at {done.get('commit', '')[:10]}; its log is .ade/logs/{c['id']}.log")
        return 0
    while True:
        if log.is_file():
            with open(log, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            pos += len(chunk)
            *whole, rest = (rest + chunk).split(b"\n")
            sys.stdout.write("".join(t + "\n" for b in whole for t in _render(b.decode("utf-8", "replace"), last)))
            sys.stdout.flush()
        landed = records.merged(ctx, "card")[c["id"]]["status"] == "landed"
        end = args.once or landed or (not args.until_landed and _stopped(ctx, c["id"], since))
        if end and rest:  # a last line the engine never ended
            sys.stdout.write("".join(t + "\n" for t in _render(rest.decode("utf-8", "replace"), last)))
            rest = b""
        if end:
            return 0
        if pos and wait.stalled(ctx, c["id"], wait.seconds(f"{ctx.cfg['watch']['stall']}m")):
            # Silence from a running engine is a reading, not peace.
            print(f"\nstalled: {c['id']}'s lane log has not grown for {ctx.cfg['watch']['stall']} minutes")
            return wait.STALLED
        try:
            time.sleep(args.poll)
        except KeyboardInterrupt:
            return 0

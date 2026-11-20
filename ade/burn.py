# id: ade.burn
# what: the 7-day burn as one command: this machine's Claude Code and Codex token use, per day and by surface, thread, model and class, priced in meter points
# cmd: ade telemetry --week [--json] [--since ISO]
# reads: $CLAUDE_CONFIG_DIR or ~/.claude projects/**/*.jsonl (usage, model, timestamp, entrypoint, isSidechain only), $CODEX_HOME or ~/.codex sessions token counts, .ade/records.jsonl usage rows and the machine lanes.jsonl (lane labels), ade/meter.json
# writes: -
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ade import records, telemetry
from ade.refuse import refuse

SURFACE = {"cli": "terminal", "claude-desktop": "desktop", "sdk-cli": "headless/SDK", "sdk-ts": "headless/SDK", "sdk-py": "headless/SDK"}
BLIND = "not visible here: cloud sessions, claude.ai, other machines"


def _lines(root, pattern, since=None):
    # Every line of every matching file, unreadable files skipped (and, given `since`, files last written before it); the caller keeps only the fields it needs.
    for p in sorted(root.glob(pattern)):
        try:
            if since and datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < since:
                continue
            yield from p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue


def _n(u, k):
    # One count under whichever of the provider's names for it is present (Codex says cached_input_tokens).
    return next((u[n] for n in telemetry.NAMES[k] if u.get(n)), 0)


def _json(line, *needles):
    if all(n in line for n in needles):
        try:
            r = json.loads(line)
        except ValueError:
            return None
        return r if isinstance(r, dict) else None


def rows(ctx, since, info=None):
    """One dict per API message (Claude: once per message id and request id) or Codex token delta since `since`."""
    cfg = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    lanes, nrepos = telemetry.lane_calls(ctx)
    seen, out = {}, []
    if info is not None:
        info["lane_repos"] = nrepos
    for line in _lines(cfg, "projects/**/*.jsonl", since):
        r = _json(line, '"usage"')
        m = (r or {}).get("message")
        at = telemetry._when((r or {}).get("timestamp"))
        if not (r and r.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict) and at and at >= since):
            continue
        key = (m.get("id"), r.get("requestId"))
        u = m["usage"]
        if key in seen:  # one message is written as several lines; the first may hold a partial count, so keep each field's max
            seen[key].update({k: max(seen[key][k], _n(u, k)) for k in telemetry.KINDS})
            continue
        lane = any(end - timedelta(seconds=w + 5) <= at <= end + timedelta(seconds=5) for end, w in lanes if end)
        seen[key] = {"day": at.astimezone(timezone.utc).date().isoformat(), "model": str(m.get("model", "?")),
                     "surface": "ADE lanes" if lane else SURFACE.get(r.get("entrypoint"), "other"),
                     "thread": "subagent" if r.get("isSidechain") else "main", "priced": True,
                     **{k: _n(u, k) for k in telemetry.KINDS}}
        out.append(seen[key])
    home = Path(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))
    last = {}
    for f in sorted((home / "sessions").glob("*/*/*/rollout-*.jsonl")):
        prev = defaultdict(int)
        for line in _lines(f.parent, f.name):
            r = _json(line, '"token_count"')
            p = (r or {}).get("payload") or {}
            tot = (p.get("info") or {}).get("total_token_usage")
            at = telemetry._when(r.get("timestamp")) if r else None
            if not (isinstance(tot, dict) and p.get("type") == "token_count" and at):
                continue
            now = {k: _n(tot, k) for k in telemetry.KINDS}
            d = {k: now[k] - prev[k] for k in now}
            prev.update(now)
            d["tokens_in"] -= d["cache_read"]  # codex counts cached tokens inside input
            if at >= since and min(d.values()) >= 0 and any(d.values()):
                out.append({"day": at.astimezone(timezone.utc).date().isoformat(), "model": "codex", "surface": "codex",
                            "thread": "main", "priced": False, **d})
    return out


def week(ctx, since=None):
    """The burn as plain data: the same dict is printed and dumped as JSON."""
    now = datetime.now(timezone.utc)
    reset = telemetry._reading(ctx, "claude")[3]
    start = since or (telemetry._when(reset) - timedelta(days=7) if reset and telemetry._when(reset) else None) or now - timedelta(days=7)
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    info = {}
    m, data = telemetry._model(ctx), rows(ctx, start, info)
    days = max((now - start).total_seconds() / 86400, 1)
    for r in data:
        r["tokens"] = sum(r[k] for k in telemetry.KINDS)
        r["points"] = round(telemetry.points(ctx, r, m) * 100, 3) if r["priced"] else 0.0
    def by(key):
        t = defaultdict(lambda: dict.fromkeys(("tokens", "points", *telemetry.KINDS), 0))
        for r in data:
            for k in ("tokens", "points", *telemetry.KINDS):
                t[r[key]][k] += r[k]
        return {n: {k: round(v, 3) for k, v in sorted(t[n].items())} for n in sorted(t)}
    cards = {c: {"calls": t["calls"], "tokens": sum(t[k] for k in telemetry.KINDS), "wall_s": round(t["wall_s"], 1)}
             for c, t in sorted(telemetry.totals(ctx, since=start)[0].items(), key=lambda x: -sum(x[1][k] for k in telemetry.KINDS))}
    total = {k: round(sum(r[k] for r in data), 3) for k in ("tokens", "points", *telemetry.KINDS)}
    meters = {n: {"state": s, "week": w, "five_hour": f} for n in ("claude", "codex") for s, w, f in [telemetry.reading(ctx, n)] if s != "none"}
    cl = meters.get("claude", {})
    gap = cl.get("week") if cl.get("state") == "ok" else None
    rs = telemetry._when(reset) if reset else None
    wk = rs - timedelta(days=7) if rs else None  # the current meter week starts here; the gap never reaches before it
    span = bool(since) and wk is None
    if span:
        gap = None
    gap_rows = [r for r in data if wk is None or r["day"] >= wk.astimezone(timezone.utc).date().isoformat()]
    from_week = wk.date().isoformat() if wk and wk > start else None
    seen = max((r["seen"] for r in telemetry.readings(ctx) if r["provider"] == "claude" and "week" in r), default=None)
    return {"since": start.isoformat(timespec="seconds"), "days": round(days, 2), "total": total,
            "per_day_average": {k: round(v / days, 3) for k, v in total.items()},
            "by_day": by("day"), "by_surface": by("surface"), "by_thread": by("thread"), "by_model": by("model"), "by_card": cards,
            "by_class": {k: total[k] for k in telemetry.KINDS}, "meters": meters,
            "meter_seen": seen if cl and cl["state"] != "ok" else None,
            "priced_vs_meter_points": None if gap is None else round(gap * 100 - sum(r["points"] for r in gap_rows), 3),
            "gap_since": from_week, "gap_may_span_reset": span,
            "lane_repos": info["lane_repos"], "unit": "points are priced by ade/meter.json for Claude only; Codex shows tokens", "blind": BLIND}


def show(d):
    t, a = d["total"], d["per_day_average"]
    print(f"since {d['since'][:10]} ({d['days']} days): {t['tokens']} tokens, {t['points']:.1f} points; per day {a['tokens']:.0f} tokens, {a['points']:.1f} points")
    for title, key in (("day", "by_day"), ("surface", "by_surface"), ("thread", "by_thread"), ("model", "by_model")):
        print(f"by {title}:")
        for n, v in d[key].items():
            print(f"  {n:<22} {v['tokens']:>12} tokens {v['points']:>8.1f} points")
    print("by card (ADE lane calls only):")
    for c, v in d["by_card"].items():
        print(f"  {c:<22} {v['calls']:>5} calls {v['tokens']:>12} tokens {v['wall_s'] / 60:>8.1f} min")
    print(f"lanes from {d['lane_repos']} repo{'s' * (d['lane_repos'] != 1)} on this machine")
    print("by class: " + ", ".join(f"{k[7:] if k.startswith('tokens_') else k} {v}" for k, v in d["by_class"].items()))
    for n, v in d["meters"].items():
        print(f"{n} meter: {v['state']}, week {'?' if v['week'] is None else format(v['week'], '.0%')}" + (", no five-hour window reported" if v["five_hour"] is None else f", 5h {v['five_hour']:.0%}"))
    if d["priced_vs_meter_points"] is not None:
        print(f"meter minus priced burn: {d['priced_vs_meter_points']:.1f} points" + (f" (only the week since {d['gap_since']})" if d.get("gap_since") else ""))
    elif d.get("gap_may_span_reset"):
        print("no meter-minus-priced gap: the reset is unknown, so this window may span a reset")
    elif "claude" in d["meters"]:
        if d["meters"]["claude"]["state"] == "reset":
            print("no fresh meter reading since the week reset")
        else:
            print(f"no fresh meter reading since {d['meter_seen']}")
    print(d["blind"])


def run(ctx, args):
    if args.since and not args.week or args.json and not args.week:
        refuse("burn-args", "--json and --since go with --week", "run ade telemetry --week [--json] [--since ISO]")
    d = week(ctx, args.since)
    print(json.dumps(d, indent=1, sort_keys=True)) if args.json else show(d)

# id: ade.telemetry
# what: local-only usage: parse each provider call's own report, week meters against the share, OpenTelemetry-shaped export
# cmd: -
# reads: provider stdout, .ade/records.jsonl usage rows, the per-user ade/meters.jsonl and lanes.jsonl (and an older $ADE_WORK/meters.jsonl), ade.toml [usage], ade/meter.json (or [usage] meter_model), codex's own session files (rate_limits only)
# writes: the per-user ade/meters.jsonl and lanes.jsonl (time, length, provider, repo name) in the machine config folder (one meter file per machine, whatever ADE_WORK is), .ade/telemetry/{traces,metrics}.json (both stay local); share rows in .ade/records.jsonl (a yes/no per provider, the one meter fact pushed with ade-records)
import contextlib
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

from ade import firstrun, host, records

METER = Path(__file__).with_name("meter.json")
# Weekly size per plan in Pro-weeks (pro 1, max5x 5, max20x 10), from u/flobernd's cost model (phoebis, MIT).
PLANS = {"max20x": 10, "max5x": 5, "pro": 1}
KINDS = ("tokens_in", "tokens_out", "cache_read", "cache_write")
# Each provider's own names for the same four counts; the first one present wins.
NAMES = {"tokens_in": ("input_tokens",), "tokens_out": ("output_tokens",),
         "cache_read": ("cache_read_input_tokens", "cached_input_tokens"),
         "cache_write": ("cache_creation_input_tokens",)}


def _objects(stdout):
    # Claude prints one JSON object; Codex, opencode and stream-json print JSON lines; else plain text, no report.
    try:
        one = json.loads(stdout)
        return [one] if isinstance(one, dict) else []
    except ValueError:
        out = []
        for line in stdout.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


def _walk(obj):
    yield obj
    for v in obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ():
        if isinstance(v, (dict, list)):
            yield from _walk(v)


def _when(value):
    # A reset is epoch seconds or an ISO string (no zone: UTC); unreadable is None, so a meter turns "unknown", not an error.
    try:
        dt = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _week(limits):
    # The longest window a provider reports is its week; used_percent is 0 to 100.
    wins = [w for w in limits.values() if isinstance(w, dict) and "used_percent" in w]
    if not wins: return None, None
    w = max(wins, key=lambda w: w.get("window_minutes", 0))
    at = _when(w["resets_at"]) if "resets_at" in w else None
    if at is None and isinstance(w.get("resets_in_seconds"), (int, float)):
        at = datetime.now(timezone.utc) + timedelta(seconds=w["resets_in_seconds"])
    return w["used_percent"] / 100, at.isoformat(timespec="seconds") if at else None


def parse(stdout):
    """(the call's answer text, its usage fields). A provider that reports nothing gives the raw text and no counts,
    never zeros: an instrument that reads 0 over nothing is lying.
    Contract: with no report found, the answer is the very `stdout` object passed in (same object, so `is` works), never a copy."""
    objs = _objects(stdout)
    if not objs:
        return stdout, {}
    text, usage, fields, steps = stdout, {}, {}, []
    for obj in objs:
        if isinstance(obj.get("result"), str):  # claude
            text = obj["result"]
        for node in (obj, obj.get("item"), obj.get("part")):  # the model the provider says it served (claude's init event)
            if isinstance(node, dict) and isinstance(node.get("model"), str) and node["model"]:
                fields["served_model"] = node["model"]
        item = obj.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]  # codex: the last agent message is the answer
        part = obj.get("part") or {}
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            text = part["text"]  # opencode: the last text part is the answer
        if isinstance(part, dict) and part.get("type") == "step-finish" and isinstance(part.get("tokens"), dict):
            steps.append(part)
        if obj.get("type") == "message_end" and isinstance(msg := obj.get("message"), dict) and msg.get("role") == "assistant":  # Pi: one per turn
            u = msg["usage"] if isinstance(msg.get("usage"), dict) else {}  # agent_end repeats every turn, so it is not read
            steps.append({"cost": (u.get("cost") or {}).get("total", 0), "tokens": {**u, "cache": {"read": u.get("cacheRead", 0), "write": u.get("cacheWrite", 0)}}})
            text = "".join(c.get("text", "") for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")
            fields["served_model"] = msg.get("model") if isinstance(msg.get("model"), str) and msg["model"] else fields.get("served_model")
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]  # the last report is the call's total (a stream's assistant events nest theirs in message)
        if isinstance(obj.get("permission_denials"), list) and obj["permission_denials"]:
            names = sorted({d["tool_name"] for d in obj["permission_denials"]
                            if isinstance(d, dict) and isinstance(d.get("tool_name"), str)})
            fields["denied"] = names or None
        for key, field in (("total_cost_usd", "cost"), ("num_turns", "turns")):
            if isinstance(obj.get(key), (int, float)):
                fields[field] = obj[key]
        for node in _walk(obj):
            if isinstance(node, dict) and isinstance(node.get("rate_limits"), dict):
                fields["week_used"], fields["week_resets"] = _week(node["rate_limits"])
        uw = (obj.get("rate_limit_info") or {}).get("unifiedWindows") if obj.get("type") == "rate_limit_event" else None
        if isinstance(uw, dict):  # claude's own weekly meter, in every stream-json call; utilization is a fraction
            fields["limits"] = {k: {"used_percent": w["utilization"] * 100, "resets_at": w.get("resetsAt")}
                                for k, w in uw.items() if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float))}
    for kind, names in NAMES.items():  # the first name a provider reports wins, so the update runs last-to-first
        fields.update({kind: usage[n] for n in reversed(names) if isinstance(usage.get(n), int)})
    cc = usage.get("cache_creation")  # claude: the part of cache_write billed at the 1-hour rate
    fields["cache_write_1h"] = cc.get("ephemeral_1h_input_tokens") if isinstance(cc, dict) else None
    if steps:  # opencode and Pi report each step or turn on its own: the call's total is their sum; input excludes cache
        t = [(s["tokens"], s["tokens"].get("cache") or {}) for s in steps]
        fields.update(tokens_in=sum(a.get("input", 0) for a, _ in t), tokens_out=sum(a.get("output", 0) for a, _ in t),
                      cache_read=sum(c.get("read", 0) for _, c in t), cache_write=sum(c.get("write", 0) for _, c in t),
                      turns=len(steps))
        if all(isinstance(s.get("cost"), (int, float)) for s in steps):
            fields["cost"] = round(sum(s["cost"] for s in steps), 6)
    return text, {k: v for k, v in fields.items() if v is not None}


def _model(ctx):
    custom = ctx.cfg.get("usage", {}).get("meter_model")
    return json.loads(host.read(ctx.root / custom if custom else METER))


def points(ctx, row, m=None):
    """A usage row's share of the week (params points / 100) by the meter model's formula, its default set, the row's
    model weight and the plan's weekly size in Pro-weeks relative to Max 20x. `m` is an optional preloaded _model(ctx)."""
    m = m or _model(ctx)
    s = next(v for v in m["sets"].values() if v.get("default"))
    name = str(row.get("model", "")).lower()
    key = next((k for k in ("sonnet", "haiku", "fable") if k in name), "opus")
    weight = s["fableWeight"] if key == "fable" else m["weights"][key]["value"]
    i, o, r, w = (row.get(k, 0) for k in ("tokens_in", "tokens_out", "cache_read", "cache_write"))
    total, cold = i + r + w, m["coldWrite"]
    is_cold = w >= cold["fractionOfContext"] * total and total >= cold["minContextTokens"]
    p = m["prices"]
    write = p["coldWrite"] * (w if is_cold else w * s["warmWriteFactor"])
    raw = weight * (s["A"] * (p["in"] * i + p["out"] * o) + s["B"] * (p["cacheRead"] * r + write)) / 1e6
    return raw / 100 * PLANS["max20x"] / PLANS[ctx.cfg.get("usage", {}).get("plan", "max20x")]


def record(ctx, card, stage, role, provider, model, wall, fields, profile=None):
    # `profile` is the role profile in force for the call (None when there is none), on every row.
    keep_lane(ctx, provider, wall)
    return records.append(ctx, "usage", {"card": card, "stage": stage, "role": role, "profile": profile,
                                         "provider": provider, "model": model, "wall_s": round(wall, 2), **fields})


def _lane_file():
    return firstrun.config_dir() / "lanes.jsonl"  # every repo's lane calls on this machine: time, length, provider, repo name only


def keep_lane(ctx, provider, wall):
    _keep({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "wall_s": round(wall, 2),
           "provider": provider, "repo": ctx.root.name}, _lane_file(), "at")


def lane_calls(ctx):
    """([(at, wall_s)] of Claude lane calls from this machine's lanes file and this repo's usage rows, repos seen)."""
    rows, repos = [], set()
    p = _lane_file()
    for line in (host.read(p, errors="replace").splitlines() if p.is_file() else []):
        with contextlib.suppress(ValueError, KeyError, TypeError):
            r = json.loads(line)
            repos.add(r["repo"])
            rows.append(r)
    mine = records.read(ctx, "usage", archive=True)
    repos |= {ctx.root.name} if mine else set()
    rows += mine
    return [(_when(r["at"]), r.get("wall_s", 0)) for r in rows if r.get("provider") == "claude" and r.get("at")], len(repos)


def _at(row):
    return datetime.fromisoformat(row["at"])


# A reading older than this is shown as stale, never as a number (learned from an earlier tool): a meter that froze
# is worse than none. Windows are told apart by their length, else by NAMED.
STALE = timedelta(minutes=30)
NAMED = {"five_hour": 300, "seven_day": 10080, "weekly": 10080}


def _windows(limits):
    """{"week": (fraction used, resets_at), "five_hour": (...)} from a rate_limits block of 0 to 100 percents; the row says "fraction"."""
    out = {}
    for key, w in (limits or {}).items():
        pct = w.get("used_percentage", w.get("used_percent")) if isinstance(w, dict) else None
        mins = (w.get("window_minutes") or NAMED.get(key)) if isinstance(pct, (int, float)) else None
        kind = "week" if mins and mins >= 6 * 1440 else "five_hour" if mins and 200 <= mins <= 400 else None
        if kind:
            at = _when(w.get("resets_at"))
            out[kind] = (pct / 100, at.isoformat(timespec="seconds") if at else None)
    return out


def _meter_file():
    return firstrun.config_dir() / "meters.jsonl"  # an account's week is the same in every repo and lane folder: one file per machine


def readings(ctx):
    """Every meter reading this machine has kept, oldest first; rows an older kit wrote into this repo's records or a lane folder too."""
    out, old = list(records.read(ctx, "meter", archive=True)), Path(os.environ.get("ADE_WORK") or "~/.ade-work").expanduser() / "meters.jsonl"
    for p in {_meter_file(), old}:
        for line in (host.read(p, errors="replace").splitlines() if p.is_file() else []):
            with contextlib.suppress(ValueError):  # a line cut short by a crash is skipped, never fatal
                out.append(json.loads(line))
    out = [r for r in out if isinstance(r, dict) and isinstance(r.get("seen"), str)]
    return sorted(out, key=lambda r: r["seen"])


def _keep(row, p=None, key="seen"):
    p = p or _meter_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with host.lock(p.with_suffix(".lock")):
        if p.is_file() and p.stat().st_size > 1_000_000:  # only the last eight days can matter to a week
            cut = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
            keep = []
            for line in host.read(p, errors="replace").splitlines():
                try:
                    if json.loads(line)[key] >= cut:
                        keep.append(line + "\n")
                except (ValueError, KeyError, TypeError):
                    pass
            host.write(p, "".join(keep))
        with host.opentext(p, "a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def note(ctx, provider, src, limits, seen=None, session=None, cost=None):
    """Keep one `meter` row for a provider's rate_limits as reported at `seen`; repeats of the last row are skipped.
    `session` and `cost` are the interactive session that sent it and its dollars so far, which `headless` subtracts."""
    w = _windows(limits)
    if not w:
        return None
    row = {"provider": provider, "src": src, "unit": "fraction",
           "seen": (seen or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    if session and isinstance(cost, (int, float)):
        row.update({"session": str(session), "session_cost": cost})
    for kind, (used, resets) in w.items():
        row.update({kind: used, f"{kind}_resets": resets})
    last = next((r for r in reversed(readings(ctx)) if r["provider"] == provider), None)
    if last and last["seen"] >= row["seen"]:
        return None  # an older reading never replaces a newer one
    same = last and all(last.get(k) == v for k, v in row.items() if k != "seen")
    t0, t1 = (_when(last["seen"]), _when(row["seen"])) if same else (None, None)
    if t0 and t1 and t1 - t0 < timedelta(minutes=5):
        return None  # the status line fires often; an unchanged reading is kept fresh every five minutes, not each time
    return _keep(row)


def codex_limits():
    """The newest rate_limits Codex wrote to its own session files, with when it wrote them; (None, None) if none.
    Free: no model call and no credentials. Only the rate_limits block of a token_count event is read."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    files = sorted(home.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    for f in files:
        with open(f, "rb") as fh:
            fh.seek(max(0, f.stat().st_size - 2_000_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            p = obj.get("payload") or {}
            if obj.get("type") == "event_msg" and p.get("type") == "token_count" and isinstance(p.get("rate_limits"), dict):
                seen = _when(obj.get("timestamp")) or datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
                return p["rate_limits"], seen
    return None, None


def reading(ctx, provider, now=None):
    """(state, week fraction, five-hour fraction) for the provider: state is "ok", "stale", "reset" or "none"."""
    return _reading(ctx, provider, now)[:3]


def _reading(ctx, provider, now=None):
    # reading() plus the week's reset, which the over-share flag carries.
    now = now or datetime.now(timezone.utc)
    if provider == "codex":
        limits, seen = codex_limits()
        if limits:
            note(ctx, "codex", "codex sessions", limits, seen)
    rows = [r for r in readings(ctx) if r["provider"] == provider and "week" in r]
    if not rows:
        old = [r for r in records.read(ctx, "usage", archive=True) if r.get("provider") == provider and "week_used" in r]
        rows = [{"seen": r["at"], "week": r["week_used"], "week_resets": r.get("week_resets")} for r in old[-1:]]
    if not rows:
        return "none", None, None, None
    last = max(rows, key=lambda r: r["seen"])
    resets = _when(last.get("week_resets")) if last.get("week_resets") else None
    if last.get("week_resets") and (resets is None or resets <= now):
        return "reset", None, None, None
    seen = _when(last["seen"])
    if seen is None or now - seen > STALE:
        return "stale", last["week"], last.get("five_hour"), last.get("week_resets")
    return "ok", last["week"], last.get("five_hour"), last.get("week_resets")


def meter(ctx, provider):
    """Share of the provider's week used, from a fresh reading; None when there is none, it is stale, or it reset."""
    state, week, _ = reading(ctx, provider)
    return week if state == "ok" else None


def meters(ctx, providers):
    # (provider, used, share, over) for each provider in use that has a share. `used` is a word whenever there is no
    # fresh number ("stale", "reset passed", "not reported"), never a zero; `over` is only ever set by a fresh one.
    out = []
    for name, share in ctx.cfg.get("usage", {}).get("share", {}).items():
        if name not in providers:
            continue
        state, week, five, resets = _reading(ctx, name)
        used = {"none": "not reported", "reset": "reset passed",
                "stale": f"stale ({week:.0%} when last read)" if week is not None else "stale"}.get(state)
        if state == "ok":
            used = f"{week:.0%}" + (f" (5h {five:.0%})" if five is not None else "")
        over = state == "ok" and week >= share
        if state in ("ok", "reset"):  # a stale or missing reading knows nothing, so it never moves the flag
            flag(ctx, name, over, resets if over else None)
        out.append((name, used, share, over))
    return out


def flag(ctx, provider, over, until):
    """Keep one durable `share` row each time a provider goes over its week share or comes back under. It is the
    only meter fact that leaves the machine (pushed on ade-records; the owner's call, 27/10/2026): a yes/no and the
    reset it lasts until, never a usage number. No row yet reads as not over."""
    last = next((r for r in reversed(records.read(ctx, "share")) if r["provider"] == provider), {})
    if (last.get("over", False), last.get("until")) == (over, until):
        return None
    return records.append(ctx, "share", {"provider": provider, "over": over, "until": until})


def headless(ctx, provider):
    """Each headless call's share of the week, inferred from the meter: the jump between two readings, less the
    interactive sessions' dollars, split by amount and by how much of each overlapping call ran inside the window
    (an average, so not exact). A jump across a reset is skipped; readings are whole percents (about one point each way).
    Returns [{"card", "stage", "at", "points", "exact"}], points as a fraction of the week."""
    rows = [r for r in readings(ctx) if r["provider"] == provider and "week" in r]
    calls = [(u, _at(u) - timedelta(seconds=u.get("wall_s", 0)), _at(u))
             for u in records.read(ctx, "usage", archive=True) if u.get("provider") == provider]
    spent, est = {}, {}  # each interactive session's dollars as of the reading in hand

    def take(r):
        if r.get("session"):
            spent[r["session"]] = r["session_cost"]
    for r in rows[:1]:
        take(r)
    for a, b in zip(rows, rows[1:]):
        before = dict(spent)
        take(b)
        t0, t1 = _when(a["seen"]), _when(b["seen"])
        ra, rb = _when(a.get("week_resets")), _when(b.get("week_resets"))
        if not (t0 and t1):
            continue
        if (ra and rb and abs(ra - rb) > timedelta(hours=1)) or b["week"] < a["week"]:
            continue
        inside = []
        for u, s, e in calls:
            ran = (min(e, t1) - max(s, t0)).total_seconds()
            whole = (e - s).total_seconds()
            if ran > 0 or (whole == 0 and t0 < e <= t1):
                inside.append((u, ran / whole if whole else 1.0))
        if not inside:
            continue
        mine = sum(max(0, c - before.get(k, 0)) for k, c in spent.items())
        theirs = sum(u.get("cost", 0) * f for u, f in inside)
        if mine and not theirs:
            continue  # no dollars to split the jump by; unattributed rather than guessed
        part = (b["week"] - a["week"]) * (theirs / (theirs + mine) if mine else 1.0)
        weight = sum(f for _, f in inside)
        for u, f in inside:
            e = est.setdefault(id(u), {"card": u["card"], "stage": u["stage"], "at": u["at"], "points": 0.0,
                                       "exact": True})
            e["points"] += part * f / weight
            e["exact"] = e["exact"] and len(inside) == 1 and f == 1.0 and not mine
    return list(est.values())


def totals(ctx, since=None):
    """Per card and per provider sums of tokens, cost, calls and wall time, optionally since a datetime."""
    by_card, by_provider = {}, {}
    for r in records.read(ctx, "usage", archive=True):
        if since and _at(r) < since:
            continue
        for key, bucket in ((r["card"], by_card), (r["provider"], by_provider)):
            t = bucket.setdefault(key, {"calls": 0, "cost": 0.0, "wall_s": 0.0, **dict.fromkeys(KINDS, 0)})
            t["calls"] += 1
            for k in (*KINDS, "cost", "wall_s"):
                t[k] += r.get(k, 0)
    return by_card, by_provider


def shares(ctx, since=None):
    """Per card and per provider, the summed points() of Claude usage rows (a share of the week); only claude has a meter model."""
    by_card, by_provider, m = {}, {}, None
    for r in records.read(ctx, "usage", archive=True):
        if r["provider"] != "claude" or (since and _at(r) < since):
            continue
        m = m or _model(ctx)  # once per call, not once per row
        for key, bucket in ((r["card"], by_card), (r["provider"], by_provider)):
            bucket[key] = bucket.get(key, 0) + points(ctx, r, m)
    return by_card, by_provider


def _id(*parts, n=16):
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[: n * 2]


def _ns(dt):
    return str(int(dt.timestamp() * 1_000_000_000))


def _attrs(**kv):
    return [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else
             {"doubleValue": v} if isinstance(v, float) else {"stringValue": str(v)}} for k, v in kv.items()]


def export(ctx):
    """OTLP/JSON files any OpenTelemetry tool can load: a span per card, per card stage and per provider call;
    sums for tokens and cost. Written under .ade/telemetry/ and nowhere else."""
    rows = records.read(ctx, "usage", archive=True)
    resource = {"attributes": _attrs(**{"service.name": "ade", "ade.repo": ctx.root.name})}
    scope = {"name": "ade", "version": "1"}
    spans, groups = [], {}
    for i, r in enumerate(rows):
        end = _at(r)
        start = end - timedelta(seconds=r.get("wall_s", 0))
        groups.setdefault(r["card"], {}).setdefault(r["stage"], []).append((start, end, i, r))
    for card, stages in groups.items():
        trace = _id(ctx.root, card)
        calls = [c for st in stages.values() for c in st]
        card_span = _id(trace, "card", n=8)
        spans.append({"traceId": trace, "spanId": card_span, "name": f"card {card}", "kind": 1,
                      "startTimeUnixNano": _ns(min(c[0] for c in calls)), "endTimeUnixNano": _ns(max(c[1] for c in calls)),
                      "attributes": _attrs(**{"ade.card": card})})
        for stage, st in stages.items():
            stage_span = _id(trace, stage, n=8)
            spans.append({"traceId": trace, "spanId": stage_span, "parentSpanId": card_span, "name": stage, "kind": 1,
                          "startTimeUnixNano": _ns(min(c[0] for c in st)), "endTimeUnixNano": _ns(max(c[1] for c in st)),
                          "attributes": _attrs(**{"ade.card": card, "ade.stage": stage})})
            for start, end, i, r in st:
                fields = {f"ade.{k}": r[k] for k in (*KINDS, "cost", "turns") if k in r}
                spans.append({"traceId": trace, "spanId": _id(trace, i, n=8), "parentSpanId": stage_span,
                              "name": f"{r['provider']} {r['role']}", "kind": 3, "startTimeUnixNano": _ns(start),
                              "endTimeUnixNano": _ns(end), "attributes": _attrs(**{
                                  "gen_ai.system": r["provider"], "gen_ai.request.model": r["model"], **fields})})
    points = {}
    for r in rows:
        for k in (*KINDS, "cost"):
            if k in r:
                key = ("ade.cost_usd" if k == "cost" else "ade.tokens", r["provider"], r["model"], k)
                points[key] = points.get(key, 0) + r[k]
    first = _ns(_at(rows[0])) if rows else _ns(datetime.now(timezone.utc))
    now = _ns(datetime.now(timezone.utc))
    metrics = {}
    for (name, provider, model, kind), v in sorted(points.items()):
        attrs = _attrs(**{"gen_ai.system": provider, "gen_ai.request.model": model,
                          **({} if name == "ade.cost_usd" else {"ade.kind": kind})})
        point = {"attributes": attrs, "startTimeUnixNano": first, "timeUnixNano": now,
                 **({"asDouble": float(v)} if name == "ade.cost_usd" else {"asInt": str(v)})}
        metrics.setdefault(name, []).append(point)
    out = ctx.state / "telemetry"
    out.mkdir(parents=True, exist_ok=True)
    traces = {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": scope, "spans": spans}]}]}
    sums = [{"name": n, "unit": "USD" if n == "ade.cost_usd" else "{token}",
             "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": pts}} for n, pts in metrics.items()]
    host.write(out / "traces.json", json.dumps(traces, indent=1) + "\n")
    host.write(out / "metrics.json", json.dumps({"resourceMetrics": [{"resource": resource, "scopeMetrics": [
        {"scope": scope, "metrics": sums}]}]}, indent=1) + "\n")
    return out, len(spans), sum(len(p) for p in metrics.values())

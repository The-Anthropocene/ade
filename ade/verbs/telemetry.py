# id: ade.verbs.telemetry
# what: show usage per card and per provider for the week, each week meter against its share; export OTLP files locally
# cmd: ade telemetry [export] [--flights] [--outcomes [--since DATE]] [--week [--json] [--since ISO]]
# reads: .ade/records.jsonl usage rows, ade.toml [usage]; with --week, ade.burn's sources
# writes: .ade/telemetry/ (export only)
import contextlib
import math
import os
from datetime import datetime, timedelta, timezone

from ade import burn, config, providers, records, telemetry

PLAN_PRICE = dict(zip(telemetry.PLANS, (200, 100, 20)))  # in PLANS order; monthly US dollars: the [usage] plan_price defaults


def add(p):
    p.add_argument("--outcomes", action="store_true", help="what landed, parked and was answered this week, from the records")
    p.add_argument("--since", type=datetime.fromisoformat, metavar="DATE", help="with --outcomes or --week: start at DATE, not 7 days ago")
    p.add_argument("--week", action="store_true", help="the 7-day burn from this machine's Claude Code and Codex transcripts (ade/burn.py)")
    p.add_argument("--json", action="store_true", help="with --week: the same data as JSON")
    p.add_argument("action", nargs="?", choices=["export"], help="write OpenTelemetry JSON to .ade/telemetry/")
    p.add_argument("--flights", action="store_true", help="list each conduct flight: card, builder, reviewer, outcome")


@contextlib.contextmanager
def flight(ctx):
    # One `ade conduct` run is one flight: f1, f2, ... per project; usage rows written meanwhile carry its id (records.py).
    os.environ["ADE_FLIGHT_ID"] = f"f{max((int(r['flight'][1:]) for r in records.read(ctx, 'flight', archive=True)), default=0) + 1}"
    try:
        yield
    finally:
        os.environ.pop("ADE_FLIGHT_ID", None)


def flights(ctx):
    cards, journal, usage = records.merged(ctx, "card"), records.read(ctx, "journal", archive=True), records.read(ctx, "usage", archive=True)
    flights_ = records.read(ctx, "flight", archive=True)
    for f in flights_:
        who = {s: next((u["provider"] for u in usage if u.get("flight") == f["flight"] and u["card"] == f["card"]
                        and u["stage"] == s), "nobody") for s in ("build", "review")}
        last = next((r for r in reversed(journal) if r["card"] == f["card"] and r["at"] >= f["at"]), None)
        out = "landed" if cards.get(f["card"], {}).get("status") == "landed" else f"{last['stage']} {last['outcome']}" if last else "no stop"
        # Rows are stamped at a stage's end: a stage's minutes run from the card's previous row in this flight (or the flight's at).
        later = [g["at"] for g in flights_ if g["card"] == f["card"] and g["at"] > f["at"]]
        rows, prev, took = [r for r in journal if r["card"] == f["card"] and r["at"] >= f["at"] and not (later and r["at"] >= later[0])], f["at"], []
        for r in rows:
            took.append(f"{r['stage']} {round((datetime.fromisoformat(r['at']) - datetime.fromisoformat(prev)).total_seconds() / 60)}m")
            prev = r["at"]
        print(f"{f['flight']}: {f['card']}, built by {who['build']}, reviewed by {who['review']}, {out}" + (f" ({', '.join(took)})" if took else ""))


def outcomes(ctx, since):
    # Records only; the meant-for split is whom [conduct] answerer meant the question for when it was raised.
    from ade.stages import land
    from ade.verbs import decide, digest
    at = since.isoformat(timespec="seconds")
    first = {r["id"]: r["at"] for r in reversed(records.read(ctx, "card", archive=True)) if r.get("status") == "landed"}
    won = {c for c, t in first.items() if t >= at}
    undone = won & set(digest.reverted(ctx, at))  # git is the one source for a revert; the card row stays landed
    won -= undone
    qs = [d for d in records.merged(ctx, "decision").values() if d.get("kind") == "question"]
    redone = {d["card"] for d in qs if d.get("card") in won and land.rework_note(d) is not None}
    parked = {d["card"] for d in decide.unsettled(ctx) if d.get("rec") == "retry" and decide.state(d) == "open"}
    ans = [d for d in qs if d.get("answered", "") >= at[:10]]
    agent, inside = sum(d.get("answerer") == "agent" for d in ans), sum(d.get("by") == "agent" for d in ans)
    print(f"since {at[:10]}: landed first time {len(won - redone)}, after a rework answer {len(redone)}, reverted {len(undone)}, parked and open {len(parked)}")
    print(f"questions answered: {len(ans) - agent} meant for a person, {agent} meant for the agent" + (f"\nanswered from inside an agent: {inside}" if inside else ""))
    used = [t for c, t in telemetry.totals(ctx)[0].items() if c in won]
    if used:
        print(f"usage per landed card: {sum(t['tokens_in'] + t['tokens_out'] for t in used) // len(used) // 1000}k tokens, {sum(t['calls'] for t in used) / len(used):.1f} calls")


def in_use(ctx):
    # The roles' engines now, and any engine that ran in the last 7 days (an ADE_PROVIDER override is a real run too).
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    ran = {r["provider"] for r in records.read(ctx, "usage", archive=True) if r["at"] >= since}
    return sorted({providers.engine(ctx, role) for role in ctx.cfg["roles"]} | ran)


def meter_text(ctx):
    # Without a week window the honest reading is this kit's own spend over seven days, not a share of the week.
    since = datetime.now(timezone.utc) - timedelta(days=7)
    week = telemetry.totals(ctx, since=since)[1]
    est = telemetry.shares(ctx, since)[1] if metered(ctx) else {}
    got = measured(ctx, since)[1]

    def spent(n):
        t = week.get(n)
        if not t:
            return "no calls in 7d"
        cost = f", {share_text(ctx, got[n], True)}" if n in got else f", {share_text(ctx, est[n])}" if n in est else f", ${t['cost']:.2f}" if t["cost"] else ""
        tokens = t["tokens_in"] + t["tokens_out"] + t["cache_read"] + t["cache_write"]
        return f"{t['calls']} call{'s' * (t['calls'] != 1)}, {tokens // 1000}k tokens{cost} in 7d"

    # API-key billing has no subscription week, so claude's "not reported" is no fault there (config-only check, as loop.billing).
    api_key = "ANTHROPIC_API_KEY" in ctx.cfg["providers"].get("claude", {}).get("secrets", {})
    def fresh(n, used, share, over):  # a real reading: the lanes' own estimated part rides beside it
        return f"{n} week {used} of {share:.0%}{' OVER' if over else ''}" + (f" · lanes ~{est[n]:.1%} (estimated)" if n in est else "")

    return " · ".join(f"claude on API key ({spent(n)})" if n == "claude" and used == "not reported" and api_key else
                      f"{n} week {used} ({spent(n)})" if used in ("not reported", "reset passed") or used.startswith("stale") else
                      fresh(n, used, share, over)
                      for n, used, share, over in telemetry.meters(ctx, in_use(ctx)))


def metered(ctx):
    return ctx.cfg.get("usage", {}).get("unit", "dollars") in ("meter", "plan_dollars")


def measured(ctx, since=None):
    # Codex's share of the week from its own meter jumps (telemetry.headless): per card and per provider, if any.
    by_card, by_provider = {}, {}
    if metered(ctx):
        for e in telemetry.headless(ctx, "codex"):
            if since and e["at"] < since.isoformat(timespec="seconds"):
                continue
            by_card[e["card"]] = by_card.get(e["card"], 0) + e["points"]
            by_provider["codex"] = by_provider.get("codex", 0) + e["points"]
    return by_card, by_provider


def _sig(x, unit=1, floor=1.0):
    # Two significant figures below `floor` (0.0042), else `unit` decimals; a zero keeps the plain form.
    if x <= 0 or x >= floor:
        return f"{x:.{unit}f}"
    return f"{x:.{1 - math.floor(math.log10(x))}f}"


def share_text(ctx, share, measured=False):
    # A share of the week: as a percent, or (plan_dollars) as that share of the weekly price, monthly price * 12 / 52.
    u = ctx.cfg.get("usage", {}) if ctx else {}
    how = "measured from meter jumps" if measured else "estimated, lanes only"
    if measured or u.get("unit") != "plan_dollars":  # measured is codex's own meter: the plan price is Claude's
        return f"{_sig(share * 100)}% of week ({how})"
    plan = u.get("plan", "max20x")
    price = u.get("plan_price", {}).get(plan, PLAN_PRICE[plan])
    return f"${_sig(share * price * 12 / 52, 2, 0.1)} of plan ({how})"


def _row(name, t, share=None, ctx=None, got=None):
    cost = f" {share_text(ctx, got, True)}" if got is not None else f" {share_text(ctx, share)}" if share is not None else f" ${t['cost']:.2f}" if t["cost"] else ""
    return (f"  {name:<10} {t['calls']:>3} calls  in {t['tokens_in']}  out {t['tokens_out']}  "
            f"cache {t['cache_read']}/{t['cache_write']}{cost}  {t['wall_s']:.0f}s")


def run(args, cwd):
    ctx = config.load(cwd)
    if args.action == "export":
        out, spans, points = telemetry.export(ctx)
        print(f"wrote {out}/traces.json ({spans} spans) and metrics.json ({points} points); nothing was sent anywhere")
        return 0
    if args.week or args.json or (args.since and not args.outcomes):
        return burn.run(ctx, args) or 0
    if args.outcomes:
        return outcomes(ctx, args.since or datetime.now(timezone.utc) - timedelta(days=7)) or 0
    if args.flights:
        return flights(ctx) or 0
    since = datetime.now(timezone.utc) - timedelta(days=7)
    cards, week = telemetry.totals(ctx)[0], telemetry.totals(ctx, since=since)[1]
    card_share, week_share = ({}, {}) if not metered(ctx) else (telemetry.shares(ctx)[0], telemetry.shares(ctx, since)[1])
    got_card, got_week = measured(ctx)[0], measured(ctx, since)[1]
    print("last 7 days, by provider:")
    print("\n".join(_row(n, t, week_share.get(n), ctx, got_week.get(n)) for n, t in week.items()) or "  no calls yet")
    for prov, alias, served in sorted({(r["provider"], r["model"], r["served_model"]) for r in records.read(ctx, "usage", archive=True)
                                       if r.get("served_model") and r["served_model"] != r["model"]}):
        print(f"  {prov} {alias} was served as {served}")
    print("all time, by card:")
    print("\n".join(_row(n, t, card_share.get(n), ctx, got_card.get(n)) for n, t in cards.items()) or "  no calls yet")
    print(meter_text(ctx) or "no week meters: no provider in use has a share in ade.toml [usage]")
    for n in in_use(ctx):
        est = telemetry.headless(ctx, n)
        if est:
            exact = sum(e["exact"] for e in est)
            print(f"{n} headless, inferred from its meter: {len(est)} calls, about "
                  f"{sum(e['points'] for e in est) / len(est):.1%} of the week each ({exact} exact, "
                  f"{len(est) - exact} averaged over overlapping runs or interactive spend; each jump is ±1 point)")
    if metered(ctx):
        print("lane figures leave out attended sessions and helper agents, which the real week reading includes")

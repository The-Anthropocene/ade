# id: ade.verbs.wait
# what: wait for a condition with a bound (exit 0 met, 3 stage parked, 4 bound passed, 5 lane stalled); run it in the background to be told; `stage` is a condition for a pilot that needs to block on one stage
# cmd: ade wait clean|journal CARD|stage CARD lane|build|review|test|land|answered ID|exit PID|file PATH|time 10m [--bound 30m] [--lane CARD]
# reads: main checkout, .ade/records.jsonl, .ade/lanes/<card>.log
# writes: -
from ade import config, wait


def add(p):
    p.add_argument("condition", choices=sorted(wait.CONDITIONS))
    p.add_argument("arg", nargs="*", help="the card (and for `stage` its stage), decision id, pid, path or duration the condition watches")
    p.add_argument("--bound", default="30m", help="give up after this long (90s, 30m, 2h); every wait has one")
    p.add_argument("--lane", metavar="CARD", help="also stop if this card's lane log goes quiet for [watch] stall minutes")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between looks")


def run(args, cwd):
    ctx = config.load(cwd)
    arg = " ".join(args.arg) or None
    code = wait.until(ctx, args.condition, arg, args.bound, args.lane, args.poll)
    what = f"{args.condition} {arg or ''}".strip()
    row = wait.SEEN if args.condition == "stage" else {}
    seen = f"{row.get('outcome', '')} {row.get('note', '')}".strip()
    print({wait.MET: f"met: {what} {seen}".strip(), wait.STOPPED: f"stopped: {what} {seen}", wait.BOUND: f"bound passed ({args.bound}) before {what}",
           wait.STALLED: f"stalled: {args.lane}'s lane log has not grown for {ctx.cfg['watch']['stall']} minutes"}[code])
    return code

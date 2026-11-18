# id: ade.verbs.q
# what: keep long output out of a model's context: run a command through the store, page or grep what it kept, query it
# cmd: ade q run -- CMD | ade q show ID [--page N] | ade q grep ID|all PATTERN | ade q sql QUERY | ade q list
# reads: .ade/q.sqlite, .ade/records.jsonl, graph.json
# writes: .ade/q.sqlite
import re
import subprocess
import sys

from ade import config, host, q


def add(p):
    sub = p.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run", help="run a command; long output is stored and shown as head, tail and a handle")
    r.add_argument("--card", help="the card this output belongs to")
    r.add_argument("cmd", nargs="+", help="the command after --; one quoted argument runs through the shell")
    s = sub.add_parser("show", help="one page of a stored output")
    s.add_argument("id")
    s.add_argument("--page", type=int, default=1)
    g = sub.add_parser("grep", help="lines matching a regex in one stored output, or in all of them")
    g.add_argument("id", help="q17, or all")
    g.add_argument("pattern")
    sq = sub.add_parser("sql", help="a read-only query over outputs, records, files and edges")
    sq.add_argument("query")
    sub.add_parser("list", help="the newest stored outputs")


def run(args, cwd):
    ctx = config.load(cwd)
    if args.action == "run":
        # One argument is a shell line (pipes and all); several are an argv. Either way the exit code passes through.
        shell = len(args.cmd) == 1 and not re.fullmatch(r"[\w./+-]+", args.cmd[0])  # a bare word is a program, not a line
        argv = args.cmd[0] if shell else host.which(args.cmd)
        try:
            r = subprocess.run(argv, shell=shell, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            print(f"ade: cannot run {args.cmd[0]}: {e.strerror or e}", file=sys.stderr)
            return 127
        out = r.stdout.decode("utf-8", "replace")
        shown = q.emit(ctx, out, cmd=" ".join(args.cmd), rc=r.returncode, card=args.card)
        print(shown, end="" if shown == out and out.endswith("\n") else "\n")
        return r.returncode
    if args.action == "show":
        print(q.page(ctx, args.id, args.page))
    elif args.action == "grep":
        print(q.grep(ctx, args.id, args.pattern))
    elif args.action == "sql":
        print(q.sql(ctx, args.query))
    else:
        print(q.listing(ctx) or "nothing stored")
    sys.stdout.flush()

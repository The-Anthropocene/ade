# id: ade.verbs.land
# what: land a card whose lane tree has an approved review and passing tests: merge, verdict, outcome
# cmd: ade land CARD [--catch-up]
# reads: .ade/records.jsonl, lane
# writes: main branch, .ade/records.jsonl (verdict, outcome, card status)
from ade import config, stages


def add(p):
    p.add_argument("card")
    p.add_argument("--catch-up", action="store_true", help="merge remote trunk into the pushed ade/<card>")


def run(args, cwd):
    ctx = config.load(cwd)
    res = stages.land.run(ctx, stages.card(ctx, args.card), args.catch_up)
    print(f"land    {res.outcome:<16} {res.note}")
    if res.next:
        print(f"next: {res.next}")
    return 0 if res.outcome == "done" else 3

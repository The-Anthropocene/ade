# id: ade.verbs.decide
# what: rulings that govern path globs, and questions for a person with a recommendation, a default and a deadline
# cmd: ade decide --path GLOB TEXT | ade decide --standing TEXT | ade decide --import FILE.jsonl | ade decide --ask TEXT --rec R --default D --by DATE|Nd [--card CARD] | ade decide --answer ID TEXT | ade decide --retire ID --why TEXT [--superseded-by ID] | ade decide --list|--rulings|--queue|--desk|--show ID
# reads: .ade/records.jsonl
# writes: .ade/records.jsonl (decision rows), .ade/logs/<card>.dropped.patch (a dropped card's lane work); removes that lane and its ade/<card> branch
import getpass
import json
import re
from datetime import date, timedelta

from ade import config, host, records
from ade.lib.agent import answerer
from ade.lib.askcard import warn_unlinked
from ade.gitx import git
from ade.refuse import Refusal, refuse
from ade.stages import branch, lane, lane_path, main


# the answers each machine-read question (keyed by rec) accepts; any other rec is free text
FORMS = {"land": ["land", "rework: TEXT", "drop"], "retry": ["retry", "retry: NOTE", "drop"], "accept": ["accept", "retry", "retry: NOTE", "drop"]}


def who():
    """The user acting; "unknown" when getpass cannot read one (a container with no USER and no passwd entry)."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def park(ctx, text, fields):
    """Record a park question whose text names its own id and who answers ([conduct] answerer; unset reads as person)."""
    i, agent = records.append(ctx, "decision", {**fields, "text": text}, prefix="d")["id"], ctx.cfg.get("conduct", {}).get("answerer") == "agent"
    who = (f'the agent watching this project answers it with ade decide --answer {i} "retry: NOTE" when the stop is clear, or asks its person'
           if agent else f'a person answers: ade decide --answer {i} retry (or "retry: NOTE")')
    return records.append(ctx, "decision", {"id": i, "card": fields.get("card"), "text": f"{text}; {who}. A NOTE reaches the card's next model.", **({"answerer": "agent"} if agent else {})})


def first_sentence(text, limit=120):
    """First sentence of a ruling (ends at . ! or ? before whitespace or the end), cut at limit with an ellipsis."""
    first = re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit - 1] + "…"


def by(d):
    """The stamp a ruling row carries: (by person), (by agent) or (by unknown); a row written before stamps reads unknown."""
    stood = f", stood by {d['stood_by']}" if d.get("stood_by") else ""
    return f"(by {d.get('by') or 'unknown'}{stood})"


def add(p):
    p.add_argument("text", nargs="?")
    p.add_argument("--path", action="append", default=[], help="glob the decision governs (repeatable)")
    p.add_argument("--standing", action="store_true", help="record TEXT as a standing ruling with no paths: every role's handover prints it first")
    p.add_argument("--ask", action="store_true", help="record TEXT as a question for a person, not a ruling")
    p.add_argument("--card", help="with --ask, the card the question is about: the card is held until it is answered (or its default applies); any answer releases it and reaches that card's next model call")
    p.add_argument("--rec", help="the question's recommended answer")
    p.add_argument("--default", help="the answer that applies if nobody answers by the deadline")
    p.add_argument("--by", help="deadline: YYYY-MM-DD, or Nd for N days from today")
    p.add_argument("--retire", metavar="ID", help="retire ruling ID: it stays in --list but leaves briefs and prompts; needs --why")
    p.add_argument("--why", help="the reason for --retire")
    p.add_argument("--review", help="with a new ruling, the date to review it: YYYY-MM-DD, or Nd from today (default 90d)")
    p.add_argument("--superseded-by", metavar="ID", help="with --retire, the ruling that replaces it")
    p.add_argument("--answer", metavar="ID", help="answer question ID with TEXT; land, retry and accept questions take only their listed forms (land, 'rework: TEXT', 'retry: NOTE', accept, drop)")
    p.add_argument("--list", action="store_true", help="every ruling and question")
    p.add_argument("--show", metavar="ID", help="one decision in full: its line, then its whole text")
    p.add_argument("--import", dest="import_file", metavar="FILE", help="record rulings from a JSONL file (text, paths, source, date), once per source")
    p.add_argument("--rulings", action="store_true", help="the rule book: every live ruling whole, standing first, then the counts against the limits")
    p.add_argument("--queue", action="store_true", help="open questions, nearest deadline first")
    p.add_argument("--desk", action="store_true", help="walk the open questions one at a time: Enter takes the recommendation, s skips, q stops")


REVIEW_DAYS, WARN_DAYS = 90, 7  # constants on purpose: a ruling is reviewed every 90 days, and waits on the person from 7 days before


def review_date(d):
    """The date a ruling is due for review: the stored one, or for a ruling written before dates 90 days after it was written."""
    return d.get("review") or (date.fromisoformat(d["at"][:10]) + timedelta(days=REVIEW_DAYS)).isoformat()


def waiting(ctx, today=None):
    """Live rulings that wait on a person: from 7 days before the review date on. They still govern; nothing stops."""
    cut = ((today or records.today()) + timedelta(days=WARN_DAYS)).isoformat()
    return sorted((d for d in book(ctx) if review_date(d) <= cut), key=lambda d: int(d["id"][1:]))


def waiting_line(ctx):
    """One line naming the waiting rulings, never their text; empty when none wait."""
    ws = waiting(ctx)
    if not ws:
        return ""
    ids = ", ".join(d["id"] for d in ws[:5]) + (f" and {len(ws) - 5} more" if len(ws) > 5 else "")
    return f"rulings waiting on a person: {len(ws)} ({ids}): ade decide --desk"


def reviewed(s):
    """The review date a new ruling stores: --review DATE|Nd, else 90 days on."""
    return (when(s) if s else records.today() + timedelta(days=REVIEW_DAYS)).isoformat()


def when(s):
    try:
        return (records.today() + timedelta(days=int(s[:-1]))) if s.endswith("d") else date.fromisoformat(s)
    except ValueError:
        refuse("decide.incomplete", f"cannot read deadline {s!r}", "use --by 2026-11-02 or --by 7d")


def state(d, today=None):
    """ruling, retired, open, answered, or defaulted: a question nobody answered by its deadline takes its default."""
    if d.get("kind", "ruling") == "ruling":
        return "retired" if d.get("retired") else "proposal" if d.get("proposed") else "ruling"
    if "answer" in d:
        return "answered"
    if not d.get("default") or not d.get("deadline"):
        return "open"  # no default or no deadline: silence never answers it
    return "defaulted" if (today or records.today()) > date.fromisoformat(d["deadline"]) else "open"


def line(d, today=None):
    s, where = state(d, today), f" [{', '.join(d['paths'])}]" if d["paths"] else ""
    if s == "ruling":
        kept = f", kept by {d['kept_by']} {d['kept']}" if d.get("kept") else ""
        return (f"{d['id']}{where}{' (standing)' if d.get('standing') else ''} {d['text']} {by(d)} "
                f"written {d['at'][:10]}{kept}, review {review_date(d)}")
    if s == "proposal":
        return f"{d['id']} (standing, PROPOSAL: governs nothing until the person answers stand or drop: WHY) {d['text']} {by(d)} proposed {d['at'][:10]}"
    if s == "retired":
        sup = f", superseded by {d['superseded_by']}" if d.get("superseded_by") else ""
        return f"{d['id']}{where} {d['text']} {by(d)} (retired {d['retired']} by {d['retired_by']}{sup}: {d['why']})"
    if s == "open":
        rec = f"{d['id']}{where} open question: {d['text']} (rec: {d.get('rec')}; " + (f"parked {d['parked_days']} days; " if d.get("parked_days") else "")
        if d.get("default") and d.get("deadline"):
            return rec + f"default {d['default']} after {d['deadline']})"
        return rec + f"default: {d.get('default') or 'none'}; no deadline; silence never answers)"
    held = d["answer"] if s == "answered" else f"{d['default']} (default, no answer by {d['deadline']})"
    return f"{d['id']}{where} {d['text']} -> {held}"


def unsettled(ctx):
    """Decisions minus park questions of a landed or closed card, or answered or defaulted (a park is not a ruling); a ruling naming such a card still governs."""
    done = {c for c, r in records.merged(ctx, "card").items() if r.get("status") in ("landed", "closed")}
    return [d for d in records.merged(ctx, "decision").values()
            if not (d.get("kind") == "question" and d.get("card")
                    and (d["card"] in done or state(d) in ("answered", "defaulted")))]


def undelivered(ctx, card):
    """The card's answered or defaulted questions whose answer no model has been given yet (providers.call marks 'reworked')."""
    return [d for d in records.merged(ctx, "decision").values()
            if d.get("card") == card and state(d) in ("answered", "defaulted") and not d.get("reworked")]


def parked_days(ctx, card, today=None):
    """How many days running, ending today, the card has stopped parked or needing judgement."""
    days = {r["at"][:10] for r in records.read(ctx, "journal") if r["card"] == card and r.get("outcome") in ("parked", "needs-judgement")}
    n, day = 0, today or records.today()
    while day.isoformat() in days:
        n, day = n + 1, day - timedelta(days=1)
    return n


def queue(ctx, today=None):
    qs = [{**d, "parked_days": n} if d.get("card") and (n := parked_days(ctx, d["card"], today)) >= 3 else d
          for d in unsettled(ctx) if state(d, today) == "open"]
    # A question with a deadline first, nearest first; then those without, oldest first (the sort is stable).
    return sorted(qs, key=lambda d: (not d.get("deadline"), d.get("deadline", "")))


def live(ctx):
    """The live rulings: the ones that govern (a proposal or a retired one does not)."""
    return [d for d in records.merged(ctx, "decision").values() if state(d) == "ruling"]


def book(ctx):
    """The rule book: live rulings and proposals; both count toward the limits and wait on the person."""
    return [d for d in records.merged(ctx, "decision").values() if state(d) in ("ruling", "proposal")]


def fit(ctx, n, standing=0):
    """Refuse when n more rulings (standing of them standing) would pass [budgets] rulings or standing; a book already over takes none."""
    held, lim = book(ctx), ctx.cfg["budgets"]
    have, stand = len(held), sum(1 for d in held if d.get("standing"))
    if have + n <= lim["rulings"] and stand + standing <= lim["standing"]:
        return
    counts = f"the rule book holds {have} rulings ({stand} standing) against limits of {lim['rulings']} ({lim['standing']} standing)"
    if have > lim["rulings"] or stand > lim["standing"]:
        counts += "; it was already over, and the rulings it holds stay in force"
    refuse("budget.over", f"{counts}; this adds {n} ({standing} standing)",
           "retire one with `ade decide --retire ID --why TEXT`, or ask the person to raise [budgets] rulings or standing in ade.toml and rerun `ade init`")


def rulings(ctx):
    """Every live ruling whole, standing first, then both counts against the limits."""
    held, lim = book(ctx), ctx.cfg["budgets"]
    held.sort(key=lambda d: not d.get("standing"))
    for d in held:
        print(line(d))
    print(f"{len(held)} of {lim['rulings']} rulings, {sum(1 for d in held if d.get('standing'))} of {lim['standing']} standing")


def import_rulings(ctx, file):
    """Rulings from a JSONL file, each once per source; a bad row refuses the whole file before anything is written."""
    rows = []
    for n, raw in enumerate(host.read(file).splitlines(), 1):
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            r = None
        if not isinstance(r, dict) or not r.get("text") or not r.get("source") or not isinstance(r.get("paths", []), list):
            refuse("decide.incomplete", f"{file} line {n}: each row needs text, source and paths as a list",
                   'one object per line: {"text": "...", "paths": ["src/*"], "source": "id", "date": "2026-01-01"}')
        rows.append(r)
    have = {d["source"]: d["id"] for d in records.merged(ctx, "decision").values() if d.get("source")}
    seen, new = set(have), []
    for r in rows:
        if str(r["source"]) not in seen:
            seen.add(str(r["source"]))
            new.append(r)
    for r in new:
        if len(r["text"]) > 400:
            refuse("decide.incomplete", f"{file}: a ruling is at most 400 characters; the one from {r['source']} is {len(r['text'])}",
                   "keep each rule in 400 characters; the story behind it goes in the card, the commit or the docs")
    fit(ctx, len(new))
    added = skipped = 0
    for r in rows:
        if str(r["source"]) in have:
            print(f"skipped {have[str(r['source'])]}: source {r['source']} already imported")
            skipped += 1
            continue
        row = records.append(ctx, "decision", {"text": r["text"], "paths": r.get("paths", []), "kind": "ruling",
                                               "source": str(r["source"]), "date": r.get("date") or records.today().isoformat(),
                                               "review": (records.today() + timedelta(days=REVIEW_DAYS)).isoformat(), **answerer()}, prefix="d",
                              guard=lambda: fit(ctx, 1))
        have[row["source"]] = row["id"]
        added += 1
    print(f"imported {added}, skipped {skipped}")


def drop_lane(ctx, card):
    """A dropped card's lane work (committed and not, new files too) becomes a patch against trunk; then the lane goes."""
    lp = lane_path(ctx, card)
    if not (lp / ".git").exists():
        return
    git(lp, "add", "-N", ".")  # intent-to-add: new files show in the diff
    base = git(lp, "merge-base", main(ctx), "HEAD")
    out = ctx.state / "logs"
    out.mkdir(parents=True, exist_ok=True)
    patch = out / f"{card}.dropped.patch"
    host.write(patch, git(lp, "diff", "--binary", base) + "\n")
    print(f"lane work saved: {patch}")
    lane.unshare(ctx, lp)
    git(ctx.root, "worktree", "remove", "--force", str(lp))
    git(ctx.root, "branch", "-q", "-D", branch(card))


def accepts(d, text):
    """Whether text is an answer the question reads: any text for free text, else one of its FORMS."""
    head, sep, rest = text.partition(":")
    word = (text.lower().split() or [""])[0].strip(".,:;")
    return d.get("rec") not in FORMS or any(head.strip().lower() == f.split(":")[0] and sep and rest.strip() if ":" in f else word == f for f in FORMS[d["rec"]])


def answer(ctx, d, text):
    """Record the answer to question d; a drop answer also closes its card. Both --answer and --desk come here."""
    records.append(ctx, "decision", {"id": d["id"], "answer": text, "answered": records.today().isoformat(), **answerer()})
    print(f"{d['id']} answered: {text}")
    if d.get("rec") == "land" and text.lower().startswith("land") and answerer()["by"] == "agent":
        own = "ade/install.py" in (d.get("paths") or [])  # the card's own generator rendered protected files
        if own or ctx.cfg.get("conduct", {}).get("answerer") != "agent":
            engine = answerer()["engine"]
            print(f"an agent's land will not land {d.get('card') or d['id']}: "
                  + ("a person answers" if own else f"this shell carries {engine}'s session marker, which a `!` command inside it inherits too; "
                     'answer from your own terminal (or set [conduct] answerer = "agent" in ade.toml)'))
    if d.get("card") and text.split()[0].lower().strip(".,:;") == "drop":
        drop_lane(ctx, d["card"])
        c = records.merged(ctx, "card").get(d["card"])
        if c and c["status"] not in ("landed", "closed"):  # a vetoed card must not fly again
            records.append(ctx, "card", {"id": c["id"], "status": "closed", "closed_why": f"dropped: answer to {d['id']}",
                                         "closed_by": answerer()["by"], "retired": True, "closed_at": records.today().isoformat()})
            print(f"{c['id']} closed: dropped: answer to {d['id']}")


def review_answer(ctx, d, text):
    """keep (90 days more, stamped) or retire: WHY for a ruling waiting on a person; anything else is refused, naming both."""
    head, sep, rest = text.partition(":")
    if state(d) == "proposal":
        who_ = answerer()["by"]
        if text.strip().lower().rstrip(".") == "stand":
            agent = ctx.cfg.get("conduct", {}).get("answerer") == "agent"
            if who_ != "person" and not agent:
                refuse("decide.incomplete", f"{d['id']} is a proposal; only the person makes a standing ruling stand",
                       f"tell them it waits (`ade decide --desk`); {d['id']} governs nothing until then")
            records.append(ctx, "decision", {"id": d["id"], "proposed": False, "stood": records.today().isoformat(),
                                             "stood_by": "person" if who_ == "person" else "agent",
                                             "review": (records.today() + timedelta(days=REVIEW_DAYS)).isoformat()})
            print(f"{d['id']} stands for every card, stood by {'person' if who_ == 'person' else 'agent'}")
        elif head.strip().lower() == "drop" and sep and rest.strip():
            records.append(ctx, "decision", {"id": d["id"], "retired": records.today().isoformat(), "retired_by": who_, "why": rest.strip()})
            print(f"{d['id']} dropped: {rest.strip()}")
        else:
            refuse("decide.incomplete", f"{d['id']} is a proposal; {text!r} is not an answer it reads: the answers are stand and drop: WHY",
                   f'answer with one of: ade decide --answer {d["id"]} stand (the person) | ade decide --answer {d["id"]} "drop: WHY"')
        return True
    if text.strip().lower().rstrip(".") == "keep":
        records.append(ctx, "decision", {"id": d["id"], "review": (records.today() + timedelta(days=REVIEW_DAYS)).isoformat(),
                                         "kept": records.today().isoformat(), "kept_by": answerer()["by"]})
        print(f"{d['id']} kept: next review in {REVIEW_DAYS} days")
    elif head.strip().lower() == "retire" and sep and rest.strip():
        records.append(ctx, "decision", {"id": d["id"], "retired": records.today().isoformat(), "retired_by": who(), "why": rest.strip()})
        print(f"{d['id']} retired: {rest.strip()}")
    else:
        refuse("decide.incomplete", f"{d['id']} is due for review; {text!r} is not an answer it reads: the answers are keep and retire: WHY",
               f'answer with one of: ade decide --answer {d["id"]} keep | ade decide --answer {d["id"]} "retire: WHY"')
    return True


def desk(ctx):
    """Walk the open questions in queue order; ends at q or the end of input."""
    qs = queue(ctx)
    if not qs:
        print("no open questions")
    ws = waiting(ctx)
    for d in qs:
        forms = FORMS.get(d.get("rec"))
        print(line(d))
        print("accepts: " + (" | ".join(forms) if forms else "any text"))
        while True:
            try:
                text = input(f"[Enter = {d.get('rec')}, s skips, q stops] ").strip()
            except EOFError:
                return
            if text == "q":
                return
            if text == "s":
                break
            if not text:
                text = d.get("rec") or ""
            if text and accepts(d, text):
                answer(ctx, d, text)
                break
            print("accepted answers: " + (" | ".join(forms) if forms else "any text"))
    for d in ws:
        print(line(d))
        while True:
            try:
                text = input("[stand, drop: WHY, s skips, q stops] " if state(d) == "proposal" else "[keep, retire: WHY, s skips, q stops] ").strip()
            except EOFError:
                return
            if text == "q":
                return
            if text == "s":
                break
            try:
                if text and review_answer(ctx, d, text):
                    break
            except Refusal as e:
                print(e)


def run(args, cwd):
    ctx = config.load(cwd)
    if not (args.text or args.path or args.ask or args.answer or args.list or args.desk or args.show or args.retire or args.import_file or args.rulings or args.rec or args.default or args.by):
        args.queue = True  # bare `ade decide` shows what waits for an answer
    if args.rulings:
        rulings(ctx)
        return 0
    if args.import_file:
        import_rulings(ctx, args.import_file)
        return 0
    if args.retire:
        decs = records.merged(ctx, "decision")
        d = decs.get(args.retire)
        if args.by:
            refuse("decide.incomplete", "--by is a deadline for --ask, not for --retire", "use --superseded-by for the ruling that replaces it")
        if d is None or state(d) != "ruling" or not args.why or (args.superseded_by and args.superseded_by not in decs):
            refuse("decide.incomplete", f"{args.retire} is not a live ruling, the --why is missing, or --superseded-by names no decision",
                   "ade decide --list, then: ade decide --retire d2 --why \"what changed\" [--superseded-by d9]")
        records.append(ctx, "decision", {"id": args.retire, "retired": records.today().isoformat(), "retired_by": who(),
                                         "why": args.why, **({"superseded_by": args.superseded_by} if args.superseded_by else {})})
        print(f"{args.retire} retired: {args.why}")
        return 0
    if args.show:
        d = records.merged(ctx, "decision").get(args.show)
        if d is None:
            refuse("decide.incomplete", f"no decision {args.show}", "ade decide --list")
        print(line(d))
        if d["text"] not in line(d):
            print(d["text"])
        return 0
    if args.desk:
        desk(ctx)
        return 0
    if args.list or args.queue:
        rows = queue(ctx) if args.queue else records.merged(ctx, "decision").values()
        for d in rows:
            print(line(d))
        if args.queue and not rows:
            print("no open questions")
        if args.queue and rows:
            print(f"answer with: ade decide --answer {rows[0]['id']} \"the answer\"")
        if args.queue and (w := waiting_line(ctx)):
            print(w)
        return 0
    if args.answer:
        d = records.merged(ctx, "decision").get(args.answer)
        nxt = next((q["id"] for q in queue(ctx)), "dN")  # the cure names a question still open, never one already answered
        if d is not None and state(d) == "retired":
            refuse("decide.incomplete", f"{args.answer} is retired ({d['why']}); a retired ruling takes no answer",
                   f"see open questions with `ade decide --queue`, then: ade decide --answer {nxt} \"the answer\"")
        if d is not None and state(d) in ("ruling", "proposal") and d in waiting(ctx) and args.text:
            review_answer(ctx, d, args.text)
            return 0
        if d is None or state(d) in ("ruling", "proposal") or not args.text:
            refuse("decide.incomplete", f"{args.answer} is not a question, or the answer text is missing",
                   f"see open questions with `ade decide --queue`, then: ade decide --answer {nxt} \"the answer\"")
        if not accepts(d, args.text):
            refuse("decide.incomplete", f"{d['rec']} cannot read the answer {args.text!r}",
                   "answer with one of: " + " | ".join(f"ade decide --answer {args.answer} " + (f'"{f}"' if ":" in f else f) for f in FORMS[d["rec"]]))
        answer(ctx, d, args.text)
        return 0
    if args.ask:
        if not (args.text and args.rec and args.default and args.by):
            refuse("decide.incomplete", "a question needs its text, a recommendation, a default and a deadline",
                   "ade decide --ask \"which?\" --rec A --default A --by 7d [--path GLOB]")
        warn_unlinked(args.text, args.card)
        row = records.append(ctx, "decision", {"text": args.text, "paths": args.path, "kind": "question",
                                               "rec": args.rec, "default": args.default,
                                               "deadline": when(args.by).isoformat(),
                                               **({"card": args.card} if args.card else {})}, prefix="d")
        print(f"{row['id']} asked; {row['default']} applies after {row['deadline']} unless answered")
        return 0
    if not args.text or bool(args.path) == args.standing:
        refuse("decide.incomplete", "a decision needs its text and either at least one --path glob or --standing (not both)",
               "ade decide --path 'src/billing/*' \"why and what we decided\", or ade decide --standing \"a rule for every card\"")
    if len(args.text) > 400:
        refuse("decide.incomplete", f"a ruling is at most 400 characters; this one is {len(args.text)}",
               "keep the rule in 400 characters; the story behind it goes in the card, the commit or the docs")
    guard = lambda: fit(ctx, 1, int(args.standing))  # read again under the write lock
    if args.standing:
        who_ = answerer()
        prop = who_["by"] != "person"  # an agent or a caller ADE cannot place only proposes
        row = records.append(ctx, "decision", {"text": args.text, "paths": [], "kind": "ruling", "standing": True, "review": reviewed(args.review), **who_,
                                               **({"proposed": True, "review": records.today().isoformat()} if prop else {})}, prefix="d", guard=guard)
        print(f"{row['id']} is proposed: it governs nothing until the person answers stand (ade decide --desk)" if prop else f"{row['id']} stands for every card")
        return 0
    row = records.append(ctx, "decision", {"text": args.text, "paths": args.path, "kind": "ruling", "review": reviewed(args.review), **answerer()}, prefix="d", guard=guard)
    print(f"{row['id']} governs {', '.join(args.path)}")

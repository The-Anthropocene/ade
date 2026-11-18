# id: ade.verbs.cockpit
# what: the pilot's view: every open card with its state and next command, plus budgets; orca mode opens it in Orca too
# cmd: ade cockpit [--mode cli|orca|auto] [--follow] [--every SECONDS]
# reads: .ade/records.jsonl (journal, flight and outcome rows for the flight strips), source files
# writes: .ade/cockpit.pid (the pid of a running --follow, removed when it ends)
import os
import sys
import time

from ade import budget, config, orca, records, stages
from ade.refuse import refuse
from ade.verbs import conduct, decide, telemetry
from ade.verbs.handover import next_command


def add(p):
    p.add_argument("--mode", choices=["cli", "orca", "herdr", "auto"],
                   help="orca or herdr also opens the board in its terminal; auto does when Orca, else herdr, is running; default [cockpit] mode")


    p.add_argument("--follow", action="store_true", help="clear the screen and redraw the board each poll until Ctrl-C")
    p.add_argument("--every", type=float, default=5, help="seconds between redraws with --follow (default 5)")


def key(every):
    """Wait up to `every` seconds for one keypress and return it ('' if none); plain sleep where stdin is no terminal."""
    if not sys.stdin.isatty():
        time.sleep(every)
        return ""
    if os.name == "nt":
        import msvcrt
        end = time.time() + every
        while time.time() < end:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(0.05)
        return ""
    import select
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        mode = termios.tcgetattr(fd)  # tty.setcbreak, but TCSANOW: TCSAFLUSH would discard a key typed during the redraw
        mode[3] &= ~(termios.ECHO | termios.ICANON)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        return sys.stdin.read(1) if select.select([sys.stdin], [], [], every)[0] else ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def follow(ctx, every):
    # one key only (d), which hands off to `ade decide --desk`; the cockpit itself never writes a record
    pidfile = ctx.root / ".ade" / "cockpit.pid"  # lets a starting loop see that a cockpit terminal is already open
    try:
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        while True:
            print("\033[H\033[2J", end="")
            show(ctx)
            qs = decide.queue(ctx) + decide.waiting(ctx) if sys.stdin.isatty() else []
            if qs:
                print(f"{len(qs)} to decide: press d")
            if key(every) == "d" and qs:
                decide.desk(ctx)
    except KeyboardInterrupt:
        return 0
    finally:
        pidfile.unlink(missing_ok=True)


def run(args, cwd):
    ctx = config.load(cwd)
    if args.follow:
        return follow(ctx, args.every)
    if args.mode:
        ctx.cfg.setdefault("cockpit", {})["mode"] = args.mode
    shown = orca.on(ctx)
    if shown:
        try:
            orca.board(ctx)
        except orca.Unavailable as e:
            if args.mode not in ("orca", "herdr"):  # only a display asked for by name refuses; from ade.toml, the board below still shows
                print(f"{shown}: not shown ({e}); the board follows here")
                return show(ctx)
            refuse("cockpit.orca_unavailable", f"{shown} could not show the board: {e}",
                   f"start {shown}{' (`orca open`)' if shown == 'orca' else ''}, or use `ade cockpit`; it shows the same board here")
        print(f"the board is open in a {shown} terminal; the same board follows here")
    show(ctx)


def stage_outcome(row):
    # The short "stage outcome" form for a normal row; a row without those fields falls back to row_line.
    return f"{row['stage']} {row['outcome']}" if "stage" in row and "outcome" in row else stages.row_line(row)


def strips(ctx):
    # Twin of flight-band register.ts line(): one line per card of the latest flight, from records only: ✓ passed, ✗ other outcome, ■ under way, ○ not reached; an ended flight reads landed/parked.
    flights, journal = records.read(ctx, "flight"), records.read(ctx, "journal")
    lines = []
    for f in [f for f in flights if f["flight"] == flights[-1]["flight"]]:
        rows = [r for r in journal if r["card"] == f["card"] and r["at"] >= f["at"]]
        last = rows[-1] if rows else None
        if last and last.get("stage") == "land" and last["outcome"] == "done":
            sha = next((o["commit"] for o in reversed(records.read(ctx, "outcome")) if o["card"] == f["card"]), "")
            lines.append(f"{f['card']} landed {sha[:7]}")
        elif last and last["outcome"] == "needs-judgement":
            lines.append(f"{f['card']} parked: {last.get('note', '')}")
        else:
            outcome = {r["stage"]: r["outcome"] for r in rows if "stage" in r}
            title = records.merged(ctx, "card").get(f["card"], {}).get("title", "")
            marks, current = [], False
            for s in stages.STAGES:
                o = outcome.get(s)
                mark = "✓" if o in ("advance", "done") else "✗" if o else "○" if current else "■"
                current = current or not o
                marks.append(f"{mark} {s}")
            lines.append(f"{f['card']}{' ' + title if title else ''}  " + "  ".join(marks))
    return lines


def show(ctx):
    cards, journal = records.merged(ctx, "card"), records.read(ctx, "journal")
    print(f"ADE cockpit · {ctx.root.name}")
    for line in strips(ctx):
        print(line)
    done = [c for c in cards.values() if c["status"] == "landed" and not c.get("every")]  # one-off landed cards fold into a count so the board stays a screen
    for c in [c for c in cards.values() if c["status"] != "closed" and c not in done]:
        last = next((r for r in reversed(journal) if r["card"] == c["id"]), None)
        state = (f"resting to {c['due']}" if c.get("every") else "landed") if c["status"] == "landed" else (stage_outcome(last) if last else "new")
        nxt = "" if c["status"] == "landed" else f"  -> {next_command(ctx, c, journal)}"
        if c["status"] == "open" and conduct.waiting(ctx, c):
            q = [d for d in records.merged(ctx, "decision").values() if d.get("card") == c["id"]][-1]
            state, nxt = "held", f"  -> ade decide --answer {q['id']} {(decide.FORMS.get(q.get('rec')) or ['retry'])[0]}"
        print(f"  {c['id']:<5} {state:<24} {c['title']}{nxt}")
    if done:
        print(f"  {len(done)} landed")
    if not cards:
        print("  no cards: ade card add TITLE --path GLOB --done TEXT")
    if flights := records.read(ctx, "flight"):
        print(f"flight {flights[-1]['flight']}: {flights[-1]['card']}")
    qs = decide.queue(ctx)
    print(f"decisions: {len(records.merged(ctx, 'decision'))}, {len(qs)} open question(s)"
          + (f", next due {qs[0].get('deadline') or 'no deadline'} ({qs[0]['id']}): ade decide --queue" if qs else ""))
    if w := decide.waiting_line(ctx):
        print(w)
    print(telemetry.meter_text(ctx) or "week meters: none in use")
    if not budget.off(ctx):
        print(budget.render(budget.measure(ctx)))

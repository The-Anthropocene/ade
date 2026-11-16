# id: ade.q
# what: the output store: long output kept whole in .ade/q.sqlite and shown as head, tail and a handle; records and graph as tables
# cmd: -
# reads: .ade/q.sqlite, .ade/records.jsonl, graph.json
# writes: .ade/q.sqlite (an index and a cache, never the source of a fact; delete it at any time)
import contextlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from ade import graph, records
from ade.refuse import refuse

_ERR = re.compile(r"error|fail|traceback|exception|panic|denied|not found", re.I)


@contextlib.contextmanager
def _use(ctx):
    # Commit and close each time: an open handle would keep the file locked on Windows.
    con = _db(ctx)
    try:
        with con:
            yield con
    finally:
        con.close()


def _db(ctx):
    ctx.state.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ctx.state / "q.sqlite")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outputs (id INTEGER PRIMARY KEY, at TEXT, card TEXT, cmd TEXT, rc INTEGER,
                                            lines INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);""")
    return con


def _cfg(ctx):
    return ctx.cfg["q"]


def store(ctx, body, cmd="", rc=None, card=None):
    """Keep body whole and return its handle (q17). Outputs older than [q] keep_days go on each write."""
    now = datetime.now(timezone.utc)
    with _use(ctx) as con:
        con.execute("DELETE FROM outputs WHERE at < ?", ((now - timedelta(days=_cfg(ctx)["keep_days"])).isoformat(),))
        cur = con.execute("INSERT INTO outputs (at, card, cmd, rc, lines, body) VALUES (?, ?, ?, ?, ?, ?)",
                          (now.isoformat(timespec="seconds"), card, cmd, rc, body.count("\n") + 1, body))
    return f"q{cur.lastrowid}"


def emit(ctx, text, cmd="", rc=None, card=None):
    """Text short enough to read whole comes back as it is. Longer text is stored, and what comes back is its
    head, its tail, the lines that look like errors and the handle to page through the rest."""
    c = _cfg(ctx)
    lines = text.splitlines()
    if len(lines) <= c["cap_lines"] and len(text) <= c["cap_bytes"]:
        return text
    qid = store(ctx, text, cmd, rc, card)
    head, tail = lines[: c["head"]], lines[-c["tail"]:]
    middle = lines[c["head"]: -c["tail"]]
    errs = [f"{c['head'] + i + 1}: {x[:200]}" for i, x in enumerate(middle) if _ERR.search(x)][:10]
    clip = lambda xs: [x[:400] for x in xs]  # one huge line (minified output) must not slip past the cap
    return "\n".join(clip(head) + [f"... {len(middle)} lines not shown"
                                   + (f"; error-looking lines among them:\n" + "\n".join(errs) if errs else "")]
                     + clip(tail) + [f"[stored as {qid}, {len(lines)} lines: `ade q show {qid} --page 2`, "
                                     f"`ade q grep {qid} PATTERN`]"])


def body(ctx, qid):
    with _use(ctx) as con:
        row = con.execute("SELECT body FROM outputs WHERE id = ?", (_num(qid),)).fetchone()
    if row is None:
        refuse("q.failed", f"no stored output {qid}", "list them with `ade q list`; outputs older than [q] keep_days are dropped")
    return row[0]


def _num(qid):
    return int(str(qid).lstrip("q") or 0) if str(qid).lstrip("q").isdigit() else -1


def page(ctx, qid, n):
    size = _cfg(ctx)["page_lines"]
    lines = body(ctx, qid).splitlines()
    pages = max(1, -(-len(lines) // size))
    out = lines[(n - 1) * size: n * size]
    return "\n".join(out) + f"\n[{qid} page {n} of {pages}]"


def grep(ctx, qid, pattern):
    rx = re.compile(pattern)
    with _use(ctx) as con:
        rows = con.execute("SELECT id, body FROM outputs" + ("" if qid == "all" else " WHERE id = ?"),
                           () if qid == "all" else (_num(qid),)).fetchall()
    hits = [f"q{i}:{n}: {x[:300]}" for i, b in rows for n, x in enumerate(b.splitlines(), 1) if rx.search(x)]
    return "\n".join(hits[: _cfg(ctx)["page_lines"]]) + (f"\n[{len(hits)} matches, first {_cfg(ctx)['page_lines']} shown]"
                                                       if len(hits) > _cfg(ctx)["page_lines"] else "")


def listing(ctx):
    with _use(ctx) as con:
        rows = con.execute("SELECT id, at, card, rc, lines, cmd FROM outputs ORDER BY id DESC LIMIT 30").fetchall()
    return "\n".join(f"q{i:<5} {at}  {card or '-':<5} rc={rc if rc is not None else '-':<3} {n:>7} lines  {cmd[:60]}"
                     for i, at, card, rc, n, cmd in rows)


def _refresh(ctx, con):
    # records and graph come in as tables when their files have changed since the last load; the files stay the truth.
    sources = {"records": ctx.state / "records.jsonl", "graph": ctx.root / ctx.cfg["graph"]["out"]}
    seen = dict(con.execute("SELECT k, v FROM meta").fetchall())
    stamp = {k: str(p.stat().st_mtime_ns) if p.is_file() else "" for k, p in sources.items()}
    if seen.get("records") != stamp["records"]:
        con.executescript("DROP TABLE IF EXISTS records; CREATE TABLE records (type TEXT, id TEXT, at TEXT, row TEXT);")
        con.executemany("INSERT INTO records VALUES (?, ?, ?, ?)",
                        [(r["type"], r.get("id"), r.get("at"), json.dumps(r)) for r in records.read(ctx)])
    if seen.get("graph") != stamp["graph"]:
        g = graph.load(ctx.root, ctx.cfg) or {"files": {}}
        con.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS edges;"
                          "CREATE TABLE files (path TEXT, lang TEXT, test INTEGER, what TEXT);"
                          "CREATE TABLE edges (kind TEXT, src TEXT, dst TEXT);")
        con.executemany("INSERT INTO files VALUES (?, ?, ?, ?)",
                        [(p, f["lang"], int(f["test"]), (f["header"] or {}).get("what")) for p, f in g["files"].items()])
        edges = [("imports", p, d) for p, f in g["files"].items() for d in f["imports"]]
        edges += [(kind, src, dst) for kind in ("tests", "docs", "binds") for dst, srcs in g.get(kind, {}).items()
                  for src in srcs]
        con.executemany("INSERT INTO edges VALUES (?, ?, ?)", edges)
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", stamp.items())


def sql(ctx, query):
    """A read-only query over outputs, records, files and edges; the answer goes through emit like any long output."""
    with _use(ctx) as con:
        _refresh(ctx, con)
    con = _db(ctx)
    try:
        con.execute("PRAGMA query_only = ON")
        cur = con.execute(query)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(_cfg(ctx)["sql_rows"] + 1)
    except sqlite3.Error as e:
        refuse("q.failed", f"the query failed: {e}",
               "tables: outputs(id, at, card, cmd, rc, lines, body), records(type, id, at, row as JSON), "
               "files(path, lang, test, what), edges(kind, src, dst)")
    finally:
        con.close()
    more = len(rows) > _cfg(ctx)["sql_rows"]
    text = "\t".join(cols) + "\n" + "\n".join("\t".join("" if v is None else str(v) for v in r)
                                             for r in rows[: _cfg(ctx)["sql_rows"]])
    return emit(ctx, text + (f"\n[first {_cfg(ctx)['sql_rows']} rows; add LIMIT/OFFSET for more]" if more else ""),
                cmd=query)

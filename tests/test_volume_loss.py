"""A map whose volume goes away under a running server.

The installed 0.3.0 app died on 2026-11-23 with a SIGBUS in SQLite's `walIndexReadHdr`,
after serving a map for two days from an external drive. Sleep dismounts that drive on
macOS. A read connection to a WAL database memory-maps the `-shm` index beside it, so when
the volume went the mapped pages had nothing behind them, and the next query killed the
process: a signal, not an exception, so nothing in Python could answer it.

What is tested, each a way to look right and still crash:

  - a writer that leaves its map in WAL mode, so the next reader maps a `-shm` again;
  - a long-lived reader that opens a 0.3.0 map without taking it off WAL first;
  - a read connection that maps the database itself (`mmap_size`), which dies the same way;
  - the two stores whose "read-only" open was a read-write connection (BACKLOG 62);
  - a server that, once its connections have failed, never reads again, or that takes a
    bad statement for a lost volume;
  - and, on macOS, the thing itself: a disk image force-detached under a reader.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from dunneflow import revisions
from dunneflow.docstore import DocStore
from dunneflow.extractors.docs import extract_doc
from dunneflow.extractors.python.extract import extract_file
from dunneflow.mcp import Refused
from dunneflow.mcp import server as mcp_server
from dunneflow.mcp import tools as mcp_tools
from dunneflow.sourcedb import SourceStore
from dunneflow.store import Store, in_wal_mode, leave_wal
from dunneflow.web import serve as serve_mod

SOURCE = '''\
import sqlite3


def open_it(path):
    """Open the database."""
    return sqlite3.connect(path)


def use_it(path):
    return open_it(path)
'''

REV = "2026-11-25T10:00:00Z"


def _program(tmp_path: Path) -> Path:
    """One program directory with one revision and its documents, as `analyze` leaves it."""
    out = tmp_path / "prog"
    out.mkdir()
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "m.py").write_text(SOURCE, encoding="utf-8")
    (tree / "README.md").write_text("# Prog\n\nWhat it is for.\n", encoding="utf-8")
    store = Store(revisions.db_path(out, revisions.GRAPH, REV))
    store.write_file_facts(extract_file(tree / "m.py", tree))
    store.set_meta("revision", REV)
    store.conn.commit()
    store.close()
    docs = DocStore(revisions.db_path(out, revisions.DOCS, REV))
    docs.set_meta("revision", REV)
    docs.write_doc_facts(extract_doc(tree / "README.md", tree))
    docs.conn.commit()
    docs.close()
    return out


def _graph(out: Path) -> Path:
    return revisions.db_path(out, revisions.GRAPH, REV)


def _as_030_left_it(db: Path) -> None:
    """Put a map back in WAL mode, which is how every 0.3.0 writer left one."""
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    conn.close()
    assert in_wal_mode(db)


def _routines(db: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT * FROM routines ORDER BY id").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------ the journal mode --- #


def test_a_writer_leaves_its_map_on_a_rollback_journal(tmp_path: Path) -> None:
    """Both WAL stores, written and closed, leave nothing a reader would map."""
    out = _program(tmp_path)
    for db in (_graph(out), revisions.db_path(out, revisions.DOCS, REV)):
        assert not in_wal_mode(db), f"{db.name} was left in WAL mode by its writer"
        assert not db.with_name(db.name + "-wal").exists()
        assert not db.with_name(db.name + "-shm").exists()


def test_a_long_lived_reader_takes_a_030_map_off_wal_and_changes_no_row(
        tmp_path: Path) -> None:
    """`settle=True` converts; the rows are identical; a CLI read does not convert."""
    out = _program(tmp_path)
    db = _graph(out)
    before = _routines(db)
    _as_030_left_it(db)

    # A CLI read leaves the file as it found it: rule 20 still holds outside the server.
    Store(db, writable=False).close()
    assert in_wal_mode(db), "a read without `settle` rewrote the journal mode"

    Store(db, writable=False, settle=True).close()
    assert not in_wal_mode(db), "`settle=True` left a 0.3.0 map in WAL mode"
    assert _routines(db) == before
    assert before, "the fixture has no routines, so the comparison above is blind"


def test_a_map_that_is_refused_is_not_settled(tmp_path: Path) -> None:
    """Rule 20's exception is for a map about to be read, never for one refused."""
    from dunneflow.store import StaleGraphError

    db = _graph(_program(tmp_path))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '0:written-by-another-version' "
                 "WHERE key = 'extractor'")
    conn.commit()
    conn.close()
    _as_030_left_it(db)
    with pytest.raises(StaleGraphError):
        Store(db, writable=False, settle=True)
    assert in_wal_mode(db), "a stale map was rewritten on its way to being refused"


def test_settling_a_map_somebody_else_holds_leaves_it_alone(tmp_path: Path) -> None:
    """An analysis still writing a map is not waited on and not disturbed."""
    db = _graph(_program(tmp_path))
    _as_030_left_it(db)
    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert leave_wal(db) is False
        assert in_wal_mode(db)
        writer.rollback()
    finally:
        writer.close()


@pytest.mark.parametrize("opener", [
    lambda out: Store(_graph(out), writable=False, settle=True),
    lambda out: DocStore(revisions.db_path(out, revisions.DOCS, REV), writable=False,
                         settle=True),
    lambda out: SourceStore(_source(out), writable=False, settle=True),
], ids=["graph", "docs", "source"])
def test_a_read_connection_maps_nothing_and_writes_nothing(tmp_path: Path,
                                                           opener: Any) -> None:
    """`mmap_size` is 0, and a write is refused (BACKLOG 62 for the docs and source)."""
    store = opener(_program(tmp_path))
    try:
        assert store.conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.conn.execute("CREATE TABLE written_by_a_reader(x)")
    finally:
        store.close()


def _source(out: Path) -> Path:
    path = out / "source.db"
    store = SourceStore(path)
    store.close()
    return path


# ------------------------------------------------------ the server recovering --- #


@pytest.fixture()
def served(tmp_path: Path) -> Any:
    out = _program(tmp_path)
    live = serve_mod._Live(out / "source.db", out)
    live.open(_graph(out))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(serve_mod._Handler, live=live))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", live, out
    finally:
        httpd.shutdown()
        httpd.server_close()
        live.close()


def _get(url: str) -> tuple[int, str]:
    return _ask(url)[:2]


def _ask(url: str) -> tuple[int, str, str]:
    """Status, body, and what the answer says about the map (`X-DunneFlow-Map`)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:      # noqa: S310 -- localhost
            return r.status, r.read().decode("utf-8"), r.headers.get("X-DunneFlow-Map")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers.get("X-DunneFlow-Map")


def _lose_every_connection(live: Any) -> None:
    """What waking from sleep leaves: every held handle fails on its next read."""
    for store in live.held_stores():
        store.conn.close()


def _node_url(url: str, live: Any) -> str:
    with live.lock:
        node = live.current.store.conn.execute(
            "SELECT id FROM routines ORDER BY id LIMIT 1").fetchone()[0]
    return f"{url}/api/node?id={urllib.parse.quote(node)}"


def test_the_server_reads_again_once_the_volume_is_back(served: Any) -> None:
    url, live, _out = served
    node = _node_url(url, live)
    status, before = _get(node)
    assert status == 200 and json.loads(before)["found"], before

    _lose_every_connection(live)
    status, after = _get(node)
    assert status == 200, after
    assert after == before


def test_the_server_names_the_file_while_the_volume_is_gone(served: Any) -> None:
    url, live, out = served
    node = _node_url(url, live)
    graph = _graph(out)
    away = graph.with_name("elsewhere.db")

    _lose_every_connection(live)
    graph.rename(away)
    try:
        status, body, said = _ask(node)
        assert status == 503 and said == "lost", body
        lost = json.loads(body)["lost"]
        assert str(graph) in lost and "Reconnect it" in lost, lost
        # A route that never touches the map still answers 200, and must not say the map
        # is back: the page clears its notice on this header and on nothing else. Found in
        # a browser, where the analysis panel's poll cleared the notice within a second.
        status, _body, said = _ask(f"{url}/api/analysis")
        assert status == 200 and said == "lost", said
    finally:
        away.rename(graph)
    # And the volume coming back is all it takes.
    status, body, said = _ask(node)
    assert status == 200 and json.loads(body)["found"], body
    assert said == "readable"


def test_a_bad_statement_is_not_taken_for_a_lost_volume(served: Any) -> None:
    """Connections that still answer mean the error was the statement's own."""
    _url, live, _out = served
    held = [s.conn for s in live.held_stores()]
    assert live.recover() is None
    assert [s.conn for s in live.held_stores()] == held, "healthy connections were replaced"


def test_the_mcp_server_recovers_through_the_same_live(served: Any) -> None:
    _url, live, _out = served

    class _Session:
        pass

    session = _Session()
    session.live = live                       # type: ignore[attr-defined]
    server = mcp_server.Server(io.StringIO(), io.StringIO(), session=session)  # type: ignore[arg-type]
    calls: list[int] = []

    def body(_s: Any, _a: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        with live.lock:
            n = live.current.store.conn.execute("SELECT COUNT(*) FROM routines").fetchone()[0]
        return {"routines": n}

    tool = mcp_tools.Tool(name="probe", description="", body=body)
    _lose_every_connection(live)
    assert server._run(tool, {})["routines"] > 0
    assert len(calls) == 2, "the tool was not asked again after the reconnect"

    def bad(_s: Any, _a: dict[str, Any]) -> dict[str, Any]:
        with live.lock:
            live.current.store.conn.execute("SELECT * FROM no_such_table")
        return {}

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        server._run(mcp_tools.Tool(name="bad", description="", body=bad), {})

    graph = live.current.graph_db
    away = graph.with_name("elsewhere.db")
    _lose_every_connection(live)
    graph.rename(away)
    try:
        with pytest.raises(Refused, match="Reconnect it"):
            server._run(tool, {})
    finally:
        away.rename(graph)


# ------------------------------------------------------------- the real thing --- #

_READER = textwrap.dedent('''
    import sqlite3, sys, time
    from pathlib import Path
    from dunneflow.store import Store
    db, go, back = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    store = Store(db, writable=False, settle=True)
    count = "SELECT COUNT(*) FROM routines"
    print("before", store.conn.execute(count).fetchone()[0], flush=True)
    while not go.exists():
        time.sleep(0.05)
    try:
        store.conn.execute(count).fetchone()
        print("after detach: still read", flush=True)
    except sqlite3.DatabaseError as exc:
        print("after detach: raised", type(exc).__name__, flush=True)
    while not back.exists():
        time.sleep(0.05)
    store.reconnect()
    print("after reattach", store.conn.execute(count).fetchone()[0], flush=True)
''')


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("hdiutil") is None,
                    reason="detaches a disk image, which is macOS's hdiutil")
def test_a_volume_detached_under_a_reader_is_survived(tmp_path: Path) -> None:
    """A 0.3.0 map on a disk image, force-detached under a reading process.

    Before 0.3.1 the reader died of SIGBUS here (`returncode` -10; a shell says 138). It
    must now see an exception, keep running, and read again once the image is attached at
    the same place.
    """
    image = tmp_path / "v.dmg"
    mount = tmp_path / "mnt"
    mount.mkdir()
    subprocess.run(["hdiutil", "create", "-size", "20m", "-fs", "APFS", "-volname",
                    "dfvolumeloss", "-quiet", str(image)], check=True)

    def attach() -> None:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-quiet", "-mountpoint",
                        str(mount), str(image)], check=True)

    attach()
    try:
        out = _program(mount)
        db = _graph(out)
        _as_030_left_it(db)
        go, back = tmp_path / "go", tmp_path / "back"
        reader = subprocess.Popen([sys.executable, "-c", _READER, str(db), str(go), str(back)],
                                  stdout=subprocess.PIPE, text=True)
        assert reader.stdout is not None
        first = reader.stdout.readline()
        assert first.startswith("before "), first
        subprocess.run(["hdiutil", "detach", "-force", "-quiet", str(mount)], check=True)
        go.touch()
        second = reader.stdout.readline()
        attach()
        back.touch()
        rest, _ = reader.communicate(timeout=60)
        assert reader.returncode == 0, (
            f"the reader died with {reader.returncode} when its volume went away "
            f"(-10 is SIGBUS): {first}{second}{rest}")
        assert second.startswith("after detach:"), second
        assert rest.split() == ["after", "reattach", first.split()[1]], rest
    finally:
        subprocess.run(["hdiutil", "detach", "-force", "-quiet", str(mount)], check=False)
        time.sleep(0)

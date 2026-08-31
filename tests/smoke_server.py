"""End-to-end smoke test: launch server.py and talk to it over real MCP stdio.

test_search.py covers the query layer. Nothing covered the server itself, so
the two rules server.py claims to keep were never actually checked:

  * stdout carries only JSON-RPC. This test enforces it structurally -- a
    single stray print() breaks the client's message parsing and fails here.
  * the index is opened and verified at startup, not inside a tool call.

Assertions are against tests/fixture_wiki.xml, whose content is known and
fixed. This is deliberately NOT a relevance test: the fixture is eleven pages
and says nothing about ranking on the real corpus.

Run:  python tests/smoke_server.py [--db PATH]

With no --db it builds a throwaway index from the fixture, so it works from a
clean checkout with no dump present. --db exists so CI can reuse the index it
already built; an index built from anything other than the fixture is refused
rather than failed, since these assertions only describe fixture content.
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
FIXTURE_XML = os.path.join(TESTS_DIR, "fixture_wiki.xml")
SERVER_PY = os.path.join(PROJECT_DIR, "server.py")

sys.path.insert(0, PROJECT_DIR)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"search_wiki", "read_wiki_article", "wiki_index_info"}


def build_fixture_index(dest_dir):
    """Build an index from the fixture via the real ingest path."""
    db = os.path.join(dest_dir, "fixture.db")
    proc = subprocess.run(
        [sys.executable, os.path.join(PROJECT_DIR, "ingest.py"),
         "--xml", FIXTURE_XML, "--db", db],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"fixture build failed:\n{proc.stderr}")
    return db


def require_fixture_index(db_path):
    """Refuse an index this test's assertions do not describe.

    Every assertion below names a page from the fixture. Pointed at the real
    119 MB index, three of them fail for reasons that have nothing to do with
    the server -- so say that plainly instead of reporting a false failure.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        src = dict(conn.execute("SELECT key, value FROM build_meta")).get("source_file")
    finally:
        conn.close()
    if src != os.path.basename(FIXTURE_XML):
        raise SystemExit(
            f"{db_path} was built from {src!r}, not {os.path.basename(FIXTURE_XML)!r}.\n"
            "This test asserts against fixture content only. Run it with no --db "
            "to build a throwaway fixture index, or use test_search.py for the "
            "real corpus."
        )


def check(failures, label, condition, detail=""):
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


async def run(db_path, failures):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-u", SERVER_PY],
        env={**os.environ, "DF_WIKI_DB": db_path, "PYTHONUNBUFFERED": "1"},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # Reaching this line at all proves startup succeeded and stdout was
            # clean enough to parse as JSON-RPC.
            await session.initialize()
            print("== handshake ==")
            check(failures, "server started and completed initialize", True)

            print("\n== tools/list ==")
            names = {t.name for t in (await session.list_tools()).tools}
            check(failures, f"three tools exposed: {sorted(names)}",
                  names == EXPECTED_TOOLS, f"got {sorted(names)}")

            async def call(tool, args):
                res = await session.call_tool(tool, args)
                return "".join(c.text for c in res.content if hasattr(c, "text"))

            print("\n== wiki_index_info ==")
            info = await call("wiki_index_info", {})
            from _version import __version__
            check(failures, "reports the server version",
                  f"df-wiki-search {__version__}" in info, info[:200])
            check(failures, "reports the source file",
                  "fixture_wiki.xml" in info, info[:200])

            print("\n== search_wiki ==")
            # Stemming: 'mining' must reach pages that say 'mine'/'mines'.
            hits = await call("search_wiki", {"query": "mining"})
            check(failures, "stemmed query 'mining' finds content",
                  "DF2014:Steel" in hits or "DF2014:Magma" in hits, hits[:200])

            scoped = await call("search_wiki", {"query": "DF2012:Steel"})
            check(failures, "namespace prefix scopes the search",
                  "DF2012:Steel" in scoped, scoped[:200])

            # Punctuation must be inert, not an FTS5 operator.
            punct = await call("search_wiki", {"query": "what is steel?"})
            check(failures, "punctuation does not raise",
                  "Cannot run that search" not in punct, punct[:200])

            bad_ns = await call("search_wiki",
                                {"query": "steel", "namespaces": "Guides"})
            check(failures, "unknown namespace is rejected by name",
                  "unknown namespace" in bad_ns, bad_ns[:200])

            empty = await call("search_wiki", {"query": "!!!"})
            check(failures, "unsearchable query returns a message, not a crash",
                  "Cannot run that search" in empty, empty[:200])

            print("\n== read_wiki_article ==")
            art = await call("read_wiki_article", {"title": "Steel"})
            check(failures, "reads an article body",
                  "alloy" in art.lower(), art[:200])

            red = await call("read_wiki_article", {"title": "Steel production"})
            check(failures, "follows a redirect and says so",
                  "Redirected from" in red and "alloy" in red.lower(), red[:200])

            old = await call("read_wiki_article", {"title": "DF2012:Steel"})
            check(failures, "prefixed title resolves to its own namespace",
                  "**Namespace:** DF2012" in old, old[:200])

            missing = await call("read_wiki_article", {"title": "Nonexistent page"})
            check(failures, "missing article returns a message, not a crash",
                  "Cannot read that article" in missing, missing[:200])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="index to serve (default: build one from the fixture)")
    args = ap.parse_args(argv)

    tmp = None
    try:
        if args.db:
            db = args.db
            require_fixture_index(db)
        else:
            tmp = tempfile.mkdtemp(prefix="df-wiki-smoke-")
            db = build_fixture_index(tmp)
        print(f"server: {SERVER_PY}\nindex : {db}\n")

        failures = []
        asyncio.run(run(db, failures))

        print("\n" + "=" * 60)
        if failures:
            print(f"{len(failures)} FAILURE(S):")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("server smoke test passed")
        return 0
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

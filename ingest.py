"""Build the DF wiki search database from a MediaWiki XML dump.

Safe to run any number of times: the build goes to a temporary file, is checked
against invariants, and only then atomically replaces the target. Nothing ever
mutates a live index in place (LESSONS.md L3).

Usage:
    python ingest.py [--xml PATH] [--db PATH]
"""

import argparse
import os
import sqlite3
import sys
import time
import hashlib
import xml.etree.ElementTree as ET

import schema
import wikitext
from _version import __version__

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XML = os.path.join(PROJECT_DIR, "df_wiki.xml")
DEFAULT_DB = os.path.join(PROJECT_DIR, "df_wiki_v2.db")

# The parser version recorded in every index is the release that built it, so
# `wiki_index_info()` can say when the server is newer than the index it serves.
PARSER_VERSION = __version__


def log(msg):
    """Progress goes to stderr; stdout belongs to the MCP transport (L11)."""
    print(msg, file=sys.stderr, flush=True)


def norm_key(title):
    """MediaWiki title normalisation for lookup: _ == space, leading cap."""
    t = title.replace("_", " ").strip()
    return t[:1].upper() + t[1:] if t else t


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def iter_pages(xml_path):
    """Stream <page> elements, yielding (page_id, ns, title, timestamp, text).

    root.clear() matters: elem.clear() empties an element but does not detach
    it, so without this the root accumulates one emptied node per page.
    """
    context = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(context)
    for event, elem in context:
        if event != "end" or not elem.tag.endswith("page"):
            continue
        title_el = elem.find("{*}title")
        ns_el = elem.find("{*}ns")
        pid_el = elem.find("{*}id")
        text_el = elem.find(".//{*}text")
        ts_el = elem.find(".//{*}timestamp")
        if title_el is not None and ns_el is not None and text_el is not None:
            yield (
                int(pid_el.text) if pid_el is not None and pid_el.text else None,
                int(ns_el.text),
                title_el.text or "",
                ts_el.text if ts_el is not None else None,
                text_el.text or "",
            )
        elem.clear()
        root.clear()


def build(xml_path, db_path):
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    started = time.time()
    log(f"source : {xml_path}")
    log(f"hashing source ...")
    src_hash = sha256_of(xml_path)
    src_size = os.path.getsize(xml_path)
    if src_hash != schema.KNOWN_SOURCE["sha256"]:
        # Not fatal: a deliberate dump replacement (ADR-003) should update
        # schema.KNOWN_SOURCE, not be blocked by it. But a byte the pipeline
        # never saw before -- corruption, a truncated download, the wrong
        # file entirely -- should say so loudly rather than build silently.
        log(f"WARNING: source does not match the known dump "
            f"(sha256 {src_hash[:12]}..., expected {schema.KNOWN_SOURCE['sha256'][:12]}...; "
            f"{src_size:,} bytes, expected {schema.KNOWN_SOURCE['bytes']:,}). "
            "If this is an intentional new export, update schema.KNOWN_SOURCE "
            "once the build's invariants pass.")

    conn = sqlite3.connect(tmp_path)
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    conn.executescript(schema.SCHEMA)
    conn.executescript(schema.FTS_SCHEMA)

    seen = {}          # (ns, base) -> page_id, for collision detection
    unmapped_ns = {}
    skipped = 0
    kept = 0
    rev_min = rev_max = None
    rows = []
    link_rows = []
    cat_rows = []

    log("parsing ...")
    for page_id, ns, dump_title, ts, raw in iter_pages(xml_path):
        if ns not in schema.NS_NAMES:
            unmapped_ns[ns] = unmapped_ns.get(ns, 0) + 1
            skipped += 1
            continue

        base, full = schema.canonical_titles(ns, dump_title)
        key = (ns, base)
        if key in seen:
            # Under (ns, base_title) this should be impossible. v1 hit 185 of
            # these because it keyed on the title string alone; if it ever
            # happens again we want it loud, not silently resolved.
            log(f"  ! duplicate key {key!r} (page ids {seen[key]}, {page_id}) - skipped")
            skipped += 1
            continue
        seen[key] = page_id

        kept += 1
        aid = kept

        rtarget = wikitext.redirect_target(raw)
        if rtarget:
            body, links, cats = "", [], []
        else:
            body, links, cats = wikitext.clean(raw)

        rows.append((aid, page_id, ns, schema.NS_NAMES[ns], base, full,
                     raw, body, ts, 1 if rtarget else 0, rtarget,
                     1 if not body.strip() and not rtarget else 0))
        for t in links:
            link_rows.append((aid, t))
        for c in cats:
            cat_rows.append((aid, c))

        if ts:
            rev_min = ts if rev_min is None or ts < rev_min else rev_min
            rev_max = ts if rev_max is None or ts > rev_max else rev_max

        if kept % 2000 == 0:
            log(f"  {kept:6} pages")

    log(f"parsed {kept} pages ({skipped} skipped)")

    conn.executemany(
        """INSERT INTO articles
           (id, page_id, ns, ns_name, base_title, full_title, raw_text,
            body_text, last_modified, is_redirect, redirect_to, is_empty)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.executemany("INSERT OR IGNORE INTO links VALUES (?,?)", link_rows)
    conn.executemany("INSERT OR IGNORE INTO categories VALUES (?,?)", cat_rows)
    conn.commit()
    log(f"inserted {len(rows)} articles, {len(link_rows)} links, {len(cat_rows)} categories")

    # --- resolve redirect targets -------------------------------------------
    by_ns_base = {}
    for aid, ns, base in conn.execute("SELECT id, ns, base_title FROM articles"):
        by_ns_base[(ns, schema.parse_title(base)[1])] = aid

    resolved = []
    for aid, src_ns, target in conn.execute(
            "SELECT id, ns, redirect_to FROM articles WHERE redirect_to IS NOT NULL"):
        tgt_ns, base = schema.parse_title(target)
        if tgt_ns is not None:
            hit = by_ns_base.get((tgt_ns, base))
        else:
            # Bare target: try the source page's own namespace, then Main, then
            # DF2014. This wiki migrated its articles into DF2014 and left 1,581
            # redirect stubs behind in Main, so the DF2014 fallback is the one
            # that matters.
            hit = (by_ns_base.get((src_ns, base))
                   or by_ns_base.get((0, base))
                   or by_ns_base.get((116, base)))
        if hit:
            resolved.append((hit, aid))
    conn.executemany("UPDATE articles SET redirect_id=? WHERE id=?", resolved)
    conn.commit()

    n_red = conn.execute("SELECT COUNT(*) FROM articles WHERE is_redirect=1").fetchone()[0]
    log(f"redirects: {n_red} total, {len(resolved)} resolved, {n_red - len(resolved)} dangling")

    # --- build the FTS index in one shot ------------------------------------
    # Only content rows are indexed. Redirect stubs, empty pages, Talk, User,
    # File and Template namespaces stay in `articles` but out of the ranking --
    # 44% of v1's index was non-substantive and competed for its ten slots.
    log("building FTS index ...")
    conn.execute(f"""
        INSERT INTO articles_fts(rowid, base_title, body_text)
        SELECT id, base_title, body_text FROM articles
        WHERE {schema.INDEXABLE_PREDICATE}
    """)
    conn.commit()
    n_idx = conn.execute("SELECT COUNT(*) FROM articles_fts_docsize").fetchone()[0]
    log(f"indexed {n_idx} of {kept} rows")

    # --- provenance ---------------------------------------------------------
    meta = {
        "parser_version": PARSER_VERSION,
        "source_file": os.path.basename(xml_path),
        "source_bytes": str(src_size),
        "source_sha256": src_hash,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build_seconds": f"{time.time() - started:.1f}",
        "pages_read": str(kept + skipped),
        "pages_indexed": str(kept),
        "pages_skipped": str(skipped),
        "dump_revision_min": rev_min or "",
        "dump_revision_max": rev_max or "",
    }
    for ns, name in sorted(schema.NS_NAMES.items()):
        n = conn.execute("SELECT COUNT(*) FROM articles WHERE ns=?", (ns,)).fetchone()[0]
        if n:
            meta[f"ns_{ns}_{name}"] = str(n)
    if unmapped_ns:
        meta["unmapped_namespaces"] = repr(unmapped_ns)
    # Record source fidelity: this dump had its [[link]] syntax stripped before
    # it reached us, so the links table cannot be populated from it. Recording
    # the fact beats rediscovering it (LESSONS.md L7).
    with_brackets = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE raw_text LIKE '%[[%'").fetchone()[0]
    meta["source_has_link_markup"] = f"{with_brackets}/{kept} pages"
    meta["links_captured"] = str(len(link_rows))
    conn.executemany("INSERT INTO build_meta VALUES (?,?)", sorted(meta.items()))
    conn.commit()

    # --- verify before publishing -------------------------------------------
    log("checking invariants ...")
    schema.check_invariants(conn, kept)
    log("  all invariants passed")

    conn.executescript("PRAGMA optimize; VACUUM;")
    conn.close()

    os.replace(tmp_path, db_path)
    log(f"published {db_path} ({os.path.getsize(db_path)/1e6:.1f} MB) "
        f"in {time.time() - started:.1f}s")
    return db_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", default=DEFAULT_XML)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    try:
        build(args.xml, args.db)
    except schema.InvariantError as exc:
        log(f"BUILD REJECTED: {exc}")
        log("the temporary database was not published")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

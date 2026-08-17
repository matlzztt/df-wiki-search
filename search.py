"""Query layer for the DF wiki index.

Pure library: no MCP, no printing, no global state. Every function takes an
open connection so it can be tested directly.

Design points carried from LESSONS.md:
  L5  errors say what actually failed; no diagnosis without evidence
  L6  caller input is escaped by default, advanced syntax is opt-in
  L7  every result carries namespace and last_modified
  L8  true match counts, offset, and score are exposed
"""

import re
import sqlite3

import schema


class SearchError(Exception):
    """Raised for faults the caller can act on."""


class IndexUnavailable(Exception):
    """The database is missing or not a usable v2 index."""


# --------------------------------------------------------------------------
# query handling
# --------------------------------------------------------------------------

_RE_PHRASE = re.compile(r'"([^"]+)"')
_RE_WORD = re.compile(r"\w+", re.UNICODE)


def escape_query(text):
    """Turn arbitrary caller text into a safe FTS5 MATCH expression.

    v1 passed the raw string to MATCH, so a colon became a column filter and a
    hyphen became an operator: 12 of 22 ordinary query shapes raised. Here every
    term is quoted, which makes punctuation inert. Double-quoted spans in the
    input are preserved as phrases.
    """
    phrases = _RE_PHRASE.findall(text)
    rest = _RE_PHRASE.sub(" ", text)
    terms = []
    for p in phrases:
        words = _RE_WORD.findall(p)
        if words:
            terms.append('"%s"' % " ".join(words))
    terms += ['"%s"' % w for w in _RE_WORD.findall(rest)]
    if not terms:
        raise SearchError(
            "the query contains no searchable words "
            "(letters, digits or underscores)"
        )
    return " ".join(terms)


def _ns_clause(namespaces):
    """Return (sql_fragment, params) restricting a.ns."""
    if namespaces is None:
        ns = sorted(schema.DEFAULT_NS)
    elif namespaces == "all":
        return "", []
    else:
        ns = sorted(namespaces)
        if not ns:
            raise SearchError("no namespaces selected")
    return " AND a.ns IN (%s)" % ",".join("?" * len(ns)), list(ns)


def resolve_namespaces(names):
    """Map caller-supplied namespace names to ids, or raise with the valid set.

    v1 documented a namespace ('Guides') that matched nothing and returned a
    clean empty result, so the caller could not tell a bad filter from a real
    absence.
    """
    if not names:
        return None
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    if any(n.lower() == "all" for n in names):
        return "all"

    lookup = {v.lower(): k for k, v in schema.NS_NAMES.items()}
    lookup.update(schema.NS_ALIASES)
    out = []
    for n in names:
        hit = lookup.get(n.strip().lower())
        if hit is None:
            valid = ", ".join(sorted(schema.NS_NAMES[n] for n in schema.SEARCHABLE_NS))
            raise SearchError(f"unknown namespace {n!r}. Searchable namespaces: {valid}")
        out.append(hit)
    return out


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

# bm25() returns a negative score where more negative ranks better, so boosts
# are subtracted. Title column is weighted 10x body.
_RANK = """(
    bm25(articles_fts, 10.0, 1.0)
    - CASE WHEN lower(a.base_title) = lower(:exact) THEN 1000.0 ELSE 0.0 END
    - CASE WHEN lower(a.base_title) LIKE lower(:prefix) THEN 50.0 ELSE 0.0 END
)"""


def search(conn, query, namespaces=None, limit=10, offset=0, advanced=False):
    """Full-text search. Returns a dict with total, results, and the scope used.

    `advanced=True` passes the query to FTS5 verbatim (column filters, NEAR,
    boolean operators). Otherwise the query is escaped and every term must
    appear.
    """
    if limit < 1 or limit > 50:
        raise SearchError("limit must be between 1 and 50")
    if offset < 0:
        raise SearchError("offset must be >= 0")

    # A namespace prefix in the query is intent, not a search term. `DF2012:Steel`
    # means "Steel, in DF2012" -- v1 answered it with `no such column: DF2012`,
    # and treating it as two words searches the wrong scope for the wrong thing.
    text = query
    scoped_by_prefix = None
    if not advanced and namespaces is None and ":" in query:
        hint, rest = schema.parse_title(query)
        if hint is not None and rest.strip():
            namespaces, text, scoped_by_prefix = [hint], rest, schema.NS_NAMES[hint]

    match = text if advanced else escape_query(text)
    _, ns_ids = _ns_clause(namespaces)

    params = {"match": match, "exact": text.strip(),
              "prefix": text.strip() + "%"}
    ns_named = ""
    if ns_ids:
        keys = []
        for i, n in enumerate(ns_ids):
            params[f"ns{i}"] = n
            keys.append(f":ns{i}")
        ns_named = " AND a.ns IN (%s)" % ",".join(keys)

    where = f"""FROM articles_fts f
                JOIN articles a ON f.rowid = a.id
                WHERE articles_fts MATCH :match{ns_named}"""

    try:
        total = conn.execute(f"SELECT COUNT(*) {where}", params).fetchone()[0]
        params["lim"] = limit
        params["off"] = offset
        rows = conn.execute(f"""
            SELECT a.full_title, a.ns_name, a.last_modified,
                   snippet(articles_fts, 1, '**', '**', ' … ', 18),
                   {_RANK} AS score,
                   length(a.body_text)
            {where}
            ORDER BY score
            LIMIT :lim OFFSET :off
        """, params).fetchall()
    except sqlite3.OperationalError as exc:
        # Say what failed. v1 answered every OperationalError -- including index
        # corruption -- with "Search parser error. Try simpler keywords."
        if advanced:
            raise SearchError(
                f"FTS5 rejected the advanced query {match!r}: {exc}"
            ) from exc
        raise SearchError(
            f"the search index failed on a safely-escaped query ({exc}). "
            "This is an index fault, not a problem with your query."
        ) from exc

    return {
        "query": query,
        "match_expression": match,
        "scoped_by_prefix": scoped_by_prefix,
        "namespaces": ("all" if namespaces == "all"
                       else [schema.NS_NAMES[n] for n in
                             (sorted(schema.DEFAULT_NS) if namespaces is None
                              else sorted(namespaces))]),
        "total": total,
        "offset": offset,
        "returned": len(rows),
        "results": [
            {"title": r[0], "namespace": r[1], "last_modified": r[2],
             "snippet": (r[3] or "").replace("\n", " ").strip(),
             "score": round(r[4], 3), "length": r[5]}
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# article retrieval
# --------------------------------------------------------------------------

def _lookup(conn, title):
    """Find candidate rows for a title, most-current namespace first."""
    ns, base = schema.parse_title(title)
    order = "CASE a.ns WHEN 116 THEN 0 WHEN 0 THEN 1 WHEN 1000 THEN 2 ELSE 3 END, a.ns"
    if ns is not None:
        return conn.execute(
            f"SELECT a.id FROM articles a WHERE a.ns = ? AND lower(a.base_title) = lower(?)"
            f" ORDER BY {order}", (ns, base)).fetchall()
    return conn.execute(
        f"SELECT a.id FROM articles a WHERE lower(a.base_title) = lower(?)"
        f" ORDER BY {order}", (base,)).fetchall()


def _row(conn, aid):
    return conn.execute("""
        SELECT id, full_title, ns_name, ns, base_title, body_text,
               last_modified, is_redirect, redirect_to, redirect_id
        FROM articles WHERE id = ?""", (aid,)).fetchone()


def read_article(conn, title, follow_redirects=True):
    """Fetch one article by title. Case-insensitive, whitespace-tolerant.

    Follows redirects and reports when it did. Raises SearchError listing the
    candidates when a bare title exists in several namespaces.
    """
    if not title or not title.strip():
        raise SearchError("no title given")

    hits = _lookup(conn, title)
    if not hits:
        near = conn.execute("""
            SELECT full_title FROM articles
            WHERE lower(base_title) LIKE lower(?) ORDER BY length(base_title) LIMIT 5
        """, ("%" + title.strip() + "%",)).fetchall()
        msg = f"no article titled {title!r}"
        if near:
            msg += ". Similar titles: " + ", ".join(n[0] for n in near)
        raise SearchError(msg)

    row = _row(conn, hits[0][0])
    trail = []
    seen = {row[0]}
    while follow_redirects and row[7] and row[9] and row[9] not in seen:
        trail.append(row[1])
        seen.add(row[9])
        row = _row(conn, row[9])

    if row[7] and not row[9]:
        raise SearchError(
            f"{row[1]!r} is a redirect to {row[8]!r}, which is not in this dump"
        )

    other = [_row(conn, h[0])[1] for h in hits[1:]]
    return {
        "title": row[1],
        "namespace": row[2],
        "last_modified": row[6],
        "body": row[5],
        "length": len(row[5]),
        "redirected_from": trail or None,
        "also_in_namespaces": other or None,
    }


def build_info(conn):
    """Provenance of the index: source, vintage, counts."""
    meta = dict(conn.execute("SELECT key, value FROM build_meta"))
    ns_rows = conn.execute("""
        SELECT ns_name, COUNT(*),
               SUM(CASE WHEN id IN (SELECT id FROM articles_fts_docsize)
                        THEN 1 ELSE 0 END)
        FROM articles GROUP BY ns ORDER BY COUNT(*) DESC""").fetchall()
    meta["namespaces"] = [
        {"name": r[0], "rows": r[1], "indexed": r[2]} for r in ns_rows
    ]
    meta["default_scope"] = [schema.NS_NAMES[n] for n in sorted(schema.DEFAULT_NS)]
    return meta


def open_index(db_path):
    """Open the index read-only and verify it is a usable v2 build."""
    import os
    if not os.path.exists(db_path):
        raise IndexUnavailable(
            f"no index at {db_path}. Build one with:  python ingest.py"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        idx = conn.execute("SELECT COUNT(*) FROM articles_fts_docsize").fetchone()[0]
    except sqlite3.Error as exc:
        raise IndexUnavailable(
            f"{db_path} is not a v2 index ({exc}). Rebuild with:  python ingest.py"
        ) from exc
    if not n or not idx:
        raise IndexUnavailable(
            f"{db_path} is empty ({n} articles, {idx} indexed). "
            "Rebuild with:  python ingest.py"
        )
    return conn

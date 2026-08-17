"""Schema, namespace map, and build invariants for the DF wiki index.

See DECISIONS.md ADR-002: identity comes from the dump's <ns> and page <id>,
never from splitting the title string.
"""

# Namespace id -> name. Every entry except 116/117 comes from the dump's
# <siteinfo> block. 116/117 are undeclared there and are identified as DF2014 /
# DF2014 Talk from in-corpus evidence -- see AUDIT.md 11.1.
NS_NAMES = {
    0: "Main",              1: "Talk",
    2: "User",              3: "User talk",
    4: "Dwarf Fortress Wiki", 5: "Dwarf Fortress Wiki talk",
    6: "File",              7: "File talk",
    8: "MediaWiki",         9: "MediaWiki talk",
    10: "Template",         11: "Template talk",
    12: "Help",             13: "Help talk",
    14: "Category",         15: "Category talk",
    100: "Bloodline",       101: "Bloodline Talk",
    102: "Utility",         103: "Utility Talk",
    104: "Modification",    105: "Modification Talk",
    106: "40d",             107: "40d Talk",
    108: "Unused",          109: "Unused Talk",
    110: "23a",             111: "23a Talk",
    112: "v0.31",           113: "v0.31 Talk",
    114: "DF2012",          115: "DF2012 Talk",
    116: "DF2014",          117: "DF2014 Talk",   # not in <siteinfo>
    1000: "Masterwork",     1001: "Masterwork Talk",
}

# Namespaces whose titles appear WITHOUT a prefix in this dump. Every other
# non-main namespace carries "Name:" in <title>. Verified against the dump:
# ns 116 is bare in 3,609 of 3,610 pages.
BARE_TITLE_NS = frozenset({0, 116, 117})

# Talk namespaces are excluded from default search results.
TALK_NS = frozenset(n for n, name in NS_NAMES.items()
                    if name.lower().endswith("talk"))

# Namespaces worth putting in the search index. Everything else is still stored
# in `articles` -- we filter at read time rather than discarding at ingest
# (LESSONS.md L9) -- but User, File, Template, MediaWiki and every Talk
# namespace only diluted v1's ranking.
SEARCHABLE_NS = frozenset({
    0,     # Main
    12,    # Help
    14,    # Category
    100,   # Bloodline
    102,   # Utility
    104,   # Modification
    106,   # 40d
    110,   # 23a
    112,   # v0.31
    114,   # DF2012
    116,   # DF2014
    1000,  # Masterwork
})

# Default search scope: the current game plus Masterwork. Masterwork articles
# are among the most detailed on the wiki and belong in the default.
#
# v1 appeared to show Masterwork crowding out vanilla articles -- `steel`
# returned six Masterwork pages above the real one. That was a symptom of v1's
# defects (redirect stubs ranked as content, no stemming, no title weighting,
# the vanilla article clobbered and sitting at #11), not of Masterwork itself.
# Excluded here are the superseded version namespaces: 40d, 23a, v0.31, DF2012.
DEFAULT_NS = frozenset({0, 116, 1000})

# `Title/raw` subpages are template wrappers around the game's raw token files.
# Most are empty shells -- `{{raw|DF2014:inorganic_metal.txt|INORGANIC|STEEL}}`
# renders from a data page that is not in the export, leaving ~100 chars of
# repeated identifiers. BM25 loves those (very short, very dense) and they led
# 11 of 12 test queries. But the distribution is bimodal: the tail carries real
# token data (Masterwork:Undead/raw is 32k chars of creature definitions), so
# the cut is on substance, not on the title pattern.
RAW_SHELL_MAX_CHARS = 300

# Rows that go into the FTS index: searchable namespace, real content.
INDEXABLE_PREDICATE = (
    "ns IN (%s) AND is_redirect = 0 AND is_empty = 0"
    " AND NOT (base_title LIKE '%%/raw' AND length(body_text) < %d)"
    % (",".join(str(n) for n in sorted(SEARCHABLE_NS)), RAW_SHELL_MAX_CHARS)
)

SCHEMA = """
CREATE TABLE build_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE articles (
    id            INTEGER PRIMARY KEY,
    page_id       INTEGER NOT NULL,
    ns            INTEGER NOT NULL,
    ns_name       TEXT    NOT NULL,
    base_title    TEXT    NOT NULL,   -- title with any namespace prefix removed
    full_title    TEXT    NOT NULL,   -- canonical: "DF2014:Steel", "Main Page"
    raw_text      TEXT    NOT NULL,   -- original wikitext; cleaning is derived
    body_text     TEXT    NOT NULL,
    last_modified TEXT,
    is_redirect   INTEGER NOT NULL DEFAULT 0,
    redirect_to   TEXT,               -- raw target string from the dump
    redirect_id   INTEGER,            -- resolved articles.id, NULL if dangling
    is_empty      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ns, base_title)
);

CREATE INDEX idx_articles_full_title ON articles(full_title);
CREATE INDEX idx_articles_base_lower ON articles(lower(base_title));
CREATE INDEX idx_articles_ns         ON articles(ns);

CREATE TABLE links (
    from_id INTEGER NOT NULL REFERENCES articles(id),
    target  TEXT    NOT NULL,
    PRIMARY KEY (from_id, target)
) WITHOUT ROWID;

CREATE INDEX idx_links_target ON links(target);

CREATE TABLE categories (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    category   TEXT    NOT NULL,
    PRIMARY KEY (article_id, category)
) WITHOUT ROWID;

CREATE INDEX idx_categories_category ON categories(category);
"""

# Porter stemming so `dwarf`/`dwarves` and `mine`/`mining` share index terms
# (LESSONS.md L13). No sync triggers: the build bulk-loads and then issues a
# single 'rebuild', which cannot desync the way v1's INSERT-only trigger did
# (L4).
# base_title, not full_title: the namespace prefix must not be a search term.
# Indexing "Masterwork:Crow" put the word "masterwork" in 1,007 titles at 10x
# weight, so the query `masterwork` ranked every short Masterwork page above
# any article actually about masterwork quality. Namespace is a filter, applied
# through the join, not a term.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE articles_fts USING fts5(
    base_title,
    body_text,
    content='articles',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""


# Namespace aliases used in links and redirects but absent from <siteinfo>.
# "cv" is the wiki's alias for the current-version namespace; 1,254 redirects
# target it and every one of them dangled in v1.
NS_ALIASES = {
    "cv": 116,
    "cv talk": 117,
}

_NS_BY_LOWER_NAME = {name.lower(): n for n, name in NS_NAMES.items()}
_NS_BY_LOWER_NAME.update(NS_ALIASES)


def parse_title(title):
    """Split a wiki title into (ns_or_None, base_title), MediaWiki-normalised.

    Returns ns=None when the title carries no recognised namespace prefix, so
    the caller can apply its own fallback order.
    """
    t = title.replace("_", " ").strip()
    ns = None
    if ":" in t:
        prefix, _, rest = t.partition(":")
        hit = _NS_BY_LOWER_NAME.get(prefix.strip().lower())
        if hit is not None and rest.strip():
            ns, t = hit, rest.strip()
    # <case>first-letter</case>: the initial character is case-insensitive.
    t = t[:1].upper() + t[1:] if t else t
    return ns, t


def canonical_titles(ns, dump_title):
    """Return (base_title, full_title) for a page.

    The dump is inconsistent: most namespaces prefix <title>, but ns 0/116/117
    do not. Normalising here is what makes DF2014:Steel and DF2012:Steel both
    addressable.
    """
    ns_name = NS_NAMES.get(ns)
    if ns_name is None:
        raise KeyError(f"unmapped namespace {ns}")

    if ns in BARE_TITLE_NS:
        base = dump_title
    else:
        prefix = ns_name + ":"
        base = dump_title[len(prefix):] if dump_title.startswith(prefix) else dump_title

    full = base if ns == 0 else f"{ns_name}:{base}"
    return base, full


class InvariantError(RuntimeError):
    """A build invariant failed. The build must not be published."""


def check_invariants(conn, expected_pages):
    """Assert the build is internally consistent. Raises InvariantError.

    These are the checks whose absence let v1 ship a half-orphaned index while
    PRAGMA quick_check reported 'ok' (LESSONS.md L12).
    """
    problems = []
    q = lambda sql: conn.execute(sql).fetchone()[0]

    rows = q("SELECT COUNT(*) FROM articles")
    if rows != expected_pages:
        problems.append(f"row count {rows} != {expected_pages} pages read from dump")

    idx = q("SELECT COUNT(*) FROM articles_fts_docsize")
    want = q(f"SELECT COUNT(*) FROM articles WHERE {INDEXABLE_PREDICATE}")
    if idx != want:
        problems.append(f"FTS index holds {idx} docs but {want} rows are indexable")

    orphans = q("""SELECT COUNT(*) FROM articles_fts_docsize
                   WHERE id NOT IN (SELECT id FROM articles)""")
    if orphans:
        problems.append(f"{orphans} orphaned FTS rows")

    unmapped = q("SELECT COUNT(*) FROM articles WHERE ns_name IS NULL OR ns_name = ''")
    if unmapped:
        problems.append(f"{unmapped} rows with no namespace name")

    dupes = q("""SELECT COUNT(*) FROM (
                   SELECT ns, base_title FROM articles
                   GROUP BY ns, base_title HAVING COUNT(*) > 1)""")
    if dupes:
        problems.append(f"{dupes} duplicate (ns, base_title) keys")

    # snippet() over external content raises SQLITE_CORRUPT on orphaned rows;
    # v1 was one query-plan change away from every search failing (AUDIT 4.2).
    try:
        conn.execute("""SELECT snippet(articles_fts, 1, '[', ']', '...', 12)
                        FROM articles_fts WHERE articles_fts MATCH 'dwarf'
                        LIMIT 50""").fetchall()
    except Exception as exc:
        problems.append(f"snippet() over the index failed: {exc}")

    if problems:
        raise InvariantError("; ".join(problems))

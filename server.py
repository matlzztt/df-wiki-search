"""MCP server for the Dwarf Fortress wiki index.

Thin wrapper over search.py. All logic and all testing live there; this module
only maps tools onto it and formats output.

Two rules this server keeps that v1 did not:
  * stdout belongs to the JSON-RPC transport. Nothing prints to it, ever.
  * the index is opened and verified once, at startup. No tool call does heavy
    work, and no tool call can trigger a rebuild.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import search
import schema
from _version import __version__

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DF_WIKI_DB", os.path.join(PROJECT_DIR, "df_wiki_v2.db"))

mcp = FastMCP("Dwarf Fortress Wiki")

# Fail fast and loudly at startup rather than per call (LESSONS.md L12).
try:
    CONN = search.open_index(DB_PATH)
    _META = dict(CONN.execute("SELECT key, value FROM build_meta"))
except search.IndexUnavailable as exc:
    print(f"df-wiki-search: {exc}", file=sys.stderr)
    raise SystemExit(1)

_VINTAGE = _META.get("dump_revision_max", "")[:10]
_SCOPE = ", ".join(schema.NS_NAMES[n] for n in sorted(schema.DEFAULT_NS))
_ALL_NS = ", ".join(sorted(schema.NS_NAMES[n] for n in schema.SEARCHABLE_NS))


@mcp.tool()
def search_wiki(query: str, namespaces: str = "", limit: int = 10,
                offset: int = 0, advanced: bool = False) -> str:
    """Search the Dwarf Fortress wiki by keyword.

    Punctuation is safe: colons, hyphens, apostrophes and question marks are
    treated as ordinary text. Terms are stemmed, so "mining" also finds "mine".
    Wrap words in double quotes to require them as a phrase.

    :param query: Words to search for, e.g. 'magma pump' or "steel production".
    :param namespaces: Comma-separated scope. Defaults to Main, DF2014,
        Masterwork (the current game plus the Masterwork mod). Pass 'all' to
        search everything, or name namespaces explicitly -- superseded game
        versions are available as DF2012, v0.31, 40d and 23a, and other
        content lives in Category, Utility, Modification, Bloodline and Help.
    :param limit: Results to return, 1-50 (default 10).
    :param offset: Skip this many results, for paging past the first page.
    :param advanced: Pass the query to FTS5 verbatim, enabling NEAR(), OR, and
        column filters. Punctuation is no longer escaped for you.
    """
    try:
        ns = search.resolve_namespaces(namespaces) if namespaces else None
        res = search.search(CONN, query, namespaces=ns, limit=limit,
                            offset=offset, advanced=advanced)
    except search.SearchError as exc:
        return f"Cannot run that search: {exc}"

    if not res["results"]:
        scope = res["namespaces"]
        return (f"No matches for {query!r} in {scope}. "
                f"The index holds {_META.get('pages_indexed', '?')} pages "
                f"(wiki content up to {_VINTAGE}). "
                "Try fewer words, or namespaces='all'.")

    shown = res["offset"] + res["returned"]
    head = (f"{res['total']} matches for {query!r} in {res['namespaces']}; "
            f"showing {res['offset'] + 1}-{shown}.")
    if shown < res["total"]:
        head += f" Use offset={shown} for more."

    out = [head, ""]
    for i, r in enumerate(res["results"], res["offset"] + 1):
        date = (r["last_modified"] or "")[:10]
        out.append(f"{i}. **{r['title']}**  [{r['namespace']}, rev {date}, "
                   f"{r['length']:,} chars]")
        if r["snippet"]:
            out.append(f"   {r['snippet']}")
    return "\n".join(out)


@mcp.tool()
def read_wiki_article(title: str) -> str:
    """Retrieve the full text of one Dwarf Fortress wiki article.

    Title matching is case-insensitive and ignores surrounding whitespace.
    Redirects are followed. A bare title such as 'Steel' resolves to the
    current-version article; prefix it to target another, e.g. 'DF2012:Steel'
    or 'Masterwork:Steel'.

    :param title: Article title, e.g. 'Advanced world generation' or
        'Masterwork:Rusty steel'.
    """
    try:
        art = search.read_article(CONN, title)
    except search.SearchError as exc:
        return f"Cannot read that article: {exc}"

    head = [f"# {art['title']}",
            f"**Namespace:** {art['namespace']}  ·  "
            f"**Last revised:** {(art['last_modified'] or '?')[:10]}  ·  "
            f"**{art['length']:,} characters**"]
    if art["redirected_from"]:
        head.append(f"*Redirected from {' -> '.join(art['redirected_from'])}*")
    if art["also_in_namespaces"]:
        head.append("*Also available as: " + ", ".join(art["also_in_namespaces"]) + "*")
    head.append("---")
    return "\n\n".join(head) + "\n\n" + art["body"]


@mcp.tool()
def wiki_index_info() -> str:
    """Report what this index contains: source, vintage, and per-namespace counts.

    Use this to judge how current an answer is likely to be before relying on it.
    """
    info = search.build_info(CONN)
    lines = [
        "# Dwarf Fortress wiki index",
        "",
        f"- **Source:** {info.get('source_file')} "
        f"({int(info.get('source_bytes', 0)):,} bytes)",
        f"- **Wiki revisions:** {info.get('dump_revision_min','?')[:10]} "
        f"to **{info.get('dump_revision_max','?')[:10]}**",
        f"- **Pages stored:** {info.get('pages_indexed')} "
        f"(searchable: {sum(n['indexed'] for n in info['namespaces']):,})",
        f"- **Built:** {info.get('built_at')} by df-wiki-search "
        f"{info.get('parser_version')}",
        f"- **Server:** df-wiki-search {__version__}"
        + ("" if info.get("parser_version") == __version__ else
           "  (newer than the index above -- rebuild with `python ingest.py`)"),
        f"- **Default search scope:** {', '.join(info['default_scope'])}",
        "",
        "This dump predates DF v50. Treat mechanics as broadly current but",
        "verify anything version-sensitive -- worldgen parameters especially.",
        "",
        "| Namespace | Pages | Searchable |",
        "|---|---:|---:|",
    ]
    for n in info["namespaces"]:
        if n["rows"] >= 40:
            lines.append(f"| {n['name']} | {n['rows']:,} | {n['indexed']:,} |")
    return "\n".join(lines)


def main():
    """Console-script entry point (`df-wiki-search`).

    The index is already open by the time this runs: verification happens at
    import, so a bad or missing index fails before the transport starts rather
    than inside a tool call.
    """
    mcp.run()


if __name__ == "__main__":
    main()

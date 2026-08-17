# DF Wiki Search

Full-text search over a Dwarf Fortress wiki dump, exposed to Claude as an MCP
server.

> Every tool, table and column named below exists in the code. If you change the
> code, change this file — `AUDIT.md` records what happened last time it drifted.

## Layout

| File | Role |
|---|---|
| `df_wiki.xml` | Source MediaWiki export (64 MB, 28,880 pages) |
| `ingest.py` | Builds the database from the XML |
| `wikitext.py` | MediaWiki markup → readable text; link/category capture |
| `schema.py` | Tables, namespace map, build invariants |
| `search.py` | Query layer (pure library, no MCP) |
| `server.py` | MCP server — three tools |
| `test_search.py` | Known-answer relevance and robustness tests |
| `df_wiki_v2.db` | Built index (~119 MB) |

`code.py`, `mcp_df_wiki.py` and `df_wiki.db` are v1 and are superseded. See
`AUDIT.md`, `LESSONS.md`, `DECISIONS.md`.

## Setup

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -r requirements.txt
```

Python 3.14+. On Linux/macOS use `.venv/bin/` instead of `.venv/Scripts/`. Only
the MCP server has a dependency; ingest and search are pure stdlib.

## Obtaining the dump

`df_wiki.xml` is **not tracked in git** — it is a third-party MediaWiki export,
64 MB, produced via `Special:Export` on the Dwarf Fortress Wiki. Neither it nor
the built index ships with this repository; supply the dump and build the index
yourself. Point `ingest.py` at a different path with `--xml`.

## Build

```bash
python ingest.py
```

Safe to run repeatedly: it builds to `df_wiki_v2.db.tmp`, checks invariants, and
only then atomically replaces the live file. If a check fails, nothing is
published. Takes ~30 s.

Options: `--xml PATH`, `--db PATH`.

```bash
python test_search.py
```

Runs relevance, robustness, stemming, integrity and retrieval checks. Exit code
0 means all passed.

## Tools

### `search_wiki(query, namespaces="", limit=10, offset=0, advanced=False)`

Keyword search. Punctuation is escaped for you — colons, hyphens, apostrophes
and question marks are ordinary text. Terms are Porter-stemmed, so `mining`
finds `mine`. Double-quoted spans are treated as phrases.

A namespace prefix in the query is read as intent: `DF2012:Steel` searches the
DF2012 namespace for "Steel".

Default scope is **Main, DF2014, Masterwork**. Pass `namespaces="all"`, or name
them: `DF2012`, `v0.31`, `40d`, `23a`, `Category`, `Utility`, `Modification`,
`Bloodline`, `Help`. An unknown name is rejected with the valid list.

Returns the true match count, so `offset` can page past the first screen. Each
result carries its namespace, revision date and length.

### `read_wiki_article(title)`

Full article text. Case-insensitive, whitespace-tolerant, follows redirects and
says when it did. A bare title resolves to the current-version article; prefix
it to target another (`DF2012:Steel`, `Masterwork:Rusty steel`). If the title
exists in several namespaces the others are listed.

### `wiki_index_info()`

Source file, revision range, per-namespace counts, build time, parser version.
Use it to judge how current an answer is likely to be.

## Schema

```
build_meta(key, value)
articles(id, page_id, ns, ns_name, base_title, full_title,
         raw_text, body_text, last_modified,
         is_redirect, redirect_to, redirect_id, is_empty)
         UNIQUE (ns, base_title)
links(from_id, target)
categories(article_id, category)
articles_fts  -- FTS5 over (base_title, body_text), porter unicode61,
                 external content, no sync triggers
```

Notes that matter:

- **Identity is `(ns, base_title)`**, taken from the dump's `<ns>` and page
  `<id>`. Titles are never split to infer a namespace.
- **`raw_text` is kept** alongside `body_text`, so cleaning can be changed and
  the corpus re-derived without re-fetching the source.
- **The FTS index holds content rows only** — 9,128 of 28,880. Redirects, empty
  pages, `Title/raw` template shells, and the Talk, User, File and Template
  namespaces are stored but not indexed.
- **No sync triggers.** The build bulk-loads then populates the index in one
  statement, which cannot desync the way an insert-only trigger can.

## Namespaces

`<siteinfo>` names every namespace except 116 and 117. Those are **DF2014** and
**DF2014 Talk** — identified from in-corpus evidence (`AUDIT.md` §11.1) — and
their titles appear *unprefixed* in the dump, unlike every other namespace.
`cv:` is an alias for DF2014 used by redirects and links.

## Corpus vintage

Revisions run 2007-10-29 to **2014-09-25**. There is no post-2014 content. Most
mechanics are broadly unchanged in v50+, which is why the dump is retained
(`DECISIONS.md` ADR-003), but every result reports its revision date so callers
can judge for themselves.

**Known exception:** worldgen changed in v50+ (mesh-size semantics reversed,
zero-weighted band behaviour). For worldgen specifically, read the current
`Advanced world generation` article (v53.14) directly rather than trusting this
index.

## MCP configuration

Copy `.mcp.json.example` to `.mcp.json`, or merge the entry into your
user-level `~/.claude.json`:

```jsonc
"df-wiki-search": {
  "command": "C:\\path\\to\\wiki\\.venv\\Scripts\\python.exe",
  "args": ["-u", "C:\\path\\to\\wiki\\server.py"],
  "env": { "PYTHONUNBUFFERED": "1" }
}
```

Both paths must be absolute — the server is not launched from the project
directory. `DF_WIKI_DB` overrides the database path. All logging goes to
stderr; stdout carries only JSON-RPC.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).

Dwarf Fortress is © Bay 12 Games. This repository contains no wiki content —
only the code that indexes a dump you supply. The Dwarf Fortress Wiki's text is
licensed by its own authors under the GFDL.

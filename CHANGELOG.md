# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [SemVer](https://semver.org/), with the version tracked in
a single place: [`_version.py`](_version.py).

## [2.0.0] — 2026-08-31

The v1 codebase (`code.py`, `mcp_df_wiki.py`, `df_wiki.db`) is superseded and
removed. This is a full rebuild of the ingest and query layers — see
`AUDIT.md`, `LESSONS.md` and `DECISIONS.md` for how v1 failed and why v2 is
built the way it is.

### Added
- MediaWiki dump ingest (`ingest.py`) with atomic, invariant-checked builds:
  a bad index is never published.
- Query layer (`search.py`) with Porter stemming, escaped-by-default FTS5
  queries, namespace scoping and resolution, and redirect-following article
  retrieval.
- MCP server (`server.py`) exposing `search_wiki`, `read_wiki_article`, and
  `wiki_index_info`.
- Known-answer test suite (`test_search.py`) covering relevance, robustness,
  rejection, stemming, index integrity and retrieval; a `--structural` mode
  runs the subset that holds for any index, independent of the real corpus.
- Packaging: `pyproject.toml`, `_version.py`, and a `df-wiki-search` console
  script.
- CI (`.github/workflows/ci.yml`): builds a real index from a small fixture
  export (`tests/fixture_wiki.xml`), runs the structural suite, and drives a
  live server over real MCP stdio (`tests/smoke_server.py`) on Linux and
  Windows across the declared Python floor and latest.
- Pinned dump provenance: `schema.KNOWN_SOURCE` records the sha256 and size
  of the dump this project has always built from; `ingest.py` warns (without
  refusing to build) if `--xml` doesn't match it.

### Removed
- `code.py`, `mcp_df_wiki.py`, `df_wiki.db` (v1). `code.py` in particular
  shadowed the standard library's `code` module for anything sharing its
  `sys.path` entry.

[2.0.0]: https://github.com/matlzztt/df-wiki-search/releases/tag/v2.0.0

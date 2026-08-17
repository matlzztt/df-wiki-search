# Lessons Learned — `df-wiki-search` v1

**Companion to:** [`AUDIT.md`](AUDIT.md) — 20 findings, all evidence-backed.
**Purpose:** the audit says *what* is broken. This says *why it broke* and *what the rewrite must do differently*. Read this before writing v2.

---

## The through-line

**Every one of the twenty findings is silent.**

Nothing crashes. Both tools start, answer in 8–14 ms, and return well-formatted, plausible output. The server confidently returns a talk page as if it were an article. It confidently reports "Found 10 matching articles" while discarding half the matches. It confidently tells the caller their query syntax was bad when the real problem is a corrupted index. `PRAGMA quick_check` returns `ok` throughout.

v1 was built to *look* right, and nothing in it could tell the difference between looking right and being right. Every lesson below is a variation on that.

---

## L1 — The documentation was written *about* the code, not *from* it

**Evidence.** `wiki/README.md` was created in commit `43b8424`, which is **newer** than the last commit touching `code.py`. It documents three tools; one exists. It documents three tables; none exist. The docstring enumerates a namespace `'Guides'` that matches zero rows — and 598 article bodies contain the literal string `Category:` after sanitisation, including `Category:Guides`. The value looks like it was read out of corpus prose rather than out of `SELECT DISTINCT namespace`.

**Why it happened.** The README was produced as a description of what the subproject *ought* to offer. Nobody ran the three documented function names against the source. Grepping for `get_wiki_article_by_title` takes two seconds and returns nothing.

**Rule for v2.**
- Every documented symbol must be greppable in source. This is a CI check, not a habit.
- Every enumerated value in a docstring must be generated from a query against the data, never typed by hand.
- Docstrings are the model-facing API contract — treat them with the rigour of a type signature, because that is exactly what they become in the tool schema.

---

## L2 — Identity was reconstructed from a display string when the source provided a key

**Evidence.** `code.py:108`:

```python
if ":" in raw_title:
    namespace = raw_title.split(":")[0]
```

Every page in the XML carries an authoritative `<ns>` element and a unique page `<id>`. The parser uses neither.

**What that one line cost:**

| Consequence | Scale |
|---|---|
| Articles silently overwritten by their own talk pages | 185 |
| Distinct wiki namespaces merged into `Main` (ns 0 / 116 / 117) | 3 |
| Junk namespaces invented from colons in ordinary titles | 12 |
| Redirects left dangling on a `DF2014:` prefix that no row has | 270 |
| Current-version articles rendered unfilterable | 3,610 |

This is the single most expensive decision in the codebase, and it is one line.

**Why it happened.** The title *looks* like it contains the namespace, and for `DF2012:Steel` it does. The heuristic is right often enough to pass casual inspection and wrong in exactly the cases that matter — bare-titled current-version content, and article titles that happen to contain a colon (`Dwarf cancels task`).

**Rule for v2.** Never derive identity or classification from a human-readable string when the source has a field for it. Titles are for humans; `<ns>` and `<id>` are for machines. Key the table on `(ns, title)` or on page id, and carry `ns` as an integer with a lookup table for names.

---

## L3 — "Idempotent-looking" is not idempotent

**Evidence.** `CREATE TABLE IF NOT EXISTS` plus `INSERT OR REPLACE` reads as safe to re-run, and both READMEs document `python wiki/code.py` as *the* build command with no warning. Running it a second time doubled the FTS index to 25,038 documents, 12,704 of them permanently orphaned. Nothing reported it. The documented happy path is what broke the index.

**Why it happened.** The two SQLite idioms that *sound* idempotent aren't, in combination: `IF NOT EXISTS` skips schema creation, so the second run appends to a live index; `INSERT OR REPLACE` deletes and re-inserts with a **fresh rowid**, orphaning the old FTS entry (see L4). Every article id in the database is from the second pass.

**Rule for v2.** Builds are atomic and reproducible. Build into a temporary database, verify invariants, then atomically swap it into place. Never mutate a live index incrementally. A build command that is unsafe to run twice is a bug, not a caveat for the README.

---

## L4 — External-content FTS5 needs the complete trigger set, or it desyncs invisibly

**Evidence.** `code.py:38` defines `AFTER INSERT` and nothing else. There is no `AFTER DELETE`, no `AFTER UPDATE`. With `INSERT OR REPLACE`, every replaced row leaves a permanent orphan.

Three compounding effects, none of them visible:

1. `bm25()` computes IDF over 25,038 documents in which each real document appears twice — ranking is derived from a corpus that does not exist.
2. The inner join then silently drops the orphans. Query `steel`: 572 FTS matches → 274 after join. Because `LIMIT 10` applies *after* the join, the caller still gets ten results and no indication that 298 were discarded.
3. `snippet()` on an orphaned row raises `SQLITE_CORRUPT` — `database disk image is malformed` — because it cannot fetch content for a rowid that no longer exists. **The shipped query survives only by query-plan luck.** The current planner filters before computing the snippet. A different SQLite build, an `ANALYZE`, or a selectivity change reorders it, and then every search returns "Search parser error."

`PRAGMA quick_check` returns `ok` for all of this. It is a page-structure check; it knows nothing about external-content consistency.

**Rule for v2.** Either define the full `INSERT`/`UPDATE`/`DELETE` trigger set, or — better for a bulk-load pipeline — skip triggers entirely and issue `INSERT INTO fts(fts) VALUES('rebuild')` once after the load completes. Verify with a reconciliation query, never with an integrity pragma:

```sql
SELECT (SELECT COUNT(*) FROM wiki_search_index_docsize) AS idx,
       (SELECT COUNT(*) FROM wiki_articles)             AS rows;
-- must be equal; fail the build if not
```

---

## L5 — Errors were classified by where they were caught, not by what they were

**Evidence.** `mcp_df_wiki.py:64` — a single `except sqlite3.OperationalError` returns:

> `Search parser error. Try simpler keywords.`

That one message is returned for a colon in the query, a hyphen, an apostrophe, a slash, a question mark, an empty string, **and** for index corruption. Twelve of twenty-two tested query shapes hit it.

**Why it's the worst finding in the file.** The message is a *diagnosis*, and it is confidently wrong. It tells the caller the fault is theirs and that the fix is to simplify. A model receiving it will dutifully simplify its query — which cannot fix a punctuation-hostile parser and cannot fix a corrupted index. The error message actively steers the caller away from the real problem.

**Rule for v2.** Distinguish user-input faults from system faults, and never emit a diagnosis you have not established. If the cause is unknown, say the cause is unknown and surface the raw error. A wrong explanation is worse than no explanation, because it terminates investigation.

---

## L6 — Raw caller input was passed straight to a query language

**Evidence.** The `query` string goes directly to FTS5 `MATCH`. FTS5 treats `:` as a column filter, `-` and `NOT` as operators, `*` as a wildcard, and rejects unbalanced quotes and most punctuation.

The self-defeating case: `read_wiki_article`'s docstring recommends titles like `"DF2012:Starting build"`, and its not-found message tells the caller to verify the title with `search_wiki_articles`. Searching that exact string fails with `no such column: DF2012`. **The documented recovery path is broken for exactly the titles that need it.**

This is a usability defect, not a security one — the SQL is parameterised, and the `'; DROP TABLE` case dies harmlessly in the FTS5 tokenizer.

**Rule for v2.** Escape and quote caller input by default; a search box should accept anything a human would type. If FTS5 operator syntax is worth exposing, make it opt-in through a separate parameter with a documented grammar — never by leaking the underlying engine's parser through the default path.

---

## L7 — The artifact never recorded its own provenance

**Evidence.** No build metadata exists anywhere: no source file hash, no build timestamp, no dump date range, no row counts, no parser version. Consequence, from this project's own history: several rounds were spent arguing whether the DF wiki was stale, conducted from inside a database whose newest revision is **2014-09-25** — a fact that took a `SELECT MAX(last_modified)` to establish and that no document recorded.

The bitter detail: `last_modified` is captured correctly for all 12,334 rows and then **exposed by no tool.** The one field that answers "how old is this?" was collected and hidden.

**Rule for v2.** Ship a `build_meta` table and a tool that returns it:

```
source_file, source_bytes, source_sha256, built_at, parser_version,
dump_revision_min, dump_revision_max, page_count, row_count, per-namespace counts
```

And put vintage at the point of use: every search result and every article read returns its `last_modified` and its namespace label. A caller seeing `DF2014` and `2014-08-27` can judge for itself. Provenance buried in a README is provenance nobody reads.

---

## L8 — Silent truncation, reported with a confident count

**Evidence.** `Found {len(rows)} matching articles` where `rows` is capped at a hardcoded `LIMIT 10`. There is no total, no offset, no pagination, no relevance score. For the query `steel`, the actual current-version `Steel` article ranks **#11** — one past the cutoff — while four of the ten visible results are `#REDIRECT` stubs. **No query the caller can express reaches it.**

**Rule for v2.** Report the true match count, support `offset`, expose the relevance score, and make the limit a parameter with a documented default. If results are truncated, say so in the payload.

---

## L9 — Cleaning was lossy, and the lossy copy was the only copy

**Evidence.** Two separate lossy transforms, one of which was invisible.

*Upstream, before this codebase:* `df_wiki_compressed.xml` is a **trimmed** dump, not a raw MediaWiki export. Its `[[link]]` markup was already stripped — only 21 of 12,519 pages retain it — and 16,361 pages were dropped entirely, including the whole `40d`, `23a`, `v0.31`, `Category`, and `File` namespaces. Nothing recorded that this had happened, so the audit initially misattributed the missing links to `code.py`. *(Corrected in `AUDIT.md` §2.)*

*In this codebase:* `sanitize_wiki_text` deleted every `{{template}}` outright. That is where the real loss lives — **10.8% of the corpus text sits inside templates**, and it is data, not chrome. The `Creature` article is 106,079 raw characters, 96,897 of them inside `{{...}}`; v1 stored **3,541**. Infoboxes like `{{Metal|name=Adamantine|properties=* Material value 300}}` *are* the article.

Meanwhile the cleaning that was attempted is incomplete — measured across 11,037 non-empty bodies:

| Residue | Share |
|---|---:|
| `'''` bold markup | 29.5% |
| `==` headings | 17.2% |
| orphan `}}` from nested templates | 13.6% |
| raw wiki tables `{\|`…`\|}` | 5.9% |
| mangled `File:`/`Image:` embeds | 5.5% |

So the transform destroyed the structured data (links) while preserving the noise (markup). The 647 rows with raw table markup matter disproportionately — MediaWiki tables are where the numeric data lives: metal properties, creature stats, item values.

**Rule for v2.** Store raw wikitext alongside cleaned text. Clean at read time, or into a derived column that can be regenerated. A lossy transform must never be the only copy — cleaning strategy will change, and it must be possible to re-derive without re-fetching a source that may no longer be obtainable.

**And record what the source itself already lost.** The trimmed dump looked like a dump. Nothing in the filename, the file, or any document said 16,361 pages and all link markup had been removed, so an audit reasoning carefully from the artifact still reached the wrong conclusion about *where* the loss occurred. v2 writes `source_sha256`, `source_has_link_markup`, and `pages_read` into `build_meta` for exactly this reason: the next person should be able to tell a trimmed source from a complete one without re-deriving it.

---

## L10 — Module names have operational consequences

**Evidence.** `mcp_df_wiki.py:11-12` does `sys.path.insert(0, PROJECT_DIR)` then `from code import ensure_database_ready`. Verified: `import code` resolves to `C:\DFProject\wiki\code.py`, and `code.interact` is unavailable **for the entire server process**. Anything reaching for the stdlib `code` module — some `pdb` paths, REPL tooling, several debuggers — breaks inside this process.

`code.py` is also uninformative enough that `README.md:30` has to explain what it does.

**Rule for v2.** Never name a module after a stdlib module. Name modules for what they do (`ingest.py`, `search.py`, `schema.py`). Prefer package-relative imports over `sys.path` mutation.

---

## L11 — On a stdio server, stdout belongs to the transport

**Evidence.** `code.py:122` and `code.py:129` both `print()`. `FastMCP.run()` defaults to stdio, and the desktop config launches this server over stdio. Those prints fire during `parse_and_populate` — which `ensure_database_ready` (`code.py:75-77`) can invoke **from inside a tool call**. If the tables were ever missing, the server would parse a 24 MB XML file on the request path — a multi-minute hang with no progress signal — while writing non-JSON into the JSON-RPC stream.

**Rule for v2.** All logging to stderr. No `print()` in any module the server imports. Never do heavy or unbounded work on the request path; if the database is not ready, fail fast with an actionable message and let a separate build command fix it.

---

## L12 — A liveness check is not a health check

**Evidence.** `ensure_database_ready` (`code.py:70-73`) checks that two tables *exist*. Both existed throughout, while the index was half-orphaned and 185 articles were missing. It cannot detect either. It runs on **every tool call** — a connection plus two `sqlite_master` queries — and can repair nothing it might find.

**Rule for v2.** Health checks assert invariants, not existence:

- `COUNT(wiki_search_index_docsize) == COUNT(articles)` — no orphans
- `COUNT(articles) == build_meta.page_count` — nothing lost at load
- zero rows where `ns` is unmapped

Run them at build time (fail the build) and at startup (fail fast, loudly). Not per request.

---

## L13 — No stemming was configured, and nobody checked recall

**Evidence.** The FTS5 table declares no tokenizer, so it uses default `unicode61` with no stemming. Morphological variants are entirely separate index terms:

| Term A | hits | Term B | hits | overlap |
|---|---:|---|---:|---:|
| `dwarf` | 1,865 | `dwarves` | 1,718 | 1,031 |
| `mine` | 116 | `mining` | 258 | 79 |
| `forge` | 285 | `forging` | 50 | 42 |
| `goblin` | 345 | `goblins` | 252 | 115 |

A search for `dwarf` misses roughly 690 articles that say `dwarves`. On a wiki about dwarves.

**Why it went unnoticed.** Every query returns ten plausible results, so recall failures are invisible from the outside. You only find them by measuring.

**Rule for v2.** Configure `tokenize='porter unicode61'`. More importantly: **relevance needs a test set.** A dozen known-answer queries (`"steel" must return the Steel article in the top 3`) asserted in CI would have caught this, the orphan pollution, *and* the `LIMIT 10` cutoff problem in one pass.

---

## What v1 got right

Worth naming, so the rewrite doesn't churn on settled decisions:

- **Streaming `iterparse`** over the 24 MB dump rather than loading a DOM — correct instinct. *(Minor flaw: `elem.clear()` empties each element but does not detach it, so the root retains 12,520 emptied `<page>` objects. The 24 MB of text is freed, which is the bulk. Add `root.clear()` and move on.)*
- **SQLite FTS5 with bm25** — the right engine for this corpus and workload. 8–14 ms queries.
- **`__file__`-relative path resolution** — the server works regardless of cwd. This is why the MCP wiring is the one area of the project with zero findings.
- **Parameterised SQL throughout** — no injection surface anywhere.
- **Venv-pinned interpreter in the MCP config** — correct and already fixed in `6f72448`.
- **The two-tool shape** — broad search, then targeted read — is the right decomposition for a model-facing retrieval server. Keep it; fix what the tools return.

---

## Requirements for v2

Derived from the above. Ordered by what unblocks what.

### Ingest
1. Key rows on `(ns, title)`; store `page_id` from the dump. Never split titles.
2. Carry `ns` as an integer plus a `ns_name` from the lookup table in `AUDIT.md` §11.3 — including `116: "DF2014"`, `117: "DF2014 Talk"`, which `<siteinfo>` omits.
3. Store raw wikitext **and** cleaned text. Cleaning must be re-derivable.
4. Build into a temp database; verify; atomic swap. Safe to run any number of times.
5. Populate `build_meta` with source hash, timestamps, dump revision range, and counts.
6. Classify rows at ingest: `is_redirect`, `is_empty`, plus resolved redirect target. 44% of the current corpus is non-substantive and competes for result slots.

### Index
7. `tokenize='porter unicode61'`.
8. No sync triggers — bulk load, then `INSERT INTO fts(fts) VALUES('rebuild')`.
9. Reconciliation assertion; fail the build on mismatch.
10. Exclude or down-rank redirects, empties, and `Template:` pages by default.

### Tools
11. Escape caller input by default; opt-in advanced syntax via a separate parameter.
12. Return `namespace`, `last_modified`, and score on every result. Vintage is visible at point of use.
13. Report true match count; support `limit` and `offset`.
14. `read_article(title, ns=None)` — case-insensitive and trimmed, disambiguating across namespaces instead of silently picking one.
15. Expose `get_build_info()`.
16. Errors state what actually failed. No diagnosis without evidence.

### Hygiene
17. Rename modules; nothing shadowing the stdlib.
18. Logging to stderr only.
19. Startup asserts invariants and fails loudly; nothing heavy on the request path.
20. A known-answer relevance test set in CI.

---

## The one-line version

> **v1's defects were not caused by ignorance of the domain. They were caused by a system with no way to notice it was wrong — and documentation that described intentions rather than behaviour.** Build v2 so that being wrong is loud.

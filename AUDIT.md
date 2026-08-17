# `df-wiki-search` — Documentation vs. Code Conformance Audit

**Date:** 2026-08-17
**Scope:** `wiki/README.md`, `README.md` (wiki sections), tool docstrings in `wiki/mcp_df_wiki.py`, and the behaviour of `wiki/code.py` + `wiki/df_wiki.db` that those documents describe.
**Method:** every documented claim was executed — against the live MCP server where possible, otherwise against the shipped SQL and the source XML.
**Mutations:** none. All database access used read-only handles or non-writing statements; `df_wiki.db` mtime is unchanged at `Jul 19 22:25`.

---

## 0. Verdict

| Layer | Claims checked | Wrong |
|---|---|---|
| `wiki/README.md` — tool surface | 3 | 3 |
| `wiki/README.md` — schema | 3 | 3 |
| `wiki/README.md` — workflows | 3 | 1 |
| Tool docstrings (model-facing) | 5 | 4 |
| `README.md` — wiki sections | 5 | 1 |

The headline: **of the three tools `wiki/README.md` documents, one exists.** The one real retrieval tool is documented under a name it does not have. Separately, testing the documented claims surfaced five defects in the data layer, two of which mean the server returns confidently wrong content.

Nothing here is a startup failure. Both tools run, respond in 8–14 ms, and return plausible-looking output. Every problem below is silent.

---

## 1. Tool surface — `wiki/README.md:28`

| Documented | Reality |
|---|---|
| `search_wiki_articles(query)` | Exists — but the real signature is `(query, namespace=None)` (`mcp_df_wiki.py:15`). The `namespace` filter is undocumented in the README. |
| `get_wiki_article(article_id)` | **Does not exist.** No id-based accessor exists in any code path. |
| `get_wiki_article_by_title(title)` | **Does not exist.** The real tool is `read_wiki_article(title)` (`mcp_df_wiki.py:69`). |

`read_wiki_article` — the only retrieval tool that ships — appears nowhere in either README under its actual name.

### Provenance

`git log` shows `wiki/README.md` was created in `43b8424` *("Add README files … with model-oriented documentation")*, which is **newer** than `265429c`, the last commit to touch `code.py`. The README was written after the code existed and still describes an API the code never had. It was not derived from the source.

---

## 2. Schema — `wiki/README.md:18`

All three documented tables are fictional.

| Documented | Actual |
|---|---|
| `pages` — "titles, namespaces, and raw content" | `wiki_articles(id, title, namespace, body_content, last_modified)` |
| `page_text` — "indexed text fields" | `wiki_search_index` — FTS5 virtual table, `content='wiki_articles'`, `content_rowid='id'` |
| `page_links` — "optional link tables for semantic graph construction" | **Never created by any code path.** |

Undocumented and present: the `last_modified` column, the `after_wiki_insert` trigger, and four FTS5 shadow tables (`_config`, `_data`, `_docsize`, `_idx`).

### The `page_links` gap is unimplementable from *this* dump

`wiki/README.md:36` offers the workflow *"trace cross-references between pages via wiki links for entity mapping."*

> **Correction (2026-08-17).** This section originally attributed the missing link data to `sanitize_wiki_text` (`code.py:60`), which does rewrite `[[Target|Label]]` → `Label`:
>
> ```python
> cleaned = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', cleaned)
> ```
>
> That attribution was wrong. `df_wiki_compressed.xml` is **not a raw MediaWiki dump** — its link syntax had already been stripped upstream, before `code.py` ever ran. Only **21 of 12,519** pages in it still contain `[[`. The v1 regex was operating on text that mostly had no links left to destroy.
>
> The original conclusion held for the wrong reason: no link graph was recoverable, but the loss happened one stage earlier than stated. With the untrimmed `df_wiki.xml` (28,880 pages, 60% retaining `[[` markup), the link graph *is* buildable — v2 captures **149,730 links** and **5,995 categories**. See §12.

The other two documented workflows (`README.md:34`, `:35` — term search, passage extraction) are supported.

---

## 3. Docstrings — the model-facing contract

The docstrings matter more than the README: they are what reaches the tool schema and what a model actually reads. Verified schema as served:

```json
search_wiki_articles  {"properties":{"query":{"type":"string"},"namespace":{"type":"string"}},"type":"object"}
read_wiki_article     {"properties":{"title":{"type":"string"}},"type":"object"}
```

Note there is no `required` array on either — `query` and `title` are advertised as optional. Omitting them raises a `TypeError` inside the tool rather than being rejected at validation.

### 3.1 `namespace` — one documented value can never match

`mcp_df_wiki.py:21` — *"Use `'Main'`, `'DF2012'`, `'Masterwork'`, or `'Guides'`."*

**`'Guides'` matches 0 rows.** Confirmed live against the running server:

```
search_wiki_articles(query="magma pump", namespace="Guides")
→ "No wiki articles found matching: 'magma pump' in namespace 'Guides'"
```

A model following the docstring receives a clean empty result with no signal that the value is invalid, and will reasonably conclude the wiki has no guide content.

Likely origin: 598 article bodies still contain the literal string `Category:` after sanitisation (see §6), including `Category:Guides`. The docstring appears to have been written from stray category text in the corpus rather than from `SELECT DISTINCT namespace`.

**Actual namespace values, none documented:**

| Value | Rows | | Value | Rows |
|---|---:|---|---|---:|
| `Main` | 5182 | | `Utility` | 46 |
| `DF2012` | 3720 | | `Masterwork Talk` | 19 |
| `Masterwork` | 1896 | | `Utility Talk` | 18 |
| `Template` | 1080 | | `Help` | 13 |
| `DF2012 Talk` | 336 | | `Stonesense` | 4 |

Plus 12 junk namespaces invented from colons appearing inside ordinary article titles — `Dwarf cancels task` (4), `Dwarf cancels Construct Building` (2), `Cat cancels Store Item in Stockpile` (1), `Stories/Goblin entryism` (1), `Sandbox` (2), and others.

### 3.2 `'Main'` is not a namespace — it is three namespaces merged

`namespace` is derived by string-splitting the title (`code.py:108`):

```python
if ":" in raw_title:
    namespace = raw_title.split(":")[0]
else:
    namespace = "Main"
```

The authoritative `<ns>` element present on every page in the XML is **ignored**. Consequence — three distinct wiki namespaces collapse into `Main`:

| XML `<ns>` | Meaning | Pages | Labelled |
|---|---|---:|---|
| `0` | true main namespace | 1,696 | `Main` |
| `116` | **current-version articles** | 3,610 | `Main` |
| `117` | current-version talk pages | 83 | `Main` |

There is therefore **no way to filter to current-version article content** — the primary thing this server exists to retrieve — and no way to exclude talk pages from it.

Note `<siteinfo>` in the dump declares namespaces only up to `1001` and never declares 116 or 117, so no name for them is available from the `<siteinfo>` block itself. That is a reason the mapping needs a decision, not a reason the `<ns>` value should be discarded — see §11, where the names are recovered from the corpus.

### 3.3 `read_wiki_article` — "the full, cleaned body text of a specific page"

For **185 titles this returns a different page's text.** Root cause in §4. Confirmed live:

```
read_wiki_article(title="Advanced world generation")
→ "I updated the section on Mesh sizes and weighted ranges according to the
   research I did. I'm not experienced with wiki editing…"
```

That is talk-page chatter. The actual 64,896-character article is not in the database at all.

Also undocumented: matching is **exact, case-sensitive, and untrimmed**.

| Input | Result |
|---|---|
| `Steel` | ✅ found |
| `steel`, `STEEL` | ❌ not found |
| `' Steel'`, `'Steel '` | ❌ not found |
| `DF2012:Steel` | ✅ found |
| `df2012:steel` | ❌ not found |

The failure message ("*Use search_wiki_articles to verify the title*") is sound advice, but §5 shows that the recommended recovery path fails for exactly these titles.

### 3.4 `search_wiki_articles` — "returns highly ranked matching article titles"

Undocumented hard `LIMIT 10` (`mcp_df_wiki.py:35`, `:45`), and the ranking is degraded by index pollution (§4.2). Worked example — query `steel`:

```
 1. Masterwork:Rusty steel               (mod content)
 2. Masterwork:Caravanserai arms bazaar  (mod content)
 3. Steel Bars                           '#REDIRECT Steel'
 4. Masterwork:Steel                     '#REDIRECT DF2012:Steel'
 5. Masterwork:Rusy steel/raw            '#REDIRECT Masterwork:Rusty s'
 6. Masterwork:Urist's steel emporium    '#REDIRECT Masterwork:Urist's'
 7. Masterwork:Bloodsteel                (mod content)
 8. Masterwork:Ammo caster               (mod content)
 9. Masterwork:Urist's steel             (mod content)
10. DF2012:Steel                         (v0.34 article)
────────────────────────────────────────── LIMIT 10 cutoff
11. Steel                                ← the actual current-version article
```

The article a caller wants ranks **#11** — one position past the cutoff, therefore invisible. Four of the ten visible results are redirect stubs.

### 3.5 `snippet()` markers are indistinguishable from truncation

`mcp_df_wiki.py:30` passes `snippet(wiki_search_index, 1, '...', '...', '...', 15)`. The signature is `snippet(table, col, start_marker, end_marker, ellipsis, tokens)` — all three markers are `'...'`, so the matched term is not visually distinguishable from elided text. Line 61 then wraps the whole thing in two more `...`. Column index `1` is correct (`body_content`).

---

## 4. Data-layer defects found while testing documented behaviour

### 4.1 — 185 articles silently overwritten

`title TEXT UNIQUE` (`code.py:20`) combined with `INSERT OR REPLACE` (`code.py:117`), while namespace comes from the title string rather than `<ns>`. When an ns=116 article and its ns=117 talk page share a bare title, the parse order decides which survives. Last write wins:

```
XML pages with title+text : 12,519
distinct titles           : 12,334
colliding titles          :    185
pages silently overwritten:    185
```

Worst losses (`title → [(ns, body length)]`):

```
'Adventurer mode'            ns116: 89,660 chars  ← lost to  ns117:   538
'Quickstart guide'           ns116: 81,798 chars  ← lost to  ns117: 8,069
'Advanced world generation'  ns116: 64,896 chars  ← lost to  ns117:   696
'Creature token'             ns116: 60,912 chars  ← lost to  ns117: 1,566
'Tilesets'                   ns116: 48,233 chars  ← lost to  ns117:   126
'Minecart'                   ns116: 46,429 chars  ← lost to  ns117:     0
'Starting build'             ns116: 45,740 chars  ← lost to  ns117: 5,474
'Personality trait'          ns116: 43,657 chars  ← lost to  ns117:   252
'Trap design'                ns116: 38,250 chars  ← lost to  ns117:   490
'Strange mood'               ns116: 34,829 chars  ← lost to  ns117:   778
'Weapon'                     ns116: 34,008 chars  ← lost to  ns117:   363
```

`success_count` (`code.py:120`) increments per `execute`, not per surviving row, so the completion message reports 12,519 pages indexed when 12,334 exist. The loss is invisible at build time.

### 4.2 — The FTS index is double-populated; ~50% of every search is discarded

The sync trigger (`code.py:38`) is `AFTER INSERT` only. There is no `AFTER DELETE` or `AFTER UPDATE`. `INSERT OR REPLACE` deletes the old row and inserts a new one with a **fresh rowid**, so every replaced row leaves a permanent orphan in the index. `wiki/code.py`'s `__main__` calls `parse_and_populate()` unconditionally, and the database was built twice:

```
FTS index documents : 25,038   (docsize id range 1 – 25,038)
reachable articles  : 12,334   (article id range 12,521 – 25,038)
orphaned index rows : 12,704
```

Every article id is from the second pass; ids 1–12,520 are entirely orphaned. Two consequences:

**Ranking is computed over a doubled corpus.** `bm25()` derives IDF from 25,038 documents in which each real document appears twice.

**The inner join then silently drops the orphans.** Measured:

| Query | FTS matches | After join | Dropped |
|---|---:|---:|---:|
| `"advanced world generation"` | 80 | 38 | 42 |
| `"magma pump"` | 16 | 8 | 8 |
| `steel` | 572 | 274 | 298 |

Because `LIMIT 10` is applied after the join, callers still receive ten results and see no sign of the loss — only degraded relevance, as in §3.4.

**`PRAGMA quick_check` returns `ok`.** The file is not corrupt; this is a logical inconsistency between the index and its external content table, which no integrity check will report.

**Latent hard failure.** `snippet()` on an external-content FTS5 table must fetch the source row by rowid. For orphaned rows that row is gone, and FTS5 raises `SQLITE_CORRUPT`:

```
sqlite3.OperationalError: database disk image is malformed
```

The shipped query survives only because the current planner (`SCAN f VIRTUAL TABLE` → `SEARCH a USING INTEGER PRIMARY KEY`) evaluates `snippet()` after the join filter. 33 ordinary queries and 25 namespace-filtered queries all passed. But that is a query-plan accident, not a guarantee — a different SQLite build, an `ANALYZE`, or a selectivity change could reorder it, at which point **every search returns `"Search parser error. Try simpler keywords."`** for what is actually index corruption.

### 4.3 — 44% of the indexed corpus is non-substantive

All of it competes for the ten result slots:

| Category | Rows | Share |
|---|---:|---:|
| `#REDIRECT` stubs | 2,721 | 22.1% |
| Empty bodies | 1,297 | 10.5% |
| `Template:` pages | 1,080 | 8.8% |
| Talk pages | 375 | 3.0% |
| **Combined** | **5,473** | **44.4%** |
| Total rows | 12,334 | |

Nothing filters these at index or query time.

---

## 5. Query robustness — 12 of 22 syntax cases raise

The `query` string is passed directly to FTS5 `MATCH` with no escaping. FTS5 treats `:` as a column filter, `-`/`NOT` as operators, `*` as a wildcard, and rejects unbalanced quotes and most punctuation.

| Case | Input | Result |
|---|---|---|
| plain two words | `magma pump` | ✅ 10 rows |
| **title with colon** | `DF2012:Steel` | ❌ `no such column: DF2012` |
| **namespaced phrase** | `Masterwork:Steel` | ❌ `no such column: Masterwork` |
| **hyphenated** | `well-being` | ❌ `no such column: being` |
| **apostrophe** | `Urist's steel` | ❌ `syntax error near "'"` |
| **unbalanced quote** | `"magma` | ❌ `unterminated string` |
| **bare wildcard** | `*` | ❌ `unknown special query` |
| **leading wildcard** | `*teel` | ❌ `unknown special query: teel` |
| trailing wildcard | `stee*` | ✅ 10 rows |
| boolean OR | `steel OR iron` | ✅ 10 rows |
| boolean NOT | `steel NOT iron` | ✅ 10 rows |
| **minus sign** | `steel -iron` | ❌ `no such column: iron` |
| NEAR | `NEAR(steel iron, 5)` | ✅ 10 rows |
| caret | `^steel` | ✅ 4 rows |
| **empty string** | `` | ❌ `syntax error near ""` |
| **whitespace only** | `   ` | ❌ `syntax error near ""` |
| parens | `(steel)` | ✅ 10 rows |
| **question mark** | `what is steel?` | ❌ `syntax error near "?"` |
| **slash** | `fps/death` | ❌ `syntax error near "/"` |
| unicode | `Ísland` | ✅ 10 rows |
| **quote injection** | `'; DROP TABLE …` | ❌ `syntax error near "'"` (parameterised — no injection risk) |
| very long | `steel ` ×80 | ✅ 10 rows |

The colon case is the damaging one. `read_wiki_article`'s own docstring recommends titles of the form `"DF2012:Starting build"`, and its not-found message tells the caller to *"use search_wiki_articles to verify the title"* — but searching that exact string fails. Confirmed live against the running server:

```
search_wiki_articles(query="DF2012:Steel")
→ "Search parser error. Try simpler keywords. Error: no such column: DF2012"
```

The handler at `mcp_df_wiki.py:64` reports every one of these as a user query-syntax problem, which conflates genuine syntax errors with index corruption (§4.2) and with ordinary punctuation the caller had no reason to avoid.

**Not a security finding:** all queries are parameterised. The `DROP TABLE` case fails in the FTS5 tokenizer, not at the SQL layer.

---

## 6. Corpus vintage — the corpus is frozen at September 2014

Neither README states what the dump covers. Every timestamp in the database:

| Year | Rows | | Year | Rows |
|---|---:|---|---|---:|
| 2007 | 212 | | 2011 | 261 |
| 2008 | 212 | | 2012 | 1,998 |
| 2009 | 365 | | 2013 | 3,599 |
| 2010 | 604 | | 2014 | 5,083 |

```
oldest revision : 2007-10-30
newest revision : 2014-09-25
today           : 2026-08-17
```

**There is no content in this database from after September 2014.** Version strings cited in the bare-title (`Main`) articles top out at `0.40.02` / `0.40.01`, confirming that ns=116 — the namespace that carries "current version" articles — is **DF2014 / v0.40.x**, not v50 or v53.

### Interaction with the recorded project memory

The memory note `df-wiki-is-current-not-stale` is correct that the **live wiki** is current (the `Advanced world generation` page covers v53.14, last edited April 2026) and correct that a retrieval failure — not staleness — produced the talk-page content. §4.1 identifies the mechanism behind that failure.

But it carries a misleading implication worth correcting: that the `Main` namespace would have yielded current content if retrieval had worked. It would not. `Main` is v0.40.x from 2014. The distinction that matters:

- **The live DF wiki** is current and authoritative.
- **This MCP database** is a 2014 snapshot and cannot serve post-2014 content from any namespace.

The archived file `worldmaking/sources/wiki-advanced-world-generation-v53.14.md` (88 KB) remains the correct source for worldgen parameters. The memory's guidance to read it first is right; the reason is stronger than recorded.

Compounding this: `last_modified` is populated for all 12,334 rows, and is exactly the field that would let a caller judge vintage — but **no tool exposes it.** Neither `search_wiki_articles` nor `read_wiki_article` returns it.

> **Resolution (§11):** a fresh dump is unobtainable, and most documented mechanics are substantially unchanged in v50+, so the 2014 corpus is retained by decision. The defect to fix is therefore *labelling*, not *content*: name ns 116 `DF2014` so vintage is visible in every result, and expose `last_modified`. Worldgen remains the known exception where v50+ diverged — see §11.4.

---

## 7. Sanitisation quality — "cleaned body text" overstates it

Measured over the 11,037 rows with non-empty bodies:

| Residue | Rows | Share | Cause |
|---|---:|---:|---|
| `'''` bold/italic markup | 3,252 | 29.5% | never handled |
| `==` heading markup | 1,897 | 17.2% | never handled |
| orphan `}}` | 1,497 | 13.6% | nested templates — `code.py:56` is non-greedy to the first `}}` |
| raw wiki tables `{\|`…`\|}` | 647 | 5.9% | never handled |
| mangled `File:`/`Image:` embeds | 609 | 5.5% | link regex captures the wrong group |
| literal `Category:` in prose | 598 | 5.4% | link regex strips syntax, keeps text |
| residual `{{` | 17 | 0.2% | multi-line templates — no `re.DOTALL` |
| HTML comments | 1 | 0.0% | — |
| `<ref>` tags | 0 | 0.0% | — |

Two specific mechanisms:

**Nested templates.** `re.sub(r'\{\{[^\|\}]+\|.*?\}\}', '', raw)` at `code.py:56` is non-greedy and stops at the first `}}`. Given `{{a|{{b}}}}` it consumes through `{{b}}` and leaves a bare `}}`.

**File embeds.** `\[\[(?:[^|\]]*\|)?([^\]]+)\]\]` at `code.py:60` matches only the *first* pipe as a prefix, so the capture group takes everything after it:

```
[[Image:Df1.jpg|thumb|500px]]  →  'Image:Df1.jpg (thumb|500px)'
```

The 647 rows with raw table markup matter disproportionately: MediaWiki tables are where Dwarf Fortress numeric data lives (metal properties, creature stats, item values). That content is present but not usefully readable.

---

## 8. Code hygiene

**`code.py` shadows the Python standard library.** `mcp_df_wiki.py:11-12` does `sys.path.insert(0, PROJECT_DIR)` then `from code import ensure_database_ready`. Verified:

```
import code  →  C:\DFProject\wiki\code.py
code.interact available?  False
```

The stdlib `code` module is unreachable for the entire server process. Anything importing it — `pdb` in some paths, REPL tooling, several debuggers — breaks inside this process. The filename is also uninformative; `README.md:30` has to explain what it does.

**`print()` to stdout would corrupt the MCP transport.** `FastMCP.run()` defaults to stdio, and the desktop config launches the server over stdio. `code.py:122` and `code.py:129` both `print()`. These fire only during `parse_and_populate` — but `ensure_database_ready` (`code.py:75-77`) can invoke exactly that from inside a tool call. If the tables were ever missing, the server would parse a 24 MB XML file on the request path (multi-minute hang, no progress signal) while writing non-JSON to the JSON-RPC stream.

**`ensure_database_ready` cannot repair anything it detects.** It checks only that two tables *exist* (`code.py:70-73`). Both exist, so it is a no-op — yet it opens a connection and runs two `sqlite_master` queries on **every tool call**. It cannot detect orphaned index rows, overwritten articles, or an empty database.

**Connection handling.** `read_wiki_article` (`mcp_df_wiki.py:76-87`) has no `try`/`finally`; an exception between `connect` and `close` leaks the handle. `parse_and_populate` closes a connection it does not own (`code.py:128`), after which `ensure_database_ready` closes it again (`code.py:79`) — harmless in `sqlite3`, but it means the function cannot be composed.

**Dead artifact.** `C:\DFProject\df_wiki.db` is 0 bytes and unreferenced — a decoy next to the real 35 MB `wiki/df_wiki.db`. It predates the folder reorganisation.

---

## 9. Wiring and environment — correct

The one area with no findings.

```jsonc
// %APPDATA%\Claude\claude_desktop_config.json
"df-wiki-search": {
  "command": "C:\\DFProject\\.venv\\Scripts\\python.exe",
  "args": ["-u", "C:\\DFProject\\wiki\\mcp_df_wiki.py"],
  "env": { "PYTHONUNBUFFERED": "1" }
}
```

Matches `README.md:101-103`. Interpreter is the project venv (Python 3.14.6), `mcp` is importable, paths resolve via `__file__` so cwd does not matter, and `-u` is set. Query latency is 8–14 ms.

`requirements.txt` contains exactly `mcp` — no pin, and no declaration that Python ≥3.10 is required by the `mcp` package.

---

## 10. Root `README.md`

Largely accurate for the wiki subproject.

| Line | Claim | Status |
|---|---|---|
| `:30-33` | Wiki file list | ✅ correct |
| `:59` | `python wiki/code.py` builds the DB | ⚠️ see below |
| `:99` | Wiki MCP uses `wiki/df_wiki.db` | ✅ correct |
| `:100` | "Both MCP servers have been verified to start and execute their core tools" | ⚠️ true but misleading — they execute and return wrong results |
| `:101-103` | Config should point to `wiki/mcp_df_wiki.py` | ✅ correct |

**Documentation trap.** Both READMEs give `python wiki/code.py` as the build command (`README.md:59`, `wiki/README.md:42`) with no warning that `__main__` calls `parse_and_populate()` unconditionally and that `CREATE TABLE IF NOT EXISTS` will not reset anything. Running it twice is precisely what produced the 12,704 orphaned index rows in §4.2. The documented happy path is the thing that broke the index.

*(The legends file list at `README.md:16-24` is stale against the working tree — `parser.py`, `legends_mcp.py`, and `legends_mcp_enhanced.py` are deleted, and `legends_mcp_v2.py`, which the MCP config actually launches, is undocumented. Outside this audit's scope; noted for the backlog.)*

---

## 11. Resolved — namespace naming, and the decision to keep the 2014 dump

**Decision (2026-08-17):** a fresh dump is unobtainable (server-side error at the source). The corpus is retained as-is and the unnamed namespaces are given proper names. Rationale: most DF mechanics documented here are substantially unchanged in v50+, so 2014 content is materially useful — the defect was never that the content is old, it was that nothing said so and the pages were unaddressable.

### 11.1 ns 116 = `DF2014`, ns 117 = `DF2014 Talk`

The names are not guesses. They are recoverable from the corpus even though `<siteinfo>` omits them:

| Evidence | Finding |
|---|---|
| Pair pattern in `<siteinfo>` | Every version namespace is an `N`/`N+1` article/talk pair — `106/107 40d`, `110/111 23a`, `112/113 v0.31`, `114/115 DF2012`. 116/117 continues it. |
| In-corpus prefix references | **938** occurrences of a `DF2014:` namespace prefix in article text — second only to `DF2012` (1,169). |
| Redirects targeting it | e.g. `Bugs` → `#REDIRECT DF2014:Known bugs and issues` |
| Wiki templates constructing it | `Template:ArticleVersion` and `Template:CategoryVersion` both build `DF2014:` links |
| Version banners on ns=116 pages | `{{av}}` ×2,040, `{{Migrated_article}}` ×1,653 |
| Version strings on ns=116 pages | `0.40.01` … `0.40.11` — the DF2014 release line |

The DF wiki migrated v0.40 content into `DF2014` and served it at bare titles as the then-current version. That is exactly what ns=116 holds.

### 11.2 Naming them also repairs 270 dangling redirects

Because DF2014 pages are stored under bare titles, every redirect pointing at a `DF2014:`-prefixed target fails to resolve today:

```
redirect rows     : 2,721
  target resolves : 1,551
  target dangling : 1,170  (43%)
```

Dangling targets by prefix:

| Prefix | Count | Cause |
|---|---:|---|
| `cv` | 366 | `{{cv}}` current-version template mangled by sanitisation (§7), not a namespace issue |
| **`DF2014`** | **270** | **fixed by naming ns 116** |
| *(bare)* | 233 | mixed — includes the 185 overwritten titles (§4.1) |
| `40d` | 65 | genuinely absent from this dump |
| `23a` | 39 | genuinely absent from this dump |
| `DF2012` | 37 | genuinely absent from this dump |

So the naming change is not cosmetic: it makes 270 redirects resolvable and makes 3,610 DF2014 articles addressable and filterable for the first time.

### 11.3 Namespace map for the reparse

Take `<ns>` as authoritative; do not split titles. `<siteinfo>` supplies every name except the two below.

```python
NS_NAMES = {
    0: "Main", 1: "Talk", 2: "User", 3: "User talk",
    4: "Dwarf Fortress Wiki", 5: "Dwarf Fortress Wiki talk",
    6: "File", 7: "File talk", 8: "MediaWiki", 9: "MediaWiki talk",
    10: "Template", 11: "Template talk", 12: "Help", 13: "Help talk",
    14: "Category", 15: "Category talk",
    100: "Bloodline",    101: "Bloodline Talk",
    102: "Utility",      103: "Utility Talk",
    104: "Modification", 105: "Modification Talk",
    106: "40d",          107: "40d Talk",
    108: "Unused",       109: "Unused Talk",
    110: "23a",          111: "23a Talk",
    112: "v0.31",        113: "v0.31 Talk",
    114: "DF2012",       115: "DF2012 Talk",
    116: "DF2014",       117: "DF2014 Talk",   # not in <siteinfo>; see §11.1
    1000: "Masterwork",  1001: "Masterwork Talk",
}
```

Expected result of a `(ns, title)`-keyed reparse: 12,519 rows instead of 12,334 (the 185 overwritten articles recovered), a clean FTS index with no orphans, `DF2014` filterable as its own namespace, and the 12 junk namespaces (`Dwarf cancels task`, etc.) gone — those pages return to `Main` where they belong.

### 11.4 What this decision does *not* resolve

Retaining the 2014 dump is sound for stable mechanics, but two caveats should be surfaced to callers rather than assumed away:

- **Worldgen is a known exception.** Per the project's own findings, v50+ changed mesh-size semantics (higher value = finer, the reverse of the old guide) and the behaviour of zero-weighted bands. `worldmaking/sources/wiki-advanced-world-generation-v53.14.md` remains authoritative there, not this corpus.
- **`last_modified` should be exposed** (finding #10). It is populated on all 12,334 rows and is the cheapest possible honesty mechanism — a caller seeing `2014-08-27` on a result can judge for itself whether the mechanic is likely to have moved. Combined with a namespace label of `DF2014`, the vintage becomes self-evident at the point of use instead of being a fact buried in a README.

With those two in place, finding #7 downgrades from *critical* to *medium*: the corpus is old, clearly labelled, and useful.

---

## Note for the rewrite

Findings 4, 5, and 6 share one root cause: **identity is reconstructed from the title string instead of read from the dump.** The XML gives every page an authoritative `<ns>` and a unique page `<id>`; the parser uses neither, deriving namespace by splitting on `:` and letting a `UNIQUE` title constraint arbitrate identity. Fixing the tool layer alone cannot recover the 185 lost articles or make current-version content addressable — those require a reparse keyed on `(ns, title)` or on page id.

---

## Appendix A — Reproduction

All read-only. Run from `C:\DFProject\wiki`.

```bash
# Corpus counts, namespaces, junk share
"C:/DFProject/.venv/Scripts/python.exe" -c "import sqlite3;c=sqlite3.connect('file:df_wiki.db?mode=ro',uri=True);print(c.execute('SELECT COUNT(*) FROM wiki_articles').fetchone());[print(r) for r in c.execute('SELECT namespace,COUNT(*) n FROM wiki_articles GROUP BY namespace ORDER BY n DESC')]"

# Orphaned FTS rows (expect 12704)
"C:/DFProject/.venv/Scripts/python.exe" -c "import sqlite3;c=sqlite3.connect('file:df_wiki.db?mode=ro',uri=True);print(c.execute('SELECT COUNT(*) FROM wiki_search_index_docsize WHERE id NOT IN (SELECT id FROM wiki_articles)').fetchone())"

# Recall loss: raw FTS matches vs joined
"C:/DFProject/.venv/Scripts/python.exe" -c "import sqlite3;c=sqlite3.connect('file:df_wiki.db?mode=ro',uri=True);q='steel';print('raw',c.execute('SELECT COUNT(*) FROM wiki_search_index WHERE wiki_search_index MATCH ?',(q,)).fetchone()[0]);print('joined',c.execute('SELECT COUNT(*) FROM wiki_search_index f JOIN wiki_articles a ON f.rowid=a.id WHERE wiki_search_index MATCH ?',(q,)).fetchone()[0])"

# Corpus vintage (expect max 2014-09-25)
"C:/DFProject/.venv/Scripts/python.exe" -c "import sqlite3;c=sqlite3.connect('file:df_wiki.db?mode=ro',uri=True);print(c.execute('SELECT MIN(last_modified),MAX(last_modified) FROM wiki_articles WHERE last_modified<>\"\"').fetchone())"

# Latent snippet() failure on orphaned rows
"C:/DFProject/.venv/Scripts/python.exe" -c "import sqlite3;c=sqlite3.connect('file:df_wiki.db?mode=ro',uri=True);print(c.execute(\"SELECT snippet(wiki_search_index,1,'[',']','...',15) FROM wiki_search_index WHERE wiki_search_index MATCH 'magma' LIMIT 1\").fetchone())"
```

Title collisions require the XML:

```bash
"C:/DFProject/.venv/Scripts/python.exe" -c "
import xml.etree.ElementTree as ET, collections
t=collections.Counter(); d=collections.defaultdict(list)
for _,e in ET.iterparse('df_wiki_compressed.xml',events=('end',)):
    if e.tag.endswith('page'):
        ti=e.find('.//{*}title'); tx=e.find('.//{*}text'); ns=e.find('{*}ns')
        if ti is not None and tx is not None:
            t[ti.text]+=1; d[ti.text].append((ns.text if ns is not None else '?',len(tx.text or '')))
        e.clear()
dup={k:v for k,v in t.items() if v>1}
print('pages',sum(t.values()),'titles',len(t),'collisions',len(dup))
for k in sorted(dup,key=lambda k:-max(l for _,l in d[k]))[:10]: print(' ',k,d[k])"
```

## Appendix B — Live MCP calls made

| Call | Result |
|---|---|
| `search_wiki_articles("magma pump", namespace="Guides")` | `No wiki articles found…` — documented value matches nothing |
| `search_wiki_articles("DF2012:Steel")` | `Search parser error. Try simpler keywords. Error: no such column: DF2012` |
| `read_wiki_article("Advanced world generation")` | Returned 696 chars of talk-page content instead of the 64,896-char article |

## Appendix C — Findings index

| # | Finding | Severity |
|---|---|---|
| 1 | 2 of 3 documented tools do not exist; the real one is undocumented | Doc — critical |
| 2 | All 3 documented schema tables are fictional; `page_links` unimplementable | Doc — high |
| 3 | Documented namespace `'Guides'` matches 0 rows | Doc — high |
| 4 | `'Main'` silently merges XML ns 0 / 116 / 117 | Design — critical |
| 5 | 185 articles silently overwritten by their own talk pages | Data — critical |
| 6 | 12,704 orphaned FTS rows; ~50% recall loss, corrupted bm25 | Data — critical |
| 7 | Corpus frozen at 2014-09-25; undocumented everywhere | Doc — medium *(downgraded — see §11: dump retained by decision; fix is to label it via `DF2014` namespace + expose `last_modified`)* |
| 8 | 12 of 22 query shapes raise, reported as user syntax error | Robustness — high |
| 9 | 44% of indexed corpus is redirects/empties/templates/talk | Data — high |
| 10 | `last_modified` stored but exposed by no tool | Design — medium |
| 11 | Undocumented `LIMIT 10`; target article ranks #11 for `steel` | Doc — medium |
| 12 | `snippet()` markers all `'...'`, indistinguishable from ellipsis | Cosmetic — low |
| 13 | `code.py` shadows stdlib `code` module | Hygiene — medium |
| 14 | `print()` to stdout can corrupt the stdio JSON-RPC transport | Hygiene — medium |
| 15 | `ensure_database_ready` runs per call, can repair nothing | Hygiene — low |
| 16 | Documented build command corrupts the index when re-run | Doc — high |
| 17 | Case-sensitive, untrimmed title lookup, undocumented | Doc — medium |
| 18 | Tool schemas mark `query` / `title` as optional | Hygiene — low |
| 19 | 0-byte `df_wiki.db` at repo root | Hygiene — low |
| 20 | 43% of redirects dangle; 270 of them fixed by naming ns 116 `DF2014` | Data — high |

**Resolved during the audit:** ns 116 / 117 identified as `DF2014` / `DF2014 Talk` from in-corpus evidence (§11.1); decision taken to retain the 2014 dump and label it rather than chase a fresh export (§11).

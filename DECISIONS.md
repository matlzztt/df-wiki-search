# Architecture Decisions — `df-wiki-search`

Short records of choices made and why. Detail lives in [`AUDIT.md`](AUDIT.md) (what broke) and [`LESSONS.md`](LESSONS.md) (why, and rules for v2).

---

## ADR-001 — Keep SQLite + FTS5; rebuild the ingest stage

**Date:** 2026-08-17 · **Status:** accepted · **Supersedes:** nothing

### Context

The v1 audit found 20 defects. The open question was whether the retrieval architecture itself was wrong — specifically whether to replace lexical search with vector embeddings, which have worked well on other projects here.

Mapping all 20 findings to pipeline stages:

| Stage | Findings | Character |
|---|---|---|
| **① Ingest** (`code.py`) | 4, 5, 6, 7, 9, 20, L9 | **Every data-destroying fault.** Irreversible without reparse. |
| ② Index (FTS5 config) | 6 partly, missing stemmer | Config errors; fixed by rebuilding. |
| ③ Query (`mcp_df_wiki.py`) | 3, 8, 10, 11, 12, 17, 18, L5 | Misleading but **fixable in place, no data change.** |
| ④ Docs | 1, 2, 16 | Prose. |

The faults are concentrated in ingest, not in retrieval.

### Decision

**Rebuild stage ① and ②. Keep SQLite + FTS5 + BM25. Defer any embedding work until measured.**

### Rationale

**Embeddings would not have prevented a single finding.** With the same parser, a vector store would have embedded 185 talk pages as their overwritten articles, 2,721 redirect stubs as retrievable documents, and every document twice — with no namespace filter and no vintage. The retrieval algorithm was never the failing component.

**A correct corpus is a prerequisite under every option**, so rebuilding ingest is never wasted work regardless of what retrieval we end up with.

**The corpus shape favours lexical retrieval.** DF content is identifier-dense (`[MINERAL_SCARCITY]`, `0.40.02`, material and token names) — exactly where BM25 is strongest and embeddings weakest. And it is short: of 7,006 substantive articles, 3,940 (56%) are under 500 characters, where chunking is overhead and lexical matching is near-optimal.

| Length | Articles |
|---|---:|
| <500 | 3,940 |
| 500–2k | 1,503 |
| 2k–8k | 1,209 |
| 8k–20k | 274 |
| >20k | 80 |

**Cost and debuggability.** FTS5 answers in 8–14 ms with no model, no API, no re-embedding on rebuild, and a ranking you can inspect. After a session spent diagnosing silent failures, adding an opaque second failure surface is the wrong next move.

### What this decision does not claim

Lexical search cannot answer conceptual queries — *"my dwarves keep going insane"* should return `Stress` and `Tantrum spiral`, which may contain none of those words. No ingest fix changes that. That is a real gap and a real case for embeddings.

### Consequences

- Rebuild ingest keyed on the dump's authoritative `<ns>` and page `<id>`; never split titles (ADR-002).
- Configure `porter unicode61`; build a known-answer relevance test set.
- **Revisit embeddings once the test set exists.** If lexical handles the real query mix, we saved a second index. If it fails on paraphrase queries, add embeddings as a *second* index and fuse — not as a replacement.
- Decision to be re-evaluated with data, not intuition.

### Considered and rejected

| Option | Why not |
|---|---|
| Pure vector store | Requires the same ingest rebuild anyway, then adds cost and opacity. Weakest exactly on DF's identifier-dense content. |
| Hybrid now (BM25 + vectors) | Right eventual shape, wrong sequencing — commits to ongoing cost before knowing whether it is needed. |
| Curated subset, no search | Genuinely correct for narrow projects — the worldgen slice is 34 articles / ~51k tokens and fits in context. Does not serve general wiki lookup, so it complements rather than replaces this. Noted in ADR-003. |

---

## ADR-002 — Identity comes from the dump, never from the title string

**Date:** 2026-08-17 · **Status:** accepted

### Context

v1 derived namespace via `raw_title.split(":")[0]`. Cost: 185 articles silently overwritten, 3 namespaces merged into `Main`, 12 junk namespaces invented from colons in ordinary titles, 270 dangling redirects, 3,610 current-version articles unfilterable.

### Decision

Key articles on **`(ns, base_title)`**, carrying the dump's `<ns>` integer and page `<id>`. Titles are display data.

### Namespace naming

`<siteinfo>` declares every namespace except 116 and 117. Those are identified as **`DF2014`** and **`DF2014 Talk`** from in-corpus evidence: the `<siteinfo>` article/talk pair pattern, 938 `DF2014:` prefix references in article text, redirects targeting `DF2014:`, `Template:ArticleVersion` constructing `DF2014:` links, and `0.40.x` version banners on those pages. See `AUDIT.md` §11.1.

### Title prefixing is not uniform

Verified against the dump — this drives the schema:

| ns | Dump title form | Example |
|---|---|---|
| 0 | bare (21 of 1,696 contain a colon) | `Main Page` |
| 10, 12, 13, 102, 103, 114, 115, 1000, 1001 | **prefixed** | `DF2012:Train` |
| **116, 117** | **bare** (3,609 of 3,610) | `Bugs` |

So the schema stores `base_title` (prefix stripped) and derives `full_title` (`ns_name:base_title`, except ns 0). This makes `DF2014:Steel` and `DF2012:Steel` both addressable and unambiguous for the first time.

---

## ADR-004 — Default search scope includes Masterwork; exclude raw-token shells

**Date:** 2026-08-17 · **Status:** accepted

### Masterwork is in the default scope

`DEFAULT_NS = {Main, DF2014, Masterwork}`. Superseded version namespaces (`40d`, `23a`, `v0.31`, `DF2012`) are indexed but excluded from the default; callers widen explicitly.

An earlier draft excluded Masterwork, reasoning from v1's behaviour where `steel` returned six Masterwork pages above the vanilla article. **That reasoning was wrong** — the crowding was a symptom of v1's defects (redirect stubs ranked as content, no stemming, no title weighting, the vanilla article clobbered and sitting at #11), not of Masterwork. Measured on v2 across seven queries, adding Masterwork left five rankings identical and moved the vanilla top hit from #1 to #2 in two. Every result carries its `ns_name`, so a caller can always tell mod content from vanilla.

Masterwork articles are among the most detailed on the wiki and belong in the default.

### `Title/raw` shells are excluded from the index

`/raw` subpages were 4,492 of 12,912 indexed rows (35%) and led **11 of 12** test queries. They are template wrappers — `{{raw|DF2014:inorganic_metal.txt|INORGANIC|STEEL}}` — whose data pages are not in the export, leaving ~100 characters of repeated identifiers. Very short plus very dense is exactly what BM25 over-rewards.

**The cut is on substance, not on the title pattern.** The distribution is bimodal: p50 is 100 chars, but the tail is real content — `Masterwork:Undead/raw` is 32,201 chars of creature definitions with descriptions, `DF2014:Interface.txt/raw` is 65,768 chars of key bindings. Excluding every `/raw` page would have discarded exactly the Masterwork raw data worth keeping. The threshold (`RAW_SHELL_MAX_CHARS = 300`) drops 3,784 shells and keeps 708 substantive pages.

### Consequence

Indexed rows: 12,912 → 9,128. Rankings now lead with real articles across all twelve test queries.

**Open, for the query layer:** exact title matches do not yet win. `steel` returns `Masterwork:Rusty steel` before `DF2014:Steel`, and `plump helmet` returns `Plump helmet man` before `Plump helmet`. Column weighting alone cannot express "this title *is* the query" — that needs an explicit exact-match boost in `search.py`, not a scope change.

---

## ADR-003 — Retain the 2014 dump; label it rather than replace it

**Date:** 2026-08-17 · **Status:** accepted

### Context

The corpus's newest revision is 2014-09-25; there is no post-2014 content in any namespace. A fresh export is currently unobtainable (server-side error upstream).

### Decision

Retain the dump. Make its vintage visible at the point of use rather than documenting it in a README nobody reads.

### Rationale

Most DF mechanics documented here are substantially unchanged in v50+, so 2014 content is materially useful. The defect was never that the content is old — it was that nothing said so, and that the pages were unaddressable.

### Consequences

- Every search result and article read returns `ns_name` (e.g. `DF2014`) and `last_modified`. A caller seeing `DF2014` / `2014-08-27` can judge for itself.
- `build_meta` records the dump's revision range, source hash, and build time; exposed via a tool.
- **Known exception:** worldgen diverged in v50+ (mesh-size semantics reversed; zero-weighted band behaviour changed). `worldmaking/sources/wiki-advanced-world-generation-v53.14.md` stays authoritative there, not this corpus.
- If a fresh dump becomes obtainable, ingest should absorb it unchanged — nothing in ADR-002 is dump-vintage specific.

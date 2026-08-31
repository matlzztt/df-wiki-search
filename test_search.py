"""Known-answer tests for the DF wiki index.

LESSONS.md L13: v1 shipped with no stemmer, a half-orphaned index and a cutoff
that hid the answer at #11 -- and every query still returned ten plausible
results, so nothing looked wrong. A dozen assertions like these would have
caught all three.

Run:  python test_search.py [--db PATH] [--structural]

Two modes. By default every check runs, including the known-answer relevance,
stemming and retrieval assertions -- those are calibrated against the full
28,880-page dump and only mean anything there. `--structural` runs the subset
that holds for *any* index ingest.py produces (robustness, rejection, index
integrity), which is what CI can assert against a small fixture.
"""

import argparse
import os
import sys

import search
import schema

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Resolved against the project directory, not the caller's cwd. The old
# relative default meant the suite ran from exactly one working directory,
# which is a fine way to end up with no CI.
DEFAULT_DB = os.environ.get("DF_WIKI_DB",
                            os.path.join(PROJECT_DIR, "df_wiki_v2.db"))

# (query, title that must appear, max acceptable rank)
RELEVANCE = [
    ("steel",                    "DF2014:Steel",                     1),
    ("adamantine",               "DF2014:Adamantine",                3),
    ("aquifer",                  "DF2014:Aquifer",                   1),
    ("strange mood",             "DF2014:Strange mood",              1),
    ("plump helmet",             "DF2014:Plump helmet",              1),
    ("magma",                    "DF2014:Magma",                     5),
    ("soap",                     "DF2014:Soap",                      3),
    ("advanced world generation", "DF2014:Advanced world generation", 1),
    ("minecart",                 "DF2014:Minecart",                  1),
    ("creature token",           "DF2014:Creature token",            3),
    # "Tantrum spiral" is a redirect to "Tantrum"; redirects are deliberately
    # not indexed, so the target is the correct answer.
    ("tantrum spiral",           "DF2014:Tantrum",                   1),
    ("masterwork",               "Masterwork:Main Page",             3),
    ("item quality",             "DF2014:Item quality",              1),
]

# Query shapes that raised in v1 (12 of 22). All must succeed now.
ROBUSTNESS = [
    "DF2014:Steel", "Masterwork:Steel", "well-being", "Urist's steel",
    '"magma', "steel -iron", "what is steel?", "fps/death", "*teel",
    "(steel)", "stee*", "steel OR iron", "NEAR(steel iron, 5)", "^steel",
    "Ísland", "'; DROP TABLE articles;--", "steel " * 80,
]

# Queries that must be rejected with a clear message rather than crashing.
# A bare "*" has no searchable words: rejecting it with that message is the
# correct outcome, not a robustness failure.
MUST_REJECT = ["", "   ", "!!!", "###", "*"]

STEM_PAIRS = [("mine", "mining"), ("forge", "forging"), ("wall", "walls")]

# (title, expected namespace, minimum length)
RETRIEVAL = [
    ("Advanced world generation", "DF2014", 40000),
    ("steel", "DF2014", 1000),          # case-insensitive
    ("  Minecart  ", "DF2014", 20000),  # whitespace-tolerant
    ("DF2012:Steel", "DF2012", 1000),
]


# --------------------------------------------------------------------------
# corpus-independent -- these hold for any index ingest.py builds
# --------------------------------------------------------------------------

def check_robustness(conn, failures):
    print("== robustness (all must succeed) ==")
    for q in ROBUSTNESS:
        try:
            # Default scope on purpose: the namespace-prefix branch in
            # search() only runs when no explicit scope is given, and
            # 'DF2014:Steel' is here to exercise exactly that.
            res = search.search(conn, q)
            print(f"  ok   {q[:34]!r:38} {res['total']:5} hits")
        except Exception as exc:
            print(f"  FAIL {q[:34]!r:38} {type(exc).__name__}: {exc}")
            failures.append(f"robustness {q!r}: {exc}")


def check_rejection(conn, failures):
    print("\n== rejection (must fail with a clear message) ==")
    for q in MUST_REJECT:
        try:
            search.search(conn, q)
            print(f"  FAIL {q!r:12} accepted, should have been rejected")
            failures.append(f"{q!r} was accepted")
        except search.SearchError as exc:
            print(f"  ok   {q!r:12} {exc}")
        except Exception as exc:
            print(f"  FAIL {q!r:12} wrong exception {type(exc).__name__}: {exc}")
            failures.append(f"{q!r}: {type(exc).__name__}")


def check_integrity(conn, failures):
    print("\n== index integrity ==")
    n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_idx = conn.execute("SELECT COUNT(*) FROM articles_fts_docsize").fetchone()[0]
    orph = conn.execute("""SELECT COUNT(*) FROM articles_fts_docsize
                           WHERE id NOT IN (SELECT id FROM articles)""").fetchone()[0]
    want = conn.execute(
        f"SELECT COUNT(*) FROM articles WHERE {schema.INDEXABLE_PREDICATE}").fetchone()[0]
    for label, got, exp in [("orphaned rows", orph, 0),
                            ("indexed == indexable", n_idx, want)]:
        ok = got == exp
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got} (expect {exp})")
        if not ok:
            failures.append(f"{label}: {got} != {exp}")
    print(f"  info articles={n_art} indexed={n_idx}")


# --------------------------------------------------------------------------
# corpus-calibrated -- meaningful only against the full dump
# --------------------------------------------------------------------------

def check_relevance(conn, failures):
    print("== relevance ==")
    for query, want, max_rank in RELEVANCE:
        res = search.search(conn, query, limit=max(max_rank, 10))
        titles = [r["title"] for r in res["results"]]
        rank = titles.index(want) + 1 if want in titles else None
        ok = rank is not None and rank <= max_rank
        print(f"  {'ok ' if ok else 'FAIL'} {query!r:30} {want:36} "
              f"rank={rank} (<= {max_rank}) of {res['total']} hits")
        if not ok:
            failures.append(f"{query!r}: {want} at rank {rank}, wanted <= {max_rank}"
                            f" (top: {titles[:3]})")


def check_stemming(conn, failures):
    print("\n== stemming ==")
    for a, b in STEM_PAIRS:
        na = search.search(conn, a, namespaces="all")["total"]
        nb = search.search(conn, b, namespaces="all")["total"]
        ok = na == nb and na > 0
        print(f"  {'ok ' if ok else 'FAIL'} {a}={na} {b}={nb}")
        if not ok:
            failures.append(f"stemming {a}/{b}: {na} != {nb}")


def check_retrieval(conn, failures):
    print("\n== retrieval ==")
    for title, want_ns, min_len in RETRIEVAL:
        try:
            art = search.read_article(conn, title)
            ok = art["namespace"] == want_ns and art["length"] >= min_len
            print(f"  {'ok ' if ok else 'FAIL'} {title!r:28} -> {art['title']} "
                  f"[{art['namespace']}] {art['length']} chars")
            if not ok:
                failures.append(f"read {title!r}: got {art['title']} "
                                f"[{art['namespace']}] {art['length']}")
        except Exception as exc:
            print(f"  FAIL {title!r:28} {type(exc).__name__}: {exc}")
            failures.append(f"read {title!r}: {exc}")

    # The v1 headline failure: this must be the article, not its talk page.
    art = search.read_article(conn, "Advanced world generation")
    if "mesh" not in art["body"].lower():
        failures.append("Advanced world generation body lacks 'mesh' - talk page again?")
        print("  FAIL Advanced world generation does not look like the article")
    else:
        print("  ok  Advanced world generation is the article, not the talk page")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Known-answer tests for the DF wiki index.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="index to test (default: %(default)s)")
    ap.add_argument("--structural", action="store_true",
                    help="run only the checks that hold for any index, skipping "
                         "the known-answer relevance, stemming and retrieval sets")
    args = ap.parse_args(argv)

    try:
        conn = search.open_index(args.db)
    except search.IndexUnavailable as exc:
        print(f"FAIL: {exc}")
        return 1

    scope = "structural checks only" if args.structural else "full suite"
    print(f"index: {args.db}  ({scope})\n")

    failures = []
    if not args.structural:
        check_relevance(conn, failures)
        print()
    check_robustness(conn, failures)
    check_rejection(conn, failures)
    if not args.structural:
        check_stemming(conn, failures)
    check_integrity(conn, failures)
    if not args.structural:
        check_retrieval(conn, failures)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

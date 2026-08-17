"""MediaWiki markup -> readable text, with link and category capture.

Design notes (see LESSONS.md L9):
  * Cleaning is a *derived* transform. `articles.raw_text` keeps the original
    wikitext, so this module can be changed and the corpus re-derived without
    re-fetching the dump.
  * Templates are UNWRAPPED, not deleted. 10.8% of the corpus text lives inside
    `{{...}}` and it is mostly data, not chrome -- the `Creature` article is
    102,664 chars of which 96,897 are inside templates. v1 deleted all of it and
    kept 3,541 chars. Only known-chrome templates are dropped.
  * All scanners are nesting-aware. v1 used a non-greedy regex that stopped at
    the first `}}`, which left an orphan `}}` in 13.6% of articles.
"""

import re

# Templates that carry no article content: version banners, quality ratings,
# maintenance flags. Everything else is unwrapped for its parameter values.
CHROME_TEMPLATES = frozenset({
    "av", "quality", "migrated_article", "old", "verify", "stub", "cleanup",
    "prettytable", "notoc", "toc", "clear", "-", "disambig", "disambiguation",
})

# Link prefixes that are embeds rather than prose references.
_EMBED_PREFIXES = ("file:", "image:", "media:")


def _match_pair(text, start, open_tok, close_tok):
    """Return index just past the balanced close_tok, or None if unbalanced."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text.startswith(open_tok, i):
            depth += 1
            i += len(open_tok)
        elif text.startswith(close_tok, i):
            depth -= 1
            i += len(close_tok)
            if depth == 0:
                return i
        else:
            i += 1
    return None


def _split_top_level(body, sep="|"):
    """Split on `sep`, ignoring separators nested inside {{ }} or [[ ]]."""
    parts = []
    buf = []
    i = 0
    n = len(body)
    while i < n:
        if body.startswith("{{", i):
            j = _match_pair(body, i, "{{", "}}")
            j = j if j is not None else n
            buf.append(body[i:j])
            i = j
        elif body.startswith("[[", i):
            j = _match_pair(body, i, "[[", "]]")
            j = j if j is not None else n
            buf.append(body[i:j])
            i = j
        elif body.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
        else:
            buf.append(body[i])
            i += 1
    parts.append("".join(buf))
    return parts


def _render_template(body, sink):
    """Unwrap one template body into text, or '' if it is chrome."""
    parts = _split_top_level(body)
    name = parts[0].strip().lower()

    if name in CHROME_TEMPLATES:
        return ""
    if name == "category":
        for p in parts[1:]:
            v = p.strip()
            if v:
                sink["categories"].add(v)
        return ""

    rendered = []
    for p in parts[1:]:
        if "=" in p:
            key, _, val = p.partition("=")
            key = key.strip()
            val = _clean_fragment(val, sink).strip()
            if val:
                rendered.append(f"{key}: {val}" if key and not key.isdigit() else val)
        else:
            val = _clean_fragment(p, sink).strip()
            if val:
                rendered.append(val)

    if not rendered:
        return ""
    return " ".join(rendered) if len(rendered) == 1 else "\n".join(rendered)


def _render_link(body, sink):
    """Resolve one [[...]] link; record its target."""
    parts = _split_top_level(body)
    target = parts[0].strip()
    low = target.lower()

    if any(low.startswith(p) for p in _EMBED_PREFIXES):
        return ""  # image/file embed: drop entirely, including its caption
    if low.startswith("category:"):
        sink["categories"].add(target.split(":", 1)[1].strip())
        return ""

    if target:
        sink["links"].add(target.split("#", 1)[0].strip())

    label = parts[-1].strip() if len(parts) > 1 else target
    return _clean_fragment(label, sink)


def _clean_fragment(text, sink):
    """Recursively expand templates and links inside a fragment."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            j = _match_pair(text, i, "{{", "}}")
            if j is None:
                i += 2
                continue
            rendered = _render_template(text[i + 2:j - 2], sink)
            if rendered:
                # Adjacent template expansions must not fuse into one token --
                # {{raw header|..|STEEL}}{{raw|DF2014:..}} was producing
                # "STEELDF2014:". Block-shaped output gets newlines, inline
                # output (the 3,593 {{k|..}} key references) gets a space.
                pad = "\n" if "\n" in rendered else " "
                out.append(pad + rendered + pad)
            i = j
        elif text.startswith("[[", i):
            j = _match_pair(text, i, "[[", "]]")
            if j is None:
                i += 2
                continue
            out.append(_render_link(text[i + 2:j - 2], sink))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _flatten_tables(text):
    """Turn `{| ... |}` wiki tables into pipe-separated lines.

    Kept rather than stripped: 647 articles carry their numeric data (metal
    properties, creature stats, item values) only inside tables.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("{|", i):
            j = _match_pair(text, i, "{|", "|}")
            if j is None:
                i += 2
                continue
            body = text[i + 2:j - 2]
            rows = []
            for raw_row in re.split(r"\n\s*\|-+", body):
                cells = []
                for line in raw_row.split("\n"):
                    line = line.strip()
                    if not line or line.startswith("|+"):
                        continue
                    if line[:1] in ("!", "|"):
                        line = line[1:]
                        # strip a leading cell-style attribute chunk
                        for cell in re.split(r"\|\||!!", line):
                            cell = cell.strip()
                            if "|" in cell and re.match(
                                r'^[a-zA-Z\-]+\s*=\s*("[^"]*"|\S+)', cell
                            ):
                                cell = cell.split("|", 1)[1].strip()
                            if cell:
                                cells.append(cell)
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                out.append("\n" + "\n".join(rows) + "\n")
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_REF = re.compile(r"<ref[^>]*?/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_RE_NOWIKI = re.compile(r"</?(?:nowiki|noinclude|includeonly|onlyinclude)>", re.I)
_RE_BR = re.compile(r"<br\s*/?>", re.I)
_RE_HTML = re.compile(r"</?(?:div|span|center|small|big|font|sup|sub|code|pre|tt|b|i|u|table|tr|td|th)\b[^>]*>", re.I)
_RE_HEADING = re.compile(r"^\s*(={2,6})\s*(.*?)\s*\1\s*$", re.M)
_RE_BOLDITAL = re.compile(r"'''''|'''|''")
_RE_EXTLINK = re.compile(r"\[(?:https?:|//)\S+?(?:\s+([^\]]*))?\]")
_RE_MAGIC = re.compile(r"__(?:TOC|NOTOC|NOEDITSECTION|FORCETOC)__")
_RE_BLANKS = re.compile(r"\n{3,}")
_RE_TRAILSP = re.compile(r"[ \t]+$", re.M)


def clean(raw_text):
    """Return (body_text, sorted_links, sorted_categories)."""
    if not raw_text:
        return "", [], []

    sink = {"links": set(), "categories": set()}

    text = _RE_COMMENT.sub("", raw_text)
    text = _RE_REF.sub("", text)
    text = _flatten_tables(text)
    text = _clean_fragment(text, sink)

    text = _RE_HEADING.sub(lambda m: "\n" + "#" * len(m.group(1)) + " " + m.group(2), text)
    text = _RE_EXTLINK.sub(lambda m: m.group(1) or "", text)
    text = _RE_BOLDITAL.sub("", text)
    text = _RE_BR.sub("\n", text)
    text = _RE_NOWIKI.sub("", text)
    text = _RE_HTML.sub("", text)
    text = _RE_MAGIC.sub("", text)
    text = _RE_TRAILSP.sub("", text)
    text = _RE_BLANKS.sub("\n\n", text)

    return text.strip(), sorted(sink["links"]), sorted(sink["categories"])


# Both bracketed and bare forms. This dump carries the bare form: its link
# syntax was stripped upstream, before code.py ever saw it -- only 21 of 12,519
# pages still contain "[[". The bracketed branch is kept so a real MediaWiki
# dump would ingest unchanged.
_RE_REDIRECT = re.compile(r"^\s*#\s*REDIRECT\s*:?\s*(?:\[\[)?([^\]\n#|]+)", re.I)


def redirect_target(raw_text):
    """Return the redirect target from raw wikitext, or None."""
    if not raw_text:
        return None
    m = _RE_REDIRECT.match(raw_text)
    if not m:
        return None
    return m.group(1).strip() or None

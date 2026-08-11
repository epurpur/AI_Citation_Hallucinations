"""
check_references.py
--------------------
Reads all PDFs from a folder and, for each one, checks whether it contains
a references/citations/bibliography/works-cited section. Prints a yes/no
statement for each file.
"""

import re
import unicodedata
from collections import Counter
from pathlib import Path

import pdfplumber

# Folder to read PDFs from
FOLDER = "/Users/ep9k/Desktop/AI_Citation_Hallucinations/2026_files"

# Section headings we're looking for, as regex patterns. Matched against a
# single line after stripping numbering (e.g. "8.", "III.", "[3]") and
# punctuation, so "References", "8. References", "REFERENCES:", and
# "III. Bibliography" all count.
SECTION_HEADING_PATTERNS = [
    r"references?",
    r"citations?",
    r"bibliography",
    r"works\s+cited",
    r"reference\s+list",
    r"cited\s+works",
    r"literature\s+cited",
    r"sources\s+cited",
]

# Same headings, but with every non-letter character stripped out (lowercase).
# Used as a fallback to catch letter-spaced headings like "R E F E R E N C E S"
# or headings mangled by unusual font kerning during text extraction.
_SQUEEZED_TARGETS = {
    re.sub(r"[^a-z]", "", re.sub(r"\\s\+", "", p.lower())) for p in SECTION_HEADING_PATTERNS
}

# Leading numbering/bullets to strip before matching, e.g. "8.", "8)",
# "III.", "[3]", "-".
_NUMBERING_PREFIX = re.compile(
    r"^[\(\[]?\s*(\d+(\.\d+)*|[ivxlcdm]+)[\.\)\]]?\s*[-:.]?\s*",
    flags=re.IGNORECASE,
)

# Headings that mark where the reference list ENDS (start of the next
# section), so trailing appendices/supplements aren't counted as citations.
SECTION_END_PATTERNS = [
    r"appendi(x|ces)",
    r"acknowledg(e)?ments?",
    r"supplement(al|ary)?\s*(material)?",
    r"author\s+biograph\w*",
    r"about\s+the\s+author\w*",
]

# How close (in PDF points) a line's left edge must be to its column's
# left margin to count as the start of a new reference entry.
MARGIN_TOLERANCE = 4.0


def list_pdf_filenames(folder_path):
    """Return a sorted list of PDF file names (not full paths) in folder_path."""
    folder = Path(folder_path).expanduser()

    if not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")

    pdf_files = sorted(folder.glob("*.pdf"))
    return [f.name for f in pdf_files]


def _normalize(text):
    """Strip invisible/odd characters (zero-width spaces, cid glyphs, NBSPs)."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\(cid:\d+\)", "", text)
    return text.strip()


def _matches_heading_patterns(text, patterns):
    """
    Return True if the (already-normalized) text is essentially just one of
    the given heading patterns -- allowing for leading numbering, trailing
    colons/punctuation, ALL CAPS, and letter-spaced text
    (e.g. "R E F E R E N C E S").
    """
    if not text:
        return False

    candidate = _NUMBERING_PREFIX.sub("", text)
    candidate = candidate.strip().strip(":;.-").strip()

    for pattern in patterns:
        if re.fullmatch(pattern, candidate, flags=re.IGNORECASE):
            return True

    # The squeezed fallback strips ALL non-letter characters (to catch
    # letter-spaced headings like "R E F E R E N C E S"), but that also
    # erases digits -- which means a Table-of-Contents line like
    # "9. References........................90" reduces to exactly
    # "references" and would otherwise be mistaken for the real heading,
    # causing collection to start from the ToC and never find a valid end
    # point. A genuine heading line never has digits glued onto it, so if
    # the raw text contains any digit, skip this fallback entirely.
    if any(ch.isdigit() for ch in text):
        return False

    squeezed = re.sub(r"[^a-z]", "", text.lower())
    squeezed_targets = {
        re.sub(r"[^a-z]", "", re.sub(r"\\s\+", "", p.lower())) for p in patterns
    }
    if squeezed in squeezed_targets:
        return True

    return False


def _is_heading_line(raw_text):
    """True if raw_text reads as a References/Bibliography/Works Cited heading."""
    return _matches_heading_patterns(_normalize(raw_text), SECTION_HEADING_PATTERNS)


def _is_section_end_line(raw_text):
    """True if raw_text reads as a heading that marks the end of the reference list.

    Also catches a stop-heading that got merged onto the same extracted
    line as the START of the next subsection (e.g. "10. Appendices 10.1
    Kinetic Parameters used for Reactor Design..."), which happens when a
    PDF doesn't leave enough vertical gap between a heading and the body
    text right after it for pdfplumber to treat them as separate lines.
    In that case the exact-match check below never fires because there's
    extra text after the heading word, so we additionally check whether
    the line at least STARTS with a stop-heading word immediately
    followed by what looks like the next subsection's own numbering
    (e.g. "10.1 ..."), which is a much safer signal than a bare substring
    match (it wouldn't fire on a citation that merely mentions "Appendix"
    in its title).
    """
    text = _normalize(raw_text)
    if _matches_heading_patterns(text, SECTION_END_PATTERNS):
        return True

    candidate = _NUMBERING_PREFIX.sub("", text).strip()
    for pattern in SECTION_END_PATTERNS:
        m = re.match(pattern + r"\b", candidate, flags=re.IGNORECASE)
        if m:
            rest = candidate[m.end():].lstrip()
            if not rest or re.match(r"\d+(\.\d+)*\b", rest):
                return True
    return False


def has_references_section(pdf_path):
    """
    Return True if the PDF contains a line that reads as one of the
    reference-section headings (References, Citations, Bibliography,
    Works Cited, etc.), False otherwise.

    For each page, checks three views of the text:
      1. The whole page via extract_text_lines()
      2. The whole page via extract_text() split on newlines
      3. The LEFT and RIGHT half of the page cropped separately

    The crop step matters for two-column layouts (common in journal-style
    papers): pdfplumber groups text into "lines" purely by vertical
    position across the full page width, so a heading in the left column
    can get merged with unrelated body text from the right column on the
    same visual line (e.g. "References orthotics on plantar pressure...").
    Cropping to each column before extracting lines isolates a short
    heading like "References" on its own line again.
    """
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            candidate_lines = []

            lines = page.extract_text_lines() or []
            candidate_lines.extend(ln["text"] for ln in lines)

            plain_text = page.extract_text() or ""
            candidate_lines.extend(plain_text.split("\n"))

            # Column-aware pass: split the page into left/right halves and
            # extract lines from each independently. Harmless no-op for
            # genuinely single-column pages (a short heading at the left
            # margin still fits entirely within the left-half crop).
            width, height = page.width, page.height
            left = page.crop((0, 0, width / 2, height))
            right = page.crop((width / 2, 0, width, height))
            for half in (left, right):
                half_lines = half.extract_text_lines() or []
                candidate_lines.extend(ln["text"] for ln in half_lines)

            for text in candidate_lines:
                if _is_heading_line(text):
                    return True
    return False


# ----------------------------------------------------------------------------
# Counting individual references
# ----------------------------------------------------------------------------

# A page is treated as two-column if there's a tight cluster of words that
# repeatedly start at (almost) the same x-position, well into the right
# half of the page. Centered/cover-page text produces at most a handful of
# coincidental repeats; a real second column produces dozens.
_RIGHT_COLUMN_X_FRACTION = 0.35
_RIGHT_COLUMN_MIN_REPEATS = 10


def _page_is_two_column(page):
    """Detect whether a page uses a two-column layout, via word x0 clustering.

    Word-level positions are used (rather than pdfplumber's line-grouping)
    because on genuine two-column pages, extract_text_lines() can merge
    left- and right-column text onto the same "line" when they happen to
    fall at a similar height -- which would corrupt a line-based check.
    Word positions aren't affected by that merging.
    """
    words = page.extract_words() or []
    if not words:
        return False
    threshold_x = page.width * _RIGHT_COLUMN_X_FRACTION
    right_words = [w for w in words if w["x0"] > threshold_x]
    if not right_words:
        return False
    counts = Counter(round(w["x0"]) for w in right_words)
    _, top_count = counts.most_common(1)[0]
    return top_count >= _RIGHT_COLUMN_MIN_REPEATS


def _page_lines_in_reading_order(page):
    """
    Return this page's text lines as a list of (text, x0) tuples, in
    reading order, correctly handling two-column layouts.

    For two-column pages, cropping to the left half and then the right
    half (each run through extract_text_lines() independently) avoids
    pdfplumber's cross-column line merging and matches how a two-column
    academic layout is actually meant to be read: all the way down the
    left column, then all the way down the right column.
    """
    if _page_is_two_column(page):
        width, height = page.width, page.height
        left = page.crop((0, 0, width / 2, height))
        right = page.crop((width / 2, 0, width, height))
        ordered = []
        for col_id, half in enumerate((left, right)):
            for ln in half.extract_text_lines() or []:
                ordered.append((ln["text"], ln["x0"], col_id, ln["top"], height))
        return ordered
    else:
        return [
            (ln["text"], ln["x0"], 0, ln["top"], page.height)
            for ln in (page.extract_text_lines() or [])
        ]


_REPEATING_LINE_MIN_PAGES = 4
_REPEATING_LINE_MIN_FRACTION = 0.2


def _detect_repeating_lines(pdf):
    """
    Return the set of (normalized) line texts that recur on a large
    fraction of the document's pages -- i.e. running headers/footers like
    "Smith et al., 1st February, 2020" that repeat on nearly every page.

    These can otherwise slip into the reference count: a header that
    happens to sit at the same x-position as the reference list's left
    margin, on the page right after the last real reference (but before
    a stop-heading like "Supplementary Material" appears later on that
    page), gets miscounted as one extra reference.
    """
    total_pages = len(pdf.pages)
    if total_pages < _REPEATING_LINE_MIN_PAGES:
        return set()

    page_counts = Counter()
    for page in pdf.pages:
        texts_on_this_page = set()
        for ln in page.extract_text_lines() or []:
            text = _normalize(ln["text"])
            if text:
                texts_on_this_page.add(text)
        for text in texts_on_this_page:
            page_counts[text] += 1

    threshold = max(_REPEATING_LINE_MIN_PAGES, total_pages * _REPEATING_LINE_MIN_FRACTION)
    return {text for text, count in page_counts.items() if count >= threshold}


def _collect_reference_lines(pdf):
    """
    Walk every page of an open pdfplumber PDF (in column-aware reading
    order) and return the list of {"text", "x0"} lines that fall inside
    the reference/bibliography section.
    """
    collecting = False
    collected = []
    repeating_lines = _detect_repeating_lines(pdf)

    for page in pdf.pages:
        for raw_text, x0, col_id, top, page_height in _page_lines_in_reading_order(page):
            text = _normalize(raw_text)
            if not text:
                continue

            if not collecting:
                if _is_heading_line(text):
                    collecting = True
                continue

            if _is_section_end_line(text):
                collecting = False
                continue

            # A bare number is only a footer page number if it sits near
            # the bottom of the page. A bare number mid-page/mid-column is
            # more likely a wrapped DOI/URL fragment (e.g. a DOI ending in
            # "...2012.666795" that got line-broken right before the "5")
            # and should be kept so it can be reattached during extraction.
            is_footer_page_number = bool(re.fullmatch(r"\d+", text)) and (
                top > page_height * 0.9
            )

            if _is_heading_line(text) or is_footer_page_number or text in repeating_lines:
                continue

            collected.append({"text": text, "x0": x0, "col": col_id})

    return collected


def count_references(pdf_path):
    """
    Return the number of individual references/citations found in the
    PDF's reference section (0 if no such section is found).

    Delegates to _group_into_entries so the printed count always matches
    what extract_references_from_pdf actually produces -- see that
    function's docstring for the three grouping strategies used.
    """
    with pdfplumber.open(pdf_path) as pdf:
        lines = _collect_reference_lines(pdf)

    if not lines:
        return 0

    count = len(_group_into_entries(lines))

    # Degenerate fallback: if literally nothing landed at the margin
    # (extremely unlikely), treat the block as one reference rather than zero.
    return count if count > 0 else 1


# ----------------------------------------------------------------------------
# Building full reference text + extracting DOIs
# ----------------------------------------------------------------------------

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>\]]+", re.IGNORECASE)
URL_RE = re.compile(r"(https?://[^\s\"'<>\]]+|www\.[^\s\"'<>\]]+)", re.IGNORECASE)

# After finding a DOI/URL match, look for a short lowercase/digit token
# immediately following it (separated by the single space our line-joining
# inserts). This reattaches fragments that wrapped onto their own line in
# the original PDF -- e.g. a DOI ending "...2012.66679" continuing as "5"
# on the next line ("...2012.666795"), or "...CD006801." continuing as
# "pub2". URLs can wrap across several short hyphenated lines
# (e.g. "fightcancer.org/policy-" / "resources/costs-cancer-rural-" /
# "communities-" / "0"), so this allows more hops than a DOI would ever
# need, while still stopping once real prose (a capitalized word, a new
# sentence) resumes.
_WRAP_CONTINUATION_RE = re.compile(r"^ ([a-z0-9][a-z0-9._/\-]{0,60})(?=[\s).,;\]]|$)")
_MAX_WRAP_HOPS = 12


def _trim_unbalanced_parens(s):
    """Drop a trailing ')' that has no matching '(' within the match.

    DOIs/URLs often legitimately contain a balanced "(24)"-style segment
    (e.g. an issue number), but a trailing ')' that closes a *sentence*
    around the citation -- not part of the link itself -- should still be
    stripped.
    """
    while s.endswith(")") and s.count("(") < s.count(")"):
        s = s[:-1]
    return s


def _reattach_wrapped_suffix(entry_text, match):
    """Extend a DOI/URL regex match to absorb any wrapped continuation tokens."""
    result = match.group(0)
    idx = match.end()
    for _ in range(_MAX_WRAP_HOPS):
        m = _WRAP_CONTINUATION_RE.match(entry_text[idx:])
        if not m:
            break
        result += m.group(1)
        idx += m.end()
    result = result.rstrip(".,;")
    result = _trim_unbalanced_parens(result)
    return result


def _best_url_match(entry):
    """
    Return the URL regex Match most likely to be the actual citation link,
    not an incidental mention of a site name earlier in the reference
    (e.g. "...Www.bbc.com, 24 May 2024, www.bbc.com/news/articles/..."
    should pick the second one, which has a real path).

    Preference order: matches containing a "/" (i.e. an actual path, not
    just a bare domain) over ones without; among ties, the longest match;
    among further ties, the last one in the text (citations conventionally
    put the actual link at the end).
    """
    matches = list(URL_RE.finditer(entry))
    if not matches:
        return None

    def score(m):
        text = m.group(0)
        has_path = 1 if "/" in text else 0
        return (has_path, len(text))

    best_score = max(score(m) for m in matches)
    candidates = [m for m in matches if score(m) == best_score]
    return candidates[-1]


# Some reference lists (common in IEEE-style technical reports) use
# "[1]", "[2]", ... numbering with NO hanging indent -- every line,
# including wrapped continuation lines, starts at the same left margin.
# For those lists, the margin-based heuristic can't tell a new reference
# from a continuation (everything looks like "new"), so we detect this
# format and switch to using the bracket number itself as the boundary.
_NUMBERED_ENTRY_RE = re.compile(r"^\[\d+\]\s*")


# Some reference lists have NEITHER a hanging indent NOR bracket
# numbering -- every line, including wrapped continuations, sits at the
# same left margin, and there's no "[n]" marker to lean on either.
# Almost every citation style (APA in particular, but this covers MLA's
# date-in-parens variants too) places a parenthetical year -- or "(n.d.)"
# for undated sources -- shortly after the reference starts, regardless
# of whether it's led by a person's name ("Smith, J. (2020)...") or an
# organization/title ("World Health Organization. (2014)...",
# "ClearTax Accountants. (2025, November 14)..."). That makes it a far
# more general boundary signal than matching person-author name patterns
# alone, which misses every org- or title-led reference in a mixed list.
#
# A new entry is only recognized where BOTH of these hold:
#   1. The line starts with a capital letter (title-case start).
#   2. Everything accumulated so far for the CURRENT entry already
#      contains a year/n.d. marker, AND the last line added ended with
#      what looks like the end of a citation (a period, or a bare
#      URL/DOI with no trailing period, which is common).
# Requiring both avoids two failure modes: without (1), any mid-sentence
# "(YYYY)"-shaped text (e.g. a subtitle like "(2025 update)") could be
# mistaken for a new entry; without (2), an entry that ends in a bare URL
# (no period) would never be recognized as "done", merging it with the
# next one.
_YEAR_OR_ND_PAREN = r"\((?:1[89]|20)\d{2}[^)]*\)|\(n\.d\.\)"
_YEAR_OR_ND_PAREN_RE = re.compile(_YEAR_OR_ND_PAREN)
_TITLE_CASE_START_RE = re.compile(r"^[A-Z]")
_ENTRY_END_RE = re.compile(r"(https?://\S+|www\.\S+|10\.\d{4,9}/\S+)$", re.IGNORECASE)


def _ends_with_url_or_doi(text):
    """True if text ends with a bare URL/DOI -- the strongest signal that
    a citation has actually finished, since a plain mid-entry period
    (e.g. the break between a title and its journal name) can look
    superficially "complete" too, but never coincides with a URL."""
    return bool(_ENTRY_END_RE.search(text.rstrip()))


_MIN_ENTRY_LENGTH_FOR_SPLIT = 60
# A citation lacking any URL/DOI can still end in a plain period, but a
# plain period alone is a much weaker signal (it can also just be the
# break between a title and its journal name mid-citation) -- so it only
# counts once the accumulated entry is unambiguously long.
_MIN_ENTRY_LENGTH_FOR_PERIOD_ONLY_SPLIT = 150


def _split_flush_left_by_year_marker(lines):
    """Group flush-left lines into entries using the year/n.d.-marker
    + entry-end combined signal described above.

    A short "Author, I. (Year)." fragment already satisfies "has a year
    marker" and "ends with a period" after just one line -- but that's
    only the opening clause of the citation, not the whole thing. Real
    references are almost always much longer than that (they still need
    a title, journal, and often a URL/DOI), so a split is only allowed
    once the accumulated entry has passed a minimum length -- otherwise
    every reference gets chopped in half right after its author/date.

    Ending in a URL/DOI is trusted at a lower length threshold, since
    it's a much stronger "this citation is actually done" signal than a
    plain period, which can also just be a title/journal sentence break
    partway through a longer citation.
    """
    entries = []
    current = []
    seen_year_marker = False

    for ln in lines:
        text = ln["text"]
        starts_title_case = bool(_TITLE_CASE_START_RE.match(text))

        prev_entry_complete = False
        if current and seen_year_marker:
            joined_len = len(" ".join(current))
            last = current[-1]
            if _ends_with_url_or_doi(last) and joined_len >= _MIN_ENTRY_LENGTH_FOR_SPLIT:
                prev_entry_complete = True
            elif last.rstrip().endswith(".") and joined_len >= _MIN_ENTRY_LENGTH_FOR_PERIOD_ONLY_SPLIT:
                prev_entry_complete = True

        is_new_entry = starts_title_case and (not current or prev_entry_complete)

        if is_new_entry and current:
            entries.append(" ".join(current))
            current = [text]
            seen_year_marker = bool(_YEAR_OR_ND_PAREN_RE.search(text))
        else:
            current.append(text)
            if _YEAR_OR_ND_PAREN_RE.search(text):
                seen_year_marker = True

    if current:
        entries.append(" ".join(current))
    return entries


# If more than this fraction of lines sit at the column margin, there's
# no meaningful hanging indent to distinguish continuations from new
# entries -- everything is flush left -- so we fall back to the
# year-marker heuristic instead of trusting the margin.
_FLUSH_LEFT_MARGIN_FRACTION = 0.85


def _group_into_entries(lines):
    """
    Group already-collected reference-section lines into full citation
    strings.

    Three strategies, chosen per document:
      1. Bracket-numbered lists ("[1] Author...", "[2] Author..."): a new
         reference starts exactly where a line begins with "[n]".
      2. Hanging-indent lists (the common academic style): a line at (or
         very near) its column's left margin starts a new reference;
         anything indented further is a continuation of the previous one.
      3. Flush-left lists with no numbering at all (no hanging indent to
         lean on, detected when nearly every line sits at the margin):
         see _split_flush_left_by_year_marker.
    """
    if not lines:
        return []

    uses_bracket_numbering = any(_NUMBERED_ENTRY_RE.match(ln["text"]) for ln in lines)

    entries = []
    current = []

    if uses_bracket_numbering:
        for ln in lines:
            is_new_entry = bool(_NUMBERED_ENTRY_RE.match(ln["text"]))
            if is_new_entry and current:
                entries.append(" ".join(current))
                current = [ln["text"]]
            else:
                current.append(ln["text"])
        if current:
            entries.append(" ".join(current))
    else:
        margins = {}
        for ln in lines:
            col = ln["col"]
            margins[col] = min(margins.get(col, ln["x0"]), ln["x0"])

        at_margin = [ln["x0"] <= margins[ln["col"]] + MARGIN_TOLERANCE for ln in lines]
        margin_fraction = sum(at_margin) / len(lines)

        if margin_fraction > _FLUSH_LEFT_MARGIN_FRACTION:
            entries = _split_flush_left_by_year_marker(lines)
        else:
            for ln in lines:
                is_new_entry = ln["x0"] <= margins[ln["col"]] + MARGIN_TOLERANCE
                if is_new_entry and current:
                    entries.append(" ".join(current))
                    current = [ln["text"]]
                else:
                    current.append(ln["text"])
            if current:
                entries.append(" ".join(current))

    entries = [re.sub(r"\s{2,}", " ", e).strip() for e in entries]
    return entries


def extract_references_from_pdf(pdf_path):
    """
    Extract every individual reference from one PDF's reference section.

    Returns a list of dicts with keys: filename, reference, doi.
    "doi" is None if no DOI was found in that reference.
    """
    pdf_path = Path(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        lines = _collect_reference_lines(pdf)

    entries = _group_into_entries(lines)

    records = []
    for entry in entries:
        doi_match = DOI_RE.search(entry)
        doi = _reattach_wrapped_suffix(entry, doi_match) if doi_match else None

        url_match = _best_url_match(entry)
        url = _reattach_wrapped_suffix(entry, url_match) if url_match else None

        records.append(
            {
                "filename": pdf_path.name,
                "reference": entry,
                "url": url,
                "doi": doi,
            }
        )
    return records


def build_references_dataframe(pdf_paths):
    """
    Extract references from multiple PDFs and return one combined
    pandas DataFrame with columns: filename, reference, doi.
    """
    import pandas as pd

    all_records = []
    for path in pdf_paths:
        path = Path(path)
        if not has_references_section(path):
            continue
        all_records.extend(extract_references_from_pdf(path))

    return pd.DataFrame(all_records, columns=["filename", "reference", "url", "doi"])



if __name__ == "__main__":
    folder = Path(FOLDER).expanduser()
    filenames = list_pdf_filenames(FOLDER)

    print(f"Found {len(filenames)} PDF(s) in {FOLDER}:\n")
    for name in filenames:
        path = folder / name
        found = has_references_section(path)
        if found:
            n = count_references(path)
            print(f"{name}: Yes ({n} reference{'s' if n != 1 else ''} found)")
        else:
            print(f"{name}: No")

    print()
    df = build_references_dataframe(folder / name for name in filenames)
    print(f"Built a DataFrame with {len(df)} references total.\n")
    print(df.to_string(index=False, max_colwidth=60))

    output_path = folder / "references.csv"
    df.to_csv(output_path, index=False)
    print(f"\nSaved to {output_path}")
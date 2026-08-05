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
FOLDER = "/Users/ep9k/Desktop/2026_files"

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
    r"appendix\w*",
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
    """True if raw_text reads as a heading that marks the end of the reference list."""
    return _matches_heading_patterns(_normalize(raw_text), SECTION_END_PATTERNS)


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

    Uses a hanging-indent left-margin heuristic: a line at (or very near)
    the section's left margin starts a new reference; anything indented
    further is a continuation of the previous one. Two-column layouts have
    a left-column margin and a right-column margin that can differ by
    hundreds of points, so each line is compared against the margin of
    *its own* column, not a single margin for the whole page.
    """
    with pdfplumber.open(pdf_path) as pdf:
        lines = _collect_reference_lines(pdf)

    if not lines:
        return 0

    margins = {}
    for ln in lines:
        col = ln["col"]
        margins[col] = min(margins.get(col, ln["x0"]), ln["x0"])

    count = sum(1 for ln in lines if ln["x0"] <= margins[ln["col"]] + MARGIN_TOLERANCE)

    # Degenerate fallback: if literally nothing landed at the margin
    # (extremely unlikely), treat the block as one reference rather than zero.
    return count if count > 0 else 1


# ----------------------------------------------------------------------------
# Building full reference text + extracting DOIs
# ----------------------------------------------------------------------------

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]]+", re.IGNORECASE)
URL_RE = re.compile(r"(https?://[^\s\"'<>)\]]+|www\.[^\s\"'<>)\]]+)", re.IGNORECASE)

# After finding a DOI/URL match, look for a short lowercase/digit token
# immediately following it (separated by the single space our line-joining
# inserts). This reattaches fragments that wrapped onto their own line in
# the original PDF -- e.g. a DOI ending "...2012.66679" continuing as "5"
# on the next line ("...2012.666795"), or "...CD006801." continuing as
# "pub2". Capped at a few iterations so it can't run away into real prose.
_WRAP_CONTINUATION_RE = re.compile(r"^ ([a-z0-9][a-z0-9._\-]{0,30})(?=[\s).,;\]]|$)")


def _reattach_wrapped_suffix(entry_text, match):
    """Extend a DOI/URL regex match to absorb any wrapped continuation tokens."""
    result = match.group(0)
    idx = match.end()
    for _ in range(3):
        m = _WRAP_CONTINUATION_RE.match(entry_text[idx:])
        if not m:
            break
        result += m.group(1)
        idx += m.end()
    return result.rstrip(".,;")


def _group_into_entries(lines):
    """
    Group already-collected reference-section lines into full citation
    strings, using each line's own column margin to decide where a new
    reference starts (see count_references for why margins are per-column).
    """
    if not lines:
        return []

    margins = {}
    for ln in lines:
        col = ln["col"]
        margins[col] = min(margins.get(col, ln["x0"]), ln["x0"])

    entries = []
    current = []
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
        records.append(
            {
                "filename": pdf_path.name,
                "reference": entry,
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

    return pd.DataFrame(all_records, columns=["filename", "reference", "doi"])



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


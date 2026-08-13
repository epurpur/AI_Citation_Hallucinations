"""
check_DOIs.py

Reads a CSV file with columns: filename, reference, url, doi
For every row that has a non-empty DOI, checks whether it's valid using the
Crossref API (https://api.crossref.org). For every row that does NOT have a
DOI but does have a URL, checks whether that URL is reachable instead --
if a DOI is present, the URL is not checked (the DOI already verifies the
reference).

Builds a pandas DataFrame that is the original CSV plus two new columns:
    "DOI Check" -   "Valid"   - if the DOI was found on Crossref
                    "Invalid" - if the DOI was not found / errored
                    ""        - if there was no DOI to check for that row
    "URL Check" -   "Valid"   - if the URL responded successfully, OR if
                                the site blocked the request (401/403/
                                429/503) -- a block is treated as
                                evidence the page exists (it has
                                something worth protecting), not as a
                                dead link
                    "Invalid" - if the URL returned another 4xx/5xx
                                status (e.g. 404, 410), or if it still
                                failed to connect/timed out after retries
                    ""        - if there was no URL to check, OR if the row
                                already has a DOI (so the URL was skipped)

The DataFrame is saved to OUTPUT_CSV.

Usage:
    python check_DOIs.py

Just edit the INPUT_CSV and OUTPUT_CSV variables below to point at your files,
then run the script with no arguments.

Requires: pip install requests pandas
"""

import sys
import time
import requests
import pandas as pd

# ----------------------------------------------------------------------------
# EDIT THESE TWO LINES to point at your input file and desired output file
INPUT_CSV = "2026_files/references.csv"
OUTPUT_CSV = "check_DOIs_output.csv"
# ----------------------------------------------------------------------------

CROSSREF_URL = "https://api.crossref.org/works/{doi}"
# Add your email here to use Crossref's "polite pool" for better reliability
DOI_HEADERS = {"User-Agent": "DOI-Checker/1.0 (mailto:your_email@example.com)"}

# Many sites (publishers, job boards, some .gov sites) block requests that
# don't look like a real browser, regardless of what a script's own
# User-Agent says -- realistic browser headers get past the simplest of
# these checks, though sites with stronger anti-bot protection (Cloudflare
# challenges, fingerprinting) will still block automated requests no
# matter what headers are sent. That's a different failure mode from a
# genuinely dead link, and is handled separately below.
URL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_DELAY_SECONDS = 0.5  # be polite to the API/servers
REQUEST_TIMEOUT = 10

# Status codes that mean "the site refused this specific request" rather
# than "this page doesn't exist" -- most commonly anti-bot/rate-limit
# defenses (Cloudflare, WAFs, login/paywall gates) reacting to an
# automated client, not evidence the URL itself is broken. Treated as
# Valid rather than Invalid, since a site actively blocking a script is
# strong evidence the page exists (it has something worth protecting).
BLOCKED_STATUS_CODES = {401, 403, 429, 503}

# A single timeout/connection error is often just a transient hiccup
# (rate-limiting, a momentarily slow server) rather than proof the URL is
# dead, so each URL gets a couple of attempts with a short pause between
# them before it's reported as Invalid.
URL_MAX_ATTEMPTS = 3
URL_RETRY_BACKOFF_SECONDS = 2


def check_doi(doi):
    """
    Check a single DOI against the Crossref API.
    Returns "Valid", "Invalid", or "" (if no DOI was given).
    """
    doi = str(doi).strip()
    if not doi or doi.lower() == "nan":
        return ""

    url = CROSSREF_URL.format(doi=doi)
    try:
        resp = requests.get(url, headers=DOI_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return "Invalid"

    return "Valid" if resp.status_code == 200 else "Invalid"


def _request_url_once(url):
    """One attempt at reaching url. Returns a requests.Response, or None
    if the request itself failed (timeout/connection error/etc)."""
    try:
        resp = requests.head(
            url, headers=URL_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        if resp.status_code >= 400 and resp.status_code not in BLOCKED_STATUS_CODES:
            # Some servers don't support HEAD properly -- retry with GET
            # before concluding the URL itself is bad.
            resp = requests.get(
                url, headers=URL_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
            )
        return resp
    except requests.RequestException:
        return None


def check_url(url):
    """
    Check whether a single URL is reachable.
    Returns "Valid", "Invalid", or "" (if no URL was given).

    A response in the 200-399 range (including redirects, which requests
    follows automatically) counts as valid. A response in
    BLOCKED_STATUS_CODES (401/403/429/503) ALSO counts as valid -- it
    means the site refused this specific automated request (usually
    anti-bot/rate-limit defenses), which is evidence the page exists, not
    that it's dead. Only a clear 404/410/other 4xx-5xx, or a connection
    failure that persists across URL_MAX_ATTEMPTS retries, is reported as
    Invalid.
    """
    url = str(url).strip()
    if not url or url.lower() == "nan":
        return ""

    resp = None
    for attempt in range(URL_MAX_ATTEMPTS):
        resp = _request_url_once(url)
        if resp is not None:
            break
        if attempt < URL_MAX_ATTEMPTS - 1:
            time.sleep(URL_RETRY_BACKOFF_SECONDS)

    if resp is None:
        return "Invalid"

    if resp.status_code < 400 or resp.status_code in BLOCKED_STATUS_CODES:
        return "Valid"
    return "Invalid"


def main():
    try:
        df = pd.read_csv(INPUT_CSV)
    except FileNotFoundError:
        print(f"Error: could not find input file '{INPUT_CSV}'.")
        print("Edit the INPUT_CSV variable at the top of this script to point at your file.")
        sys.exit(1)

    missing_cols = [c for c in ("doi", "url") if c not in df.columns]
    if missing_cols:
        print(f"Error: missing column(s) {missing_cols}. Columns present: {list(df.columns)}")
        sys.exit(1)

    def _non_empty_count(series):
        return series.notna().sum() - (series.astype(str).str.strip() == "").sum()

    def _is_empty(value):
        s = str(value).strip()
        return not s or s.lower() == "nan"

    total_with_doi = _non_empty_count(df["doi"])
    # Only rows that are missing a DOI need a URL check -- if a DOI is
    # present, that reference is already verifiable via Crossref, so
    # there's no need to also check its URL.
    total_with_url = sum(
        1
        for doi, url in zip(df["doi"], df["url"])
        if _is_empty(doi) and not _is_empty(url)
    )
    checked_doi = 0
    checked_url = 0

    print(
        f"Found {len(df)} rows total, {total_with_doi} with a DOI to check "
        f"and {total_with_url} with a URL to check (rows with a DOI skip the URL check).\n"
    )

    doi_results = []
    for doi in df["doi"]:
        doi_str = str(doi).strip()
        if not doi_str or doi_str.lower() == "nan":
            doi_results.append("")
            continue

        checked_doi += 1
        print(f"[DOI {checked_doi}/{total_with_doi}] Checking DOI: {doi_str} ...", end=" ")
        status = check_doi(doi_str)
        print(status.upper())
        doi_results.append(status)

        time.sleep(REQUEST_DELAY_SECONDS)

    url_results = []
    for doi, url in zip(df["doi"], df["url"]):
        url_str = str(url).strip()

        if not _is_empty(doi):
            # A DOI is present for this row -- no need to also check the URL.
            url_results.append("")
            continue

        if not url_str or url_str.lower() == "nan":
            url_results.append("")
            continue

        checked_url += 1
        print(f"[URL {checked_url}/{total_with_url}] Checking URL: {url_str} ...", end=" ")
        status = check_url(url_str)
        print(status.upper())
        url_results.append(status)

        time.sleep(REQUEST_DELAY_SECONDS)

    df["DOI Check"] = doi_results
    df["URL Check"] = url_results

    df.to_csv(OUTPUT_CSV, index=False)

    invalid_doi_count = (df["DOI Check"] == "Invalid").sum()
    invalid_url_count = (df["URL Check"] == "Invalid").sum()
    print(
        f"\nDone. {invalid_doi_count} invalid DOI(s) out of {checked_doi} checked, "
        f"{invalid_url_count} invalid URL(s) out of {checked_url} checked."
    )
    print(f"Results written to: {OUTPUT_CSV}")

    return df


if __name__ == "__main__":
    main()
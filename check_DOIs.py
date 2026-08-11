"""
check_DOIs.py

Reads a CSV file with columns: filename, reference, doi
For every row that has a non-empty DOI, checks whether it's valid using the
Crossref API (https://api.crossref.org).

Builds a pandas DataFrame that is the original CSV plus a new column called
"DOI Check", which is:
    "Valid"   - if the DOI was found on Crossref
    "Invalid" - if the DOI was not found / errored
    ""        - if there was no DOI to check for that row

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
INPUT_CSV = "references.csv"
OUTPUT_CSV = "check_DOIs_output.csv"
# ----------------------------------------------------------------------------

CROSSREF_URL = "https://api.crossref.org/works/{doi}"
# Add your email here to use Crossref's "polite pool" for better reliability
HEADERS = {"User-Agent": "DOI-Checker/1.0 (mailto:your_email@example.com)"}
REQUEST_DELAY_SECONDS = 0.5  # be polite to the API
REQUEST_TIMEOUT = 10


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
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return "Invalid"

    return "Valid" if resp.status_code == 200 else "Invalid"


def main():
    try:
        df = pd.read_csv(INPUT_CSV)
    except FileNotFoundError:
        print(f"Error: could not find input file '{INPUT_CSV}'.")
        print("Edit the INPUT_CSV variable at the top of this script to point at your file.")
        sys.exit(1)

    if "doi" not in df.columns:
        print(f"Error: no 'doi' column found. Columns present: {list(df.columns)}")
        sys.exit(1)

    total_with_doi = df["doi"].notna().sum() - (df["doi"].astype(str).str.strip() == "").sum()
    checked = 0

    print(f"Found {len(df)} rows total, {total_with_doi} with a DOI to check.\n")

    results = []
    for doi in df["doi"]:
        doi_str = str(doi).strip()
        if not doi_str or doi_str.lower() == "nan":
            results.append("")
            continue

        checked += 1
        print(f"[{checked}/{total_with_doi}] Checking DOI: {doi_str} ...", end=" ")
        status = check_doi(doi_str)
        print(status.upper())
        results.append(status)

        time.sleep(REQUEST_DELAY_SECONDS)

    df["DOI Check"] = results

    df.to_csv(OUTPUT_CSV, index=False)

    invalid_count = (df["DOI Check"] == "Invalid").sum()
    print(f"\nDone. {invalid_count} invalid DOI(s) out of {checked} checked.")
    print(f"Results written to: {OUTPUT_CSV}")

    return df


if __name__ == "__main__":
    main()
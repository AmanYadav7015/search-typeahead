"""
Fetch a REAL open-source dataset: Wikipedia pageviews.

Wikimedia publishes hourly pageview dumps (CC0) at
https://dumps.wikimedia.org/other/pageviews/. Each line is:

    <domain_code> <page_title> <view_count> <total_response_bytes>
    e.g.  en iPhone 4213 0

We download one pinned hourly dump (so the result is reproducible),
keep English-Wikipedia article titles, clean them into search-query-like
text, aggregate view counts, and write `data/queries.csv` in the
`query,count` format the app expects.

"Page titles ... with a count/frequency value" is exactly one of the
dataset types the assignment allows (§3), and pageviews easily exceeds
the 100k-row minimum.

Usage:
    python fetch_dataset.py                 # pinned date, ~150k rows
    python fetch_dataset.py 2024-06-01 12 200000
    DATE=2024-03-15 HOUR=09 python fetch_dataset.py

If there is no network access, the app falls back to the synthetic
generator in data_loader.py — but this script produces real data.
"""
from __future__ import annotations
import os
import re
import sys
import csv
import gzip
import time
import urllib.request
from urllib.parse import unquote

# Pinned for reproducibility (a fixed historical dump never changes).
DEFAULT_DATE = os.environ.get("DATE", "2024-06-01")   # YYYY-MM-DD
DEFAULT_HOUR = os.environ.get("HOUR", "12")           # 00..23
TARGET_ROWS = 150_000

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "queries.csv")

# Accept English Wikipedia desktop + mobile; aggregate together.
_DOMAINS = {"en", "en.m"}
# A "search-query-like" title: starts alphanumeric, 2..50 chars, only
# letters/digits/space and a few separators, and contains a letter.
_CLEAN = re.compile(r"[a-z0-9][a-z0-9 .,'&\-]{1,49}$")
_HAS_LETTER = re.compile(r"[a-z]")
_MULTISPACE = re.compile(r"\s+")


def _url(date: str, hour: str) -> str:
    y, m, _ = date.split("-")
    return (f"https://dumps.wikimedia.org/other/pageviews/"
            f"{y}/{y}-{m}/pageviews-{date.replace('-', '')}-{hour}0000.gz")


def _clean_title(raw: str) -> str | None:
    if ":" in raw or raw == "Main_Page":
        return None                     # drop namespaces (File:, Talk:, …)
    t = unquote(raw).replace("_", " ").strip().lower()
    t = _MULTISPACE.sub(" ", t)
    if not _CLEAN.match(t) or not _HAS_LETTER.search(t):
        return None
    return t


def fetch(date: str, hour: str, target: int, out: str) -> int:
    url = _url(date, hour)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".gz.tmp"

    print(f"[fetch] downloading {url}")
    t0 = time.time()
    urllib.request.urlretrieve(url, tmp)
    print(f"[fetch] downloaded {os.path.getsize(tmp)/1e6:.1f} MB in {time.time()-t0:.1f}s")

    counts: dict[str, int] = {}
    lines = kept = 0
    with gzip.open(tmp, "rt", encoding="utf-8", errors="ignore") as f:
        for line in f:
            lines += 1
            parts = line.split(" ")
            if len(parts) < 3 or parts[0] not in _DOMAINS:
                continue
            try:
                c = int(parts[2])
            except ValueError:
                continue
            title = _clean_title(parts[1])
            if title is None:
                continue
            counts[title] = counts.get(title, 0) + c
            kept += 1
    os.remove(tmp)
    print(f"[fetch] scanned {lines:,} lines, kept {kept:,} en rows, "
          f"{len(counts):,} distinct titles")

    # Top-N by view count → the realistic head-heavy distribution.
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:target]
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["query", "count"])
        w.writerows(top)
    print(f"[fetch] wrote {len(top):,} rows -> {out}")
    return len(top)


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATE
    hour = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HOUR
    target = int(sys.argv[3]) if len(sys.argv) > 3 else TARGET_ROWS
    n = fetch(date, hour, target, OUT)
    if n < 100_000:
        print(f"[fetch] WARNING: only {n} rows (< 100k). Try another hour "
              f"or raise the target.", file=sys.stderr)
        sys.exit(1)

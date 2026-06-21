"""
Load the dataset from data/queries.csv into:
  - SQLite Frequency DB (primary store: query -> count; its query
    PRIMARY-KEY B-tree is range-scanned for prefix matches — no trie)
  - TrendingTracker (seeds total counts + recency state)

CSV format: query,count  (one per line; header optional)

If the file does not exist, we synthesize a >=100k-query dataset
so the project runs out of the box. See `data/queries.csv` for the
generated file once you've run this once.
"""
from __future__ import annotations
import csv
import os
import random
from typing import Iterable, Tuple


def _read_csv(path: str) -> Iterable[Tuple[str, int]]:
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if not row:
                continue
            if i == 0 and row[0].lower() == "query":
                continue  # skip header
            if len(row) < 2:
                continue
            try:
                yield row[0].strip().lower(), int(row[1])
            except ValueError:
                continue


def load_into(db, trending, csv_path: str):
    """
    Bulk-load the dataset into the two stores of Approach 2:
      1. Frequency DB (SQLite) — the primary `query -> count` store.
         Its `query` PRIMARY-KEY B-tree is what we range-scan for
         prefix matches (no trie).
      2. TrendingTracker — seeds the in-memory count + recency state.
    Returns number of rows loaded.
    """
    if not os.path.exists(csv_path):
        _synthesize_dataset(csv_path)

    rows = list(_read_csv(csv_path))
    db.bulk_load(rows)
    for q, c in rows:
        trending.seed(q, c)
    return len(rows)


# ---------------------------------------------------------------------------
# Synthetic dataset generator — used only if the user hasn't dropped a real
# CSV in data/queries.csv. Produces ~120k realistic-looking search queries
# with Zipfian counts.
# ---------------------------------------------------------------------------

_BRANDS = [
    "iphone", "samsung galaxy", "macbook", "macbook pro", "macbook air",
    "ipad", "airpods", "apple watch", "kindle", "fire tv", "pixel",
    "oneplus", "xiaomi", "realme", "redmi", "boat", "sony", "lg", "dell xps",
    "hp pavilion", "asus rog", "lenovo thinkpad", "nintendo switch", "ps5",
    "xbox series x", "echo dot", "nest hub",
]
_PRODUCT_NOUNS = [
    "charger", "case", "screen protector", "cable", "adapter", "stand",
    "cover", "headphones", "earphones", "battery", "sleeve", "skin",
    "mount", "tripod", "ring light", "keyboard", "mouse", "monitor",
    "webcam", "ssd", "hard disk", "pendrive", "memory card", "router",
]
_HOW_TO = [
    "install python on mac", "install python on windows",
    "fix wifi not working", "reset ipad", "update macos",
    "screenshot on mac", "convert pdf to word", "compress pdf",
    "remove background", "format ssd", "factory reset android",
    "install docker", "install nodejs", "use chatgpt for free",
    "make resume in canva", "edit pdf for free", "speed up laptop",
    "transfer files iphone to mac", "install kali linux",
]
_TUTORIALS = [
    "java tutorial", "python tutorial", "react tutorial", "spring boot tutorial",
    "system design tutorial", "dsa tutorial", "leetcode roadmap",
    "kubernetes tutorial", "docker compose tutorial", "kafka tutorial",
    "redis tutorial", "postgres tutorial", "mongodb tutorial",
    "graphql tutorial", "rust tutorial", "go tutorial",
    "git tutorial", "linux tutorial", "aws tutorial", "gcp tutorial",
]
_NEWS = [
    "ipl 2026 schedule", "ipl points table", "fifa world cup",
    "india vs australia", "stock market today", "nifty 50",
    "bitcoin price", "ethereum price", "rbi repo rate",
    "weather today", "earthquake today", "election results 2026",
]
_PLACES = [
    "restaurants near me", "atm near me", "petrol pump near me",
    "hospitals in bangalore", "best biryani in hyderabad",
    "flights to goa", "hotels in manali", "trains to delhi",
    "tickets to mumbai", "things to do in jaipur",
]
_QUESTIONS = [
    "what is", "how to", "why is", "where is", "when did", "who is",
]


def _synthesize_dataset(path: str, target_rows: int = 120_000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rng = random.Random(42)
    queries: dict[str, int] = {}

    # 1. Cartesian product of brands x product nouns → typical ecomm queries
    for b in _BRANDS:
        queries[b] = rng.randint(50_000, 500_000)
        for n in _PRODUCT_NOUNS:
            queries[f"{b} {n}"] = rng.randint(500, 30_000)
        for v in range(8, 18):  # iphone 8 .. iphone 17
            queries[f"{b} {v}"] = rng.randint(5_000, 80_000)

    for q in _HOW_TO + _TUTORIALS + _NEWS + _PLACES:
        queries[q] = rng.randint(1_000, 40_000)

    # 2. Pad with synthetic long-tail queries until we hit target_rows.
    #    Zipfian counts: a few huge, many tiny — realistic search distribution.
    pool_words = _BRANDS + _PRODUCT_NOUNS + [
        "best", "top", "cheap", "buy", "online", "review", "vs", "near me",
        "2026", "free", "tutorial", "download", "price", "in india",
    ]
    while len(queries) < target_rows:
        n_words = rng.choice([2, 3, 3, 4])
        q = " ".join(rng.choice(pool_words) for _ in range(n_words))
        if q in queries:
            continue
        # Zipfian-ish count
        rank = len(queries) + 1
        count = max(1, int(100_000 / (rank ** 0.6)))
        queries[q] = count + rng.randint(0, 50)

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "count"])
        for q, c in queries.items():
            w.writerow([q, c])

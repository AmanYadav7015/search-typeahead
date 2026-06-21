"""
SQLite-backed primary data store.

Why SQLite?
- Single-file, zero-setup → reviewers can clone & run.
- ACID transactions let the batch writer flush hundreds of updates
  atomically in one round-trip.
- For 100k–1M queries this is more than fast enough; the whole
  hot-path read serving lives in the distributed suggestion cache, and
  cache misses fall back here via a prefix range scan (no trie).
"""
import sqlite3
import threading
import os
from typing import Iterable, Tuple, List

DB_PATH = os.environ.get(
    "TYPEAHEAD_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "typeahead.db"),
)


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # check_same_thread=False because the batch-writer flushes from
        # a background thread; we serialize access with a lock.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # better concurrent reads
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.Lock()
        self._init_schema()
        # counters that prove batching reduces write load
        self.total_flushes = 0
        self.total_rows_written = 0
        self.total_search_events = 0
        # how many times we fell back to the primary store (a cache miss)
        self.total_db_reads = 0

    def _init_schema(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queries (
                    query TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    last_searched_ts REAL NOT NULL DEFAULT 0,
                    recent_score REAL NOT NULL DEFAULT 0
                );
                -- No secondary index on count: the hot read (prefix_topk)
                -- range-scans the `query` PRIMARY-KEY B-tree, then sorts the
                -- small in-prefix candidate set by count. A global count
                -- index wouldn't help that prefix-scoped sort and would only
                -- add write cost on every flush.
                """
            )
            self.conn.commit()

    def bulk_load(self, rows: Iterable[Tuple[str, int]]):
        """One-time dataset ingestion."""
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO queries(query, count) VALUES(?, ?)",
                rows,
            )
            self.conn.commit()

    def flush_batch(self, agg: dict, now_ts: float):
        """
        Apply an aggregated batch of search events.
        `agg` maps query -> count_increment for this flush window.

        We do one UPSERT per unique query, all inside ONE transaction.
        If the user searches "iphone" 50 times during a 2-second window,
        this becomes exactly 1 row write, not 50.
        """
        if not agg:
            return 0
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            for q, inc in agg.items():
                cur.execute(
                    """
                    INSERT INTO queries(query, count, last_searched_ts, recent_score)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(query) DO UPDATE SET
                        count = count + excluded.count,
                        last_searched_ts = excluded.last_searched_ts,
                        recent_score = recent_score + excluded.recent_score
                    """,
                    (q, inc, now_ts, float(inc)),
                )
            self.conn.commit()
            self.total_flushes += 1
            self.total_rows_written += len(agg)
            self.total_search_events += sum(agg.values())
            return len(agg)

    # Upper sentinel for a prefix range. Our queries are lowercase
    # ASCII + space + digits, so this code point sorts after any of them
    # under SQLite's default BINARY (byte-wise) collation.
    _HIGH = "\U0010FFFF"

    def prefix_topk(self, prefix: str, limit: int = 100) -> List[Tuple[str, int]]:
        """
        Fall back to the PRIMARY STORE for a prefix (no trie!).

        Every query that starts with `prefix` forms one contiguous block
        in the `query` PRIMARY-KEY B-tree, so this is an index RANGE SCAN
        (`query >= prefix AND query < prefix+sentinel`) — exactly what a
        trie traversal would find, but using the database's own ordered
        index. We pull the top `limit` by raw count; the caller re-ranks
        the small candidate set (e.g. by recency) and keeps the top 10.

        This runs only on a suggestion-cache MISS — the read path is
        normally served from the distributed cache.
        """
        prefix = prefix.lower().strip()
        if not prefix:
            return []
        with self.lock:
            self.total_db_reads += 1
            return list(
                self.conn.execute(
                    """
                    SELECT query, count FROM queries
                    WHERE query >= ? AND query < ?
                    ORDER BY count DESC
                    LIMIT ?
                    """,
                    (prefix, prefix + self._HIGH, limit),
                )
            )

    def all_queries(self) -> List[Tuple[str, int, float, float]]:
        with self.lock:
            return list(
                self.conn.execute(
                    "SELECT query, count, last_searched_ts, recent_score FROM queries"
                )
            )

    def stats(self) -> dict:
        with self.lock:
            n = self.conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
        return {
            "rows": n,
            "total_flushes": self.total_flushes,
            "total_rows_written": self.total_rows_written,
            "total_search_events": self.total_search_events,
            "total_db_reads": self.total_db_reads,
            "write_reduction_ratio": (
                round(self.total_search_events / self.total_rows_written, 2)
                if self.total_rows_written else 0
            ),
        }

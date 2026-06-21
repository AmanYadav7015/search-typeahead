"""
Asynchronous batch writer.

Flow:
   POST /search ──► batch_writer.submit(query)  (just appends to a buffer)
                       │
                       ▼
       background loop drains every FLUSH_INTERVAL seconds
       OR immediately if buffer >= FLUSH_BATCH_SIZE
                       │
                       ▼
       aggregate duplicates (Counter)
                       │
                       ▼
       db.flush_batch(...)  ← single SQLite transaction

Why?
- Writes to a primary store are the most expensive part of a typeahead
  system. The same query is searched many times per second; storing
  each as its own row update is wasteful.
- Aggregating in memory turns N searches of the same query into 1 row
  write — proven by the `write_reduction_ratio` metric we expose.

Failure trade-off (documented in DESIGN.md):
- A crash between flushes loses the unflushed buffer (up to FLUSH_INTERVAL
  worth of search events). For typeahead this is acceptable because counts
  are statistical, not transactional. If we needed durability we'd
  front the buffer with an append-only WAL (Kafka, a local file, etc.).
"""
from __future__ import annotations
import asyncio
import time
import logging
import threading
from collections import Counter
from typing import Optional

log = logging.getLogger("typeahead.batch")


class BatchWriter:
    def __init__(self, db, cache, trending,
                 flush_interval: float = 2.0,
                 flush_batch_size: int = 200):
        self.db = db
        self.cache = cache
        self.trending = trending
        self.flush_interval = flush_interval
        self.flush_batch_size = flush_batch_size
        self.buffer: list[str] = []
        # `submit()` runs on a worker thread (FastAPI runs sync endpoints in
        # a threadpool) while the flush loop runs on the event loop, so the
        # buffer is shared across execution contexts -> guard it with a
        # *threading* lock (an asyncio.Lock would not exclude the threadpool).
        self._buf_lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        # metrics
        self.total_submissions = 0
        self.total_flushes = 0

    def submit(self, query: str):
        """Called from the request path — must be fast and non-blocking."""
        q = query.lower().strip()
        if not q:
            return
        with self._buf_lock:
            self.buffer.append(q)
            self.total_submissions += 1
        # Hot-path: only cheap in-memory work here.
        #  - trending.record bumps the in-memory count + recency (EMA) so
        #    recency-ranked suggestions react immediately;
        #  - invalidate this query's prefixes in the suggestion cache so
        #    the next read recomputes them from the primary store.
        # The durable count write to the frequency DB is deferred to the
        # batched flush (eventual consistency — stale suggestion reads are
        # acceptable per the NFRs).
        self.trending.record(q)
        self.cache.invalidate_prefixes_of(q)

    async def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stop = True
        if self._task:
            await self._task
        # final drain so we don't lose anything on graceful shutdown
        await self._flush_now()

    async def _loop(self):
        """
        Flush when EITHER trigger fires (matches the documented contract):
          - the buffer reaches `flush_batch_size`  (size-based backpressure)
          - `flush_interval` seconds have elapsed with anything buffered
        We poll on a short tick so size-based flushes react quickly.
        """
        tick = min(0.25, self.flush_interval)
        elapsed = 0.0
        while not self._stop:
            await asyncio.sleep(tick)
            elapsed += tick
            size = len(self.buffer)  # atomic read under the GIL
            if size >= self.flush_batch_size or (elapsed >= self.flush_interval and size > 0):
                await self._flush_now()
                elapsed = 0.0

    async def _flush_now(self):
        with self._buf_lock:
            if not self.buffer:
                return
            local = self.buffer
            self.buffer = []
        # Aggregate duplicates: ["iphone","iphone","ipad"] -> {"iphone":2,"ipad":1}
        agg = Counter(local)
        try:
            self.db.flush_batch(dict(agg), now_ts=time.time())
            self.total_flushes += 1
            log.info("batch flushed", extra={
                "events": len(local), "distinct_rows": len(agg),
                "total_flushes": self.total_flushes,
            })
        except Exception:
            # Don't let a flush error kill the loop; the events in `local`
            # are lost (acceptable per the NFRs), but the writer survives.
            log.exception("batch flush failed", extra={"events": len(local)})

    def stats(self) -> dict:
        return {
            "buffered": len(self.buffer),
            "total_submissions": self.total_submissions,
            "total_flushes": self.total_flushes,
            "flush_interval_s": self.flush_interval,
            "flush_batch_size": self.flush_batch_size,
        }

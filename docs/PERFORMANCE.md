# Performance Report

**Aman Yadav · 24bcs10183 · Class B · 2nd Year**
HLD101 — SST-2028

Consolidated answer to §10 (non-functional) and the §12 "performance
report" deliverable: latency (incl. p95), cache hit rate, and DB
read/write reduction through batching.

All numbers are reproducible with the included load script:

```bash
cd backend
../.venv/bin/uvicorn main:app --port 8765        # terminal 1
../.venv/bin/python bench.py 2500                 # terminal 2
```

`bench.py` fires N `/suggest` requests across a realistic mix of prefixes
and prints latency percentiles, the cache hit/miss split, and per-shard
hit rates. The same numbers are exposed live at `GET /stats` and on the
UI's telemetry tiles.

---

## 1. Latency

Dataset: 120,000 queries. Machine: local laptop (Apple Silicon),
single Uvicorn worker. 2,500 requests after a cold start.

| Metric | Value |
| --- | --- |
| p50 | **~0.4 ms** |
| p95 | **~0.45 ms** |
| p99 | **~1.5 ms** |

Why single-digit-ms (in fact sub-ms on a hit): a `/suggest` is an O(1)
`prefix → top-K` cache lookup. The p99 tail (~1.5 ms) is the **cache
miss** path — a prefix range scan against SQLite — which is exactly the
notes' model: "absorb reads in the cache; on a miss the read is slower
because it hits the store." Misses are rare behind the cache (~3–4%),
so they barely move p50/p95. See [DESIGN.md §3](DESIGN.md).

> Note: server-side `latency_ms` reported by `/stats` is even lower
> (~0.01–0.05 ms on a hit) because it times only the handler body;
> `bench.py` figures include the full localhost HTTP round-trip, which
> is the more honest end-to-end number.

---

## 2. Cache hit rate

| Metric | Value (2,500-req run) |
| --- | --- |
| Overall hit rate (warm) | **96.4 %** |
| Hits / Misses | ~2,417 / ~91 |

Per-shard hit rates stay within ~95–97 % of each other, confirming the
consistent-hash ring distributes prefixes evenly across the 4 cache
nodes:

```
cache-node-A  hits=562  misses=20  hit_rate=96.6%
cache-node-B  hits=839  misses=25  hit_rate=97.1%
cache-node-C  hits=440  misses=24  hit_rate=94.8%
cache-node-D  hits=576  misses=22  hit_rate=96.3%
```

Misses occur only on (a) the first request for a prefix and (b) after a
search invalidates that prefix or its 30 s TTL expires.

---

## 3. Consistent-hashing distribution

`GET /ring` over a 5,000-prefix sample:

```
cache-node-A: 1285   cache-node-B: 786
cache-node-C: 1348   cache-node-D: 1581
```

A ~±25 % spread across 4 physical nodes with 100 virtual nodes each.
Spread tightens toward ±5 % as node count grows — the point of vnodes.
The UI draws the actual ring (every vnode position) and highlights which
node owns a typed prefix.

---

## 4. Write reduction through batching

Searches are buffered and flushed every 2 s (or at 200 buffered), with
**duplicate queries aggregated** into a single row UPSERT per flush.

`/stats` exposes the proof directly:

| Field | Meaning |
| --- | --- |
| `db.total_search_events` | how many `/search` submissions arrived |
| `db.total_rows_written` | how many rows we actually wrote |
| `db.write_reduction_ratio` | `events / rows` |
| `writes saved` (UI tile) | `events − rows` |

- On the demo traffic (head-heavy, lots of repeats) we observe **1.6×–5×+**
  fewer writes than naïve per-request writes.
- The ratio grows with repetition: in a steady state where the top
  queries dominate (as in real search), a 2 s window collapses dozens of
  duplicate hits of the same hot query into **one** write, pushing the
  ratio to **10×–100×**.
- Reads hit the DB **only on a cache miss** (the prefix range scan).
  Behind a ~96% hit rate, suggestion serving issues a DB read on only
  ~4% of requests; the `DB reads` UI tile (`db.total_db_reads`) counts
  exactly those range-scan fallbacks, and it tracks the cache-miss count
  1:1.

**Failure trade-off:** a crash between flushes loses up to 2 s of
unflushed search events. Acceptable because counts are statistical, not
transactional; a graceful shutdown drains the buffer first. See
[DESIGN.md §6](DESIGN.md).

---

## 5. How to reproduce every number

| Claim | Where to see it |
| --- | --- |
| p50/p95/p99 latency | `bench.py` output, `/stats.latency_ms`, UI p95 tile |
| Cache hit rate + per-shard | `bench.py` output, `/stats.cache`, UI hit-rate tile |
| Consistent-hash spread | `/ring`, UI ring SVG + legend |
| Write reduction | `/stats.db`, UI "writes saved" tile |
| Which node owns a prefix | `/cache/debug?prefix=…`, UI ring + OWNER badge |

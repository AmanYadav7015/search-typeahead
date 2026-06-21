# Project Report — Search Typeahead System

**Aman Yadav** · Roll No. **24bcs10183** · Class **B** · 2nd Year
HLD101 Assignment — **SST-2028**

A search-typeahead (Google/Amazon-style suggestion) system built on the
**Hashmap / Key-Value (Approach 2)** design from the case-study notes —
**not** a trie. It serves prefix suggestions ranked by global search
count (and optionally by recency), records searches, reduces write load
via batching, and distributes its suggestion cache with consistent
hashing.

This report is the consolidated submission document. Deeper write-ups
live in [DESIGN.md](DESIGN.md), [WALKTHROUGH.md](WALKTHROUGH.md),
[PERFORMANCE.md](PERFORMANCE.md) and [VIVA.md](VIVA.md).

---

## 1. Architecture

### 1.1 Diagram

```
                    ┌──────────────────────────────────┐
   Browser  ──────► │   FastAPI app (backend/main.py)  │
   (debounced UI)   │   request-id mw + error envelope │
                    └──────────────────────────────────┘
                         │                       ▲
              GET /suggest│ (read path)          │ POST /search (write path)
                         ▼                       │
        ┌────────────────────────────────┐      │
        │  TOP-SUGGESTIONS CACHE          │      │   BatchWriter
        │  prefix → top-K   (O(1) hit)    │      │   ├─ buffer (thread-safe)
        │  4 LRU shards, TTL=30s,         │      │   ├─ EMA recency bump
        │  routed by CONSISTENT HASHING   │      │   └─ invalidate prefixes
        └────────────────────────────────┘      │        │
                         │ miss                  │        │ flush every 2s
                         ▼                       │        │  OR ≥ batch size
        ┌────────────────────────────────┐      │        ▼
        │  FREQUENCY DB  (SQLite, WAL)    │ ◄────┴───  aggregated UPSERT
        │  query → count  (primary store) │            (1 row per distinct
        │  PREFIX RANGE SCAN on the       │             query per flush)
        │  query B-tree index — no trie   │
        └────────────────────────────────┘
```

### 1.2 How it works

The system has the **two stores** the notes prescribe:

1. **Frequency DB** (`query → count`) — the durable primary store
   (SQLite, WAL mode). Because `query` is the PRIMARY KEY, SQLite keeps
   it in a sorted **B-tree**; every query sharing a prefix is a
   contiguous block, so a prefix lookup is an indexed **range scan**.
   This is what replaces the trie.
2. **Top-Suggestions cache** (`prefix → top-K`) — the read-optimized,
   consistent-hashed key-value cache. The notes' key insight: a trie's
   per-node "top-K augmentation" is *just a cache of top-K per prefix*,
   so we store it as a cache and skip the trie entirely.

**Read path (`GET /suggest`).** A cache lookup. On a **hit** (~96% of
requests) it returns in O(1). On a **miss** it falls back to the
frequency DB range scan, re-ranks the candidates by the requested mode,
caches the result, and returns. Repeat reads of the same prefix are hits.

**Write path (`POST /search`).** The handler does only cheap in-memory
work — append to a buffer, bump the in-memory recency (EMA), invalidate
the query's cached prefixes — then returns immediately. A background
task aggregates the buffer and flushes durable counts to SQLite every
2 s (or when the buffer hits the batch size). This is the read-heavy
playbook: absorb reads in the cache, optimize the store for writes.

### 1.3 Components

| File | Responsibility |
| --- | --- |
| `backend/main.py` | FastAPI app, routes, request-id middleware + error envelope, wiring |
| `backend/db.py` | Frequency DB (SQLite) + `prefix_topk()` range scan (the no-trie prefix match) |
| `backend/cache.py` | Top-Suggestions cache: LRU shards + `DistributedCache` |
| `backend/consistent_hash.py` | Hash ring with virtual nodes |
| `backend/batch_writer.py` | Thread-safe buffer + size/interval flush loop |
| `backend/trending.py` | EMA recency tracker + combined scoring |
| `backend/data_loader.py` | CSV ingestion + synthetic dataset generator |
| `frontend/` | Dark "engineering console" UI (vanilla JS) |

---

## 2. Dataset — source & loading

**Format.** `data/queries.csv`, two columns `query,count` (header
optional), matching the assignment's expected input format:

```
query,count
iphone,385243
iphone charger,4120
java tutorial,32732
...
```

**Source (default).** A **synthetic** dataset of **120,000** queries is
generated on first boot by `backend/data_loader.py` (`_synthesize_dataset`)
if `data/queries.csv` is absent. It mixes realistic e-commerce / how-to /
tutorial / news / places queries with a **Zipfian** count distribution
(a few very popular queries, a long tail of rare ones) — the same shape
as real search traffic. It is seeded (`random.Random(42)`), so every run
produces the identical dataset. This satisfies §3 (≥100k queries, with
counts). In the Docker image the dataset is baked in at build time.

**Loading.** On startup, `load_into()`:
1. bulk-loads the CSV into the SQLite frequency DB (`INSERT OR REPLACE`);
2. seeds the in-memory recency tracker with each query's baseline count.

The suggestion cache is **not** preloaded — it fills lazily on the first
read of each prefix (and is kept warm thereafter).

**Using a real open-source dataset.** Drop any `query,count` CSV at
`data/queries.csv` before first boot and it is loaded as-is (the
synthesizer only runs when the file is missing). Good sources: AOL
search logs, Google Trends exports, Wikipedia page titles + pageview
counts, an Amazon product-title dump. With Docker, mount it:
`docker run -p 8765:8765 -v /path/to/data:/app/data search-typeahead`.

---

## 3. API documentation

Base URL `http://localhost:8765`. All responses carry an `X-Request-ID`
header. Errors use a consistent envelope:
`{"error": {"code", "message", "request_id"}}`.

### `GET /suggest?q=<prefix>&mode=<popularity|recency>`
Top-10 suggestions for a prefix. `mode` defaults to `recency`; an
invalid value returns **422**.

```jsonc
// GET /suggest?q=iph&mode=popularity
{
  "prefix": "iph",
  "mode": "popularity",
  "source": "miss",                 // "hit" | "miss" | "empty"
  "cache_node": "cache-node-B",     // which shard owns this prefix
  "suggestions": [
    {"query": "iphone", "score": 385243.0, "count": 385243},
    {"query": "iphone 8", "score": 78563.0, "count": 78563}
    // … up to 10, sorted by score (count for popularity)
  ],
  "latency_ms": 1.21
}
```
- Suggestions always start with the prefix and number ≤ 10.
- Empty / missing `q` → `{"suggestions": [], "source": "empty"}`.
- Mixed-case input is normalized to lower case.

### `POST /search`
Records a search (fire-and-forget, batched) and returns the dummy result.

```jsonc
// POST /search   body: {"query": "iphone charger"}
{"message": "Searched", "query": "iphone charger"}
// empty query → 400 {"error": {"code": "http_error", "message": "empty query", "request_id": "…"}}
```

### `GET /trending`
Top recently-trending queries (recency-ranked, EMA-decayed).

```jsonc
{"trending": [{"query": "iphone charger", "score": 48.31}, … ]}
```

### `GET /cache/debug?prefix=<prefix>`
Shows which cache shard owns a prefix and whether it is currently cached.

```jsonc
{
  "prefix": "iph",
  "owner_node": "cache-node-B",
  "hit": true,
  "key_pos": 0.3729,                 // position on the 0..1 hash ring
  "all_nodes": ["cache-node-A","cache-node-B","cache-node-C","cache-node-D"],
  "ring_positions": 400
}
```

### `GET /ring`
Full virtual-node ring layout + how a prefix sample distributes across
nodes (used to render the UI ring and prove even distribution).

```jsonc
{
  "nodes": ["cache-node-A", … ],
  "vnodes_per_node": 100,
  "vnodes": [{"pos": 0.0043, "node": "cache-node-B"}, … ],  // 400 entries
  "sample_size": 5000,
  "distribution": {"cache-node-A": 1285, "cache-node-B": 786,
                   "cache-node-C": 1348, "cache-node-D": 1581}
}
```

### `GET /stats`
Live observability: latency percentiles, cache hit rate, batch metrics,
DB stats.

```jsonc
{
  "latency_ms": {"p50": 0.004, "p95": 0.04, "p99": 1.37, "samples": 600},
  "cache": {"overall_hits": 580, "overall_misses": 20,
            "overall_hit_rate": 0.966, "shards": [ … ]},
  "batch": {"buffered": 0, "total_submissions": 17, "total_flushes": 4,
            "flush_interval_s": 2.0, "flush_batch_size": 200},
  "db": {"rows": 120000, "total_search_events": 17, "total_rows_written": 4,
         "total_db_reads": 21, "write_reduction_ratio": 4.25}
}
```

### `GET /health`
Liveness probe (used by the Docker HEALTHCHECK) → `{"status": "ok"}`.

---

## 4. Design choices & trade-offs

### 4.1 No trie — Approach 2 (the central decision)
The notes reject a trie (Approach 1) because **no mainstream database is
built to store tries** — you'd hand-build and shard your own. Approach 2
observes that a trie's per-node top-K is *just a cache keyed by prefix*,
so we store `prefix → top-K` in an ordinary key-value cache (every store
— Redis/Memcached/DynamoDB — shards that for free). When we need prefix
matching on a miss, we range-scan the frequency DB's `query` B-tree —
the DB already keeps it sorted, giving us "the subtree of a prefix" with
zero custom-index code.
- **Trade-off:** a broad-prefix **miss** is slower than a trie (O(R + R·log K)
  range-scan vs O(L+K) precomputed). Accepted because misses are rare
  behind the cache, the NFRs permit slow eventually-consistent reads on a
  miss, and we gain distributable storage + no bespoke tree to maintain.

### 4.2 Recency-aware ranking (one endpoint, two modes)
`?mode=` switches the comparator over the same candidate set:
`popularity` sorts by all-time count; `recency` sorts by
`α·log(count) + β·recent_score`. `recent_score` is an **EMA** — each
search adds 1, decaying with a ~21-min half-life. This is the
continuous-time form of the notes' "decay the count by a fixed % each
period."
- **Why EMA over a sliding window:** O(1) update, constant memory, no
  sweeper, and it *naturally forgets* — a one-hour spike decays ~87%, so
  it can't permanently dominate.
- **Trade-off:** the cache holds a snapshot, so a recency shift surfaces
  only after that prefix is invalidated (on the next search) or its 30 s
  TTL expires — eventual consistency, which the NFRs allow.

### 4.3 Distributed cache + consistent hashing
4 LRU shards (TTL + capacity eviction), routed by a 2³²-ring with **100
virtual nodes per node**.
- **Consistent hashing over `hash % N`:** adding/removing a node moves
  only ~K/N keys instead of remapping (and cold-starting) the whole cache.
- **Virtual nodes:** with only 4 physical nodes, one-position-each gives
  badly skewed load; 100 vnodes/node averages it out (measured spread on
  a 5,000-prefix sample: 17 / 26 / 27 / 31 %).
- **Trade-off:** in this submission the shards are in-process (no Redis),
  so it *demonstrates* the routing pattern rather than achieving true
  multi-host scale-out. The routing code is unchanged if swapped for Redis.

### 4.4 Batched writes
`POST /search` buffers; a background loop aggregates duplicates with a
`Counter` and writes one row per distinct query per flush (every 2 s or
at `flush_batch_size`).
- **Why:** turns N searches of a hot query into 1 row write — the
  `write_reduction_ratio` metric proves it.
- **Failure trade-off:** a crash between flushes loses up to 2 s of
  events. Acceptable because counts are statistical, not transactional;
  a graceful shutdown drains the buffer first. The notes' other lever,
  **sampling** (process a random 0.1% of searches), is discussed but not
  enabled at demo scale (it would discard almost everything).

### 4.5 Engineering hygiene (applied from the team guidelines)
- **Concurrency:** the buffer is shared between a FastAPI worker thread
  (`submit`) and the event-loop flush, so it's guarded by a
  `threading.Lock` (an `asyncio.Lock` wouldn't exclude the threadpool).
- **API:** consistent error envelope + `X-Request-ID`; `mode` is a typed
  `Literal` (bad input → 422, not silent coercion); CORS narrowed off `*`.
- **Logging:** a real `logging` logger (no `print`), structured records
  on startup, each flush, and exceptions.
- **DB:** no secondary `count` index (it wouldn't help the prefix-scoped
  sort and would slow every flush).

---

## 5. Performance report

**Setup.** 120,000 queries, single Uvicorn worker, Apple-Silicon laptop.
Reproduce with `python backend/bench.py 2500` (load test) and
`python backend/test_e2e.py` (33-check functional suite). Live numbers
are at `GET /stats` and on the UI telemetry tiles.

### 5.1 Latency
| Metric | Value | Notes |
| --- | --- | --- |
| p50 (end-to-end) | **~0.40 ms** | full localhost HTTP round-trip |
| p95 (end-to-end) | **~0.45 ms** | well under the <10 ms NFR |
| p99 (end-to-end) | **~1.5 ms** | the cache-**miss** path (DB range scan) |
| server handler only | ~0.004 ms (hit) | `/stats` times the handler body |

A hit is an O(1) in-memory dict lookup; the p99 tail is the rare DB
range-scan on a miss — exactly the notes' "absorb reads in cache; on a
miss the read is slower because it hits the store."

### 5.2 Cache hit rate
- **96.5 %** overall (warm), from a 2,500-request benchmark.
- Per-shard hit rates stay within ~95–97 % of each other, confirming the
  consistent-hash ring spreads prefixes evenly.

### 5.3 Consistent-hash distribution
`GET /ring` over a 5,000-prefix sample: A 1285 / B 786 / C 1348 / D 1581
(≈ ±25 % across 4 nodes; tightens toward ±5 % as node count grows).

### 5.4 Write reduction (batching)
`/stats` exposes `total_search_events`, `total_rows_written`, and
`write_reduction_ratio = events / rows`.
- Demo traffic: **4.25×** fewer writes than naïve per-request writes.
- Grows with repetition: in steady state where head queries dominate, a
  2 s window collapses dozens of duplicate hits into one write → **10×–100×**.
- DB **reads** occur only on a cache miss (`total_db_reads`), tracking
  the miss count 1:1 (~4 % of reads behind a 96 % hit rate).

### 5.5 Correctness
The end-to-end suite (`backend/test_e2e.py`) asserts every functional
requirement (§4–§8) plus the API/validation fixes: **33 passed, 0 failed**
— both against `uvicorn` directly and against the Docker container.

### 5.6 Containerization
Multi-stage `python:3.12-slim` image, **264 MB**, non-root, with a
`/health` HEALTHCHECK and graceful SIGTERM shutdown. `docker compose up
--build` → the full app on `http://localhost:8765/`.

---

## 6. How to run

```bash
# Local
cd search-typeahead && python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd backend && ../.venv/bin/uvicorn main:app --port 8765

# Docker
cd search-typeahead && docker compose up --build
```
Open <http://localhost:8765/>. See [README.md](../README.md) for full
setup, API table, and test instructions.

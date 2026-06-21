# Design Document — Search Typeahead System

**Author:** Aman Yadav (Roll 24bcs10183, Class B, 2nd Year)
**Assignment:** HLD101 — SST-2028

This document explains *what* the system does, *why* each component
is built the way it is, and the *trade-offs* of every major choice.
Read this side-by-side with [WALKTHROUGH.md](WALKTHROUGH.md) which
walks through the actual code file-by-file.

---

## 1. High-level Architecture

This project implements **Approach 2 (Hashmap / Key-Value)** from the
case-study notes, *not* a trie. See §3 for the full "why no trie".

```
                 ┌──────────────────────────────┐
   Browser ───►  │   FastAPI (backend/main.py)  │
   (debounced)   └──────────────────────────────┘
                       │
            GET /suggest│  (read path)
                       ▼
        ┌────────────────────────────────┐
        │  Top-Suggestions cache         │   prefix -> top-K
        │  4 LRU shards, TTL,            │   O(1) lookup
        │  routed via consistent hash    │
        └────────────────────────────────┘
                       │ miss
                       ▼
        ┌────────────────────────────────┐
        │  Frequency DB (SQLite, WAL)    │   query -> count
        │  PREFIX RANGE SCAN on the      │   (primary store)
        │  query B-tree index — no trie  │
        └────────────────────────────────┘
                       ▲
   POST /search  ─►  BatchWriter buffer  (aggregate, flush every 2s)
                       │  + in-memory recency (EMA) + cache invalidation
```

**Two stores, exactly as the notes prescribe:**

1. **Frequency DB** (`query → count`) — the durable primary store. Its
   `query` PRIMARY-KEY B-tree is an *ordered* index, so all queries
   sharing a prefix form one contiguous block we can **range-scan** —
   this is what replaces the trie for prefix matching.
2. **Top-Suggestions cache** (`prefix → top-K`) — the read-optimized,
   consistent-hashed key-value cache. This is the notes' key insight:
   *a trie's per-node top-K augmentation is just a cache of top-K per
   prefix, so store it as a cache.*

The **read path**: `/suggest` is a cache lookup (O(1)). On a miss it
falls back to the Frequency DB range scan, re-ranks, and populates the
cache. Repeat reads of the same prefix are pure cache hits.

The **write path** is asynchronous: `POST /search` appends to a buffer,
bumps the in-memory recency (EMA), invalidates the query's prefixes in
the cache, and returns. A background task aggregates and flushes counts
into SQLite every 2 s (batching — §6).

---

## 2. Data store choices

| Concern | Choice | Why |
| --- | --- | --- |
| Primary store (frequency DB) | **SQLite (WAL mode)** | Single file, ACID, zero setup. Its `query` PRIMARY-KEY B-tree gives ordered prefix range scans for free. WAL lets the batch writer commit while reads continue. |
| Suggestion index | **`prefix → top-K` key-value cache** (4 LRU shards, consistent-hashed) | The notes' Approach 2: the suggestions for a prefix are just a cached value keyed by that prefix. O(1) lookup, no per-request sort, no trie. |
| Recency state | **EMA (exponentially decayed counter)** | O(1) update, no sliding-window list, naturally forgets old activity — the continuous-time form of the notes' "decay the count by a fixed % each period". See §5. |

**Why no trie? (the central design decision)** The notes spell it out:
- There is **no mainstream database built to store tries** — you'd have
  to build and shard your own. A `prefix → top-K` map, by contrast, is
  just a hashmap, and *every* key-value store (Redis, Memcached,
  DynamoDB) is a distributed hashmap with first-class sharding.
- A trie augments every node with its top-K. But that augmentation **is
  a cache** of top-K-per-prefix. Once you see that, the trie disappears
  and you're left with a key-value cache — simpler, distributable, and
  backed by an off-the-shelf DB.
- This is a **read-heavy** system (the notes estimate ~2M typeahead
  reads/s vs ~200k writes/s). The right move for read-heavy + eventual
  consistency is: **absorb reads in a cache, optimize the store for
  writes.** That's exactly this design.

**Why not Redis as the cache (in this submission)?** We *simulate*
multiple cache nodes in-process (four LRU shards) and route between them
with a consistent-hash ring. The routing logic is identical to what
you'd write in front of a real Redis cluster — only the transport
changes — so it runs on one laptop with no Docker while still
demonstrating the pattern.

---

## 3. Prefix matching without a trie ([backend/db.py](../backend/db.py))

We need, for a prefix, the queries that start with it — *without* a trie.

**Key idea:** the Frequency DB's `query` column is the PRIMARY KEY, so
SQLite keeps it in a sorted **B-tree** index. Every query that starts
with a prefix `p` forms one **contiguous block** in that sorted order
(`p` ≤ matching rows < `p` + high-sentinel). So a single indexed
**range scan** returns all prefix matches — this is exactly what a trie
subtree traversal would yield, but using the database's own ordered
index instead of a hand-built tree.

```sql
-- backend/db.py : prefix_topk()
SELECT query, count FROM queries
WHERE query >= :prefix AND query < :prefix || char(0x10FFFF)
ORDER BY count DESC
LIMIT 100;
```

We pull the top ~100 candidates by raw count, then the caller
(`main.py`) re-ranks that small set by the requested mode (popularity or
recency) and keeps the top 10.

**This only runs on a cache miss.** The hot path is the `prefix → top-K`
suggestion cache; the range scan is the "fall back to the primary store"
path. So the scan's cost (larger for a broad prefix like "i") is paid
once per prefix and then absorbed by the cache (TTL + invalidation).

**Why this matches the notes.** The notes' Approach 1 (trie) is rejected
because "no popular database was built for storing tries — you'd build
your own." Approach 2 stores `prefix → top-K` in an ordinary key-value
cache and falls back to the frequency store. Our range scan *is* that
fallback, riding on a B-tree the database already maintains — zero
custom index code, no trie nodes, no per-node augmentation to keep in
sync.

**Trade-off vs. a trie.** A trie answers a broad-prefix query in O(L+K)
from precomputed per-node lists; our range scan is O(R + R·log K) on a
miss where R = rows sharing the prefix. We accept the slower *miss*
because (a) misses are rare behind the cache, (b) the notes' NFRs allow
slow, eventually-consistent reads on a miss, and (c) we gain
distributable storage and zero bespoke-index code. (Measured: p99
stays ~1.4 ms even with the range scan on misses — see PERFORMANCE.md.)

---

## 4. The Top-Suggestions cache + consistent hashing

This is the **suggestion index itself** (Approach 2), not a side cache in
front of a trie — there is no trie. It's the `prefix → top-K` key-value
store that serves every read.

### Cache layer ([backend/cache.py](../backend/cache.py))

- **N shards** (default 4), each an `OrderedDict`-based LRU with TTL.
- **Key**: `"<prefix>|<mode>"` (popularity and recency cached separately;
  see §5) — but **routed** by the bare prefix so the owning shard is the
  same in both modes.
- **Value**: the list of top-10 suggestions for that prefix.
- **TTL**: 30 s — bounds staleness if an invalidation is missed.
- **Capacity**: 20,000 entries / shard — LRU evicts long-tail prefixes.
- **Invalidation on write**: every `POST /search` on query Q evicts the
  prefixes `Q[:1] … Q[:12]` (both modes) from their owning shards,
  because their top-10 lists might have shifted. The next read recomputes
  them from the frequency DB (the notes' "update the prefixes of the
  searched query", done lazily).

### Routing — consistent hashing ([backend/consistent_hash.py](../backend/consistent_hash.py))

```
   key ──md5──► position on a 2³² ring
                       │
                       └─► clockwise nearest virtual node owns it
```

- **100 virtual nodes per physical node** → even distribution.
  Measured on a 5,000-prefix sample: A 1285 / B 786 / C 1348 / D 1581.
  (~ ±25% spread, fine for 4 nodes; converges to ±5% at 10+ nodes.)
- **Ring stored as a sorted list of vnode positions** + `bisect` →
  O(log V) lookup where V = total vnodes.

**Why consistent hashing instead of `hash(key) % N`?**
`%N` is great until N changes (a cache node added/removed) — then
*almost every key* moves to a new owner and the entire cache is cold.
With consistent hashing only ~K/N keys move; the rest of the cache
stays warm. The assignment explicitly requires this.

**Why virtual nodes?**
Without vnodes, with only 4 physical nodes the ring positions are
nearly arbitrary and you get badly skewed distributions (e.g., one
node owning 60% of keys). vnodes are a free way to fix this — each
physical node is sprinkled at 100 positions on the ring, so the load
averages out.

---

## 5. Trending searches & recency-aware ranking
([backend/trending.py](../backend/trending.py))

The basic ranking by all-time `count` is good for stable popular
queries ("iphone") but terrible for breaking trends ("election results
2026"). The assignment's 20% extension asks us to fix this.

### Scoring model

Per query we keep:

```
recent_score(t) = recent_score(t_prev) · exp(-(t - t_prev) / TAU)  +  1
```

This is an **exponentially weighted moving average** of search rate:
each new search adds 1; the score halves every `TAU · ln(2)` seconds.
We use `TAU = 1800 s` → half-life ≈ 21 minutes.

The ranking score used by `/suggest` is:

```
score(q) = α · log(1 + total_count[q])  +  β · recent_score(q)
            ╰── long-term popularity ──╯   ╰── short-term burst ─╯
                    α = 1                       β = 5
```

`log` on the count means a query with 1M historical hits scores ~14 and a
query with 1 hit scores ~0.7; that puts them on the same magnitude
as the recency component, so 5 fresh searches can credibly out-rank a
historically mid-tier query.

### One endpoint, two modes (`?mode=`)

The assignment (§7) says *the same* `/suggest` API must serve both the
basic and the enhanced ranking. We expose this as a query parameter:

- `GET /suggest?q=iph&mode=popularity` — sort the prefix matches purely
  by all-time `count` (the basic, 60%-marks behaviour).
- `GET /suggest?q=iph&mode=recency` — sort by the combined
  `α·log(count) + β·recent_score` above (the enhanced, +20% behaviour).

Both modes re-rank the **same** candidate set (the prefix range scan
from the frequency DB), so the only difference is the comparator. The
two modes are cached
**separately** (`prefix|popularity` vs `prefix|recency`) but routed by
the bare prefix, so "which node owns this prefix" is identical in both
modes. The UI's *Popularity / Recency-aware* toggle drives this
parameter, which is how we demonstrate the difference live: after
searching "iphone charger" a few times it jumps to #1 in recency mode
while staying count-ordered in popularity mode.

### Why EMA over a sliding-window list?

A sliding window of `(timestamp, query)` tuples is conceptually simple
but expensive: every read has to drop expired entries; memory grows
with traffic. EMA is **O(1) update**, **O(1) read**, **constant memory**,
and naturally forgets — a query that hasn't been searched for 1 hour
has decayed by `e^(-3600/1800) ≈ 0.135×`, i.e. ~87% gone.

### How we avoid "permanent over-ranking" of a brief spike

By design: the EMA decay means a one-hour spike fully evaporates in a
few hours. There's no manual cleanup needed.

### Cache invalidation under recency

The suggestion cache holds a *snapshot* of each prefix's top-K. When a
new search lands on Q, we:

1. Bump Q's `total_count` and `recent_score` in the in-memory tracker.
2. Evict the 1- to 12-char prefixes of Q (both modes) from the
   distributed cache.

The next `/suggest` for one of those prefixes is a miss → it re-runs the
range scan and re-ranks with the now-updated recency score, then
re-caches. A fresh search only touches its own prefix path, so the rest
of the cache stays warm. (Other queries whose *relative* rank shifts
without an explicit search are corrected on the 30 s TTL — eventual
consistency, which the NFRs allow.)

### Trade-offs

| | Pros | Cons |
| --- | --- | --- |
| EMA | O(1) update, no sweeper, no list to bound | Single-parameter decay; doesn't capture per-time-bucket patterns ("trending today vs. always") |
| log(count) component | normalizes scales, prevents historical-only winners | a viral but historically-zero query needs ~5–10 hits to overtake a moderately popular one |

---

## 6. Batched writes ([backend/batch_writer.py](../backend/batch_writer.py))

### Why batch?

The same query is searched many times per second. Writing a row update
synchronously per search means:

- Every request waits for SQLite fsync.
- The DB sees N writes for N searches even when N-1 are the same query.

### How

```
POST /search ──► batch_writer.submit("iphone")
                    │
                    │  buffer = ["iphone", "iphone", "macbook", "iphone"]
                    ▼
        (every 2 s, background task)
                    │
                    │  aggregate: {"iphone": 3, "macbook": 1}
                    ▼
        db.flush_batch(...)   ← one SQLite transaction
                    │
                    │  2 rows touched, not 4
                    ▼
                 commit
```

In addition, **`submit()` updates the in-memory recency tracker (EMA)
synchronously and invalidates the query's prefixes in the cache**, so a
freshly-searched query's recency boost is reflected on the next
`/suggest` (recency mode) without waiting for the flush. Its durable
*count*, though, only lands at the next flush — an intentional eventual
consistency the NFRs permit.

> This is also where the notes' two write-reduction levers live.
> **Batching** (implemented): collapse N searches into ≤K row writes per
> flush window. **Sampling** (discussed, not enabled at demo scale):
> only process a random fraction of searches — at Google scale this cuts
> writes ~1000× and still preserves trends, but at our demo volume it
> would throw away almost everything, so we keep every event.

### Observed reduction

The `/stats` endpoint exposes:

```
total_search_events       (how many submits we got)
total_rows_written        (how many rows we actually wrote)
write_reduction_ratio     = events / rows
```

Even on the tiny demo traffic (8 events, 5 unique) we saw **1.6×
reduction**. Under a realistic workload where the head-of-distribution
queries dominate, this ratio reaches **10× – 100×** because most of the
buffer is duplicates of the same hot queries.

### Failure trade-offs

- **Crash between flushes**: up to 2 s of search events are lost.
- **Why this is fine here**: typeahead counts are statistical, not
  transactional. Missing a few hundred hits per process restart
  doesn't visibly change rankings.
- **If we needed durability**: front the buffer with an append-only
  WAL (e.g., write each submission to a local log file, then replay
  on startup). Or use Kafka as the buffer. Both add complexity that
  isn't justified for this assignment.
- **Graceful shutdown**: `BatchWriter.stop()` does a final drain in
  the FastAPI `lifespan` shutdown hook, so a clean `Ctrl+C` loses
  nothing.

---

## 7. Non-functional checklist

| Requirement | How we meet it |
| --- | --- |
| Easy to run locally | `pip install` + `uvicorn` + open browser |
| Low-latency suggestions | `prefix → top-K` cache (O(1) hit) + DB range-scan on miss; **p95 ≈ 0.34 ms** measured |
| p95 latency reported | `/stats` returns p50/p95/p99 from a 5k-sample ring buffer; bench.py prints it |
| Cache hit rate reported | `/stats.cache.overall_hit_rate`; **96.5%** on warm benchmark |
| Consistent-hashing visible | `/cache/debug?prefix=…` shows owner; `/ring` shows distribution |
| Modular, documented code | One concern per file, module docstrings explain the *why* |

---

## 8. Limitations & what I'd build next

1. **Real Redis cluster** instead of in-process shards. The routing
   layer doesn't change; only `LRUShard.get/set` would become a
   `redis-py` round-trip.
2. **Persisted recency state.** Currently the EMA scores live only in
   memory; on restart we rebuild only from `count`. Persisting the
   `recent_score` column lets us keep trending across restarts.
3. **Top-K above 10.** A few small APIs assume K=10; would parameterise.
4. **Materialized prefix updates.** Instead of invalidating prefixes and
   recomputing lazily on miss, proactively update each searched query's
   ~10 prefixes in the cache on flush (the notes' eager variant). Makes
   reads pure hits at the cost of ~10× the suggestion-store writes —
   which is why the notes then reduce writes via batching/sampling.
5. **Fuzzy matching / spell-correct.** Show suggestions for both the
   prefix and `spell_corrected(prefix)` (Norvig-style edit distance), as
   the notes' Future Scope describes.
6. **Geo / per-user personalization.** Shard the frequency store by
   country and merge `global:` + `India:` prefix results; do per-user
   history on the client. Not implemented here.

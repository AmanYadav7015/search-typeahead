# Code Walkthrough

**Aman Yadav · 24bcs10183 · Class B · 2nd Year**

A guided tour of every important file. Read this with the code open
in another window.

---

## Order to read the files

1. [`backend/main.py`](../backend/main.py) — the FastAPI app, the
   routes, request-id middleware + error envelope, and the wiring.
2. [`backend/db.py`](../backend/db.py) — the Frequency DB (primary store)
   **and** `prefix_topk()`, the prefix range scan that replaces the trie.
3. [`backend/cache.py`](../backend/cache.py) — the Top-Suggestions cache:
   LRU shards + the `DistributedCache` wrapper.
4. [`backend/consistent_hash.py`](../backend/consistent_hash.py) — the
   hash ring with virtual nodes.
5. [`backend/batch_writer.py`](../backend/batch_writer.py) — the
   thread-safe buffer + size/interval flush loop.
6. [`backend/trending.py`](../backend/trending.py) — the EMA recency
   tracker and the combined scoring formula.
7. [`backend/data_loader.py`](../backend/data_loader.py) — CSV
   ingestion + synthetic generator.
8. [`frontend/app.js`](../frontend/app.js) — debounced typeahead UI.

---

## Lifecycle of a request

### `GET /suggest?q=iph&mode=recency`

1. `main.py:suggest()` receives the request; `mode` defaults to
   `recency` and is validated to `{popularity, recency}`.
2. `prefix = "iph"` after `.lower().strip()`.
3. Look up in `DistributedCache.get("iph", mode)`:
    - `cache.shard_for("iph")` asks the **ring**: `_hash("iph")`,
      `bisect_right` on ring positions → returns owner shard name.
      (Routing uses the bare prefix, so the owner is the same for both
      modes; the stored key is `"iph|recency"`.)
    - The shard's `LRUShard.get("iph|recency")` checks the entry's TTL,
      returns the cached list if alive (and bumps LRU recency), else `None`.
4. **Cache hit** (`source: "hit"`): return immediately — O(1). This is
   the normal case.
   **Cache miss** (`source: "miss"`): fall back to the primary store.
    - `db.prefix_topk("iph", limit=100)` runs an indexed **range scan**
      (`query >= 'iph' AND query < 'iph'+sentinel ORDER BY count DESC`) —
      the no-trie candidate set, straight off the `query` B-tree.
    - Re-rank by mode: `popularity` keeps the count order; `recency`
      sorts by `trending.combined_score(q)`. Take the top 10, carrying
      each query's display `count`.
    - `cache.set("iph", result, mode)` populates the cache, so repeat
      reads of this prefix are hits.
5. Record latency to the ring buffer used by `/stats`.
6. JSON response:
   `{ prefix, mode, source, cache_node, suggestions:[{query,score,count}], latency_ms }`.
   The frontend uses `cache_node` for the inspector and feeds `prefix`
   to `/cache/debug` to draw the routed key on the ring.

### `POST /search` with `{"query":"iphone 17 pro"}`

1. `main.py:search()` calls `batch.submit(q)`.
2. `BatchWriter.submit` (runs on a worker thread):
    - Append to `buffer` under a `threading.Lock` (the flush loop runs
      on the event loop, so the buffer is shared across contexts).
    - `trending.record(q)` — bump EMA + in-memory count, so recency-mode
      suggestions react immediately.
    - `cache.invalidate_prefixes_of(q)` — evict `i, ip, iph, …, iphone 17 p`
      (both modes) so the next read recomputes them from the DB.
3. Return `{"message": "Searched", "query": q}` — no durable DB write yet.
4. **Background:** the loop in `BatchWriter._loop` flushes when the buffer
   hits `flush_batch_size` OR every 2 s — the buffer is swapped under the
   lock, aggregated with `Counter`, and
   written in one transaction by `db.flush_batch`. **All duplicates
   of the same query become a single row.**

---

## Why each file is structured the way it is

### `db.py` — frequency store + the no-trie prefix scan

The `queries` table keys on `query TEXT PRIMARY KEY`, so SQLite keeps it
in a sorted B-tree. `prefix_topk(prefix, limit)` exploits that: all rows
sharing a prefix are contiguous, so a single `WHERE query >= :p AND
query < :p+sentinel ORDER BY count DESC LIMIT :n` range scan returns the
candidates — **this is what replaces the whole trie.** `flush_batch`
applies an aggregated batch in one transaction (one `ON CONFLICT` UPSERT
per distinct query). Counters (`total_db_reads`, `total_rows_written`,
`total_search_events`) make the read/write behaviour observable in
`/stats`. There is deliberately **no secondary index on `count`** — it
wouldn't help the prefix-scoped sort and would only slow every flush.

### `cache.py` — composition of shards

`LRUShard` is a textbook OrderedDict-based LRU with a TTL on top.
`DistributedCache` owns N shards plus a `ConsistentHashRing` to pick
between them. Importantly, `DistributedCache` is the *only* place that
knows how to route a key — `LRUShard` itself is unaware of being
sharded. That's the right boundary: tomorrow we could swap the in-
process shards for Redis clients without changing routing code.

### `consistent_hash.py` — sorted list + bisect

Not a beautiful ring visualization but exactly what you'd write for
production. `_hash` uses md5 mod 2³² — md5 isn't crypto here, it's
just a uniform 128-bit spread. `bisect_right` over a sorted positions
list is O(log V) and avoids any Python-level loop.

### `batch_writer.py` — submit fast, flush atomically

The `submit()` path is latency-sensitive — it never does I/O, just an
append (under a `threading.Lock`, because `submit` runs on a FastAPI
worker thread while the flush loop runs on the event loop) plus the
in-memory recency bump and cache invalidation. The flush loop fires on
**either** trigger — buffer ≥ `flush_batch_size` or `flush_interval`
elapsed — swaps the buffer under the lock, and writes to SQLite in a
single `BEGIN; … COMMIT;` (one fsync per flush regardless of size). A
failed flush is logged and swallowed so the loop survives.

I deliberately *do not* use `asyncio.Queue` because we want
**aggregation**, not FIFO ordering. A `Counter` on the list at flush
time is the simplest, fastest way to do "merge by key, sum the values."

### `trending.py` — math goes here, nowhere else

All the scoring logic — EMA decay, combined `α·log(count) + β·recent`
— is in one file, with named constants at the top so reviewers can
see the knobs at a glance.

### `main.py` — wiring + cross-cutting concerns

No business logic. It glues the modules together, owns the FastAPI app,
handles startup/shutdown, serves the static frontend, and holds the two
cross-cutting concerns: a **request-id middleware** (stamps every
response with `X-Request-ID`) and **exception handlers** that return a
consistent `{"error": {code, message, request_id}}` envelope instead of
leaking raw 500s. `mode` is a `Literal` so a bad value 422s with a clear
message rather than being silently coerced.

### `frontend/app.js` — the console logic

The UI is an "engineering console": it doesn't just show suggestions,
it surfaces every system internal (cache hit/miss, routed node, batch
buffer, the consistent-hash ring) so a viva examiner can *see* the
design working. Tricks worth pointing out:

1. **Debounce.** `setTimeout(fetchSuggest, 180)` after every keystroke,
   replaced if another keystroke comes first. This collapses "i", "ip",
   "iph", … into a single request for the final prefix.
2. **AbortController.** If a new request starts while the previous one
   is in flight, we `ctrl.abort()` the old fetch so the dropdown
   never flickers stale results from a slower-arriving older response.
3. **Keyboard nav.** `ArrowDown`/`ArrowUp` move `activeIdx`, `Enter`
   submits the active suggestion (or the raw typed query), `Esc` closes.
4. **Mode toggle.** The *Popularity / Recency-aware* segmented control
   sets `mode` and re-issues `/suggest`; the difference is visible
   immediately in the dropdown order.
5. **Real consistent-hash ring.** On load we fetch `/ring` once and draw
   every virtual node as a coloured dot around an SVG circle (`drawRing`).
   On each suggest we call `/cache/debug?prefix=` and draw a line from
   the centre to the key's real ring position, highlighting the owning
   node and tagging it `OWNER` in the legend.
6. **Live telemetry.** `/stats` is polled every 4 s to update the p95,
   hit-rate, store-reads, DB-writes, writes-saved tiles and the batch
   buffer gauge.

---

## Common things you might want to change

| Goal | File(s) | Edit |
| --- | --- | --- |
| K = 10 → K = 20 suggestions | `main.py` | change the `[:10]` slice in `suggest()` |
| Wider miss candidate set | `main.py` | raise `db.prefix_topk(prefix, limit=…)` |
| More cache shards | `main.py` | extend `CACHE_NODES` list |
| Faster decay | `trending.py` | reduce `TAU_SECONDS` |
| Tip recency over popularity | `trending.py` | increase `BETA`, decrease `ALPHA` |
| Bigger flush window / size | `main.py` | change `flush_interval` / `flush_batch_size` |
| Disable cache (debug) | `cache.py` | make `LRUShard.get` always return `None` |

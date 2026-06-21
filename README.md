# Search Typeahead System

**Aman Yadav** · Roll No. **24bcs10183** · Class **B** · 2nd Year
HLD101 Assignment — **SST-2028**

![Typeahead Console — overview](docs/ui-console.png)
![Live interaction — dropdown, recency-boosted suggestion, routed consistent-hash ring](docs/ui-console-active.png)

A backend-heavy search typeahead (Google/Amazon-style suggestion) system with:

- **Approach 2 (Hashmap / Key-Value)** — *no trie.* Suggestions are served from a `prefix → top-10` cache; a miss falls back to a **prefix range scan** of the primary frequency DB → sub-millisecond suggestions
- **Distributed LRU cache** across 4 logical nodes routed via **consistent hashing** (100 vnodes/node)
- **Recency-aware ranking** using exponentially-decayed search-rate (EMA) blended with all-time popularity
- **Batched, asynchronous writes** to SQLite — repeated searches collapse into a single row update
- **Trending searches** endpoint + UI section
- A clean dark-themed frontend with debouncing, keyboard navigation, and a live metrics panel

## Measured performance (150k real Wikipedia queries, local laptop)

```
end-to-end latency (warm):  p50 ~0.40ms   p95 ~0.45ms   p99 ~1.5ms (DB-fallback on a miss)
cache hit-rate (warm):      ~96.5%
write reduction:            4.25× on demo traffic; 10×–100× in steady state
functional tests:           33 passed, 0 failed  (backend/test_e2e.py)
```
See [docs/PERFORMANCE.md](docs/PERFORMANCE.md) for the full report.

## Quick start

```bash
cd search-typeahead
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cd backend
# (optional) fetch the real Wikipedia dataset — else a synthetic one is
# auto-generated on first boot:
../.venv/bin/python fetch_dataset.py
../.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/> in the browser.

### Tests

With the server running, the end-to-end suite asserts every functional
requirement (§4–§8) plus the API/validation fixes:

```bash
cd backend
../.venv/bin/python test_e2e.py    # 33 checks; exits non-zero on any failure
```

Latest run: **33 passed, 0 failed** — prefix matching, ≤10 count-sorted
suggestions, empty/mixed-case/no-match handling, new-query insert +
existing-count increment, popularity↔recency divergence, trending,
cache hit/miss + fallback, consistent-hash spread, batch write-reduction,
the error envelope, and `mode` validation.

### Run with Docker

The app is a single self-contained service (FastAPI serving the API +
static frontend). The build bakes the dataset in — the real Wikipedia
dataset if the build host has network, else the synthetic fallback — so
no network is needed at runtime.

```bash
cd search-typeahead
docker compose up --build        # → http://localhost:8765/
# or plain docker:
docker build -t search-typeahead .
docker run -p 8765:8765 search-typeahead
```

Image details (follows `guidelines/docker_best_practices.md`): multi-stage
build, `python:3.12-slim` base, **non-root** user, stdlib `/health`
HEALTHCHECK, logs to stdout, graceful SIGTERM shutdown (drains the batch
buffer). Final image ~264 MB. To use a real dataset instead of the baked
synthetic one, mount it: `-v /path/to/data:/app/data` (with a
`queries.csv` inside).

## Dataset

- **Format:** `data/queries.csv`, two columns `query,count` (header optional),
  matching the assignment's expected input format.
- **Source — real, open-source (Wikipedia pageviews):** run
  [`backend/fetch_dataset.py`](backend/fetch_dataset.py) to download a
  *pinned* Wikimedia hourly pageviews dump (CC0), keep English article
  titles, clean them into search-query text, aggregate view counts, and
  write the **top 150,000** as `data/queries.csv`:
  ```bash
  cd backend && ../.venv/bin/python fetch_dataset.py
  ```
  Page titles + pageview counts are exactly a §3-allowed entry type, the
  counts are real and head-heavy (`2024 indian general election` 8118,
  `cleopatra` 7639, …), and 150k exceeds the 100k minimum. The pinned hour
  makes it reproducible.
- **Synthetic fallback (no network):** if the fetch can't reach Wikimedia,
  [`backend/data_loader.py`](backend/data_loader.py) (`_synthesize_dataset`)
  generates a deterministic, seeded 120k-query Zipfian dataset so the
  project always runs. (The CSV is only generated when missing.)
- **Any other dataset:** drop your own `query,count` CSV at
  `data/queries.csv` before first boot and it's loaded as-is.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/suggest?q=<prefix>&mode=<popularity\|recency>` | Top-10 matching suggestions (suggestion-cache → frequency-DB range-scan fallback). `mode` toggles all-time popularity vs. recency-aware ranking. Returns the routed `cache_node`, hit/miss `source`, and per-item `count`. |
| POST | `/search` `{query}` | Dummy "Searched" response + records the query |
| GET | `/trending` | Top-10 currently-trending queries (EMA-decayed) |
| GET | `/cache/debug?prefix=<p>` | Which cache shard owns this prefix, hit/miss status, ring position |
| GET | `/ring` | Full virtual-node ring layout + prefix distribution across shards |
| GET | `/stats` | p50/p95/p99 latency, hit rates, batch metrics, DB stats |

The frontend is an **engineering console** (dark, Space Grotesk + IBM Plex Mono) that
surfaces every system internal live: a popularity↔recency toggle, the per-request cache
hit/miss + routed node inspector, the batch-buffer fill gauge, telemetry tiles, and a
real consistent-hash ring SVG drawn from the actual virtual-node positions.

## Repository layout

```
search-typeahead/
├── backend/
│   ├── main.py              FastAPI app, routes
│   ├── db.py                Frequency DB (primary store) + prefix range-scan fallback — no trie
│   ├── cache.py             Top-Suggestions cache: LRU shards + distributed wrapper
│   ├── consistent_hash.py   Hash ring with virtual nodes
│   ├── batch_writer.py      Async buffer → batched DB flushes
│   ├── trending.py          EMA-decayed recency tracker
│   ├── fetch_dataset.py     Download the real Wikipedia-pageviews dataset
│   ├── data_loader.py       CSV ingestion + synthetic generator (fallback)
│   └── bench.py             Tiny load-test script
├── frontend/                index.html + style.css + app.js
├── data/                    queries.csv + typeahead.db (generated)
└── docs/
    ├── DESIGN.md            Architecture, choices, trade-offs
    ├── PERFORMANCE.md       Latency / hit-rate / write-reduction report
    ├── VIVA.md              Likely viva questions + answers
    └── WALKTHROUGH.md       Code-walkthrough commentary
```

**📄 [docs/PROJECT_REPORT.md](docs/PROJECT_REPORT.md) — the consolidated
submission report** (architecture, dataset, API docs, design choices &
trade-offs, performance).

Deeper write-ups: [docs/DESIGN.md](docs/DESIGN.md) for the architecture
detail, [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) for a guided code tour,
[docs/PERFORMANCE.md](docs/PERFORMANCE.md) for the full performance
report, and [docs/VIVA.md](docs/VIVA.md) for viva prep questions.

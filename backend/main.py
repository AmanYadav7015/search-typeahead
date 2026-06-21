"""
FastAPI entry point.

Routes:
  GET  /suggest?q=<prefix>          -> top-10 suggestions
  POST /search                       -> dummy "Searched" + records query
  GET  /trending                     -> top recent queries
  GET  /cache/debug?prefix=<prefix>  -> which shard owns a prefix
  GET  /stats                        -> cache hit rate, batch metrics, DB stats
  GET  /ring                         -> consistent-hash distribution sample

Static files (/) are served from ../frontend so the whole thing runs
from a single `uvicorn main:app`.
"""
from __future__ import annotations
import os
import time
import uuid
import logging
from typing import Literal
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

from db import Database
from cache import DistributedCache
from batch_writer import BatchWriter
from trending import TrendingTracker, ALPHA, BETA
from data_loader import load_into

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("typeahead")

# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

CACHE_NODES = ["cache-node-A", "cache-node-B", "cache-node-C", "cache-node-D"]
DATA_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "queries.csv")

# Approach 2 (Hashmap / Key-Value), per the case-study notes:
#   - db        : Frequency DB  (primary store: query -> count)
#   - cache     : Top-Suggestions DB (prefix -> top-K), distributed by
#                 consistent hashing. This IS the typeahead index — there
#                 is NO trie. A miss falls back to a DB prefix range scan.
db = Database()
cache = DistributedCache(CACHE_NODES, capacity_per_node=20_000, ttl=30)
trending = TrendingTracker()
batch = BatchWriter(db, cache, trending,
                    flush_interval=2.0, flush_batch_size=200)

# Latency samples (in-memory ring buffer for p50/p95)
_latency_samples: list[float] = []
_LATENCY_BUFFER = 5000


def _record_latency(ms: float):
    _latency_samples.append(ms)
    if len(_latency_samples) > _LATENCY_BUFFER:
        del _latency_samples[: len(_latency_samples) - _LATENCY_BUFFER]


@asynccontextmanager
async def lifespan(app: FastAPI):
    rows = load_into(db, trending, DATA_CSV)
    log.info("startup: dataset loaded", extra={"rows": rows})
    await batch.start()
    yield
    await batch.stop()


app = FastAPI(title="Search Typeahead — Aman Yadav 24bcs10183", lifespan=lifespan)

# The frontend is served from this same app (same origin), so CORS is only
# needed if someone opens the HTML from a different origin. Restrict to local
# dev origins rather than the wide-open "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8765", "http://127.0.0.1:8765"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- request id + consistent error envelope -------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = str(uuid.uuid4())
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


def _error_body(code: str, message: str, request_id: str | None) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body("http_error", str(exc.detail), rid),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    rid = getattr(request.state, "request_id", None)
    log.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", "Internal server error", rid),
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SearchPayload(BaseModel):
    query: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/suggest")
def suggest(q: str = Query("", description="prefix"),
            mode: Literal["popularity", "recency"] = Query("recency")):
    """
    Hot path (Approach 2 — no trie):
       Top-Suggestions cache lookup (prefix -> top-K)
         -> on HIT  : O(1) return  (this is the normal case)
         -> on MISS : fall back to the primary Frequency DB with a
                      prefix RANGE SCAN, re-rank, then populate the cache.

    Two ranking modes share this one endpoint (assignment §7):
      - popularity: sort by all-time count (historically popular first)
      - recency:    sort by α·log(count) + β·recent_score (trending first)

    Empty/whitespace input is handled gracefully (returns []).
    """
    t0 = time.perf_counter()
    prefix = (q or "").strip().lower()
    # `mode` is validated to the Literal by FastAPI (invalid -> 422).
    if not prefix:
        return {"prefix": "", "suggestions": [], "source": "empty",
                "mode": mode, "cache_node": None, "latency_ms": 0.0}

    cached = cache.get(prefix, mode)
    source = "hit"
    if cached is None:
        source = "miss"
        # Cache miss -> fall back to the PRIMARY STORE. The DB range scan
        # returns the prefix's top candidates by raw count (no trie). We
        # then re-rank that small candidate set by the requested mode.
        rows = db.prefix_topk(prefix, limit=100)
        if mode == "popularity":
            scored = [(qry, float(cnt), int(cnt)) for qry, cnt in rows]
        else:
            # blend in the in-memory recency (EMA) so trending queries
            # surface immediately, even before the next batch flush
            scored = [(qry, trending.combined_score(qry), int(cnt))
                      for qry, cnt in rows]
        scored.sort(key=lambda x: x[1], reverse=True)
        cached = [(qry, s, cnt) for qry, s, cnt in scored[:10]]
        cache.set(prefix, cached, mode)

    latency_ms = (time.perf_counter() - t0) * 1000
    _record_latency(latency_ms)
    return {
        "prefix": prefix,
        "mode": mode,
        "source": source,
        "cache_node": cache.ring.get_node(prefix),
        "suggestions": [
            {"query": qq, "score": round(s, 3), "count": int(cnt)}
            for qq, s, cnt in cached
        ],
        "latency_ms": round(latency_ms, 3),
    }


@app.get("/health")
def health():
    """Lightweight liveness probe (used by the Docker HEALTHCHECK)."""
    return {"status": "ok"}


@app.post("/search")
def search(payload: SearchPayload):
    q = (payload.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="empty query")
    batch.submit(q)
    return {"message": "Searched", "query": q}


@app.get("/trending")
def trending_route():
    return {"trending": [
        {"query": q, "score": round(s, 3)} for q, s in trending.trending(10)
    ]}


@app.get("/cache/debug")
def cache_debug(prefix: str = Query(...)):
    return cache.debug(prefix)


@app.get("/ring")
def ring_distribution():
    """
    Full ring layout (every virtual node, normalized position) plus the
    distribution of a prefix sample across nodes. The frontend draws the
    ring SVG straight from `vnodes`.
    """
    sample = []
    for q in list(trending.all_queries())[:5000]:
        for L in (1, 2, 3):
            if len(q) >= L:
                sample.append(q[:L])
    dist = cache.ring.distribution(sample[:5000])
    return {
        "nodes": list(cache.shards.keys()),
        "vnodes_per_node": cache.ring.vnodes_per_node,
        "vnodes": cache.ring.layout(),
        "sample_size": min(5000, len(sample)),
        "distribution": dist,
    }


@app.get("/stats")
def stats():
    p50 = p95 = p99 = 0.0
    if _latency_samples:
        s = sorted(_latency_samples)
        p50 = s[len(s) // 2]
        p95 = s[int(len(s) * 0.95)]
        p99 = s[int(len(s) * 0.99)]
    return {
        "latency_ms": {"p50": round(p50, 3), "p95": round(p95, 3), "p99": round(p99, 3),
                       "samples": len(_latency_samples)},
        "cache": cache.stats(),
        "batch": batch.stats(),
        "db": db.stats(),
        "trending": {
            "tracked_queries": trending.tracked_count(),
            "alpha_popularity_weight": ALPHA,
            "beta_recency_weight": BETA,
        },
    }


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/ui", StaticFiles(directory=_frontend_dir, html=True), name="ui")

    @app.get("/")
    def root():
        return FileResponse(os.path.join(_frontend_dir, "index.html"))

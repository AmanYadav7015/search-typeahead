"""
End-to-end functional test for the Search Typeahead service.

Boots nothing itself — point it at a running server:
    ../.venv/bin/uvicorn main:app --port 8765    # terminal 1
    ../.venv/bin/python test_e2e.py              # terminal 2  (or TYPEAHEAD_URL=...)

DATASET-AGNOSTIC: it discovers real queries from the running service
(no hardcoded values), so it passes on the Wikipedia dataset, the
synthetic fallback, or any `query,count` CSV. Asserts the assignment's
functional requirements (§4–§8) and the best-practice fixes (error
envelope, mode validation). Exits non-zero on any failure.
"""
import os, sys, json, time, urllib.request, urllib.error
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

BASE = os.environ.get("TYPEAHEAD_URL", "http://127.0.0.1:8765")
_passed = 0
_failed = 0


def _encode(path):
    parts = urlsplit(path)
    q = urlencode(parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit(("", "", parts.path, q, ""))


def _req(method, path, body=None):
    url = BASE + _encode(path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}   {detail}")


def section(t):
    print(f"\n=== {t} ===")


def suggest(prefix, mode="popularity"):
    return _req("GET", f"/suggest?q={prefix}&mode={mode}")[1]


# ---------------------------------------------------------------------------
section("Discovery — pick real queries from the running dataset")
# Find a short prefix that returns >= 3 popularity matches (works on any
# dataset). Prefixes are intentionally short so they're within the cache
# invalidation window (<= MAX_INVALIDATION_LEN).
CANDIDATES = ["the ", "list", "new ", "de", "ca", "ba", "ma", "20",
              "a", "s", "m", "b", "c", "p"]
PFX, R = None, []
for c in CANDIDATES:
    res = suggest(c, "popularity")["suggestions"]
    if len(res) >= 3:
        PFX, R = c, res
        break
check("found a usable prefix with >=3 matches", PFX is not None, CANDIDATES)
if PFX is None:
    print("RESULT: could not discover test data; aborting")
    sys.exit(1)
LEADER = R[0]["query"]          # highest-count match for PFX
TARGET = R[-1]["query"]         # lowest-count match in PFX's top-10
print(f"  prefix={PFX!r}  leader={LEADER!r}  target={TARGET!r}  ({len(R)} matches)")

# ---------------------------------------------------------------------------
section("§4.1 Typeahead suggestions")
d = suggest(PFX, "popularity")
sugg = d["suggestions"]
check("returns <= 10 suggestions", len(sugg) <= 10, f"got {len(sugg)}")
check("all start with the prefix", all(s["query"].startswith(PFX) for s in sugg),
      [s["query"] for s in sugg][:3])
counts = [s["count"] for s in sugg]
check("popularity sorted by count desc", counts == sorted(counts, reverse=True), counts)

d_mixed = suggest(PFX.upper(), "popularity")
check("mixed-case prefix handled", len(d_mixed["suggestions"]) > 0)
check("mixed case == lower case result",
      [s["query"] for s in d_mixed["suggestions"]] == [s["query"] for s in sugg])

st, d, _ = _req("GET", "/suggest?q=")
check("empty input -> [] gracefully", st == 200 and d["suggestions"] == [] and d["source"] == "empty")
st, d, _ = _req("GET", "/suggest")
check("missing q param -> [] gracefully", st == 200 and d["suggestions"] == [])
check("no-match prefix -> []", suggest("zzqqxyno", "popularity")["suggestions"] == [])

# ---------------------------------------------------------------------------
section("§4.2 / §5 Search submission")
st, d, _ = _req("POST", "/search", {"query": TARGET})
check("POST /search returns 200", st == 200, f"status={st}")
check('returns {"message":"Searched"}', d.get("message") == "Searched", d)

NEW = "zzznovelquery testcase"
_req("POST", "/search", {"query": NEW})
time.sleep(2.6)  # let the batch flush land
d = suggest(NEW[:6], "popularity")
check("new query inserted & retrievable after flush",
      any(s["query"] == NEW for s in d["suggestions"]), d["suggestions"])

# existing query count increments (typed prefix within invalidation window)
base = next((s["count"] for s in suggest(PFX, "popularity")["suggestions"]
             if s["query"] == TARGET), None)
for _ in range(5):
    _req("POST", "/search", {"query": TARGET})
time.sleep(2.6)
after = next((s["count"] for s in suggest(PFX, "popularity")["suggestions"]
             if s["query"] == TARGET), None)
check("existing query count increases after searches",
      base is not None and after is not None and after >= base + 5, f"{base} -> {after}")

# ---------------------------------------------------------------------------
section("§7 Trending / recency vs popularity (same API, ?mode=)")
for _ in range(10):
    _req("POST", "/search", {"query": TARGET})
time.sleep(0.3)
rec_top = suggest(PFX, "recency")["suggestions"][0]["query"]
pop_top = suggest(PFX, "popularity")["suggestions"][0]["query"]
check("recency boosts the freshly-searched query to #1", rec_top == TARGET, rec_top)
check("popularity #1 unchanged by the recent spike", pop_top == LEADER, f"{pop_top} vs {LEADER}")
check("the two modes produce different #1", rec_top != pop_top, f"rec={rec_top} pop={pop_top}")
dt = _req("GET", "/trending")[1]
check("/trending returns recency-ranked list", len(dt["trending"]) > 0)
check("the spiked query is trending", any(t["query"] == TARGET for t in dt["trending"]))

# ---------------------------------------------------------------------------
section("§6 Cache + consistent hashing")
HP = "wiki"   # a prefix we do NOT search, so its cache entry stays warm
suggest(HP, "recency")
check("repeat read is a cache hit", suggest(HP, "recency")["source"] == "hit")
dbg = _req("GET", f"/cache/debug?prefix={HP}")[1]
check("/cache/debug shows owner node", dbg.get("owner_node") in dbg.get("all_nodes", []), dbg)
check("/cache/debug reports hit/miss", isinstance(dbg.get("hit"), bool))
check("/cache/debug returns ring position", "key_pos" in dbg)
ring = _req("GET", "/ring")[1]
dist = ring["distribution"]
total = sum(dist.values())
check("consistent hashing spreads across all 4 nodes",
      all(v > 0 for v in dist.values()) and max(dist.values()) < total * 0.75, dist)
check("/ring exposes vnode layout", len(ring.get("vnodes", [])) == 4 * ring["vnodes_per_node"])

# ---------------------------------------------------------------------------
section("§8 Batch writes (write reduction)")
dbs = _req("GET", "/stats")[1]["db"]
check("more search events than DB row writes (batching aggregates)",
      dbs["total_search_events"] >= dbs["total_rows_written"],
      f'events={dbs["total_search_events"]} rows={dbs["total_rows_written"]}')
check("write_reduction_ratio >= 1.0", dbs["write_reduction_ratio"] >= 1.0, dbs["write_reduction_ratio"])
check("db_reads counted (cache-miss fallbacks)", dbs["total_db_reads"] > 0, dbs["total_db_reads"])

# ---------------------------------------------------------------------------
section("Best-practice fixes (error envelope, validation)")
st, d, _ = _req("POST", "/search", {"query": ""})
check("empty query -> 400", st == 400, st)
check("error envelope shape {error:{code,message,request_id}}",
      "error" in d and {"code", "message", "request_id"} <= set(d["error"].keys()), d)
hdrs = _req("GET", "/stats")[2]
check("X-Request-ID header present", any(k.lower() == "x-request-id" for k in hdrs))
st, _, _ = _req("GET", "/suggest?q=the&mode=bogus")
check("invalid mode -> 422", st == 422, st)

# ---------------------------------------------------------------------------
section("§10 Latency (steady-state, cache-warm)")
WARM = ["a", "b", "c", "d", "th", "the", "ma", "li", "list", "new",
        "20", "de", "ca", "ba", "s"]
for _ in range(40):
    for p in WARM:
        suggest(p, "recency")
lat = _req("GET", "/stats")[1]["latency_ms"]
check("p50 latency well under 10ms NFR", lat["p50"] < 10, f'p50={lat["p50"]}ms')
check("p95 latency under 10ms NFR (warm hot path)", lat["p95"] < 10, f'p95={lat["p95"]}ms')

# ---------------------------------------------------------------------------
print(f"\n{'='*40}\nRESULT: {_passed} passed, {_failed} failed\n{'='*40}")
sys.exit(1 if _failed else 0)

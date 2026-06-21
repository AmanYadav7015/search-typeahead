"""
Tiny benchmark — fire N /suggest requests against a running server and
report p50/p95/p99 latency + cache hit rate before and after.

Usage:
   python bench.py [N]
"""
import os, sys, time, random, json, urllib.request

PREFIXES = ["i", "ip", "iph", "iphon", "ipad", "mac", "macb", "samsung",
            "java", "python", "react", "how", "best", "top", "kindle",
            "ai", "ml", "kaf", "doc", "kub", "rust", "go", "git", "lin",
            "ips", "ipo", "ipl", "win", "fed", "del", "lap", "nig"]

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
BASE = os.environ.get("TYPEAHEAD_URL", "http://127.0.0.1:8765")

def http_get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())

before = http_get("/stats")["cache"]
lats = []
sources = {"hit": 0, "miss": 0, "empty": 0}
for i in range(N):
    p = random.choice(PREFIXES)
    if random.random() < 0.3:
        # vary length to exercise more prefixes
        p = p[:random.randint(1, len(p))]
    t = time.perf_counter()
    r = http_get(f"/suggest?q={p}")
    lats.append((time.perf_counter() - t) * 1000)
    sources[r.get("source", "miss")] += 1

lats.sort()
p50 = lats[len(lats) // 2]
p95 = lats[int(len(lats) * 0.95)]
p99 = lats[int(len(lats) * 0.99)]

after = http_get("/stats")["cache"]
print(f"requests: {N}")
print(f"p50: {p50:.2f}ms  p95: {p95:.2f}ms  p99: {p99:.2f}ms")
print(f"source breakdown: {sources}")
print(f"cache hit-rate before: {before['overall_hit_rate']*100:.1f}%")
print(f"cache hit-rate after : {after['overall_hit_rate']*100:.1f}%")
print(f"cache hits: {after['overall_hits']}  misses: {after['overall_misses']}")
print(f"per-shard:")
for s in after["shards"]:
    print(f"  {s['name']:>15}  size={s['size']:>4}  hits={s['hits']:>5}  "
          f"misses={s['misses']:>4}  hit_rate={s['hit_rate']*100:.1f}%")

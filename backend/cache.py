"""
Distributed cache layer.

We simulate N cache nodes as N in-process LRU dicts. In production each
"node" would be a separate Redis/Memcached process — the routing logic
on top stays identical.

Routing: prefix string -> ConsistentHashRing.get_node(prefix) -> shard.

Each cache entry:
   key   = prefix (lower-cased)
   value = list[(query, score)]  (the top-K suggestions for that prefix)
   ttl   = soft expiry; on read, expired entries are treated as miss.

Invalidation: when a new search submission lands on query Q, every
prefix of Q (up to MAX_INVALIDATION_LEN chars) is evicted from its
owning shard, because its top-K may have changed. Short prefixes
that match thousands of queries are the most valuable to cache and
the least likely to actually change ranking — so eviction is cheap
in practice.
"""
from __future__ import annotations
import time
import threading
from collections import OrderedDict
from typing import List, Tuple, Optional

from consistent_hash import ConsistentHashRing


DEFAULT_TTL_SECONDS = 30
MAX_INVALIDATION_LEN = 12  # don't bother invalidating very long prefixes


class LRUShard:
    def __init__(self, name: str, capacity: int = 10_000):
        self.name = name
        self.capacity = capacity
        self.data: "OrderedDict[str, Tuple[List[Tuple[str, float]], float]]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.lock = threading.Lock()

    def get(self, key: str) -> Optional[List[Tuple[str, float]]]:
        with self.lock:
            entry = self.data.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expires_at = entry
            if expires_at < time.time():
                # expired -> treat as miss & evict
                self.data.pop(key, None)
                self.misses += 1
                return None
            self.data.move_to_end(key)  # LRU touch
            self.hits += 1
            return value

    def set(self, key: str, value: List[Tuple[str, float]], ttl: float):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
            self.data[key] = (value, time.time() + ttl)
            while len(self.data) > self.capacity:
                self.data.popitem(last=False)
                self.evictions += 1

    def invalidate(self, key: str):
        with self.lock:
            self.data.pop(key, None)

    def stats(self) -> dict:
        with self.lock:
            total = self.hits + self.misses
            hit_rate = round(self.hits / total, 3) if total else 0.0
            return {
                "name": self.name,
                "size": len(self.data),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "hit_rate": hit_rate,
            }


class DistributedCache:
    def __init__(self, node_names: List[str], capacity_per_node: int = 10_000,
                 ttl: float = DEFAULT_TTL_SECONDS):
        self.ring = ConsistentHashRing(node_names)
        self.shards: dict[str, LRUShard] = {
            n: LRUShard(n, capacity=capacity_per_node) for n in node_names
        }
        self.ttl = ttl

    # We ROUTE by the bare prefix (so "which node owns this prefix" is
    # stable regardless of ranking mode) but STORE under a composite
    # "prefix|mode" key so popularity and recency results never collide.
    MODES = ("popularity", "recency")

    def shard_for(self, prefix: str) -> LRUShard:
        node_name = self.ring.get_node(prefix)
        return self.shards[node_name]

    def get(self, prefix: str, mode: str = "recency"):
        return self.shard_for(prefix).get(f"{prefix}|{mode}")

    def set(self, prefix: str, value: List[Tuple[str, float]], mode: str = "recency"):
        self.shard_for(prefix).set(f"{prefix}|{mode}", value, self.ttl)

    def invalidate_prefixes_of(self, query: str):
        """Evict every prefix of `query` (both modes) from its owning shard."""
        q = query.lower().strip()
        upto = min(len(q), MAX_INVALIDATION_LEN)
        for i in range(1, upto + 1):
            p = q[:i]
            shard = self.shard_for(p)
            for mode in self.MODES:
                shard.invalidate(f"{p}|{mode}")

    def debug(self, prefix: str) -> dict:
        prefix = prefix.lower()
        shard = self.shard_for(prefix)
        now = time.time()
        with shard.lock:
            present = any(
                (e := shard.data.get(f"{prefix}|{m}")) is not None and e[1] >= now
                for m in self.MODES
            )
        return {
            "prefix": prefix,
            "owner_node": shard.name,
            "hit": present,
            "key_pos": self.ring.key_position(prefix),
            "all_nodes": list(self.shards.keys()),
            "ring_positions": len(self.ring._ring_positions),
        }

    def stats(self) -> dict:
        shard_stats = [s.stats() for s in self.shards.values()]
        total_hits = sum(s["hits"] for s in shard_stats)
        total_misses = sum(s["misses"] for s in shard_stats)
        total = total_hits + total_misses
        return {
            "shards": shard_stats,
            "overall_hits": total_hits,
            "overall_misses": total_misses,
            "overall_hit_rate": round(total_hits / total, 3) if total else 0.0,
        }

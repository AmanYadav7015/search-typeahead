"""
Recency-aware ranking + trending searches.

Goal: a query that's HOT RIGHT NOW should out-rank a query that was
popular two years ago but is now dormant.

Approach: per-query EMA (exponentially weighted moving average) of
search rate, decaying with wall-clock time.

   recent_score(t) = recent_score(t_last) * exp(-(t - t_last) / TAU) + 1

Each new search adds 1 to the score; the score halves every
`TAU * ln(2)` seconds (~half-life). This means a 1-hour-old spike
has already decayed and can't permanently dominate the ranking.

Final ranking score used by the recency-mode /suggest re-rank:

   score = ALPHA * log(1 + total_count)         <-- long-term popularity
         + BETA  * recent_score                 <-- short-term burstiness

ALPHA dominates for queries with millions of historical searches;
BETA lets a fresh trending query (e.g., breaking news) bubble up
within a minute.

This EMA is the continuous-time form of the notes' "decay the historical
count by a fixed % each period" recency idea — instead of a discrete
-10%/day step we decay continuously by exp(-Δt/TAU).

Trade-offs (also in DESIGN.md):
- Pros: O(1) per update, no sliding-window list to maintain, no
  background sweeper, naturally forgets old activity.
- Cons: the suggestion cache holds a snapshot, so a shifted recent
  score only shows after that prefix's cache entry is invalidated or
  its TTL expires — i.e. eventually consistent, which the NFRs allow.
"""
from __future__ import annotations
import math
import time
import threading
from typing import Dict, List, Tuple

TAU_SECONDS = 1800.0  # half-life ~ 21 minutes
ALPHA = 1.0
BETA = 5.0


class TrendingTracker:
    def __init__(self, tau: float = TAU_SECONDS):
        self.tau = tau
        # query -> (recent_score, last_ts)
        self._state: Dict[str, Tuple[float, float]] = {}
        # query -> total historical count (mirrors the frequency DB)
        self._total: Dict[str, int] = {}
        self.lock = threading.Lock()

    # ---------- writes ----------

    def seed(self, query: str, total_count: int):
        """Called once at startup with the dataset baseline."""
        self._total[query] = total_count

    def record(self, query: str):
        """Called on every search submission (hot path)."""
        now = time.time()
        with self.lock:
            prev_score, prev_ts = self._state.get(query, (0.0, now))
            decayed = prev_score * math.exp(-(now - prev_ts) / self.tau)
            self._state[query] = (decayed + 1.0, now)
            self._total[query] = self._total.get(query, 0) + 1

    # ---------- reads ----------

    def recent_score(self, query: str, now: float = None) -> float:
        now = now or time.time()
        with self.lock:
            prev_score, prev_ts = self._state.get(query, (0.0, now))
            return prev_score * math.exp(-(now - prev_ts) / self.tau)

    def total(self, query: str) -> int:
        """All-time search count for a query (popularity)."""
        return self._total.get(query, 0)

    def tracked_count(self) -> int:
        """How many queries currently have live recency state."""
        with self.lock:
            return len(self._state)

    def combined_score(self, query: str, now: float = None) -> float:
        total = self._total.get(query, 0)
        return ALPHA * math.log(1 + total) + BETA * self.recent_score(query, now)

    def trending(self, k: int = 10) -> List[Tuple[str, float]]:
        """Top-K queries by *recent* activity only (not historical count)."""
        now = time.time()
        scored = []
        with self.lock:
            for q, (score, ts) in self._state.items():
                s = score * math.exp(-(now - ts) / self.tau)
                if s > 0.01:
                    scored.append((q, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def all_queries(self):
        return list(self._total.keys())

"""
Consistent hashing ring with virtual nodes.

Why consistent hashing?
- Plain `hash(key) % N` re-shuffles ~all keys when N changes
  (a node is added or removed), invalidating the whole cache.
- Consistent hashing only moves K/N keys on a topology change.
- Virtual nodes (vnodes) make the key distribution roughly even
  even with very few physical nodes.

We use a sorted list of vnode positions on a 2^32 ring + bisect
to find the owner in O(log V) where V = total vnodes.
"""
import hashlib
import bisect
from typing import List, Optional


def _hash(key: str) -> int:
    # md5 is fine here — we're not doing crypto, we want uniform spread
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % (2**32)


class ConsistentHashRing:
    def __init__(self, nodes: List[str], vnodes_per_node: int = 100):
        self.vnodes_per_node = vnodes_per_node
        self._ring_positions: List[int] = []
        self._position_to_node: dict[int, str] = {}
        self.nodes: List[str] = []
        for n in nodes:
            self.add_node(n)

    def add_node(self, node: str):
        if node in self.nodes:
            return
        self.nodes.append(node)
        for v in range(self.vnodes_per_node):
            pos = _hash(f"{node}#vn{v}")
            bisect.insort(self._ring_positions, pos)
            self._position_to_node[pos] = node

    def remove_node(self, node: str):
        if node not in self.nodes:
            return
        self.nodes.remove(node)
        for v in range(self.vnodes_per_node):
            pos = _hash(f"{node}#vn{v}")
            idx = bisect.bisect_left(self._ring_positions, pos)
            if (idx < len(self._ring_positions)
                    and self._ring_positions[idx] == pos):
                self._ring_positions.pop(idx)
            self._position_to_node.pop(pos, None)

    def get_node(self, key: str) -> Optional[str]:
        if not self._ring_positions:
            return None
        h = _hash(key)
        idx = bisect.bisect_right(self._ring_positions, h)
        # wrap around the ring
        if idx == len(self._ring_positions):
            idx = 0
        return self._position_to_node[self._ring_positions[idx]]

    def distribution(self, sample_keys: List[str]) -> dict:
        """Return how many of `sample_keys` land on each node."""
        d: dict = {n: 0 for n in self.nodes}
        for k in sample_keys:
            owner = self.get_node(k)
            if owner is not None:
                d[owner] += 1
        return d

    def key_position(self, key: str) -> float:
        """Where `key` lands on the ring, normalized to [0, 1)."""
        return _hash(key) / (2**32)

    def layout(self) -> List[dict]:
        """
        Every virtual node as {pos, node} with pos normalized to [0, 1),
        sorted clockwise. Used by the frontend to draw the ring SVG.
        """
        return [
            {"pos": p / (2**32), "node": self._position_to_node[p]}
            for p in self._ring_positions
        ]

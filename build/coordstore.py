"""Node coordinates in a form that fits in memory.

Holding them as a Python dict of ``{node_id: (lat, lon)}`` was fine while only
E-road ways were loaded - four and a half million nodes.  Adding the general
trunk network, so that gaps in E-road tagging can be bridged along real roads,
takes that to tens of millions, and a dict of that size wants roughly ten
gigabytes: an int object, a tuple and two floats per entry, plus the table.
There is not that much memory.

Three numpy arrays cost sixteen bytes a node instead of two hundred, and a
sorted id column makes lookup a binary search.  The class deliberately quacks
like the dict it replaces - ``store[node]``, ``store.get(node)``,
``node in store`` - so nothing downstream has to know.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class CoordStore:
    """Immutable node -> (lat, lon) lookup backed by sorted numpy arrays."""

    __slots__ = ("_ids", "_lat", "_lon")

    def __init__(self, ids: np.ndarray, lat: np.ndarray, lon: np.ndarray):
        order = np.argsort(ids, kind="stable")
        self._ids = ids[order]
        self._lat = lat[order]
        self._lon = lon[order]

    # -- construction -------------------------------------------------------

    @classmethod
    def from_mapping(cls, mapping) -> "CoordStore":
        count = len(mapping)
        ids = np.empty(count, dtype=np.int64)
        lat = np.empty(count, dtype=np.float64)
        lon = np.empty(count, dtype=np.float64)
        for index, (key, value) in enumerate(mapping.items()):
            ids[index] = int(key)
            lat[index] = value[0]
            lon[index] = value[1]
        return cls(ids, lat, lon)

    @classmethod
    def load(cls, path: Path) -> "CoordStore":
        with np.load(path) as data:
            return cls(data["ids"], data["lat"], data["lon"])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, ids=self._ids, lat=self._lat, lon=self._lon)

    # -- the dict-shaped surface -------------------------------------------

    def _index(self, node: int) -> int:
        position = int(np.searchsorted(self._ids, node))
        if position < self._ids.size and self._ids[position] == node:
            return position
        return -1

    def __contains__(self, node) -> bool:
        return self._index(int(node)) >= 0

    def __getitem__(self, node):
        position = self._index(int(node))
        if position < 0:
            raise KeyError(node)
        return (float(self._lat[position]), float(self._lon[position]))

    def get(self, node, default=None):
        position = self._index(int(node))
        if position < 0:
            return default
        return (float(self._lat[position]), float(self._lon[position]))

    def __len__(self) -> int:
        return int(self._ids.size)

    def __iter__(self):
        return (int(value) for value in self._ids)

    def many(self, nodes: np.ndarray):
        """Vectorised lookup: coordinates for a whole array of node ids.

        Returns ``(lat, lon, ok)`` where ``ok`` marks the ids that were found;
        the coordinate columns are zero where it is false.  One searchsorted
        over the batch replaces a binary search per node, which matters when
        the caller is filtering millions of trunk ways.
        """
        nodes = np.asarray(nodes, dtype=np.int64)
        if self._ids.size == 0:
            blank = np.zeros(nodes.shape, dtype=np.float64)
            return blank, blank.copy(), np.zeros(nodes.shape, dtype=bool)
        position = np.searchsorted(self._ids, nodes)
        np.clip(position, 0, self._ids.size - 1, out=position)
        ok = self._ids[position] == nodes
        return self._lat[position] * ok, self._lon[position] * ok, ok

    def points(self, nodes) -> list:
        """``[(lat, lon), ...]`` for those of ``nodes`` that are present.

        The obvious spelling, ``[store[n] for n in nodes if n in store]``, pays
        for two binary searches a node where one will do.  Over the twenty
        million node lookups an export makes, that difference is minutes.
        """
        if not nodes:
            return []
        ids = np.fromiter(nodes, dtype=np.int64, count=len(nodes))
        lat, lon, ok = self.many(ids)
        return list(zip(lat[ok].tolist(), lon[ok].tolist()))

    def keys(self):
        return iter(self)

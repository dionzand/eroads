"""Pin AGR control cities to real places, and prove each road reaches them.

Annex I says, for instance, that E6 runs

    Trelleborg - Malmoe - Halmstad - Goeteborg - Oslo - Lillehammer -
    Trondheim - Narvik - Olderfjord - Karasjok - Kirkenes

That is a testable claim, and it is the strongest coverage check available:
rather than sweeping the whole continent hoping to have found every E-road, ask
whether every road actually reaches every place the treaty requires, and go
looking only where it does not.

Resolving the names is itself the hard part, because names are not identifiers.
"Brest" is a city in France *and* in Belarus, and picking the nearest one to the
road fails precisely in the case that matters - when the road's geometry near
that city is what is missing.  So the chain is resolved **as a whole**: choose
one candidate per control point so that the total distance along the resulting
chain is least.  A chain through Brest, France on a road running Warszawa -
Brest - Minsk would have to cross Europe twice, so the Belarusian Brest wins on
the shape of the road rather than on a guess.
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field

from junctions import haversine_km

# How far a road may pass from one of its control cities and still count as
# serving it.  Generous: a bypass can legitimately run well outside a city.
SERVED_KM = 30.0

# Control points that are not places and should never be geocoded.
NON_PLACES = {"(missing link)", "(deleted)"}


@dataclass
class ChainPoint:
    name: str
    city_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    country: str | None = None
    ambiguous: int = 0          # how many candidates the name had
    distance_km: float | None = None   # to the road's own geometry
    served: bool = False


@dataclass
class RoadCoverage:
    road: str
    points: list[ChainPoint] = field(default_factory=list)
    geometry_km: float = 0.0

    @property
    def unmatched(self) -> list[ChainPoint]:
        return [p for p in self.points if p.city_id is None]

    @property
    def unserved(self) -> list[ChainPoint]:
        return [p for p in self.points if p.city_id and not p.served]


def resolve_chain(names: list[str], index) -> list[ChainPoint]:
    """Choose one city per control name so the whole chain is geographically tight.

    A shortest-path over the lattice of candidates: each control point offers
    its candidate cities, and consecutive choices are charged the distance
    between them.  The cheapest path through the lattice is the interpretation
    of the chain that actually looks like a road.
    """
    columns: list[list] = []
    keep: list[str] = []
    for name in names:
        if name in NON_PLACES:
            continue
        candidates = index.candidates(name)
        keep.append(name)
        columns.append(candidates)

    # Run the dynamic programme over only the columns that have candidates, so a
    # single unrecognised spelling thins the chain rather than breaking it.
    live = [position for position, candidates in enumerate(columns) if candidates]

    best: list[list[float]] = []
    back: list[list[int]] = []
    for step, position in enumerate(live):
        candidates = columns[position]
        if step == 0:
            best.append([0.0] * len(candidates))
            back.append([-1] * len(candidates))
            continue
        previous = columns[live[step - 1]]
        scores, pointers = [], []
        for city in candidates:
            best_cost, best_from = math.inf, -1
            for j, other in enumerate(previous):
                cost = best[step - 1][j] + haversine_km(
                    (city.lat, city.lon), (other.lat, other.lon))
                if cost < best_cost:
                    best_cost, best_from = cost, j
            scores.append(best_cost)
            pointers.append(best_from)
        best.append(scores)
        back.append(pointers)

    chosen: dict[int, int] = {}
    if live:
        step = len(live) - 1
        pick = min(range(len(best[step])), key=lambda i: best[step][i])
        while step >= 0 and pick >= 0:
            chosen[live[step]] = pick
            pick = back[step][pick]
            step -= 1

    points = []
    for position, name in enumerate(keep):
        candidates = columns[position]
        point = ChainPoint(name=name, ambiguous=len(candidates))
        pick = chosen.get(position)
        if pick is not None and candidates:
            city = candidates[pick]
            point.city_id, point.lat, point.lon = city.id, city.lat, city.lon
            point.country = city.country
        points.append(point)
    return points


class RoadGeometryIndex:
    """Coarse spatial index of each road's points, for "does it come near here?".

    Rounding to a grid rather than building a real spatial index is deliberate:
    the question is only ever "within a few tens of kilometres", the answer
    needs to be cheap for 250 roads times a thousand cities, and a dictionary of
    occupied cells answers it in constant time.
    """

    CELL_DEGREES = 0.25

    def __init__(self, corridors):
        self.cells: dict[str, set[tuple[int, int]]] = collections.defaultdict(set)
        self.km: collections.Counter = collections.Counter()
        self.points: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)

    def add(self, road: str, points: list[tuple[float, float]], km: float) -> None:
        self.km[road] += km
        for lat, lon in points:
            self.cells[road].add((int(lat / self.CELL_DEGREES),
                                  int(lon / self.CELL_DEGREES)))
            self.points[road].append((lat, lon))

    def distance_km(self, road: str, lat: float, lon: float,
                    cutoff_km: float = 60.0) -> float | None:
        cells = self.cells.get(road)
        if not cells:
            return None
        span = int(cutoff_km / 111.0 / self.CELL_DEGREES) + 1
        base = (int(lat / self.CELL_DEGREES), int(lon / self.CELL_DEGREES))
        near = any((base[0] + dy, base[1] + dx) in cells
                   for dy in range(-span, span + 1)
                   for dx in range(-span, span + 1))
        if not near:
            return None
        best = math.inf
        for point in self.points[road]:
            if abs(point[0] - lat) > cutoff_km / 100.0:
                continue
            best = min(best, haversine_km((lat, lon), point))
            if best < 1.0:
                break
        return best if best < math.inf else None


def build_index(network) -> RoadGeometryIndex:
    """Sample every corridor so each road has a point cloud to measure against."""
    index = RoadGeometryIndex(network.corridors)
    for corridor in network.corridors:
        points = [network.coords[n] for n in corridor.nodes[::5] if n in network.coords]
        if not points:
            continue
        for road in corridor.roads:
            index.add(road, points, corridor.km)
    return index


def assess(roster: dict, network, city_index) -> dict[str, RoadCoverage]:
    """For every road, resolve its control chain and check the road reaches it."""
    geometry = build_index(network)
    results: dict[str, RoadCoverage] = {}

    for road_id, road in sorted(roster.items()):
        coverage = RoadCoverage(road=road_id,
                                geometry_km=round(geometry.km.get(road_id, 0.0), 1))
        if not road.get("deleted"):
            coverage.points = resolve_chain(road.get("points", []), city_index)
        for point in coverage.points:
            if point.lat is None:
                continue
            distance = geometry.distance_km(road_id, point.lat, point.lon)
            point.distance_km = None if distance is None else round(distance, 1)
            point.served = distance is not None and distance <= SERVED_KM
        results[road_id] = coverage
    return results


def gap_boxes(results: dict[str, RoadCoverage], pad_degrees: float = 0.6
              ) -> list[tuple[float, float, float, float]]:
    """Bounding boxes around control cities their own road fails to reach.

    These are the only places a tag sweep still needs to look.  Overlapping
    boxes are merged crudely by rounding, which is enough: fetching a slightly
    larger area costs one query, fetching a hundred tiny ones costs a hundred.
    """
    seen: set[tuple[float, float]] = set()
    for coverage in results.values():
        for point in coverage.unserved:
            seen.add((round(point.lat, 0), round(point.lon, 0)))
    return [(lat - pad_degrees, lon - pad_degrees, lat + pad_degrees, lon + pad_degrees)
            for lat, lon in sorted(seen)]

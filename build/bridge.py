"""Close the gaps where a road is real but its tagging is not.

Two kinds of gap survive everything else:

*   **Intermittent tagging.**  A road is labelled E87 on some ways and not on
    others, so it arrives as fragments.  E87's Aegean coast came in as
    twenty-five pieces over a hundred kilometres; the tarmac joining them is
    ordinary trunk road that no E-road query would ever return.

*   **Unmapped crossings.**  The AGR writes a sea link and no ferry carries the
    road's number.  The Channel Tunnel is the case: "Le Shuttle" is a
    ``route=shuttle_train`` with no E-number, so E15 stopped at Folkestone and
    Britain was reached only via the Hook of Holland.

Both are closed the same way: find the real thing that already exists in OSM,
and give it the road's number.  Nothing is invented - a bridge is only accepted
when a genuine path of trunk road connects the two ends and is not much longer
than the gap itself, and a crossing only when a car-carrying ferry or shuttle
actually spans it.
"""

from __future__ import annotations

import collections
import heapq
import math

import numpy as np
from scipy.spatial import cKDTree

from junctions import haversine_km

# A bridging path may be this much longer than the straight-line gap before it
# stops being "the road continues here" and becomes a detour.
DETOUR_LIMIT = 2.5

# Absolute ceiling on a bridge; past this the road really is broken.
MAX_BRIDGE_KM = 120.0

# How close a crossing's ends must be to the two sides of a sea link.
CROSSING_REACH_KM = 60.0

# Trunk road is only ever loaded near a gap, in cells of about 28 km.
CELL_DEG = 0.25

# Cell coordinates are packed into one integer for a single sorted lookup;
# the stride has to exceed the number of cells spanning the world in longitude.
CELL_STRIDE = 100_000

# A component is thinned to at most this many points before being measured.
SAMPLE_CAP = 20000

EARTH_KM = 6371.0


def _ecef(lat, lon):
    """Points on a sphere of Earth radius, so a KD-tree ranks them correctly.

    Distance in this space is the chord rather than the arc, but over the tens
    of kilometres this is asked about the two agree to a part in a hundred
    thousand - and unlike a flattened lat/lon plane it stays honest from Cyprus
    to Nordkapp, which matters when one road spans both.
    """
    phi = np.radians(np.asarray(lat, dtype=np.float64))
    lam = np.radians(np.asarray(lon, dtype=np.float64))
    cos_phi = np.cos(phi)
    return np.column_stack((EARTH_KM * cos_phi * np.cos(lam),
                            EARTH_KM * cos_phi * np.sin(lam),
                            EARTH_KM * np.sin(phi)))


def _to_latlon(point):
    x, y, z = float(point[0]), float(point[1]), float(point[2])
    return (math.degrees(math.asin(max(-1.0, min(1.0, z / EARTH_KM)))),
            math.degrees(math.atan2(y, x)))


def _components(ways: dict) -> list[list[int]]:
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for way in ways.values():
        for a, b in zip(way.nodes, way.nodes[1:]):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

    groups: dict[int, list[int]] = collections.defaultdict(list)
    for node in parent:
        groups[find(node)].append(node)
    return sorted(groups.values(), key=len, reverse=True)


def _cells_around(point_a, point_b, pad_deg: float) -> set:
    """Grid cells covering the box between two points, padded."""
    lo_lat = min(point_a[0], point_b[0]) - pad_deg
    hi_lat = max(point_a[0], point_b[0]) + pad_deg
    lo_lon = min(point_a[1], point_b[1]) - pad_deg
    hi_lon = max(point_a[1], point_b[1]) + pad_deg
    cells = set()
    for i in range(int(math.floor(lo_lat / CELL_DEG)),
                   int(math.floor(hi_lat / CELL_DEG)) + 1):
        for j in range(int(math.floor(lo_lon / CELL_DEG)),
                       int(math.floor(hi_lon / CELL_DEG)) + 1):
            cells.add((i, j))
    return cells


def _piece_points(coords, piece: list):
    """ECEF points for a component, thinned, or ``None`` if it cannot be found."""
    step = max(1, len(piece) // SAMPLE_CAP)
    nodes = piece[::step]
    lat, lon, ok = coords.many(np.fromiter(nodes, dtype=np.int64,
                                           count=len(nodes)))
    if not ok.any():
        return None
    return _ecef(lat[ok], lon[ok])


def _nearest_between(points_a, tree_b, points_b):
    """Closest approach between two components, and the two points involved."""
    distance, index = tree_b.query(points_a)
    best = int(np.argmin(distance))
    return (float(distance[best]), _to_latlon(points_a[best]),
            _to_latlon(points_b[int(index[best])]))


def _trunk_graph_near(bridges: list, coords, cells: set) -> dict:
    """Adjacency over just the trunk ways that touch the given cells.

    The whole trunk network of Europe, held as a Python adjacency dict, runs to
    tens of gigabytes.  Gaps are few and local, so only the ways near one are
    ever materialised: a way is admitted when any of its nodes falls in a wanted
    cell, tested for every way at once with a vectorised coordinate lookup.
    """
    if not cells or not bridges:
        return {}

    # One flat array of every node of every trunk way, tagged with the way it
    # belongs to, so the whole membership test is a single pass.
    counts = np.fromiter((len(record[1]) for record in bridges),
                         dtype=np.int64, count=len(bridges))
    total = int(counts.sum())
    if not total:
        return {}
    flat = np.fromiter((node for record in bridges for node in record[1]),
                       dtype=np.int64, count=total)
    owner = np.repeat(np.arange(len(bridges), dtype=np.int64), counts)

    lat, lon, ok = coords.many(flat)
    key = (np.floor(lat / CELL_DEG).astype(np.int64) * CELL_STRIDE
           + np.floor(lon / CELL_DEG).astype(np.int64))
    wanted = np.unique(np.fromiter(
        (i * CELL_STRIDE + j for i, j in cells), dtype=np.int64, count=len(cells)))
    position = np.searchsorted(wanted, key).clip(0, wanted.size - 1)
    inside = ok & (wanted[position] == key)

    graph: dict[int, list] = collections.defaultdict(list)
    for index in np.unique(owner[inside]):
        record = bridges[int(index)]
        nodes = record[1]
        points = [coords.get(node) for node in nodes]
        for a, b, pa, pb in zip(nodes, nodes[1:], points, points[1:]):
            if pa is None or pb is None:
                continue
            km = haversine_km(pa, pb)
            graph[a].append((b, km, record[0]))
            graph[b].append((a, km, record[0]))
    return graph


def _shortest(graph: dict, sources: set[int], targets: set[int],
              limit_km: float):
    """Cheapest trunk path from any source node to any target node."""
    best: dict[int, float] = {}
    previous: dict[int, tuple] = {}
    queue: list[tuple] = []
    for node in sources:
        if node in graph:
            best[node] = 0.0
            heapq.heappush(queue, (0.0, node))
    while queue:
        cost, node = heapq.heappop(queue)
        if cost > best.get(node, math.inf) + 1e-9:
            continue
        if node in targets and cost > 0:
            trail = []
            while node in previous:
                node, way_id = previous[node]
                trail.append(way_id)
            return cost, set(trail)
        if cost > limit_km:
            continue
        for other, km, way_id in graph.get(node, ()):
            candidate = cost + km
            if candidate < best.get(other, math.inf) - 1e-9:
                best[other] = candidate
                previous[other] = (node, way_id)
                heapq.heappush(queue, (candidate, other))
    return None, set()


def bridge_tagging_gaps(ways: dict, coords, bridges: list,
                        make_way) -> list[tuple]:
    """Join a road's pieces along real trunk road, and label that road with it.

    The path has to be a plausible continuation, not any connection at all: it
    is refused when it is more than a couple of times the straight-line gap, or
    longer than a road would ever detour.

    Pieces are joined **nearest pair first**, not each to the largest piece.
    Bridging everything to the largest piece looks reasonable until a road has
    pieces on both sides of water: E15's largest piece is France, so its London
    piece measured itself against the French coast, tried to path-find across
    the Channel, failed - and the five-kilometre gap on the A20 next door, the
    one actually severing London from Folkestone and the tunnel, was never
    considered at all.  Taking the shortest gap first grows each road the way it
    is really joined up.

    Gaps are surveyed first and the trunk network then loaded once for all of
    them together, so the expensive part is paid on the few hundred kilometres
    that matter rather than on the continent.
    """
    by_road: dict[str, list] = collections.defaultdict(list)
    for way in ways.values():
        for road in way.roads:
            by_road[road].append(way)

    # -- survey: every pair of pieces close enough to be worth trying --------
    plan: dict[str, list] = {}
    pieces_of: dict[str, list] = {}
    cells: set = set()
    for road, road_ways in sorted(by_road.items()):
        pieces = _components({w.id: w for w in road_ways})
        if len(pieces) < 2:
            continue

        points = [_piece_points(coords, piece) for piece in pieces]
        trees = [cKDTree(p) if p is not None else None for p in points]

        candidates: list[tuple] = []
        for i in range(len(pieces)):
            if points[i] is None:
                continue
            for j in range(i + 1, len(pieces)):
                if trees[j] is None:
                    continue
                gap, point_a, point_b = _nearest_between(points[i], trees[j],
                                                         points[j])
                if gap > MAX_BRIDGE_KM:
                    continue
                candidates.append((gap, i, j, point_a, point_b))
        if not candidates:
            continue

        # Nearest first, and only ever enough pairs to span the pieces a few
        # times over - the rest cannot win and would only enlarge the survey.
        candidates.sort(key=lambda row: row[0])
        del candidates[4 * len(pieces):]
        plan[road] = candidates
        pieces_of[road] = pieces
        for gap, _, _, point_a, point_b in candidates:
            cells |= _cells_around(point_a, point_b, max(gap, 10.0) / 111.0)

    if not plan:
        return []
    graph = _trunk_graph_near(bridges, coords, cells)
    by_id = {record[0]: record for record in bridges}

    joined: list[tuple] = []
    for road, candidates in sorted(plan.items()):
        pieces = pieces_of[road]
        # Union-find over the pieces, so a pair already joined - directly or
        # through a chain of earlier bridges - is skipped, while a pair whose
        # bridge fails can still be reached by a longer one later on.
        parent = list(range(len(pieces)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        merged = [set(piece) for piece in pieces]
        for gap, i, j, _, _ in candidates:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            cost, used = _shortest(graph, merged[ri], merged[rj],
                                   min(MAX_BRIDGE_KM, gap * DETOUR_LIMIT))
            if cost is None or not used:
                continue
            for way_id in used:
                record = by_id.get(way_id)
                if record is None:
                    continue
                existing = ways.get(way_id)
                if existing is not None:
                    existing.roads = existing.roads | {road}
                else:
                    ways[way_id] = make_way(record, {road})
            parent[rj] = ri
            merged[ri] |= merged[rj]
            merged[rj] = merged[ri]
            joined.append((road, round(gap, 1), round(cost, 1), len(used)))
    return joined


# A port is not always the control city the treaty names - E5 sails from
# Portsmouth where Annex I says Southampton, 26 km away - so the match allows
# some slack, but not enough to reach the next port along.
SEA_LINK_REACH_KM = 55.0

# The slack also has to be small next to the crossing itself.  Dover and Calais
# are 42 km apart, so a fixed 55 km put *both* shores within reach of any point
# in Kent and the two ends stopped meaning anything: Folkestone to Ashford was
# accepted by reading Folkestone as the Calais side.  Half the span is the most
# that can still tell one shore from the other.
SEA_LINK_REACH_FRACTION = 0.45
SEA_LINK_REACH_FLOOR_KM = 8.0


def attach_crossings(ways: dict, coords, crossings: list,
                     sea_links: dict, make_crossing) -> list[tuple]:
    """Give a car-carrying crossing the number of the road whose sea link it spans.

    The treaty says where a road crosses water and *between which two places*;
    OSM says where the boats and shuttles run.  Where those agree - one end of
    the crossing near one named place, the other end near the other - the
    crossing is that road, whatever its tags say.  This is what puts the Channel
    Tunnel on E15.

    The earlier rule only asked whether the two ends were near two *different*
    pieces of the road, which sounds equivalent and is not.  A fragmented road
    has pieces everywhere, so the test passed on things that cross no sea the
    treaty knows about: nine crossings ended up on E15, among them Folkestone to
    Ashford and Boulogne to Calais - neither of which leaves its own country -
    and nine commuter ferries around the Bosphorus were adopted onto E80,
    including a Kadikoy-to-Kadikoy round trip.  E70 acquired a 1 132 km line
    from Varna straight to Georgia, when Annex I sails it only as far as Samsun.
    Matching against the named endpoints says what was meant all along.
    """
    if not crossings or not sea_links:
        return []

    kept, end_a, end_b = [], [], []
    for record in crossings:
        nodes = record[1]
        first, last = coords.get(nodes[0]), coords.get(nodes[-1])
        if first is None or last is None:
            continue
        kept.append(record)
        end_a.append(first)
        end_b.append(last)
    if not kept:
        return []

    near_a = _ecef([p[0] for p in end_a], [p[1] for p in end_a])
    near_b = _ecef([p[0] for p in end_b], [p[1] for p in end_b])

    attached: list[tuple] = []
    for road_id, pairs in sorted(sea_links.items()):
        for here, there in pairs:
            side_one = _ecef([here[0]], [here[1]])[0]
            side_two = _ecef([there[0]], [there[1]])[0]
            span = haversine_km(here, there)
            reach = max(SEA_LINK_REACH_FLOOR_KM,
                        min(SEA_LINK_REACH_KM, SEA_LINK_REACH_FRACTION * span))
            to_one_a = np.linalg.norm(near_a - side_one, axis=1)
            to_two_a = np.linalg.norm(near_a - side_two, axis=1)
            to_one_b = np.linalg.norm(near_b - side_one, axis=1)
            to_two_b = np.linalg.norm(near_b - side_two, axis=1)
            # Either end may be the near shore, so both orientations count.
            accept = (((to_one_a <= reach) & (to_two_b <= reach))
                      | ((to_two_a <= reach) & (to_one_b <= reach)))

            for index in np.flatnonzero(accept):
                record = kept[int(index)]
                existing = ways.get(record[0])
                if existing is not None:
                    if road_id in existing.roads:
                        continue
                    existing.roads = existing.roads | {road_id}
                else:
                    ways[record[0]] = make_crossing(record, {road_id})
                attached.append((road_id, record[2] or "unnamed",
                                 round(haversine_km(end_a[int(index)],
                                                    end_b[int(index)]), 1)))
    return attached

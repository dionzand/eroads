"""Where E-roads actually meet, and the small graph that routing runs on.

The corridor graph from :mod:`graph` is faithful but far too detailed to route
on or to ship to a browser: every ramp and every carriageway split is a vertex.
This module reduces it to the only thing a traveller cares about - the places
where you can change from one E-road to another - and the runs of road between
them.

Two ideas do the work:

*   **A junction is where the set of E-roads on the pavement changes.**  Not
    where two lines cross on a map: E-roads cross on grade-separated bridges all
    the time without connecting, and they run concurrently for hundreds of
    kilometres while sharing every node.  Comparing road *sets* across the
    corridors meeting at a node gets both cases right, and gets concurrency
    right for free - if a corridor carries {E25, E35}, you are on both at once
    and moving between them costs nothing.

*   **An interchange is a cluster of such nodes, not a point.**  A motorway
    interchange sprawls over a couple of kilometres of ramps, and the start of a
    concurrency is a slip road, not a crossing.  Clustering along *short
    corridors* rather than by straight-line distance means two junctions a
    kilometre apart on the map but twenty apart along the road stay separate.
"""

from __future__ import annotations

import collections
import heapq
import math
from dataclasses import dataclass, field

from graph import Corridor, Network, INTERCHANGE_LINK_KM, INTERCHANGE_MAX_RADIUS_KM

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


@dataclass
class Interchange:
    id: int
    lat: float
    lon: float
    vertices: set[int]
    roads: set[str] = field(default_factory=set)
    radius_km: float = 0.0
    label: str = ""
    country: str | None = None
    near_city: str | None = None      # city id, never a bare name
    near_city_km: float = 0.0

    @property
    def key(self) -> str:
        return "jx%d" % self.id


@dataclass
class Leg:
    """A directed run of one E-road between two interchanges."""
    road: str
    start: int          # interchange id
    end: int            # interchange id
    km: float
    corridors: list[int]
    national: list[tuple[str, float]]
    ferry: bool
    countries: list[str] = field(default_factory=list)


class RoadNetwork:
    def __init__(self, network: Network):
        self.net = network
        self.incident: dict[int, list[tuple[int, int]]] = {}
        self.interchanges: list[Interchange] = []
        self.of_vertex: dict[int, int] = {}
        self.legs: list[Leg] = []
        self.rejected_legs: list[Leg] = []
        self.loop_legs = 0

    # -- adjacency ----------------------------------------------------------

    def build_adjacency(self) -> None:
        """Directed adjacency over corridors, honouring one-way carriageways."""
        forward: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        for corridor in self.net.corridors:
            if corridor.oneway >= 0:
                forward[corridor.start].append((corridor.end, corridor.id))
            if corridor.oneway <= 0:
                forward[corridor.end].append((corridor.start, corridor.id))
        self.incident = forward

        undirected: dict[int, list[int]] = collections.defaultdict(list)
        for corridor in self.net.corridors:
            undirected[corridor.start].append(corridor.id)
            undirected[corridor.end].append(corridor.id)
        self.touching = undirected

    # -- junctions ----------------------------------------------------------

    def junction_vertices(self) -> set[int]:
        """Vertices where you can move between two different E-roads."""
        found = set()
        for vertex, corridor_ids in self.touching.items():
            sets = {self.net.corridors[cid].roads for cid in corridor_ids}
            if len(sets) < 2:
                continue
            union: set[str] = set()
            for roads in sets:
                union |= roads
            if len(union) >= 2:
                found.add(vertex)
        return found

    def cluster(self, junction_vertices: set[int]) -> None:
        """Merge junction vertices linked by short corridors into interchanges.

        Short corridors are the internals of an interchange - ramps and
        connector stubs.  Merges are considered shortest-first and refused when
        they would stretch a cluster past the radius cap, so a dense urban area
        cannot snowball into one enormous pseudo-junction.
        """
        coords = self.net.coords

        # Flood out from each junction vertex along short corridors, through
        # ordinary vertices as well as junction ones.
        #
        # Only unioning corridors whose *both* ends are junction vertices was
        # too strict: a ramp joins two plain points on two carriageways, so the
        # two halves of an interchange were never merged.  The result was that
        # at junction after junction one carriageway held all the arriving legs
        # and the other all the departing ones - Hanover could be driven to and
        # never from, and E30 fell into four disconnected pieces.
        short: dict[int, list[int]] = collections.defaultdict(list)
        for corridor in self.net.corridors:
            if corridor.km <= INTERCHANGE_LINK_KM:
                short[corridor.start].append(corridor.end)
                short[corridor.end].append(corridor.start)

        groups: list[list[int]] = []
        claimed: set[int] = set()
        for seed in sorted(junction_vertices):
            if seed in claimed:
                continue
            group = [seed]
            claimed.add(seed)
            frontier = [seed]
            while frontier:
                vertex = frontier.pop()
                for neighbour in short.get(vertex, ()):
                    if neighbour in claimed or neighbour not in coords:
                        continue
                    # The radius cap stops a dense urban area snowballing into
                    # one enormous pseudo-junction.
                    if _radius_km(group + [neighbour], coords) > INTERCHANGE_MAX_RADIUS_KM:
                        continue
                    claimed.add(neighbour)
                    group.append(neighbour)
                    frontier.append(neighbour)
            groups.append(group)
        groups = self._merge_carriageway_pairs(groups)
        self._materialise(groups)

    def _merge_carriageway_pairs(self, groups: list[list[int]],
                                 within_km: float = 3.5,
                                 radius_cap_km: float = 4.5) -> list[list[int]]:
        """Fuse the two carriageways of one interchange into a single place.

        A dual carriageway meets a crossing road twice - once per direction -
        and the two junction nodes sit a few hundred metres apart.  If the ramps
        between them happen not to be tagged as part of either E-road, the
        short-corridor pass above cannot see that they are one interchange, and
        they stay separate.  The consequence is not cosmetic: a leg between them
        then has to drive to the next turnaround and back, producing exactly the
        out-and-back artefact this whole design exists to avoid.

        Two clusters are the same place when they are close together and carry
        the *same set* of E-roads.  The distance alone would be too blunt - exits
        on a busy motorway can be a kilometre apart - but two junctions that
        close together whose road sets are identical are, in practice, the two
        halves of one interchange.  A real neighbouring junction differs in its
        roads, which is what it means to be a different junction.

        The threshold has had to grow twice, each time because the data showed
        a pair that was plainly one place.  At 600 m, pairs 0.64-1.33 km apart
        stayed split.  At 1.5 km, Karlsruhe and Ettlingen - 2.74 km apart, both
        carrying exactly {E35, E52} - stayed split, and E35 had no leg between
        them at all: a route from Amsterdam to Ulm ran 62 km south to Offenburg
        and 62 km back to cross 2.74 km.  Sixty such pairs existed network-wide.

        What keeps this safe at 3.5 km is the *identical* road set, not the
        distance.  For two genuinely separate junctions to be confused, the same
        pair of E-roads would have to meet twice within a few kilometres, which
        is not two junctions - it is one interchange drawn large.
        """
        coords = self.net.coords
        signatures: dict[tuple, list[list[int]]] = collections.defaultdict(list)
        for group in groups:
            roads: set[str] = set()
            for vertex in group:
                for corridor_id in self.touching.get(vertex, ()):
                    roads |= set(self.net.corridors[corridor_id].roads)
            signatures[tuple(sorted(roads))].append(group)

        merged_groups: list[list[int]] = []
        for _, candidates in signatures.items():
            centres = [_centroid(g, coords) for g in candidates]
            taken = [False] * len(candidates)
            for i, group in enumerate(candidates):
                if taken[i]:
                    continue
                taken[i] = True
                combined = list(group)
                for j in range(i + 1, len(candidates)):
                    if taken[j] or centres[i] is None or centres[j] is None:
                        continue
                    if haversine_km(centres[i], centres[j]) > within_km:
                        continue
                    if _radius_km(combined + candidates[j], coords) > radius_cap_km:
                        continue
                    combined += candidates[j]
                    taken[j] = True
                merged_groups.append(combined)
        return merged_groups

    def _materialise(self, groups: list[list[int]]) -> None:
        coords = self.net.coords
        self.interchanges = []
        self.of_vertex = {}
        for group in groups:
            centre = _centroid(group, coords)
            if centre is None:
                continue
            interchange = Interchange(
                id=len(self.interchanges), lat=centre[0], lon=centre[1],
                vertices=set(group), radius_km=_radius_km(group, coords))
            for vertex in group:
                for corridor_id in self.touching.get(vertex, ()):
                    interchange.roads |= set(self.net.corridors[corridor_id].roads)
                self.of_vertex[vertex] = interchange.id
            self.interchanges.append(interchange)

    def add_terminals(self, min_gap_km: float = 10.0) -> None:
        """Give each road's genuine far ends an interchange, so routes can reach them.

        Without this the tip of E69 at Nordkapp, or a spur to a ferry port,
        would have no node and no city near it could ever be selected.

        The trap is that "loose end" is not the same as "end of the road".  The
        corridor graph is full of dead ends that are merely ramp stubs - a slip
        road in an E-road relation that stops where it leaves the E-road - and
        treating each as a place would bury the real termini under tens of
        thousands of meaningless nodes.  A real terminus is a dead end that is a
        long way from any junction on the same road; a ramp stub, by
        construction, is a few hundred metres from one.
        """
        per_road: dict[str, list[Corridor]] = collections.defaultdict(list)
        for corridor in self.net.corridors:
            for road in corridor.roads:
                per_road[road].append(corridor)

        added = 0
        for road, corridors in per_road.items():
            degree: collections.Counter = collections.Counter()
            for corridor in corridors:
                degree[corridor.start] += 1
                degree[corridor.end] += 1

            anchors = [self.interchanges[self.of_vertex[v]]
                       for v in degree if v in self.of_vertex]
            anchor_points = [(a.lat, a.lon) for a in anchors]

            for vertex, count in degree.items():
                if count != 1 or vertex in self.of_vertex:
                    continue
                point = self.net.coords.get(vertex)
                if point is None:
                    continue
                if anchor_points and min(haversine_km(point, a)
                                         for a in anchor_points) < min_gap_km:
                    continue
                interchange = Interchange(
                    id=len(self.interchanges), lat=point[0], lon=point[1],
                    vertices={vertex}, roads={road})
                for corridor_id in self.touching.get(vertex, ()):
                    interchange.roads |= set(self.net.corridors[corridor_id].roads)
                self.of_vertex[vertex] = interchange.id
                self.interchanges.append(interchange)
                anchor_points.append(point)
                added += 1
        return added

    def add_city_anchors(self, places: list[tuple[float, float]],
                         want_within_km: float = 6.0,
                         search_km: float = 25.0,
                         usable: set[int] | None = None) -> int:
        """Give a city a node on the road that passes it, if it has none.

        Interchanges only exist where two E-roads meet, which leaves cities on a
        long uninterrupted stretch with nowhere to join.  Ulm is the case that
        showed it: E52 runs 6.9 km away, but nothing crosses it there, so the
        nearest place a route could end was 35 km off - and the router was right
        to choose it, because by its own arithmetic stopping short and covering
        the rest off-network was fewer kilometres than driving on past.

        Promoting the nearest point on the road to an interchange makes the road
        joinable where it actually passes the city, which is what a driver does.

        The "already covered" threshold is deliberately tight.  At 12 km, Ulm
        counted as served by Langenau 11.7 km away - and a route still ended 35
        km short at Weilheim, because reaching Langenau meant 45 km more
        motorway while the extra straight-line access was only 24 km.  The
        router was right on its own arithmetic; the graph was wrong to have no
        node where the E52 passes 7 km from the city.
        """
        coords = self.net.coords
        cell = 0.25

        # An anchor has to be a place you can both reach and leave.  Picking the
        # nearest vertex regardless gave Ulm a node 7 km away with five outgoing
        # corridors and no incoming one - a diverge point, not a destination -
        # so a route to Ulm still had to stop 35 km short at Weilheim.
        arrivable: set[int] = set()
        departable: set[int] = set()
        for corridor in self.net.corridors:
            if corridor.oneway >= 0:
                departable.add(corridor.start)
                arrivable.add(corridor.end)
            if corridor.oneway <= 0:
                departable.add(corridor.end)
                arrivable.add(corridor.start)
        two_way = arrivable & departable

        buckets: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
        for corridor in self.net.corridors:
            if corridor.ramp:
                continue
            for vertex in (corridor.start, corridor.end):
                if vertex not in two_way:
                    continue
                point = coords.get(vertex)
                if point is not None:
                    buckets[(int(point[0] / cell), int(point[1] / cell))].append(vertex)

        # Only an interchange a route can actually *use* counts as covering a
        # city.  Ulm has one 6.9 km away with five outbound legs and none
        # inbound - you can leave from it but never arrive - so treating its
        # presence as coverage left the city with nowhere to end a journey.
        existing = [(i.lat, i.lon) for i in self.interchanges
                    if usable is None or i.id in usable]
        existing_buckets: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
        for index, (lat, lon) in enumerate(existing):
            existing_buckets[(int(lat / cell), int(lon / cell))].append(index)

        span = int(search_km / 111.0 / cell) + 1
        added = 0
        for lat, lon in places:
            base = (int(lat / cell), int(lon / cell))
            near_existing = min(
                (haversine_km((lat, lon), existing[i])
                 for dy in range(-span, span + 1) for dx in range(-span, span + 1)
                 for i in existing_buckets.get((base[0] + dy, base[1] + dx), ())),
                default=math.inf)
            if near_existing <= want_within_km:
                continue

            best = None
            for dy in range(-span, span + 1):
                for dx in range(-span, span + 1):
                    for vertex in buckets.get((base[0] + dy, base[1] + dx), ()):
                        if vertex in self.of_vertex:
                            continue
                        km = haversine_km((lat, lon), coords[vertex])
                        if km <= search_km and (best is None or km < best[0]):
                            best = (km, vertex)
            if best is None or best[0] >= near_existing:
                continue

            vertex = best[1]
            point = coords[vertex]
            interchange = Interchange(id=len(self.interchanges), lat=point[0],
                                      lon=point[1], vertices={vertex})
            for corridor_id in self.touching.get(vertex, ()):
                interchange.roads |= set(self.net.corridors[corridor_id].roads)
            self.of_vertex[vertex] = interchange.id
            self.interchanges.append(interchange)
            existing.append((point[0], point[1]))
            existing_buckets[(int(point[0] / cell), int(point[1] / cell))].append(
                len(existing) - 1)
            added += 1
        return added

    # -- legs ---------------------------------------------------------------

    def build_legs(self) -> None:
        """Contract the corridor graph down to interchange-to-interchange runs.

        Done per road: within the subgraph of corridors carrying road R, find
        the cheapest path from each anchor to the next anchor it can reach
        without passing through a third.  That is exactly a leg of a journey -
        "E35 from Oudenrijn to Lunetten, 8 km, via A12".
        """
        by_road: dict[str, list[Corridor]] = collections.defaultdict(list)
        for corridor in self.net.corridors:
            for road in corridor.roads:
                by_road[road].append(corridor)

        self.legs = []
        self.rejected_legs = []
        self.loop_legs = 0
        for road, corridors in sorted(by_road.items()):
            adjacency: dict[int, list[tuple[int, int, float]]] = collections.defaultdict(list)
            for corridor in corridors:
                if corridor.oneway >= 0:
                    adjacency[corridor.start].append((corridor.end, corridor.id, corridor.km))
                if corridor.oneway <= 0:
                    adjacency[corridor.end].append((corridor.start, corridor.id, corridor.km))

            # Adjacency is keyed by vertices with somewhere to go, so a vertex
            # that is only ever a destination - the far end of a one-way stretch,
            # or a road's own tip - would be missed without this second pass.
            anchors = {v for v in adjacency if v in self.of_vertex}
            for corridor in corridors:
                for vertex in (corridor.start, corridor.end):
                    if vertex in self.of_vertex:
                        anchors.add(vertex)

            # One leg per (road, from-interchange, to-interchange), keeping the
            # shortest: every vertex of a cluster is searched from, so the same
            # pair of places is reached many times over.
            best: dict[tuple[int, int], Leg] = {}
            for anchor in anchors:
                home = self.of_vertex.get(anchor)
                for target, km, path in _reach(adjacency, anchor, anchors,
                                               self.of_vertex, home):
                    leg = self._make_leg(road, anchor, target, km, path)
                    if leg.start == leg.end:
                        self.loop_legs += 1
                        continue
                    if self._is_doubling_back(leg):
                        self.rejected_legs.append(leg)
                        continue
                    key = (leg.start, leg.end)
                    if key not in best or leg.km < best[key].km:
                        best[key] = leg
            self.legs.extend(best.values())

    def _is_doubling_back(self, leg: Leg) -> bool:
        """Reject a leg that drives out and back rather than getting anywhere.

        When two interchanges are really the same place but were not merged,
        the only path between them runs to the next turnaround and returns, so
        the leg is enormously longer than the gap it closes.  Such a leg is
        never a real journey, and leaving it in lets the router "travel" 700 km
        to arrive where it started.
        """
        start = self.interchanges[leg.start]
        end = self.interchanges[leg.end]
        direct = haversine_km((start.lat, start.lon), (end.lat, end.lon))
        return leg.km > max(30.0, direct * 8.0)

    def mirror_missing_legs(self) -> int:
        """Add the return journey where only one direction was found.

        A dual carriageway is two separate sets of ways, and an interchange
        cluster sometimes catches the junction vertices of only one of them.
        The road is then drivable A to B and not B to A, which is never true of
        an E-road in the world - and the router, forced to obey it, produces
        absurdities: from Leiden to Trier it ran 70 km south past Peronne and 51
        km back north, because the only leg into Peronne on E19 arrived from the
        south.

        Mirroring uses the same distance and the same national roads, since it
        is the same stretch of tarmac travelled the other way.  A genuinely
        one-way E-road stretch would be mismodelled by this, which is a far
        smaller error than a hundred-kilometre detour that does not exist.
        """
        seen = {(leg.road, leg.start, leg.end) for leg in self.legs}
        added = 0
        for leg in list(self.legs):
            key = (leg.road, leg.end, leg.start)
            if key in seen:
                continue
            seen.add(key)
            self.legs.append(Leg(
                road=leg.road, start=leg.end, end=leg.start, km=leg.km,
                corridors=list(reversed(leg.corridors)),
                national=list(reversed(leg.national)),
                ferry=leg.ferry, countries=list(leg.countries)))
            added += 1
        return added

    def _make_leg(self, road: str, start_vertex: int, end_vertex: int,
                  km: float, path: list[int]) -> Leg:
        corridors = [self.net.corridors[cid] for cid in path]
        national: list[list] = []
        for corridor in corridors:
            for label, run_km in corridor.national:
                if national and national[-1][0] == label:
                    national[-1][1] += run_km
                else:
                    national.append([label, run_km])
        return Leg(
            road=road,
            start=self.of_vertex[start_vertex],
            end=self.of_vertex[end_vertex],
            km=round(km, 3),
            corridors=path,
            national=[(label, round(value, 2)) for label, value in national],
            ferry=any(c.ferry for c in corridors),
        )


def _centroid(vertices: list[int],
              coords: dict[int, tuple[float, float]]) -> tuple[float, float] | None:
    points = [coords[v] for v in vertices if v in coords]
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _radius_km(vertices: list[int], coords: dict[int, tuple[float, float]]) -> float:
    points = [coords[v] for v in vertices if v in coords]
    if len(points) < 2:
        return 0.0
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return max(haversine_km((lat, lon), p) for p in points)


def _reach(adjacency: dict[int, list[tuple[int, int, float]]], source: int,
           anchors: set[int], of_vertex: dict[int, int], home: int | None,
           limit_km: float = 1500.0) -> list[tuple[int, float, list[int]]]:
    """Cheapest path from ``source`` to each *other interchange* it can reach.

    Stopping the search at anchors is what makes the result a leg rather than a
    whole journey: expanding past one would skip a place the traveller could
    have changed roads.

    The subtlety, and the source of a bad bug, is that an interchange is a
    *cluster* of vertices, not one vertex.  Stopping at every anchor vertex
    meant the search halted on the far side of the very interchange it started
    from - so nearly every leg was a loop back to where it began, and real legs
    onwards were never found at all.  The graph came out almost entirely
    one-directional: most interchanges could be arrived at and never left.
    Vertices of the *home* cluster must therefore be passed straight through.

    The distance cap is a guard against pathological geometry, not a modelling
    choice, so it sits well beyond the longest gap between junctions anywhere on
    the network - a leg quietly dropped for being long would look exactly like a
    road that does not connect.
    """
    best: dict[int, float] = {source: 0.0}
    parent: dict[int, tuple[int, int]] = {}   # vertex -> (previous, corridor)
    found: list[tuple[int, float, list[int]]] = []
    queue: list[tuple[float, int]] = [(0.0, source)]

    def path_to(vertex: int) -> list[int]:
        corridors = []
        while vertex in parent:
            vertex, corridor_id = parent[vertex]
            corridors.append(corridor_id)
        corridors.reverse()
        return corridors

    while queue:
        cost, vertex = heapq.heappop(queue)
        if cost > best.get(vertex, math.inf) + 1e-9:
            continue
        if (vertex != source and vertex in anchors
                and of_vertex.get(vertex) != home):
            found.append((vertex, cost, path_to(vertex)))
            continue  # do not expand past a *different* interchange
        if cost > limit_km:
            continue
        for neighbour, corridor_id, km in adjacency.get(vertex, ()):
            candidate = cost + km
            if candidate < best.get(neighbour, math.inf) - 1e-9:
                best[neighbour] = candidate
                parent[neighbour] = (vertex, corridor_id)
                heapq.heappush(queue, (candidate, neighbour))
    return found

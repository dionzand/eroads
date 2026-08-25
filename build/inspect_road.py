"""Inspect one E-road end to end: is it whole, and does it go where it should?

    python inspect_road.py E35
    python inspect_road.py E35 D:\\GraphHopper\\data\\germany-latest.osm.pbf

The question this answers is the one that matters for every road: "does this
run as a single connected path from the place the treaty says it starts to the
place it says it ends, and is it the right length?"  A route planner can look
entirely healthy while a road is quietly in three pieces, because the router
will simply never offer a journey that crosses the break.

Three independent checks, because each catches something the others miss:

*   **Connectivity** - how much of the road is in its largest connected piece.
    Anything below 100% means a driver could not actually make the journey.
*   **Termini** - where the geometry's loose ends are, against the first and
    last control city in AGR Annex I.
*   **Length** - centreline kilometres against the AGR chain, remembering that
    a dual carriageway is stored twice and would otherwise read as double.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import agr
import graph
import junctions
import pbf
from junctions import haversine_km


def components(network) -> list[tuple[float, list[int]]]:
    """Connected pieces of the corridor graph, ignoring one-way restrictions."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for corridor in network.corridors:
        union(corridor.start, corridor.end)

    km: collections.Counter = collections.Counter()
    members: dict[int, list[int]] = collections.defaultdict(list)
    for corridor in network.corridors:
        root = find(corridor.start)
        km[root] += corridor.km
        members[root].append(corridor.id)
    return sorted(((total, members[root]) for root, total in km.items()),
                  reverse=True, key=lambda item: item[0])


def loose_ends(network) -> list[int]:
    degree: collections.Counter = collections.Counter()
    for corridor in network.corridors:
        degree[corridor.start] += 1
        degree[corridor.end] += 1
    return [vertex for vertex, count in degree.items() if count == 1]


def describe(road_id: str, source: Path, cities_index=None, countries=None) -> None:
    roster = agr.load_roster()
    entry = roster.get(road_id)
    if entry is None:
        print("%s is not in the AGR roster" % road_id)
        return

    ways, coords, stats = graph.load_from_pbf(set(roster), source, only={road_id})
    if not ways:
        print("%s: no geometry at all" % road_id)
        return

    network = graph.Network(ways, coords)
    network.find_vertices()
    network.build_corridors()

    total_km = sum(c.km for c in network.corridors)
    ramp_km = sum(c.km for c in network.corridors if c.ramp)
    pieces = components(network)
    largest = pieces[0][0] if pieces else 0.0

    print("=" * 68)
    print("%s  (%s, %s)" % (entry["display"], entry["cls"], entry["orientation"]))
    print("=" * 68)
    print("AGR chain: %s" % " - ".join(entry["points"]))
    print()
    print("ways                %d  (%d ramps)"
          % (len(ways), sum(1 for w in ways.values() if w.ramp)))
    print("corridors           %d" % len(network.corridors))
    print("carriageway km      %.0f   (of which ramps %.0f)" % (total_km, ramp_km))
    print("centreline km       ~%.0f   <- both carriageways counted once"
          % ((total_km - ramp_km) / 2))
    print()
    print("connected pieces    %d" % len(pieces))
    print("  largest holds     %.0f km  (%.1f%% of the road)"
          % (largest, 100 * largest / max(total_km, 1)))
    for size, _ in pieces[1:6]:
        print("  detached piece    %.0f km" % size)
    print()

    ends = loose_ends(network)
    print("loose ends          %d" % len(ends))
    for vertex in sorted(ends, key=lambda v: -coords[v][0])[:8]:
        lat, lon = coords[vertex]
        where = ""
        if countries is not None:
            where = " %s" % (countries.resolve(lat, lon) or "?")
        near = ""
        if cities_index is not None:
            best = _nearest_city(cities_index, lat, lon)
            if best:
                near = "  near %s (%s), %.0f km" % (best[0].name, best[0].country, best[1])
        print("   %9.4f, %9.4f%s%s" % (lat, lon, where, near))
    print()

    if cities_index is not None:
        import coverage as coverage_module
        chain = coverage_module.resolve_chain(entry["points"], cities_index)
        index = coverage_module.build_index(network)
        print("control cities, in AGR order:")
        for point in chain:
            if point.lat is None:
                print("   %-28s  no settlement matched that name" % point.name)
                continue
            distance = index.distance_km(road_id, point.lat, point.lon)
            mark = "ok " if distance is not None and distance <= 30 else "MISS"
            shown = "%.1f km away" % distance if distance is not None else "not near the road"
            print("   %-28s  %s  %-8s %s"
                  % (point.name, mark, point.country or "?", shown))


def _nearest_city(index, lat, lon, limit_km: float = 60.0):
    best = None
    for city in index.cities.values():
        if abs(city.lat - lat) > 1.0 or abs(city.lon - lon) > 1.6:
            continue
        km = haversine_km((lat, lon), (city.lat, city.lon))
        if km <= limit_km and (best is None or km < best[1]):
            best = (city, km)
    return best


if __name__ == "__main__":
    road = sys.argv[1] if len(sys.argv) > 1 else "E35"
    source = Path(sys.argv[2]) if len(sys.argv) > 2 else pbf.DEFAULT_PBF
    pbf.use_source(source)

    index = None
    countries = None
    try:
        import cities as cities_module
        import geo
        countries = geo.Countries()
        places = pbf._load("places")
        if places:
            index = cities_module.CityIndex(
                cities_module.load_from_pbf(places, countries))
    except Exception as error:            # the geometry checks still work
        print("(city data unavailable: %s)\n" % error)

    describe(road, source, index, countries)

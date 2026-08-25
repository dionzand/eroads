"""Write the compact JSON the webapp loads.

Everything the browser needs ships in two files, because the app is static and
has no server to ask for more: ``network.json`` (roads, interchanges, legs and
their geometry) and ``cities.json`` (the pickable places and how they reach the
network).

Two things keep the payload small enough to be worth doing this way:

*   **Geometry is simplified once, on the way out.**  Exact lengths are already
    computed on the full-resolution geometry, so simplifying for display can
    never change a reported distance - the numbers and the picture are allowed
    to disagree in detail without the numbers being wrong.
*   **Coordinates are delta-encoded integers.**  Successive points on a road are
    close together, so their differences are small numbers that JSON stores in a
    few characters each, where full-precision pairs cost twenty.
"""

from __future__ import annotations

import collections
import json
from datetime import date
from pathlib import Path

from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data"

# About 330 m.  The map caps at zoom 14, where one pixel covers roughly 370 m at
# European latitudes, so this is still finer than the screen can show.  Going
# below it just multiplies points that nobody can see: at 55 m the payload was
# 28 MB.  Lengths are computed on the full-resolution geometry before this runs,
# so simplifying for display can never change a reported distance.
SIMPLIFY_DEGREES = 0.003

# The whole-network layer is simplified harder still.  It is drawn as a quarter
# of a million points across 228 paths, and because the strokes do not scale
# with the map the browser re-strokes all of them on every pan - which is what
# made the full network feel laggy.  Route lines keep the finer tolerance:
# there are only ever a handful of them on screen.
NET_SIMPLIFY_DEGREES = 0.008
PRECISION = 10_000   # four decimal places, about 11 m


def _points(coords, nodes) -> list:
    """Coordinates for a run of nodes, skipping any that are unknown.

    ``CoordStore`` can resolve a whole run in one vectorised lookup; the plain
    dict used by the legacy Overpass path cannot, so it keeps the slow spelling.
    """
    vectorised = getattr(coords, "points", None)
    if vectorised is not None:
        return vectorised(nodes)
    return [coords[n] for n in nodes if n in coords]


def encode_line(points: list[tuple[float, float]]) -> list[int]:
    """Delta-encode a polyline as integers: [lat0, lon0, dlat, dlon, ...]."""
    if not points:
        return []
    out: list[int] = []
    previous_lat = previous_lon = 0
    for lat, lon in points:
        y, x = round(lat * PRECISION), round(lon * PRECISION)
        out.append(y - previous_lat)
        out.append(x - previous_lon)
        previous_lat, previous_lon = y, x
    return out


def simplify(points: list[tuple[float, float]],
             tolerance: float = SIMPLIFY_DEGREES) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    line = LineString([(lon, lat) for lat, lon in points]).simplify(tolerance)
    return [(lat, lon) for lon, lat in line.coords]


def leg_geometry(leg, network, interchanges=None) -> list[tuple[float, float]]:
    """Stitch a leg's corridors into one polyline, running start to end.

    Each corridor is stored in its own direction, which may be the reverse of
    the way this leg travels, so every corridor is flipped to continue the line
    rather than jump back to its far end.  The *first* corridor has nothing
    before it to continue, so it is oriented against the leg's starting
    interchange - otherwise a leg can come out drawn back to front, and the
    route then appears to double back at every change of road.
    """
    points: list[tuple[float, float]] = []
    anchor = None
    if interchanges is not None and leg.start < len(interchanges):
        start = interchanges[leg.start]
        anchor = (start.lat, start.lon)

    for corridor_id in leg.corridors:
        corridor = network.corridors[corridor_id]
        run = _points(network.coords, corridor.nodes)
        if not run:
            continue
        reference = points[-1] if points else anchor
        if reference is not None and \
                _distance2(reference, run[-1]) < _distance2(reference, run[0]):
            run.reverse()
        if points and run and points[-1] == run[0]:
            run = run[1:]
        points.extend(run)
    return points


def _distance2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def road_chains(network) -> dict[str, list[list[tuple[float, float]]]]:
    """Merge each road's corridors into the longest continuous runs possible.

    The corridor graph is chopped at every point where anything joins it, and
    with 174 000 ramps in play that means the average corridor is 2.4 points
    long and 84% of them are bare two-point stubs.  Shipping them individually
    costs far more in per-object overhead than in coordinates, and simplifying
    them individually can remove nothing, because a two-point line is already
    minimal.

    Joined into continuous chains first, the same road becomes a few long
    polylines that simplification can actually work on.
    """
    by_road: dict[str, list] = collections.defaultdict(list)
    for corridor in network.corridors:
        if corridor.ramp:
            continue
        for road in corridor.roads:
            by_road[road].append(corridor)

    chains: dict[str, list[list[tuple[float, float]]]] = {}
    for road, corridors in by_road.items():
        # Index corridor ends so runs can be walked from either direction.
        ends: dict[int, list[int]] = collections.defaultdict(list)
        for index, corridor in enumerate(corridors):
            ends[corridor.start].append(index)
            ends[corridor.end].append(index)

        used = [False] * len(corridors)
        out: list[list[tuple[float, float]]] = []
        for index in range(len(corridors)):
            if used[index]:
                continue
            used[index] = True
            nodes = list(corridors[index].nodes)

            # Grow the chain forwards by appending, which is cheap.
            while True:
                tip = nodes[-1]
                nxt = next((j for j in ends.get(tip, ()) if not used[j]), None)
                if nxt is None:
                    break
                used[nxt] = True
                piece = corridors[nxt].nodes
                # orient so the piece starts where the chain ends
                body = piece if piece[0] == tip else piece[::-1]
                nodes.extend(body[1:])

            # Growing backwards used to prepend - `body[:-1] + nodes` - which
            # copies the whole accumulated chain every time.  On a road the
            # length of E30 that is quadratic and costs minutes, so the backward
            # pieces are collected separately and joined once at the end.
            head: list[list[int]] = []
            while True:
                tip = head[-1][0] if head else nodes[0]
                nxt = next((j for j in ends.get(tip, ()) if not used[j]), None)
                if nxt is None:
                    break
                used[nxt] = True
                piece = corridors[nxt].nodes
                # orient so the piece ends where the chain starts
                body = piece if piece[-1] == tip else piece[::-1]
                head.append(body[:-1])

            if head:
                joined: list[int] = []
                for body in reversed(head):
                    joined.extend(body)
                joined.extend(nodes)
                nodes = joined

            points = _points(network.coords, nodes)
            if len(points) >= 2:
                out.append(points)
        chains[road] = out
    return chains


def corridor_geometry(network) -> tuple[list, dict[int, int]]:
    """Encode every corridor once, and say which encoded line each one uses.

    The map used to draw *leg* geometry - the paths the router found between
    interchanges - and that turned out to show only about an eighth of the
    network: a leg is a shortest path, so parallel routes, spurs, and any
    stretch with no interchange on it were simply never drawn.  Corridors are
    the network, so they are what gets drawn.

    Encoding them once and referring to them by index also removes the
    duplication of shipping a leg's geometry separately from the network's.
    """
    encoded: list = []
    of_corridor: dict[int, int] = {}
    for corridor in network.corridors:
        # Ramps get no geometry.  They are not drawn on the network layer, and
        # the couple of hundred metres a route spends on one is far below a
        # pixel at any zoom the map allows - but there are 174 000 of them, so
        # carrying their shape costs more than everything else put together.
        if corridor.ramp:
            continue
        points = _points(network.coords, corridor.nodes)
        if len(points) < 2:
            continue
        simplified = simplify(points)
        if len(simplified) < 2:
            continue
        of_corridor[corridor.id] = len(encoded)
        encoded.append(encode_line(simplified))
    return encoded, of_corridor


def drawable_legs(legs: list) -> list[bool]:
    """Mark one leg of each carriageway pair, for counting a road's length once.

    Both directions of a dual carriageway exist as separate legs and must, for
    routing; counting both would report every motorway at twice its length.
    """
    seen: set[tuple[str, int, int]] = set()
    flags = []
    for leg in legs:
        key = (leg.road, min(leg.start, leg.end), max(leg.start, leg.end))
        flags.append(key not in seen)
        seen.add(key)
    return flags


def build(roster: dict, network, roads_network, cities, access: dict,
          coverage: dict | None = None) -> dict:
    interchanges = roads_network.interchanges
    legs = roads_network.legs
    flags = drawable_legs(legs)

    # A road's length comes from its geometry, never from summing legs.
    #
    # Legs overlap: a dual carriageway meets its neighbours as two separate
    # interchanges, so the same stretch of road appears in several legs between
    # different pairs of ids, and no deduplication by pair can see it.  Summing
    # them reported E35 - about 1 650 km of road - as 10 527.
    #
    # Corridors are the road itself.  A one-way corridor is one carriageway of a
    # pair and counts half; a two-way corridor is the whole road and counts once.
    # Ramps are interchange plumbing and count for nothing.
    road_km: collections.Counter = collections.Counter()
    for corridor in network.corridors:
        if corridor.ramp:
            continue
        share = corridor.km if corridor.oneway == 0 else corridor.km / 2.0
        for road in corridor.roads:
            road_km[road] += share

    road_countries: dict[str, set[str]] = collections.defaultdict(set)
    for index, leg in enumerate(legs):
        for node in (leg.start, leg.end):
            country = interchanges[node].country
            if country:
                road_countries[leg.road].add(country)

    roads_out = {}
    for road_id, road in sorted(roster.items()):
        if road.get("deleted") or not road_km.get(road_id):
            continue
        entry = {
            "d": road["display"],
            "cls": road["cls"],
            "o": road["orientation"],
            "agr": bool(road.get("agr", True)),
            "km": round(road_km[road_id], 1),
            "countries": sorted(road_countries.get(road_id, ())),
            "chain": road.get("points", []),
        }
        if coverage and road_id in coverage:
            entry["unserved"] = [p.name for p in coverage[road_id].unserved]
        roads_out[road_id] = entry

    jx_out = []
    for interchange in interchanges:
        jx_out.append({
            "lat": round(interchange.lat, 5),
            "lon": round(interchange.lon, 5),
            "n": interchange.label,
            "c": interchange.country,
            "km": interchange.near_city_km,
            "r": sorted(interchange.roads),
        })

    # Two geometry sets, each simplified after merging rather than before:
    #  - "net" is the drawable network, one entry per continuous run of a road
    #  - each leg carries its own line, for drawing a planned route
    # They overlap, but both are small once merged, and the alternative - one
    # line per corridor, referenced by index - cost 19 MB in per-object
    # overhead for lines averaging 2.4 points.
    net_out = []
    for road, chains in sorted(road_chains(network).items()):
        for points in chains:
            simplified = simplify(points, NET_SIMPLIFY_DEGREES)
            if len(simplified) >= 2:
                net_out.append([road, encode_line(simplified)])

    # Leg geometry is shared, not copied.  Two things make the same line come up
    # over and over: the opposite carriageway of a dual road is a separate leg
    # over the same ground, and where two E-roads run concurrently each has its
    # own leg along the shared tarmac.  Storing one copy and an index took this
    # from 12.8 MB to a fraction of it.
    lines: list = []
    line_index: dict[tuple, int] = {}
    legs_out = []
    for leg in legs:
        entry = {
            "r": leg.road, "a": leg.start, "b": leg.end,
            "km": round(leg.km, 2),
            "nat": [[label, round(km, 1)] for label, km in leg.national],
        }
        if leg.ferry:
            entry["f"] = 1
        points = simplify(leg_geometry(leg, network, interchanges))
        if len(points) >= 2:
            encoded = encode_line(points)
            key = tuple(encoded)
            # The reverse carriageway yields the same shape walked backwards.
            reverse = tuple(encode_line(points[::-1]))
            if key in line_index:
                entry["g"] = line_index[key]
            elif reverse in line_index:
                entry["g"] = line_index[reverse]
            else:
                line_index[key] = len(lines)
                lines.append(encoded)
                entry["g"] = line_index[key]
        legs_out.append(entry)

    return {
        "generated": date.today().isoformat(),
        "roads": roads_out,
        "jx": jx_out,
        "legs": legs_out,
        "lines": lines,
        "net": net_out,
    }


def build_cities(cities, access: dict, selectable: set[str]) -> list[dict]:
    out = []
    for city in cities.values():
        if city.id not in selectable:
            continue
        entry = {
            "id": city.id,
            "n": city.name,
            "en": city.name_en,
            "c": city.country,
            "lat": round(city.lat, 4),
            "lon": round(city.lon, 4),
            "a": access.get(city.id, []),
        }
        if city.population:
            entry["p"] = city.population
        if city.agr:
            entry["agr"] = 1
            entry["roads"] = sorted(city.agr_roads)
        out.append(entry)
    out.sort(key=lambda c: (-(c.get("p") or 0), c["n"]))
    return out


def write(network_payload: dict, cities_payload: list[dict]) -> dict[str, float]:
    DATA.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name, payload in (("network.json", network_payload),
                          ("cities.json", cities_payload)):
        path = DATA / name
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        sizes[name] = path.stat().st_size / 1e6
    return sizes

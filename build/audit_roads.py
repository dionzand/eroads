"""Walk every E-road and ask: is it one path, and if not, why not?

A break in a road is not automatically a defect.  Some are real: the AGR itself
writes sea links as ``...`` rather than ``-``, and where no ferry runs the road
genuinely cannot be driven through.  E90 is the clear case - between Spain and
Italy the treaty's line crosses open water that nothing sails, so a gap there is
the truth about the road, not a hole in the data.

So each break is classified rather than counted:

``sea``          the AGR writes a sea link (``...``) between the control cities
                 either side of the gap - the road really does stop here
``ferry``        the AGR writes a sea link and OSM has a ferry across it, so the
                 crossing exists and the road continues
``defect``       the AGR writes a road link (``-``) and the road stops anyway -
                 this one is ours to fix

The verdict comes from the treaty, never from how wide the gap is.  An earlier
version binned anything beyond a 30 km search radius as "far", which put a 27 km
break in the Czech Republic in the same bucket as the 1,550 km of Mediterranean
between Spain and Italy.  Distance describes a gap; it does not explain it.

Run:  python audit_roads.py  [road ...]
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import agr
import geo
import graph
import junctions
import pbf
from junctions import haversine_km

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "road_audit.md"

_COORDS: dict[int, tuple[float, float]] = {}


def way_point(node: int):
    return _COORDS.get(node)

# A gap wider than this between two parts of a road is worth explaining; below
# it the pieces are effectively touching and the stitcher will have joined them.
NOTABLE_GAP_M = 50.0

# Below this a gap is too short to be a sea crossing, whatever the samples say:
# a bridge, a tunnel or a mapping seam, but not water anyone sails.  Without it
# a 1.5 km break on E30 came back as a ferry.
MIN_SEA_GAP_M = 2000.0


def components_of(ways: dict, coords: dict) -> list[list[int]]:
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
        if node in coords:
            groups[find(node)].append(node)
    return sorted(groups.values(), key=len, reverse=True)


def closest_between(a: list[int], b: list[int], coords: dict):
    cell = 0.05
    grid: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    for node in b:
        point = coords[node]
        grid[(int(point[0] / cell), int(point[1] / cell))].append(node)

    best = None
    span = 6      # ~30 km of cells, enough to spot a bridgeable gap
    for node in a:
        point = coords[node]
        base = (int(point[0] / cell), int(point[1] / cell))
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                for other in grid.get((base[0] + dy, base[1] + dx), ()):
                    metres = haversine_km(point, coords[other]) * 1000.0
                    if best is None or metres < best[0]:
                        best = (metres, node, other)
    return best


def crosses_water(a_point, b_point, countries, samples: int = 9) -> bool:
    """Is the straight line between two gap ends mostly over water?

    A cheap, direct test of the thing that actually matters, and it does not
    depend on matching a place name.  Relying on the treaty's control cities
    alone was fragile: E87's Dardanelles crossing is written
    ``Eceabat ... Canakkale`` in Annex I while OSM ferries it Gelibolu-Lapseki,
    so neither end matched and a real sea crossing was filed as a defect.
    """
    wet = 0
    for step in range(1, samples + 1):
        t = step / (samples + 1)
        lat = a_point[0] + (b_point[0] - a_point[0]) * t
        lon = a_point[1] + (b_point[1] - a_point[1]) * t
        if countries.lookup(lat, lon) is None:
            wet += 1
    return wet >= samples // 2


def ferry_bridges(ways: dict, road: str, a_point, b_point,
                  within_km: float = 60.0) -> bool:
    """Does a ferry carrying this road actually span *this* break?

    Both ends matter.  Testing only whether a ferry lay near one side declared
    E90's 1 550 km of Mediterranean a ferry crossing, because the road happens
    to have a ferry at its far end in Turkey - which bridges nothing here.
    """
    for way in ways.values():
        if not way.ferry or road not in way.roads:
            continue
        points = [way_point(n) for n in way.nodes]
        points = [p for p in points if p is not None]
        if not points:
            continue
        near_a = min(haversine_km(a_point, p) for p in points)
        near_b = min(haversine_km(b_point, p) for p in points)
        if near_a <= within_km and near_b <= within_km:
            return True
    return False


def sea_link_between(road: dict, chain: list, a_point, b_point) -> bool:
    """Does the AGR write a sea link near where this road breaks?

    The control points either side of the gap are found by proximity, and the
    treaty's own separators between them decide the answer.
    """
    located = [(i, p) for i, p in enumerate(chain) if p.lat is not None]
    if len(located) < 2:
        return False

    def nearest_index(point):
        return min(located,
                   key=lambda item: haversine_km(point, (item[1].lat, item[1].lon)))[0]

    i, j = sorted((nearest_index(a_point), nearest_index(b_point)))
    links = road.get("links", [])
    # links[k] joins points[k] and points[k+1]
    return any(links[k] == "sea" for k in range(i, min(j, len(links))))


def audit(source: Path, only: list[str] | None = None) -> list[dict]:
    roster = agr.load_roster()
    countries = geo.Countries()
    import cities as cities_module
    places = pbf._load("places") or []
    index = cities_module.CityIndex(cities_module.load_from_pbf(places, countries))

    import coverage as coverage_module
    results = []
    roads = [r for r in sorted(roster) if not only or r in only]

    # Load the whole network once.  Re-reading the 25 MB scan per road turned a
    # five-minute audit into a three-hour one.
    print("loading network...", file=sys.stderr, flush=True)
    all_ways, coords, stats = graph.load_from_pbf(set(roster), source)
    _COORDS.update(coords)

    # Audit what the build produces, not the raw scan: the same stray-fragment
    # pruning has to run, or the report describes a network nobody uses.
    expected = {road: [(p.lat, p.lon)
                       for p in coverage_module.resolve_chain(
                           roster[road].get("points", []), index)
                       if p.lat is not None]
                for road in roster}
    expected = {k: v for k, v in expected.items() if v}
    graph.stitch_endpoints(all_ways, coords)
    graph.prune_orphan_fragments(all_ways, coords,
                                 stats.pop("_member_of", {}), expected)
    by_road: dict[str, list] = collections.defaultdict(list)
    for way in all_ways.values():
        for road in way.roads:
            by_road[road].append(way)
    print("  %d ways over %d roads" % (len(all_ways), len(by_road)),
          file=sys.stderr, flush=True)

    for road_id in roads:
        road = roster[road_id]
        if road.get("deleted"):
            continue
        # Work on copies: the stitchers rewrite node lists, and a way shared
        # with a concurrent road must not be altered for everyone else.
        ways = {w.id: graph.Way(w.id, w.roads, list(w.nodes), w.national, w.name,
                                w.highway, w.ferry, w.oneway, set(w.sources), w.ramp)
                for w in by_road.get(road_id, ())}
        if not ways:
            continue
        graph.stitch_road_components(ways, coords)

        pieces = components_of(ways, coords)
        chain = coverage_module.resolve_chain(road.get("points", []), index)

        entry = {
            "road": road_id,
            "display": road["display"],
            "pieces": len(pieces),
            "sizes": [len(p) for p in pieces[:6]],
            "gaps": [],
        }

        for piece in pieces[1:]:
            found = closest_between(piece, pieces[0], coords)
            if found is None:
                # Nothing within the search radius; widen it on samples so the
                # gap can still be measured and classified.  A break of hundreds
                # of kilometres is usually the treaty's own sea link.
                sample = pieces[0][::max(1, len(pieces[0]) // 400)]
                mine = piece[::max(1, len(piece) // 200)]
                far = min(((haversine_km(coords[a], coords[b]) * 1000.0, a, b)
                           for a in mine for b in sample),
                          key=lambda item: item[0], default=None)
                mid = coords[piece[0]]
                if far is None:
                    gap = {"metres": None, "kind": "unknown"}
                else:
                    metres, node, other = far
                    mid = coords[node]
                    sea = metres >= MIN_SEA_GAP_M and (
                        sea_link_between(road, chain, coords[node], coords[other])
                        or crosses_water(coords[node], coords[other], countries))
                    if sea and ferry_bridges(ways, road_id, coords[node], coords[other]):
                        kind = "ferry"
                    else:
                        kind = "sea" if sea else "defect"
                    gap = {"metres": round(metres, 1),
                           "kind": kind,
                           "from_country": countries.resolve(*coords[node]),
                           "to_country": countries.resolve(*coords[other])}
            else:
                metres, node, other = found
                mid = coords[node]
                a_country = countries.resolve(*coords[node])
                b_country = countries.resolve(*coords[other])
                sea = metres >= MIN_SEA_GAP_M and (
                    sea_link_between(road, chain, coords[node], coords[other])
                    or crosses_water(coords[node], coords[other], countries))
                if sea and ferry_bridges(ways, road_id, coords[node], coords[other]):
                    kind = "ferry"
                elif sea:
                    kind = "sea"
                elif metres < NOTABLE_GAP_M:
                    kind = "touching"
                else:
                    kind = "defect"
                gap = {"metres": round(metres, 1), "kind": kind,
                       "from_country": a_country, "to_country": b_country}
            gap["lat"] = round(mid[0], 4)
            gap["lon"] = round(mid[1], 4)
            gap["nodes"] = len(piece)
            entry["gaps"].append(gap)

        results.append(entry)
        whole = "one path" if len(pieces) == 1 else "%d pieces" % len(pieces)
        print("%-6s %-10s %s" % (road_id, whole,
                                 ", ".join("%s %sm" % (g["kind"], g["metres"])
                                           for g in entry["gaps"][:4])),
              file=sys.stderr, flush=True)
    return results


def write_report(results: list[dict]) -> None:
    whole = [r for r in results if r["pieces"] == 1]
    broken = [r for r in results if r["pieces"] > 1]
    kinds: collections.Counter = collections.Counter(
        g["kind"] for r in results for g in r["gaps"])

    lines = ["# E-road continuity audit", ""]
    lines.append("Every road, walked end to end.  A break is only a defect when "
                 "the treaty says the road runs through and the data says it "
                 "does not; sea links the AGR writes as `...` are real gaps.")
    lines.append("")
    lines.append("- Roads audited: **%d**" % len(results))
    lines.append("- Single connected path: **%d** (%.0f%%)"
                 % (len(whole), 100 * len(whole) / max(len(results), 1)))
    lines.append("- In more than one piece: **%d**" % len(broken))
    lines.append("")
    lines.append("| break | meaning | count |")
    lines.append("|---|---|---|")
    for kind, label in (("sea", "AGR writes a sea link - a real gap in the road"),
                        ("ferry", "AGR sea link with a ferry across it"),
                        ("touching", "under 50 m apart"),
                        ("defect", "**AGR says road; data says gap - ours to fix**"),
                        ("unknown", "could not be measured")):
        lines.append("| %s | %s | %d |" % (kind, label, kinds.get(kind, 0)))
    lines.append("")

    defects = [(g["metres"], r["display"], g) for r in results for g in r["gaps"]
               if g["kind"] == "defect"]
    defects.sort(key=lambda item: -item[0])
    if defects:
        lines.append("## Breaks that look like defects")
        lines.append("")
        lines.append("| road | gap | where | countries |")
        lines.append("|---|---|---|---|")
        for metres, display, gap in defects[:60]:
            lines.append("| %s | %.0f m | %.3f, %.3f | %s -> %s |"
                         % (display, metres, gap["lat"], gap["lon"],
                            gap.get("from_country"), gap.get("to_country")))
        lines.append("")

    real = [(g["metres"] or 0, r["display"], g) for r in results for g in r["gaps"]
            if g["kind"] in ("sea", "ferry")]
    real.sort(key=lambda item: -item[0])
    if real:
        lines.append("## Breaks that are real")
        lines.append("")
        lines.append("The treaty writes these as sea links.  Where no ferry runs "
                     "the road genuinely cannot be driven through - E90 between "
                     "Spain and Italy is the clearest case.")
        lines.append("")
        lines.append("| road | gap | where | countries |")
        lines.append("|---|---|---|---|")
        for metres, display, gap in real[:40]:
            lines.append("| %s | %s | %.3f, %.3f | %s -> %s |"
                         % (display,
                            "%.0f km" % (metres / 1000) if metres else "beyond 30 km",
                            gap["lat"], gap["lon"],
                            gap.get("from_country"), gap.get("to_country")))
        lines.append("")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.endswith(".pbf")]
    source = next((Path(a) for a in sys.argv[1:] if a.endswith(".pbf")),
                  Path("C:/osm-staging/europe-latest.osm.pbf"))
    pbf.use_source(source)
    results = audit(source, args or None)
    write_report(results)
    (ROOT / "reports" / "road_audit.json").write_text(
        json.dumps(results, indent=1), encoding="utf-8")
    print("\nreport -> %s" % REPORT, file=sys.stderr)

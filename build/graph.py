"""Turn raw OSM ways into a routable E-road corridor graph.

The shape of the problem, and why this is done at node level:

*   An E-road is not a line.  It is a few thousand short one-way ways, two per
    carriageway, plus ramps.  Concatenating relation members end-to-end - the
    obvious approach - produces the "roundtrip" artefact where a road appears to
    run out and back, because it walks up one carriageway and down the other.
    Working from the shared OSM *node ids* instead makes that impossible.

*   Two E-roads are concurrent over long stretches.  OSM states this outright in
    the tags (``int_ref=E 25;E 35;E 60``), so a way carries a *set* of E-roads,
    and switching between roads in that set costs nothing.

*   The place where two E-roads actually meet is therefore not "where the lines
    cross" but "where the set of E-roads on the pavement changes", which is what
    junction detection looks for.  Grade-separated crossings share no OSM node
    and correctly produce no junction.

The pipeline is: load ways -> node graph -> contract to corridors -> prune
mistagged fragments -> cluster switch points into interchanges.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

import numpy as np
from pyproj import Geod

import fetch
import osm

GEOD = Geod(ellps="WGS84")

# A mistagged fragment (a British minor road reffed "E4") shows up as a tiny
# component of a road, a long way from the rest of that road.
MIN_ORPHAN_KM = 5.0
ORPHAN_DISTANCE_KM = 75.0

# How far a way may sit from the chain of control cities the AGR gives for a
# road and still be believed to be that road, when the only evidence is its own
# ref tag.  Generous on purpose: the treaty names cities, not the route between
# them, and a road can swing a long way off the straight line joining two of
# them.  It is still nowhere near enough to let Cyprus claim a French road.
TAG_CHAIN_TOLERANCE_KM = 150.0

# Highway classes where a bare "E nnn" in the tags is not to be believed.
# An E-road is a main international traffic artery; where one really does drop
# to a minor class, a route relation says so, and relation membership is trusted
# at any class.  Without this, Swedish county roads (county E = Oestergoetland,
# so "E 551", "E 576") arrive as European routes and scatter fragments of
# Czech and Romanian roads across Sweden.
TAG_UNTRUSTED_CLASSES = {
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service",
}

# How many times to push E-road labels along ramp chains.  Four covers the
# sprawling free-flow interchanges; beyond that a "ramp" is really a local road
# and labelling it would invent a connection that no driver would recognise.
RAMP_PROPAGATION_ROUNDS = 4

# Interchange clustering: corridors this short are interchange internals
# (ramps, connector stubs) rather than journeys between places.
INTERCHANGE_LINK_KM = 1.2
INTERCHANGE_MAX_RADIUS_KM = 2.5

# Letters here must include Cyrillic and Greek: Russian refs look like "М-5",
# Greek ones like "ΕΟ1", and both use letterforms that are not ASCII even when
# they look identical to it.
_NATIONAL_REF = re.compile(r"^(\w{1,3}?)\s*[-\s]?\s*(\d+\w{0,4})$", re.UNICODE)
_E_NUMBER = re.compile(r"^E[\s-]?\d{1,3}$", re.IGNORECASE)


def normalise_national_ref(value: str | None) -> list[str]:
    """National road numbers from a ``ref`` tag: "A2;A 3" -> ["A2", "A3"].

    OSM is inconsistent about the space ("A2" vs "A 3"), so normalise it away or
    a corridor description reads as two roads where there is one.  E-numbers
    that appear in ``ref`` (the Norwegian convention) are not national refs and
    are dropped here - they are picked up as E-roads elsewhere.
    """
    if not value:
        return []
    out: list[str] = []
    for part in re.split(r"[;,/]", value):
        part = part.strip()
        if not part or _E_NUMBER.match(part):
            continue
        match = _NATIONAL_REF.match(part)
        out.append(match.group(1).upper() + match.group(2) if match else part)
    return out


@dataclass
class Way:
    id: int
    roads: frozenset[str]
    nodes: list[int]
    national: list[str]
    name: str
    highway: str
    ferry: bool
    oneway: int  # +1 along node order, -1 against it, 0 both ways
    sources: set[str] = field(default_factory=set)
    # A ramp is real tarmac and must be drawn and driven, but it is interchange
    # plumbing rather than part of a road's length.
    ramp: bool = False

    @property
    def national_label(self) -> str:
        return "/".join(self.national)


def _oneway_of(tags: dict) -> int:
    value = (tags.get("oneway") or "").strip().lower()
    if value in ("yes", "true", "1"):
        return 1
    if value in ("-1", "reverse"):
        return -1
    if not value and tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def _way_from_element(element: dict, roads: frozenset[str], source: str) -> Way | None:
    tags = element.get("tags", {})
    nodes = element.get("nodes") or []
    if len(nodes) < 2:
        return None
    ferry = tags.get("route") == "ferry"
    return Way(
        id=element["id"],
        roads=roads,
        nodes=nodes,
        national=normalise_national_ref(tags.get("ref")),
        name=tags.get("name", ""),
        highway=tags.get("highway", "ferry" if ferry else ""),
        ferry=ferry,
        oneway=0 if ferry else _oneway_of(tags),
        sources={source},
    )


def load_from_pbf(roster: set[str], source=None, only: set[str] | None = None,
                  expected: dict[str, list] | None = None
                  ) -> tuple[dict[int, Way], dict[int, tuple[float, float]],
                             collections.Counter]:
    """Build the way table from a PBF scan.

    Two kinds of way go in.  E-road ways are those a route relation claims or
    that carry an E-number in ``ref``/``int_ref`` - the union is what makes
    Norway (tags only, no relations) and the rest of Europe (relations) both
    work.  **Ramps** go in too, carrying no E-road of their own: they exist
    purely so that two E-roads meeting at a grade-separated interchange are
    actually connected.  Without them E35 arrives at Duisburg and stops, because
    the piece of tarmac that carries you round the interchange belongs to
    neither route relation and has no E-number on it.
    """
    import pbf as pbf_module

    if source is not None:
        pbf_module.use_source(source)
    stats: collections.Counter = collections.Counter()

    scanned = pbf_module._load("ways")
    if scanned is None or not pbf_module.coords_path().exists():
        raise FileNotFoundError(
            "no PBF scan found; run: python build/pbf.py <europe-latest.osm.pbf>")

    import coordstore
    coords = coordstore.CoordStore.load(pbf_module.coords_path())
    ways: dict[int, Way] = {}

    membership = pbf_module._load("relations") or {}
    member_of: dict[int, set[str]] = collections.defaultdict(set)
    for road, way_ids in membership.items():
        if road in roster:
            for way_id in way_ids:
                member_of[way_id].add(road)

    for way_id, record in scanned["roads"].items():
        roads, nodes, ref, name, highway, oneway, junction, ferry = record
        roads = {r for r in roads if r in roster}
        if only:
            roads &= only
        if not roads or len(nodes) < 2:
            continue

        # Separate what a relation asserts from what the way's own tags claim.
        # A relation is an explicit statement that this way is part of that
        # road, and is trusted anywhere.  A bare ``ref`` is not, because several
        # countries number their own roads with an E prefix:
        #
        #   Sweden   county code E = Oestergoetland, so "E 551" is county road
        #            551 - tertiary lanes claiming to be a Czech European route
        #   Cyprus   numbers its district roads E101, E311, E601, E713 ... - 987
        #            ways, mostly secondary, claiming to be French and Italian
        #            routes a couple of thousand kilometres away
        #
        # A class test alone catches Sweden and misses Cyprus.  What separates
        # both from the real thing is *place*: a tag-derived claim is believed
        # only where the treaty says that road actually runs.
        from_relation = member_of.get(int(way_id), set()) & roads
        from_tags = roads - from_relation
        if from_tags and highway in TAG_UNTRUSTED_CLASSES:
            stats["tag_refs_rejected_by_class"] += len(from_tags)
            from_tags = set()
        if from_tags and expected:
            here = coords.get(nodes[0]) or coords.get(nodes[-1])
            if here is not None:
                believable = set()
                for road in from_tags:
                    chain = expected.get(road)
                    if chain is None or _near_chain(here, chain):
                        believable.add(road)
                stats["tag_refs_rejected_by_place"] += len(from_tags - believable)
                from_tags = believable
        roads = from_relation | from_tags
        if not roads:
            continue
        ways[int(way_id)] = Way(
            id=int(way_id), roads=frozenset(roads), nodes=nodes,
            national=normalise_national_ref(ref), name=name, highway=highway,
            ferry=bool(ferry),
            oneway=0 if ferry else _oneway_of({"oneway": oneway,
                                               "junction": junction}),
            sources={"pbf"})
        stats["e_road_ways"] += 1

    roads_at_node: dict[int, set[str]] = collections.defaultdict(set)
    for way in ways.values():
        for node in way.nodes:
            roads_at_node[node] |= way.roads

    ramps: dict[int, Way] = {}
    for record in scanned.get("links", ()):
        link_id, nodes, ref, name, highway, oneway, junction = record
        if len(nodes) < 2:
            stats["ramps_degenerate"] += 1
            continue
        # Every ramp the scan kept is admitted, including ones that touch no
        # E-road directly.  Requiring a direct touch here discarded the *middle*
        # of every ramp chain, and a large interchange is exactly a chain:
        # E30 -> ramp -> ramp -> E45.  Cutting the middle link severed A2 from
        # A7 at Hanover, leaving the two roads sharing not one node in Germany.
        ramps[link_id] = Way(
            id=link_id, roads=frozenset(), nodes=nodes,
            national=normalise_national_ref(ref), name=name, highway=highway,
            ferry=False,
            oneway=_oneway_of({"oneway": oneway, "junction": junction}),
            sources={"ramp"}, ramp=True)

    # Give each ramp the E-roads it joins.
    #
    # This is what makes a grade-separated interchange visible at all.  A ramp
    # carries no E-number, so if it stayed unlabelled the node where E35 meets
    # it would see only E35, the node where the ramp meets E31 would see only
    # E31, and no junction would be found anywhere - the two roads would cross
    # in the data without ever connecting.  Labelling the ramp with both makes
    # the road set *change* at the interchange, which is exactly the signal
    # junction detection looks for.
    #
    # Several rounds, because a large interchange is a chain of ramps and only
    # the first of them touches the carriageway; the roads have to travel back
    # along the chain before both ends know they are connected.
    for _ in range(RAMP_PROPAGATION_ROUNDS):
        changed = False
        for ramp in ramps.values():
            joined: set[str] = set()
            for node in ramp.nodes:
                joined |= roads_at_node.get(node, set())
            if joined and joined != set(ramp.roads):
                ramp.roads = frozenset(joined)
                changed = True
        if not changed:
            break
        for ramp in ramps.values():
            for node in ramp.nodes:
                roads_at_node[node] |= ramp.roads

    for link_id, ramp in ramps.items():
        if not ramp.roads:
            stats["ramps_without_roads"] += 1
            continue
        ways[link_id] = ramp
        stats["ramps_kept"] += 1

    stats["nodes"] = len(coords)
    missing = {n for way in ways.values() for n in way.nodes} - set(coords)
    stats["nodes_without_coordinates"] = len(missing)
    return ways, coords, stats


def _geometry_sources() -> list:
    """Every cached Overpass response that holds way geometry.

    Superseded by :func:`load_from_pbf` and kept only so an existing Overpass
    cache can still be read; a planet extract is complete where a set of queries
    is merely as complete as the questions asked of it.
    """
    return (osm.cached_paths("tileB_") + osm.cached_paths("gap_")
            + osm.cached_paths("fill_"))


def load_ways(roster: set[str], only: set[str] | None = None
              ) -> tuple[dict[int, Way], dict[int, tuple[float, float]], collections.Counter]:
    """Build the way table from relation membership plus validated way tags.

    Both sources contribute road ids to the same way: membership is
    authoritative but incomplete (Norway has no relations), tags are complete
    but need checking (British minor roads are reffed "E4").  Taking the union
    is what makes the network whole.
    """
    stats: collections.Counter = collections.Counter()

    membership = fetch.fetch_membership()
    by_way: dict[int, set[str]] = collections.defaultdict(set)
    for road, way_ids in membership.items():
        if road not in roster:
            stats["relation_road_not_in_roster"] += 1
            continue
        if only and road not in only:
            continue
        for way_id in way_ids:
            by_way[way_id].add(road)

    ways: dict[int, Way] = {}
    coords: dict[int, tuple[float, float]] = {}

    for source in _geometry_sources():
        stats["geometry_files"] += 1
        for element in osm.load_path(source)["elements"]:
            if element["type"] != "way":
                continue
            way_id = element["id"]
            tags = element.get("tags", {})
            is_ferry = tags.get("route") == "ferry"

            from_relation = by_way.get(way_id, set())
            from_tags: set[str] = set()
            if is_ferry or tags.get("highway") in fetch.ROAD_CLASSES:
                tagged = (fetch.e_refs_from_tag(tags.get("int_ref"))
                          | fetch.e_refs_from_tag(tags.get("ref")))
                from_tags = {r for r in tagged if r in roster}
                stats["tag_rejected_not_in_roster"] += len(tagged - from_tags)
                if only:
                    from_tags &= only
            elif not from_relation:
                stats["rejected_highway_class"] += 1
                continue

            roads = from_relation | from_tags
            if not roads:
                continue
            if from_tags - from_relation:
                stats["roads_added_by_tags"] += len(from_tags - from_relation)

            existing = ways.get(way_id)
            if existing is not None:
                existing.roads |= frozenset(roads)
                continue

            origin = "relation" if from_relation else "tag"
            way = _way_from_element(element, frozenset(roads), origin)
            if way is None:
                stats["skipped_degenerate"] += 1
                continue
            if from_tags:
                way.sources.add("tag")
            ways[way_id] = way
            for node_id, point in zip(element.get("nodes") or [],
                                      element.get("geometry") or []):
                if point is not None and node_id not in coords:
                    coords[node_id] = (point["lat"], point["lon"])

    wanted = {w for ids in membership.values() for w in ids}
    stats["member_ways_without_geometry"] = len(wanted - set(ways))
    stats["_member_of"] = member_of
    return ways, coords, stats


def stitch_endpoints(ways: dict[int, Way], coords: dict[int, tuple[float, float]],
                     tolerance_m: float = 12.0) -> list[tuple[int, int, float]]:
    """Join way ends that meet on the ground but share no OSM node.

    Two ways can be drawn to the same spot and still be separate objects, and
    the graph then has a break where the map shows a road.  E35's whole Dutch
    and Ruhr section - 257 km, Amsterdam through Oberhausen - hung off the rest
    of the road exactly this way, passing within a hundred metres of it without
    ever connecting.

    Only *way endpoints* are considered, and only over a very short distance.
    That matters: the two carriageways of a motorway run twenty to forty metres
    apart, and welding those together would let a route hop the central
    reservation.  An endpoint is where a road was cut; a point mid-carriageway
    is not.

    Returns the joins made, so the build report can show them rather than have
    the graph silently repaired.
    """
    endpoints: dict[int, list[int]] = collections.defaultdict(list)
    for way in ways.values():
        for node in (way.nodes[0], way.nodes[-1]):
            endpoints[node].append(way.id)

    # ~11 m of latitude per 1e-4 degree, so this cell size matches the tolerance.
    cell = 1e-4
    buckets: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    for node in endpoints:
        point = coords.get(node)
        if point is not None:
            buckets[(int(point[0] / cell), int(point[1] / cell))].append(node)

    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    joins: list[tuple[int, int, float]] = []
    for (cy, cx), group in buckets.items():
        candidates = list(group)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    candidates.extend(buckets.get((cy + dy, cx + dx), ()))
        for i, a in enumerate(group):
            for b in candidates:
                if b <= a:
                    continue
                metres = _haversine_m(coords[a], coords[b])
                if metres > tolerance_m:
                    continue
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
                    joins.append((a, b, round(metres, 2)))

    if joins:
        alias = {node: find(node) for node in parent}
        for way in ways.values():
            if any(node in alias for node in way.nodes):
                way.nodes = [alias.get(node, node) for node in way.nodes]
    return joins


def _chain_distance_km(point: tuple[float, float], chain: list) -> float:
    """Distance from a point to the polyline through a road's control cities."""
    if not chain:
        return 0.0
    if len(chain) == 1:
        return _haversine_m(point, chain[0]) / 1000.0
    return min(_segment_km(point, a, b) for a, b in zip(chain, chain[1:]))


def _near_chain(point: tuple[float, float], chain: list) -> bool:
    """Is this point near the polyline through a road's AGR control cities?

    Distance to the *chain*, not to its individual cities: between two control
    points several hundred kilometres apart, every kilometre of real road in
    between is far from both and close to the line joining them.
    """
    if not chain:
        return True
    if len(chain) == 1:
        return _haversine_m(point, chain[0]) / 1000.0 <= TAG_CHAIN_TOLERANCE_KM
    for start, end in zip(chain, chain[1:]):
        if _segment_km(point, start, end) <= TAG_CHAIN_TOLERANCE_KM:
            return True
    return False


def _segment_km(point, start, end) -> float:
    """Great-circle-ish distance from a point to a segment, in kilometres.

    Latitude and longitude are treated as a plane scaled by cos(lat), which at
    these distances is well inside the tolerance this feeds.
    """
    import math
    scale = math.cos(math.radians(point[0])) or 1e-6
    px, py = point[1] * scale, point[0]
    ax, ay = start[1] * scale, start[0]
    bx, by = end[1] * scale, end[0]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        closest = (ay, ax / scale)
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        closest = (ay + t * dy, (ax + t * dx) / scale)
    return _haversine_m(point, closest) / 1000.0


def stitch_treaty_crossings(ways: dict[int, Way], coords: dict[int, tuple[float, float]],
                            meetings: list[tuple[str, str, float, float]],
                            near_city_km: float = 25.0,
                            max_gap_m: float = 250.0) -> list[tuple]:
    """Connect two roads where the treaty says they meet and the data does not.

    Annex I names the control cities of every road, so when the same city
    appears on two roads the treaty is asserting that a driver can get from one
    to the other there.  Swiebodzin is named on both E30 and E65; in OSM the two
    pass within *thirty metres* of each other and share no node, and no ramp
    chains between them - so a route from Amsterdam to Athens that should be
    E30 then E65 came out as five changes.

    Welding anything thirty metres apart is not an option: E-roads cross on
    grade-separated flyovers all over Europe and joining those would invent
    turns that do not exist.  What makes this safe is that it only ever fires
    where the treaty has already stated the two roads meet, and only at their
    closest approach to the named city.

    Returns the joins made so the report can list them; a repair invented here
    should be visible, not silent.
    """
    # Index every road's nodes into cells once.  Scanning a road's full node
    # list per meeting is 700 million comparisons across Europe; a grid lookup
    # touches only the handful of cells around the city in question.
    cell = 0.02          # roughly 2 km, comfortably wider than the gaps sought
    grid: dict[str, dict[tuple[int, int], list]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    wanted = {road for meeting in meetings for road in meeting[:2]}
    for way in ways.values():
        for road in way.roads & wanted:
            for node in way.nodes:
                point = coords.get(node)
                if point is not None:
                    grid[road][(int(point[0] / cell), int(point[1] / cell))].append(
                        (node, point))

    span = int(near_city_km / 111.0 / cell) + 1
    joins: list[tuple] = []
    alias: dict[int, int] = {}

    for road_a, road_b, lat, lon in meetings:
        base = (int(lat / cell), int(lon / cell))
        cells = [(base[0] + dy, base[1] + dx)
                 for dy in range(-span, span + 1) for dx in range(-span, span + 1)]
        near_a = [item for key in cells for item in grid.get(road_a, {}).get(key, ())]
        near_b = [item for key in cells for item in grid.get(road_b, {}).get(key, ())]
        if not near_a or not near_b:
            continue

        # Already connected near this city?  Then there is nothing to repair.
        if {n for n, _ in near_a} & {n for n, _ in near_b}:
            continue

        fine: dict[tuple[int, int], list] = collections.defaultdict(list)
        for node, point in near_b:
            fine[(int(point[0] / 0.005), int(point[1] / 0.005))].append((node, point))

        best = None
        for node, point in near_a:
            key = (int(point[0] / 0.005), int(point[1] / 0.005))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for other, other_point in fine.get((key[0] + dy, key[1] + dx), ()):
                        metres = _haversine_m(point, other_point)
                        if best is None or metres < best[0]:
                            best = (metres, node, other)
        if best is None or best[0] > max_gap_m:
            continue
        metres, node, other = best
        alias[other] = node
        joins.append((road_a, road_b, round(lat, 4), round(lon, 4), round(metres, 1)))

    if alias:
        for way in ways.values():
            if any(node in alias for node in way.nodes):
                way.nodes = [alias.get(node, node) for node in way.nodes]
    return joins


def stitch_ferry_landings(ways: dict[int, Way], coords: dict[int, tuple[float, float]],
                          max_gap_m: float = 30000.0) -> list[tuple]:
    """Tie each end of a ferry to the road it carries.

    A ferry way ends at a berth, and the road up to that berth is a separate
    object that rarely shares its final node - so the crossing exists in the
    data while connecting nothing.  Both Channel crossings were like this:
    Portsmouth held the E5 ferry to Le Havre and Harwich the E30 ferry to Hook
    of Holland, and each sat alone in its own two-node island while 259 British
    interchanges were cut off from the continent entirely.

    The join is only ever made to the *same road* the ferry carries, so it can
    never invent a crossing the ferry does not already make.  The tolerance has
    to be generous, though, because a port and the road are not always the same
    place: OSM sails E5 from **Portsmouth** while the treaty routes it through
    **Southampton**, 26 km along the coast, and at a 3 km limit that crossing
    stayed unconnected and Britain stayed an island.
    """
    landward: dict[str, list[tuple[int, tuple[float, float]]]] = collections.defaultdict(list)
    for way in ways.values():
        if way.ferry:
            continue
        for road in way.roads:
            for node in (way.nodes[0], way.nodes[-1]):
                point = coords.get(node)
                if point is not None:
                    landward[road].append((node, point))

    cell = 0.05
    grids: dict[str, dict[tuple[int, int], list]] = {}
    for road, items in landward.items():
        grid: dict[tuple[int, int], list] = collections.defaultdict(list)
        for node, point in items:
            grid[(int(point[0] / cell), int(point[1] / cell))].append((node, point))
        grids[road] = grid

    joins: list[tuple] = []
    alias: dict[int, int] = {}
    span = int(max_gap_m / 1000.0 / 111.0 / cell) + 1

    for way in ways.values():
        if not way.ferry:
            continue
        for end in (0, -1):
            node = way.nodes[end]
            point = coords.get(node)
            if point is None:
                continue
            best = None
            for road in way.roads:
                grid = grids.get(road, {})
                base = (int(point[0] / cell), int(point[1] / cell))
                for dy in range(-span, span + 1):
                    for dx in range(-span, span + 1):
                        for other, other_point in grid.get((base[0] + dy, base[1] + dx), ()):
                            if other == node:
                                continue
                            metres = _haversine_m(point, other_point)
                            if metres <= max_gap_m and (best is None or metres < best[0]):
                                best = (metres, other, road)
            if best is None:
                continue
            metres, other, road = best
            if metres < 1.0:
                continue      # already joined
            alias[node] = other
            joins.append((road, round(point[0], 4), round(point[1], 4), round(metres, 1)))

    if alias:
        for way in ways.values():
            if any(node in alias for node in way.nodes):
                way.nodes = [alias.get(node, node) for node in way.nodes]
    return joins


def prune_orphan_fragments(ways: dict[int, Way], coords: dict[int, tuple[float, float]],
                           member_of: dict[int, set[str]],
                           expected: dict[str, list] | None = None,
                           away_km: float = 40.0) -> list[tuple]:
    """Drop road labels from stray pieces no relation vouches for.

    A road's number can appear on tarmac that is not that road.  E5 ends at
    Algeciras in the treaty, but ways along the coast road east of it - Spain's
    A-7, which is really E15 - carry an E5 number in their tags and arrive as
    fragments sitting well beyond where the road actually stops.

    The test is provenance plus *the treaty's own line*, never distance from the
    rest of the road.  Measuring from the main component looked reasonable and
    was badly wrong: E1's Irish section is 981 km from its Iberian one and E5's
    British section 195 km from its French one, because a sea link separates
    them - and an earlier version of this function duly deleted Dublin and
    Birmingham, both of which the treaty names as control cities.

    So a piece is dropped only when no relation claims it *and* it lies far from
    the chain of cities the AGR says the road runs through.  Only the label
    goes; the way stays, since it is usually a good piece of some other E-road.
    """
    dropped: list[tuple] = []
    by_road: dict[str, list[Way]] = collections.defaultdict(list)
    for way in ways.values():
        for road in way.roads:
            by_road[road].append(way)

    for road, road_ways in sorted(by_road.items()):
        parent: dict[int, int] = {}

        def find(node: int) -> int:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for way in road_ways:
            for a, b in zip(way.nodes, way.nodes[1:]):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

        groups: dict[int, list[Way]] = collections.defaultdict(list)
        for way in road_ways:
            groups[find(way.nodes[0])].append(way)
        if len(groups) < 2:
            continue

        chain = (expected or {}).get(road)
        if not chain:
            continue          # no treaty line to judge against; leave it alone

        ordered = sorted(groups.values(), key=lambda g: sum(len(w.nodes) for w in g),
                         reverse=True)
        for group in ordered[1:]:
            if any(road in member_of.get(way.id, ()) for way in group):
                continue          # a relation vouches for it; leave it alone
            points = [coords[n] for w in group for n in w.nodes[::4] if n in coords]
            if not points:
                continue
            near = min(_chain_distance_km(p, chain) for p in points[::5])
            if near < away_km:
                continue
            for way in group:
                way.roads = way.roads - {road}
            dropped.append((road, round(points[0][0], 4), round(points[0][1], 4),
                            len(group), round(near, 1)))

    for way_id in [w.id for w in ways.values() if not w.roads and not w.ramp]:
        ways.pop(way_id, None)
    return dropped


def stitch_road_components(ways: dict[int, Way], coords: dict[int, tuple[float, float]],
                           max_gap_m: float = 400.0,
                           rounds: int = 3) -> list[tuple]:
    """Join a road to itself where OSM leaves it in pieces.

    A road that is in three pieces cannot be driven end to end, and the router
    will silently route around the break instead of reporting it - E65 came
    apart near Brno into a northern and a southern half, so no journey could
    follow it from Poland to Greece and Amsterdam to Athens needed five changes
    where two would do.

    Only gaps of a few hundred metres are closed, and only between two parts of
    *the same road*, which is a much weaker claim than joining two different
    roads: the treaty already says this is one continuous route, so a short gap
    in the middle of it is a mapping artefact rather than a fact about the
    world.  Anything wider is left broken and reported, because a real gap -
    a missing bridge, a road that genuinely stops - should not be invented away.
    """
    joins: list[tuple] = []
    by_road: dict[str, list[Way]] = collections.defaultdict(list)
    for way in ways.values():
        for road in way.roads:
            by_road[road].append(way)

    alias: dict[int, int] = {}
    for road, road_ways in sorted(by_road.items()):
        for _ in range(rounds):
            parent: dict[int, int] = {}

            def find(node: int) -> int:
                parent.setdefault(node, node)
                while parent[node] != node:
                    parent[node] = parent[parent[node]]
                    node = parent[node]
                return node

            for way in road_ways:
                nodes = [alias.get(n, n) for n in way.nodes]
                for a, b in zip(nodes, nodes[1:]):
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra

            groups: dict[int, list[int]] = collections.defaultdict(list)
            for node in parent:
                if node in coords:
                    groups[find(node)].append(node)
            if len(groups) < 2:
                break

            ordered = sorted(groups.values(), key=len, reverse=True)
            main = ordered[0]
            cell = 0.005
            grid: dict[tuple[int, int], list] = collections.defaultdict(list)
            for node in main:
                point = coords[node]
                grid[(int(point[0] / cell), int(point[1] / cell))].append(node)

            joined_any = False
            for group in ordered[1:]:
                best = None
                for node in group:
                    point = coords[node]
                    base = (int(point[0] / cell), int(point[1] / cell))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            for other in grid.get((base[0] + dy, base[1] + dx), ()):
                                metres = _haversine_m(point, coords[other])
                                if best is None or metres < best[0]:
                                    best = (metres, node, other)
                if best is None or best[0] > max_gap_m:
                    continue
                metres, node, other = best
                alias[node] = other
                joins.append((road, round(coords[node][0], 4),
                              round(coords[node][1], 4), round(metres, 1)))
                joined_any = True
            if not joined_any:
                break

    if alias:
        # Resolve chains so an alias never points at another alias.
        def resolve(node: int) -> int:
            seen = set()
            while node in alias and node not in seen:
                seen.add(node)
                node = alias[node]
            return node

        final = {node: resolve(node) for node in alias}
        for way in ways.values():
            if any(node in final for node in way.nodes):
                way.nodes = [final.get(node, node) for node in way.nodes]
    return joins


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat = lat2 - lat1
    dlon = math.radians(b[1] - a[1])
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def segment_lengths_km(points: list[tuple[float, float]]) -> np.ndarray:
    """Geodesic length of each consecutive pair of points, in kilometres."""
    if len(points) < 2:
        return np.zeros(0)
    lats = np.fromiter((p[0] for p in points), dtype=float, count=len(points))
    lons = np.fromiter((p[1] for p in points), dtype=float, count=len(points))
    _, _, metres = GEOD.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])
    return np.asarray(metres, dtype=float) / 1000.0


@dataclass
class Corridor:
    """A stretch between two graph vertices over which the E-roads do not change."""
    id: int
    start: int
    end: int
    roads: frozenset[str]
    nodes: list[int]
    km: float
    national: list[tuple[str, float]]  # ordered (road number, km) runs
    ferry: bool
    oneway: int
    ramp: bool = False
    names: list[str] = field(default_factory=list)


@dataclass
class _Arc:
    """A run of one way between two split points, kept with its parent way."""
    nodes: list[int]
    way: int


class Network:
    def __init__(self, ways: dict[int, Way], coords: dict[int, tuple[float, float]]):
        self.ways = ways
        self.coords = coords
        self.vertices: set[int] = set()
        self.corridors: list[Corridor] = []

    # -- step 1: which nodes are topologically interesting -------------------

    def find_vertices(self) -> None:
        """A vertex is a node where the road forks, ends, or changes identity."""
        degree: collections.Counter = collections.Counter()
        first_roads: dict[int, frozenset[str]] = {}
        varies: set[int] = set()

        for way in self.ways.values():
            nodes = way.nodes
            for index in range(len(nodes) - 1):
                degree[nodes[index]] += 1
                degree[nodes[index + 1]] += 1
            roads = way.roads
            for node in nodes:
                previous = first_roads.get(node)
                if previous is None:
                    first_roads[node] = roads
                elif previous != roads:
                    varies.add(node)

        self.degree = degree
        self.vertices = {
            node for node, count in degree.items()
            if (count != 2 or node in varies) and node in self.coords
        }

    # -- step 2: contract everything between vertices -------------------------

    def build_corridors(self) -> None:
        """Split each way at vertices, then glue the pieces into corridors."""
        arcs: list[_Arc] = []
        for way in self.ways.values():
            current = [way.nodes[0]]
            for node in way.nodes[1:]:
                current.append(node)
                if node in self.vertices:
                    arcs.append(_Arc(current, way.id))
                    current = [node]
            if len(current) > 1:
                arcs.append(_Arc(current, way.id))

        # Arcs that meet at a non-vertex node are two halves of one corridor:
        # that node is simply where one way ends and the next begins.
        open_ends: dict[int, list[int]] = collections.defaultdict(list)
        for index, arc in enumerate(arcs):
            for end in (arc.nodes[0], arc.nodes[-1]):
                if end not in self.vertices:
                    open_ends[end].append(index)

        used = [False] * len(arcs)
        self.corridors = []

        for index in range(len(arcs)):
            if used[index]:
                continue
            used[index] = True
            nodes = list(arcs[index].nodes)
            # ways[i] carries the segment nodes[i] -> nodes[i+1], and facing[i]
            # is +1 when that segment runs along its way's own node order and -1
            # when the arc had to be reversed to fit.  Without this, a one-way
            # carriageway spliced in backwards would be modelled as drivable in
            # the wrong direction.
            ways = [arcs[index].way] * (len(nodes) - 1)
            facing = [1] * (len(nodes) - 1)

            while nodes[-1] not in self.vertices:
                nxt = _free_arc(open_ends, nodes[-1], used)
                if nxt is None:
                    break
                used[nxt] = True
                piece = arcs[nxt]
                forward = piece.nodes[0] == nodes[-1]
                body = piece.nodes if forward else piece.nodes[::-1]
                nodes.extend(body[1:])
                ways.extend([piece.way] * (len(body) - 1))
                facing.extend([1 if forward else -1] * (len(body) - 1))

            while nodes[0] not in self.vertices:
                previous = _free_arc(open_ends, nodes[0], used)
                if previous is None:
                    break
                used[previous] = True
                piece = arcs[previous]
                forward = piece.nodes[-1] == nodes[0]
                body = piece.nodes if forward else piece.nodes[::-1]
                nodes = body[:-1] + nodes
                ways = [piece.way] * (len(body) - 1) + ways
                facing = [1 if forward else -1] * (len(body) - 1) + facing

            corridor = self._make_corridor(len(self.corridors), nodes, ways, facing)
            if corridor is not None:
                self.corridors.append(corridor)

    def _make_corridor(self, corridor_id: int, nodes: list[int],
                       segment_ways: list[int],
                       facing: list[int]) -> Corridor | None:
        if any(node not in self.coords for node in nodes):
            return None
        points = [self.coords[node] for node in nodes]
        lengths = segment_lengths_km(points)
        if len(lengths) == 0:
            return None

        ways = [self.ways[w] for w in segment_ways]
        first = ways[0]
        # Each segment's direction is its way's one-way sense, corrected for
        # whether the arc was spliced in forwards or backwards.
        directions = {way.oneway * side for way, side in zip(ways, facing)}
        # Anything mixed is treated as bidirectional rather than guessed at:
        # a corridor that claims to be one-way in both directions is worse than
        # one that claims nothing.
        oneway = directions.pop() if len(directions) == 1 else 0

        names: list[str] = []
        for way in ways:
            if way.name and way.name not in names:
                names.append(way.name)

        return Corridor(
            id=corridor_id,
            start=nodes[0],
            end=nodes[-1],
            roads=first.roads,
            nodes=nodes,
            km=float(lengths.sum()),
            national=_runs(( w.national_label for w in ways), lengths),
            ferry=any(w.ferry for w in ways),
            oneway=oneway,
            ramp=all(w.ramp for w in ways),
            names=names[:4],
        )


def _runs(labels, lengths: np.ndarray) -> list[tuple[str, float]]:
    """Collapse a per-segment label sequence into ordered (label, km) runs.

    This is what turns a leg into "via A12 (34 km) then A50 (18 km)".  Untagged
    segments are dropped rather than shown as a gap, and the merge is repeated
    afterwards so that "A1, untagged, A1" reads as one run of A1 rather than
    listing the same road twice.
    """
    runs: list[list] = []
    for index, label in enumerate(labels):
        km = float(lengths[index]) if index < len(lengths) else 0.0
        if runs and runs[-1][0] == label:
            runs[-1][1] += km
        else:
            runs.append([label, km])

    merged: list[list] = []
    for label, km in runs:
        if not label:
            continue
        if merged and merged[-1][0] == label:
            merged[-1][1] += km
        else:
            merged.append([label, km])
    return [(label, round(km, 2)) for label, km in merged]


def _free_arc(open_ends: dict[int, list[int]], node: int,
              used: list[bool]) -> int | None:
    for candidate in open_ends.get(node, ()):
        if not used[candidate]:
            return candidate
    return None

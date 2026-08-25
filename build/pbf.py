"""Read the E-road network straight out of an OSM PBF extract.

This replaces Overpass as the source of geometry, and it is better in every way
that matters here:

*   **Complete.**  Overpass answers the query you asked; a planet extract holds
    everything.  The gap that broke E35 at Oberhausen - two loose ends 2.3 km
    apart with nothing joining them - is exactly what happens when you only
    fetch ways that are relation members or carry an E-number.  The connecting
    ramps exist in OSM; they were simply in neither set.
*   **Fast.**  Three filtered passes over the file beat several hundred network
    round trips against a public API that returns 504 under load.  The filters
    run in C++, so a way that is not a road is never turned into a Python
    object: 2.8M highway ways come out of the 1.4 GB Netherlands extract in
    eleven seconds.
*   **Reproducible.**  The same file gives the same answer tomorrow.

The three passes are filtered to one entity type each so osmium can skip whole
blocks:

1.  ``relations`` - E-road route relations, giving road id -> member way ids.
2.  ``ways``      - members, ways carrying an E-number, and link ways that might
                    connect two E-roads through an interchange.
3.  ``nodes``     - coordinates for exactly the nodes those ways use (by id
                    filter), plus every settlement, for the city picker.
"""

from __future__ import annotations

import collections
import gzip
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import osmium
import osmium.filter

import agr
import coordstore

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "pbf"


def use_source(pbf: Path) -> None:
    """Namespace the cache per source file.

    Country extracts are useful for testing a single road quickly without
    waiting on the continental file, and they must not overwrite each other's
    results or, worse, be mistaken for them.
    """
    global CACHE
    CACHE = ROOT / "cache" / "pbf" / pbf.stem

DEFAULT_PBF = Path(r"D:\GraphHopper\data\europe-latest.osm.pbf")

# Way classes that can carry an E-road.
ROAD_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
}

# Interchange ramps.  A slip road joining two E-roads carries neither an
# E-number nor membership of either relation, so it cannot be recognised from
# its own tags - it is kept speculatively and then filtered by whether it
# actually touches E-road geometry.
#
# Only genuine ramp classes are listed.  Adding "unclassified" and "service"
# pulled in 749 477 ways from the Netherlands alone, which across Europe would
# have meant resolving coordinates for a hundred million nodes to gain nothing:
# a driveway is not how you get from the E35 to the E31.
LINK_CLASSES = {
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
}

# How many times to grow the kept-link set.  One round catches a ramp touching
# an E-road; each further round reaches one link deeper along a ramp chain.
#
# Two was not enough.  At Swiebodzin - where the AGR puts *both* E30 and E65 -
# the two roads ended up sharing no node at all, because the ramps in the middle
# of the interchange touch only other ramps and were dropped, leaving E30's
# ramps labelled E30 and E65's labelled E65 with nothing joining them.  A route
# from Amsterdam to Athens that should be E30 then E65, one change, was coming
# out as five.
LINK_GROWTH_ROUNDS = 6

# Crossings that carry cars but are not ferries.  The Channel Tunnel is a
# `route=shuttle_train` called "Le Shuttle" and carries no E-number at all, so
# without this E15 stops at Folkestone and every journey from Britain to the
# continent detours via the Hook of Holland.
CROSSING_ROUTES = {"ferry", "shuttle_train"}

# The trunk network, kept so that gaps in E-road tagging can be bridged along
# real roads.  A road tagged E87 on some ways and not others shatters into
# fragments, and the tarmac joining them is ordinary trunk road that no E-road
# query would ever return.
BRIDGE_CLASSES = {"motorway", "trunk", "primary"}

PLACE_RANKS = {"city", "town"}

_E_REF = re.compile(r"^\s*E[\s-]?(\d{1,3})\s*$", re.IGNORECASE)


def e_refs(value: str | None) -> set[str]:
    """Canonical E-road ids in a ``ref``/``int_ref`` value, e.g. "E 25;E 35"."""
    if not value:
        return set()
    found = set()
    for part in re.split(r"[;,/]", value):
        match = _E_REF.match(part)
        if match:
            found.add(agr.canonical_id(match.group(1)))
    return found


def _log(message: str) -> None:
    print("[pbf] %s" % message, file=sys.stderr, flush=True)


def _dump(name: str, payload) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (name + ".json.gz")
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    tmp.replace(path)
    return path


def _load(name: str):
    path = CACHE / (name + ".json.gz")
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def cached(name: str) -> bool:
    return (CACHE / (name + ".json.gz")).exists()


# -- pass 1: relations -----------------------------------------------------


def scan_relations(pbf: Path, refresh: bool = False) -> dict[str, list[int]]:
    """Map canonical road id -> member way ids, from the route relations.

    Superroutes nest: a road holds per-country relations which hold the ways.
    The nesting is resolved afterwards over what was collected, rather than by
    re-reading the file.  Child relations' own refs are ignored - they are the
    "E-01 / E17 / no ref at all" mess - and the parent's ref is used throughout.
    """
    if not refresh and cached("relations"):
        return _load("relations")

    started = time.time()
    ways_of: dict[int, list[int]] = {}
    subs_of: dict[int, list[int]] = {}
    ref_of: dict[int, str] = {}
    seen = 0

    # Filter in C++ to road routes.  Europe's relations are overwhelmingly bus
    # lines, hiking trails and cycle networks; materialising all of them as
    # Python objects to read their member lists took 45 seconds on Germany alone
    # and was the single slowest thing in the build.  This cuts it to five.
    processor = (osmium.FileProcessor(str(pbf), osmium.osm.RELATION)
                 .with_filter(osmium.filter.TagFilter(
                     ("route", "road"), ("network", "e-road"),
                     ("type", "network"), ("type", "superroute"))))
    for relation in processor:
        seen += 1
        tags = relation.tags
        kind = tags.get("type")
        if kind not in ("route", "superroute", "network"):
            continue
        raw = tags.get("ref") or tags.get("int_ref")
        network = (tags.get("network") or "").lower()
        ways, subs = [], []
        for member in relation.members:
            if member.type == "w":
                ways.append(member.ref)
            elif member.type == "r":
                subs.append(member.ref)
        ways_of[relation.id] = ways
        subs_of[relation.id] = subs
        # Parse the ref through e_refs, never through canonical_id directly.
        # A relation reffed "E 264;E 25;E 411" is three concurrent roads, but
        # canonical_id only strips non-digits, so it would yield the single
        # nonsense road "E26425411" and quietly take 394 ways with it.
        if raw and (network == "e-road" or e_refs(raw)):
            found = e_refs(raw)
            if found:
                ref_of[relation.id] = sorted(found)

    membership: dict[str, set[int]] = collections.defaultdict(set)
    for relation_id, roads in ref_of.items():
        pending = [relation_id]
        seen_ids = {relation_id}
        while pending:
            current = pending.pop()
            for road in roads:
                membership[road].update(ways_of.get(current, ()))
            for child in subs_of.get(current, ()):
                if child not in seen_ids:
                    seen_ids.add(child)
                    pending.append(child)

    result = {road: sorted(ids) for road, ids in sorted(membership.items())}
    _dump("relations", result)
    _log("relations: %d scanned, %d E-roads, %d member ways, %.0fs"
         % (seen, len(result), len({w for v in result.values() for w in v}),
            time.time() - started))
    return result


# -- pass 2: ways ----------------------------------------------------------


def scan_ways(pbf: Path, membership: dict[str, list[int]], roster: set[str],
              refresh: bool = False) -> dict:
    if not refresh and cached("ways"):
        return _load("ways")

    started = time.time()
    member_of: dict[int, list[str]] = collections.defaultdict(list)
    for road, ids in membership.items():
        if road in roster:
            for way_id in ids:
                member_of[way_id].append(road)

    roads_out: dict[str, list] = {}
    links_out: list = []
    bridges_out: list = []
    crossings_out: list = []
    seen = 0

    processor = (osmium.FileProcessor(str(pbf), osmium.osm.WAY)
                 .with_filter(osmium.filter.KeyFilter("highway", "route")))
    for way in processor:
        seen += 1
        if seen % 10_000_000 == 0:
            _log("  ...%dM road ways scanned (%.0fs)"
                 % (seen // 1_000_000, time.time() - started))
        tags = way.tags
        highway = tags.get("highway")
        crossing = tags.get("route") in CROSSING_ROUTES
        ferry = crossing and tags.get("motor_vehicle") != "no"
        if highway is None and not crossing:
            continue

        from_relation = member_of.get(way.id)
        tagged = (e_refs(tags.get("int_ref")) | e_refs(tags.get("ref"))) & roster
        nodes = [n.ref for n in way.nodes]
        if len(nodes) < 2:
            continue

        if crossing and not (from_relation or tagged):
            # A car-carrying crossing with no E-number of its own; kept so a
            # road's sea link can be matched to it later.
            crossings_out.append([way.id, nodes, tags.get("name") or "",
                                  tags.get("route")])
        if highway in BRIDGE_CLASSES and not (from_relation or tagged):
            bridges_out.append([way.id, nodes, tags.get("ref") or "", highway,
                                tags.get("oneway") or "",
                                tags.get("junction") or ""])

        if (from_relation or tagged) and (ferry or highway in ROAD_CLASSES):
            roads_out[str(way.id)] = [
                sorted(set(from_relation or ()) | tagged),
                nodes,
                tags.get("ref") or "",
                tags.get("name") or "",
                highway or "ferry",
                tags.get("oneway") or "",
                tags.get("junction") or "",
                1 if ferry else 0,
            ]
        elif highway in LINK_CLASSES:
            links_out.append([way.id, nodes, tags.get("ref") or "",
                              tags.get("name") or "", highway,
                              tags.get("oneway") or "",
                              tags.get("junction") or ""])

    kept_links = _links_touching_roads(roads_out, links_out)
    payload = {"roads": roads_out, "links": kept_links}
    _dump("ways", payload)
    _dump("bridges", {"roads": bridges_out, "crossings": crossings_out})
    _log("ways: %d road ways scanned, kept %d E-road ways; "
         "%d ramps of %d candidates touch them; "
         "%d trunk ways and %d crossings held for bridging, %.0fs"
         % (seen, len(roads_out), len(kept_links), len(links_out),
            len(bridges_out), len(crossings_out), time.time() - started))
    return payload


def _links_touching_roads(roads_out: dict, links: list) -> list:
    """Keep only the ramps that actually reach E-road geometry.

    A ramp is worth carrying if a driver could use it to get from one E-road to
    another, which means it has to share a node with the E-road network - either
    directly, or through another ramp that does.  Everything else is a slip road
    to somewhere we do not route.
    """
    reachable: set[int] = set()
    for record in roads_out.values():
        reachable.update(record[1])

    kept: dict[int, list] = {}
    for _ in range(LINK_GROWTH_ROUNDS):
        added = False
        for record in links:
            if record[0] in kept:
                continue
            if any(node in reachable for node in record[1]):
                kept[record[0]] = record
                reachable.update(record[1])
                added = True
        if not added:
            break
    return list(kept.values())


# -- pass 3: nodes ---------------------------------------------------------


def coords_path() -> Path:
    return CACHE / "coords.npz"


def scan_nodes(pbf: Path, wanted: set[int], refresh: bool = False):
    """Coordinates for the wanted nodes, by id filter, in one pass.

    Written straight into numpy arrays rather than a dict.  Once the trunk
    network is included this is tens of millions of nodes, and a Python dict of
    that size needs about ten gigabytes where three arrays need half of one.
    """
    if not refresh and coords_path().exists():
        return coordstore.CoordStore.load(coords_path())

    started = time.time()
    size = max(len(wanted), 1)
    ids = np.empty(size, dtype=np.int64)
    lat = np.empty(size, dtype=np.float64)
    lon = np.empty(size, dtype=np.float64)
    found = 0

    processor = (osmium.FileProcessor(str(pbf), osmium.osm.NODE)
                 .with_filter(osmium.filter.IdFilter(wanted)))
    for node in processor:
        location = node.location
        if not location.valid():
            continue
        if found >= size:            # ids can repeat across an extract
            break
        ids[found] = node.id
        lat[found] = location.lat
        lon[found] = location.lon
        found += 1

    store = coordstore.CoordStore(ids[:found], lat[:found], lon[:found])
    store.save(coords_path())
    _log("nodes: resolved %d of %d wanted, %.0fs"
         % (found, len(wanted), time.time() - started))
    return store


def scan_places(pbf: Path, refresh: bool = False) -> list:
    """Every settlement, for the city picker."""
    if not refresh and cached("places"):
        return _load("places")

    started = time.time()
    places = []
    processor = (osmium.FileProcessor(str(pbf), osmium.osm.NODE)
                 .with_filter(osmium.filter.KeyFilter("place")))
    for node in processor:
        tags = node.tags
        if tags.get("place") not in PLACE_RANKS or not tags.get("name"):
            continue
        location = node.location
        if not location.valid():
            continue
        places.append([
            node.id, round(location.lat, 6), round(location.lon, 6),
            tags.get("name"), tags.get("place"), tags.get("population") or "",
            tags.get("name:en") or "", tags.get("int_name") or "",
            tags.get("wikidata") or "",
        ])
    _dump("places", places)
    _log("places: %d settlements, %.0fs" % (len(places), time.time() - started))
    return places


def run(pbf: Path, roster: set[str], refresh: bool = False) -> dict:
    use_source(pbf)
    membership = scan_relations(pbf, refresh)
    ways = scan_ways(pbf, membership, roster, refresh)

    wanted: set[int] = set()
    for record in ways["roads"].values():
        wanted.update(record[1])
    for record in ways["links"]:
        wanted.update(record[1])
    extra = _load("bridges") or {}
    for record in extra.get("roads", ()):
        wanted.update(record[1])
    for record in extra.get("crossings", ()):
        wanted.update(record[1])
    _log("need coordinates for %d nodes" % len(wanted))

    coords = scan_nodes(pbf, wanted, refresh)
    places = scan_places(pbf, refresh)
    return {"membership": membership, "ways": ways,
            "coords": coords, "places": places}


if __name__ == "__main__":
    pbf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PBF
    roster = set(agr.load_roster())
    _log("reading %s (%.1f GB)" % (pbf, pbf.stat().st_size / 1e9))
    run(pbf, roster)
    _log("done")

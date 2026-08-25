"""Pull every E-road way out of OpenStreetMap, by three cheap bulk steps.

Two independent sources have to be combined, because neither is complete:

*   **Relation membership.**  OSM relation 7884303 is a curated index of the
    E-road network - ``type=network``, 246 members, one relation per road, each
    with a clean canonical ``ref``.  Taking the road id from the *parent* means
    the children's refs never matter, and they are a mess: E 05 alone has
    children reffed "E 05", "E-01", "E17" and nothing at all.

*   **Way tags.**  Relations are not the whole story.  Norway has no route
    relation for E6, E69, E134 or E136 at all; its E-roads live on the ways as a
    plain ``ref=E 6``.  The UK puts them in ``int_ref``.  So sweep every way in
    Europe carrying an E-number in either tag, and validate what comes back
    against the AGR roster before believing it.

The three steps:

1.  ``membership`` - one query returns all 550 E-road relations with their
    member way ids.  443 621 ids in 38 MB, in about half a minute.  This
    replaces 246 separate per-road queries.
2.  ``tiles`` - sweep the continent in bounding-box tiles for ways tagged with
    an E-number, fetching geometry.  Bounding boxes are used rather than
    ``area["ISO3166-1"=...]`` because resolving the area of a country the size
    of Russia is slow and times out, whereas tiles are uniform, individually
    cacheable and resumable.
3.  ``fill`` - a member way that carries no E-number tag is not picked up by the
    tile sweep, so fetch whatever is still missing by id, in large batches.
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import agr
import osm

MASTER_RELATION = 7884303

# Fetch box: Atlantic to beyond the Volga, Crete to Nordkapp.  Wider than the
# routing extent on purpose - the graph is clipped later, and a road cut off
# mid-fetch would look broken rather than clipped.
FETCH_BBOX = (-25.0, 34.0, 52.0, 72.0)  # west, south, east, north
TILE_LON, TILE_LAT = 6.0, 4.0

FILL_BATCH = 4000

ROOT = Path(__file__).resolve().parent.parent
MEMBERSHIP_PATH = ROOT / "cache" / "membership.json"

# Way classes that can plausibly carry an E-road.  Anything else tagged with an
# E-number is mistagging or coincidence (British minor roads reffed "E4").
ROAD_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
}

_E_REF = re.compile(r"^\s*E[\s-]?(\d{1,3})\s*$", re.IGNORECASE)


def e_refs_from_tag(value: str | None) -> set[str]:
    """Canonical E-road ids mentioned in a ``ref``/``int_ref`` tag value.

    Handles the separators OSM uses in practice - ``E 25;E 35;E 60`` for a
    concurrency - and the spelling variants ``E 6`` / ``E6`` / ``E-6``.
    """
    if not value:
        return set()
    found = set()
    for part in re.split(r"[;,/]", value):
        match = _E_REF.match(part)
        if match:
            found.add(agr.canonical_id(match.group(1)))
    return found


def tiles() -> list[tuple[float, float, float, float]]:
    return osm.bbox_tiles(*FETCH_BBOX, TILE_LON, TILE_LAT)


def _tile_key(tile: tuple[float, float, float, float]) -> str:
    return "tileB_%.4f_%.4f_%.4f_%.4f" % tile


# -- step 1: relation membership ------------------------------------------


def fetch_membership(refresh: bool = False) -> dict[str, list[int]]:
    """Map canonical road id -> member way ids, from the master network.

    Two levels are walked: the master's 246 members, and the per-country
    subrelations of the 60 that are superroutes.
    """
    if MEMBERSHIP_PATH.exists() and not refresh:
        return json.loads(MEMBERSHIP_PATH.read_text(encoding="utf-8"))

    payload = osm.query(
        "[out:json][timeout:900];"
        "rel(%d)->.top;"
        "rel(r.top)->.lvl1;"
        "rel(r.lvl1)->.lvl2;"
        "(.lvl1; .lvl2;);"
        "out body;" % MASTER_RELATION,
        key="membership_all", refresh=refresh)

    by_id = {element["id"]: element for element in payload["elements"]}

    # Only the master's direct members carry a trustworthy ref.
    top_level = {}
    for element in payload["elements"]:
        tags = element.get("tags", {})
        raw = tags.get("ref") or tags.get("int_ref")
        if raw and tags.get("type") in ("route", "superroute"):
            top_level[element["id"]] = raw

    membership: dict[str, set[int]] = collections.defaultdict(set)

    def collect(relation_id: int, road: str, depth: int = 0) -> None:
        element = by_id.get(relation_id)
        if element is None or depth > 3:
            return
        for member in element.get("members", []):
            if member["type"] == "way":
                membership[road].add(member["ref"])
            elif member["type"] == "relation":
                collect(member["ref"], road, depth + 1)

    # Seed from the master relation's own member list.
    master = osm.query("[out:json][timeout:300];rel(%d);out body;" % MASTER_RELATION,
                       key="master_relation")
    master_members = master["elements"][0].get("members", [])
    for member in master_members:
        if member["type"] != "relation":
            continue
        raw = top_level.get(member["ref"])
        if not raw:
            continue
        collect(member["ref"], agr.canonical_id(raw))

    result = {road: sorted(ids) for road, ids in sorted(membership.items())}
    MEMBERSHIP_PATH.write_text(json.dumps(result), encoding="utf-8")
    return result


# -- step 2: tag sweep by tile ---------------------------------------------


def fetch_tile(tile: tuple[float, float, float, float]) -> dict:
    south, west, north, east = tile
    box = "%.4f,%.4f,%.4f,%.4f" % (south, west, north, east)
    query = (
        "[out:json][timeout:600];"
        "("
        'way["highway"]["int_ref"~"E[ -]?[0-9]"](%s);'
        'way["highway"]["ref"~"(^|[;,/])[ ]*E[ -]?[0-9]"](%s);'
        'way["route"="ferry"]["int_ref"~"E[ -]?[0-9]"](%s);'
        'way["route"="ferry"]["ref"~"(^|[;,/])[ ]*E[ -]?[0-9]"](%s);'
        ");"
        "out body geom;" % (box, box, box, box)
    )
    return osm.query(query, key=_tile_key(tile))


def _split(tile: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    south, west, north, east = tile
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    return [(south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
            (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east)]


def fetch_tile_adaptive(tile, depth: int = 0, max_depth: int = 2) -> bool:
    """Fetch one tile, subdividing it if Overpass cannot manage the whole cell.

    Some cells really are too heavy to answer - a dense country plus a hard
    regex over every way in it - and no amount of retrying fixes that.  Cutting
    the cell into quarters does, and quarters are cached under their own keys so
    the work is never repeated.
    """
    key = _tile_key(tile)
    if osm.cached(key):
        return True
    try:
        fetch_tile(tile)
        return True
    except osm.OverpassError as error:
        south, west, north, east = tile
        print("  tile lat %.1f..%.1f lon %.1f..%.1f failed (%s)"
              % (south, north, west, east, error), file=sys.stderr)
        if depth >= max_depth:
            print("  giving up on this cell; it will be reported as a gap",
                  file=sys.stderr)
            return False
        print("  splitting into quarters", file=sys.stderr)
        return all([fetch_tile_adaptive(part, depth + 1, max_depth)
                    for part in _split(tile)])


def run_tiles() -> None:
    todo = tiles()
    pending = [t for t in todo if not osm.cached(_tile_key(t))]
    print("tiles: %d total, %d still to fetch" % (len(todo), len(pending)),
          file=sys.stderr)
    failed = []
    for index, tile in enumerate(pending, 1):
        south, west, north, east = tile
        print("[tile %3d/%3d] lat %.0f..%.0f lon %.0f..%.0f"
              % (index, len(pending), south, north, west, east), file=sys.stderr)
        if not fetch_tile_adaptive(tile):
            failed.append(tile)
    if failed:
        print("tiles: %d cells could not be fetched: %s" % (len(failed), failed),
              file=sys.stderr)


# -- targeted sweep of coverage gaps ---------------------------------------


def _gap_key(box: tuple[float, float, float, float]) -> str:
    return "gap_%.3f_%.3f_%.3f_%.3f" % box


def gap_keys() -> list[str]:
    return [path.name.split(".")[0] for path in osm.cached_paths("gap_")]


def run_gap_sweep(boxes: list[tuple[float, float, float, float]]) -> list:
    """Sweep by tag only around control cities their own road fails to reach.

    This is the whole discovery mechanism, and it is pointed rather than blind.
    A road that does not reach a city Annex I says it must has a gap, and the
    gap is at that city - so look there, in a box a degree or so across, instead
    of scanning the continent.  Norway triggers most of these, because its
    E-roads carry no route relation and live only on the ways' own ``ref``.
    """
    pending = [box for box in boxes if not osm.cached(_gap_key(box))]
    print("gap sweep: %d areas, %d still to fetch" % (len(boxes), len(pending)),
          file=sys.stderr)
    failed = []
    for index, box in enumerate(pending, 1):
        south, west, north, east = box
        area = "%.4f,%.4f,%.4f,%.4f" % box
        print("[gap %3d/%3d] lat %.1f..%.1f lon %.1f..%.1f"
              % (index, len(pending), south, north, west, east), file=sys.stderr)
        try:
            osm.query(
                "[out:json][timeout:600];"
                "("
                'way["highway"]["int_ref"~"E[ -]?[0-9]"](%s);'
                'way["highway"]["ref"~"(^|[;,/])[ ]*E[ -]?[0-9]"](%s);'
                'way["route"="ferry"]["int_ref"~"E[ -]?[0-9]"](%s);'
                'way["route"="ferry"]["ref"~"(^|[;,/])[ ]*E[ -]?[0-9]"](%s);'
                ");"
                "out body geom;" % (area, area, area, area),
                key=_gap_key(box), attempts=5)
        except osm.OverpassError as error:
            print("  gap area failed: %s" % error, file=sys.stderr)
            failed.append(box)
    return failed


# -- step 3: fill in member ways the tag sweep never saw --------------------


def member_batches() -> list[list[int]]:
    """Every member way id, in fixed-size batches with a stable ordering.

    The batching must not depend on what happens to be cached already, or the
    same way would land in a different batch on a resumed run and be fetched
    twice under a different key.  Sorting the full member list and chunking it
    makes batch N mean the same thing on every run.
    """
    membership = fetch_membership()
    wanted = sorted({way_id for ids in membership.values() for way_id in ids})
    return [wanted[i:i + FILL_BATCH] for i in range(0, len(wanted), FILL_BATCH)]


def fill_keys() -> list[str]:
    return ["fill_%04d" % index for index in range(len(member_batches()))]


def run_fill(reverse: bool = False) -> None:
    """Fetch geometry for every relation member way, by id.

    This is the bulk of the network and is deliberately independent of the tile
    sweep: id lookups are cheap and reliable, whereas a regex-over-bounding-box
    scan is the most expensive thing you can ask Overpass for.  Running the two
    separately keeps the critical path off the slow query.

    Overpass allows two concurrent queries per client, so a second worker
    started with ``reverse`` walks the batches from the far end and the two meet
    in the middle.  Each batch is checked for cache immediately before it is
    fetched, so the overlap costs at most one duplicated query.
    """
    batches = member_batches()
    order = list(range(len(batches)))
    if reverse:
        order.reverse()
    pending = [i for i in order if not osm.cached("fill_%04d" % i)]
    print("fill: %d member ways in %d batches, %d still to fetch%s"
          % (sum(len(b) for b in batches), len(batches), len(pending),
             " (reverse)" if reverse else ""),
          file=sys.stderr)
    def attempt(index: int, label: str) -> bool:
        key = "fill_%04d" % index
        if osm.cached(key):      # the other worker may have taken it meanwhile
            return True
        print("[fill %s] batch %d, %d ways"
              % (label, index, len(batches[index])), file=sys.stderr)
        try:
            osm.query("[out:json][timeout:900];way(id:%s);out body geom;"
                      % ",".join(str(w) for w in batches[index]), key=key)
            return True
        except osm.OverpassError as error:
            print("  batch %d failed, will retry at the end: %s" % (index, error),
                  file=sys.stderr)
            return False

    # A batch that will not come back must not take the whole run with it: the
    # server is usually just busy, and it will very likely answer on a later
    # pass once the rest of the queue has drained.
    failed = [index for position, index in enumerate(pending, 1)
              if not attempt(index, "%3d/%3d" % (position, len(pending)))]

    for round_number in range(3):
        if not failed:
            break
        print("retry round %d: %d batches" % (round_number + 1, len(failed)),
              file=sys.stderr)
        failed = [index for index in failed if not attempt(index, "retry")]

    if failed:
        print("fill: %d batches could not be fetched: %s" % (len(failed), failed),
              file=sys.stderr)


if __name__ == "__main__":
    steps = sys.argv[1:] or ["membership", "tiles", "fill"]
    if "membership" in steps:
        membership = fetch_membership()
        total = len({w for ids in membership.values() for w in ids})
        print("membership: %d roads, %d distinct ways"
              % (len(membership), total), file=sys.stderr)
    if "tiles" in steps:
        run_tiles()
    if "fill" in steps:
        run_fill(reverse="reverse" in steps)
    print("done", file=sys.stderr)

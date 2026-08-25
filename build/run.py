"""Build the whole thing: AGR roster -> OSM -> graph -> webapp data -> report.

Every expensive step is cached, so re-running is cheap and interrupting is safe.
Stages can be run individually while iterating:

    python run.py roster
    python run.py fetch
    python run.py graph
    python run.py                 # everything
"""

from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import agr
import cities as cities_module
import coverage as coverage_module
import export
import geo
import graph
import bridge as bridge_module
import junctions
import pbf
import verify

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
NETWORK_PICKLE = CACHE / "network.pickle"


def log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


# Which extract to build from.  A country file is handy for iterating on one
# road without waiting on the continental one.
PBF = pbf.DEFAULT_PBF


def stage_roster() -> dict:
    log("parsing AGR Annex I")
    agr.build(agr.DEFAULT_PDF, agr.ROSTER_PATH)
    pbf.use_source(PBF)
    membership = pbf.scan_relations(PBF)
    roster = agr.load_roster(set(membership))
    log("roster: %d roads (%d also carry an OSM route relation)"
        % (len(roster), len(membership)))
    return roster


def expected_chains(roster: dict, city_index) -> dict[str, list]:
    """Where each road is supposed to run, as a polyline of its control cities.

    Used to sanity-check road numbers that come only from a way's own tags.
    Several countries number their own roads with an E prefix, and place is what
    separates those from the real thing.
    """
    chains: dict[str, list] = {}
    for road_id, road in roster.items():
        points = coverage_module.resolve_chain(road.get("points", []), city_index)
        located = [(p.lat, p.lon) for p in points if p.lat is not None]
        if located:
            chains[road_id] = located
    return chains


def treaty_meetings(expected: dict[str, list]) -> list[tuple[str, str, float, float]]:
    """Road pairs the AGR says share a control city, with where that is.

    Two roads listing the same city are the treaty stating you can change
    between them there.  That is the only warrant strong enough to connect
    geometry that OSM leaves apart.
    """
    at_place: dict[tuple[float, float], set[str]] = {}
    for road, chain in expected.items():
        for point in chain:
            at_place.setdefault(point, set()).add(road)
    meetings = []
    for (lat, lon), roads in at_place.items():
        ordered = sorted(roads)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                meetings.append((a, b, lat, lon))
    return meetings


def stage_graph(roster: dict, expected: dict | None = None,
                places: list | None = None):
    """Build the corridor graph, the interchanges and the legs."""
    log("loading ways from the PBF scan")
    ways, coords, stats = graph.load_from_pbf(set(roster), PBF, expected=expected)
    log("  %d ways, %d nodes" % (len(ways), len(coords)))

    log("stitching way ends that meet on the ground but share no node")
    joins = graph.stitch_endpoints(ways, coords)
    stats["endpoints_stitched"] = len(joins)
    log("  %d joins made (none further than 12 m)" % len(joins))

    log("tying ferry landings to the roads they carry")
    landings = graph.stitch_ferry_landings(ways, coords)
    stats["ferry_landings_joined"] = len(landings)
    log("  %d ferry ends joined" % len(landings))
    for record in landings[:6]:
        log("    %s at %.3f,%.3f - %.0f m to the road" % record)

    log("dropping stray fragments no relation vouches for")
    orphans = graph.prune_orphan_fragments(ways, coords,
                                          stats.pop("_member_of", {}), expected)
    stats["orphan_fragments_dropped"] = len(orphans)
    log("  %d fragments dropped" % len(orphans))
    for record in orphans[:6]:
        log("    %s: %d ways at %.3f,%.3f, %.0f km off the treaty line"
            % (record[0], record[3], record[1], record[2], record[4]))

    extra = pbf._load("bridges") or {}
    if extra:
        log("bridging tagging gaps along the trunk network")

        def make_way(record, roads):
            way_id, nodes, ref, highway, oneway, junction = record
            return graph.Way(id=way_id, roads=frozenset(roads), nodes=nodes,
                             national=graph.normalise_national_ref(ref),
                             name="", highway=highway, ferry=False,
                             oneway=graph._oneway_of({"oneway": oneway,
                                                      "junction": junction}),
                             sources={"bridge"})

        bridged = bridge_module.bridge_tagging_gaps(
            ways, coords, extra.get("roads", []), make_way)
        stats["tagging_gaps_bridged"] = len(bridged)
        log("  %d gaps bridged" % len(bridged))
        for record in bridged[:6]:
            log("    %s: %.1f km gap closed along %.1f km of trunk road (%d ways)"
                % record)

        def make_crossing(record, roads):
            way_id, nodes, name, kind = record
            return graph.Way(id=way_id, roads=frozenset(roads), nodes=nodes,
                             national=[], name=name, highway=kind, ferry=True,
                             oneway=0, sources={"crossing"})

        log("matching car crossings to the sea links they span")
        attached = bridge_module.attach_crossings(
            ways, coords, extra.get("crossings", []), roster,
            expected or {}, make_crossing)
        stats["crossings_attached"] = len(attached)
        log("  %d crossings attached" % len(attached))
        for record in attached[:6]:
            log("    %s <- %s (%.0f km)" % record)

    log("closing short gaps within a single road")
    self_joins = graph.stitch_road_components(ways, coords)
    stats["road_gaps_closed"] = len(self_joins)
    log("  %d gaps closed (none wider than 400 m)" % len(self_joins))
    for record in self_joins[:6]:
        log("    %s at %.3f,%.3f - %.0f m" % record)

    if expected:
        log("connecting roads the treaty says meet but the data leaves apart")
        meetings = treaty_meetings(expected)
        treaty = graph.stitch_treaty_crossings(ways, coords, meetings)
        stats["treaty_crossings_joined"] = len(treaty)
        log("  %d of %d treaty meetings needed a join" % (len(treaty), len(meetings)))
        for record in treaty[:8]:
            log("    %s x %s at %.3f,%.3f - %.0f m apart"
                % (record[0], record[1], record[2], record[3], record[4]))

    network = graph.Network(ways, coords)
    log("finding vertices")
    network.find_vertices()
    log("  %d vertices" % len(network.vertices))
    log("contracting corridors")
    network.build_corridors()
    log("  %d corridors" % len(network.corridors))

    roads_network = junctions.RoadNetwork(network)
    roads_network.build_adjacency()
    junction_vertices = roads_network.junction_vertices()
    log("  %d junction vertices" % len(junction_vertices))
    roads_network.cluster(junction_vertices)
    log("  %d interchanges after clustering" % len(roads_network.interchanges))
    added = roads_network.add_terminals()
    log("  %d road termini added -> %d interchanges"
        % (added, len(roads_network.interchanges)))
    if places:
        # A first pass of legs tells us which interchanges are actually usable,
        # so a city is only considered covered by one it could really use.
        roads_network.build_legs()
        usable = cities_module.usable_interchanges(
            roads_network.legs, len(roads_network.interchanges))
        anchored = roads_network.add_city_anchors(places, usable=usable)
        log("  %d cities given their own point on the road -> %d interchanges"
            % (anchored, len(roads_network.interchanges)))
    log("building legs")
    roads_network.build_legs()
    mirrored = roads_network.mirror_missing_legs()
    log("  %d legs (%d mirrored for the return journey, %d rejected as doubling back)"
        % (len(roads_network.legs), mirrored, len(roads_network.rejected_legs)))
    stats["legs_mirrored"] = mirrored
    return network, roads_network, stats


def stage_cities(network, roads_network, roster, countries, all_cities, index):
    log("assessing control-city coverage")
    coverage = coverage_module.assess(roster, network, index)
    for road_id, result in coverage.items():
        for point in result.points:
            city = all_cities.get(point.city_id) if point.city_id else None
            if city is not None:
                city.agr = True
                if road_id not in city.agr_roads:
                    city.agr_roads.append(road_id)

    log("naming interchanges")
    cities_module.label_interchanges(roads_network.interchanges, all_cities, countries)
    log("attaching cities to the network")
    usable = cities_module.usable_interchanges(roads_network.legs,
                                               len(roads_network.interchanges))
    log("  %d of %d interchanges can be both entered and left"
        % (len(usable), len(roads_network.interchanges)))
    access = cities_module.attach(all_cities, roads_network.interchanges, usable)
    log("  %d settlements have an interchange within reach" % len(access))

    selectable = {
        city.id for city in all_cities.values()
        if city.id in access
        and (city.agr or (city.population or 0) >= cities_module.MIN_POPULATION)
    }
    log("  %d selectable cities" % len(selectable))
    return all_cities, index, coverage, access, selectable


def main(stages: list[str]) -> None:
    everything = not stages

    roster = stage_roster()
    if stages == ["roster"]:
        return

    countries = geo.Countries()
    log("basemap")
    geo.build_basemap(ROOT / "web" / "data" / "europe.geo.json")

    # Cities are loaded before the graph so that a road's control-city chain is
    # available to vet road numbers taken from way tags.
    log("loading settlements")
    all_cities = cities_module.load_from_pbf(pbf._load("places") or [], countries)
    index = cities_module.CityIndex(all_cities)
    log("  %d settlements" % len(all_cities))
    log("resolving AGR control chains for tag validation")
    expected = expected_chains(roster, index)
    log("  %d roads have a located chain" % len(expected))

    # Cities that will be selectable, so each can be given a node on the road
    # that passes it even where no two E-roads meet.
    control = {point for chain in expected.values() for point in chain}
    places = sorted({(c.lat, c.lon) for c in all_cities.values()
                     if (c.population or 0) >= cities_module.MIN_POPULATION}
                    | control)
    log("  %d places to anchor" % len(places))

    network, roads_network, stats = stage_graph(roster, expected, places)
    all_cities, index, coverage, access, selectable = stage_cities(
        network, roads_network, roster, countries, all_cities, index)

    # The coverage check is now purely a report: reading a planet extract means
    # there is nothing left to go and fetch, so a control city a road fails to
    # reach is a fact about OSM or about the AGR text, not a gap in what we
    # asked for.
    gap_boxes = coverage_module.gap_boxes(coverage)
    log("%d control cities are not reached by their own road" % len(gap_boxes))

    log("checking the three-changes claim")
    changes = verify.changes_matrix(roads_network.legs, all_cities, access, selectable)

    log("exporting")
    payload = export.build(roster, network, roads_network, all_cities, access, coverage)
    city_payload = export.build_cities(all_cities, access, selectable)
    sizes = export.write(payload, city_payload)
    for name, size in sizes.items():
        log("  %s %.2f MB" % (name, size))

    log("writing report")
    verify.write_report(
        roster=roster, stats=stats, network=network, roads_network=roads_network,
        coverage=coverage, cities=all_cities, access=access, selectable=selectable,
        city_index=index, sizes=sizes, gap_boxes=gap_boxes, changes=changes)
    log("report -> %s" % verify.REPORT_PATH)
    log("done")


if __name__ == "__main__":

    arguments = sys.argv[1:]
    if arguments and arguments[0].endswith(".pbf"):
        PBF = Path(arguments.pop(0))
    main(arguments)

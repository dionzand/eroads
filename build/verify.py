"""The build report: what the network actually contains, and where it is thin.

Written as a document rather than a pass/fail, because most of the interesting
answers are numbers with context.  Every section is designed so that a bad
result is *visible* rather than absorbed - a road that reaches none of its
control cities, a city pair needing four changes, a junction that had to be
repaired - all get named, not counted.

The claim the user actually wants checked is "every pair of large European
cities is reachable with at most three road changes".  Checking that pair by
pair would be a hundred million searches.  It collapses instead to a
breadth-first search on the road-adjacency graph - a couple of hundred nodes -
because the minimum number of changes between two cities is the hop count
between the roads that serve them.
"""

from __future__ import annotations

import collections
from datetime import datetime
from pathlib import Path

import route as routing

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "build_report.md"

MAX_ALLOWED_CHANGES = 3


def _table(rows: list[list[str]], headers: list[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def changes_matrix(legs, cities, access: dict, selectable: set[str]):
    """Minimum road changes between every pair of selectable cities.

    Returns the distribution and the pairs that exceed the limit.  Cities are
    grouped by the *set of roads* they can reach, because thousands of cities
    share only a few hundred distinct road sets - two cities on the same road
    have identical connectivity, and computing them separately is wasted work.
    """
    adjacency = routing.road_adjacency(legs)
    roads_at: dict[int, set[str]] = collections.defaultdict(set)
    for leg in legs:
        roads_at[leg.start].add(leg.road)
        roads_at[leg.end].add(leg.road)

    served: dict[str, frozenset[str]] = {}
    for city_id in selectable:
        roads: set[str] = set()
        for node, _ in access.get(city_id, ()):
            roads |= roads_at.get(node, set())
        if roads:
            served[city_id] = frozenset(roads)

    signatures = sorted({s for s in served.values()})
    hops_from: dict[str, dict[str, int]] = {}
    for road in {r for s in signatures for r in s}:
        hops_from[road] = routing.road_hops(adjacency, road)

    def changes_between(a: frozenset[str], b: frozenset[str]) -> int | None:
        best = None
        for start in a:
            reach = hops_from.get(start, {})
            for end in b:
                if end in reach:
                    hops = reach[end]
                    best = hops if best is None else min(best, hops)
        return best

    pair_changes: dict[tuple[frozenset, frozenset], int | None] = {}
    for i, a in enumerate(signatures):
        for b in signatures[i:]:
            pair_changes[(a, b)] = changes_between(a, b)

    distribution: collections.Counter = collections.Counter()
    unreachable = 0
    worst: list[tuple[str, str, int]] = []
    ids = sorted(served)
    for i, city_a in enumerate(ids):
        sig_a = served[city_a]
        for city_b in ids[i + 1:]:
            sig_b = served[city_b]
            key = (sig_a, sig_b) if (sig_a, sig_b) in pair_changes else (sig_b, sig_a)
            value = pair_changes.get(key)
            if value is None:
                unreachable += 1
                continue
            distribution[value] += 1
            if value > MAX_ALLOWED_CHANGES and len(worst) < 60:
                worst.append((city_a, city_b, value))
    return {
        "distribution": distribution,
        "unreachable": unreachable,
        "worst": worst,
        "cities_considered": len(served),
        "cities_without_roads": len(selectable) - len(served),
    }


def write_report(*, roster, stats, network, roads_network, coverage, cities,
                 access, selectable, city_index, sizes, gap_boxes,
                 changes) -> str:
    lines: list[str] = []
    add = lines.append

    add("# E-road network build report")
    add("")
    add("Generated %s." % datetime.now().strftime("%Y-%m-%d %H:%M"))
    add("")

    # -- 1. roster coverage ------------------------------------------------
    add("## 1. Roster")
    add("")
    live = [r for r in roster.values() if not r.get("deleted")]
    agr_roads = [r for r in live if r.get("agr", True)]
    # "Missing" covers two quite different failures and the report used to
    # blur them, because it measured presence by legs: a road could have its
    # full length on the ground and still be listed as having no geometry at
    # all.  E841 was exactly that - 35 km of Avellino-Salerno, both control
    # cities reached, but its two carriageways never join, so no leg can be
    # built and no journey can use it.  That is a different problem from a road
    # no way in the extract claims, and wants a different fix.
    with_legs = {leg.road for leg in roads_network.legs}
    with_geometry = {road for corridor in network.corridors for road in corridor.roads}
    absent = sorted(r["id"] for r in live if r["id"] not in with_geometry)
    stranded = sorted(r["id"] for r in live
                      if r["id"] in with_geometry and r["id"] not in with_legs)
    add("- AGR Annex I roads parsed: **%d** (%d live, %d marked deleted)"
        % (len(roster), len(live), len(roster) - len(live)))
    add("- Roads present in OSM but not in the 2016 AGR text: %s"
        % (", ".join(sorted(r["id"] for r in live if not r.get("agr", True))) or "none"))
    add("- Roads with routable geometry: **%d of %d**"
        % (len(with_legs), len(live)))
    if absent:
        add("- **No way in the extract claims these:** " + ", ".join(absent))
        add("  Nothing there carries the E-number in `ref`/`int_ref` and no route")
        add("  relation lists it. The Central Asian tails (E002-E019, E121-E127)")
        add("  are outside the mapped extent by design; the rest are OSM tagging")
        add("  gaps. They are left absent rather than reconstructed from the")
        add("  treaty text, which would be inventing a road.")
    if stranded:
        add("- **Present but not routable:** " + ", ".join(stranded))
        add("  Geometry was found and measured, but no leg could be built between")
        add("  interchanges - usually carriageways that never meet - so the router")
        add("  cannot offer a journey along them.")
    add("")

    # -- 2. what was fetched ----------------------------------------------
    add("## 2. Source data")
    add("")
    add(_table([[key.replace("_", " "), "{:,}".format(value)]
                for key, value in sorted(stats.items())],
               ["measure", "count"]))
    add("")
    add("- Corridors after contraction: **{:,}**".format(len(network.corridors)))
    add("- Interchanges: **{:,}**".format(len(roads_network.interchanges)))
    add("- Legs (directed, interchange to interchange): **{:,}**"
        .format(len(roads_network.legs)))
    add("")

    # -- 3. control-city coverage -----------------------------------------
    add("## 3. Does every road reach the places the treaty says it must?")
    add("")
    add("Annex I names the control cities each E-road has to pass through. "
        "This is the coverage test: a road that does not come within %d km of "
        "one of its own control cities has a gap in its geometry."
        % int(__import__("coverage").SERVED_KM))
    add("")
    total_points = sum(len(c.points) for c in coverage.values())
    unmatched = sum(len(c.unmatched) for c in coverage.values())
    unserved = sum(len(c.unserved) for c in coverage.values())
    matched = total_points - unmatched
    add("- Control points in Annex I: **{:,}**".format(total_points))
    add("- Matched to a real settlement: **{:,}** ({:.1%})"
        .format(matched, matched / max(total_points, 1)))
    add("- Reached by their own road: **{:,}** ({:.1%} of matched)"
        .format(matched - unserved, (matched - unserved) / max(matched, 1)))
    add("")

    if matched == 0:
        add("> **This check did not run.** No control city could be matched to a "
            "settlement, which means the settlement dataset is missing or empty - "
            "not that coverage is good. Fetch the cities and rebuild before "
            "trusting anything in this section.")
        add("")

    worst_roads = sorted(coverage.values(),
                         key=lambda c: -len(c.unserved))[:25]
    rows = [[c.road, "{:,.0f}".format(c.geometry_km), len(c.points),
             len(c.unserved),
             ", ".join(p.name for p in c.unserved[:6]) or "-"]
            for c in worst_roads if c.unserved]
    if rows:
        add("### Roads that miss control cities")
        add("")
        add(_table(rows, ["road", "km of geometry", "control points",
                          "unreached", "which"]))
        add("")
    elif matched:
        add("Every road reaches every control point it could be matched to.")
        add("")

    if gap_boxes:
        add("%d gap areas were swept by tag to look for untagged or "
            "unrelated geometry." % len(gap_boxes))
        add("")

    # -- 4. repairs and rejections ----------------------------------------
    add("## 4. Repairs and rejections")
    add("")
    rejected = roads_network.rejected_legs
    add("- Loops back to the same interchange, discarded as not being journeys: "
        "**%d** (these are normal - a path round an interchange's own ramps)"
        % getattr(roads_network, "loop_legs", 0))
    add("- Legs rejected as doubling back, meaning two interchanges that should "
        "have been merged into one: **%d**" % len(rejected))
    if rejected:
        rows = [[leg.road, "{:.0f}".format(leg.km), leg.start, leg.end]
                for leg in sorted(rejected, key=lambda l: -l.km)[:15]]
        add("")
        add(_table(rows, ["road", "km", "from jx", "to jx"]))
    add("")
    radii = [i.radius_km for i in roads_network.interchanges if i.radius_km > 0]
    if radii:
        radii.sort()
        add("- Interchange cluster radius: median %.2f km, 95th %.2f km, max %.2f km"
            % (radii[len(radii) // 2], radii[int(len(radii) * 0.95)], radii[-1]))
    unnamed = sum(1 for i in roads_network.interchanges if not i.near_city)
    add("- Interchanges with no city within 25 km (shown by coordinate): **%d**"
        % unnamed)
    add("")

    # -- 5. the three-changes claim ---------------------------------------
    add("## 5. Can every pair of cities be linked with at most %d changes?"
        % MAX_ALLOWED_CHANGES)
    add("")
    distribution = changes["distribution"]
    total_pairs = sum(distribution.values())
    add("- City pairs examined: **{:,}**".format(total_pairs))
    add("- Cities with no road at all: %d" % changes["cities_without_roads"])
    add("- Pairs with no E-road connection whatsoever: **{:,}**"
        .format(changes["unreachable"]))
    add("")
    rows = [[changes_needed, "{:,}".format(count),
             "{:.2%}".format(count / max(total_pairs, 1))]
            for changes_needed, count in sorted(distribution.items())]
    add(_table(rows, ["road changes", "pairs", "share"]))
    add("")
    over = sum(count for value, count in distribution.items()
               if value > MAX_ALLOWED_CHANGES)
    if over == 0 and changes["unreachable"] == 0:
        add("**Every pair is reachable within %d changes.**" % MAX_ALLOWED_CHANGES)
    else:
        add("**{:,} pairs need more than {} changes** ({:.2%} of all pairs)."
            .format(over, MAX_ALLOWED_CHANGES, over / max(total_pairs, 1)))
        if changes["worst"]:
            add("")
            add("Examples:")
            add("")
            rows = []
            for city_a, city_b, value in changes["worst"][:20]:
                a, b = cities.get(city_a), cities.get(city_b)
                if a and b:
                    rows.append([a.label, b.label, value])
            add(_table(rows, ["from", "to", "changes"]))
    add("")

    # -- 6. name collisions -----------------------------------------------
    add("## 6. Cities whose names are not unique")
    add("")
    add("Every city is keyed by its OSM node id and shown with its country, "
        "because these names cannot identify a place on their own.")
    add("")
    collisions = city_index.collisions()
    selectable_collisions = {
        name: [c for c in group if c.id in selectable]
        for name, group in collisions.items()}
    selectable_collisions = {
        name: group for name, group in selectable_collisions.items()
        if len({c.country for c in group}) > 1}
    add("- Name collisions among selectable cities: **%d**"
        % len(selectable_collisions))
    add("")
    rows = []
    for name in sorted(selectable_collisions)[:25]:
        group = selectable_collisions[name]
        rows.append([group[0].name,
                     ", ".join("%s (%s)" % (c.country, c.id) for c in group[:6])])
    if rows:
        add(_table(rows, ["name", "distinct places"]))
    add("")

    # -- 7. output --------------------------------------------------------
    add("## 7. Output")
    add("")
    add(_table([[name, "%.2f MB" % size] for name, size in sorted(sizes.items())],
               ["file", "size"]))
    add("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(text, encoding="utf-8")
    return text

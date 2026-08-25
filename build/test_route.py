"""Self-tests for the parts whose logic is easy to get subtly wrong.

Routing, canonical numbering and the control-chain resolver are all testable
without touching OSM, and all three have failure modes that would be invisible
in the finished app: a route that quietly takes four roads where three would do,
an E01 that collides with E001, a "Brest" resolved to the wrong country.

Run with:  python test_route.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import agr
import route as routing


@dataclass
class FakeLeg:
    road: str
    start: int
    end: int
    km: float
    national: list = None
    ferry: bool = False

    def __post_init__(self):
        if self.national is None:
            self.national = []


def both_ways(road, a, b, km):
    return [FakeLeg(road, a, b, km), FakeLeg(road, b, a, km)]


def check(name, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print("%s %s%s" % (mark, name, ("  <- " + detail) if detail and not condition else ""))
    return bool(condition)


def test_canonical_ids():
    ok = True
    ok &= check("E-01 / E 1 / E1 / E01 all fold to one id",
                {agr.canonical_id(s) for s in ("E-01", "E 1", "E1", "E01")} == {"E01"})
    ok &= check("E01 and E001 stay distinct",
                agr.canonical_id("E 01") != agr.canonical_id("E 001"),
                "%s vs %s" % (agr.canonical_id("E 01"), agr.canonical_id("E 001")))
    ok &= check("three-digit ids are kept verbatim",
                agr.canonical_id("E574") == "E574")
    ok &= check("display drops the zero pad only for two-digit roads",
                (agr.display_name("E01"), agr.display_name("E001")) == ("E1", "E001"))
    return ok


def test_prefers_fewer_changes():
    """A long single road must beat a short zig-zag across three roads."""
    legs = []
    legs += both_ways("E10", 0, 1, 200)
    legs += both_ways("E10", 1, 2, 200)      # E10: 0 -> 2 in 400 km, no changes
    legs += both_ways("E20", 0, 3, 100)
    legs += both_ways("E30", 3, 4, 100)
    legs += both_ways("E40", 4, 2, 100)      # via three roads: 300 km, 2 changes

    router = routing.Router(legs, 5)
    routes = router.plan({0: 0.0}, {2: 0.0}, wanted=3)

    ok = check("a route was found", bool(routes))
    if not routes:
        return False
    first = routes[0]
    ok &= check("first route makes no road changes", first.changes == 0,
                "changes=%d roads=%s" % (first.changes, first.roads))
    ok &= check("first route is the 400 km one", abs(first.km - 400) < 1e-6,
                "km=%s" % first.km)
    shorter = [r for r in routes if r.km < 400]
    ok &= check("a shorter alternative is also offered", bool(shorter),
                "routes=%s" % [(r.km, r.changes) for r in routes])
    if shorter:
        # Three roads means two changes, not three: the count is transitions,
        # not roads used.
        ok &= check("the alternative is the 300 km, two-change route",
                    abs(shorter[0].km - 300) < 1e-6 and shorter[0].changes == 2,
                    "km=%s changes=%d" % (shorter[0].km, shorter[0].changes))
    return ok


def test_change_is_counted_once_per_road():
    """Consecutive legs on one road are a single step, not one step per leg."""
    legs = []
    for i in range(5):
        legs += both_ways("E10", i, i + 1, 50)
    legs += both_ways("E20", 5, 6, 50)

    router = routing.Router(legs, 7)
    routes = router.plan({0: 0.0}, {6: 0.0}, wanted=1)
    ok = check("route found", bool(routes))
    if not routes:
        return False
    route = routes[0]
    ok &= check("five legs on E10 collapse into one step",
                len(route.steps) == 2, "steps=%s" % [s.road for s in route.steps])
    ok &= check("exactly one change is counted", route.changes == 1,
                "changes=%d" % route.changes)
    ok &= check("distance is the sum of the legs", abs(route.km - 300) < 1e-6,
                "km=%s" % route.km)
    return ok


def test_concurrency_costs_one_change():
    """Sharing pavement still counts as changing road number, at the split."""
    legs = []
    legs += both_ways("E10", 0, 1, 100)
    # 1 -> 2 is concurrent: both roads have a leg over it.
    legs += both_ways("E10", 1, 2, 50)
    legs += both_ways("E20", 1, 2, 50)
    legs += both_ways("E20", 2, 3, 100)

    router = routing.Router(legs, 4)
    routes = router.plan({0: 0.0}, {3: 0.0}, wanted=1)
    ok = check("route found", bool(routes))
    if not routes:
        return False
    route = routes[0]
    ok &= check("one change across the concurrency", route.changes == 1,
                "changes=%d roads=%s" % (route.changes, route.roads))
    ok &= check("distance counts the shared stretch once",
                abs(route.km - 250) < 1e-6, "km=%s" % route.km)
    return ok


def test_oneway_is_respected():
    """A leg that exists in one direction only must not be driven backwards."""
    legs = [FakeLeg("E10", 0, 1, 100)]           # forward only
    legs += both_ways("E10", 1, 2, 100)

    router = routing.Router(legs, 3)
    ok = check("forward journey works", bool(router.plan({0: 0.0}, {2: 0.0}, 1)))
    ok &= check("reverse journey is refused",
                router.plan({2: 0.0}, {0: 0.0}, 1) == [])
    return ok


def test_road_hops():
    legs = []
    legs += both_ways("E10", 0, 1, 10)
    legs += both_ways("E20", 1, 2, 10)
    legs += both_ways("E30", 2, 3, 10)
    adjacency = routing.road_adjacency(legs)
    hops = routing.road_hops(adjacency, "E10")
    ok = check("E10 reaches E20 in one hop", hops.get("E20") == 1, str(hops))
    ok &= check("E10 reaches E30 in two hops", hops.get("E30") == 2, str(hops))
    return ok


def test_unreachable():
    legs = both_ways("E10", 0, 1, 10) + both_ways("E20", 5, 6, 10)
    router = routing.Router(legs, 7)
    return check("disconnected cities yield no route",
                 router.plan({0: 0.0}, {6: 0.0}, 3) == [])


@dataclass
class FakeCity:
    id: str
    name: str
    country: str
    lat: float
    lon: float
    population: int = 100000

    @property
    def name_en(self):
        return self.name

    @property
    def aliases(self):
        return []

    @property
    def label(self):
        return "%s, %s" % (self.name, self.country)


def test_chain_resolves_ambiguous_names():
    """The two Brests must be told apart by the shape of the road, not by luck.

    E30 runs Warszawa - Brest - Minsk.  Both Brests match by name, but only the
    Belarusian one keeps the chain short; choosing the French one would mean
    crossing Europe and coming back.
    """
    import cities as cities_module
    import coverage as coverage_module

    catalogue = {
        c.id: c for c in [
            FakeCity("n1", "Warszawa", "PL", 52.23, 21.01),
            FakeCity("n2", "Brest", "BY", 52.10, 23.73),
            FakeCity("n3", "Brest", "FR", 48.39, -4.49),
            FakeCity("n4", "Minsk", "BY", 53.90, 27.57),
        ]
    }
    index = cities_module.CityIndex(catalogue)

    points = coverage_module.resolve_chain(["Warszawa", "Brest", "Minsk"], index)
    picked = {p.name: p.city_id for p in points}
    ok = check("both Brests are found as candidates",
               [p for p in points if p.name == "Brest"][0].ambiguous == 2)
    ok &= check("Brest on a Warszawa-Minsk road resolves to Belarus",
                picked.get("Brest") == "n2",
                "picked %s" % picked.get("Brest"))

    # The reverse case: a road that really does go to Brittany.
    catalogue["n5"] = FakeCity("n5", "Rennes", "FR", 48.11, -1.68)
    catalogue["n6"] = FakeCity("n6", "Paris", "FR", 48.86, 2.35)
    index = cities_module.CityIndex(catalogue)
    points = coverage_module.resolve_chain(["Paris", "Rennes", "Brest"], index)
    picked = {p.name: p.city_id for p in points}
    ok &= check("Brest on a Paris-Rennes road resolves to France",
                picked.get("Brest") == "n3", "picked %s" % picked.get("Brest"))
    return ok


def test_chain_survives_unknown_names():
    """One unrecognised spelling must thin the chain, not break it."""
    import cities as cities_module
    import coverage as coverage_module

    catalogue = {
        c.id: c for c in [
            FakeCity("n1", "Praha", "CZ", 50.08, 14.44),
            FakeCity("n2", "Brno", "CZ", 49.20, 16.61),
        ]
    }
    index = cities_module.CityIndex(catalogue)
    points = coverage_module.resolve_chain(
        ["Praha", "Nowhereville", "Brno"], index)
    ok = check("every control point is still reported", len(points) == 3)
    ok &= check("the unknown one is flagged rather than dropped",
                points[1].city_id is None and points[1].ambiguous == 0)
    ok &= check("the known ones still resolve",
                (points[0].city_id, points[2].city_id) == ("n1", "n2"),
                "%s" % [p.city_id for p in points])
    return ok


def main() -> int:
    tests = [
        ("canonical ids", test_canonical_ids),
        ("ambiguous control names", test_chain_resolves_ambiguous_names),
        ("unknown control names", test_chain_survives_unknown_names),
        ("prefers fewer changes", test_prefers_fewer_changes),
        ("changes counted per road", test_change_is_counted_once_per_road),
        ("concurrency", test_concurrency_costs_one_change),
        ("one-way legs", test_oneway_is_respected),
        ("road hop counts", test_road_hops),
        ("unreachable pairs", test_unreachable),
    ]
    failures = 0
    for name, function in tests:
        print("\n--- %s" % name)
        if not function():
            failures += 1
    print("\n%d of %d test groups failed" % (failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

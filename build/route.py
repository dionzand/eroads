"""Planning journeys that change E-road as little as possible.

The routing state is a *pair*: which interchange you are at, and which E-road
you are currently following.  That is the whole trick.  A plain shortest-path
over interchanges cannot express "stay on E35" because staying on a road is a
property of the edges you chain together, not of any single edge; making the
current road part of the state turns "changing road" into an explicit,
countable transition that can be priced.

Three routes come back:

*   the first minimises changes, and only then distance - this is what the tool
    is for, and it is found with a lexicographic ``(changes, km)`` cost so the
    answer is exact rather than a guess at a penalty weight;
*   the other two minimise distance under a finite per-change penalty, so they
    may use more roads but get there sooner.

Concurrency needs no special case.  Where two E-roads share pavement, both have
a leg over it, so switching between them happens at the interchange where the
concurrency begins - which is precisely where a driver would see both numbers
on the sign.
"""

from __future__ import annotations

import collections
import heapq
import math
from dataclasses import dataclass, field

# Penalties in kilometres per road change, used for the alternative routes.
# The first is roughly "worth an hour's detour to avoid a change"; the second
# is mild, and lets genuinely shorter routes through.
ALTERNATIVE_PENALTIES = (60.0, 15.0)

# Two routes sharing this much of their leg sequence are the same route.
DUPLICATE_OVERLAP = 0.80

# Access distance is measured as a straight line to the city, over roads that
# are not in this graph and are not motorways; a kilometre of it is worth more
# than a kilometre on an E-road.  Without this a route will happily stop far
# short of its destination whenever the road there was a little longer.
ACCESS_WEIGHT = 1.8


@dataclass
class Step:
    """One continuous run on a single E-road."""
    road: str
    from_node: int
    to_node: int
    km: float
    legs: list[int]
    national: list[tuple[str, float]] = field(default_factory=list)
    ferry: bool = False


@dataclass
class Route:
    steps: list[Step]
    km: float
    changes: int
    access_km: float = 0.0
    optimised_for: str = ""

    @property
    def roads(self) -> list[str]:
        return [step.road for step in self.steps]


class Router:
    """Routes over the interchange graph, counting road changes explicitly."""

    def __init__(self, legs: list, interchange_count: int):
        # node -> road -> list of (target, km, leg index)
        self.out: dict[int, dict[str, list[tuple[int, float, int]]]] = \
            collections.defaultdict(lambda: collections.defaultdict(list))
        self.roads_at: dict[int, set[str]] = collections.defaultdict(set)
        self.legs = legs
        for index, leg in enumerate(legs):
            self.out[leg.start][leg.road].append((leg.end, leg.km, index))
            self.roads_at[leg.start].add(leg.road)
            self.roads_at[leg.end].add(leg.road)
        self.interchange_count = interchange_count

    # -- the search ---------------------------------------------------------

    # A virtual node every destination access point leads to, so that the walk
    # from the last interchange into the city is priced like any other leg.
    DESTINATION = -1

    def _search(self, starts: dict[int, float], goals: dict[int, float],
                penalty: float | None) -> Route | None:
        """Dijkstra over (node, current road).

        ``penalty`` is the cost in kilometres of one road change; ``None`` means
        minimise changes first and distance second, which is done by ordering on
        the tuple rather than by picking a very large penalty (a large penalty
        works until two routes differ by more kilometres than the penalty, and
        then quietly stops being lexicographic).

        Both ends of the journey carry an access distance - a city centre is not
        on the motorway - and both must be priced.  Charging only the origin let
        the search finish at whichever access point was cheapest *to reach*,
        which for Ulm meant stopping 35 km away when an interchange 12 km out
        was available: the road distance was shorter, and the walk from there
        cost nothing.  Routing to a virtual node behind every access point
        prices the last few kilometres like any other.
        """
        def key(changes: int, km: float):
            return (changes, km) if penalty is None else (km + penalty * changes,)

        best: dict[tuple[int, str], tuple] = {}
        previous: dict[tuple[int, str], tuple] = {}
        queue: list[tuple] = []

        for node, access in starts.items():
            for road in self.roads_at.get(node, ()):
                state = (node, road)
                cost = key(0, access)
                if cost < best.get(state, (math.inf,)):
                    best[state] = cost
                    previous[state] = None
                    heapq.heappush(queue, (cost, 0, access, node, road))

        goal_state = None
        while queue:
            cost, changes, km, node, road = heapq.heappop(queue)
            state = (node, road)
            if cost > best.get(state, (math.inf,)):
                continue
            if node == self.DESTINATION:
                goal_state = state
                break

            access = goals.get(node)
            if access is not None:
                arrival = (self.DESTINATION, road)
                candidate = key(changes, km + access * ACCESS_WEIGHT)
                if candidate < best.get(arrival, (math.inf,)):
                    best[arrival] = candidate
                    previous[arrival] = (state, None)
                    heapq.heappush(queue,
                                   (candidate, changes, km + access * ACCESS_WEIGHT,
                                    self.DESTINATION, road))

            for target, leg_km, leg_index in self.out.get(node, {}).get(road, ()):
                nxt = (target, road)
                candidate = key(changes, km + leg_km)
                if candidate < best.get(nxt, (math.inf,)):
                    best[nxt] = candidate
                    previous[nxt] = (state, leg_index)
                    heapq.heappush(queue, (candidate, changes, km + leg_km, target, road))

            for other in self.roads_at.get(node, ()):
                if other == road:
                    continue
                nxt = (node, other)
                candidate = key(changes + 1, km)
                if candidate < best.get(nxt, (math.inf,)):
                    best[nxt] = candidate
                    previous[nxt] = (state, None)
                    heapq.heappush(queue, (candidate, changes + 1, km, node, other))

        if goal_state is None:
            return None
        return self._rebuild(goal_state, previous)

    def _rebuild(self, goal: tuple[int, str], previous: dict) -> Route:
        trail: list[tuple[tuple[int, str], int | None]] = []
        state = goal
        while previous.get(state) is not None:
            parent, leg_index = previous[state]
            trail.append((state, leg_index))
            state = parent
        trail.reverse()

        steps: list[Step] = []
        for (node, road), leg_index in trail:
            if leg_index is None:
                continue  # a road change; it adds no distance of its own
            leg = self.legs[leg_index]
            if steps and steps[-1].road == road and steps[-1].to_node == leg.start:
                step = steps[-1]
                step.to_node = leg.end
                step.km += leg.km
                step.legs.append(leg_index)
                step.ferry = step.ferry or leg.ferry
                _extend_national(step.national, leg.national)
            else:
                steps.append(Step(road=road, from_node=leg.start, to_node=leg.end,
                                  km=leg.km, legs=[leg_index],
                                  national=list(leg.national), ferry=leg.ferry))

        return Route(steps=steps,
                     km=round(sum(s.km for s in steps), 1),
                     changes=max(len(steps) - 1, 0))

    # -- the public entry point --------------------------------------------

    def plan(self, starts: dict[int, float], goals: dict[int, float],
             wanted: int = 3) -> list[Route]:
        """Up to ``wanted`` routes, fewest-changes first, then shorter ones."""
        routes: list[Route] = []

        fewest = self._search(starts, goals, penalty=None)
        if fewest is not None:
            fewest.optimised_for = "fewest road changes"
            routes.append(fewest)

        for penalty in ALTERNATIVE_PENALTIES:
            if len(routes) >= wanted:
                break
            candidate = self._search(starts, goals, penalty=penalty)
            if candidate is None:
                continue
            if any(_same_route(candidate, existing) for existing in routes):
                continue
            candidate.optimised_for = "shortest (%g km penalty per change)" % penalty
            routes.append(candidate)
        return routes


def _extend_national(target: list, incoming: list) -> None:
    for label, km in incoming:
        if target and target[-1][0] == label:
            target[-1] = (label, round(target[-1][1] + km, 2))
        else:
            target.append((label, km))


def _same_route(a: Route, b: Route) -> bool:
    left = {leg for step in a.steps for leg in step.legs}
    right = {leg for step in b.steps for leg in step.legs}
    if not left or not right:
        return left == right
    overlap = len(left & right) / len(left | right)
    return overlap >= DUPLICATE_OVERLAP


def road_adjacency(legs: list) -> dict[str, set[str]]:
    """Which E-roads can be changed between, ignoring distance entirely.

    This tiny graph - a couple of hundred nodes - is what makes the "at most
    three changes" claim checkable.  The minimum number of changes between two
    cities is the hop count between the roads that serve them, so the whole
    all-pairs question collapses into one breadth-first search per road instead
    of a search per city pair.
    """
    roads_at: dict[int, set[str]] = collections.defaultdict(set)
    for leg in legs:
        roads_at[leg.start].add(leg.road)
        roads_at[leg.end].add(leg.road)

    adjacency: dict[str, set[str]] = collections.defaultdict(set)
    for roads in roads_at.values():
        for road in roads:
            adjacency[road] |= roads - {road}
    return adjacency


def road_hops(adjacency: dict[str, set[str]], source: str) -> dict[str, int]:
    """Breadth-first hop count from one road to every road it can reach."""
    seen = {source: 0}
    queue = collections.deque([source])
    while queue:
        road = queue.popleft()
        for neighbour in adjacency.get(road, ()):
            if neighbour not in seen:
                seen[neighbour] = seen[road] + 1
                queue.append(neighbour)
    return seen

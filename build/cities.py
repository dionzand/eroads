"""European cities: fetching them, giving them stable identity, naming places.

The identity problem is the whole point of this module.  "Brest" is a city in
France and a city in Belarus; "Frankfurt", "Newport", "Valencia", "Tripoli" and
"Bologna" all collide somewhere in Europe or its spelling variants.  A route
planner keyed on names silently plans the wrong journey.  So every city here is
keyed by its **OSM node id** and always displayed with its country, and nothing
downstream is ever allowed to look a city up by name alone.

Population comes from OSM where tagged.  Where it is missing on a city the AGR
names as a control point, the city is kept anyway: being an AGR control point is
itself the qualification.
"""

from __future__ import annotations

import collections
import math
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

import osm

ROOT = Path(__file__).resolve().parent.parent
CITIES_PATH = ROOT / "cache" / "cities.json"

# Place ranks worth offering as an origin or destination.
PLACE_RANKS = ("city", "town")

# Node queries are cheap but not free: a 20x10 degree cell timed out repeatedly,
# so keep the cells modest even though that means more of them.
CITY_TILE_LON, CITY_TILE_LAT = 10.0, 5.0

MIN_POPULATION = 100_000


def _population(tags: dict) -> int | None:
    raw = tags.get("population")
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw.split(".")[0])
    return int(digits) if digits else None


def fold(name: str) -> str:
    """Fold a place name for comparison: no accents, no punctuation, lowercase.

    The AGR text, OSM and Wikidata disagree constantly about diacritics and
    hyphenation - Malmoe/Malmö, Bacau/Bacău, s-Hertogenbosch - and this makes
    those spellings comparable without pretending they are identical.
    """
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = stripped.replace("ß", "ss").replace("Ø", "O").replace("ø", "o")
    stripped = stripped.replace("Đ", "D").replace("đ", "d").replace("Ł", "L").replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


@dataclass
class City:
    id: str            # "n<osm node id>" - never the name
    name: str          # local name, as OSM has it
    name_en: str       # English exonym where one exists, else the local name
    country: str       # ISO 3166-1 alpha-2
    lat: float
    lon: float
    place: str
    population: int | None = None
    aliases: list[str] = field(default_factory=list)
    agr: bool = False          # named as a control point in AGR Annex I
    agr_roads: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "%s, %s" % (self.name_en or self.name, self.country)


def city_tiles() -> list[tuple[float, float, float, float]]:
    import fetch
    return osm.bbox_tiles(*fetch.FETCH_BBOX, CITY_TILE_LON, CITY_TILE_LAT)


def _city_key(tile) -> str:
    return "cities_%.2f_%.2f_%.2f_%.2f" % tile


def fetch_cities() -> list:
    """Fetch settlement nodes tile by tile, reporting rather than dying on failure.

    A cell that will not answer costs the cities in it, not the whole dataset,
    and the failure is returned so the build report can say which corner of the
    map is thin rather than leaving it to be discovered later.
    """
    tiles = city_tiles()
    failed = []
    print("cities: %d tiles" % len(tiles), file=sys.stderr)
    for index, tile in enumerate(tiles, 1):
        key = _city_key(tile)
        if osm.cached(key):
            continue
        south, west, north, east = tile
        print("[cities %2d/%2d] lat %.0f..%.0f lon %.0f..%.0f"
              % (index, len(tiles), south, north, west, east), file=sys.stderr)
        box = "%.4f,%.4f,%.4f,%.4f" % tile
        # Two exact-value lookups rather than one regex: an exact tag match uses
        # the index, while ~"^(city|town)$" forces a scan and times out.
        try:
            osm.query(
                "[out:json][timeout:600];"
                "("
                'node["place"="city"]["name"](%s);'
                'node["place"="town"]["name"](%s);'
                ");"
                "out body;" % (box, box),
                key=key, attempts=5)
        except osm.OverpassError as error:
            print("  cities tile failed: %s" % error, file=sys.stderr)
            failed.append(tile)
    if failed:
        print("cities: %d tiles unavailable" % len(failed), file=sys.stderr)
    return failed


def load_cities(countries) -> dict[str, City]:
    """All fetched settlements, with country resolved and identity assigned."""
    cities: dict[str, City] = {}
    for tile in city_tiles():
        key = _city_key(tile)
        if not osm.cached(key):
            continue
        for element in osm.load_cached(key)["elements"]:
            if element["type"] != "node":
                continue
            tags = element.get("tags", {})
            name = tags.get("name")
            if not name:
                continue
            lat, lon = element["lat"], element["lon"]
            iso = countries.resolve(lat, lon)
            if iso is None:
                continue
            aliases = []
            for tag in ("name:en", "int_name", "alt_name", "official_name",
                        "name:de", "name:fr", "old_name"):
                value = tags.get(tag)
                if value:
                    aliases.extend(part.strip() for part in value.split(";"))
            city = City(
                id="n%d" % element["id"],
                name=name,
                name_en=tags.get("name:en") or tags.get("int_name") or name,
                country=iso,
                lat=lat, lon=lon,
                place=tags.get("place", ""),
                population=_population(tags),
                aliases=sorted({a for a in aliases if a and a != name}),
            )
            cities[city.id] = city
    return cities


def load_from_pbf(places: list, countries) -> dict[str, City]:
    """Settlements as scanned out of a PBF, with country resolved from geometry.

    Identity stays the OSM node id.  The country comes from a point-in-polygon
    test rather than from any tag, because that is the only thing that reliably
    separates Brest, France from Brest, Belarus.
    """
    cities: dict[str, City] = {}
    for record in places:
        (node_id, lat, lon, name, place, population,
         name_en, int_name, wikidata) = record
        if not name:
            continue
        iso = countries.resolve(lat, lon)
        if iso is None:
            continue
        aliases = {value for value in (name_en, int_name) if value and value != name}
        city = City(
            id="n%d" % node_id,
            name=name,
            name_en=name_en or int_name or name,
            country=iso,
            lat=lat, lon=lon,
            place=place,
            population=_population({"population": population}),
            aliases=sorted(aliases),
        )
        cities[city.id] = city
    return cities


class CityIndex:
    """Name lookup that always answers with candidates, never with one guess.

    Matching an AGR control name is done by name *and* position: the name is
    folded to make spellings comparable, and the caller supplies the road
    geometry so that "Brest" resolves to whichever Brest is actually on the
    road in question.
    """

    def __init__(self, cities: dict[str, City]):
        self.cities = cities
        self.by_name: dict[str, list[City]] = collections.defaultdict(list)
        for city in cities.values():
            for label in {city.name, city.name_en, *city.aliases}:
                self.by_name[fold(label)].append(city)

    def candidates(self, name: str) -> list[City]:
        folded = fold(name)
        found = list(self.by_name.get(folded, ()))
        if found:
            return found
        # AGR sometimes parenthesises or slashes alternatives:
        # "Aveiro (Albergaria)", "Stockholm/Kapellskar".
        for part in re.split(r"[/(]", name):
            part = part.strip(" )")
            if not part:
                continue
            found.extend(self.by_name.get(fold(part), ()))
        return found

    def collisions(self) -> dict[str, list[City]]:
        """Folded names shared by cities in more than one country."""
        out = {}
        for folded, group in self.by_name.items():
            countries = {c.country for c in group}
            if len(countries) > 1:
                out[folded] = group
        return out


def usable_interchanges(legs: list, count: int) -> set[int]:
    """Interchanges you can both arrive at and leave from.

    Legs are directed, so an interchange at the end of a one-way stub has legs
    coming in and none going out.  Attaching a city to one of those makes the
    city reachable but not departable: Hanover could be routed *to* and never
    *from*, because all four of its nearest interchanges were such stubs and its
    real E30/E45 junctions were crowded out of the list by distance.
    """
    outgoing: set[int] = set()
    incoming: set[int] = set()
    for leg in legs:
        outgoing.add(leg.start)
        incoming.add(leg.end)
    return outgoing & incoming


def attach(cities: dict[str, City], interchanges: list, usable: set[int] | None = None,
           max_km: float = 40.0, keep: int = 6) -> dict[str, list[tuple[int, float]]]:
    """Link each city to the interchanges a driver would actually use.

    A city centre is a poor place to start a journey on a trunk road - the
    nearest E-road interchange can easily be twenty kilometres away - so the
    access distance is recorded rather than pretended away, and several
    interchanges are kept so the router can pick whichever suits the direction
    of travel.

    Nearest is not the same as usable: a one-way dead end can be the closest
    thing to a city and still be no use for leaving it, so those are only ever
    used when a city has nothing better within reach.
    """
    buckets: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    cell = 0.5
    for index, interchange in enumerate(interchanges):
        buckets[(int(interchange.lat / cell), int(interchange.lon / cell))].append(index)

    span = int(max_km / 111.0 / cell) + 1
    access: dict[str, list[tuple[int, float]]] = {}
    for city in cities.values():
        base = (int(city.lat / cell), int(city.lon / cell))
        nearby: list[tuple[float, int]] = []
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                for index in buckets.get((base[0] + dy, base[1] + dx), ()):
                    interchange = interchanges[index]
                    km = _haversine(city.lat, city.lon, interchange.lat, interchange.lon)
                    if km <= max_km:
                        nearby.append((km, index))
        nearby.sort()
        if nearby:
            # Keep the nearest handful, but make sure at least a couple can be
            # both entered and left.  Requiring that of *every* access point
            # discarded the close ones and left Amsterdam joining the network at
            # De Meern, 35 km away near Utrecht, and Ulm at 35 km when an
            # interchange 12 km out existed.  Keeping both kinds lets the router
            # choose, now that it prices the distance from the road to the city.
            chosen = nearby[:keep]
            if usable is not None:
                have = sum(1 for _, index in chosen if index in usable)
                if have < 2:
                    for item in nearby[keep:]:
                        if item[1] in usable:
                            chosen.append(item)
                            have += 1
                            if have >= 2:
                                break
            access[city.id] = [(index, round(km, 2)) for km, index in chosen]
    return access


def label_interchanges(interchanges: list, cities: dict[str, City],
                       countries, max_km: float = 25.0) -> None:
    """Name each interchange after the nearest city, and record the country.

    The name is descriptive only - identity stays the synthetic ``jx`` id - so a
    junction near two same-named towns cannot be confused with either.  The
    distance is kept and shown, because "near Utrecht" reads very differently at
    3 km and at 22 km.
    """
    cell = 0.5
    buckets: dict[tuple[int, int], list[City]] = collections.defaultdict(list)
    for city in cities.values():
        buckets[(int(city.lat / cell), int(city.lon / cell))].append(city)

    span = int(max_km / 111.0 / cell) + 1
    for interchange in interchanges:
        base = (int(interchange.lat / cell), int(interchange.lon / cell))
        best: tuple[float, City] | None = None
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                for city in buckets.get((base[0] + dy, base[1] + dx), ()):
                    km = _haversine(interchange.lat, interchange.lon, city.lat, city.lon)
                    # Prefer a big city slightly further away over a hamlet next
                    # door: a junction is known by the place it serves.
                    weight = km / (1.0 + math.log10(max(city.population or 1000, 1000)) - 3.0)
                    if km <= max_km and (best is None or weight < best[0]):
                        best = (weight, city)
        interchange.country = countries.resolve(interchange.lat, interchange.lon)
        if best is not None:
            city = best[1]
            interchange.near_city = city.id
            interchange.near_city_km = round(
                _haversine(interchange.lat, interchange.lon, city.lat, city.lon), 1)
            interchange.label = city.name_en or city.name
        else:
            interchange.label = "%.3f, %.3f" % (interchange.lat, interchange.lon)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def save(cities: dict[str, City], path: Path = CITIES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(c) for c in cities.values()], ensure_ascii=False),
        encoding="utf-8")


if __name__ == "__main__":
    if "fetch" in sys.argv[1:] or not sys.argv[1:]:
        fetch_cities()
    import geo

    countries = geo.Countries()
    cities = load_cities(countries)
    print("loaded %d settlements" % len(cities))
    ranks = collections.Counter(c.place for c in cities.values())
    print("  by rank:", dict(ranks))
    with_pop = [c for c in cities.values() if c.population]
    print("  with population:", len(with_pop),
          " >=100k:", sum(1 for c in with_pop if c.population >= MIN_POPULATION))

    index = CityIndex(cities)
    for probe in ("Brest", "Frankfurt", "Newport", "Valencia"):
        found = index.candidates(probe)
        shown = sorted(found, key=lambda c: -(c.population or 0))[:5]
        print("  %-10s -> %s" % (probe, [
            "%s (%s, pop %s)" % (c.name, c.country, c.population) for c in shown]))
    print("  names colliding across countries:", len(index.collisions()))
    save(cities)

"""Country lookup and the minimal Europe basemap, both from Natural Earth.

Two jobs, one dataset:

*   **Which country is this point in?**  Needed to tell Brest, France from
    Brest, Belarus - the collision that makes city *names* useless as
    identifiers - and to label corridors and interchanges.  Uses the 10m
    admin-0 polygons, which are accurate to about a hundred metres, so only a
    town sitting directly astride a border could be misplaced.

*   **What does the map look like?**  A clipped, simplified 50m land outline,
    small enough to ship to the browser, with no labels, no roads and no tiles.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from shapely.geometry import shape, box, Point
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"

SOURCES = {
    "ne10": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
            "master/geojson/ne_10m_admin_0_countries.geojson",
    "ne50": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
            "master/geojson/ne_50m_admin_0_countries.geojson",
}

# The map window.  Wider than the routing extent so that clipped roads run to
# the edge of the frame rather than stopping in open space.
MAP_BBOX = (-26.0, 33.0, 53.0, 72.5)  # west, south, east, north

USER_AGENT = "eroad-corridor-planner/0.1"


def _download(name: str) -> Path:
    path = CACHE / (name + ".geojson")
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(SOURCES[name], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = response.read()
    path.write_bytes(payload)
    return path


def _iso_of(properties: dict) -> str:
    """Two-letter code, working around Natural Earth's "-99" placeholders."""
    for key in ("ISO_A2_EH", "ISO_A2", "WB_A2"):
        value = (properties.get(key) or "").strip()
        if value and value not in ("-99", "-9"):
            return value
    return (properties.get("ADM0_A3") or properties.get("NAME") or "??")[:3]


class Countries:
    """Point-in-polygon country lookup over the Natural Earth 10m boundaries."""

    def __init__(self, resolution: str = "ne10"):
        payload = json.loads(_download(resolution).read_text(encoding="utf-8"))
        window = box(MAP_BBOX[0], MAP_BBOX[1], MAP_BBOX[2], MAP_BBOX[3])

        self.geometries = []
        self.iso: list[str] = []
        self.names: list[str] = []
        for feature in payload["features"]:
            geometry = shape(feature["geometry"])
            if not geometry.intersects(window):
                continue
            self.geometries.append(geometry)
            self.iso.append(_iso_of(feature["properties"]))
            self.names.append(feature["properties"].get("NAME_EN")
                              or feature["properties"].get("NAME") or "?")
        self.tree = STRtree(self.geometries)

    def lookup(self, lat: float, lon: float) -> str | None:
        point = Point(lon, lat)
        for index in self.tree.query(point):
            if self.geometries[index].contains(point):
                return self.iso[index]
        return None

    def nearest(self, lat: float, lon: float) -> tuple[str, float] | None:
        """Country of the nearest landmass, with its distance in degrees."""
        point = Point(lon, lat)
        index = self.tree.nearest(point)
        if index is None:
            return None
        return self.iso[index], self.geometries[index].distance(point)

    def resolve(self, lat: float, lon: float, tolerance_km: float = 15.0) -> str | None:
        """Country of a point, tolerating coastal slop.

        Coastlines are generalised, so genuinely-onshore places can fall just
        outside a polygon - Nordkapp is the obvious case, and every ferry
        terminal is another.  Anything within the tolerance of land is treated
        as being in that country; anything further out really is at sea.
        """
        inside = self.lookup(lat, lon)
        if inside:
            return inside
        found = self.nearest(lat, lon)
        if found is None:
            return None
        iso, degrees = found
        return iso if degrees * 111.0 <= tolerance_km else None

    def name_of(self, iso: str) -> str:
        for code, name in zip(self.iso, self.names):
            if code == iso:
                return name
        return iso


def build_basemap(out_path: Path, simplify_degrees: float = 0.02) -> dict:
    """Write the clipped, simplified land outline the webapp draws."""
    payload = json.loads(_download("ne50").read_text(encoding="utf-8"))
    window = box(MAP_BBOX[0], MAP_BBOX[1], MAP_BBOX[2], MAP_BBOX[3])

    features = []
    for feature in payload["features"]:
        geometry = shape(feature["geometry"])
        if not geometry.intersects(window):
            continue
        clipped = geometry.intersection(window).simplify(simplify_degrees)
        if clipped.is_empty:
            continue
        features.append({
            "type": "Feature",
            "properties": {"iso": _iso_of(feature["properties"]),
                           "name": feature["properties"].get("NAME_EN")
                           or feature["properties"].get("NAME")},
            "geometry": _round_geometry(clipped.__geo_interface__, 3),
        })

    collection = {"type": "FeatureCollection", "bbox": list(MAP_BBOX),
                  "features": features}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(collection, separators=(",", ":")), encoding="utf-8")
    return collection


def _round_geometry(geometry: dict, digits: int) -> dict:
    def walk(value):
        if isinstance(value, (list, tuple)):
            if value and isinstance(value[0], (int, float)):
                return [round(float(v), digits) for v in value]
            return [walk(v) for v in value]
        return value

    return {"type": geometry["type"], "coordinates": walk(geometry["coordinates"])}


if __name__ == "__main__":
    countries = Countries()
    print("loaded %d country polygons in the map window" % len(countries.geometries))
    for name, lat, lon in [
        ("Brest FR", 48.390, -4.486),
        ("Brest BY", 52.098, 23.734),
        ("Utrecht", 52.091, 5.122),
        ("Nordkapp", 71.170, 25.784),
        ("Istanbul", 41.008, 28.978),
    ]:
        print("  %-10s -> %s" % (name, countries.resolve(lat, lon)))

    out = ROOT / "web" / "data" / "europe.geo.json"
    collection = build_basemap(out)
    print("basemap: %d features, %.2f MB -> %s"
          % (len(collection["features"]), out.stat().st_size / 1e6, out))

"""A patient, caching Overpass client.

Overpass is the only practical way to get E-road geometry, but the public
instances return 504s and 500s on perfectly valid queries whenever they are
busy - during development even ``relation(7884303); out tags;`` failed on the
first attempt and succeeded on the second.  So every request retries with
backoff across several mirrors, and every successful response is written to
``cache/raw`` so a full build is resumable and only ever paid for once.

``requests`` is deliberately not used: the miniconda install in this
environment fails certificate verification against every Overpass mirror,
while ``urllib`` (which uses the Windows trust store) works fine.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# overpass-api.de is the primary and the only public instance that reliably
# serves the whole planet.  The others are tried only after the primary has had
# several goes, and each is dropped for the rest of the run the first time it
# fails: during development both were down (one timing out, one serving 502s),
# and spending a third of every retry budget on dead hosts turned transient
# failures into long stalls.
PRIMARY = "https://overpass-api.de/api/interpreter"
STATUS_URL = "https://overpass-api.de/api/status"
FALLBACKS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

_dead_mirrors: set[str] = set()

# Attempts on the primary before trying a fallback at all.
PRIMARY_ATTEMPTS = 4

# Overpass asks for a contact in the User-Agent so it can reach you if a
# query misbehaves.  Set EROADS_CONTACT to your own address; the fallback
# is the repository, so no personal address is committed.
USER_AGENT = "eroad-corridor-planner/0.1 (%s)" % (
    os.environ.get("EROADS_CONTACT")
    or "https://github.com/dionzand/eroads")

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "raw"

# Overpass asks clients to pause between queries; this is the floor.
MIN_INTERVAL = 4.0

_last_request = 0.0


class OverpassError(RuntimeError):
    pass


def _sleep_until_allowed() -> None:
    global _last_request
    wait = MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80]
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return CACHE_DIR / f"{safe}.{digest}.json.gz"


def cached(key: str) -> bool:
    return _cache_path(key).exists()


def load_cached(key: str) -> dict:
    return load_path(_cache_path(key))


def load_path(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def cached_paths(prefix: str) -> list[Path]:
    """Every cached response whose key starts with ``prefix``.

    Discovering these from disk rather than recomputing the key list matters
    once tiles can subdivide: a tile that failed and was split into quarters
    leaves four files whose keys no caller knows how to regenerate.
    """
    if not CACHE_DIR.exists():
        return []
    return sorted(CACHE_DIR.glob(prefix + "*.json.gz"))


def _store(key: str, payload: dict) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    tmp.replace(path)


def query(sparql_free_query: str, key: str, *, attempts: int = 8,
          timeout: int = 900, refresh: bool = False) -> dict:
    """Run an Overpass QL query, caching the parsed result under ``key``."""
    if not refresh and cached(key):
        return load_cached(key)

    body = urllib.parse.urlencode({"data": sparql_free_query}).encode("utf-8")
    delay = 5.0
    last_error = "no attempt made"

    for attempt in range(attempts):
        alive = [m for m in FALLBACKS if m not in _dead_mirrors]
        if attempt < PRIMARY_ATTEMPTS or not alive:
            mirror = PRIMARY
        else:
            mirror = alive[(attempt - PRIMARY_ATTEMPTS) % len(alive)]
        _sleep_until_allowed()
        if mirror == PRIMARY and attempt:
            wait_for_slot()
        request = urllib.request.Request(
            mirror, data=body, headers={"User-Agent": USER_AGENT})
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
            if not text.lstrip().startswith("{"):
                # Overpass reports runtime errors as an HTML page with HTTP 200.
                last_error = "non-JSON response: " + _strip_html(text)[:200]
            else:
                payload = json.loads(text)
                _store(key, payload)
                _log("  overpass %s: %d elements, %.1f MB, %.1fs"
                     % (key, len(payload.get("elements", [])),
                        len(text) / 1e6, time.time() - started))
                return payload
        except urllib.error.HTTPError as error:
            detail = _strip_html(error.read().decode("utf-8", "replace"))
            last_error = "HTTP %d: %s" % (error.code, detail[:200])
        except Exception as error:  # timeouts, resets, truncated bodies
            last_error = "%s: %s" % (type(error).__name__, error)

        if mirror != PRIMARY:
            _dead_mirrors.add(mirror)
            _log("  overpass: dropping unhealthy mirror %s for this run" % mirror)

        _log("  overpass %s attempt %d/%d failed (%s) - retrying in %.0fs"
             % (key, attempt + 1, attempts, last_error, delay))
        time.sleep(delay)
        delay = min(delay * 1.7, 120.0)

    raise OverpassError("query %s failed after %d attempts; last error: %s"
                        % (key, attempts, last_error))


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def wait_for_slot(max_wait: float = 180.0) -> None:
    """Block until the primary instance has a query slot free.

    Overpass allows two concurrent queries per client and answers a third with
    HTTP 429.  Its status endpoint says both how many slots are free and, when
    none are, when the next one frees up - so waiting on that is far cheaper
    than backing off blindly and guessing.
    """
    try:
        request = urllib.request.Request(STATUS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.read().decode("utf-8", "replace")
    except Exception:
        return  # status unavailable is not a reason to stop working

    if re.search(r"(\d+) slots? available now", status):
        if int(re.search(r"(\d+) slots? available now", status).group(1)) > 0:
            return
    waits = [int(m) for m in re.findall(r"Slot available after:.*?in (\d+) seconds", status)]
    if waits:
        delay = min(min(waits) + 2, max_wait)
        _log("  overpass: no slot free, waiting %ds" % delay)
        time.sleep(max(delay, 1))


def bbox_tiles(west: float, south: float, east: float, north: float,
               step_lon: float, step_lat: float) -> list[tuple[float, float, float, float]]:
    """Split a lon/lat box into tiles, as (south, west, north, east) for Overpass."""
    tiles = []
    lat = south
    while lat < north:
        lon = west
        top = min(lat + step_lat, north)
        while lon < east:
            right = min(lon + step_lon, east)
            tiles.append((lat, lon, top, right))
            lon = right
        lat = top
    return tiles

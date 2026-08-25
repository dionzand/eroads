# E-Road Corridor Planner

Plans routes between large European cities using **only** the International
E-road network, preferring the fewest changes of E-road over the shortest
distance.

**Live: https://dionzand.github.io/eroads/**

## Running it

The app in `web/` is static and self-contained - no build step, no
dependencies, no server. To look at it locally:

```
python -m http.server -d web 8000
```

`web/data/*.json` is committed, so that is all you need. Rebuilding the data is
a separate, offline job that needs two inputs which are *not* in this
repository:

- an OpenStreetMap extract of Europe (`europe-latest.osm.pbf`, ~34 GB)
- the UNECE AGR consolidated text, ECE/TRANS/SC.1/2016/3/Rev.1

```
export EROADS_AGR_PDF=/path/to/ECE-TRANS-SC1-2016-03-Rev1e.pdf
PYTHONIOENCODING=utf-8 python build/run.py /path/to/europe-latest.osm.pbf
```

Requires Python 3.12 with `pyosmium`, `numpy`, `scipy`, `shapely`, `pyproj` and
`pymupdf`. The scan is cached in `cache/` and is resumable: the expensive pass
over the extract is paid once. A full build takes about half an hour, most of
it the way and node scans.

Note that `web/data` is a *snapshot* of one build. It does not update itself,
and OSM moves underneath it.

## How it is put together

**`build/agr.py` — the roster.** Annex I of the UNECE AGR agreement
(ECE/TRANS/SC.1/2016/3/Rev.1) is the authority on which E-roads exist, how they
are numbered, and the ordered list of control cities each must pass through.
It yields exactly 250 roads. Everything else is checked against it.

**`build/pbf.py` — the geometry.** Read straight from an OSM extract with
pyosmium rather than over Overpass, which could not carry a query this size.
Two sources are combined, because neither is complete on its own: relation
7884303 indexes the network, and a way-tag sweep catches what no relation
knows - Norway has no route relations at all, its E-roads carrying a plain
`ref=E 6`. A tag-derived number is only trusted if the road is a plausible
class and runs near the treaty's own control-city line, which is what keeps
Swedish county road `E 591` in Östergötland from being mistaken for the E591
in southern Russia.

**`build/bridge.py` — the gaps.** Where a road is real but its tagging stops,
the pieces are rejoined along actual trunk road, nearest pair first; and where
the treaty writes a sea link that no ferry carries the number of, a
car-carrying crossing that spans it is adopted. That is what puts the Channel
Tunnel on E15, without which Britain is reachable only via the Hook of
Holland.

**`build/graph.py` — the corridor graph.** Built from shared OSM *node ids*,
never by concatenating relation members. Concatenation is what produces the
"roundtrip" artefact, where a road appears to run out and back because the
member list walks up one carriageway and down the other.

**`build/junctions.py` — where roads meet.** A junction is a node where the
*set* of E-roads on the pavement changes. That one rule handles both hard cases:
E-roads that cross on a bridge without connecting produce no junction, and
E-roads that run concurrently for 300 km produce junctions only at the two ends.

**`build/route.py` — the search.** The state is `(interchange, current road)`,
which is what makes "staying on E35" expressible and "changing road" countable.
The primary route minimises `(changes, km)` lexicographically; two alternatives
minimise distance under a finite per-change penalty.

**`build/verify.py` — the report.** Writes `reports/build_report.md`, including
the check that every pair of selectable cities is reachable within three
changes, and naming any pair that is not.

## Things that bite

- Names are not identifiers. Brest is in France *and* Belarus. Every city is
  keyed by OSM node id and always displayed with its country.
- `E01`, `E-1`, `E 1` and `E1` are one road; `E001` is a different one.
  `agr.canonical_id` is the only place that decision is made.
- Fewest changes is not shortest. The first route is the one with the fewest
  changes of road and can be much longer than the alternatives offered beside
  it - Lisbon to Helsinki is 5 628 km with two changes, or 3 928 km with
  fourteen. That is the tool working as intended, not a fault.
- `E841` (Avellino-Salerno) has its full geometry but no routable leg: a 2.6 km
  stretch of it carries no E-number in OSM. It is reported in the build report
  rather than papered over.
- On Windows, set `PYTHONIOENCODING=utf-8` or printing a Norwegian place name
  will crash the build.

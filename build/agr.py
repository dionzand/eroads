"""Parse Annex I of the UNECE AGR agreement into the canonical E-road roster.

The AGR consolidated text (ECE/TRANS/SC.1/2016/3/Rev.1) is the authoritative
source for *which* E-roads exist, how they are numbered, and which control
cities each one runs through, in order.  Everything downstream keys off this:
OSM supplies geometry, but the AGR supplies identity and expected shape.

Annex I lives on pages 9-19 and is laid out as:

    E 06
    Trelleborg - Malmoe - ... - Olderfjord - Karasjok - Kirkenes

where "-" separates places joined by road and "..." marks a sea link.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import fitz  # pymupdf

# Annex I occupies these pages (1-based, as printed in the document).
ANNEX_I_PAGES = range(9, 20)

# Running header/footer noise to drop before parsing.
_HEADER = re.compile(r"ECE/TRANS/SC\.1/2016/3/Rev\.1")
_PAGENO = re.compile(r"^\s*\d{1,2}\s*$")

# Section headings mapped to (class, orientation); "_" leaves a field unchanged.
# The "(1)" / "(a)" markers sit on their own line in the extracted text, with the
# label on the next line, so match the labels rather than the numbering.
_SECTIONS = [
    (re.compile(r"^\s*(?:A\.\s*)?Main roads\s*$"), None),
    (re.compile(r"^\s*West-east orientation\s*$"), ("_", "west-east")),
    (re.compile(r"^\s*North-south orientation\s*$"), ("_", "north-south")),
    (re.compile(r"^\s*Reference roads\s*$"), ("A-reference", "_")),
    (re.compile(r"^\s*Intermediate roads\s*$"), ("A-intermediate", "_")),
    (re.compile(r"^\s*(?:B\.\s*)?Branch, link and connecting roads\s*$"), ("B", "n/a")),
    (re.compile(r"^\s*\([12ab]\)\s*$"), None),
]

# A road entry begins with a line that is *only* an E-number.
_ENTRY = re.compile(r"^\s*(E\s?\d{2,3})\s*:?\s*$")

# Separators between control points.  The document mixes hyphen-minus, en dash,
# three dots and a real ellipsis; "..." and the ellipsis mean a sea link.
_SEA = re.compile(r"\s(?:\.\.\.|\u2026)\s")
_ROAD = re.compile(r"\s[-\u2013\u2014]\s")

# Place names whose line wrapping puts a " - " where there is no separator.
# Keyed by the (left, right) fragment pair produced by the naive split.
_JOINS = {
    ("Vilar", "Formoso"): "Vilar Formoso",
    ("Bayer", "Eisenstein"): "Bayerisch Eisenstein",
}


def canonical_id(ref: str) -> str:
    """Canonical E-road id, immune to the E-01 / E 1 / E1 / E-1 / E001 mess.

    Digits are what matter.  Three digits are kept verbatim so that E001
    (a Central Asian class-B road) never collides with E01; one or two digits
    are zero-padded to two.

    >>> [canonical_id(s) for s in ("E-01", "E 1", "E1", "E01")]
    ['E01', 'E01', 'E01', 'E01']
    >>> canonical_id("E 001"), canonical_id("E574")
    ('E001', 'E574')
    """
    digits = re.sub(r"[^0-9]", "", ref)
    if not digits:
        raise ValueError("no digits in E-road ref " + repr(ref))
    if len(digits) >= 3:
        return "E" + digits
    return "E" + digits.zfill(2)


def display_name(road_id: str) -> str:
    """Human-facing name: E01 -> E1, E001 stays E001."""
    digits = road_id[1:]
    if len(digits) == 2:
        return "E" + str(int(digits))
    return road_id


@dataclass
class Road:
    id: str
    display: str
    cls: str
    orientation: str
    points: list[str] = field(default_factory=list)
    # links[i] describes the connection between points[i] and points[i+1],
    # and is either "road" or "sea".
    links: list[str] = field(default_factory=list)
    deleted: bool = False
    raw: str = ""


def _annex_text(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    out = []
    for page_no in ANNEX_I_PAGES:
        for line in doc[page_no - 1].get_text().splitlines():
            if _HEADER.search(line) or _PAGENO.match(line):
                continue
            out.append(line.rstrip())
    doc.close()
    return "\n".join(out)


def _split_points(body: str) -> tuple[list[str], list[str]]:
    """Split a control-point chain into places and the link type between them."""
    points: list[str] = []
    links: list[str] = []
    for chunk_index, sea_chunk in enumerate(_SEA.split(body)):
        parts = [p.strip(" \t.,") for p in _ROAD.split(sea_chunk)]
        parts = [p for p in parts if p]
        merged: list[str] = []
        for part in parts:
            if merged and (merged[-1], part) in _JOINS:
                merged[-1] = _JOINS[(merged[-1], part)]
            else:
                merged.append(part)
        if chunk_index > 0:
            links.append("sea")
        for i, name in enumerate(merged):
            if i > 0:
                links.append("road")
            points.append(name)
    return points, links


def parse(pdf_path: Path) -> list[Road]:
    lines = _annex_text(pdf_path).splitlines()

    cls = orientation = "?"
    roads: list[Road] = []
    current: Road | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current is None:
            return
        body = re.sub(r"\s+", " ", " ".join(b.strip() for b in buffer if b.strip())).strip()
        current.raw = body
        if "(deleted)" in body:
            current.deleted = True
        else:
            current.points, current.links = _split_points(body)
        roads.append(current)
        current, buffer = None, []

    for line in lines:
        section_hit = False
        for pattern, value in _SECTIONS:
            if pattern.match(line):
                if value is not None:
                    new_cls, new_orientation = value
                    if new_cls != "_":
                        cls = new_cls
                    if new_orientation != "_":
                        orientation = new_orientation
                section_hit = True
                break
        if section_hit:
            continue

        entry = _ENTRY.match(line)
        if entry:
            flush()
            road_id = canonical_id(entry.group(1))
            current = Road(road_id, display_name(road_id), cls, orientation)
        elif current is not None:
            buffer.append(line)
    flush()
    return roads


def dedupe(roads: list[Road]) -> tuple[list[Road], list[tuple[str, int, int]]]:
    """Collapse roads listed more than once in Annex I.

    The document really does list E95 twice: in full among the north-south
    reference roads (Sankt Peterburg ... Odessa ... Samsun - Merzifon) and again,
    abbreviated, among the intermediate roads.  Keep the longer chain, since a
    truncated one would make the road look like it stops at a border.
    """
    kept: dict[str, Road] = {}
    collisions: list[tuple[str, int, int]] = []
    for road in roads:
        previous = kept.get(road.id)
        if previous is None:
            kept[road.id] = road
            continue
        collisions.append((road.id, len(previous.points), len(road.points)))
        if len(road.points) > len(previous.points):
            kept[road.id] = road
    return list(kept.values()), collisions


def build(pdf_path: Path, out_path: Path) -> tuple[list[Road], list[tuple[str, int, int]]]:
    roads, collisions = dedupe(parse(pdf_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "UNECE AGR consolidated text ECE/TRANS/SC.1/2016/3/Rev.1, Annex I",
        "duplicate_listings": [
            {"id": rid, "points_kept": max(a, b), "points_discarded": min(a, b)}
            for rid, a, b in collisions
        ],
        "roads": [asdict(r) for r in roads],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return roads, collisions


# The UNECE consolidated AGR text (ECE/TRANS/SC.1/2016/3/Rev.1).  Point
# EROADS_AGR_PDF at your copy; it is not redistributed here.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = Path(os.environ.get("EROADS_AGR_PDF")
                   or ROOT / "data" / "ECE-TRANS-SC1-2016-03-Rev1e.pdf")
ROSTER_PATH = Path(__file__).resolve().parent.parent / "cache" / "roster.json"


def load_roster(extra_ids: set[str] | None = None) -> dict[str, dict]:
    """The roster keyed by canonical id, optionally extended with OSM-only roads.

    The AGR text is from 2016 and OSM carries a handful of roads that postdate
    it (E903 has a relation and a Wikipedia article but no Annex I entry).
    Those are kept, flagged ``agr: false``, rather than silently dropped - the
    geometry is real even where the paperwork has not caught up.
    """
    payload = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    roster: dict[str, dict] = {}
    for road in payload["roads"]:
        road["agr"] = True
        roster[road["id"]] = road
    for road_id in sorted(extra_ids or ()):
        if road_id in roster:
            continue
        roster[road_id] = {
            "id": road_id, "display": display_name(road_id), "cls": "?",
            "orientation": "?", "points": [], "links": [], "deleted": False,
            "raw": "", "agr": False,
        }
    return roster


if __name__ == "__main__":
    import sys

    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    roads, collisions = build(pdf, ROSTER_PATH)
    deleted = [r for r in roads if r.deleted]
    print("parsed", len(roads), "roads (" + str(len(deleted)), "deleted) ->", ROSTER_PATH)
    for rid, a, b in collisions:
        print("  listed twice in Annex I: %s (%d vs %d points, kept longer)" % (rid, a, b))

    counts: dict[tuple[str, str], int] = {}
    for r in roads:
        counts[(r.cls, r.orientation)] = counts.get((r.cls, r.orientation), 0) + 1
    for key in sorted(counts):
        print("  %-16s %-12s %3d" % (key[0], key[1], counts[key]))
    print("  distinct control point names:", len({p for r in roads for p in r.points}))
    print("  total sea links:", sum(r.links.count("sea") for r in roads))

    for probe in ("E01", "E06", "E35", "E97", "E381", "E001"):
        r = next((x for x in roads if x.id == probe), None)
        if r is None:
            print("\n  " + probe + ": ABSENT")
            continue
        print("\n  %s [%s / %s] %d points, %d sea links%s"
              % (r.display, r.cls, r.orientation, len(r.points),
                 r.links.count("sea"), " DELETED" if r.deleted else ""))
        print("    " + " | ".join(r.points[:14]) + (" ..." if len(r.points) > 14 else ""))

"""Re-apply relation membership to an existing ways scan, without re-reading the PBF.

The relation scan originally parsed a ref like ``E 264;E 25;E 411`` by stripping
non-digits, yielding the single nonsense road "E26425411".  Worse were the short
ones: ``E 12;E 1`` became "E121" and ``E 1;E 05`` became "E105" - both of which
*are* real AGR roads, in Russia and the Caucasus.  So German ways quietly
acquired Central Asian road numbers, and E30 came out sharing interchanges with
E101, E105, E119, E121 and E123 without leaving Germany.

Re-scanning relations is cheap (minutes).  Re-scanning ways is not (two hours).
This bridges the two, using the fact that a way's stored road set is the union
of what the relations claimed and what its own tags said:

    tagged      = stored - membership_old      (what the tags contributed)
    corrected   = tagged + membership_new

Splitting one phantom road into the several real ones it was hiding only ever
adds roads back, so the reconstruction is exact.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import agr
import pbf


def repair(source: Path) -> dict:
    pbf.use_source(source)

    old = pbf._load("relations")
    ways = pbf._load("ways")
    if old is None or ways is None:
        raise SystemExit("nothing cached for %s" % source.name)

    print("[repair] re-scanning relations with the corrected ref parser",
          file=sys.stderr, flush=True)
    new = pbf.scan_relations(source, refresh=True)

    # The scan only ever wrote membership roads that were in the AGR roster, so
    # the reconstruction has to filter the same way.  Without this the phantom
    # ids that are *not* real roads (E26425411 and friends) look like roads the
    # way has lost, and 1937 ways get spuriously rewritten.
    roster = set(agr.load_roster())

    def invert(membership: dict) -> dict[int, set[str]]:
        out: dict[int, set[str]] = collections.defaultdict(set)
        for road, way_ids in membership.items():
            if road not in roster:
                continue
            for way_id in way_ids:
                out[way_id].add(road)
        return out

    before, after = invert(old), invert(new)
    phantom = sorted(set(old) - set(new))
    gained = sorted(set(new) - set(old))
    print("[repair] roads dropped as phantoms: %s" % (", ".join(phantom) or "none"),
          file=sys.stderr)
    print("[repair] roads recovered: %s" % (", ".join(gained) or "none"),
          file=sys.stderr)

    changed = 0
    for key, record in ways["roads"].items():
        way_id = int(key)
        stored = set(record[0])
        corrected = (stored - before.get(way_id, set())) | after.get(way_id, set())
        if corrected != stored:
            record[0] = sorted(corrected)
            changed += 1

    pbf._dump("ways", ways)
    print("[repair] %d of %d ways relabelled" % (changed, len(ways["roads"])),
          file=sys.stderr)
    return {"phantom": phantom, "gained": gained, "changed": changed}


if __name__ == "__main__":
    repair(Path(sys.argv[1]) if len(sys.argv) > 1 else pbf.DEFAULT_PBF)

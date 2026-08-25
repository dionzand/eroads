# E-road network build report

Generated 2026-08-25 23:55.

## 1. Roster

- AGR Annex I roads parsed: **253** (252 live, 1 marked deleted)
- Roads present in OSM but not in the 2016 AGR text: E02, E327, E903
- Roads with routable geometry: **225 of 252**
- **No way in the extract claims these:** E003, E004, E005, E006, E007, E008, E009, E010, E011, E012, E013, E014, E015, E016, E017, E018, E019, E02, E121, E123, E125, E327, E591, E903
  Nothing there carries the E-number in `ref`/`int_ref` and no route
  relation lists it. The Central Asian tails (E002-E019, E121-E127)
  are outside the mapped extent by design; the rest are OSM tagging
  gaps. They are left absent rather than reconstructed from the
  treaty text, which would be inventing a road.
- **Present but not routable:** E002, E127, E841
  Geometry was found and measured, but no leg could be built between
  interchanges - usually carriageways that never meet - so the router
  cannot offer a journey along them.

## 2. Source data

| measure | count |
|---|---|
| crossings attached | 96 |
| e road ways | 461,326 |
| endpoints stitched | 53,642 |
| ferry landings joined | 114 |
| legs mirrored | 17,591 |
| nodes | 19,814,261 |
| nodes without coordinates | 0 |
| orphan fragments dropped | 46 |
| ramps kept | 185,329 |
| ramps without roads | 29,679 |
| road gaps closed | 60 |
| tag refs rejected by class | 790 |
| tag refs rejected by place | 1,602 |
| tagging gaps bridged | 150 |
| treaty crossings joined | 25 |

- Corridors after contraction: **325,693**
- Interchanges: **6,769**
- Legs (directed, interchange to interchange): **42,058**

## 3. Does every road reach the places the treaty says it must?

Annex I names the control cities each E-road has to pass through. This is the coverage test: a road that does not come within 30 km of one of its own control cities has a gap in its geometry.

- Control points in Annex I: **1,815**
- Matched to a real settlement: **1,310** (72.2%)
- Reached by their own road: **1,281** (97.8% of matched)

### Roads that miss control cities

| road | km of geometry | control points | unreached | which |
|---|---|---|---|---|
| E25 | 4,890 | 33 | 3 | Bastia, Porto Vecchio, Bonifacio |
| E49 | 1,278 | 11 | 2 | Halle, Schönberg |
| E71 | 2,335 | 10 | 2 | Bihać, Knin |
| E806 | 185 | 4 | 2 | Castelo Branco, Guarda |
| E83 | 251 | 5 | 2 | Jablanica, Sofia |
| E87 | 3,594 | 27 | 2 | Marinka, Havza |
| E018 | 0 | 4 | 1 | Uspenka |
| E11 | 1,249 | 4 | 1 | Montpellier |
| E15 | 8,879 | 22 | 1 | Newcastle |
| E18 | 4,224 | 17 | 1 | Newcastle |
| E36 | 441 | 4 | 1 | Legnica |
| E40 | 7,205 | 54 | 1 | Rostov-ná-Donu |
| E462 | 656 | 4 | 1 | Kraków |
| E47 | 760 | 8 | 1 | Farø |
| E55 | 6,815 | 40 | 1 | Farø |
| E59 | 1,145 | 7 | 1 | Praha |
| E591 | 0 | 2 | 1 | Novorossiysk |
| E772 | 323 | 3 | 1 | Jablanica |
| E80 | 12,930 | 54 | 1 | Izmir |
| E881 | 1,303 | 6 | 1 | Izmit |
| E90 | 11,283 | 43 | 1 | Aksaray |
| E902 | 449 | 3 | 1 | Málaga |

26 gap areas were swept by tag to look for untagged or unrelated geometry.

## 4. Repairs and rejections

- Loops back to the same interchange, discarded as not being journeys: **0** (these are normal - a path round an interchange's own ramps)
- Legs rejected as doubling back, meaning two interchanges that should have been merged into one: **735**

| road | km | from jx | to jx |
|---|---|---|---|
| E67 | 315 | 4005 | 3989 |
| E05 | 298 | 407 | 285 |
| E90 | 285 | 3381 | 3342 |
| E05 | 285 | 407 | 1083 |
| E05 | 282 | 407 | 661 |
| E70 | 282 | 407 | 661 |
| E01 | 282 | 117 | 118 |
| E01 | 281 | 117 | 118 |
| E05 | 273 | 407 | 1107 |
| E05 | 269 | 407 | 1083 |
| E70 | 269 | 407 | 1083 |
| E05 | 257 | 407 | 1107 |
| E70 | 257 | 407 | 1107 |
| E105 | 244 | 6687 | 4984 |
| E77 | 224 | 4891 | 4890 |

- Interchange cluster radius: median 1.40 km, 95th 2.50 km, max 4.12 km
- Interchanges with no city within 25 km (shown by coordinate): **138**

## 5. Can every pair of cities be linked with at most 3 changes?

- City pairs examined: **788,140**
- Cities with no road at all: 1
- Pairs with no E-road connection whatsoever: **0**

| road changes | pairs | share |
|---|---|---|
| 0 | 46,029 | 5.84% |
| 1 | 345,663 | 43.86% |
| 2 | 325,150 | 41.26% |
| 3 | 67,256 | 8.53% |
| 4 | 3,921 | 0.50% |
| 5 | 120 | 0.02% |
| 6 | 1 | 0.00% |

**4,042 pairs need more than 3 changes** (0.51% of all pairs).

Examples:

| from | to | changes |
|---|---|---|
| Galați, RO | Jõhvi, EE | 4 |
| Leicester, GB | Toplița, RO | 4 |
| Leicester, GB | Gheorgheni, RO | 4 |
| Leicester, GB | Miercurea Ciuc, RO | 4 |
| Leicester, GB | Pula, HR | 4 |
| Leicester, GB | Banja Luka, BA | 4 |
| Leicester, GB | Barcs, HU | 4 |
| Leicester, GB | Jõhvi, EE | 4 |
| Leicester, GB | Virovitica, HR | 4 |
| Eregli, TR | Tilburg, NL | 4 |
| Eregli, TR | Toplița, RO | 4 |
| Eregli, TR | Gheorgheni, RO | 4 |
| Eregli, TR | Miercurea Ciuc, RO | 4 |
| Eregli, TR | Posof, TR | 4 |
| Eregli, TR | Hanau, DE | 4 |
| Eregli, TR | Cloppenburg, DE | 4 |
| Eregli, TR | Pula, HR | 4 |
| Eregli, TR | Kerch, RU | 4 |
| Eregli, TR | Yalta, RU | 4 |
| Eregli, TR | Bern, CH | 4 |

## 6. Cities whose names are not unique

Every city is keyed by its OSM node id and shown with its country, because these names cannot identify a place on their own.

- Name collisions among selectable cities: **2**

| name | distinct places |
|---|---|
| Αθήνα | GR (n441183), CY (n9331795), UA (n26150422), UA (n26150436), UA (n26150437), UA (n26150791) |
| Брэст | BY (n27171628), FR (n823582966) |

## 7. Output

| file | size |
|---|---|
| cities.json | 0.24 MB |
| network.json | 14.03 MB |


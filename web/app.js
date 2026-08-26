/*
 * E-Road Corridor Planner - the whole client.
 *
 * Deliberately dependency-free, including the map.  The default basemap is a
 * bundled country outline drawn as SVG; ticking "Detailed basemap" lays raster
 * tiles into the same <g> behind it.  Both use Web Mercator, so they share one
 * pan/zoom transform and cannot drift apart - see the note above mercator().
 *
 * The whole road network is a single <path> with many subpaths: twenty thousand
 * separate elements would make panning crawl.
 *
 * The router mirrors build/route.py exactly; see that file for why the search
 * state is (interchange, current road) rather than just an interchange.
 */

const NETWORK_URL = "data/network.json";
const CITIES_URL = "data/cities.json";
const BASEMAP_URL = "data/europe.geo.json";

/* The geometry is simplified for display, so past this the map would start
   showing its own approximations rather than the road. */
const MAX_ZOOM = 11;

const ALTERNATIVE_PENALTIES = [60, 15];
const DUPLICATE_OVERLAP = 0.8;

/* See ACCESS_WEIGHT in build/route.py. */
const ACCESS_WEIGHT = 1.8;

/* Squared degrees; about 1.5 km. Below this a seam between two legs is just
   rounding and needs no help. */
const SEAM_TOLERANCE = 0.00018;

/* ------------------------------------------------------------------ *
 * Projection
 * ------------------------------------------------------------------ */

const D2R = Math.PI / 180;

/*
 * Web Mercator, in unit world coordinates: (0,0) at the north-west corner of
 * the world, (1,1) at the south-east.
 *
 * A Lambert conic is the nicer projection for a map of Europe and was what this
 * used at first, but raster tiles are published in Mercator and cannot be
 * reprojected client-side.  Sharing one projection between the vector outline
 * and the optional tile layer means both are drawn by the same code, under the
 * same pan/zoom transform, and can never drift apart.  The price is the usual
 * Mercator exaggeration of Scandinavia.
 */
function mercator(lat, lon) {
  const s = Math.sin(Math.max(-85, Math.min(85, lat)) * D2R);
  return [(lon + 180) / 360, 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)];
}

/* Fitted to the viewport once the basemap bbox is known. */
let fit = { scale: 1, dx: 0, dy: 0 };

function project(lat, lon) {
  const [x, y] = mercator(lat, lon);
  return [x * fit.scale + fit.dx, y * fit.scale + fit.dy];
}

/* World units -> the same untransformed space project() writes into, so the
   tile layer can be positioned with the road geometry without extra maths. */
function projectWorld(x, y) {
  return [x * fit.scale + fit.dx, y * fit.scale + fit.dy];
}

function fitToBox(bbox, width, height, pad) {
  const [w, s, e, n] = bbox;
  const [minX, minY] = mercator(n, w);   // north-west corner
  const [maxX, maxY] = mercator(s, e);   // south-east corner
  const scale = Math.min((width - pad * 2) / (maxX - minX),
                         (height - pad * 2) / (maxY - minY));
  fit = {
    scale,
    dx: pad - minX * scale + (width - pad * 2 - (maxX - minX) * scale) / 2,
    dy: pad - minY * scale + (height - pad * 2 - (maxY - minY) * scale) / 2,
  };
}

/* ------------------------------------------------------------------ *
 * Geometry decoding
 * ------------------------------------------------------------------ */

/* Legs ship as delta-encoded ten-thousandths of a degree; see build/export.py. */
function decodeLine(encoded) {
  const points = [];
  let lat = 0, lon = 0;
  for (let i = 0; i < encoded.length; i += 2) {
    lat += encoded[i];
    lon += encoded[i + 1];
    points.push([lat / 10000, lon / 10000]);
  }
  return points;
}

/* A corridor shorter than this contributes no visible line at any zoom the map
   allows - 300 m is about a pixel at maximum zoom, and anything shorter
   is rounded away to a single point by the path encoder anyway - but it still draws two
   round line caps, and a zero-length one draws a filled dot.  Six thousand of
   the drawn runs have both ends at the same point and another three thousand
   are under 50 m, mostly stubs between the junction vertices of one
   interchange.  Zoomed out they hide under real road; zoomed in they separate
   into a scatter of blobs, thickest over exactly the junctions a reader is
   trying to look at. */
const MIN_DRAW_METRES = 300;

function longEnoughToDraw(points) {
  if (points.length < 2) return false;
  let metres = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const [lat1, lon1] = points[i], [lat2, lon2] = points[i + 1];
    const dy = (lat2 - lat1) * 111320;
    const dx = (lon2 - lon1) * 111320 * Math.cos(lat1 * D2R);
    metres += Math.hypot(dx, dy);
    if (metres >= MIN_DRAW_METRES) return true;
  }
  return false;
}

function pathOf(points) {
  let d = "";
  for (let i = 0; i < points.length; i++) {
    const [x, y] = project(points[i][0], points[i][1]);
    d += (i === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
  }
  return d;
}

/* ------------------------------------------------------------------ *
 * Router - mirrors build/route.py
 * ------------------------------------------------------------------ */

class Router {
  constructor(legs) {
    this.legs = legs;
    this.out = new Map();      // node -> Map(road -> [[target, km, legIndex]])
    this.roadsAt = new Map();  // node -> Set(road)
    legs.forEach((leg, index) => {
      if (!this.out.has(leg.a)) this.out.set(leg.a, new Map());
      const byRoad = this.out.get(leg.a);
      if (!byRoad.has(leg.r)) byRoad.set(leg.r, []);
      byRoad.get(leg.r).push([leg.b, leg.km, index]);
      for (const node of [leg.a, leg.b]) {
        if (!this.roadsAt.has(node)) this.roadsAt.set(node, new Set());
        this.roadsAt.get(node).add(leg.r);
      }
    });
  }

  /* penalty === null means lexicographic (changes, km).
     `goals` maps an access node to the distance from it into the destination
     city; that distance is priced through a virtual node, so the search cannot
     finish at a far access point just because the road to it was shorter. */
  search(starts, goals, penalty) {
    const DESTINATION = -1;
    const cost = (changes, km) =>
      penalty === null ? changes * 1e9 + km : km + penalty * changes;

    const best = new Map();
    const prev = new Map();
    const heap = new Heap();

    for (const [node, access] of starts) {
      for (const road of this.roadsAt.get(node) || []) {
        const state = node + "|" + road;
        const c = cost(0, access);
        if (c < (best.get(state) ?? Infinity)) {
          best.set(state, c);
          prev.set(state, null);
          heap.push([c, 0, access, node, road]);
        }
      }
    }

    let goal = null;
    while (heap.size()) {
      const [c, changes, km, node, road] = heap.pop();
      const state = node + "|" + road;
      if (c > (best.get(state) ?? Infinity)) continue;
      if (node === DESTINATION) { goal = state; break; }

      const access = goals.get(node);
      if (access !== undefined) {
        const arrival = DESTINATION + "|" + road;
        const c2 = cost(changes, km + access * ACCESS_WEIGHT);
        if (c2 < (best.get(arrival) ?? Infinity)) {
          best.set(arrival, c2);
          prev.set(arrival, [state, null]);
          heap.push([c2, changes, km + access * ACCESS_WEIGHT, DESTINATION, road]);
        }
      }

      const byRoad = this.out.get(node);
      const onward = byRoad ? byRoad.get(road) : null;
      if (onward) {
        for (const [target, legKm, legIndex] of onward) {
          const next = target + "|" + road;
          const c2 = cost(changes, km + legKm);
          if (c2 < (best.get(next) ?? Infinity)) {
            best.set(next, c2);
            prev.set(next, [state, legIndex]);
            heap.push([c2, changes, km + legKm, target, road]);
          }
        }
      }
      for (const other of this.roadsAt.get(node) || []) {
        if (other === road) continue;
        const next = node + "|" + other;
        const c2 = cost(changes + 1, km);
        if (c2 < (best.get(next) ?? Infinity)) {
          best.set(next, c2);
          prev.set(next, [state, null]);
          heap.push([c2, changes + 1, km, node, other]);
        }
      }
    }
    return goal === null ? null : this.rebuild(goal, prev);
  }

  rebuild(goal, prev) {
    const trail = [];
    let state = goal;
    while (prev.get(state)) {
      const [parent, legIndex] = prev.get(state);
      trail.push([state, legIndex]);
      state = parent;
    }
    trail.reverse();

    const steps = [];
    for (const [state, legIndex] of trail) {
      if (legIndex === null) continue;
      const road = state.slice(state.indexOf("|") + 1);
      const leg = this.legs[legIndex];
      const last = steps[steps.length - 1];
      if (last && last.road === road && last.to === leg.a) {
        last.to = leg.b;
        last.km += leg.km;
        last.legs.push(legIndex);
        last.ferry = last.ferry || !!leg.f;
        mergeNational(last.national, leg.nat);
      } else {
        steps.push({
          road, from: leg.a, to: leg.b, km: leg.km,
          legs: [legIndex], national: leg.nat.map((x) => x.slice()),
          ferry: !!leg.f,
        });
      }
    }
    const km = steps.reduce((sum, s) => sum + s.km, 0);
    return { steps, km, changes: Math.max(steps.length - 1, 0) };
  }

  plan(starts, goals, wanted = 3) {
    const routes = [];
    const fewest = this.search(starts, goals, null);
    if (fewest) {
      fewest.why = "fewest road changes";
      routes.push(fewest);
    }
    for (const penalty of ALTERNATIVE_PENALTIES) {
      if (routes.length >= wanted) break;
      const candidate = this.search(starts, goals, penalty);
      if (!candidate) continue;
      if (routes.some((r) => sameRoute(r, candidate))) continue;
      candidate.why = "shorter, more changes";
      routes.push(candidate);
    }
    return routes;
  }
}

function mergeNational(target, incoming) {
  for (const [label, km] of incoming) {
    const last = target[target.length - 1];
    if (last && last[0] === label) last[1] = Math.round((last[1] + km) * 10) / 10;
    else target.push([label, km]);
  }
}

function sameRoute(a, b) {
  const left = new Set(a.steps.flatMap((s) => s.legs));
  const right = new Set(b.steps.flatMap((s) => s.legs));
  let shared = 0;
  for (const x of left) if (right.has(x)) shared++;
  const union = left.size + right.size - shared;
  return union > 0 && shared / union >= DUPLICATE_OVERLAP;
}

/* A binary heap; the built-in sort is far too slow inside Dijkstra. */
class Heap {
  constructor() { this.items = []; }
  size() { return this.items.length; }
  push(item) {
    const items = this.items;
    items.push(item);
    let i = items.length - 1;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (items[parent][0] <= items[i][0]) break;
      [items[parent], items[i]] = [items[i], items[parent]];
      i = parent;
    }
  }
  pop() {
    const items = this.items;
    const top = items[0];
    const last = items.pop();
    if (items.length) {
      items[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let small = i;
        if (l < items.length && items[l][0] < items[small][0]) small = l;
        if (r < items.length && items[r][0] < items[small][0]) small = r;
        if (small === i) break;
        [items[small], items[i]] = [items[i], items[small]];
        i = small;
      }
    }
    return top;
  }
}

/* ------------------------------------------------------------------ *
 * State
 * ------------------------------------------------------------------ */

const state = {
  network: null, cities: [], basemap: null, router: null,
  from: null, to: null, routes: [], activeRoute: 0, focusLeg: null,
  lines: [], networkPaths: null, stepPaths: [], legButtons: [], picked: null, tiles: false,
  view: { k: 1, x: 0, y: 0 },
};

const el = (id) => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";

function node(name, attrs = {}) {
  const element = document.createElementNS(svgNS, name);
  for (const [key, value] of Object.entries(attrs)) {
    element.setAttribute(key, value);
  }
  return element;
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

async function boot() {
  const status = el("status");
  try {
    const [network, cities, basemap] = await Promise.all([
      fetch(NETWORK_URL).then((r) => r.json()),
      fetch(CITIES_URL).then((r) => r.json()),
      fetch(BASEMAP_URL).then((r) => r.json()),
    ]);
    state.network = network;
    state.cities = cities;
    state.basemap = basemap;
    state.router = new Router(network.legs);
  } catch (error) {
    status.textContent = "Could not load the network data (" + error.message + ").";
    return;
  }

  buildMap();
  wireSearch("from", "from-list");
  wireSearch("to", "to-list");
  wireControls();
  wireResize();

  const roads = Object.keys(state.network.roads).length;
  status.textContent = roads + " E-roads · " +
    state.network.jx.length.toLocaleString() + " interchanges · " +
    state.cities.length.toLocaleString() + " cities";
}

/* ------------------------------------------------------------------ *
 * Map
 * ------------------------------------------------------------------ */

let layers = {};

function buildMap() {
  const holder = el("map");
  const width = holder.clientWidth || 900;
  const height = holder.clientHeight || 700;
  fitToBox(state.basemap.bbox, width, height, 16);

  const svg = node("svg", { viewBox: `0 0 ${width} ${height}`,
                            preserveAspectRatio: "xMidYMid meet" });
  const root = node("g");

  /* Tiles cover the whole world, so without a clip a pan east reveals Asia
     while the E-road data stops at the Volga.  Clipping to the data's own
     window keeps the two honest about where the network actually ends. */
  const [clipX, clipY] = project(state.basemap.bbox[3], state.basemap.bbox[0]);
  const [clipX1, clipY1] = project(state.basemap.bbox[1], state.basemap.bbox[2]);
  const defs = node("defs");
  const clip = node("clipPath", { id: "map-window" });
  clip.appendChild(node("rect", { x: clipX, y: clipY,
                                  width: clipX1 - clipX, height: clipY1 - clipY }));
  defs.appendChild(clip);
  svg.append(defs, root);

  layers.tiles = node("g", { class: "tiles", "clip-path": "url(#map-window)" });
  layers.land = node("path", { class: "land" });
  layers.network = node("g", { class: "network" });
  layers.cities = node("g");
  layers.routes = node("g");
  layers.markers = node("g");
  root.append(layers.tiles, layers.land, layers.network, layers.cities,
              layers.routes, layers.markers);

  layers.network.style.display = "none";
  layers.cities.style.display = "none";
  layers.tiles.style.display = "none";

  /* One path for every country outline. */
  let landPath = "";
  for (const feature of state.basemap.features) {
    const geometry = feature.geometry;
    const polygons = geometry.type === "Polygon" ? [geometry.coordinates]
                                                 : geometry.coordinates;
    for (const polygon of polygons) {
      for (const ring of polygon) {
        landPath += pathOf(ring.map(([lon, lat]) => [lat, lon])) + "Z";
      }
    }
  }
  layers.land.setAttribute("d", landPath);

  /* The network is drawn as one path per road rather than one path for
     everything.  A single path renders fastest, but it cannot answer "which
     road is this?" on hover, and two hundred and fifty paths is still two
     orders of magnitude fewer than one per corridor. */
  state.lines = state.network.lines.map(decodeLine);
  buildNetworkLayer();

  for (const city of state.cities) {
    const [x, y] = project(city.lat, city.lon);
    const dot = node("circle", { class: "city-dot", cx: x.toFixed(1),
                                 cy: y.toFixed(1), r: 1.6 });
    /* A separate, much larger transparent circle takes the pointer, so the
       visible dot can stay small without being fiddly to hover. */
    const hit = node("circle", { class: "city-hit", cx: x.toFixed(1),
                                 cy: y.toFixed(1), r: 6 });
    hit.addEventListener("mouseenter", (event) => {
      dot.classList.add("lit");
      showTooltip(event, city.en + ", " + city.c +
        (city.p ? " · " + city.p.toLocaleString() : ""));
    });
    hit.addEventListener("mouseleave", () => {
      dot.classList.remove("lit");
      hideTooltip();
    });
    layers.cities.append(dot, hit);
  }

  holder.insertBefore(svg, holder.firstChild);
  layers.svg = svg;
  layers.root = root;
  wireZoom(svg, root, width, height);
}

/* Every path is baked in projected coordinates, so a change of viewport size
   means refitting and redrawing rather than nudging a transform.  Resizes are
   rare enough that rebuilding wholesale is simpler than keeping a second,
   resolution-independent copy of the geometry around. */
let resizeTimer = null;

function wireResize() {
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const holder = el("map");
      const previous = holder.querySelector("svg");
      if (previous) previous.remove();
      const view = state.view;
      buildMap();
      state.view = view;
      applyView();
      layers.network.style.display = el("show-network").checked ? "" : "none";
      layers.cities.style.display = el("show-cities").checked ? "" : "none";
      layers.tiles.style.display = state.tiles ? "" : "none";
      layers.land.style.display = state.tiles ? "none" : "";
      drawRoutes();
      if (state.tiles) renderTiles();
    }, 200);
  });
}

/* Which of the three AGR classes a road belongs to, as a CSS class.  This is
   the only categorical variable the network layer can honestly colour by. */
function roadClass(roadId) {
  const road = state.network.roads[roadId];
  if (!road) return "ramp";
  if (road.cls === "A-reference") return "ref";
  if (road.cls === "A-intermediate") return "intermediate";
  return "branch";
}

function buildNetworkLayer() {
  /* Ramps carry no geometry at all - they are why two E-roads connect, but on
     the map they were eighteen thousand disconnected stubs reading as debris. */
  const byRoad = new Map();
  for (const [road, encoded] of state.network.net) {
    if (!byRoad.has(road)) byRoad.set(road, []);
    byRoad.get(road).push(decodeLine(encoded));
  }
  state.networkPaths = new Map();
  layers.network.innerHTML = "";

  /* Reference roads last so the trunk of the network sits on top of the
     branches where they overlap. */
  const order = [...byRoad.keys()].sort((a, b) => {
    const rank = { branch: 0, intermediate: 1, ref: 2 };
    return rank[roadClass(a)] - rank[roadClass(b)];
  });

  for (const road of order) {
    let d = "";
    for (const line of byRoad.get(road)) {
      if (!longEnoughToDraw(line)) continue;
      d += pathOf(line);
    }
    if (!d) continue;
    const line = node("path", { class: "network-line " + roadClass(road), d });
    const hit = node("path", { class: "network-hit", d });
    hit.addEventListener("mouseenter", (event) => {
      line.classList.add("lit");
      showTooltip(event, networkLabel(road));
    });
    hit.addEventListener("mousemove", (event) => showTooltip(event, networkLabel(road)));
    hit.addEventListener("mouseleave", () => {
      line.classList.remove("lit");
      hideTooltip();
    });
    line.dataset.cls = roadClass(road);
    hit.dataset.cls = roadClass(road);
    layers.network.appendChild(line);
    layers.network.appendChild(hit);
    state.networkPaths.set(road, line);
  }
}

function networkLabel(roadId) {
  const road = state.network.roads[roadId];
  if (!road) return roadId;
  const where = road.countries && road.countries.length
    ? " · " + road.countries.slice(0, 6).join(" ") : "";
  return road.d + "  " + Math.round(road.km).toLocaleString() + " km" + where;
}

function applyView() {
  const { k, x, y } = state.view;
  layers.root.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
  /* Markers live inside the zoomed group, so their radii are in map units and
     would grow with the zoom - a 4px dot becomes a 160px blob at 40x, which is
     what was covering the map.  Strokes escape this via non-scaling-stroke;
     radii have no such property and must be divided out by hand. */
  for (const dot of layers.cities.children) {
    dot.setAttribute("r", (dot.classList.contains("city-hit") ? 7 : 1.8) / k);
  }
  for (const marker of layers.markers.children) {
    const base = Number(marker.dataset.r || 3);
    marker.setAttribute("r", base / k);
  }
  scheduleTiles();
}

/* ------------------------------------------------------------------ *
 * Optional raster basemap
 * ------------------------------------------------------------------ */

/* Always the light basemap, even when the interface is dark.  The dark tiles
   swallowed the road overlay, and a map is easier to read light regardless of
   the chrome around it - which is why paper atlases are not black. */
const TILE_URL = "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png";
const TILE_SIZE = 256;
const MAX_TILES = 400;   /* a hard ceiling on requests per redraw */

let tileTimer = null;

function scheduleTiles() {
  if (!state.tiles) return;
  clearTimeout(tileTimer);
  tileTimer = setTimeout(renderTiles, 120);
}

/*
 * Lay XYZ tiles into the same <g> the roads live in.  Because both use the
 * Mercator world square, a tile at (z, X, Y) occupies exactly the world rect
 * [X/2^z, (X+1)/2^z] x [Y/2^z, (Y+1)/2^z], which projectWorld turns into screen
 * coordinates - no separate map library and no chance of the two layers
 * disagreeing about where a junction is.
 */
function renderTiles() {
  if (!state.tiles) return;
  const box = layers.svg.viewBox.baseVal;
  const { k, x: tx, y: ty } = state.view;
  const pixelsPerWorld = fit.scale * k;

  let z = Math.round(Math.log2(pixelsPerWorld / TILE_SIZE));
  z = Math.max(2, Math.min(12, z));
  const count = Math.pow(2, z);

  /* Visible region, back-projected from the viewport corners into world units. */
  const toWorld = (sx, sy) => [
    ((sx - tx) / k - fit.dx) / fit.scale,
    ((sy - ty) / k - fit.dy) / fit.scale,
  ];
  const [w0, n0] = toWorld(0, 0);
  const [w1, n1] = toWorld(box.width, box.height);

  const x0 = Math.max(0, Math.floor(w0 * count));
  const x1 = Math.min(count - 1, Math.ceil(w1 * count));
  const y0 = Math.max(0, Math.floor(n0 * count));
  const y1 = Math.min(count - 1, Math.ceil(n1 * count));
  if ((x1 - x0 + 1) * (y1 - y0 + 1) > MAX_TILES) return;

  const template = TILE_URL;
  const size = fit.scale / count;
  const wanted = new Set();
  const existing = new Map();
  for (const image of layers.tiles.children) existing.set(image.dataset.key, image);

  for (let X = x0; X <= x1; X++) {
    for (let Y = y0; Y <= y1; Y++) {
      const key = `${z}/${X}/${Y}`;
      wanted.add(key);
      if (existing.has(key)) continue;
      const [px, py] = projectWorld(X / count, Y / count);
      const image = node("image", {
        x: px, y: py,
        width: size + 0.5,   /* a hair of overlap hides seams when scaled */
        height: size + 0.5,
        preserveAspectRatio: "none",
      });
      image.dataset.key = key;
      image.setAttribute("href", template.replace("{z}", z)
        .replace("{x}", X).replace("{y}", Y));
      layers.tiles.appendChild(image);
    }
  }
  for (const [key, image] of existing) {
    if (!wanted.has(key)) image.remove();
  }
}

function wireZoom(svg, root, width, height) {
  let dragging = false, lastX = 0, lastY = 0;

  const zoomAt = (factor, cx, cy) => {
    const k = Math.min(MAX_ZOOM, Math.max(0.8, state.view.k * factor));
    const real = k / state.view.k;
    state.view.x = cx - (cx - state.view.x) * real;
    state.view.y = cy - (cy - state.view.y) * real;
    state.view.k = k;
    applyView();
  };

  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const box = svg.getBoundingClientRect();
    const scaleX = width / box.width, scaleY = height / box.height;
    zoomAt(Math.exp(-event.deltaY * 0.0015),
           (event.clientX - box.left) * scaleX,
           (event.clientY - box.top) * scaleY);
  }, { passive: false });

  /* One finger pans, two fingers pinch.  The wheel handler above covers a
     mouse, but a phone has no wheel, and without this the only way to zoom
     was the +/- buttons - which is not what anyone tries first on a map.
     `touch-action: none` is what stops the browser panning the page instead
     of the map when the gesture starts. */
  svg.style.touchAction = "none";
  const pointers = new Map();
  let pinchDistance = 0;

  const spread = () => {
    const [a, b] = [...pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  };

  svg.addEventListener("pointerdown", (event) => {
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2) {
      pinchDistance = spread();
      dragging = false;             // the pan is now a pinch
    } else if (pointers.size === 1) {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      svg.setPointerCapture(event.pointerId);
    }
  });

  svg.addEventListener("pointermove", (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const box = svg.getBoundingClientRect();

    if (pointers.size === 2) {
      const now = spread();
      if (pinchDistance > 0 && now > 0) {
        const [a, b] = [...pointers.values()];
        zoomAt(now / pinchDistance,
               ((a.x + b.x) / 2 - box.left) * (width / box.width),
               ((a.y + b.y) / 2 - box.top) * (height / box.height));
      }
      pinchDistance = now;
      return;
    }

    if (!dragging) return;
    state.view.x += (event.clientX - lastX) * (width / box.width);
    state.view.y += (event.clientY - lastY) * (height / box.height);
    lastX = event.clientX;
    lastY = event.clientY;
    applyView();
  });

  const stop = (event) => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinchDistance = 0;
    if (pointers.size === 1) {
      /* A finger lifted out of a pinch - carry on panning with the one left,
         rather than jumping when it next moves. */
      const [only] = [...pointers.values()];
      lastX = only.x;
      lastY = only.y;
      dragging = true;
    }
    if (pointers.size === 0) dragging = false;
  };
  svg.addEventListener("pointerup", stop);
  svg.addEventListener("pointercancel", stop);

  el("zoom-in").onclick = () => zoomAt(1.5, width / 2, height / 2);
  el("zoom-out").onclick = () => zoomAt(1 / 1.5, width / 2, height / 2);
  el("zoom-reset").onclick = () => {
    state.view = { k: 1, x: 0, y: 0 };
    applyView();
  };
}

function focusOn(points) {
  if (!points.length) return;
  const box = layers.svg.viewBox.baseVal;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [lat, lon] of points) {
    const [x, y] = project(lat, lon);
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }
  const pad = 60;
  const k = Math.min(MAX_ZOOM, Math.max(0.8,
    Math.min((box.width - pad) / Math.max(maxX - minX, 1),
             (box.height - pad) / Math.max(maxY - minY, 1))));
  state.view = {
    k,
    x: box.width / 2 - ((minX + maxX) / 2) * k,
    y: box.height / 2 - ((minY + maxY) / 2) * k,
  };
  applyView();
}

function showTooltip(event, text) {
  const tip = el("tooltip");
  const box = el("map").getBoundingClientRect();
  tip.textContent = text;
  tip.style.left = (event.clientX - box.left + 12) + "px";
  tip.style.top = (event.clientY - box.top + 12) + "px";
  tip.dataset.show = "true";
}

function hideTooltip() { el("tooltip").dataset.show = "false"; }

/* ------------------------------------------------------------------ *
 * City search
 * ------------------------------------------------------------------ */

/* Mirrors cities.fold() in Python: accents and punctuation are noise when
   matching what someone typed against "Gothenburg" or "Bacău". */
function foldName(text) {
  return text.normalize("NFKD").replace(/[̀-ͯ]/g, "")
             .replace(/ß/g, "ss").replace(/[øØ]/g, "o").replace(/[đĐ]/g, "d")
             .replace(/[łŁ]/g, "l")
             .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/* Cities arrive sorted by population, so the first matches are the ones most
   people mean.  Prefix matches rank above substring matches, and the scan only
   stops once there are enough prefix hits to fill the list on their own. */
function searchCities(query, limit = 10) {
  const needle = foldName(query);
  if (!needle) return [];
  const starts = [], contains = [];
  for (const city of state.cities) {
    const name = foldName(city.n), english = foldName(city.en);
    if (name.startsWith(needle) || english.startsWith(needle)) starts.push(city);
    else if (contains.length < limit &&
             (name.includes(needle) || english.includes(needle))) contains.push(city);
    if (starts.length >= limit) break;
  }
  return starts.concat(contains).slice(0, limit);
}

function wireSearch(inputId, listId) {
  const input = el(inputId);
  const list = el(listId);
  let highlighted = -1;

  const close = () => { list.innerHTML = ""; highlighted = -1; };

  const choose = (city) => {
    state[inputId] = city;
    /* Country always shown: "Brest, FR" and "Brest, BY" are different places. */
    input.value = city.en + ", " + city.c;
    close();
    el("plan").disabled = !(state.from && state.to);
  };

  input.addEventListener("input", () => {
    state[inputId] = null;
    el("plan").disabled = true;
    const matches = searchCities(input.value);
    list.innerHTML = "";
    matches.forEach((city, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "option");
      button.innerHTML = "<span>" + escapeHtml(city.en) + "</span>" +
        '<span class="where">' + city.c +
        (city.p ? " · " + Math.round(city.p / 1000) + "k" : "") + "</span>";
      button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        choose(city);
      });
      if (index === highlighted) button.setAttribute("aria-selected", "true");
      list.appendChild(button);
    });
  });

  input.addEventListener("keydown", (event) => {
    const options = [...list.children];
    if (!options.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      highlighted = (highlighted + (event.key === "ArrowDown" ? 1 : -1) +
                     options.length) % options.length;
      options.forEach((option, index) =>
        option.toggleAttribute("aria-selected", index === highlighted));
      options[highlighted].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && highlighted >= 0) {
      event.preventDefault();
      options[highlighted].dispatchEvent(new MouseEvent("mousedown"));
    } else if (event.key === "Escape") {
      close();
    }
  });

  input.addEventListener("blur", () => setTimeout(close, 120));
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ------------------------------------------------------------------ *
 * Controls and results
 * ------------------------------------------------------------------ */

/* Three states, not two: an explicit light or dark choice, or no choice at all,
   in which case the operating system decides.  The stylesheet is written the
   same way, so the toggle only ever has to stamp or clear one attribute.
   Reading and writing storage is guarded because a private window, or a browser
   set to block site data, throws on access rather than returning nothing. */
function wireTheme() {
  const root = document.documentElement;
  let stored = null;
  try {
    stored = localStorage.getItem("eroad-theme");
  } catch (error) { /* storage unavailable; fall back to the system setting */ }
  if (stored === "dark" || stored === "light") root.dataset.theme = stored;

  const isDark = () => (root.dataset.theme
    ? root.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches);

  const button = el("theme");
  const paint = () => {
    button.firstElementChild.innerHTML = isDark() ? "&#9680;" : "&#9681;";
    button.title = isDark() ? "Switch to light" : "Switch to dark";
  };
  paint();

  button.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    root.dataset.theme = next;
    try {
      localStorage.setItem("eroad-theme", next);
    } catch (error) { /* the choice just will not persist */ }
    paint();
    /* Tiles come in a light and a dark cut; re-lay them for the new theme. */
    if (state.tiles) {
      layers.tiles.innerHTML = "";
      renderTiles();
    }
  });

  window.matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => { if (!root.dataset.theme) paint(); });
}

/* ----------------------------------------------------------------------
 * The route plan as a floating card
 *
 * The plan used to live at the bottom of the sidebar, which meant the two
 * things a reader compares - the written route and the drawn one - were as far
 * apart as the window allowed, and the sidebar had to stay open to see either.
 * As a card over the map they sit together, the sidebar can fold away, and the
 * plan can be pushed aside when it covers the very stretch it describes.
 * -------------------------------------------------------------------- */

function showPip(title) {
  const pip = el("pip");
  pip.hidden = false;
  if (title) el("pip-title").textContent = title;
}

function clearRoutes() {
  state.routes = [];
  state.activeRoute = 0;
  state.focusLeg = null;
  state.stepPaths = [];
  el("results").innerHTML = "";
  el("pip").hidden = true;
  drawRoutes();
}

function foldPip(folded) {
  const pip = el("pip");
  pip.classList.toggle("pip-folded", folded);
  const button = el("pip-collapse");
  button.setAttribute("aria-expanded", folded ? "false" : "true");
  button.title = folded ? "Expand" : "Collapse";
  button.firstElementChild.innerHTML = folded ? "&#43;" : "&#8722;";
}

/* Dragging is done with `left`/`top` in pixels, switched over from the `right`
   anchor on the first move so the card does not jump when it is grabbed. */
function wirePipDrag() {
  const pip = el("pip");
  const bar = el("pip-bar");
  let dragging = null;

  bar.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    if (onPhone()) return;          // it is a docked sheet, not a floating card
    const box = pip.getBoundingClientRect();
    const map = el("map").getBoundingClientRect();
    pip.style.left = (box.left - map.left) + "px";
    pip.style.top = (box.top - map.top) + "px";
    pip.style.right = "auto";
    dragging = { x: event.clientX, y: event.clientY,
                 left: box.left - map.left, top: box.top - map.top,
                 w: box.width, h: box.height, moved: false };
    pip.dataset.dragging = "true";
    bar.setPointerCapture(event.pointerId);
  });

  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    dragging.moved = true;
    const map = el("map").getBoundingClientRect();
    const limitX = Math.max(0, map.width - dragging.w);
    const limitY = Math.max(0, map.height - dragging.h);
    pip.style.left = clamp(dragging.left + event.clientX - dragging.x, 0, limitX) + "px";
    pip.style.top = clamp(dragging.top + event.clientY - dragging.y, 0, limitY) + "px";
  });

  const stop = (event) => {
    if (!dragging) return;
    /* Releasing a drag over a leg button must not also count as clicking it,
       which would re-frame the map to that stretch as the card is let go. */
    if (dragging.moved) {
      const swallow = (click) => { click.stopPropagation(); click.preventDefault(); };
      window.addEventListener("click", swallow, { capture: true, once: true });
      setTimeout(() => window.removeEventListener("click", swallow, true), 0);
    }
    dragging = null;
    delete pip.dataset.dragging;
    try { bar.releasePointerCapture(event.pointerId); } catch (ignored) { /* already gone */ }
  };
  bar.addEventListener("pointerup", stop);
  bar.addEventListener("pointercancel", stop);
}

function clamp(value, low, high) {
  return value < low ? low : value > high ? high : value;
}

/* Below this width the panel stops being a column beside the map and becomes
   a drawer over it, and the plan card becomes a bottom sheet. */
const PHONE = "(max-width: 760px)";
const onPhone = () => window.matchMedia(PHONE).matches;

/* Collapsing changes the width of the map, and the map is rebuilt at its
   pixel size, so the existing resize path has to run afterwards.  On a phone
   the panel floats over the map instead of taking a column from it, so the
   map's size is unchanged and there is nothing to rebuild. */
function setPanelCollapsed(collapsed) {
  const app = el("app");
  app.classList.toggle("panel-collapsed", collapsed);
  el("panel-hide").setAttribute("aria-expanded", collapsed ? "false" : "true");
  el("panel-show").setAttribute("aria-expanded", collapsed ? "false" : "true");
  try { localStorage.setItem("eroads:panel", collapsed ? "closed" : "open"); }
  catch (ignored) { /* private mode; the preference simply will not persist */ }
  if (!onPhone()) setTimeout(() => window.dispatchEvent(new Event("resize")), 200);
}

function wirePanelToggle() {
  let start = null;
  try { start = localStorage.getItem("eroads:panel"); }
  catch (ignored) { /* nothing stored, nothing to honour */ }
  /* On a phone the drawer covers the map, so it starts out of the way unless
     the reader has said otherwise.  On a desktop it starts open. */
  if (start === "closed" || (start === null && onPhone())) {
    el("app").classList.add("panel-collapsed");
  }
  el("panel-hide").addEventListener("click", () => setPanelCollapsed(true));
  el("panel-show").addEventListener("click", () => setPanelCollapsed(false));

  /* Tapping the map behind an open drawer closes it, which is what the dimmed
     backdrop invites you to do. */
  const scrim = document.createElement("div");
  scrim.id = "scrim";
  scrim.addEventListener("click", () => setPanelCollapsed(true));
  el("app").appendChild(scrim);

  /* Planning from the drawer should hand the map back straight away. */
  for (const id of ["plan", "shuffle", "swap"]) {
    el(id).addEventListener("click", () => {
      if (onPhone()) setPanelCollapsed(true);
    });
  }

  /* Rotating a phone, or crossing the breakpoint on a desktop, leaves the
     sheet carrying pixel offsets from a drag that no longer applies. */
  window.matchMedia(PHONE).addEventListener("change", releaseSheet);
}

/* Dragging positions the card with inline left/top.  Those must be cleared
   when it becomes a docked sheet, or it sits wherever it was last dropped. */
function releaseSheet() {
  const pip = el("pip");
  pip.style.left = "";
  pip.style.top = "";
  pip.style.right = "";
}

function wireControls() {
  wireTheme();
  wirePanelToggle();
  wirePipDrag();
  el("pip-clear").addEventListener("click", clearRoutes);
  el("pip-collapse").addEventListener("click", () => {
    foldPip(!el("pip").classList.contains("pip-folded"));
  });
  el("plan").addEventListener("click", planRoute);
  el("shuffle").addEventListener("click", shuffle);
  el("swap").addEventListener("click", () => {
    const a = state.from, b = state.to;
    state.from = b; state.to = a;
    el("from").value = b ? b.en + ", " + b.c : "";
    el("to").value = a ? a.en + ", " + a.c : "";
    if (state.from && state.to) planRoute();
  });
  el("show-network").addEventListener("change", (event) => {
    layers.network.style.display = event.target.checked ? "" : "none";
    el("legend").hidden = !event.target.checked;
    if (event.target.checked) applyClassFilter();
  });
  for (const box of document.querySelectorAll(".class-toggle")) {
    box.addEventListener("change", applyClassFilter);
  }
  wireRoadSearch();
  el("show-cities").addEventListener("change", (event) => {
    layers.cities.style.display = event.target.checked ? "" : "none";
  });
  el("show-tiles").addEventListener("change", (event) => {
    state.tiles = event.target.checked;
    layers.tiles.style.display = state.tiles ? "" : "none";
    /* The plain land outline would show through the tiles as a pale wash, so
       it steps aside while the detailed basemap is on. */
    layers.land.style.display = state.tiles ? "none" : "";
    el("attribution").style.display = state.tiles ? "" : "none";
    if (state.tiles) renderTiles();
    else layers.tiles.innerHTML = "";
  });
}

/* Pick two cities at random and plan between them.  Made for scanning: the
   fastest way to find a wrong answer is to look at a lot of answers, and
   choosing the pairs by hand biases towards the ones already checked. */
function shuffle() {
  const pool = el("shuffle-big").checked
    ? state.cities.filter((c) => (c.p || 0) >= 200000)
    : state.cities;
  if (pool.length < 2) return;
  const pick = () => pool[Math.floor(Math.random() * pool.length)];
  let from = pick(), to = pick();
  let guard = 0;
  while (to.id === from.id && guard++ < 20) to = pick();

  state.from = from;
  state.to = to;
  el("from").value = from.en + ", " + from.c;
  el("to").value = to.en + ", " + to.c;
  el("plan").disabled = false;
  planRoute();
}

/* Distances a driver could sanity-check, so an obviously wrong answer says so
   rather than looking authoritative. */
function routeWarnings(route, from, to) {
  const notes = [];
  const direct = haversineKm(from.lat, from.lon, to.lat, to.lon);
  if (direct > 40 && route.km > direct * 2.2) {
    notes.push("This is " + (route.km / direct).toFixed(1) +
      "x the straight-line distance (" + Math.round(direct) +
      " km), which usually means a gap in the network is forcing a detour.");
  }
  const last = route.steps[route.steps.length - 1];
  if (last) {
    const jx = state.network.jx[last.to];
    if (jx) {
      const away = haversineKm(to.lat, to.lon, jx.lat, jx.lon);
      if (away > 25) {
        notes.push("The network is left " + Math.round(away) + " km from " +
          to.en + "; the last stretch is not on an E-road.");
      }
    }
  }
  return notes;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const p1 = lat1 * D2R, p2 = lat2 * D2R;
  const dp = p2 - p1, dl = (lon2 - lon1) * D2R;
  const h = Math.sin(dp / 2) ** 2 +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * 6371.0088 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function planRoute() {
  const { from, to } = state;
  if (!from || !to) return;

  const starts = new Map(from.a.map(([node, km]) => [node, km]));
  const goals = new Map(to.a.map(([node, km]) => [node, km]));
  if (!starts.size || !goals.size) {
    renderMessage("One of these cities has no E-road interchange within reach.");
    return;
  }

  const started = performance.now();
  state.routes = state.router.plan(starts, goals, 3);
  state.activeRoute = 0;
  state.focusLeg = null;
  renderRoutes(performance.now() - started);
  drawRoutes();
  const points = state.routes.length
    ? state.routes[0].steps.flatMap(stepPoints)
    : [];
  if (points.length) focusOn(points);
}

function interchangeName(index) {
  const jx = state.network.jx[index];
  if (!jx) return "unknown";
  const where = jx.c ? " (" + jx.c + ")" : "";
  if (jx.km >= 8) return "junction near " + jx.n + where;
  return jx.n + where;
}

function renderMessage(text) {
  showPip("Route plan");
  el("results").innerHTML = '<p class="empty">' + escapeHtml(text) + "</p>";
}

function renderRoutes(elapsed) {
  const box = el("results");
  box.innerHTML = "";
  state.legButtons = [];
  foldPip(false);
  showPip(state.from && state.to
    ? state.from.en + " → " + state.to.en
    : "Route plan");
  if (!state.routes.length) {
    renderMessage("No E-road corridor connects these two cities. " +
      "They may be on separate parts of the network — an island, or a gap in the data.");
    return;
  }

  const notes = routeWarnings(state.routes[0], state.from, state.to);
  for (const note of notes) {
    const warn = document.createElement("p");
    warn.className = "warn";
    warn.textContent = note;
    box.appendChild(warn);
  }

  state.routes.forEach((route, index) => {
    const details = document.createElement("details");
    details.className = "route";
    details.dataset.index = index;
    details.dataset.active = index === state.activeRoute ? "true" : "false";
    details.open = index === 0;

    const summary = document.createElement("summary");
    summary.innerHTML =
      '<span class="swatch"></span>' +
      "<span><span class='route-roads'>" +
        route.steps.map((s) => roadPlate(s.road)).join("<span class='arrow'>→</span>") +
      "</span><br><span class='route-why'>" + escapeHtml(route.why) + "</span></span>" +
      "<span class='route-stats'><b>" + Math.round(route.km).toLocaleString() +
      " km</b><span>" + route.changes +
      (route.changes === 1 ? " change" : " changes") + "</span></span>";
    details.appendChild(summary);

    const legs = document.createElement("div");
    legs.className = "legs";
    route.steps.forEach((step, position) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "leg";
      const via = step.national.length
        ? step.national.map(([label, km]) => label + " (" + Math.round(km) + " km)")
                       .slice(0, 4).join(" · ")
        : "unnumbered roads";
      const slot = index === state.activeRoute ? slotOf(route, step.road) : 0;
      button.innerHTML =
        "<span class='leg-road'>" +
          (slot ? "<i class='leg-swatch slot-" + slot + "'></i>" : "") +
          roadPlate(step.road) + "</span>" +
        "<span><span class='leg-from'>" +
          escapeHtml(interchangeName(step.from)) + " → " +
          escapeHtml(interchangeName(step.to)) + "</span>" +
        "<span class='leg-detail'>" + Math.round(step.km) + " km · via " +
          escapeHtml(via) +
          (step.ferry ? " · <span class='ferry'>includes a ferry</span>" : "") +
        "</span></span>";
      /* Update the highlight in place rather than re-rendering: rebuilding the
         list here would collapse the open route and lose the scroll position
         under the click that caused it. */
      button.addEventListener("click", () => {
        state.activeRoute = index;
        state.focusLeg = { route: index, step: position };
        markActiveRoute();
        drawRoutes();
        focusOn(stepPoints(step));
      });
      /* Hovering a leg in the panel lights the matching stretch on the map,
         so the two halves of the answer point at each other. */
      button.addEventListener("mouseenter", () => {
        if (index === state.activeRoute) litStep(position, true);
      });
      button.addEventListener("mouseleave", () => {
        if (index === state.activeRoute) litStep(position, false);
      });
      legs.appendChild(button);
      /* The map needs to be able to light this row, not just be lit by it. */
      (state.legButtons[index] = state.legButtons[index] || [])[position] = button;

      if (position < route.steps.length - 1) {
        const change = document.createElement("div");
        change.className = "leg-change";
        change.innerHTML = "change to " +
          roadPlate(route.steps[position + 1].road) + " at <b>" +
          escapeHtml(interchangeName(step.to)) + "</b>";
        legs.appendChild(change);
      }
    });
    details.appendChild(legs);

    summary.addEventListener("click", () => {
      state.activeRoute = index;
      state.focusLeg = null;
      markActiveRoute();
      setTimeout(drawRoutes, 0);
    });
    box.appendChild(details);
  });

  if (elapsed !== undefined) {
    const note = document.createElement("p");
    note.className = "empty";
    note.style.paddingTop = "4px";
    note.textContent = "Planned in " + elapsed.toFixed(0) + " ms.";
    box.appendChild(note);
  }
}

/* Stitch a step's corridors into one polyline in travel order.  Each corridor
   is stored in its own direction, which may be the reverse of the way this
   route travels it, so each is flipped to continue the line rather than jump
   back to its far end - otherwise the drawn route zig-zags at every corridor
   boundary. */
/* Walk a step's legs into one continuous polyline, remembering for each point
   whether it came from a ferry.  Orientation has to be decided across the whole
   step - each leg is flipped to continue the line before it - so the ferry
   split has to happen afterwards, on the finished polyline, rather than by
   drawing each leg on its own. */
function stepTrace(step) {
  const points = [];
  const sea = [];

  /* The first leg has nothing before it to orient against, so it needs the
     step's own starting interchange as a reference.  Without one, a leg whose
     stored line happens to run backwards - which is common, since mirrored legs
     share their partner's line - made the trace start at the far end, walk back
     to the junction, and then jump forward again: a straight chord drawn over
     road that had just been drawn properly, closing a loop. */
  const origin = state.network.jx[step.from];
  const anchor = origin ? [origin.lat, origin.lon] : null;

  for (const legIndex of step.legs) {
    const leg = state.network.legs[legIndex];
    let run = leg.g === undefined ? null : state.lines[leg.g];
    if (!run || run.length < 2) continue;
    if (!points.length && anchor) {
      if (squareDistance(anchor, run[run.length - 1]) <
          squareDistance(anchor, run[0])) {
        run = run.slice().reverse();
      }
    }
    if (points.length) {
      const last = points[points.length - 1];
      const dHead = squareDistance(last, run[0]);
      const dTail = squareDistance(last, run[run.length - 1]);
      if (dTail < dHead) run = run.slice().reverse();

      /* Consecutive legs end at *different vertices* of the same interchange,
         and a cluster can be kilometres across, so joining their polylines
         directly cuts a chord over the junction - the stray straight segment
         that appears to overlap the route near an interchange.  Routing the
         seam through the interchange itself follows the ground instead. */
      if (squareDistance(last, run[0]) > SEAM_TOLERANCE) {
        const jx = state.network.jx[leg.a];
        if (jx) { points.push([jx.lat, jx.lon]); sea.push(!!leg.f); }
      }
    }
    const start = (points.length &&
                   points[points.length - 1][0] === run[0][0] &&
                   points[points.length - 1][1] === run[0][1]) ? 1 : 0;
    for (let i = start; i < run.length; i++) {
      points.push(run[i]);
      sea.push(!!leg.f);
    }
  }
  return { points, sea };
}

/* Split the trace into runs of road and runs of water, so only the crossing is
   drawn as a sea link.  Marking the whole step dashed because one leg of it was
   a ferry put the entire E55 - Scandinavia to Greece - on dashes for the sake
   of the Helsingor crossing. */
function stepSegments(step) {
  const { points, sea } = stepTrace(step);
  const segments = [];
  let current = null;
  for (let i = 0; i < points.length; i++) {
    if (!current || current.ferry !== sea[i]) {
      if (current) current.points.push(points[i]);   // share the boundary point
      current = { ferry: sea[i], points: [] };
      if (i > 0) current.points.push(points[i - 1]);
      segments.push(current);
    }
    current.points.push(points[i]);
  }
  return segments.filter((segment) => segment.points.length >= 2);
}

function stepPoints(step) {
  return stepTrace(step).points;
}

function squareDistance(a, b) {
  return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2;
}

function applyClassFilter() {
  const on = new Set([...document.querySelectorAll(".class-toggle")]
    .filter((box) => box.checked).map((box) => box.dataset.cls));
  for (const element of layers.network.children) {
    const cls = element.dataset.cls;
    element.style.display = !cls || on.has(cls) ? "" : "none";
  }
}

/* Picking a road fades everything else rather than hiding it, so the road is
   seen in the context of the network it belongs to. */
function pickRoad(roadId) {
  state.picked = roadId;
  for (const [road, line] of state.networkPaths) {
    line.classList.toggle("picked", road === roadId);
    line.classList.toggle("faded", roadId !== null && road !== roadId);
  }
  if (roadId) {
    const points = [];
    for (const [road, encoded] of state.network.net) {
      if (road === roadId) points.push(...decodeLine(encoded));
    }
    if (points.length) focusOn(points);
  }
}

function wireRoadSearch() {
  const input = el("road-search");
  const list = el("road-list");
  const entries = Object.entries(state.network.roads)
    .sort((a, b) => a[1].d.localeCompare(b[1].d, undefined, { numeric: true }));

  input.addEventListener("input", () => {
    const needle = input.value.trim().toLowerCase().replace(/\s+/g, "");
    list.innerHTML = "";
    if (!needle) { pickRoad(null); return; }
    const matches = entries
      .filter(([id, road]) => road.d.toLowerCase().startsWith(needle) ||
                              id.toLowerCase().startsWith(needle))
      .slice(0, 10);
    for (const [id, road] of matches) {
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = "<span>" + escapeHtml(road.d) + "</span>" +
        "<span class='where'>" + Math.round(road.km).toLocaleString() + " km · " +
        escapeHtml((road.countries || []).slice(0, 5).join(" ")) + "</span>";
      button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        input.value = road.d;
        list.innerHTML = "";
        if (!el("show-network").checked) el("show-network").click();
        pickRoad(id);
      });
      list.appendChild(button);
    }
  });
  input.addEventListener("blur", () => setTimeout(() => { list.innerHTML = ""; }, 120));
}

function markActiveRoute() {
  for (const card of el("results").querySelectorAll(".route")) {
    card.dataset.active = Number(card.dataset.index) === state.activeRoute
      ? "true" : "false";
  }
}

function roadLabel(roadId) {
  const road = state.network.roads[roadId];
  return road ? road.d : roadId;
}

/* The E-road plate, as the treaty countries actually sign it: a green field
   with a white ring inset from the edge and bold white numerals.  Proportions
   are taken from the standard drawing - 210 x 130 with the ring at 3/130 and
   the inner field at 10/130 - so it reads as the real sign at text size. */
function roadPlate(roadId) {
  return '<span class="eroad">' + escapeHtml(roadLabel(roadId)) + "</span>";
}

/* Slot numbers are assigned per *road* within a route, in the order the roads
   are first travelled, so a road keeps one colour even where it is rejoined
   later in the journey.  Colour never carries identity alone here: every step
   is direct-labelled with its road number in the panel and on hover. */
function slotOf(route, roadId) {
  const seen = [];
  for (const step of route.steps) {
    if (!seen.includes(step.road)) seen.push(step.road);
  }
  return (seen.indexOf(roadId) % 8) + 1;
}

function drawRoutes() {
  layers.routes.innerHTML = "";
  layers.markers.innerHTML = "";
  state.stepPaths = [];

  state.routes.forEach((route, index) => {
    const active = index === state.activeRoute;
    route.steps.forEach((step, position) => {
      const segments = stepSegments(step);
      if (!segments.length) return;
      const slot = active ? "slot-" + slotOf(route, step.road) : "alternative";
      const drawn = [];
      for (const segment of segments) {
        const d = pathOf(segment.points);
        if (!d) continue;
        const classes = ["route-line", slot];
        if (segment.ferry) classes.push("sea");
        const path = node("path", { class: classes.join(" "), d });
        layers.routes.appendChild(path);
        drawn.push(path);
      }
      /* A step is several paths now - road stretches and sea crossings - so
         hovering its entry in the panel has to light all of them. */
      if (active && drawn.length) {
        state.stepPaths[position] = drawn;
        /* Hovering the road on the map lights its row in the plan, the mirror
           of hovering the row to light the road.  The stretch a reader is
           pointing at and the description of it should always find each
           other, whichever end they start from. */
        for (const path of drawn) {
          path.classList.add("hoverable");
          path.addEventListener("mouseenter", () => litLeg(index, position, true));
          path.addEventListener("mouseleave", () => litLeg(index, position, false));
        }
      }
    });
  });

  const active = state.routes[state.activeRoute];
  if (!active) return;

  active.steps.forEach((step, position) => {
    if (position === active.steps.length - 1) return;
    const jx = state.network.jx[step.to];
    if (!jx) return;
    addMarker(jx.lat, jx.lon, "changepoint", 3,
      "change to " + roadLabel(active.steps[position + 1].road) +
      " at " + interchangeName(step.to));
  });

  for (const city of [state.from, state.to]) {
    if (!city) continue;
    addMarker(city.lat, city.lon, "endpoint", 4.5, city.en + ", " + city.c);
  }
  applyView();   /* sizes the new markers for the current zoom */
}

function addMarker(lat, lon, className, radius, tip) {
  const [x, y] = project(lat, lon);
  const marker = node("circle", { class: className, cx: x.toFixed(1),
                                  cy: y.toFixed(1), r: radius });
  marker.dataset.r = radius;
  marker.addEventListener("mouseenter", (event) => showTooltip(event, tip));
  marker.addEventListener("mouseleave", hideTooltip);
  layers.markers.appendChild(marker);
  return marker;
}

function litLeg(routeIndex, position, on) {
  const button = state.legButtons[routeIndex] && state.legButtons[routeIndex][position];
  if (!button) return;
  button.classList.toggle("lit", on);
  litStep(position, on);
  /* Bring the row into view only when it is actually out of it, so pointing at
     a road never yanks a list the reader is already reading. */
  if (!on) return;
  const list = el("results");
  const row = button.getBoundingClientRect();
  const frame = list.getBoundingClientRect();
  if (row.top < frame.top || row.bottom > frame.bottom) {
    button.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function litStep(position, on) {
  const paths = state.stepPaths && state.stepPaths[position];
  if (!paths) return;
  for (const path of paths) path.classList.toggle("lit", on);
}

boot();

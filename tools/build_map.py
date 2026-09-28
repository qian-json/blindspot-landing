"""Build the 3D Houston map data for the landing page from OpenStreetMap downloads.

    python3 tools/fetch_osm.py OSM_DIR      # once, ~100 MB of raw OSM JSON
    python3 tools/build_map.py OSM_DIR      # rewrites the MAP DATA block inside index.html

Everything is projected to a flat local frame centered on downtown (x east, z south), in scene units of
50 m, quantized to 5 m and stored as base64 zigzag-varint deltas. Traffic levels are a demo model
(5 PM rush plus the page's incidents), not live data. Map data (c) OpenStreetMap contributors, ODbL.
"""
import base64, heapq, json, math, re, sys
from pathlib import Path

LAT0, LON0 = 29.7604, -95.3698
KX, KZ = math.cos(math.radians(LAT0)) * 111320.0, 110574.0
UNIT, Q = 50.0, 10            # 1 scene unit = 50 m; 0.1 unit (5 m) quantization

OSM = Path(sys.argv[1] if len(sys.argv) > 1 else "osm")
PAGE = Path(__file__).resolve().parent.parent / "index.html"


def proj(lat, lon):
    return ((lon - LON0) * KX, -(lat - LAT0) * KZ)


def load(name):
    return json.load(open(OSM / f"{name}.json"))["elements"]


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def heading(a, b):  # compass degrees of a->b
    return math.degrees(math.atan2(b[0] - a[0], -(b[1] - a[1]))) % 360


def angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def dp_idx(pts, tol):
    """Douglas-Peucker; returns kept indices."""
    n = len(pts)
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        (ax, az), (bx, bz) = pts[a], pts[b]
        dx, dz = bx - ax, bz - az
        L = math.hypot(dx, dz)
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, pz = pts[i]
            d = abs(dx * (az - pz) - (ax - px) * dz) / L if L else math.hypot(px - ax, pz - az)
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [i for i in range(n) if keep[i]]


def qz(p):
    return (round(p[0] / UNIT * Q), round(p[1] / UNIT * Q))


def final_idx(pts, tol):
    """Simplify, then drop points that land on the same 5 m cell as the previous one."""
    out, last = [], None
    for i in dp_idx(pts, tol):
        q = qz(pts[i])
        if q != last:
            out.append(i)
            last = q
    return out


class Enc:
    def __init__(self):
        self.b = bytearray()
        self.n = 0

    def uv(self, v):
        while True:
            x, v = v & 0x7F, v >> 7
            if v:
                self.b.append(x | 0x80)
            else:
                self.b.append(x)
                return

    def sv(self, v):
        self.uv(v * 2 if v >= 0 else -v * 2 - 1)

    def line(self, pts):
        q = [qz(p) for p in pts]
        self.uv(len(q))
        px = pz = 0
        for x, z in q:
            self.sv(x - px)
            self.sv(z - pz)
            px, pz = x, z
        self.n += 1

    def out(self):
        return base64.b64encode(bytes(self.b)).decode()


def ring_area(r):
    return abs(sum(r[i][0] * r[i - 1][1] - r[i - 1][0] * r[i][1] for i in range(len(r)))) / 2


def rings_of(e):
    """Outer rings (meters) of a closed way or a multipolygon relation."""
    if e["type"] == "way":
        g = [proj(p["lat"], p["lon"]) for p in e.get("geometry", [])]
        return [g] if len(g) > 3 and dist(g[0], g[-1]) < 1 else []
    parts = [[proj(p["lat"], p["lon"]) for p in m["geometry"]] for m in e.get("members", [])
             if m.get("role") == "outer" and m.get("geometry")]
    rings = []
    while parts:
        cur = parts.pop()
        grown = True
        while dist(cur[0], cur[-1]) > 1 and grown:
            grown = False
            for i, p in enumerate(parts):
                if dist(cur[-1], p[0]) < 1:
                    cur += p[1:]
                elif dist(cur[-1], p[-1]) < 1:
                    cur += p[::-1][1:]
                elif dist(cur[0], p[-1]) < 1:
                    cur = p[:-1] + cur
                elif dist(cur[0], p[0]) < 1:
                    cur = p[::-1][:-1] + cur
                else:
                    continue
                parts.pop(i)
                grown = True
                break
        if dist(cur[0], cur[-1]) < 1 and len(cur) > 3:
            rings.append(cur)
    return rings


def noise(x, z):
    k = 1 / 1000
    return (0.5 * math.sin(x * k * 0.37 + z * k * 0.21 + 1.3) + 0.3 * math.sin(x * k * 0.83 - z * k * 0.61 + 0.4)
            + 0.2 * math.sin(-x * k * 1.7 + z * k * 1.3 + 2.1))


def key(g):
    return (round(g["lat"] * 1e7), round(g["lon"] * 1e7))


# ------------------------------------------------------------------ roads
major = load("major")
FW, LINKS, PRIM, SEC = [], [], [], []
for e in major:
    t, g = e["tags"], e.get("geometry") or []
    if len(g) < 2:
        continue
    h = t["highway"]
    pts = [proj(p["lat"], p["lon"]) for p in g]
    keys = [key(p) for p in g]
    r = min(math.hypot(*p) for p in pts)
    if h in ("motorway", "trunk"):
        ow = t.get("oneway")
        oneway = ow in ("yes", "1", "true", "-1") or (h == "motorway" and ow != "no")
        if ow == "-1":
            pts, keys = pts[::-1], keys[::-1]
        base = dict(ref=t.get("ref", ""), cls=h)
        FW.append(dict(base, pts=pts, keys=keys, sh=0))
        if not oneway:
            FW.append(dict(base, pts=pts[::-1], keys=keys[::-1], sh=1))
    elif h in ("motorway_link", "trunk_link"):
        LINKS.append(pts)
    elif h == "primary" and r < 32000:
        PRIM.append(dict(pts=pts, name=t.get("name", "")))
    elif h == "secondary" and r < 16000:
        SEC.append(dict(pts=pts, name=t.get("name", "")))

# baseline 5 PM traffic on freeways: outbound heavier, busier near the core, lighter past Beltway 8
for w in FW:
    p, n = w["pts"], len(w["pts"])
    lv = []
    for i in range(n):
        a, b = p[max(0, i - 1)], p[min(n - 1, i + 1)]
        dx, dz = b[0] - a[0], b[1] - a[1]
        x, z = p[i]
        r = math.hypot(x, z) or 1
        out = (dx * x + dz * z) / ((math.hypot(dx, dz) or 1) * r)
        s = 0.28 + 0.26 * out + 0.44 * noise(x, z) + 0.2 * math.exp(-r / 9000) - 0.4 * max(0, (r - 17000) / 9000)
        lv.append(3 if s > 0.8 else 2 if s > 0.4 else 1)
    w["lv"] = lv

pred, succ = {}, {}
for i, w in enumerate(FW):
    succ.setdefault(w["keys"][0], []).append(i)
    pred.setdefault(w["keys"][-1], []).append(i)


def walk(wi, idx, meters, fn, back=True):
    """Visit freeway points upstream (back=True) or downstream of (wi, idx), following connected ways."""
    d, w, i, seen = 0.0, wi, idx, {wi}
    while True:
        p = FW[w]["pts"]
        rng = range(i, -1, -1) if back else range(i, len(p))
        prev = None
        for j in rng:
            if prev is not None:
                d += dist(p[j], p[prev])
            if d > meters:
                return
            fn(w, j, d)
            prev = j
        nxt = pred.get(FW[w]["keys"][0], []) if back else succ.get(FW[w]["keys"][-1], [])
        nxt = [c for c in nxt if c not in seen]
        if not nxt:
            return
        same = [c for c in nxt if FW[c]["ref"] == FW[w]["ref"]] or nxt
        w = same[0]
        seen.add(w)
        i = len(FW[w]["pts"]) - 1 if back else 0


def snap(lat, lon, ref, hd, maxd=2500):
    x0 = proj(lat, lon)
    best = None
    for wi, w in enumerate(FW):
        if ref and not re.search(ref, w["ref"]):
            continue
        p = w["pts"]
        for i in range(len(p)):
            d = dist(p[i], x0)
            if d > maxd or (best and d >= best[0]):
                continue
            if hd is not None:
                a, b = p[max(0, i - 1)], p[min(len(p) - 1, i + 1)]
                if angdiff(heading(a, b), hd) > 60:
                    continue
            best = (d, wi, i)
    if not best:
        raise SystemExit(f"could not snap incident near {lat},{lon} to {ref}")
    return best[1], best[2]


# The page's incidents. Freeway traffic backs up upstream of each one and runs light just past it.
INC = [
    dict(id="rush", lat=29.8395, lon=-95.4990, ref=r"US 290", hd=300, heavy=3200, mod=2600),
    dict(id="heavier", lat=29.7810, lon=-95.4190, ref=r"I 10", hd=270, heavy=2400, mod=2200),
    dict(id="construction", lat=29.7885, lon=-95.3725, ref=r"I 45", hd=180, heavy=0, mod=2600),
    dict(id="event", lat=29.7500, lon=-95.3585, ref=r"I 69", hd=None, heavy=0, mod=1800),
    dict(id="crash", lat=29.7265, lon=-95.3415, ref=r"I 45", hd=135, heavy=3400, mod=2400, clear=1600),
    dict(id="closure", lat=29.7325, lon=-95.4185, ref=r"I 69", hd=45, heavy=2400, mod=2000, clear=900),
    dict(id="weather", lat=29.7395, lon=-95.4567, ref=r"I 610", hd=180, heavy=2800, mod=2000),
    dict(id="stalled", lat=29.8330, lon=-95.4105, ref=r"I 45", hd=350, heavy=2600, mod=2200, clear=1400),
]
inc_out = {}
for c in INC:
    wi, i = snap(c["lat"], c["lon"], c["ref"], c["hd"])
    hv, md = c["heavy"], c["mod"]

    def up(w, j, d, hv=hv, md=md):
        FW[w]["lv"][j] = 3 if d < hv else max(2, FW[w]["lv"][j]) if d < hv + md else FW[w]["lv"][j]
    walk(wi, i, hv + md, up)
    if c.get("clear"):
        walk(wi, i, c["clear"], lambda w, j, d: FW[w]["lv"].__setitem__(j, 1), back=False)
    p = FW[wi]["pts"]
    a, b = p[max(0, i - 1)], p[min(len(p) - 1, i + 1)]
    inc_out[c["id"]] = [round(p[i][0] / UNIT, 2), round(p[i][1] / UNIT, 2), round(heading(a, b))]

# ------------------------------------------------------------------ rail + the Navigation Blvd crossing
rail = []
for e in load("rail"):
    t = e["tags"]
    if t.get("service") in ("yard", "siding", "spur", "crossover") or t.get("usage") == "industrial":
        continue
    pts = [proj(p["lat"], p["lon"]) for p in e["geometry"]]
    if min(math.hypot(*p) for p in pts) < 30000:
        rail.append(pts)


def seg_x(a, b, c, d):
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-9:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den
    u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den
    return (a[0] + t * r[0], a[1] + t * r[1]) if 0 <= t <= 1 and 0 <= u <= 1 else None


nav = [w["pts"] for w in PRIM + SEC if w["name"] == "Navigation Boulevard"]
target = proj(29.7560, -95.3380)
xing = None
for n_ in nav:
    for r_ in rail:
        for i in range(len(n_) - 1):
            for j in range(len(r_) - 1):
                p = seg_x(n_[i], n_[i + 1], r_[j], r_[j + 1])
                if p and (not xing or dist(p, target) < dist(xing, target)):
                    xing = p
if not xing:
    raise SystemExit("no Navigation Blvd rail crossing found")
inc_out["train"] = [round(xing[0] / UNIT, 2), round(xing[1] / UNIT, 2), 90]

# street-level traffic: arterials near the core, heavier on Navigation Blvd by the crossing
ST = []
for w in PRIM:
    p = w["pts"]
    if min(math.hypot(*q) for q in p) > 9000:
        continue
    lv = []
    for q in p:
        s = 0.3 + 0.45 * noise(q[0] * 1.3 + 500, q[1] * 1.3) + 0.15 * math.exp(-math.hypot(*q) / 5000)
        lv.append(2 if s > 0.62 else 1)
    ST.append(dict(pts=p, lv=lv))
for p in nav:
    if min(dist(q, xing) for q in p) > 1400:
        continue
    ST.append(dict(pts=p, lv=[3 if dist(q, xing) < 600 else 2 if dist(q, xing) < 1300 else 1 for q in p]))

# ------------------------------------------------------------------ route: Downtown -> The Galleria
SPEED = {"motorway": 88, "motorway_link": 55, "trunk": 72, "trunk_link": 50, "primary": 48, "secondary": 40}
LVF = {1: 1.0, 2: 0.6, 3: 0.3}
adj, nodexy = {}, {}
fw_lv_at = {}
for w in FW:
    for k, l in zip(w["keys"], w["lv"]):
        fw_lv_at[k] = max(fw_lv_at.get(k, 1), l)
for e in major:
    t, g = e["tags"], e.get("geometry") or []
    h = t["highway"]
    if len(g) < 2 or h not in SPEED:
        continue
    pts = [proj(p["lat"], p["lon"]) for p in g]
    if min(math.hypot(*p) for p in pts) > 26000:
        continue
    ks = [key(p) for p in g]
    ow = t.get("oneway")
    oneway = ow in ("yes", "1", "true", "-1") or (h in ("motorway", "motorway_link") and ow != "no")
    if ow == "-1":
        pts, ks = pts[::-1], ks[::-1]
    for i in range(len(ks) - 1):
        nodexy[ks[i]], nodexy[ks[i + 1]] = pts[i], pts[i + 1]
        lvl = fw_lv_at.get(ks[i], 1) if h in ("motorway", "trunk") else 1
        cost = dist(pts[i], pts[i + 1]) / (SPEED[h] / 3.6 * LVF[lvl])
        adj.setdefault(ks[i], []).append((ks[i + 1], cost, h, t.get("ref", "")))
        if not oneway:
            adj.setdefault(ks[i + 1], []).append((ks[i], cost, h, t.get("ref", "")))


def nearest_node(lat, lon, ok=lambda cls, ref: True):
    p0 = proj(lat, lon)
    best = None
    for k, nb in adj.items():
        if not any(ok(c, r) for _, _, c, r in nb):
            continue
        d = dist(nodexy[k], p0)
        if not best or d < best[0]:
            best = (d, k)
    return best[1]


def shortest(a, b):
    dist_, prev, pq = {a: 0.0}, {}, [(0.0, a)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == b:
            break
        if d > dist_.get(u, 1e18):
            continue
        for v, c, _, _ in adj.get(u, []):
            nd = d + c
            if nd < dist_.get(v, 1e18):
                dist_[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    path, u = [b], b
    while u != a:
        u = prev[u]
        path.append(u)
    return path[::-1], dist_[b]


surface = lambda c, r: c in ("primary", "secondary")
A = nearest_node(29.7589, -95.3677, surface)          # Main St, downtown
Z = nearest_node(29.7394, -95.4613, surface)          # The Galleria, Westheimer Rd
on = lambda ref: (lambda c, r: c == "motorway" and ref in r)
routes = []
for via in ([], [(29.7810, -95.4100, "I 10"), (29.7640, -95.4560, "I 610")],
            [(29.7150, -95.3820, "TX 288"), (29.6800, -95.4300, "I 610"), (29.7150, -95.4575, "I 610")]):
    stops = [A] + [nearest_node(la, lo, on(ref)) for la, lo, ref in via] + [Z]
    path = []
    for s, e in zip(stops, stops[1:]):
        seg, _ = shortest(s, e)
        path += seg if not path else seg[1:]
    pts = [nodexy[k] for k in path]
    routes.append([pts[i] for i in final_idx(pts, 4)])

# ------------------------------------------------------------------ water, parks, buildings
wl_lines, wl_w, wpolys, parks, bl = [], [], [], [], []
for e in load("water"):
    t = e["tags"]
    ww = t.get("waterway")
    if e["type"] == "way" and ww:
        name = t.get("name", "")
        if ww == "stream" and not re.search(r"Bayou|Creek|Channel", name):
            continue
        pts = [proj(p["lat"], p["lon"]) for p in e["geometry"]]
        if min(math.hypot(*p) for p in pts) > 32000:
            continue
        wl_lines.append(pts)
        wl_w.append(3 if "Ship Channel" in name else 1 if ww in ("river", "canal") else 0)
    elif t.get("natural") == "water" or t.get("water"):
        for r in rings_of(e):
            if ring_area(r) > 150000 and min(math.hypot(*p) for p in r) < 34000:
                wpolys.append(r)
for e in load("parks"):
    for r in rings_of(e):
        if ring_area(r) > 120000 and min(math.hypot(*p) for p in r) < 26000:
            parks.append(r)


def bheight(t):
    try:
        return float(re.findall(r"[\d.]+", t["height"])[0])
    except (KeyError, IndexError, ValueError):
        pass
    try:
        return float(t["building:levels"]) * 3.6
    except (KeyError, ValueError):
        return 0.0


for e in load("bldg"):
    h = bheight(e["tags"])
    g = e.get("geometry") or []
    if h < 9 or len(g) < 4:
        continue
    r = [proj(p["lat"], p["lon"]) for p in g]
    if dist(r[0], r[-1]) > 1 or math.hypot(*r[0]) > 24000 or ring_area(r) < 150:
        continue
    bl.append((r, min(h, 320)))

# ------------------------------------------------------------------ encode
enc = {}


def lines(name, polys, tol, attr=None):
    E, extra = Enc(), []
    for k, p in enumerate(polys):
        pts = p["pts"] if isinstance(p, dict) else p
        idx = final_idx(pts, tol)
        if len(idx) < 2:
            continue
        E.line([pts[i] for i in idx])
        if attr:
            extra.append(attr(p, idx))
    enc[name] = E.out()
    return extra


def polys(name, rings, tol, heights=None):
    E, hs = Enc(), []
    for k, r in enumerate(rings):
        idx = final_idx(r, tol)
        if len(idx) < 4:
            continue
        E.line([r[i] for i in idx[:-1]])
        if heights:
            hs.append(round(heights[k] * 2))
    enc[name] = E.out()
    return hs


fw_meta = lines("fw", FW, 3, lambda w, idx: [w["sh"], "".join(str(w["lv"][i]) for i in idx)])
st_meta = lines("st", ST, 6, lambda w, idx: "".join(str(w["lv"][i]) for i in idx))
lines("lk", LINKS, 5)
lines("pr", [w["pts"] for w in PRIM], 6)
lines("sc", [w["pts"] for w in SEC], 12)
lines("mn", [[proj(p["lat"], p["lon"]) for p in e["geometry"]] for e in load("minor")], 6)
lines("rl", rail, 6)
wl_meta = lines("wl", [dict(pts=p, w=w) for p, w in zip(wl_lines, wl_w)], 10, lambda w, idx: w["w"])
polys("wp", wpolys, 10)
polys("pk", parks, 10)
bh = polys("bd", [b[0] for b in bl], 1.5, [b[1] for b in bl])
lines("rt", routes, 4)

LABELS = [("GREENSPOINT", 29.9450, -95.4150), ("ENERGY CORRIDOR", 29.7800, -95.6250), ("THE HEIGHTS", 29.7990, -95.3980),
          ("NEAR NORTHSIDE", 29.7905, -95.3530), ("DOWNTOWN", 29.7600, -95.3650), ("MIDTOWN", 29.7400, -95.3790),
          ("EAST END", 29.7420, -95.3230), ("GALLERIA", 29.7440, -95.4640), ("TEXAS MEDICAL CENTER", 29.7070, -95.3990),
          ("HOBBY AIRPORT", 29.6454, -95.2789), ("MONTROSE", 29.7470, -95.3920), ("MEMORIAL PARK", 29.7650, -95.4400),
          ("RIVER OAKS", 29.7560, -95.4200), ("BELLAIRE", 29.7058, -95.4588), ("THIRD WARD", 29.7230, -95.3620)]
PLACES = {"downtown": (29.7589, -95.3677), "galleria": (29.7394, -95.4613), "midtown": (29.7385, -95.3805),
          "pothole": (29.7340, -95.3905), "police": (29.7418, -95.3760)}
data = dict(
    unit=UNIT, q=Q,
    meta=dict(fw=fw_meta, st=st_meta, wl=wl_meta, bh=bh),
    inc=inc_out,
    labels=[[t] + [round(v / UNIT, 1) for v in proj(la, lo)] for t, la, lo in LABELS],
    places={k: [round(v / UNIT, 2) for v in proj(*ll)] for k, ll in PLACES.items()},
    **enc,
)
js = "window.BS_MAP=" + json.dumps(data, separators=(",", ":")) + ";"
page = PAGE.read_text()
a, b = page.index("<!--MAP DATA-->"), page.index("<!--/MAP DATA-->")
PAGE.write_text(page[:a] + "<!--MAP DATA-->\n<script>\n/* Houston map data (c) OpenStreetMap contributors, ODbL. Built by tools/build_map.py */\n" + js + "\n</script>\n" + page[b:])
sizes = {k: len(v) for k, v in data.items() if isinstance(v, str)}
print(f"map data {len(js):,} chars;", ", ".join(f"{k} {v // 1024}k" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])))
print(f"{len(FW)} freeway carriageways, {len(ST)} street traffic lines, {len(bl)} buildings, {len(parks)} parks, {len(wpolys)} water polygons")
print("incidents", inc_out)

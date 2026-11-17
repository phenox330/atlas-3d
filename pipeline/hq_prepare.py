"""Préparation « haute qualité » d'un secteur (après atlas.py all).

Produit dans build/hq/ :
  buildings.npz  maillage des bâtiments (murs + toits procéduraux, UV en mètres, attributs par face)
  trees.json     arbres réels : sommets de la hauteur de canopée LiDAR HD (MNH), hors bâti
  ground_mask_a.png  R = chaussée, G = trottoir/pavé, B = sable   (0,25 m/px)
  ground_mask_b.png  R = herbe/parc, G = eau OSM
  props.json     lampadaires et bancs placés le long des voies OSM
  context        ctx_mnt.npy + ctx_ortho.jpg : terrain basse résolution 8 km autour (horizon)
Repère Blender : x = est, y = nord, z = altitude NGF (m), origine = coin sud-ouest du secteur.
Usage : uv run hq_prepare.py <dossier_secteur>
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from io import BytesIO
from pathlib import Path

import mapbox_earcut as earcut
import numpy as np
import shapely
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage
from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).parent))
from atlas import WMS, Sector, get, log, progress, sample_height, to_l93, wms_params  # noqa: E402

MNH_LAYER = "IGNF_LIDAR-HD_MNH_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNT_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNT_FALLBACK = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
ORTHO_LAYER = "HR.ORTHOIMAGERY.ORTHOPHOTOS"

# matériaux (index) — repris tels quels par blender_hq.py
MATS = ["mur_enduit", "mur_brique", "mur_pierre", "toit_tuile", "toit_ardoise", "toit_plat", "mur_leger"]
M = {n: i for i, n in enumerate(MATS)}

# Teintes d'enduit balnéaire (blanc cassé, crème, sable, rosé, ocre clair, gris perle)
TINTS = [(0.95, 0.88, 0.72), (0.93, 0.80, 0.62), (0.92, 0.77, 0.72), (0.80, 0.86, 0.88), (0.96, 0.95, 0.92),
         (0.88, 0.72, 0.52), (0.85, 0.88, 0.78), (0.98, 0.92, 0.82), (0.90, 0.90, 0.88), (0.94, 0.84, 0.80)]


def rng_for(key: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.md5(key.encode()).hexdigest()[:8], 16))


# ------------------------------------------------------------------ données complémentaires
def fetch_grid(layer, bbox, w, h, fallback=None):
    for lay in [layer] + ([fallback] if fallback else []):
        r = get(WMS, wms_params(lay, bbox, w, h, "image/x-bil;bits=32"))
        if "bil" in r.headers.get("content-type", ""):
            a = np.frombuffer(r.content, dtype="<f4").reshape(h, w).copy()
            if (a < -1000).mean() < 0.5:
                return a
    raise RuntimeError(f"grille {layer} indisponible")


def fetch_extra(sec: Sector):
    raw = sec.raw
    if not (raw / "mnh.npy").exists():
        log("MNH LiDAR HD (hauteur de canopée) 0,5 m…")
        n = max(1, math.ceil(sec.size / 0.5 / 4000))
        step = sec.size / n
        px = int(round(step / 0.5))
        full = np.zeros((n * px, n * px), dtype=np.float32)
        for iy in range(n):
            for ix in range(n):
                bb = (sec.x0 + ix * step, sec.y0 + iy * step, sec.x0 + (ix + 1) * step, sec.y0 + (iy + 1) * step)
                a = fetch_grid(MNH_LAYER, bb, px, px)
                full[(n - 1 - iy) * px:(n - iy) * px, ix * px:(ix + 1) * px] = a
        full[full < -1000] = 0
        np.save(raw / "mnh.npy", full)
    if not (raw / "ctx_mnt.npy").exists():
        log("Contexte 8 km (terrain 8 m + ortho 3 m)…")
        cx, cy = (sec.x0 + sec.x1) / 2, (sec.y0 + sec.y1) / 2
        R = 4000
        bb = (cx - R, cy - R, cx + R, cy + R)
        a = fetch_grid(MNT_LAYER, bb, 1000, 1000, MNT_FALLBACK)
        a[a < -1000] = -3
        np.save(raw / "ctx_mnt.npy", a)
        img = Image.new("RGB", (2666, 2666))
        half = 1333
        for iy in range(2):
            for ix in range(2):
                b2 = (cx - R + ix * R, cy - R + iy * R, cx + ix * R, cy + iy * R)
                r = get(WMS, wms_params(ORTHO_LAYER, b2, half, half, "image/jpeg"))
                img.paste(Image.open(BytesIO(r.content)).convert("RGB"), (ix * half, (1 - iy) * half))
        img.save(raw / "ctx_ortho.jpg", quality=90)
        (raw / "ctx.json").write_text(json.dumps({"bbox_l93": bb, "origin_l93": [sec.x0, sec.y0]}))


# ------------------------------------------------------------------ bâtiments
class MeshBuf:
    def __init__(self):
        self.v, self.f, self.uv0, self.uv1, self.mat, self.col, self.seed = [], [], [], [], [], [], []

    def tri(self, pts, uv0, uv1, mat, col, seed):
        i = len(self.v)
        self.v += [list(p) for p in pts]
        self.f.append([i, i + 1, i + 2])
        self.uv0 += uv0
        self.uv1 += uv1
        self.mat.append(mat)
        self.col.append(col)
        self.seed.append(seed)

    def quad(self, a, b, c, d, uva, uvb, uvc, uvd, uv1, mat, col, seed):
        self.tri([a, b, c], [uva, uvb, uvc], [uv1] * 3, mat, col, seed)
        self.tri([a, c, d], [uva, uvc, uvd], [uv1] * 3, mat, col, seed)

    def save(self, path):
        np.savez_compressed(path, v=np.array(self.v, np.float32), f=np.array(self.f, np.int32),
                            uv0=np.array(self.uv0, np.float32), uv1=np.array(self.uv1, np.float32),
                            mat=np.array(self.mat, np.int16), col=np.array(self.col, np.float32),
                            seed=np.array(self.seed, np.float32), mats=np.array(MATS))


def planar_uv(pts):
    """UV en mètres d'une face de toit : u le long de l'égout, v dans le sens de la pente."""
    p = np.array(pts, float)
    n = np.cross(p[1] - p[0], p[2] - p[0])
    n /= np.linalg.norm(n) + 1e-9
    z = np.array([0, 0, 1.0])
    e = np.cross(z, n)
    if np.linalg.norm(e) < 1e-6:
        e = np.array([1.0, 0, 0])
    e /= np.linalg.norm(e)
    s = np.cross(n, e)
    return [[float(q @ e), float(q @ s)] for q in p]


def walls(buf, ring, base, top, mat, col, seed, ground):
    pts = [c[:2] for c in list(ring.coords)[:-1]]
    n = len(pts)
    h = top - base
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        L = math.hypot(x1 - x0, y1 - y0)
        if L < 0.05:
            continue
        # v = hauteur au-dessus du sol réel (rdc), u centré sur le segment
        vb, vt = base - ground, top - ground
        buf.quad((x0, y0, base), (x1, y1, base), (x1, y1, top), (x0, y0, top),
                 [-L / 2, vb], [L / 2, vb], [L / 2, vt], [-L / 2, vt], [L, top - ground], mat, col, seed)


def cap(buf, poly, z, mat, col, seed):
    rings = [np.array(poly.exterior.coords)[:-1, :2]] + [np.array(r.coords)[:-1, :2] for r in poly.interiors]
    verts = np.vstack(rings).astype(np.float64)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    try:
        idx = earcut.triangulate_float64(verts, ends).reshape(-1, 3)
    except Exception:
        return
    for t in idx:
        p = [(verts[k][0], verts[k][1], z) for k in t]
        # orientation vers le haut
        a, b, c = np.array(p)
        if np.cross(b - a, c - a)[2] < 0:
            p = [p[0], p[2], p[1]]
        buf.tri(p, [[q[0], q[1]] for q in p], [[0, 0]] * 3, mat, col, seed)


def pitched_roof(buf, poly, gutter, ridge_h, kind, overhang, mat_roof, mat_wall, col, seed, ground):
    """Toit à 4 pans (hip) ou 2 pans (gable) sur le rectangle orienté minimal (+ débord)."""
    rect = poly.minimum_rotated_rectangle
    c = np.array(rect.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    if np.linalg.norm(e1) < np.linalg.norm(e2):
        c = np.roll(c, -1, axis=0)
        e1, e2 = c[1] - c[0], c[2] - c[1]
    L, W = np.linalg.norm(e1), np.linalg.norm(e2)
    a, b = e1 / L, e2 / W
    ctr = c.mean(axis=0)
    L2, W2 = L / 2 + overhang, W / 2 + overhang
    z0 = gutter - overhang * (ridge_h / max(W / 2, 0.1))  # le débord descend dans le prolongement de la pente
    zt = gutter + ridge_h

    def P(sa, sb, z):
        q = ctr + a * sa + b * sb
        return (float(q[0]), float(q[1]), float(z))

    A, B, C, D = P(-L2, -W2, z0), P(L2, -W2, z0), P(L2, W2, z0), P(-L2, W2, z0)
    if kind == "hip" and L - W > 0.3:
        r = L / 2 - W / 2
        R1, R2 = P(-r, 0, zt), P(r, 0, zt)
        faces = [[A, B, R2, R1], [C, D, R1, R2], [B, C, R2], [D, A, R1]]
    elif kind == "hip":
        T = P(0, 0, zt)
        faces = [[A, B, T], [B, C, T], [C, D, T], [D, A, T]]
    else:  # gable : faîtage sur toute la longueur + pignons
        R1, R2 = P(-L2, 0, zt + 0), P(L2, 0, zt)
        faces = [[A, B, R2, R1], [C, D, R1, R2]]
        # pignons (murs triangulaires au-dessus de l'égout, sans débord)
        for s in (-1, 1):
            g1, g2, g3 = P(s * L / 2, -W / 2, gutter), P(s * L / 2, W / 2, gutter), P(s * L / 2, 0, zt)
            pts = [g1, g2, g3] if s > 0 else [g2, g1, g3]
            uv = [[-W / 2, gutter - ground], [W / 2, gutter - ground], [0, zt - ground]]
            buf.tri(pts, uv, [[W, zt - ground]] * 3, mat_wall, col, seed)
    for f in faces:
        uv = planar_uv(f)
        if len(f) == 4:
            buf.quad(*f, *uv, [0, 0], mat_roof, col, seed)
        else:
            buf.tri(f, uv, [[0, 0]] * 3, mat_roof, col, seed)


def build_buildings(sec: Sector, mnt):
    fc = json.loads((sec.raw / "batiments.geojson").read_text())
    clip = Polygon([(0, 0), (sec.size, 0), (sec.size, sec.size), (0, sec.size)])
    buf = MeshBuf()
    stats = {"villa_4pans": 0, "villa_2pans": 0, "immeuble": 0, "leger": 0, "annexe": 0}
    footprints = []
    for f in fc["features"]:
        pr = f["properties"]
        key = pr.get("cleabs") or str(f.get("id"))
        rng = rng_for(key)
        g = shapely.transform(shape(f["geometry"]), lambda c: c - np.array([sec.x0, sec.y0]), include_z=False)
        g = g.intersection(clip)
        if g.is_empty:
            continue
        polys = [p for p in (g.geoms if hasattr(g, "geoms") else [g]) if isinstance(p, Polygon) and p.area > 6]
        for poly in polys:
            poly = shapely.geometry.polygon.orient(poly.simplify(0.15), 1.0)
            if poly.is_empty or not poly.is_valid:
                poly = poly.buffer(0)
                if poly.is_empty or not isinstance(poly, Polygon):
                    continue
            footprints.append(poly)
            ring = np.array(poly.exterior.coords)
            grounds = [sample_height(mnt, sec, x, y) for x, y in ring[::max(1, len(ring) // 12)]]
            ground = float(np.median(grounds))
            base = float(min(grounds)) - 0.4
            h = pr.get("hauteur") or ((pr.get("nombre_d_etages") or 0) * 3.0) or (3.0 + rng.random() * 4)
            gutter = (pr.get("altitude_minimale_toit") if pr.get("altitude_minimale_toit") else None)
            if gutter is None or abs(gutter - (ground + h)) > 4:
                gutter = ground + h
            ridge = pr.get("altitude_maximale_toit")
            area = poly.area
            rect_ratio = area / max(poly.minimum_rotated_rectangle.area, 1e-6)
            usage = pr.get("usage_1") or ""
            leger = bool(pr.get("construction_legere"))
            tint = TINTS[rng.integers(len(TINTS))]
            seed = float(rng.random())
            h_wall = gutter - ground
            if leger:
                stats["leger"] += 1
                walls(buf, poly.exterior, base, gutter, M["mur_leger"], (0.75, 0.75, 0.72), seed, ground)
                cap(buf, poly, gutter, M["toit_plat"], tint, seed)
                continue
            is_tall = h_wall > 12.5 or area > 450
            if is_tall or rect_ratio < 0.6 or area > 900:
                stats["immeuble"] += 1
                wm = M["mur_enduit"] if rng.random() < 0.85 else M["mur_pierre"]
                walls(buf, poly.exterior, base, gutter + 0.8, wm, tint, seed, ground)  # acrotère
                for hole in poly.interiors:
                    walls(buf, hole, base, gutter + 0.8, wm, tint, seed, ground)
                cap(buf, poly, gutter + 0.05, M["toit_plat"], tint, seed)
                # retrait d'attique pour certains immeubles du front de mer
                if h_wall > 14 and rng.random() < 0.4:
                    inner = poly.buffer(-2.0)
                    if isinstance(inner, Polygon) and inner.area > 40:
                        walls(buf, inner.exterior, gutter, gutter + 3.0, wm, tint, seed, ground)
                        cap(buf, inner, gutter + 3.05, M["toit_plat"], tint, seed)
                continue
            # maisons / villas
            small = area < 45 and h_wall < 4.5
            r = rng.random()
            wall_mat = M["mur_enduit"] if r < 0.72 else (M["mur_brique"] if r < 0.87 else M["mur_pierre"])
            roof_mat = M["toit_tuile"] if rng.random() < 0.78 else M["toit_ardoise"]
            rect = poly.minimum_rotated_rectangle
            cc = np.array(rect.exterior.coords)
            W = min(np.linalg.norm(cc[1] - cc[0]), np.linalg.norm(cc[2] - cc[1]))
            pitch = math.radians(rng.uniform(32, 48) if roof_mat == M["toit_ardoise"] else rng.uniform(25, 36))
            rh = W / 2 * math.tan(pitch)
            if ridge and 0.8 < ridge - gutter < 9:
                rh = ridge - gutter
            rh = float(np.clip(rh, 0.8, 7.5))
            kind = "gable" if (small or rng.random() < 0.35) else "hip"
            stats["annexe" if small else ("villa_2pans" if kind == "gable" else "villa_4pans")] += 1
            walls(buf, poly.exterior, base, gutter, wall_mat, tint, seed, ground)
            pitched_roof(buf, poly, gutter, rh, kind, 0.45 if not small else 0.2, roof_mat, wall_mat, tint, seed, ground)
    buf.save(sec.out / "hq" / "buildings.npz")
    log(f"  bâtiments HQ : {stats} — {len(buf.f)} triangles")
    return footprints, stats


# ------------------------------------------------------------------ arbres LiDAR
def build_trees(sec: Sector, mnt, footprints, water_level):
    mnh = np.load(sec.raw / "mnh.npy")
    res = sec.size / mnh.shape[0]
    # masque bâti (dilaté) pour ne pas planter d'arbres sur les toits
    img = Image.new("L", mnh.shape[::-1], 0)
    d = ImageDraw.Draw(img)
    for p in footprints:
        pp = p.buffer(1.0)
        for g in (pp.geoms if hasattr(pp, "geoms") else [pp]):
            d.polygon([(x / res, (sec.size - y) / res) for x, y in g.exterior.coords], fill=255)
    bmask = np.array(img) > 0
    h = np.where(bmask, 0, mnh)
    hs = ndimage.gaussian_filter(h, 1.0)
    peaks = (hs == ndimage.maximum_filter(hs, size=int(round(3.5 / res)))) & (hs >= 4.0)
    ys, xs = np.nonzero(peaks)
    rng = np.random.default_rng(7)
    trees = []
    for py, px in zip(ys, xs):
        x, y = (px + 0.5) * res, sec.size - (py + 0.5) * res
        z = sample_height(mnt, sec, x, y)
        if z < water_level + 0.3:
            continue
        th = float(mnh[py, px])
        species = "pine" if th >= 9 or rng.random() < 0.35 else "broadleaf"
        trees.append([round(x, 2), round(y, 2), round(z, 2), round(th, 2), species, round(float(rng.uniform(0, 6.283)), 3)])
    (sec.out / "hq" / "trees.json").write_text(json.dumps(trees))
    log(f"  arbres LiDAR : {len(trees)} (pins {sum(t[4] == 'pine' for t in trees)})")
    return len(trees)


# ------------------------------------------------------------------ sol (masques) + mobilier
ROAD_W = {"motorway": 12, "trunk": 10, "primary": 9, "secondary": 8, "tertiary": 7, "residential": 6,
          "unclassified": 5.5, "living_street": 5, "service": 4, "cycleway": 2.2, "road": 6}
WALK_W = {"pedestrian": 6, "footway": 2.5, "steps": 2.5, "path": 1.8, "track": 3, "bridleway": 2}


def osm_local(sec, geom_pts):
    out = []
    for g in geom_pts:
        x, y = to_l93.transform(g["lon"], g["lat"])
        out.append((x - sec.x0, y - sec.y0))
    return out


def build_ground(sec: Sector, mnt):
    els = json.loads((sec.raw / "osm.json").read_text())["elements"] if (sec.raw / "osm.json").exists() else []
    roads, walks, sand, grass, water = [], [], [], [], []
    piers = []
    props = []
    rng = np.random.default_rng(11)
    for e in els:
        t = e.get("tags", {})
        if e["type"] != "way" or "geometry" not in e:
            continue
        pts = osm_local(sec, e["geometry"])
        if len(pts) < 2:
            continue
        hw = t.get("highway")
        if t.get("man_made") == "pier":
            g = Polygon(pts).buffer(0) if (pts[0] == pts[-1] and len(pts) >= 4) else LineString(pts).buffer(float(t.get("width", 6)) / 2, cap_style=2)
            for gg in (g.geoms if hasattr(g, "geoms") else [g]):
                piers.append({"name": t.get("name"), "ring": [list(map(lambda v: round(v, 2), c)) for c in gg.exterior.coords]})
            continue
        if hw in ROAD_W:
            w = float(t.get("width", ROAD_W[hw])) if str(t.get("width", "")).replace(".", "").isdigit() else ROAD_W[hw]
            line = LineString(pts)
            roads.append(line.buffer(w / 2, cap_style=2))
            if hw not in ("service", "cycleway"):
                walks.append(line.buffer(w / 2 + 1.9, cap_style=2))
                # lampadaires tous les ~28 m, en quinconce
                if hw in ("residential", "tertiary", "secondary", "primary", "living_street", "unclassified"):
                    Ltot = line.length
                    k = 0
                    for s in np.arange(10, Ltot - 5, 28):
                        p0, p1 = line.interpolate(s), line.interpolate(min(s + 1, Ltot))
                        dx, dy = p1.x - p0.x, p1.y - p0.y
                        n = math.hypot(dx, dy) or 1
                        side = 1 if k % 2 == 0 else -1
                        off = w / 2 + 1.1
                        x, y = p0.x - dy / n * off * side, p0.y + dx / n * off * side
                        props.append(["street_lamp_01", round(x, 2), round(y, 2), round(sample_height(mnt, sec, x, y), 2),
                                      round(math.atan2(dy, dx) + (math.pi / 2 if side > 0 else -math.pi / 2), 3)])
                        k += 1
        elif hw in WALK_W:
            line = LineString(pts)
            walks.append(line.buffer(WALK_W[hw] / 2, cap_style=2))
            if hw in ("pedestrian", "footway") and line.length > 40 and rng.random() < 0.6:
                for s in np.arange(15, line.length - 10, 45):
                    p = line.interpolate(s)
                    p1 = line.interpolate(min(s + 1, line.length))
                    ang = math.atan2(p1.y - p.y, p1.x - p.x)
                    props.append(["painted_wooden_bench", round(p.x, 2), round(p.y, 2),
                                  round(sample_height(mnt, sec, p.x, p.y), 2), round(ang, 3)])
        closed = pts[0] == pts[-1] and len(pts) >= 4
        if closed:
            poly = Polygon(pts).buffer(0)
            nat, lu, le = t.get("natural"), t.get("landuse"), t.get("leisure")
            if nat in ("beach", "sand") or lu == "beach":
                sand.append(poly)
            elif le in ("park", "garden", "pitch", "playground") or lu in ("grass", "recreation_ground", "village_green") or nat == "grassland":
                grass.append(poly)
            elif nat == "water" or lu == "basin" or t.get("place") == "sea":
                water.append(poly)
            elif t.get("amenity") == "parking" or t.get("area:highway") or t.get("place") == "square":
                walks.append(poly)
    res = 0.25
    N = int(sec.size / res)

    def raster(polys, blur=0.8):
        img = Image.new("L", (N, N), 0)
        d = ImageDraw.Draw(img)
        if polys:
            u = unary_union(polys)
            for g in (u.geoms if hasattr(u, "geoms") else [u]):
                if isinstance(g, Polygon) and not g.is_empty:
                    d.polygon([(x / res, (sec.size - y) / res) for x, y in g.exterior.coords], fill=255)
                    for hole in g.interiors:
                        d.polygon([(x / res, (sec.size - y) / res) for x, y in hole.coords], fill=0)
        return img.filter(ImageFilter.GaussianBlur(blur))

    r_road = raster(roads)
    r_walk = raster(walks)
    # le trottoir n'existe que hors chaussée
    walk = Image.fromarray(np.clip(np.array(r_walk, int) - np.array(r_road, int), 0, 255).astype(np.uint8))
    r_sand = raster(sand, 1.2)
    Image.merge("RGB", (r_road, walk, r_sand)).save(sec.out / "hq" / "ground_mask_a.png")
    Image.merge("RGB", (raster(grass, 2), raster(water, 2), Image.new("L", (N, N), 0))).save(sec.out / "hq" / "ground_mask_b.png")
    (sec.out / "hq" / "props.json").write_text(json.dumps(props))
    (sec.out / "hq" / "piers.json").write_text(json.dumps(piers, ensure_ascii=False))
    log(f"  sol : {len(roads)} chaussées, {len(walks)} trottoirs/places, {len(sand)} plages, {len(grass)} espaces verts ; {len(props)} objets de mobilier, {len(piers)} jetées")


def main(folder: Path):
    sec = Sector(folder)
    (sec.out / "hq").mkdir(exist_ok=True)
    mnt = np.load(sec.raw / "mnt.npy")
    water_level = float(sec.cfg.get("water_level_m", 0.6))
    progress(0.05, "données complémentaires")
    fetch_extra(sec)
    progress(0.3, "bâtiments HQ")
    log("Bâtiments (toits + façades procédurales)…")
    fps, stats = build_buildings(sec, mnt)
    progress(0.6, "arbres LiDAR")
    log("Arbres réels depuis la canopée LiDAR…")
    n_trees = build_trees(sec, mnt, fps, water_level)
    progress(0.8, "sol et mobilier")
    log("Masques de sol + mobilier urbain…")
    build_ground(sec, mnt)
    info = {"buildings": stats, "trees": n_trees, "water_level_m": water_level}
    (sec.out / "hq" / "hq_manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    progress(1, "terminé")
    log("HQ prêt")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

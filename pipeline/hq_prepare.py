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
MATS = ["mur_enduit", "mur_brique", "mur_pierre", "toit_tuile", "toit_ardoise", "toit_plat", "mur_leger", "lambrequin"]
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
# Vecteur « façade » par face (lu par le shader de mur) :
# [hauteur d'étage (0 = défaut), vitrine RDC (-1 hasard / 0 / 1), volets (-1 hasard, 0 aucun, 1 battants, 2 roulants),
#  couleur volets r g b, chaînages brique (0/1), couleur chaînages r g b]
FA_DEFAULT = [0.0, -1.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.55, 0.24, 0.17]
BRICK = (0.62, 0.27, 0.18)


class MeshBuf:
    def __init__(self):
        self.v, self.f, self.uv0, self.uv1, self.mat, self.col, self.seed = [], [], [], [], [], [], []
        self.fa, self.cur_fa = [], list(FA_DEFAULT)

    def tri(self, pts, uv0, uv1, mat, col, seed):
        i = len(self.v)
        self.v += [list(p) for p in pts]
        self.f.append([i, i + 1, i + 2])
        self.uv0 += uv0
        self.uv1 += uv1
        self.mat.append(mat)
        self.col.append(col)
        self.seed.append(seed)
        self.fa.append(self.cur_fa)

    def quad(self, a, b, c, d, uva, uvb, uvc, uvd, uv1, mat, col, seed):
        self.tri([a, b, c], [uva, uvb, uvc], [uv1] * 3, mat, col, seed)
        self.tri([a, c, d], [uva, uvc, uvd], [uv1] * 3, mat, col, seed)

    def save(self, path):
        np.savez_compressed(path, v=np.array(self.v, np.float32), f=np.array(self.f, np.int32),
                            uv0=np.array(self.uv0, np.float32), uv1=np.array(self.uv1, np.float32),
                            mat=np.array(self.mat, np.int16), col=np.array(self.col, np.float32),
                            seed=np.array(self.seed, np.float32), fa=np.array(self.fa, np.float32), mats=np.array(MATS))


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


def rect_frame(poly, ridge_short=False):
    """Rectangle orienté minimal : coins, axe a (faîtage), axe b, longueurs L (selon a), W, centre."""
    rect = poly.minimum_rotated_rectangle
    c = np.array(rect.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    if (np.linalg.norm(e1) < np.linalg.norm(e2)) != ridge_short:
        c = np.roll(c, -1, axis=0)
        e1, e2 = c[1] - c[0], c[2] - c[1]
    L, W = np.linalg.norm(e1), np.linalg.norm(e2)
    return c, e1 / max(L, 1e-9), e2 / max(W, 1e-9), L, W, c.mean(axis=0)


def hang_band(buf, p, q, depth, col, seed):
    """Bande verticale (lambrequin) suspendue sous l'arête p→q."""
    p, q = np.array(p), np.array(q)
    Lr = float(np.linalg.norm(q[:2] - p[:2])) or 0.01
    a, b = tuple(p), tuple(q)
    c, d = (q[0], q[1], q[2] - depth), (p[0], p[1], p[2] - depth)
    for (u, v, w, x) in ((a, b, c, d), (b, a, d, c)):  # deux faces : visible des deux côtés
        buf.quad(u, v, w, x, [0, depth], [Lr, depth], [Lr, 0], [0, 0], [Lr, depth], M["lambrequin"], col, seed)


def pitched_roof(buf, poly, gutter, ridge_h, kind, overhang, mat_roof, mat_wall, col, seed, ground,
                 ridge_short=False, lambrequins=False):
    """Toit à 4 pans (hip) ou 2 pans (gable) sur le rectangle orienté minimal (+ débord).
    ridge_short : faîtage parallèle au petit côté (mesuré sur le LiDAR)."""
    c, a, b, L, W, ctr = rect_frame(poly, ridge_short)
    L2, W2 = L / 2 + overhang, W / 2 + overhang
    z0 = gutter - overhang * (ridge_h / max(W / 2, 0.1))  # le débord descend dans le prolongement de la pente
    zt = gutter + ridge_h

    def P(sa, sb, z):
        q = ctr + a * sa + b * sb
        return (float(q[0]), float(q[1]), float(z))

    A, B, C, D = P(-L2, -W2, z0), P(L2, -W2, z0), P(L2, W2, z0), P(-L2, W2, z0)
    if kind == "hip" and L - W > 0.3 and not ridge_short:
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
    if lambrequins:
        dz = 0.38
        if kind == "gable":  # rives des pignons (la signature des villas arcachonnaises) + égouts
            for s_ in (-1, 1):
                e0, e1_, top = P(s_ * L2, -W2, z0), P(s_ * L2, W2, z0), P(s_ * L2, 0, zt)
                hang_band(buf, e0, top, dz, col, seed)
                hang_band(buf, top, e1_, dz, col, seed)
            hang_band(buf, A, B, dz * 0.6, col, seed)
            hang_band(buf, C, D, dz * 0.6, col, seed)
        else:
            for p_, q_ in ((A, B), (B, C), (C, D), (D, A)):
                hang_band(buf, p_, q_, dz * 0.7, col, seed)


def hex_rgb(h, default=None):
    try:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except Exception:
        return default


class LidarRoofs:
    """Forme réelle des toits depuis le LiDAR HD (MNS = MNT + MNH, 0,5 m).
    Pour chaque emprise : plat, 2 pans (faîtage long ou court) ou 4 pans, avec hauteurs d'égout et de faîtage
    mesurées. Les emprises sans échos (trou LiDAR, bâtiment démoli, verrière) sont signalées pour la QA."""

    def __init__(self, sec, mnt):
        self.mnh = np.load(sec.raw / "mnh.npy")
        r = self.mnh.shape[0] // mnt.shape[0]
        self.mns = np.repeat(np.repeat(mnt, r, 0), r, 1)[: self.mnh.shape[0], : self.mnh.shape[1]] + self.mnh
        self.res = sec.size / self.mnh.shape[0]
        self.size = sec.size
        self.qa = []

    def fit(self, poly, key):
        er = poly.buffer(-0.45)
        if er.is_empty or er.area < 4:
            er = poly
        x0, y0, x1, y1 = er.bounds
        j0, j1 = int(max(0, x0 / self.res)), int(min(self.mnh.shape[1] - 1, x1 / self.res + 1))
        i0, i1 = int(max(0, (self.size - y1) / self.res)), int(min(self.mnh.shape[0] - 1, (self.size - y0) / self.res + 1))
        if j1 <= j0 or i1 <= i0:
            return None
        J, I = np.meshgrid(np.arange(j0, j1 + 1), np.arange(i0, i1 + 1))
        X, Y = (J + 0.5) * self.res, self.size - (I + 0.5) * self.res
        m = shapely.contains_xy(er, X, Y)
        if m.sum() < 8:
            return None
        h, z = self.mnh[I[m], J[m]], self.mns[I[m], J[m]]
        ok = h > 1.4
        if ok.mean() < 0.45:
            c = poly.centroid
            self.qa.append({"cleabs": key, "x": round(c.x, 1), "y": round(c.y, 1), "probleme": "pas d'écho LiDAR sur l'emprise",
                            "couverture": round(float(ok.mean()), 2)})
            return None
        xy, z = np.stack([X[m][ok], Y[m][ok]], 1), z[ok]
        lo, hi = np.percentile(z, [3, 97])
        keep = (z >= lo) & (z <= hi)  # antennes, cheminées, arbres en surplomb
        xy, z = xy[keep], z[keep]
        if len(z) < 6:
            return None
        res = {"flat": (float(np.std(z)), None)}
        for short in (False, True):
            c, a, b, L, W, ctr = rect_frame(poly, short)
            s_, t_ = (xy - ctr) @ a, (xy - ctr) @ b
            models = {"gable": np.abs(t_)}
            if not short:
                models["hip"] = np.maximum(np.abs(t_), np.abs(s_) - (L - W) / 2)
            for kind, Xm in models.items():
                A = np.stack([np.ones_like(Xm), -Xm], 1)
                coef, *_ = np.linalg.lstsq(A, z, rcond=None)
                rms = float(np.sqrt(np.mean((A @ coef - z) ** 2)))
                if coef[1] > 0.18:  # pente > ~10°
                    res[f"{kind}{'_court' if short else ''}"] = (rms, (float(coef[0]), float(coef[1]), W / 2))
        std = res.pop("flat")[0]
        # plat : surface quasi horizontale. Sinon on garde le meilleur modèle à pans, même imparfait
        # (toits composés en L ou en T) : une surface qui varie de > 1 m n'est pas une terrasse.
        if std < 0.5 or not res or (std < 1.0 and min(r[0] for r in res.values()) > 0.8 * std):
            return {"kind": "flat", "top": float(np.percentile(z, 70))}
        best = min(res, key=lambda k: res[k][0])
        top, k, half = res[best][1]
        ridge_h = float(np.clip(k * half, 0.6, 9.0))
        return {"kind": best.split("_")[0], "short": best.endswith("_court"), "gutter": top - ridge_h, "ridge_h": ridge_h}


def planarize(xy, z, rng, max_planes=7, thr=0.22, iters=120):
    """Découpe une surface de toit LiDAR en pans plans (RANSAC séquentiel) et projette chaque point
    sur son pan : arêtiers et faîtages nets au lieu d'une surface « fondue ». Les points hors pans
    (lucarnes, cheminées) gardent leur altitude mesurée."""
    n = len(z)
    out = z.copy()
    free = np.ones(n, bool)
    min_in = max(10, int(0.06 * n))
    P = np.column_stack([xy, np.ones(n)])
    for _ in range(max_planes):
        idx = np.nonzero(free)[0]
        if len(idx) < min_in:
            break
        best, best_cnt = None, 0
        for _ in range(iters):
            s3 = idx[rng.choice(len(idx), 3, replace=False)]
            A = P[s3]
            try:
                coef = np.linalg.solve(A, z[s3])
            except np.linalg.LinAlgError:
                continue
            if abs(coef[0]) > 1.6 or abs(coef[1]) > 1.6:  # pente > ~58° : pas un pan de toit
                continue
            cnt = int((np.abs(P[idx] @ coef - z[idx]) < thr).sum())
            if cnt > best_cnt:
                best, best_cnt = coef, cnt
        if best is None or best_cnt < min_in:
            break
        inl = idx[np.abs(P[idx] @ best - z[idx]) < thr]
        coef, *_ = np.linalg.lstsq(P[inl], z[inl], rcond=None)  # réajustement sur les inliers
        inl = idx[np.abs(P[idx] @ coef - z[idx]) < thr * 1.3]
        out[inl] = P[inl] @ coef
        free[inl] = False
    return out


class LidarSurface:
    """Toits « relevés » : la surface LiDAR (MNS = MNT + MNH, 0,5 m, médiane 3×3) est triangulée dans
    chaque emprise BD TOPO ; les murs montent jusqu'au bord de cette surface. On récupère ainsi les vrais
    toits (croupes à cheval sur plusieurs parcelles, lucarnes, cours, terrasses) au lieu de les deviner.
    Les emprises sans écho LiDAR (verrières, démolitions, trous) repassent en procédural et vont en QA."""

    def __init__(self, sec, mnt):
        from scipy.ndimage import median_filter
        self.mnh = np.load(sec.raw / "mnh.npy")
        r = self.mnh.shape[0] // mnt.shape[0]
        mnt_hr = ndimage.zoom(mnt, r, order=1)[: self.mnh.shape[0], : self.mnh.shape[1]]
        self.mns = median_filter(mnt_hr + self.mnh, size=3)
        self.res = sec.size / self.mnh.shape[0]
        self.size = sec.size
        self.qa = []

    def z(self, xs, ys):
        rows = (self.size - np.asarray(ys)) / self.res - 0.5
        cols = np.asarray(xs) / self.res - 0.5
        return ndimage.map_coordinates(self.mns, [rows, cols], order=1, mode="nearest")

    def h(self, xs, ys):
        rows = ((self.size - np.asarray(ys)) / self.res).astype(int).clip(0, self.mnh.shape[0] - 1)
        cols = (np.asarray(xs) / self.res).astype(int).clip(0, self.mnh.shape[1] - 1)
        return self.mnh[rows, cols]

    def coverage(self, poly):
        er = poly.buffer(-0.5)
        if er.is_empty:
            er = poly
        x0, y0, x1, y1 = er.bounds
        X, Y = np.meshgrid(np.arange(x0, x1, self.res), np.arange(y0, y1, self.res))
        m = shapely.contains_xy(er, X, Y)
        if m.sum() < 6:
            return 0.0
        return float((self.h(X[m], Y[m]) > 1.4).mean())

    def build(self, buf, poly, base, ground, wall_mat, roof_mat, flat_mat, col, seed, step=1.0, inset=0.6, force_flat=False):
        """Ajoute murs + toit au buffer. Renvoie (hauteur de mur médiane, part de toit plat)."""
        from scipy.spatial import Delaunay
        # 1. points de bord (murs) : altitude du toit mesurée un peu à l'intérieur de l'emprise
        ring = np.array(poly.exterior.coords)[:-1, :2]
        n = len(ring)
        edges = []  # (P points, ts abscisses, L, indices dans pts)
        bpts, btop = [], []
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            e = b - a
            L = float(np.linalg.norm(e))
            if L < 0.05:
                continue
            d = e / L
            nin = np.array([-d[1], d[0]])  # emprise orientée sens trigo : intérieur à gauche
            k = max(1, int(math.ceil(L / step)))
            ts = np.linspace(0, L, k + 1)
            P = a + np.outer(ts, d)
            Q = P + nin * min(inset, L / 3)
            zt = self.z(Q[:, 0], Q[:, 1])
            i0 = sum(len(x) for x in bpts)
            edges.append((P, ts, L, i0))
            bpts.append(P)
            btop.append(zt)
        if not bpts:
            return 0.0, 0.0
        nb_total = sum(len(x) for x in bpts)
        bpts, btop = np.vstack(bpts), np.concatenate(btop)
        # 2. points intérieurs (grille de pas `step`, à distance des murs)
        x0, y0, x1, y1 = poly.bounds
        gx, gy = np.meshgrid(np.arange(x0 + step / 2, x1, step), np.arange(y0 + step / 2, y1, step))
        er = poly.buffer(-step * 0.6)
        inner = np.stack([gx.ravel(), gy.ravel()], 1)
        inner = inner[shapely.contains_xy(er, inner[:, 0], inner[:, 1])] if not er.is_empty else inner[:0]
        zi = self.z(inner[:, 0], inner[:, 1]) if len(inner) else np.zeros(0)
        pts = np.vstack([bpts, inner])
        zs = np.maximum(np.concatenate([btop, zi]), ground + 2.2)
        med = float(np.median(zs))
        # 3. terrasse ou toit à pans
        if force_flat or np.mean(np.abs(zs - med) < 0.45) > 0.62:
            walls(buf, poly.exterior, base, med + 0.8, wall_mat, col, seed, ground)
            for hole in poly.interiors:
                walls(buf, hole, base, med + 0.8, wall_mat, col, seed, ground)
            cap(buf, poly, med + 0.05, flat_mat, col, seed)
            return med - ground, 1.0
        if len(zs) >= 12:
            # bornes égout/faîtage : supprime les « jupes » (bord retombant au sol) et les pics (arbres, antennes)
            lo, hi = np.percentile(zs, [18, 97])
            zs = np.clip(zs, lo - 0.3, hi + 0.2)
            zs = np.maximum(planarize(pts, zs, np.random.default_rng(int(seed * 1e6) + 1)), ground + 2.2)
        # 4. murs jusqu'aux pans (u continu le long de chaque arête, pour la grille de fenêtres)
        for P, ts, L, i0 in edges:
            zt = zs[i0:i0 + len(P)]
            for j in range(len(P) - 1):
                p0, p1 = P[j], P[j + 1]
                z0, z1 = float(zt[j]), float(zt[j + 1])
                u0, u1 = ts[j] - L / 2, ts[j + 1] - L / 2
                buf.quad((p0[0], p0[1], base), (p1[0], p1[1], base), (p1[0], p1[1], z1), (p0[0], p0[1], z0),
                         [u0, base - ground], [u1, base - ground], [u1, z1 - ground], [u0, z0 - ground],
                         [L, min(z0, z1) - ground], wall_mat, col, seed)
        # 5. toit : triangulation des points (bords + intérieur), triangles hors emprise retirés
        try:
            tri = Delaunay(pts).simplices
        except Exception:
            return med - ground, 0.0
        cen = pts[tri].mean(axis=1)
        tri = tri[shapely.contains_xy(poly.buffer(0.01), cen[:, 0], cen[:, 1])]
        nflat = 0
        for t in tri:
            P3 = [(float(pts[k][0]), float(pts[k][1]), float(zs[k])) for k in t]
            a, b, c = np.array(P3)
            nrm = np.cross(b - a, c - a)
            if nrm[2] < 0:
                P3 = [P3[0], P3[2], P3[1]]
                nrm = -nrm
            is_flat = nrm[2] / (np.linalg.norm(nrm) + 1e-9) > 0.985
            nflat += int(is_flat)
            buf.tri(P3, planar_uv(P3), [[0, 0]] * 3, flat_mat if is_flat else roof_mat, col, seed)
        return float(np.median(zs[:nb_total]) - ground), nflat / max(1, len(tri))

WALL_CODE = {"1": "mur_pierre", "2": "mur_pierre", "3": "mur_enduit", "4": "brique", "5": "mur_enduit", "6": "mur_enduit"}
ROOF_CODE = {"1": "toit_tuile", "2": "toit_ardoise", "3": "toit_ardoise", "4": "toit_plat"}
PHOTO_WALL = {"enduit": "mur_enduit", "brique": "mur_brique", "pierre": "mur_pierre", "bois": "mur_enduit", "beton": "mur_enduit"}
PHOTO_ROOF = {"tuile": "toit_tuile", "ardoise": "toit_ardoise", "zinc": "toit_ardoise", "plat": "toit_plat"}


def build_buildings(sec: Sector, mnt):
    fc = json.loads((sec.raw / "batiments.geojson").read_text())
    clip = Polygon([(0, 0), (sec.size, 0), (sec.size, sec.size), (0, sec.size)])
    buf = MeshBuf()
    stats = {"villa_4pans": 0, "villa_2pans": 0, "immeuble": 0, "leger": 0, "annexe": 0}
    footprints = []
    lidar = LidarRoofs(sec, mnt)
    surf = LidarSurface(sec, mnt)
    ff = sec.out / "hq" / "facades.json"
    facades = json.loads(ff.read_text()) if ff.exists() else {}
    stats.update({"toit_lidar": 0, "toit_plat_lidar": 0, "facade_photo": 0, "lambrequins": 0, "chainages": 0})
    for f in fc["features"]:
        pr = f["properties"]
        key = pr.get("cleabs") or str(f.get("id"))
        rng = rng_for(key)
        g = shapely.transform(shape(f["geometry"]), lambda c: c - np.array([sec.x0, sec.y0]), include_z=False)
        g = g.intersection(clip)
        if g.is_empty:
            continue
        polys = [p for p in (g.geoms if hasattr(g, "geoms") else [g]) if isinstance(p, Polygon) and p.area > 6]
        ph = facades.get(key)
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
            leger = bool(pr.get("construction_legere"))
            tint = TINTS[rng.integers(len(TINTS))]
            seed = float(rng.random())
            cov = 0.0 if (leger or area < 14) else surf.coverage(poly)
            fit = None if (leger or cov >= 0.55) else lidar.fit(poly, key)
            if cov < 0.55 and not leger and area >= 14 and fit is None and not any(q["cleabs"] == key for q in lidar.qa):
                c_ = poly.centroid
                lidar.qa.append({"cleabs": key, "x": round(c_.x, 1), "y": round(c_.y, 1),
                                 "probleme": "pas d'écho LiDAR sur l'emprise", "couverture": round(cov, 2)})
            if fit:
                if fit["kind"] == "flat":
                    stats["toit_plat_lidar"] += 1
                    gutter = max(ground + 2.4, fit["top"] - 0.6)
                else:
                    gutter = max(ground + 2.4, fit["gutter"])
            h_wall = gutter - ground
            # --- façade : photo Panoramax > BD TOPO > hasard
            fa = list(FA_DEFAULT)
            floors = (ph or {}).get("floors") or pr.get("nombre_d_etages")
            if floors:
                fa[0] = float(np.clip(h_wall / max(1, int(floors)), 2.5, 4.4))
            wcode = str(pr.get("materiaux_des_murs") or "")[:1]
            rcode = str(pr.get("materiaux_de_la_toiture") or "")[:1]
            wall_name = roof_name = None
            if ph:
                stats["facade_photo"] += 1
                tint = hex_rgb(ph.get("wall_color"), tint)
                wall_name = PHOTO_WALL.get(ph.get("wall_material"))
                roof_name = PHOTO_ROOF.get(ph.get("roof_material"))
                fa[1] = 1.0 if ph.get("shop_ground_floor") else 0.0
                st = ph.get("shutter_type")
                fa[2] = {"battants": 1.0, "persiennes": 1.0, "roulants": 2.0, "aucun": 0.0}.get(st, -1.0)
                sc = hex_rgb(ph.get("shutter_color"))
                if sc:
                    fa[3:6] = list(sc)
                elif fa[2] > 0:
                    fa[2] = -1.0
                if ph.get("brick_trim"):
                    fa[6] = 1.0
                    fa[7:10] = list(hex_rgb(ph.get("trim_color"), BRICK))
                lamb = bool(ph.get("lambrequins"))
            else:
                if (pr.get("usage_1") or "") == "Commercial et services" and h_wall > 5:
                    fa[1] = 1.0
                lamb = None
            if wall_name is None and wcode in WALL_CODE:
                wall_name = WALL_CODE[wcode]
                if wall_name == "brique":  # en Arcachon, la brique est souvent enduite avec chaînages apparents
                    wall_name = "mur_brique" if rng.random() < 0.35 else "mur_enduit"
                    if wall_name == "mur_enduit" and rng.random() < 0.6:
                        fa[6] = 1.0
                        fa[7:10] = list(BRICK)
            if roof_name is None and rcode in ROOF_CODE:
                roof_name = ROOF_CODE[rcode]
            if fa[6] > 0:
                stats["chainages"] += 1
            buf.cur_fa = fa
            if leger:
                stats["leger"] += 1
                walls(buf, poly.exterior, base, gutter, M["mur_leger"], (0.75, 0.75, 0.72), seed, ground)
                cap(buf, poly, gutter, M["toit_plat"], tint, seed)
                continue
            if cov >= 0.55:
                wm = M[wall_name] if wall_name in M else M["mur_enduit"]
                rm = M[roof_name] if roof_name in M and roof_name != "toit_plat" else M["toit_tuile"]
                hw, flat_share = surf.build(buf, poly, base, ground, wm, rm, M["toit_plat"], tint, seed,
                                            force_flat=(roof_name == "toit_plat"))
                if floors:  # hauteur d'étage recalculée sur le mur réellement relevé
                    buf.cur_fa[0] = float(np.clip(hw / max(1, int(floors)), 2.5, 4.4))
                stats["toit_lidar"] += 1
                stats["toit_plat_lidar"] += int(flat_share > 0.6)
                if lamb if lamb is not None else (h_wall < 9 and area < 260 and flat_share < 0.3 and rng.random() < 0.15):
                    stats["lambrequins"] += 1
                    rg = np.array(poly.exterior.coords)
                    zz = surf.z(rg[:, 0], rg[:, 1])
                    for i_ in range(len(rg) - 1):
                        if np.linalg.norm(rg[i_ + 1] - rg[i_]) > 1.5:
                            hang_band(buf, (rg[i_][0], rg[i_][1], zz[i_]), (rg[i_ + 1][0], rg[i_ + 1][1], zz[i_ + 1]), 0.32, tint, seed)
                continue
            flat = fit["kind"] == "flat" if fit else (h_wall > 12.5 or area > 450 or rect_ratio < 0.6 or area > 900)
            if flat:
                stats["immeuble"] += 1
                wm = M[wall_name] if wall_name in M and wall_name != "mur_brique" else (M["mur_enduit"] if rng.random() < 0.85 else M["mur_pierre"])
                walls(buf, poly.exterior, base, gutter + 0.8, wm, tint, seed, ground)  # acrotère
                for hole in poly.interiors:
                    walls(buf, hole, base, gutter + 0.8, wm, tint, seed, ground)
                cap(buf, poly, gutter + 0.05, M["toit_plat"], tint, seed)
                if not fit and h_wall > 14 and rng.random() < 0.4:
                    inner = poly.buffer(-2.0)
                    if isinstance(inner, Polygon) and inner.area > 40:
                        walls(buf, inner.exterior, gutter, gutter + 3.0, wm, tint, seed, ground)
                        cap(buf, inner, gutter + 3.05, M["toit_plat"], tint, seed)
                continue
            # --- toits en pente (villas, maisons, immeubles à toiture)
            small = area < 45 and h_wall < 4.5
            if wall_name in M:
                wall_mat = M[wall_name]
            else:
                r = rng.random()
                wall_mat = M["mur_enduit"] if r < 0.72 else (M["mur_brique"] if r < 0.87 else M["mur_pierre"])
            if roof_name in M and roof_name != "toit_plat":
                roof_mat = M[roof_name]
            else:
                roof_mat = M["toit_tuile"] if rng.random() < 0.78 else M["toit_ardoise"]
            if fit:
                kind, rh, short = fit["kind"], fit["ridge_h"], fit.get("short", False)
            else:
                _, _, _, _, W, _ = rect_frame(poly)
                pitch = math.radians(rng.uniform(32, 48) if roof_mat == M["toit_ardoise"] else rng.uniform(25, 36))
                rh = W / 2 * math.tan(pitch)
                if ridge and 0.8 < ridge - gutter < 9:
                    rh = ridge - gutter
                rh = float(np.clip(rh, 0.8, 7.5))
                kind, short = ("gable" if (small or rng.random() < 0.35) else "hip"), False
            if lamb is None:  # sans photo : lambrequins sur une part des villas
                lamb = (not small) and h_wall < 10 and rng.random() < 0.3
            lamb = bool(lamb) and not small
            stats["lambrequins"] += int(lamb)
            stats["annexe" if small else ("villa_2pans" if kind == "gable" else "villa_4pans")] += 1
            walls(buf, poly.exterior, base, gutter, wall_mat, tint, seed, ground)
            pitched_roof(buf, poly, gutter, rh, kind, 0.45 if not small else 0.2, roof_mat, wall_mat, tint, seed, ground,
                         ridge_short=short, lambrequins=lamb)
    (sec.out / "hq" / "qa_lidar.json").write_text(json.dumps(lidar.qa, ensure_ascii=False, indent=1))
    stats["qa_trous_lidar"] = len(lidar.qa)
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

    # en ville, tout l'espace entre chaussée et façade est trottoir (OSM ne le dessine presque jamais)
    for e in els:
        t = e.get("tags", {})
        if e["type"] == "way" and "geometry" in e and t.get("highway") in ("residential", "tertiary", "secondary", "primary",
                                                                            "unclassified", "living_street"):
            pts = osm_local(sec, e["geometry"])
            if len(pts) >= 2:
                w = ROAD_W[t["highway"]]
                walks.append(LineString(pts).buffer(w / 2 + 4.5, cap_style=2))
    if walks and (sec.raw / "batiments.geojson").exists():
        blds = [shapely.transform(shape(f["geometry"]), lambda c: c - np.array([sec.x0, sec.y0]), include_z=False)
                for f in json.loads((sec.raw / "batiments.geojson").read_text())["features"]]
        bu = unary_union([b.buffer(0) for b in blds] + grass)
        walks = [unary_union(walks).difference(bu)]
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


# ------------------------------------------------------------------ détails de rue (données réelles)
def build_details(sec: Sector, mnt, footprints, water_level):
    """Ce qui fait passer de la maquette à la rue : marquages au sol, passages piétons, lignes d'arrêt,
    bordures de trottoir, mobilier réel OSM (bancs, poubelles, terrasses de restaurants), plaques d'égout,
    arbustes relevés au LiDAR, pinasses au mouillage. Tout est placé depuis des données ouvertes."""
    els = json.loads((sec.raw / "osm.json").read_text())["elements"] if (sec.raw / "osm.json").exists() else []
    rng = np.random.default_rng(23)
    lines = []  # (LineString, largeur, type, voies, sens unique)
    road_polys, walk_polys = [], []
    for e in els:
        t = e.get("tags", {})
        if e["type"] != "way" or "geometry" not in e:
            continue
        hw = t.get("highway")
        pts = osm_local(sec, e["geometry"])
        if len(pts) < 2:
            continue
        if hw in ROAD_W:
            w = float(t.get("width", ROAD_W[hw])) if str(t.get("width", "")).replace(".", "").isdigit() else ROAD_W[hw]
            ln = LineString(pts)
            lanes = int(t["lanes"]) if str(t.get("lanes", "")).isdigit() else None
            lines.append((ln, w, hw, lanes, t.get("oneway") == "yes"))
            road_polys.append(ln.buffer(w / 2, cap_style=2))
            if hw not in ("service", "cycleway"):
                walk_polys.append(ln.buffer(w / 2 + 1.9, cap_style=2))
        elif hw in WALK_W:
            walk_polys.append(LineString(pts).buffer(WALK_W[hw] / 2, cap_style=2))
    tree = shapely.STRtree([l[0] for l in lines]) if lines else None

    def nearest_road(x, y, maxd=12.0):
        if tree is None:
            return None
        p = shapely.Point(x, y)
        i = tree.nearest(p)
        ln, w, hw, lanes, ow = lines[int(i)]
        if ln.distance(p) > maxd:
            return None
        s_ = ln.project(p)
        a, b = ln.interpolate(max(0, s_ - 1)), ln.interpolate(min(ln.length, s_ + 1))
        d = np.array([b.x - a.x, b.y - a.y]); d /= np.linalg.norm(d) + 1e-9
        c = ln.interpolate(s_)
        return np.array([c.x, c.y]), d, w, hw, lanes, ow

    res = 0.25
    N = int(sec.size / res)
    mark = Image.new("L", (N, N), 0)
    dm = ImageDraw.Draw(mark)
    P = lambda q: (q[0] / res, (sec.size - q[1]) / res)

    def rect(c, d, half_len, half_wid):
        nrm = np.array([-d[1], d[0]])
        pts = [c + d * half_len + nrm * half_wid, c - d * half_len + nrm * half_wid,
               c - d * half_len - nrm * half_wid, c + d * half_len - nrm * half_wid]
        dm.polygon([P(q) for q in pts], fill=255)

    n_center = 0
    for ln, w, hw, lanes, ow in lines:
        two_way = (lanes or 0) >= 2 or (hw in ("primary", "secondary", "tertiary") and not ow and lanes != 1)
        if not two_way or ln.length < 12:
            continue
        for s_ in np.arange(4, ln.length - 4, 8.0):  # tirets de 3 m, intervalle 5 m
            a, b = ln.interpolate(s_), ln.interpolate(min(ln.length, s_ + 3))
            dm.line([P((a.x, a.y)), P((b.x, b.y))], fill=255, width=1)
            n_center += 1
    n_zebra = n_stop = 0
    props = []
    for e in els:
        if e["type"] != "node":
            continue
        t = e.get("tags", {})
        x, y = to_l93.transform(e["lon"], e["lat"])
        x, y = x - sec.x0, y - sec.y0
        if not (0 <= x <= sec.size and 0 <= y <= sec.size):
            continue
        z = sample_height(mnt, sec, x, y)
        hw, am = t.get("highway"), t.get("amenity")
        if hw == "crossing" and t.get("crossing") not in ("unmarked", "no"):
            nr = nearest_road(x, y, 6)
            if nr:
                c, d, w, *_ = nr
                nrm = np.array([-d[1], d[0]])
                for o in np.arange(-w / 2 + 0.5, w / 2 - 0.3, 1.0):  # bandes de 0,5 m, longues de 2,5 m
                    rect(c + nrm * o, d, 1.25, 0.25)
                n_zebra += 1
        elif hw in ("stop", "give_way", "traffic_signals"):
            nr = nearest_road(x, y, 8)
            if nr:
                c, d, w, *_ = nr
                nrm = np.array([-d[1], d[0]])
                if hw == "give_way":
                    for o in np.arange(-w / 2 + 0.3, w / 2 - 0.3, 1.0):
                        rect(c + nrm * o, d, 0.25, 0.25)
                else:
                    rect(c, d, 0.25, w / 2 - 0.2)
                n_stop += 1
        if am == "bench" or t.get("leisure") == "picnic_table":
            nr = nearest_road(x, y, 25)
            ang = math.atan2(nr[1][1], nr[1][0]) if nr else rng.uniform(0, 6.28)
            props.append(["painted_wooden_bench", round(x, 2), round(y, 2), round(z, 2), round(ang, 3)])
        elif am in ("waste_basket", "waste_disposal"):
            props.append(["metal_trash_can", round(x, 2), round(y, 2), round(z, 2), round(float(rng.uniform(0, 6.28)), 3)])
        elif am in ("restaurant", "cafe", "bar", "pub", "ice_cream", "fast_food"):
            nr = nearest_road(x, y, 30)
            if nr:
                c, d, w, *_ = nr
                nrm = np.array([-d[1], d[0]])
                side = 1 if np.dot(np.array([x, y]) - c, nrm) > 0 else -1
                base = c + nrm * side * (w / 2 + 1.5)
                for k in range(int(rng.integers(2, 4))):  # terrasse : 2-3 tables sur le trottoir
                    q = base + d * (k - 1) * 2.4
                    if any(f.contains(shapely.Point(*q)) for f in footprints[:0]):
                        continue
                    props.append(["outdoor_table_chair_set_01", round(float(q[0]), 2), round(float(q[1]), 2),
                                  round(sample_height(mnt, sec, *q), 2), round(math.atan2(d[1], d[0]) + rng.uniform(-0.3, 0.3), 3)])
        elif hw == "street_lamp":
            props.append(["street_lamp_02", round(x, 2), round(y, 2), round(z, 2), round(float(rng.uniform(0, 6.28)), 3)])
        elif t.get("emergency") == "fire_hydrant":
            props.append(["fire_hydrant", round(x, 2), round(y, 2), round(z, 2), 0.0])
    # plaques d'égout (détail vu sur toutes les photos de rue)
    for ln, w, hw, lanes, ow in lines:
        if hw in ("service", "cycleway") or ln.length < 20:
            continue
        for s_ in np.arange(rng.uniform(8, 20), ln.length - 5, 38):
            q = ln.interpolate(s_)
            props.append(["water_manhole_cover", round(q.x, 2), round(q.y, 2), round(sample_height(mnt, sec, q.x, q.y) - 0.04, 2),
                          round(float(rng.uniform(0, 6.28)), 3)])
    # bordures : limite chaussée / trottoir
    curbs = []
    if road_polys and walk_polys:
        R = unary_union(road_polys)
        Wk = unary_union(walk_polys).buffer(0.3)
        edge = R.boundary.intersection(Wk)
        for g in (edge.geoms if hasattr(edge, "geoms") else [edge]):
            if g.geom_type == "LineString" and g.length > 1.5:
                g = g.segmentize(2.0)
                cs = np.array(g.coords)
                curbs.append([[round(float(a), 2), round(float(b), 2), round(sample_height(mnt, sec, a, b), 2)] for a, b in cs])
    # arbustes : végétation basse du LiDAR (0,6-2,5 m), hors bâti et hors voirie
    mnh = np.load(sec.raw / "mnh.npy")
    r_ = sec.size / mnh.shape[0]
    blocked = unary_union(footprints + road_polys) if (footprints or road_polys) else None
    low = (mnh > 0.6) & (mnh < 2.5)
    pk = low & (ndimage.maximum_filter(np.where(low, mnh, 0), size=5) == mnh)
    ys, xs = np.nonzero(pk)
    sel = rng.permutation(len(xs))[:6000]
    n_shrub = 0
    for i in sel:
        x, y = (xs[i] + 0.5) * r_, sec.size - (ys[i] + 0.5) * r_
        if blocked is not None and blocked.contains(shapely.Point(x, y)):
            continue
        z = sample_height(mnt, sec, x, y)
        if z < water_level + 0.5:
            continue
        sh = float(mnh[ys[i], xs[i]])
        props.append([f"shrub_0{1 + int(rng.integers(3))}", round(x, 2), round(y, 2), round(z, 2),
                      round(float(rng.uniform(0, 6.28)), 3), round(sh, 2)])
        n_shrub += 1
        if n_shrub >= 2500:
            break
    # pinasses : pas de modèle fidèle en CC0 (le « ship_pinnace » de Poly Haven est un galion).
    # À générer en image→3D multi-vues (cf. docs/ressources) puis à placer au mouillage.
    boats = 0
    # assemblage : marquages dans le canal B du masque b
    mb = Image.open(sec.out / "hq" / "ground_mask_b.png").convert("RGB")
    r0, g0, _ = mb.split()
    Image.merge("RGB", (r0, g0, mark)).save(sec.out / "hq" / "ground_mask_b.png")
    old = json.loads((sec.out / "hq" / "props.json").read_text())
    n_osm_bench = sum(p_[0] == "painted_wooden_bench" for p_ in props)
    if n_osm_bench >= 20:  # bancs réels OSM : on retire les bancs inventés
        old = [p_ for p_ in old if p_[0] != "painted_wooden_bench"]
    (sec.out / "hq" / "props.json").write_text(json.dumps(old + props))
    (sec.out / "hq" / "curbs.json").write_text(json.dumps(curbs))
    log(f"  détails : {n_center} tirets axiaux, {n_zebra} passages piétons, {n_stop} lignes d'arrêt, {len(curbs)} bordures, "
        f"{n_shrub} arbustes LiDAR, {boats} pinasses, {len(props)} objets réels")


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
    progress(0.9, "détails de rue")
    build_details(sec, mnt, fps, water_level)
    info = {"buildings": stats, "trees": n_trees, "water_level_m": water_level}
    (sec.out / "hq" / "hq_manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    progress(1, "terminé")
    log("HQ prêt")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

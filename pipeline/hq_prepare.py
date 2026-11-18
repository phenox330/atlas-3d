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
import facade_geo as fg  # noqa: E402
from atlas import WMS, Sector, get, log, progress, sample_height, to_l93, wms_params  # noqa: E402

MNH_LAYER = "IGNF_LIDAR-HD_MNH_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNT_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNT_FALLBACK = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
ORTHO_LAYER = "HR.ORTHOIMAGERY.ORTHOPHOTOS"

# matériaux (index) — repris tels quels par blender_hq.py
MATS = ["mur_enduit", "mur_brique", "mur_pierre", "toit_tuile", "toit_ardoise", "toit_plat", "mur_leger", "lambrequin",
        "vitre", "peint", "volet", "garde_corps", "toile"]
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

    if not (raw / "mid_mns.npy").exists():
        # couronne de 3 km : surface LiDAR (sol + bâti + arbres) à 2 m et orthophoto à 0,5 m → alentours en relief
        log("Couronne 3 km (surface LiDAR 2 m + ortho 0,5 m) et ortho 8 km à 1 m…")
        cx, cy = (sec.x0 + sec.x1) / 2, (sec.y0 + sec.y1) / 2
        R = 1500
        bb = (cx - R, cy - R, cx + R, cy + R)
        t_ = fetch_grid(MNT_LAYER, bb, 1500, 1500, MNT_FALLBACK)
        t_[t_ < -1000] = -3
        try:
            h_ = fetch_grid(MNH_LAYER, bb, 1500, 1500)
            h_[(h_ < -1000) | (h_ > 60)] = 0
        except Exception:
            h_ = np.zeros_like(t_)
        np.save(raw / "mid_mns.npy", (t_ + np.maximum(h_, 0)).astype(np.float32))
        np.save(raw / "mid_mnt.npy", t_.astype(np.float32))

        def mosaic(bbox, n, px, path):
            img = Image.new("RGB", (n * px, n * px))
            st = (bbox[2] - bbox[0]) / n
            for iy in range(n):
                for ix in range(n):
                    b2 = (bbox[0] + ix * st, bbox[1] + iy * st, bbox[0] + (ix + 1) * st, bbox[1] + (iy + 1) * st)
                    r = get(WMS, wms_params(ORTHO_LAYER, b2, px, px, "image/jpeg"))
                    img.paste(Image.open(BytesIO(r.content)).convert("RGB"), (ix * px, (n - 1 - iy) * px))
            img.save(path, quality=90)
        mosaic(bb, 3, 2000, raw / "mid_ortho.jpg")
        R8 = 4000
        mosaic((cx - R8, cy - R8, cx + R8, cy + R8), 4, 2000, raw / "ctx_ortho_1m.jpg")
        (raw / "mid.json").write_text(json.dumps({"bbox_l93": bb, "origin_l93": [sec.x0, sec.y0]}))

    if not (raw / "near_mns.npy").exists() and (raw / "mid_ortho.jpg").exists():
        # abords immédiats (600 m autour du secteur) : surface LiDAR à 1 m, pour que les bâtiments voisins gardent des arêtes nettes
        log("Abords 600 m (surface LiDAR 1 m)…")
        cx, cy = (sec.x0 + sec.x1) / 2, (sec.y0 + sec.y1) / 2
        R = sec.size / 2 + 600
        n_ = int(2 * R)
        bb = (cx - R, cy - R, cx + R, cy + R)
        t_ = fetch_grid(MNT_LAYER, bb, n_, n_, MNT_FALLBACK)
        t_[t_ < -1000] = -3
        try:
            h_ = fetch_grid(MNH_LAYER, bb, n_, n_)
            h_[(h_ < -1000) | (h_ > 60)] = 0
        except Exception:
            h_ = np.zeros_like(t_)
        np.save(raw / "near_mns.npy", (t_ + np.maximum(h_, 0)).astype(np.float32))
        np.save(raw / "near_mnt.npy", t_.astype(np.float32))
        mo = Image.open(raw / "mid_ortho.jpg")
        k = mo.size[0] / 3000.0
        c0 = int((1500 - R) * k)
        mo.crop((c0, c0, mo.size[0] - c0, mo.size[1] - c0)).save(raw / "near_ortho.jpg", quality=90)
        (raw / "near.json").write_text(json.dumps({"bbox_l93": bb, "origin_l93": [sec.x0, sec.y0]}))


# ------------------------------------------------------------------ bâtiments
# Vecteur « façade » par face (lu par le shader de mur) :
# [hauteur d'étage (0 = défaut), vitrine RDC (-1 hasard / 0 / 1), volets (-1 hasard, 0 aucun, 1 battants, 2 roulants),
#  couleur volets r g b, chaînages brique (0/1), couleur chaînages r g b]
FA_DEFAULT = [0.0, -1.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.55, 0.24, 0.17, 0.0]  # dernier : 1 = baies en géométrie
BRICK = (0.62, 0.27, 0.18)


class MeshBuf:
    def __init__(self):
        self.v, self.f, self.uv0, self.uv1, self.mat, self.col, self.seed = [], [], [], [], [], [], []
        self.fa, self.cur_fa = [], list(FA_DEFAULT)
        self.lod, self.cur_lod = [], 0  # 0 = enveloppe (murs, toits, vitrages), 1 = détail (menuiseries, volets…)

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
        self.lod.append(self.cur_lod)

    def quad(self, a, b, c, d, uva, uvb, uvc, uvd, uv1, mat, col, seed):
        self.tri([a, b, c], [uva, uvb, uvc], [uv1] * 3, mat, col, seed)
        self.tri([a, c, d], [uva, uvc, uvd], [uv1] * 3, mat, col, seed)

    def save(self, path):
        np.savez_compressed(path, v=np.array(self.v, np.float32), f=np.array(self.f, np.int32),
                            uv0=np.array(self.uv0, np.float32), uv1=np.array(self.uv1, np.float32),
                            mat=np.array(self.mat, np.int16), col=np.array(self.col, np.float32),
                            seed=np.array(self.seed, np.float32), fa=np.array(self.fa, np.float32), lod=np.array(self.lod, np.uint8), mats=np.array(MATS))


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
        return tuple((int(h[i:i + 2], 16) / 255) ** 2.2 for i in (0, 2, 4))  # sRGB (photo) → linéaire (rendu)
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

    def facets(self, buf, poly, base, ground, wall_mat, roof_mat, flat_mat, col, seed, fac):
        """Toit en facettes planes : les points LiDAR sont regroupés en pans (RANSAC), chaque pan devient un
        polygone aux bords droits (simplification de couverture : les pans voisins gardent leur arête commune),
        les décrochés entre pans deviennent des murs verticaux, et le haut des murs extérieurs suit exactement
        les pans. Renvoie (hauteur de mur, part de toit plat) ou None si le relevé est insuffisant."""
        res = self.res
        x0, y0, x1, y1 = poly.bounds
        pad = 3
        gx0, gy0 = x0 - pad * res, y0 - pad * res
        nc, nr = int(math.ceil((x1 - x0) / res)) + 2 * pad, int(math.ceil((y1 - y0) / res)) + 2 * pad
        if nc * nr > 400000:
            return None
        cx = gx0 + (np.arange(nc) + 0.5) * res
        cy = gy0 + (np.arange(nr) + 0.5) * res
        CX, CY = np.meshgrid(cx, cy)
        inside = shapely.contains_xy(poly, CX, CY)
        Z = self.z(CX.ravel(), CY.ravel()).reshape(nr, nc)
        valid = inside & (Z - ground > 2.0)
        if valid.sum() < 12:
            return None
        zv = Z[valid]
        zlo, zhi = np.percentile(zv, [1.5, 99.5])
        valid &= Z <= zhi + 1.2
        pts = np.column_stack([CX[valid], CY[valid]])
        zv = Z[valid]
        n = len(zv)
        rng = np.random.default_rng(int(seed * 1e6) + 7)
        # --- pans : hypothèse = plan tangent local (pente mesurée autour d'un point tiré au hasard). Un point
        # n'adhère à un pan que s'il est proche du plan ET orienté comme lui : sans ce second critère, un plan
        # horizontal « tranche » les toits en pente le long des courbes de niveau (toits en marches d'escalier).
        Zs = ndimage.uniform_filter(np.where(valid, Z, np.nan_to_num(Z)), 3)
        gy_, gx_ = np.gradient(Zs, res)
        gy_ = -gy_  # les lignes croissent vers le nord dans cette grille → déjà y croissant ; signe rétabli ci-dessous
        gy_ = -gy_
        # gradient fiable seulement si tout le voisinage 3×3 est valide (pas de bord, pas de décroché)
        solid = ndimage.minimum_filter(valid.astype(np.uint8), 3).astype(bool)
        rough_ = ndimage.maximum_filter(Zs, 3) - ndimage.minimum_filter(Zs, 3)
        G = np.column_stack([gx_[valid], gy_[valid]])
        gok = (solid & (rough_ < 1.6))[valid]
        o = pts.mean(0)
        Pm = np.column_stack([pts - o, np.ones(n)])
        lab = np.full(n, -1)
        planes = []
        free = gok.copy()
        min_in = max(8, int(0.02 * n))
        thr, gthr = 0.22, 0.22
        for _ in range(int(np.clip(4 + poly.area / 40, 6, 36))):
            idx = np.nonzero(free)[0]
            if len(idx) < min_in:
                break
            best, best_cnt = None, 0
            for sidx in idx[rng.choice(len(idx), min(60, len(idx)), replace=False)]:
                g0 = G[sidx]
                if np.hypot(*g0) > 1.8:
                    continue
                coef = np.array([g0[0], g0[1], zv[sidx] - Pm[sidx, :2] @ g0])
                m_ = (np.abs(Pm[idx] @ coef - zv[idx]) < thr) & (np.abs(G[idx] - g0).max(1) < gthr)
                cnt = int(m_.sum())
                if cnt > best_cnt:
                    best, best_cnt = coef, cnt
            if best is None or best_cnt < min_in:
                break
            for _it in range(2):  # réajustement moindres carrés sur les adhérents
                m_ = (np.abs(Pm[idx] @ best - zv[idx]) < thr) & (np.abs(G[idx] - best[:2]).max(1) < gthr)
                if m_.sum() < 6:
                    break
                best, *_ = np.linalg.lstsq(Pm[idx[m_]], zv[idx[m_]], rcond=None)
            m_ = (np.abs(Pm[idx] @ best - zv[idx]) < thr * 1.3) & (np.abs(G[idx] - best[:2]).max(1) < gthr * 1.3)
            inl = idx[m_]
            if len(inl) < min_in:
                free[idx[(np.abs(Pm[idx] @ best - zv[idx]) < thr)]] = False
                continue
            coef = best
            if np.hypot(coef[0], coef[1]) < 0.07:  # pan quasi horizontal : on le met à plat
                coef = np.array([0.0, 0.0, float(np.median(zv[inl]))])
            lab[inl] = len(planes)
            planes.append(coef)
            free[inl] = False
        if not planes:
            return None
        PL = np.array(planes)
        rest = np.nonzero(lab < 0)[0]
        if len(rest):
            R = np.abs(Pm[rest] @ PL.T - zv[rest][:, None])
            k = R.argmin(1)
            ok = R[np.arange(len(rest)), k] < 0.7
            lab[rest[ok]] = k[ok]
        Lr = np.full((nr, nc), -1)
        Lr[valid] = lab
        # --- lissage des étiquettes (vote 3×3), suppression des miettes, remplissage par le pan le plus proche
        K = len(planes)
        votes = np.stack([ndimage.uniform_filter((Lr == k).astype(np.float32), 3, mode="constant") for k in range(K)])
        sm = votes.argmax(0)
        Lr = np.where((Lr >= 0) & (votes.max(0) > 0), sm, Lr)
        for k in range(K):
            cc, ncomp = ndimage.label(Lr == k)
            if ncomp:
                sizes = ndimage.sum(np.ones_like(cc), cc, index=np.arange(1, ncomp + 1))
                small = np.isin(cc, np.nonzero(sizes < 14)[0] + 1)
                Lr[small] = -1
        if (Lr >= 0).sum() < 8:
            return None
        _, (ir, ic) = ndimage.distance_transform_edt(Lr < 0, return_indices=True)
        Lr = Lr[ir, ic]
        # --- un polygone par composante de pan, bords simplifiés en couverture (arêtes communes conservées)
        geoms, gk = [], []
        for k in np.unique(Lr):
            rr, cc_ = np.nonzero(Lr == k)
            bx = shapely.box(gx0 + cc_ * res, gy0 + rr * res, gx0 + (cc_ + 1) * res, gy0 + (rr + 1) * res)
            u = shapely.union_all(bx)
            for g in (u.geoms if hasattr(u, "geoms") else [u]):
                geoms.append(g)
                gk.append(int(k))
        try:
            simp = shapely.coverage_simplify(np.array(geoms, dtype=object), 0.75, simplify_boundary=False)
        except Exception:
            simp = geoms
        fpolys, fks = [], []
        for g, k in zip(simp, gk):
            c = g.intersection(poly)
            for q in (c.geoms if hasattr(c, "geoms") else [c]):
                if isinstance(q, Polygon) and q.area > 0.05:
                    fpolys.append(q)
                    fks.append(k)
        if not fpolys:
            return None
        zmin_c, zmax_c = max(zlo - 0.4, ground + 2.2), zhi + 0.5

        def zat(k, x, y):
            a, b, c = PL[k]
            return np.clip(a * (np.asarray(x) - o[0]) + b * (np.asarray(y) - o[1]) + c, zmin_c, zmax_c)

        # --- murs extérieurs : le haut du mur suit le pan qui arrive sur chaque portion d'arête
        ftree = shapely.STRtree(fpolys)
        rings_tops = []
        all_tops = []
        for r_ in [poly.exterior] + list(poly.interiors):
            rc = np.array(r_.coords)[:, :2]
            tops = []
            for i in range(len(rc) - 1):
                a, b = rc[i], rc[i + 1]
                L = float(np.linalg.norm(b - a))
                if L < 0.05:
                    tops.append(None)
                    continue
                d = (b - a) / L
                seg = LineString([a, b])
                pieces = []
                for j in ftree.query(seg):
                    it = seg.intersection(fpolys[j])
                    for q in (it.geoms if hasattr(it, "geoms") else [it]):
                        if q.geom_type != "LineString" or q.length < 0.02:
                            continue
                        qc = np.array(q.coords)
                        t0, t1 = sorted((float((qc[0] - a) @ d), float((qc[-1] - a) @ d)))
                        pieces.append((t0, t1, int(j)))
                if not pieces:
                    tops.append(None)
                    continue
                pieces.sort()
                ts, zt = [], []
                for t0, t1, j in pieces:
                    t0 = max(t0, ts[-1] + 1e-4) if ts else max(0.0, t0)
                    if t1 - t0 < 1e-3:
                        continue
                    for t in (t0, t1):
                        q = a + d * t
                        ts.append(t)
                        zt.append(float(zat(fks[j], q[0], q[1])))
                if len(ts) < 2:
                    tops.append(None)
                    continue
                ts[0], ts[-1] = 0.0, L
                tops.append((np.array(ts), np.array(zt)))
                all_tops += zt
            rings_tops.append((rc, tops))
        if not all_tops:
            return None
        hw = float(np.median(all_tops)) - ground
        fg.refit(fac["s"], hw, buf.cur_fa[0] if fac.get("floors") else 0.0)
        for q, (rc, tops) in enumerate(rings_tops):
            med_top = float(np.median(all_tops))
            tops2 = [t if t is not None else (np.array([0.0, float(np.linalg.norm(rc[i + 1] - rc[i]))]), np.array([med_top, med_top]))
                     for i, t in enumerate(tops)]
            fg.facade_ring(buf, M, fac["city"], rc, tops2, base, ground, wall_mat, col, seed, fac["s"], fac["rng"], allow_door=(q == 0))
            fg.eaves(buf, M, fac["city"], rc, tops, roof_mat, col, seed, fac["s"], planar_uv, ground=ground)
        # --- pans de toit
        a_flat = a_tot = 0.0
        for fp, k in zip(fpolys, fks):
            rings = [np.array(fp.exterior.coords)[:-1, :2]] + [np.array(r.coords)[:-1, :2] for r in fp.interiors]
            verts = np.vstack(rings).astype(np.float64)
            ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
            try:
                idx = earcut.triangulate_float64(verts, ends).reshape(-1, 3)
            except Exception:
                continue
            zz = zat(k, verts[:, 0], verts[:, 1])
            is_flat = np.hypot(PL[k][0], PL[k][1]) < 0.09
            a_tot += fp.area
            a_flat += fp.area if is_flat else 0.0
            m_ = flat_mat if (is_flat and fp.area > 5) else roof_mat
            for t in idx:
                P3 = [(float(verts[i][0]), float(verts[i][1]), float(zz[i])) for i in t]
                a_, b_, c_ = np.array(P3)
                if np.cross(b_ - a_, c_ - a_)[2] < 0:
                    P3 = [P3[0], P3[2], P3[1]]
                buf.tri(P3, planar_uv(P3), [[0, 0]] * 3, m_, col, seed)
        # --- décrochés entre pans voisins : murs verticaux
        li, lj = ftree.query(np.array(fpolys, dtype=object), predicate="intersects")
        for i, j in zip(li, lj):
            if i >= j or fks[i] == fks[j]:
                continue
            it = fpolys[i].boundary.intersection(fpolys[j].boundary)
            for q in (it.geoms if hasattr(it, "geoms") else [it]):
                if q.geom_type != "LineString":
                    continue
                qc = np.array(q.coords)
                zi, zj = zat(fks[i], qc[:, 0], qc[:, 1]), zat(fks[j], qc[:, 0], qc[:, 1])
                for m in range(len(qc) - 1):
                    if max(abs(zi[m] - zj[m]), abs(zi[m + 1] - zj[m + 1])) < 0.1:
                        continue
                    p, r = qc[m], qc[m + 1]
                    Ls = float(np.linalg.norm(r - p))
                    buf.quad((p[0], p[1], float(zi[m])), (r[0], r[1], float(zi[m + 1])), (r[0], r[1], float(zj[m + 1])), (p[0], p[1], float(zj[m])),
                             [0, zi[m] - ground], [Ls, zi[m + 1] - ground], [Ls, zj[m + 1] - ground], [0, zj[m] - ground], [0, 0], wall_mat, col, seed)
        # --- souches de cheminée sur les pans inclinés
        if a_tot > 45 and a_flat / max(a_tot, 1) < 0.5:
            rng2 = fac["rng"]
            pit = [(fp, k) for fp, k in zip(fpolys, fks) if np.hypot(PL[k][0], PL[k][1]) > 0.2 and fp.area > 12]
            for _ in range(1 if a_tot < 140 else 2):
                if pit and rng2.random() < 0.8:
                    fp, k = pit[rng2.integers(len(pit))]
                    c_ = fp.representative_point()
                    fg.chimney(buf, M, c_.x, c_.y, float(zat(k, c_.x, c_.y)), col, seed, rng2, brick=rng2.random() < 0.6)
        return hw, a_flat / max(a_tot, 1e-6)

    def build(self, buf, poly, base, ground, wall_mat, roof_mat, flat_mat, col, seed, step=1.0, inset=0.6, force_flat=False, fac=None):
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
            edges.append((P, ts, L, i0, i))
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
            if fac:
                fac["s"]["modern"] = fac["s"]["modern"] or not fac["s"]["villa"]
                fg.refit(fac["s"], med - ground, 0.0)
                for r_ in [poly.exterior] + list(poly.interiors):
                    fg.facade_ring(buf, M, fac["city"], np.array(r_.coords), med + 0.8, base, ground, wall_mat, col, seed, fac["s"], fac["rng"],
                                   allow_door=r_ is poly.exterior)
                    fg.coping(buf, M, fac["city"], np.array(r_.coords), med + 0.8, col, seed)
            else:
                walls(buf, poly.exterior, base, med + 0.8, wall_mat, col, seed, ground)
                for hole in poly.interiors:
                    walls(buf, hole, base, med + 0.8, wall_mat, col, seed, ground)
            cap(buf, poly, med + 0.05, flat_mat, col, seed)
            return med - ground, 1.0
        if fac:
            try:
                r_ = self.facets(buf, poly, base, ground, wall_mat, roof_mat, flat_mat, col, seed, fac)
            except Exception as e:  # relevé inexploitable : on retombe sur la triangulation simple
                log(f"  facettes impossibles ({e})")
                r_ = None
            if r_ is not None:
                return r_
        if len(zs) >= 12:
            # bornes égout/faîtage : supprime les « jupes » (bord retombant au sol) et les pics (arbres, antennes)
            lo, hi = np.percentile(zs, [18, 97])
            zs = np.clip(zs, lo - 0.3, hi + 0.2)
            zs = np.maximum(planarize(pts, zs, np.random.default_rng(int(seed * 1e6) + 1)), ground + 2.2)
        # 4. murs jusqu'aux pans, avec leurs baies en relief et le débord de toit
        if fac:
            # lignes d'égout régularisées (droites ou pignons), raccordées aux angles
            ends = {}
            for P, ts, L, i0, ei in edges:
                zs[i0:i0 + len(P)] = fg.regular_top(ts, zs[i0:i0 + len(P)])
                ends[ei] = (i0, i0 + len(P) - 1)
            eis = sorted(ends)
            for q, ei in enumerate(eis):
                nx = eis[(q + 1) % len(eis)]
                if nx == (ei + 1) % n or len(eis) == n:
                    zc = (zs[ends[ei][1]] + zs[ends[nx][0]]) / 2
                    zs[ends[ei][1]] = zs[ends[nx][0]] = zc
            fg.refit(fac["s"], float(np.median(zs[:nb_total])) - ground, buf.cur_fa[0] if fac.get("floors") else 0.0)
            tops = [None] * n
            for P, ts, L, i0, ei in edges:
                tops[ei] = (ts, zs[i0:i0 + len(P)])
            fg.facade_ring(buf, M, fac["city"], ring, tops, base, ground, wall_mat, col, seed, fac["s"], fac["rng"])
            fg.eaves(buf, M, fac["city"], ring, tops, roof_mat, col, seed, fac["s"], planar_uv)
        else:
            for P, ts, L, i0, ei in edges:
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
            nz = nrm[2] / (np.linalg.norm(nrm) + 1e-9)
            is_flat = nz > 0.985
            nflat += int(is_flat)
            if nz < 0.42:  # décroché quasi vertical entre deux niveaux de toit : c'est un mur, pas un pan
                buf.tri(P3, planar_uv(P3), [[0, 0]] * 3, wall_mat, col, seed)
                continue
            buf.tri(P3, planar_uv(P3), [[0, 0]] * 3, flat_mat if is_flat else roof_mat, col, seed)
        # souches de cheminée près du faîtage (toits à pans)
        if fac and len(inner) > 30 and nflat / max(1, len(tri)) < 0.5:
            rng = fac["rng"]
            zi2 = zs[nb_total:]
            hi_ = np.where(zi2 > np.percentile(zi2, 80))[0]
            for _ in range(1 if len(inner) < 120 else 2):
                if len(hi_) and rng.random() < 0.8:
                    k = int(hi_[rng.integers(len(hi_))])
                    fg.chimney(buf, M, inner[k][0], inner[k][1], float(zi2[k]), col, seed, rng, brick=rng.random() < 0.6)
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
    fg.SIGNS.clear()
    stats.update({"toit_lidar": 0, "toit_plat_lidar": 0, "facade_photo": 0, "lambrequins": 0, "chainages": 0})
    # voisinage (mitoyennetés, rues) : décide où percer des baies, où poser portes, vitrines et débords de toit
    allp = []
    for f in fc["features"]:
        g = shapely.transform(shape(f["geometry"]), lambda c: c - np.array([sec.x0, sec.y0]), include_z=False).intersection(clip)
        for poly in [p for p in (g.geoms if hasattr(g, "geoms") else [g]) if isinstance(p, Polygon) and p.area > 6]:
            poly = poly.simplify(0.15)
            if poly.is_valid and not poly.is_empty:
                allp.append(poly.buffer(-0.12))
    road_lines, ped_lines, shop_pts = [], [], []
    if (sec.raw / "osm.json").exists():
        for e in json.loads((sec.raw / "osm.json").read_text())["elements"]:
            tg = e.get("tags", {})
            if e["type"] == "way" and "geometry" in e and (tg.get("highway") in ROAD_W or tg.get("highway") == "pedestrian"):
                pts_ = osm_local(sec, e["geometry"])
                if len(pts_) >= 2:
                    road_lines.append(LineString(pts_))
                    if tg.get("highway") == "pedestrian":
                        ped_lines.append(LineString(pts_))
            elif e["type"] == "node" and (tg.get("shop") or tg.get("amenity") in ("restaurant", "cafe", "bar", "bank", "pharmacy", "fast_food", "ice_cream")):
                x_, y_ = to_l93.transform(e["lon"], e["lat"])
                shop_pts.append(shapely.Point(x_ - sec.x0, y_ - sec.y0))
    city = fg.City([p for p in allp if not p.is_empty], road_lines)
    ped_tree = shapely.STRtree(ped_lines) if ped_lines else None
    shop_tree = shapely.STRtree(shop_pts) if shop_pts else None
    stats["commerces_osm"] = len(shop_pts)
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
            fa[10] = 1.0
            buf.cur_fa = fa
            flat_guess = (roof_name == "toit_plat") or h_wall > 12.5 or area > 450
            # commerce réel OSM à moins de 9 m, ou façade sur rue piétonne : rez-de-chaussée commercial
            if fa[1] < -0.5 and area > 30:
                if shop_tree is not None and shop_pts[int(shop_tree.nearest(poly))].distance(poly) < 9.0:
                    fa[1] = 1.0
                elif ped_tree is not None and ped_lines[int(ped_tree.nearest(poly))].distance(poly) < 10.0 and rng.random() < 0.75:
                    fa[1] = 1.0
            spec = fg.make_spec(rng, fa, ph, h_wall, area, flat_guess, wall_name)
            fac = {"city": city, "s": spec, "rng": rng}
            if leger:
                stats["leger"] += 1
                walls(buf, poly.exterior, base, gutter, M["mur_leger"], (0.75, 0.75, 0.72), seed, ground)
                cap(buf, poly, gutter, M["toit_plat"], tint, seed)
                continue
            if cov >= 0.55:
                wm = M[wall_name] if wall_name in M else M["mur_enduit"]
                rm = M[roof_name] if roof_name in M and roof_name != "toit_plat" else M["toit_tuile"]
                hw, flat_share = surf.build(buf, poly, base, ground, wm, rm, M["toit_plat"], tint, seed,
                                            force_flat=(roof_name == "toit_plat"), fac=fac)
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
                for r_ in [poly.exterior] + list(poly.interiors):  # murs + acrotère
                    fg.facade_ring(buf, M, city, np.array(r_.coords), gutter + 0.8, base, ground, wm, tint, seed, spec, rng,
                                   allow_door=r_ is poly.exterior)
                    fg.coping(buf, M, city, np.array(r_.coords), gutter + 0.8, tint, seed)
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
            fg.facade_ring(buf, M, city, np.array(poly.exterior.coords), gutter, base, ground, wall_mat, tint, seed, spec, rng)
            pitched_roof(buf, poly, gutter, rh, kind, 0.45 if not small else 0.2, roof_mat, wall_mat, tint, seed, ground,
                         ridge_short=short, lambrequins=lamb)
    (sec.out / "hq" / "qa_lidar.json").write_text(json.dumps(lidar.qa, ensure_ascii=False, indent=1))
    stats["qa_trous_lidar"] = len(lidar.qa)
    (sec.out / "hq" / "signs.json").write_text(json.dumps(fg.SIGNS))
    stats["enseignes"] = len(fg.SIGNS)
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
    # un tronc ne pousse pas au milieu de la chaussée : la cime vue par le LiDAR déborde, le pied est sur le trottoir
    rlines = []
    if (sec.raw / "osm.json").exists():
        for e in json.loads((sec.raw / "osm.json").read_text())["elements"]:
            hw_ = e.get("tags", {}).get("highway")
            if e["type"] == "way" and "geometry" in e and hw_ in ROAD_W:
                pts_ = osm_local(sec, e["geometry"])
                if len(pts_) >= 2:
                    rlines.append((LineString(pts_), ROAD_W[hw_]))
    rtree_ = shapely.STRtree([l for l, _ in rlines]) if rlines else None
    n_moved = 0
    for py, px in zip(ys, xs):
        x, y = (px + 0.5) * res, sec.size - (py + 0.5) * res
        if rtree_ is not None:
            p_ = shapely.Point(x, y)
            ln, w_ = rlines[int(rtree_.nearest(p_))]
            dd = ln.distance(p_)
            if dd < w_ / 2 + 1.3:
                c_ = ln.interpolate(ln.project(p_))
                v_ = np.array([x - c_.x, y - c_.y])
                nv = np.linalg.norm(v_)
                if nv < 0.3:
                    continue
                x, y = (np.array([c_.x, c_.y]) + v_ / nv * (w_ / 2 + 1.6)).tolist()
                if any(fp.distance(shapely.Point(x, y)) < 0.8 for fp in footprints if abs(fp.centroid.x - x) < 40 and abs(fp.centroid.y - y) < 40):
                    continue
                n_moved += 1
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


def build_ground(sec: Sector, mnt, footprints=None):
    els = json.loads((sec.raw / "osm.json").read_text())["elements"] if (sec.raw / "osm.json").exists() else []
    roads, walks, sand, grass, water = [], [], [], [], []
    peds = []  # rues et places piétonnes : pavage clair de façade à façade
    paths = []
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
            # allées de parc, sentiers : stabilisé clair (pas l'enrobé rouge des trottoirs)
            (walks if hw in ("pedestrian", "steps") else paths).append(line.buffer(WALK_W[hw] / 2, cap_style=2))
            if hw == "pedestrian":
                peds.append(line.buffer(9.0, cap_style=2) if not (pts[0] == pts[-1] and len(pts) >= 4) else Polygon(pts).buffer(1.0))
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
        if peds:
            peds = [unary_union(peds).difference(bu).difference(unary_union(roads) if roads else Polygon())]
    r_road = raster(roads)
    r_walk = raster(walks)
    # le trottoir n'existe que hors chaussée
    walk = Image.fromarray(np.clip(np.array(r_walk, int) - np.array(r_road, int), 0, 255).astype(np.uint8))
    r_sand = raster(sand, 1.2)
    Image.merge("RGB", (r_road, walk, r_sand)).save(sec.out / "hq" / "ground_mask_a.png")
    # végétation réelle : pixels verts de l'orthophoto (jardins, haies, pelouses) hors chaussée
    oimg = Image.open(sec.raw / "ortho.jpg").convert("RGB")
    oa = np.asarray(oimg.resize((N, N), Image.BILINEAR), np.float32) / 255
    exg = 2 * oa[..., 1] - oa[..., 0] - oa[..., 2]
    veg = ndimage.gaussian_filter(((exg > 0.06) & (oa[..., 1] > 0.12)).astype(np.float32), 1.5)
    r_grass = np.maximum(np.array(raster(grass, 2), np.float32) / 255, np.clip(veg * 1.3, 0, 1))
    r_grass *= 1 - np.clip((np.array(r_road, np.float32) + np.array(walk, np.float32) + np.array(raster(peds, 0.6), np.float32) + np.array(raster(paths, 0.6), np.float32)) / 255, 0, 1)
    Image.merge("RGB", (Image.fromarray((r_grass * 255).astype(np.uint8)), raster(peds, 0.6), Image.new("L", (N, N), 0))).save(sec.out / "hq" / "ground_mask_b.png")
    # orthophoto « sol seul » : les toits (qui penchent sur les rues dans l'ortho) sont remplacés par le sol voisin
    G = 2000
    og = np.asarray(oimg.resize((G, G), Image.LANCZOS), np.float32)
    bm = Image.new("L", (G, G), 0)
    bd = ImageDraw.Draw(bm)
    k = G / sec.size
    for fp in footprints or []:
        g = fp.buffer(2.5)
        for q in (g.geoms if hasattr(g, "geoms") else [g]):
            bd.polygon([(x * k, (sec.size - y) * k) for x, y in q.exterior.coords], fill=255)
    bmask = np.array(bm) > 0
    if bmask.any() and not bmask.all():
        _, (ir, ic) = ndimage.distance_transform_edt(bmask, return_indices=True)
        og = og[ir, ic]
    og = ndimage.gaussian_filter(og, (2.5, 2.5, 0))
    Image.fromarray(np.clip(og, 0, 255).astype(np.uint8)).save(sec.out / "hq" / "ground_ortho.jpg", quality=92)
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
            walk_polys.append(LineString(pts).buffer(9.0 if hw == "pedestrian" else WALK_W[hw] / 2, cap_style=2))
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
    blocked = unary_union(footprints + [g.buffer(3.0) for g in road_polys] + walk_polys) if (footprints or road_polys) else None
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
    # bateaux au mouillage devant les plages (corps-morts) : 40 à 380 m du rivage, tous évités dans le sens du courant
    boats = 0
    blist = []
    if (sec.raw / "mid_mnt.npy").exists():
        mid = json.loads((sec.raw / "mid.json").read_text())
        mt = np.load(sec.raw / "mid_mnt.npy")
        mb_ = mid["bbox_l93"]
        mx, my = mb_[0] - mid["origin_l93"][0], mb_[1] - mid["origin_l93"][1]
        st = (mb_[2] - mb_[0]) / mt.shape[0]
        wat = mt < water_level + 0.2
        wat = ndimage.binary_opening(wat, iterations=3)
        dist = ndimage.distance_transform_edt(wat) * st
        ok = (dist > 45) & (dist < 380)
        rr, cc = np.nonzero(ok)
        if len(rr):
            order = rng.permutation(len(rr))
            pts_b = []
            for i in order[:6000]:
                x = mx + (cc[i] + rng.random()) * st
                y = my + (mt.shape[0] - 1 - rr[i] + rng.random()) * st
                if not (-600 < x < sec.size + 600 and -200 < y < sec.size + 900):
                    continue
                if any((x - px) ** 2 + (y - py) ** 2 < 17 ** 2 for px, py in pts_b[-400:]):
                    continue
                pts_b.append((x, y))
                kind = "pinasse" if rng.random() < 0.14 else ("voilier" if rng.random() < 0.18 else "vedette")
                blist.append([round(x, 1), round(y, 1), round(0.12 + float(rng.uniform(-0.12, 0.12)), 3), kind, int(rng.integers(1 << 30))])
                if len(blist) >= 180:
                    break
    boats = len(blist)
    (sec.out / "hq" / "boats.json").write_text(json.dumps(blist))
    # ---- rues piétonnes habitées : jardinières, bancs, terrasses et ardoises devant les commerces
    n_ped = 0
    for e in els:
        t = e.get("tags", {})
        if e["type"] != "way" or t.get("highway") != "pedestrian" or "geometry" not in e:
            continue
        pts = osm_local(sec, e["geometry"])
        if len(pts) < 2 or (pts[0] == pts[-1] and len(pts) >= 4):
            continue
        ln = LineString(pts)
        k_ = 0
        for s_ in np.arange(9, ln.length - 6, 11.0):
            a, b = ln.interpolate(s_ - 1), ln.interpolate(s_ + 1)
            d = np.array([b.x - a.x, b.y - a.y]); d /= np.linalg.norm(d) + 1e-9
            nrm = np.array([-d[1], d[0]]) * (1 if k_ % 2 == 0 else -1)
            c = ln.interpolate(s_)
            q = np.array([c.x, c.y]) + nrm * float(rng.uniform(1.6, 2.6))
            k_ += 1
            if not (2 < q[0] < sec.size - 2 and 2 < q[1] < sec.size - 2):
                continue
            model = ["planter_box_01", "painted_wooden_bench", "planter_box_01", "metal_trash_can"][int(rng.integers(4))]
            props.append([model, round(q[0], 2), round(q[1], 2), round(sample_height(mnt, sec, q[0], q[1]), 2),
                          round(math.atan2(d[1], d[0]) + (math.pi / 2 if model == "painted_wooden_bench" else 0), 3)])
            n_ped += 1
    sg = sec.out / "hq" / "signs.json"
    n_terr = 0
    if sg.exists():
        for x, y, z, ang, w, h, bg, seed in json.loads(sg.read_text()):
            r2 = np.random.default_rng(seed)
            d = np.array([math.cos(ang), math.sin(ang)])
            nout = np.array([d[1], -d[0]])
            zg = sample_height(mnt, sec, x, y)
            if r2.random() < 0.35:   # ardoise de menu sur le trottoir
                q = np.array([x, y]) + nout * 1.1 + d * float(r2.uniform(-w / 2, w / 2) * 0.8)
                props.append(["standing_chalkboard_01", round(q[0], 2), round(q[1], 2), round(zg, 2), round(ang + math.pi + float(r2.uniform(-0.3, 0.3)), 3)])
            if r2.random() < 0.22 and w > 3.5:   # terrasse
                for u in np.arange(-w / 2 + 0.9, w / 2 - 0.8, 1.9):
                    q = np.array([x, y]) + nout * float(r2.uniform(1.5, 1.9)) + d * u
                    props.append(["outdoor_table_chair_set_01", round(q[0], 2), round(q[1], 2), round(zg, 2), round(float(r2.uniform(0, 6.28)), 3)])
                    n_terr += 1
    log(f"  rues piétonnes : {n_ped} objets ; terrasses de commerces : {n_terr} tables")
    # ---- voitures en stationnement le long des rues (les rues vides trahissent tout de suite la maquette)
    cars = []
    ends = [shapely.Point(c) for ln, *_ in lines for c in (ln.coords[0], ln.coords[-1])]
    etree = shapely.STRtree(ends) if ends else None
    crossings = []
    for e in els:
        if e["type"] == "node" and e.get("tags", {}).get("highway") in ("crossing", "traffic_signals", "stop", "give_way"):
            x, y = to_l93.transform(e["lon"], e["lat"])
            crossings.append(shapely.Point(x - sec.x0, y - sec.y0))
    ctree = shapely.STRtree(crossings) if crossings else None
    ftree = shapely.STRtree(footprints) if footprints else None

    def car_ok(x, y, margin=7.0):
        if not (3 < x < sec.size - 3 and 3 < y < sec.size - 3):
            return False
        p = shapely.Point(x, y)
        if etree is not None and ends[int(etree.nearest(p))].distance(p) < margin:
            return False
        if ctree is not None and crossings[int(ctree.nearest(p))].distance(p) < 4.5:
            return False
        if ftree is not None and footprints[int(ftree.nearest(p))].distance(p) < 1.6:
            return False
        if sample_height(mnt, sec, x, y) < water_level + 0.8:
            return False
        return True

    def add_car(x, y, ang, moving=False):
        cars.append([round(x, 2), round(y, 2), round(sample_height(mnt, sec, x, y), 2), round(float(ang), 3),
                     int(rng.integers(1000)), int(moving)])

    for ln, w, hw, lanes, ow in lines:
        if hw not in ("residential", "tertiary", "secondary", "primary", "unclassified", "living_street") or ln.length < 22:
            continue
        narrow = w < 7.0
        sides = [1] if narrow else [1, -1]   # rue étroite : stationnement d'un seul côté
        occ = float(rng.uniform(0.55, 0.9))
        for side in sides:
            s_ = 8.0 + float(rng.uniform(0, 3))
            while s_ < ln.length - 8:
                Lc = float(rng.uniform(5.2, 6.2))
                if rng.random() < occ:
                    a, b = ln.interpolate(s_ - 1), ln.interpolate(s_ + 1)
                    d = np.array([b.x - a.x, b.y - a.y]); d /= np.linalg.norm(d) + 1e-9
                    nrm = np.array([-d[1], d[0]]) * side
                    c = ln.interpolate(s_)
                    q = np.array([c.x, c.y]) + nrm * (w / 2 - 1.05 + float(rng.uniform(-0.08, 0.08)))
                    if car_ok(q[0], q[1]):
                        ang = math.atan2(d[1], d[0]) + (math.pi if (side < 0 and not ow) else 0) + float(rng.uniform(-0.03, 0.03))
                        add_car(q[0], q[1], ang)
                s_ += Lc
        # quelques véhicules qui roulent
        if hw in ("tertiary", "secondary", "primary") or rng.random() < 0.15:
            for s_ in np.arange(15 + rng.uniform(0, 30), ln.length - 10, rng.uniform(45, 110)):
                a, b = ln.interpolate(s_ - 1), ln.interpolate(s_ + 1)
                d = np.array([b.x - a.x, b.y - a.y]); d /= np.linalg.norm(d) + 1e-9
                c = ln.interpolate(s_)
                off = -1.05 if narrow else (-w / 4 + 0.6 if not ow else 0.0)
                q = np.array([c.x, c.y]) + np.array([-d[1], d[0]]) * off
                if car_ok(q[0], q[1], margin=3.0):
                    add_car(q[0], q[1], math.atan2(d[1], d[0]), moving=True)
    # parkings (OSM amenity=parking) : rangées de places de 2,5 × 5 m
    for e in els:
        t = e.get("tags", {})
        if e["type"] != "way" or t.get("amenity") != "parking" or "geometry" not in e or t.get("parking") in ("underground", "multi-storey"):
            continue
        pts = osm_local(sec, e["geometry"])
        if len(pts) < 4 or pts[0] != pts[-1]:
            continue
        poly = Polygon(pts).buffer(0)
        if poly.is_empty or poly.area < 60 or not isinstance(poly, Polygon):
            continue
        cr, a_, b_, Lp, Wp, ctr = rect_frame(poly)
        inner = poly.buffer(-1.2)
        occ = float(rng.uniform(0.5, 0.85))
        row = -Wp / 2 + 2.6
        k_ = 0
        while row < Wp / 2 - 2.4:
            for u in np.arange(-Lp / 2 + 1.4, Lp / 2 - 1.3, 2.55):
                q = ctr + a_ * u + b_ * row
                if rng.random() < occ and inner.contains(shapely.Point(q)) and car_ok(q[0], q[1], margin=0.0):
                    add_car(q[0], q[1], math.atan2(b_[1], b_[0]) + (math.pi if rng.random() < 0.3 else 0) + float(rng.uniform(-0.03, 0.03)))
            row += 5.2 if k_ % 2 == 0 else 11.5
            k_ += 1
    (sec.out / "hq" / "cars.json").write_text(json.dumps(cars))
    log(f"  voitures : {len(cars)} ({sum(c[5] for c in cars)} en circulation)")
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
        f"{n_shrub} arbustes LiDAR, {boats} bateaux au mouillage, {len(props)} objets réels")


# ------------------------------------------------------------------ version web (vue 3D de l'app)
def export_viewer(sec: Sector):
    """build/hq/web/tex/ : textures de la vue 3D de l'app (web/viewer3d.js). La géométrie, elle, est exportée depuis
    la scène Blender par web_export.py : la vue 3D montre la même scène que les rendus Cycles."""
    hq = sec.out / "hq"
    tex = hq / "web" / "tex"
    tex.mkdir(parents=True, exist_ok=True)
    Image.MAX_IMAGE_PIXELS = None

    def put(src: Path, name: str, size: int | None = None, gray=False):
        if not src.exists():
            return
        dst = tex / name
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return
        im = Image.open(src)
        im = im.convert("L" if gray else ("RGB" if im.mode != "RGBA" else "RGBA"))
        if size and max(im.size) > size:
            im = im.resize((size, size), Image.LANCZOS)
        im.save(dst, quality=90) if name.endswith(".jpg") else im.save(dst)

    put(sec.raw / "ortho.jpg", "ortho.jpg", 4096)
    put(hq / "ground_ortho.jpg", "ground_ortho.jpg")
    put(hq / "ground_mask_a.png", "ground_mask_a.png")
    put(hq / "ground_mask_b.png", "ground_mask_b.png")
    put(sec.raw / "near_ortho.jpg", "near_ortho.jpg")
    put(sec.raw / "mid_ortho.jpg", "mid_ortho.jpg", 4096)
    put(sec.raw / ("ctx_ortho_1m.jpg" if (sec.raw / "ctx_ortho_1m.jpg").exists() else "ctx_ortho.jpg"), "ctx_ortho.jpg", 4096)
    assets = Path(__file__).resolve().parent / "assets" / "textures"
    for name in ("asphalt_02", "gravel_concrete", "patterned_paving", "coast_sand_01", "leafy_grass", "painted_plaster_wall",
                 "brick_4", "grey_plaster", "clay_roof_tiles_02", "roof_slates_02"):
        for k in ("diff", "nor", "rough"):
            src = next(iter((assets / name).glob(f"{k}.*")), None)
            if src:
                put(src, f"{name}_{k}.jpg", 1024, gray=k == "rough")

    # bruit périodique (4 échelles dans R, V, B, A) pour les variations de matière, et normales du clapot
    def periodic(n, beta, seed):
        r = np.random.default_rng(seed)
        fx = np.fft.fftfreq(n)[:, None]; fy = np.fft.fftfreq(n)[None, :]
        k = np.sqrt(fx ** 2 + fy ** 2); k[0, 0] = 1
        spec = (r.normal(size=(n, n)) + 1j * r.normal(size=(n, n))) / k ** beta
        spec[0, 0] = 0
        h = np.real(np.fft.ifft2(spec))
        lo, hi = np.percentile(h, (1, 99))
        return np.clip((h - lo) / (hi - lo), 0, 1)

    if not (tex / "noise.png").exists():
        n = 512
        px = np.dstack([periodic(n, b, i) for i, b in enumerate((1.8, 1.4, 1.0, 2.2))])
        Image.fromarray((px * 255).astype(np.uint8), "RGBA").save(tex / "noise.png")
    if not (tex / "water_n.png").exists():
        n = 512
        h = periodic(n, 1.5, 7) * 0.7 + periodic(n, 0.9, 8) * 0.3
        dx = (np.roll(h, -1, 1) - np.roll(h, 1, 1)) * 6.0
        dy = (np.roll(h, -1, 0) - np.roll(h, 1, 0)) * 6.0
        nz = 1 / np.sqrt(dx * dx + dy * dy + 1)
        nrm = np.dstack([-dx * nz, -dy * nz, nz]) * 0.5 + 0.5
        Image.fromarray((nrm * 255).astype(np.uint8), "RGB").save(tex / "water_n.png")
    # part de ciel visible depuis le sol (rues encaissées plus sombres) : remplace l'éclairage global de Cycles pour la
    # lumière ambiante. R = part de ciel au sol, V = altitude du sol, B = hauteur du bâti/des arbres au-dessus du sol.
    mnt_p, mnh_p = sec.raw / "mnt.npy", sec.raw / "mnh.npy"
    if mnt_p.exists() and mnh_p.exists() and not (tex / "skyocc.png").exists():
        g = np.load(mnt_p).astype(np.float32)
        hh = np.load(mnh_p).astype(np.float32)
        f = hh.shape[0] // g.shape[0]
        if f > 1:
            hh = hh[:g.shape[0] * f, :g.shape[1] * f].reshape(g.shape[0], f, g.shape[1], f).max(axis=(1, 3))
        hh = np.clip(hh, 0, 60)
        hh[hh < 1.0] = 0
        res = sec.size / g.shape[0]
        surf = g + hh
        svf = np.zeros_like(g)
        ndir = 16
        for k in range(ndir):
            a = 2 * math.pi * k / ndir
            dx, dy = math.cos(a), math.sin(a)
            tmax = np.zeros_like(g)
            for d in (1, 2, 3, 4, 6, 8, 11, 14, 18, 23, 29, 36, 45):
                sh = ndimage.shift(surf, (dy * d / res, -dx * d / res), order=0, mode="nearest")   # ligne 0 = nord
                tmax = np.maximum(tmax, (sh - g) / d)
            svf += 1.0 / (1.0 + tmax * tmax)          # cos² de l'angle d'horizon
        svf /= ndir
        inside = hh > 2.5                               # sous un toit : on reprend la valeur de la rue la plus proche
        if inside.any() and (~inside).any():
            idx = ndimage.distance_transform_edt(inside, return_distances=False, return_indices=True)
            svf = svf[idx[0], idx[1]]
        top = ndimage.maximum_filter(hh, size=int(round(7 / res)) | 1)
        zmin, zrng = float(g.min()), float(max(g.max() - g.min(), 1.0))
        px = np.dstack([np.clip(svf, 0, 1), np.clip((g - zmin) / zrng, 0, 1), np.clip(top / 60.0, 0, 1)])
        Image.fromarray((px * 255 + 0.5).astype(np.uint8), "RGB").save(tex / "skyocc.png")
        (tex / "skyocc.json").write_text(json.dumps({"zmin": zmin, "zrange": zrng, "hmax": 60.0}))
        log(f"  vue 3D : part de ciel visible (moyenne {svf.mean():.2f})")
    old = hq / "viewer.glb"
    if old.exists():
        old.unlink()
    log(f"  vue 3D : textures prêtes dans {tex.relative_to(sec.out)}")


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
    build_ground(sec, mnt, fps)
    progress(0.9, "détails de rue")
    build_details(sec, mnt, fps, water_level)
    progress(0.95, "vue web")
    export_viewer(sec)
    info = {"buildings": stats, "trees": n_trees, "water_level_m": water_level}
    (sec.out / "hq" / "hq_manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    progress(1, "terminé")
    log("HQ prêt")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

"""Façades en relief : baies en creux (tableaux, vitrage, menuiseries), appuis, encadrements, volets,
portes, vitrines avec enseigne et store, balcons, bandeaux, soubassement, débords de toit et gouttières.

Remplace les fenêtres « peintes » par le shader : à hauteur d'homme, c'est le relief (ombres portées dans
les tableaux, volets décollés du mur, reflets des vitres) qui fait la différence entre une maquette et une rue.

Repère d'une arête : x le long du mur (0..L), v = hauteur au-dessus du sol du bâtiment, out = saillie vers
l'extérieur (négatif = en retrait). L'emprise est orientée (extérieur sens trigo) : l'intérieur est à gauche.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import Point
from shapely.strtree import STRtree

SHUT_PAL = [(0.07, 0.16, 0.30), (0.25, 0.36, 0.27), (0.30, 0.07, 0.09), (0.86, 0.86, 0.83), (0.36, 0.44, 0.50),
            (0.09, 0.33, 0.35), (0.45, 0.30, 0.18), (0.55, 0.62, 0.68), (0.80, 0.80, 0.78), (0.16, 0.22, 0.20)]
SHOP_PAL = [(0.06, 0.07, 0.09), (0.40, 0.07, 0.07), (0.08, 0.22, 0.17), (0.82, 0.79, 0.72), (0.10, 0.15, 0.30),
            (0.70, 0.50, 0.12), (0.18, 0.18, 0.19), (0.75, 0.74, 0.72), (0.05, 0.25, 0.30)]
AWN_PAL = [(0.55, 0.08, 0.08), (0.08, 0.14, 0.32), (0.80, 0.74, 0.60), (0.10, 0.28, 0.18), (0.75, 0.42, 0.10),
           (0.25, 0.25, 0.26), (0.85, 0.84, 0.80)]
DOOR_PAL = [(0.10, 0.20, 0.16), (0.08, 0.13, 0.26), (0.30, 0.17, 0.09), (0.82, 0.82, 0.80), (0.33, 0.06, 0.07),
            (0.12, 0.12, 0.13), (0.42, 0.30, 0.20), (0.25, 0.33, 0.38)]
SIGNS = []  # enseignes : [x, y, z, direction du mur (rad), largeur, hauteur, couleur du bandeau, graine]
STONE = (0.74, 0.71, 0.65)
ZINC = (0.40, 0.42, 0.44)
WHITE = (0.88, 0.87, 0.84)
ANTHRA = (0.10, 0.11, 0.12)


class City:
    """Voisinage : quelles portions de mur sont dégagées (pas mitoyennes), où est la rue."""

    def __init__(self, polys, roads):
        self.polys = polys
        self.tree = STRtree(polys) if polys else None
        self.roads = roads
        self.rtree = STRtree(roads) if roads else None

    def exposed(self, pts):
        pts = np.asarray(pts, float).reshape(-1, 2)
        res = np.ones(len(pts), bool)
        if self.tree is None or not len(pts):
            return res
        geoms = shapely.points(pts)
        pi, _ = self.tree.query(geoms, predicate="within")
        res[np.unique(pi)] = False
        return res

    def road_dist(self, x, y):
        if self.rtree is None:
            return 1e9
        p = Point(x, y)
        return float(self.roads[self.rtree.nearest(p)].distance(p))


class Frame:
    """Repère d'une arête de façade + primitives (rectangles, boîtes) écrites dans le MeshBuf."""

    def __init__(self, buf, a, d, nout, ground, L, seed):
        self.buf, self.a, self.d, self.n, self.g, self.L, self.seed = buf, np.asarray(a, float), d, nout, ground, L, seed

    def P(self, x, v, out=0.0):
        q = self.a + self.d * x + self.n * out
        return (float(q[0]), float(q[1]), float(self.g + v))

    def quad4(self, pts, mat, col, uvs=None, seed=None, lod=1):
        """pts : 4 × (x, v, out), dans l'ordre du contour."""
        P = [self.P(*p) for p in pts]
        if uvs is None:
            uvs = [[p[0] - self.L / 2, p[1]] for p in pts]
        b = self.buf
        old = b.cur_lod
        b.cur_lod = lod
        b.quad(P[0], P[1], P[2], P[3], uvs[0], uvs[1], uvs[2], uvs[3], [self.L, 99.0], mat, col,
               self.seed if seed is None else seed)
        b.cur_lod = old

    def rect(self, x0, x1, v0, v1, out, mat, col, uv=None, seed=None, lod=1):
        """Rectangle parallèle au mur, à la saillie `out`. uv : None = mètres du mur, sinon (u0, v0) local."""
        uvs = None
        if uv is not None:
            uvs = [[uv[0], uv[1]], [uv[0] + (x1 - x0), uv[1]], [uv[0] + (x1 - x0), uv[1] + (v1 - v0)], [uv[0], uv[1] + (v1 - v0)]]
        self.quad4([(x0, v0, out), (x1, v0, out), (x1, v1, out), (x0, v1, out)], mat, col, uvs, seed, lod)

    def box(self, x0, x1, v0, v1, o0, o1, mat, col, sides="tblr", front=True, lod=1, seed=None):
        """Boîte en saillie de o0 à o1 : face avant + chants."""
        if front:
            self.rect(x0, x1, v0, v1, o1, mat, col, seed=seed, lod=lod)
        dz = abs(o1 - o0)
        if "t" in sides:
            self.quad4([(x0, v1, o0), (x1, v1, o0), (x1, v1, o1), (x0, v1, o1)], mat, col,
                       [[x0, 0], [x1, 0], [x1, dz], [x0, dz]], seed, lod)
        if "b" in sides:
            self.quad4([(x0, v0, o1), (x1, v0, o1), (x1, v0, o0), (x0, v0, o0)], mat, col,
                       [[x0, 0], [x1, 0], [x1, dz], [x0, dz]], seed, lod)
        if "l" in sides:
            self.quad4([(x0, v0, o1), (x0, v0, o0), (x0, v1, o0), (x0, v1, o1)], mat, col,
                       [[0, v0], [dz, v0], [dz, v1], [0, v1]], seed, lod)
        if "r" in sides:
            self.quad4([(x1, v0, o0), (x1, v0, o1), (x1, v1, o1), (x1, v1, o0)], mat, col,
                       [[0, v0], [dz, v0], [dz, v1], [0, v1]], seed, lod)


class Opening:
    __slots__ = ("x0", "x1", "v0", "v1", "kind", "floor")

    def __init__(self, x0, x1, v0, v1, kind, floor):
        self.x0, self.x1, self.v0, self.v1, self.kind, self.floor = x0, x1, v0, v1, kind, floor


def make_spec(rng, fa, ph, h_wall, area, flat, wall_name):
    """Tire le « caractère » d'un bâtiment : trame, menuiseries, volets, balcons… (photo > hasard cohérent)."""
    villa = area < 260 and h_wall < 10.5
    modern = (not villa) and flat
    small = area < 45 and h_wall < 4.6
    usable = h_wall - (0.8 if flat else 0.0)
    if fa[0] > 0.5:
        F = float(fa[0])
    else:
        nfl = max(1, int(round(usable / (2.75 if modern else 3.05))))
        F = float(np.clip(usable / nfl, 2.5, 3.7))
    nfl = max(1, int((usable + 0.35) // F))
    s = {"villa": villa, "modern": modern, "small": small, "F": F, "nfl": nfl}
    s["S"] = float(rng.uniform(3.0, 3.7) if modern else rng.uniform(2.5, 3.2))
    s["WW"] = float(rng.choice([1.3, 1.5, 1.8]) if modern else rng.choice([0.95, 1.05, 1.15, 1.25]))
    s["SILL"] = 0.95 if modern else 0.9
    s["WH"] = float(np.clip(F - (1.55 if modern else 1.35), 1.15, 1.95))
    r = rng.random()
    s["frame_col"] = WHITE if r < 0.68 else (ANTHRA if r < (0.92 if modern else 0.76) else (0.36, 0.24, 0.15))
    # volets
    mode = int(fa[2]) if fa[2] > -0.5 else None
    if mode is None:
        r = rng.random()
        mode = (2 if r < 0.7 else 0) if modern else (1 if r < 0.66 else (2 if r < 0.86 else 0))
    s["shut"] = mode
    sc = tuple(fa[3:6]) if sum(fa[3:6]) > 0.02 else SHUT_PAL[rng.integers(len(SHUT_PAL))]
    s["shut_col"] = sc
    s["roll_col"] = WHITE if rng.random() < 0.7 else (0.62, 0.63, 0.64)
    s["closed"] = rng.random() < (0.10 if villa else 0.03)   # résidence fermée : tous volets clos
    # encadrements : brique (relevé) ou bandeau peint
    if fa[6] > 0.5:
        s["surround"] = ("brique", tuple(fa[7:10]), 0.20)
    elif not modern and rng.random() < 0.55:
        s["surround"] = ("peint", WHITE if rng.random() < 0.8 else STONE, 0.14)
    else:
        s["surround"] = None
    s["sill_col"] = STONE if rng.random() < 0.7 else WHITE
    s["plinth"] = rng.random() < 0.8
    s["band"] = (not small) and rng.random() < (0.5 if not villa else 0.25)
    bal = bool((ph or {}).get("balcony")) if ph else None
    if bal is None:
        bal = rng.random() < (0.55 if modern else (0.10 if villa else 0.3))
    s["balcony"] = ("filant" if (modern and rng.random() < 0.6) else "indiv") if (bal and nfl >= 2) else None
    s["rail"] = "solid" if (modern and rng.random() < 0.35) else "bars"
    s["rail_col"] = WHITE if (rng.random() < 0.3) else ANTHRA
    shop = fa[1]
    s["shop"] = (shop > 0.5) if shop > -0.5 else ((not villa) and h_wall > 8.5 and rng.random() < 0.45)
    s["shop_col"] = SHOP_PAL[rng.integers(len(SHOP_PAL))]
    s["awning"] = AWN_PAL[rng.integers(len(AWN_PAL))] if rng.random() < 0.45 else None
    s["door_col"] = DOOR_PAL[rng.integers(len(DOOR_PAL))]
    s["soffit"] = WHITE if rng.random() < 0.6 else (0.42, 0.30, 0.20)
    return s


def refit(s, usable, fa0=0.0):
    """Recale le nombre d'étages sur la hauteur de mur réellement relevée (LiDAR)."""
    usable = max(2.3, float(usable))
    if fa0 > 0.5:
        F = float(np.clip(fa0, 2.5, 4.4))
    else:
        nfl = max(1, int(round(usable / (2.75 if s["modern"] else 3.05))))
        F = float(np.clip(usable / nfl, 2.5, 3.7))
    s["F"] = F
    s["nfl"] = max(1, int((usable + 0.35) // F))
    s["WH"] = float(np.clip(F - (1.55 if s["modern"] else 1.35), 1.15, 1.95))
    if s["nfl"] < 2:
        s["balcony"] = None


def regular_top(ts, z):
    """Haut de mur régularisé : une ligne d'égout droite, ou un pignon en deux pentes, plutôt que le bruit du LiDAR."""
    ts, z = np.asarray(ts, float), np.asarray(z, float)
    n = len(z)
    if n < 3:
        return np.full(n, float(np.median(z))) if n else z
    a, b = np.polyfit(ts, z, 1)
    fit = a * ts + b
    if np.abs(z - fit).max() < 0.45 or n < 5:
        return np.full(n, float(np.median(z))) if abs(a) < 0.05 else fit
    best = None
    for k in range(2, n - 2):
        l = np.polyfit(ts[:k + 1], z[:k + 1], 1)
        r = np.polyfit(ts[k:], z[k:], 1)
        zk = (np.polyval(l, ts[k]) + np.polyval(r, ts[k])) / 2
        zl = np.interp(ts[:k + 1], [ts[0], ts[k]], [np.polyval(l, ts[0]), zk])
        zr = np.interp(ts[k:], [ts[k], ts[-1]], [zk, np.polyval(r, ts[-1])])
        cand = np.concatenate([zl, zr[1:]])
        err = np.abs(z - cand).max()
        if best is None or err < best[0]:
            best = (err, cand)
    if best and best[0] < 0.6:
        return best[1]
    # profil complexe (décrochés) : paliers = médiane glissante, puis paliers nets
    from scipy.ndimage import median_filter
    return median_filter(z, size=5, mode="nearest")


def _glass_col(rng):
    r = rng.random()
    if r < 0.5:      # pièce sombre
        g = rng.uniform(0.012, 0.035)
        return (g, g * 1.05, g * 1.12)
    if r < 0.88:     # voilages
        g = rng.uniform(0.38, 0.62)
        return (g, g * 0.98, g * 0.93)
    g = rng.uniform(0.10, 0.20)
    return (g * 1.05, g, g * 0.9)


def _window(fr, o, s, M, wall_mat, col, rng, depth=0.2):
    x0, x1, v0, v1 = o.x0, o.x1, o.v0, o.v1
    w, h = x1 - x0, v1 - v0
    D = depth
    # tableaux (retour de mur dans l'épaisseur)
    fr.box(x0, x1, v0, v1, 0.0, -D, wall_mat, col, front=False, lod=0)
    if o.kind == "door":
        dl = v0 + min(h, 2.12)
        fr.rect(x0 + 0.05, x1 - 0.05, v0, dl, -D + 0.02, M["peint"], s["door_col"], uv=(0, 0), lod=0)
        # panneaux de porte (moulures)
        dc = tuple(c * 0.8 for c in s["door_col"])
        for pv0, pv1 in ((v0 + 0.18, v0 + 0.95), (v0 + 1.08, dl - 0.16)):
            fr.box(x0 + 0.2, x1 - 0.2, pv0, pv1, -D + 0.02, -D + 0.035, M["peint"], dc)
        fr.box(x1 - 0.2, x1 - 0.16, v0 + 1.0, v0 + 1.12, -D + 0.02, -D + 0.08, M["peint"], (0.6, 0.55, 0.4))  # poignée
        if v1 - dl > 0.2:  # imposte vitrée
            fr.rect(x0, x1, dl, v1, -D, M["vitre"], _glass_col(rng), uv=(0, 0), seed=float(rng.random()), lod=0)
            fr.box(x0, x1, dl, dl + 0.06, -D, -D + 0.05, M["peint"], s["frame_col"])
        fc = s["frame_col"]
        fr.box(x0, x0 + 0.06, v0, v1, -D, -D + 0.05, M["peint"], fc, sides="r")
        fr.box(x1 - 0.06, x1, v0, v1, -D, -D + 0.05, M["peint"], fc, sides="l")
        fr.box(x0, x1, v1 - 0.06, v1, -D, -D + 0.05, M["peint"], fc, sides="b")
        # seuil
        fr.box(x0 - 0.12, x1 + 0.12, v0 - 0.25, v0, 0.0, 0.32, M["peint"], STONE)
        return
    shop = o.kind in ("shop", "shopdoor")
    gcol = _glass_col(rng)
    if shop:
        g = rng.uniform(0.03, 0.12)
        gcol = (g, g * 0.95, g * 0.85)
    fr.rect(x0, x1, v0, v1, -D, M["vitre"], gcol, uv=(0, 0), seed=float(rng.random()) * (0.5 if not shop else 1.0) + (0.5 if shop else 0.0), lod=0)
    fc = (s["shop_col"] if rng.random() < 0.5 else ANTHRA) if shop else s["frame_col"]
    fw = 0.05 if shop else 0.055
    fo = -D + 0.04
    fr.box(x0, x0 + fw, v0, v1, -D, fo, M["peint"], fc, sides="r")
    fr.box(x1 - fw, x1, v0, v1, -D, fo, M["peint"], fc, sides="l")
    fr.box(x0 + fw, x1 - fw, v1 - fw, v1, -D, fo, M["peint"], fc, sides="b")
    fr.box(x0 + fw, x1 - fw, v0, v0 + fw + 0.02, -D, fo, M["peint"], fc, sides="t")
    if shop:
        nm = max(1, int(round(w / 1.4)))
        for k in range(1, nm):
            xm = x0 + w * k / nm
            fr.box(xm - 0.025, xm + 0.025, v0, v1, -D, fo, M["peint"], fc, sides="lr")
        if o.kind == "shopdoor":
            fr.box(x0 + fw, x1 - fw, v0 + 2.1, v0 + 2.16, -D, fo, M["peint"], fc, sides="tb")
        else:  # allège pleine sous la vitrine
            fr.box(x0, x1, v0, v0 + 0.12, -D, fo + 0.01, M["peint"], fc, sides="t")
        return
    # ouvrants : deux vantaux (montant central), petits-bois sur les villas
    nleaf = 1 if w < 0.8 else (2 if w < 1.7 else 3)
    for k in range(1, nleaf):
        xm = x0 + w * k / nleaf
        fr.box(xm - 0.04, xm + 0.04, v0 + fw, v1 - fw, -D, fo, M["peint"], fc, sides="lr")
    if s.get("bars") and h > 1.3:
        for k in (1, 2):
            vm = v0 + h * k / 3
            fr.box(x0 + fw, x1 - fw, vm - 0.013, vm + 0.013, -D, fo - 0.01, M["peint"], fc, sides="")
    # appui de fenêtre
    if o.kind == "window":
        fr.box(x0 - 0.07, x1 + 0.07, v0 - 0.07, v0, 0.0, 0.07, M["peint"], s["sill_col"])
        fr.quad4([(x0, v0, 0.0), (x1, v0, 0.0), (x1, v0 + 0.015, -D), (x0, v0 + 0.015, -D)], M["peint"], s["sill_col"])
    # encadrement
    sr = s["surround"]
    if sr:
        kind, scol, sw = sr
        m = M["mur_brique"] if kind == "brique" else M["peint"]
        o_ = 0.025
        vb = v0 - (0.07 if o.kind == "window" else 0.0)
        fr.box(x0 - sw, x0, vb, v1, 0.0, o_, m, scol, sides="tbl")
        fr.box(x1, x1 + sw, vb, v1, 0.0, o_, m, scol, sides="tbr")
        fr.box(x0 - sw, x1 + sw, v1, v1 + sw + 0.03, 0.0, o_ + 0.01, m, scol)
    # volets
    closed = s["closed"] or rng.random() < 0.10
    if s["shut"] == 1:
        vc = s["shut_col"]
        if closed:
            for xa, xb in ((x0, x0 + w / 2 - 0.005), (x0 + w / 2 + 0.005, x1)):
                fr.rect(xa, xb, v0, v1, -0.05, M["volet"], vc, uv=(0, 0))
        else:
            pw = min(w / 2, (s["S"] - w) / 2 - 0.05)
            if pw > 0.25:
                for xa, xb, sd in ((x0 - pw - 0.02, x0 - 0.02, "tbl"), (x1 + 0.02, x1 + 0.02 + pw, "tbr")):
                    if xa > 0.03 and xb < fr.L - 0.03:
                        fr.box(xa, xb, v0, v1, 0.02, 0.055, M["volet"], vc, sides=sd)
    elif s["shut"] == 2:
        frac = 1.0 if closed else float(rng.choice([0.0, 0.0, 0.0, 0.25, 0.45, 1.0]))
        fr.box(x0, x1, v1 - 0.16, v1, -D, -D + 0.14, M["peint"], s["roll_col"], sides="b")  # coffre
        if frac > 0:
            fr.rect(x0 + 0.03, x1 - 0.03, v1 - 0.16 - frac * (h - 0.16), v1 - 0.16, -D + 0.09, M["volet"], s["roll_col"], uv=(0, 0))


def _balcony(fr, xa, xb, vf, depth, s, M):
    slab = (0.72, 0.71, 0.69)
    fr.box(xa, xb, vf - 0.16, vf, 0.0, depth, M["peint"], slab)
    rc = s["rail_col"]
    hh = 1.02
    if s["rail"] == "solid":
        fr.box(xa, xb, vf, vf + hh, depth - 0.1, depth, M["peint"], WHITE, sides="t")
        fr.rect(xb, xa, vf, vf + hh, depth - 0.1, M["peint"], WHITE)
        for x in (xa, xb):
            fr.quad4([(x, vf, 0.0), (x, vf, depth), (x, vf + hh, depth), (x, vf + hh, 0.0)], M["peint"], WHITE,
                     [[0, 0], [depth, 0], [depth, hh], [0, hh]])
        return
    m = M["garde_corps"]
    L = xb - xa
    fr.quad4([(xa, vf, depth - 0.03), (xb, vf, depth - 0.03), (xb, vf + hh, depth - 0.03), (xa, vf + hh, depth - 0.03)], m, rc,
             [[0, 0], [L, 0], [L, hh], [0, hh]])
    for x in (xa + 0.02, xb - 0.02):
        fr.quad4([(x, vf, 0.0), (x, vf, depth - 0.03), (x, vf + hh, depth - 0.03), (x, vf + hh, 0.0)], m, rc,
                 [[0, 0], [depth, 0], [depth, hh], [0, hh]])


def facade_edge(buf, M, city, a, b, ts, zt, base, ground, wall_mat, col, seed, s, rng, door=False, near_road=False):
    """Mur d'une arête (a→b) avec ses ouvertures. ts : abscisses 0..L, zt : altitude du haut du mur."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    e = b - a
    L = float(np.linalg.norm(e))
    if L < 0.05:
        return
    d = e / L
    nout = np.array([d[1], -d[0]])
    fr = Frame(buf, a, d, nout, ground, L, seed)
    ts = np.asarray(ts, float)
    top = np.asarray(zt, float) - ground
    vb = base - ground
    topf = lambda x: float(np.interp(x, ts, top))

    def top_min(x0, x1):
        xs = np.concatenate([[x0, x1], ts[(ts > x0) & (ts < x1)]])
        return float(np.interp(xs, ts, top).min())

    # portions dégagées (pas de mitoyen) : échantillons tous les ~1 m, 0,8 m devant le mur
    ns = max(2, int(L / 1.0) + 1)
    sx = np.linspace(0.3, L - 0.3, ns) if L > 0.8 else np.array([L / 2])
    ex = city.exposed(a + np.outer(sx, d) + nout * 0.8) if city else np.ones(len(sx), bool)

    def free(x0, x1):
        m = (sx >= x0 - 0.6) & (sx <= x1 + 0.6)
        return bool(ex[m].all()) if m.any() else bool(ex[np.argmin(np.abs(sx - (x0 + x1) / 2))])

    ops = []
    F, S, WW, WH, SILL = s["F"], s["S"], s["WW"], s["WH"], s["SILL"]
    nw = int((L - 0.7) // S) if L >= S * 0.82 + 0.5 else (1 if L >= WW + 1.0 else 0)
    nw = max(nw, 1 if L >= WW + 1.0 else 0)
    centers = [L / 2 + (k - (nw - 1) / 2) * min(S, (L - 0.5) / max(nw, 1)) for k in range(nw)]
    shop = s["shop"] and near_road and nw >= 1 and not s["small"] and top_min(0.3, L - 0.3) > 3.2
    door_k = (nw // 2 if not shop else int(rng.integers(nw))) if (door and nw) else None
    if s["small"]:  # annexes, garages : une porte ou une petite fenêtre au plus
        centers = centers[:1]
        nw = len(centers)
    hw_shop = min(S, (L - 0.5) / max(nw, 1)) / 2 - 0.22
    for k, xc in enumerate(centers):
        for j in range(s["nfl"]):
            if j == 0 and shop:
                o = Opening(xc - hw_shop, xc + hw_shop, 0.05 if k == door_k else 0.42, min(2.75, F - 0.45), "shopdoor" if k == door_k else "shop", 0)
            elif j == 0 and k == door_k:
                o = Opening(xc - 0.5, xc + 0.5, 0.06, min(F - 0.35, 2.55 if F > 2.9 else 2.18), "door", 0)
            elif j >= 1 and s["balcony"]:
                wb = WW / 2 + (0.25 if s["modern"] else 0.0)
                o = Opening(xc - wb, xc + wb, j * F + 0.04, j * F + min(2.2, F - 0.42), "french", j)
            else:
                o = Opening(xc - WW / 2, xc + WW / 2, j * F + SILL, j * F + SILL + WH, "window", j)
            if o.x0 < 0.3 or o.x1 > L - 0.3 or o.v0 < vb + 0.1:
                continue
            if o.v1 + 0.28 > top_min(o.x0 - 0.15, o.x1 + 0.15) or not free(o.x0, o.x1):
                continue
            ops.append(o)
    # ---- mur plein découpé en bandes verticales autour des ouvertures
    xs = set(float(t) for t in ts)
    for o in ops:
        xs.update((o.x0, o.x1))
    xs = sorted(x for x in xs if -1e-6 <= x <= L + 1e-6)
    old = buf.cur_lod
    buf.cur_lod = 0
    plinth_c = tuple(c * 0.72 for c in col)
    for xa, xb in zip(xs[:-1], xs[1:]):
        if xb - xa < 1e-4:
            continue
        holes = sorted((o for o in ops if o.x0 <= xa + 1e-6 and o.x1 >= xb - 1e-6), key=lambda o: o.v0)
        cur = vb
        ta, tb = topf(xa), topf(xb)
        tmin = min(ta, tb)
        for o in holes:
            buf.quad(fr.P(xa, cur), fr.P(xb, cur), fr.P(xb, o.v0), fr.P(xa, o.v0),
                     [xa - L / 2, cur], [xb - L / 2, cur], [xb - L / 2, o.v0], [xa - L / 2, o.v0], [L, tmin], wall_mat, col, seed)
            cur = o.v1
        buf.quad(fr.P(xa, cur), fr.P(xb, cur), fr.P(xb, tb), fr.P(xa, ta),
                 [xa - L / 2, cur], [xb - L / 2, cur], [xb - L / 2, tb], [xa - L / 2, ta], [L, tmin], wall_mat, col, seed)
        # soubassement
        if s["plinth"] and tmin > 1.5 and free(xa, xb) and not (holes and holes[0].v0 < 0.62):
            fr.box(xa, xb, vb, 0.55, 0.0, 0.03, M["peint"], plinth_c, sides="t", lod=1)
    buf.cur_lod = old
    if not ops:
        return
    for o in ops:
        _window(fr, o, s, M, wall_mat, col, rng, depth=0.28 if o.kind in ("shop", "shopdoor") else (0.22 if o.kind == "door" else 0.19))
    # ---- vitrines : enseigne filante + store
    sh = [o for o in ops if o.kind in ("shop", "shopdoor")]
    if sh:
        xa, xb = min(o.x0 for o in sh) - 0.1, max(o.x1 for o in sh) + 0.1
        v0 = max(o.v1 for o in sh) + 0.08
        v1 = min(v0 + 0.5, F - 0.05)
        if v1 - v0 > 0.2 and top_min(xa, xb) > v1 + 0.2:
            fr.box(xa, xb, v0, v1, 0.0, 0.07, M["peint"], s["shop_col"])
            c_ = fr.P((xa + xb) / 2, (v0 + v1) / 2, 0.075)
            SIGNS.append([round(c_[0], 3), round(c_[1], 3), round(c_[2], 3), round(math.atan2(d[1], d[0]), 4),
                          round(xb - xa, 2), round(v1 - v0, 2), [round(float(x), 3) for x in s["shop_col"]], int(rng.integers(1 << 30))])
        if s["awning"]:
            ac = s["awning"]
            v_hi, v_lo, dep = v0 - 0.03, v0 - 0.48, 1.35
            fr.quad4([(xa, v_hi, 0.08), (xb, v_hi, 0.08), (xb, v_lo, dep), (xa, v_lo, dep)], M["toile"], ac,
                     [[0, 0], [xb - xa, 0], [xb - xa, 1.4], [0, 1.4]])
            fr.quad4([(xa, v_lo, dep), (xb, v_lo, dep), (xb, v_lo - 0.2, dep), (xa, v_lo - 0.2, dep)], M["toile"], ac,
                     [[0, 1.4], [xb - xa, 1.4], [xb - xa, 1.6], [0, 1.6]])
    # ---- balcons
    if s["balcony"]:
        for j in range(1, s["nfl"]):
            fl = [o for o in ops if o.kind == "french" and o.floor == j]
            if not fl:
                continue
            if s["balcony"] == "filant":
                xa, xb = max(0.1, min(o.x0 for o in fl) - 0.5), min(L - 0.1, max(o.x1 for o in fl) + 0.5)
                _balcony(fr, xa, xb, j * F + 0.02, 1.15, s, M)
            else:
                dep = 0.85 if s["modern"] else 0.42
                for o in fl:
                    _balcony(fr, o.x0 - 0.22, o.x1 + 0.22, j * F + 0.02, dep, s, M)
    # ---- bandeaux d'étage
    elif s["band"] and len(ops) >= 2:
        for j in range(1, s["nfl"] + 1):
            vj = j * F - 0.02
            if top_min(0.0, L) > vj + 0.3 and ex.all():
                fr.box(0.0, L, vj - 0.09, vj + 0.07, 0.0, 0.035, M["peint"], WHITE if s["surround"] else tuple(c * 0.9 for c in col), sides="tb")


def facade_ring(buf, M, city, ring, tops, base, ground, wall_mat, col, seed, s, rng, allow_door=True):
    """Tous les murs d'un contour. tops : par arête (ts, zt) ou une altitude constante."""
    pts = np.asarray(ring, float)[:, :2]
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = len(pts)
    info = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        L = float(np.linalg.norm(b - a))
        if L < 0.05:
            info.append(None)
            continue
        d = (b - a) / L
        nout = np.array([d[1], -d[0]])
        mid = (a + b) / 2 + nout * 1.0
        rd = city.road_dist(mid[0], mid[1]) if city else 1e9
        ok = bool(city.exposed([mid])[0]) if city else True
        info.append((L, rd, ok))
    door_i = None
    if allow_door:
        cand = [(inf[1], i) for i, inf in enumerate(info) if inf and inf[2] and inf[0] >= 1.7]
        if cand:
            door_i = min(cand)[1]
    for i in range(n):
        if info[i] is None:
            continue
        a, b = pts[i], pts[(i + 1) % n]
        L = info[i][0]
        if isinstance(tops, (int, float)):
            ts, zt = [0.0, L], [tops, tops]
        else:
            ts, zt = tops[i]
        facade_edge(buf, M, city, a, b, ts, zt, base, ground, wall_mat, col, seed, s, rng,
                    door=(i == door_i), near_road=info[i][1] < 16.0)


def _up_quad(buf, P, mat, col, seed, uv_fn, up=True, lod=1):
    a, b, c = np.array(P[0]), np.array(P[1]), np.array(P[2])
    nz = np.cross(b - a, c - a)[2]
    if (nz < 0) == up:
        P = [P[0], P[3], P[2], P[1]]
    uv = uv_fn(P)
    old = buf.cur_lod
    buf.cur_lod = lod
    buf.quad(P[0], P[1], P[2], P[3], uv[0], uv[1], uv[2], uv[3], [0, 0], mat, col, seed)
    buf.cur_lod = old


def eaves(buf, M, city, ring, tops, roof_mat, col, seed, s, planar_uv, ov=0.38, ground=None):
    """Débord de toit le long des murs dégagés : rive de tuiles, sous-face, bandeau, gouttière."""
    pts = np.asarray(ring, float)[:, :2]
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = len(pts)
    flat_uv = lambda P: [[p[0], p[1]] for p in P]
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        L = float(np.linalg.norm(b - a))
        if L < 0.4 or tops[i] is None:
            continue
        d = (b - a) / L
        nout = np.array([d[1], -d[0]])
        ts, zt = tops[i]
        ts, zt = np.asarray(ts, float), np.asarray(zt, float)
        mids = a + np.outer((ts[:-1] + ts[1:]) / 2, d) + nout * 0.8
        ex = city.exposed(mids) if city else np.ones(len(mids), bool)
        # descente d'eaux pluviales (zinc) au départ des façades à gouttière
        if ground is not None and L > 3.5 and len(ex) and ex[0] and abs(zt[min(1, len(zt) - 1)] - zt[0]) < 0.3 * max(ts[min(1, len(ts) - 1)], 0.5):
            fr = Frame(buf, a, d, nout, ground, L, seed)
            ztop = float(zt[0]) - ground - 0.25
            if ztop > 2.2:
                fr.box(0.22, 0.30, 0.02, ztop, 0.03, 0.11, M["peint"], ZINC, sides="lr")
        for j in range(len(ts) - 1):
            if not ex[j]:
                continue
            dt = ts[j + 1] - ts[j]
            if dt < 1e-3:
                continue
            rake = abs(zt[j + 1] - zt[j]) / dt > 0.28
            o = 0.2 if rake else ov
            drop = 0.02 if rake else o * 0.42
            p0, p1 = a + d * ts[j], a + d * ts[j + 1]
            q0, q1 = p0 + nout * o, p1 + nout * o
            z0, z1 = float(zt[j]) + 0.03, float(zt[j + 1]) + 0.03
            top = [(p0[0], p0[1], z0), (p1[0], p1[1], z1), (q1[0], q1[1], z1 - drop), (q0[0], q0[1], z0 - drop)]
            _up_quad(buf, top, roof_mat, col, seed, planar_uv, up=True, lod=0)
            und = [(x, y, z - 0.07) for x, y, z in top]
            _up_quad(buf, und, M["peint"], s["soffit"], seed, flat_uv, up=False)
            # bandeau de rive
            fz0, fz1 = z0 - drop, z1 - drop
            fb = [(q0[0], q0[1], fz0 - 0.16), (q1[0], q1[1], fz1 - 0.16), (q1[0], q1[1], fz1 + 0.01), (q0[0], q0[1], fz0 + 0.01)]
            buf.cur_lod = 1
            buf.quad(*fb, [0, 0], [dt, 0], [dt, 0.17], [0, 0.17], [0, 0], M["peint"], s["soffit"], seed)
            if not rake:  # gouttière demi-ronde approchée (3 faces)
                g0, g1 = q0 + nout * 0.11, q1 + nout * 0.11
                gz0, gz1 = fz0 - 0.10, fz1 - 0.10
                buf.quad((q0[0], q0[1], gz0), (q1[0], q1[1], gz1), (g1[0], g1[1], gz1 + 0.02), (g0[0], g0[1], gz0 + 0.02),
                         [0, 0], [dt, 0], [dt, 0.1], [0, 0.1], [0, 0], M["peint"], ZINC, seed)
                buf.quad((g0[0], g0[1], gz0 + 0.02), (g1[0], g1[1], gz1 + 0.02), (g1[0], g1[1], gz1 + 0.11), (g0[0], g0[1], gz0 + 0.11),
                         [0, 0], [dt, 0], [dt, 0.1], [0, 0.1], [0, 0], M["peint"], ZINC, seed)
            buf.cur_lod = 0


def coping(buf, M, city, ring, top, col, seed):
    """Couvertine d'acrotère (toits-terrasses) : fine tablette en saillie sur le haut du mur."""
    pts = np.asarray(ring, float)[:, :2]
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    n = len(pts)
    cc = (0.70, 0.70, 0.68)
    old = buf.cur_lod
    buf.cur_lod = 1
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        L = float(np.linalg.norm(b - a))
        if L < 0.3:
            continue
        d = (b - a) / L
        nout = np.array([d[1], -d[0]])
        o0, o1 = a + nout * 0.05, b + nout * 0.05
        i0, i1 = a - nout * 0.25, b - nout * 0.25
        z = top
        buf.quad((o0[0], o0[1], z + 0.05), (o1[0], o1[1], z + 0.05), (i1[0], i1[1], z + 0.05), (i0[0], i0[1], z + 0.05),
                 [0, 0], [L, 0], [L, 0.3], [0, 0.3], [0, 0], M["peint"], cc, seed)
        buf.quad((o0[0], o0[1], z - 0.03), (o1[0], o1[1], z - 0.03), (o1[0], o1[1], z + 0.05), (o0[0], o0[1], z + 0.05),
                 [0, 0], [L, 0], [L, 0.08], [0, 0.08], [0, 0], M["peint"], cc, seed)
        buf.quad((i1[0], i1[1], z - 0.75), (i0[0], i0[1], z - 0.75), (i0[0], i0[1], z + 0.05), (i1[0], i1[1], z + 0.05),
                 [0, 0], [L, 0], [L, 0.8], [0, 0.8], [0, 0], M["peint"], tuple(c * 0.85 for c in col), seed)
    buf.cur_lod = old


def chimney(buf, M, x, y, z, col, seed, rng, brick=True):
    w, l, h = rng.uniform(0.4, 0.55), rng.uniform(0.6, 1.0), rng.uniform(0.9, 1.5)
    ang = rng.uniform(0, math.pi)
    d = np.array([math.cos(ang), math.sin(ang)])
    nrm = np.array([-d[1], d[0]])
    c = np.array([x, y])
    cs = [c + d * sx * l / 2 + nrm * sy * w / 2 for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    z0, z1 = z - 0.6, z + h
    m = M["mur_brique"] if brick else M["peint"]
    cc = (0.55, 0.27, 0.19) if brick else col
    old = buf.cur_lod
    buf.cur_lod = 1
    for k in range(4):
        p, q = cs[k], cs[(k + 1) % 4]
        Lk = float(np.linalg.norm(q - p))
        buf.quad((p[0], p[1], z0), (q[0], q[1], z0), (q[0], q[1], z1), (p[0], p[1], z1),
                 [0, 0], [Lk, 0], [Lk, z1 - z0], [0, z1 - z0], [0, 0], m, cc, seed)
    buf.quad(*[(p[0], p[1], z1) for p in cs], [0, 0], [l, 0], [l, w], [0, w], [0, 0], M["peint"], (0.25, 0.22, 0.2), seed)
    buf.cur_lod = old

"""Voitures génériques (aucune marque) : carrosserie lissée par sections, vitrage, roues, feux.
Cinq gabarits × teintes du parc français. Suffisant vu d'avion et à quelques mètres ; pas un modèle de salon."""
import math

import bpy
import numpy as np

# gabarit : L, l, h, puis profils le long de la voiture (0 = avant, 1 = arrière)
#   xs      abscisses de contrôle
#   top     hauteur du pavillon / capot / coffre
#   belt    ceinture de caisse (bas des vitres)
#   wid     demi-largeur relative
TYPES = {
    "citadine": dict(L=3.75, W=1.68, H=1.50,
                     xs=[0, .03, .10, .24, .40, .78, .93, .98, 1], top=[.46, .66, .76, .90, 1, .98, .72, .62, .44],
                     belt=[.46, .62, .70, .60, .60, .62, .64, .60, .44], wid=[.72, .90, .97, 1, 1, 1, .97, .92, .76]),
    "compacte": dict(L=4.25, W=1.78, H=1.47,
                     xs=[0, .03, .10, .27, .43, .76, .92, .98, 1], top=[.44, .62, .72, .86, 1, .97, .70, .60, .42],
                     belt=[.44, .60, .68, .60, .61, .63, .65, .60, .42], wid=[.72, .90, .97, 1, 1, 1, .97, .92, .76]),
    "berline": dict(L=4.65, W=1.82, H=1.44,
                    xs=[0, .03, .09, .27, .42, .68, .84, .97, 1], top=[.44, .62, .72, .84, 1, .98, .68, .64, .44],
                    belt=[.44, .60, .68, .61, .62, .64, .66, .62, .44], wid=[.72, .90, .97, 1, 1, 1, .98, .93, .76]),
    "suv": dict(L=4.45, W=1.86, H=1.66,
                xs=[0, .03, .10, .26, .40, .80, .94, .98, 1], top=[.50, .66, .74, .84, 1, .99, .74, .62, .46],
                belt=[.50, .64, .70, .62, .62, .64, .66, .62, .46], wid=[.74, .92, .98, 1, 1, 1, .98, .93, .78]),
    "utilitaire": dict(L=4.95, W=1.92, H=1.96,
                       xs=[0, .03, .08, .17, .27, .90, .97, .99, 1], top=[.42, .56, .64, .78, 1, 1, .98, .90, .40],
                       belt=[.42, .54, .58, .52, .52, .99, .97, .90, .40], wid=[.76, .93, .98, 1, 1, 1, 1, .97, .85]),
}
PAINTS = [((0.80, 0.80, 0.79), 25), ((0.33, 0.34, 0.36), 16), ((0.025, 0.025, 0.03), 18), ((0.55, 0.56, 0.58), 12),
          ((0.06, 0.12, 0.30), 8), ((0.38, 0.03, 0.03), 6), ((0.10, 0.11, 0.13), 6), ((0.50, 0.42, 0.30), 3),
          ((0.07, 0.20, 0.16), 2), ((0.60, 0.20, 0.04), 2), ((0.20, 0.30, 0.42), 2)]


def _mat(name, color, rough, metal=0.0, coat=0.0, emit=None):
    m = bpy.data.materials.new(name)
    b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED") if m.node_tree else None
    if b is None:
        m.use_nodes = True
        b = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if coat:
        b.inputs["Coat Weight"].default_value = coat
        b.inputs["Coat Roughness"].default_value = 0.06
    if emit:
        b.inputs["Emission Color"].default_value = (*emit[0], 1)
        b.inputs["Emission Strength"].default_value = emit[1]
    return m


def _spline(pts, sub):
    """Catmull-Rom uniforme sur une polyligne ouverte : `sub` points par segment (extrémités conservées)."""
    P = np.asarray(pts, float)
    Q = np.vstack([2 * P[0] - P[1], P, 2 * P[-1] - P[-2]])
    out = []
    for i in range(len(P) - 1):
        p0, p1, p2, p3 = Q[i], Q[i + 1], Q[i + 2], Q[i + 3]
        for k in range(sub):
            t_ = k / sub
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t_ + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t_ ** 2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t_ ** 3))
    out.append(P[-1])
    return np.array(out)


def _body(t):
    """Maillage d'un gabarit. Matériaux : 0 peinture, 1 vitre, 2 plastique sombre, 3 pneu, 4 jante, 5 feu avant, 6 feu arrière."""
    import bmesh
    from mathutils import Matrix
    L, W, H = t["L"], t["W"], t["H"]
    NS, SUB = 46, 3
    xn = np.linspace(0, 1, NS)

    def prof(key):
        a_ = np.interp(xn, t["xs"], t[key])
        for _ in range(2):
            a2 = np.convolve(np.pad(a_, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
            a_[1:-1] = a2[1:-1]
        return a_

    top, belt, wid = prof("top") * H, prof("belt") * H, prof("wid") * W / 2
    belt = np.minimum(belt, top - 0.015)
    z0 = 0.19 + 0.15 * np.clip(1 - np.minimum(xn, 1 - xn) / 0.06, 0, 1) ** 2
    rings, NC = [], 10        # NC = segments grossiers par demi-anneau
    for i in range(NS):
        w, zb, zt, zl = wid[i], belt[i], top[i], z0[i]
        gh = max(0.0, zt - zb)
        wg = w - min(0.22, gh * 0.45)
        zm = (zl + zb) / 2
        half = [(0.0, zl), (w * 0.55, zl), (w * 0.93, zl + 0.03), (w, zl + 0.17), (w * 1.012, zm), (w * 1.004, zb - 0.05), (w - 0.02, zb),
                (wg + 0.035, zt - 0.07), (wg - 0.09, zt - 0.008), (wg * 0.45, zt + 0.012), (0.0, zt + 0.018)]
        hs = _spline(half, SUB)
        hs[0, 0] = hs[-1, 0] = 0.0
        ring = [tuple(q) for q in hs] + [(-y, z) for y, z in hs[-2:0:-1]]
        rings.append([(L / 2 - xn[i] * L, y, z) for y, z in ring])
    NHF = NC * SUB + 1          # points fins par demi-anneau
    M = len(rings[0])
    v = [p for r in rings for p in r]
    f, mats = [], []
    for i in range(NS - 1):
        glass_here = (top[i] - belt[i] > 0.22) and (top[i + 1] - belt[i + 1] > 0.22)
        sloped = abs(top[i + 1] - top[i]) / max((xn[i + 1] - xn[i]) * L, 1e-3) > 0.14
        for j in range(M):
            a, b = i * M + j, i * M + (j + 1) % M
            c, d = (i + 1) * M + (j + 1) % M, (i + 1) * M + j
            f.append((a, d, c, b))
            jf = j if j < NHF - 1 else (M - 1 - j)
            jj = jf // SUB
            m = 0
            if jj <= 1:
                m = 2
            elif jj == 6 and glass_here:
                m = 1
            elif jj >= 7 and glass_here and sloped:
                m = 1
            mats.append(m)
    for i, front in ((0, True), (NS - 1, False)):
        for j in range(NHF - 1):
            jj = j // SUB
            lft = [i * M + j, i * M + j + 1]
            rgt = [i * M + (M - j) % M, i * M + (M - j - 1) % M]
            p0, p1 = np.array(v[lft[0]]), np.array(v[lft[1]])
            q0, q1 = np.array(v[rgt[0]]), np.array(v[rgt[1]])
            K = 6
            base = len(v)
            for k in range(K + 1):
                tt = k / K
                v.append(tuple(p0 + (q0 - p0) * tt))
                v.append(tuple(p1 + (q1 - p1) * tt))
            for k in range(K):
                a, b, c, d = base + 2 * k, base + 2 * k + 1, base + 2 * k + 3, base + 2 * k + 2
                f.append((a, b, c, d) if front else (a, d, c, b))
                outer = k in (0, K - 1)
                m = 0
                if jj <= 2:
                    m = 2
                elif jj == 4:
                    m = (5 if front else 6) if outer else (2 if front else 0)
                mats.append(m)
    me = bpy.data.meshes.new("car")
    me.from_pydata(v, [], f)
    me.polygons.foreach_set("material_index", mats)
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.001)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    for fc in bm.faces:
        fc.smooth = True
    wb_f, wb_r = L / 2 - L * 0.185, -L / 2 + L * 0.19
    R = 0.31 if H < 1.6 else 0.35
    rot = Matrix.Rotation(math.pi / 2, 3, "X")
    for x in (wb_f, wb_r):
        for sy in (-1, 1):
            # passage de roue (disque sombre en légère saillie), pneu, jante
            for rad, wd, mi, yy in ((R + 0.07, 0.05, 2, W / 2 * 1.012 - 0.005), (R, 0.22, 3, W / 2 - 0.06), (R * 0.64, 0.226, 4, W / 2 - 0.059)):
                ret = bmesh.ops.create_cone(bm, cap_ends=True, segments=22, radius1=rad, radius2=rad, depth=wd)
                vs = ret["verts"]
                bmesh.ops.rotate(bm, verts=vs, cent=(0, 0, 0), matrix=rot)
                bmesh.ops.translate(bm, verts=vs, vec=(x, sy * yy, R if mi != 2 else R + 0.01))
                for fc in {fc for vv in vs for fc in vv.link_faces}:
                    fc.material_index = mi
                    fc.smooth = False
    xi = int(np.argmin(np.abs(xn - (t["xs"][3] + 0.04))))
    for sy in (-1, 1):
        ret = bmesh.ops.create_cube(bm, size=1.0)
        vs = ret["verts"]
        bmesh.ops.scale(bm, verts=vs, vec=(0.08, 0.16, 0.10))
        bmesh.ops.translate(bm, verts=vs, vec=(L / 2 - xn[xi] * L, sy * (wid[xi] + 0.07), belt[xi] + 0.06))
    bm.to_mesh(me)
    bm.free()
    try:
        me.set_sharp_from_angle(angle=math.radians(38))
    except Exception:
        pass
    return me


def build(cars, parent_col, glow=0.0):
    """cars : [x, y, z, angle, graine, roule]. Crée les objets (maillages partagés par gabarit × teinte)."""
    glass = _mat("auto_vitre", (0.02, 0.028, 0.034), 0.04, coat=1.0)
    plast = _mat("auto_plastique", (0.03, 0.03, 0.032), 0.55)
    tyre = _mat("auto_pneu", (0.018, 0.018, 0.018), 0.85)
    rim = _mat("auto_jante", (0.55, 0.56, 0.58), 0.3, metal=1.0)
    front = _mat("auto_feu_av", (0.75, 0.76, 0.78), 0.12, metal=0.6, coat=1.0, emit=((1, 0.95, 0.85), glow))
    rear = _mat("auto_feu_ar", (0.35, 0.02, 0.02), 0.2, coat=1.0, emit=((1, 0.05, 0.03), glow * 0.6))
    paints = [_mat(f"auto_peinture_{i}", c, 0.42, metal=0.0, coat=0.8) for i, (c, _) in enumerate(PAINTS)]
    weights = np.array([w for _, w in PAINTS], float)
    weights /= weights.sum()
    names = list(TYPES)
    tw = np.array([0.24, 0.30, 0.12, 0.26, 0.08])
    base = {k: _body(TYPES[k]) for k in names}
    cache = {}
    for i, (x, y, z, ang, seed, moving) in enumerate(cars):
        r = np.random.default_rng(seed)
        k = names[r.choice(len(names), p=tw)]
        pi = int(r.choice(len(PAINTS), p=weights))
        if k == "utilitaire" and r.random() < 0.7:
            pi = 0
        key = (k, pi)
        if key not in cache:
            me = base[k].copy()
            for m in (paints[pi], glass, plast, tyre, rim, front, rear):
                me.materials.append(m)
            cache[key] = me
        ob = bpy.data.objects.new(f"voiture_{i}", cache[key])
        ob.location = (x, y, z + 0.01)
        ob.rotation_euler = (0, 0, ang)
        parent_col.objects.link(ob)
    return len(cars)


# ------------------------------------------------------------------ bateaux au mouillage
def _hull(L, W, depth=0.55, free=0.55, sheer=0.25, stations=14, bow=1.6):
    """Coque simple : sections en U, étrave pointue, tableau arrière droit. Matériaux : 0 coque, 1 pont, 2 capote/cabine."""
    v, f, m = [], [], []
    R = 7
    for i in range(stations):
        t = i / (stations - 1)                      # 0 = poupe, 1 = étrave
        w = W / 2 * (1 - max(0.0, (t - 0.28) / 0.72) ** bow) * (0.80 + 0.20 * min(1.0, t / 0.15))
        w = max(w, 0.02)
        zt = free + sheer * t ** 2
        x = -L / 2 + L * t
        for k in range(R):
            a = math.pi * k / (R - 1)
            v.append((x, -w * math.cos(a), zt - (zt + depth) * math.sin(a) ** 0.7))
    for i in range(stations - 1):
        for k in range(R - 1):
            a, b = i * R + k, i * R + k + 1
            f.append((a, b, b + R, a + R)); m.append(0)
        f.append((i * R + R - 1, i * R, (i + 1) * R, (i + 1) * R + R - 1)); m.append(1)   # pont
    f.append(tuple(range(R))); m.append(0)                                               # tableau
    me = bpy.data.meshes.new("coque")
    me.from_pydata(v, [], f)
    me.polygons.foreach_set("material_index", m)
    me.shade_smooth()
    return me


def _box(me_list, x0, x1, w, z0, z1, mat_i):
    v = [(x0, -w, z0), (x1, -w, z0), (x1, w, z0), (x0, w, z0), (x0, -w * 0.88, z1), (x1, -w * 0.8, z1), (x1, w * 0.8, z1), (x0, w * 0.88, z1)]
    f = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]
    me_list.append((v, f, mat_i))


def build_boats(boats, parent_col, water_z):
    import bmesh
    white = _mat("bateau_blanc", (0.82, 0.82, 0.80), 0.35, coat=0.5)
    deck = _mat("bateau_pont", (0.42, 0.40, 0.36), 0.7)
    canvas = [_mat(f"bateau_capote_{i}", c, 0.8) for i, c in enumerate([(0.05, 0.10, 0.25), (0.75, 0.75, 0.72), (0.35, 0.05, 0.05), (0.08, 0.20, 0.16)])]
    pin = [_mat(f"pinasse_{i}", c, 0.4, coat=0.5) for i, c in enumerate([(0.05, 0.22, 0.14), (0.06, 0.12, 0.35), (0.45, 0.06, 0.05), (0.55, 0.42, 0.10)])]
    mast = _mat("bateau_mat", (0.75, 0.76, 0.78), 0.3, metal=0.8)
    cache = {}
    for i, (x, y, ang, kind, seed) in enumerate(boats):
        r = np.random.default_rng(seed)
        var = int(r.integers(4))
        key = (kind, var)
        if key not in cache:
            if kind == "pinasse":
                L, W = float(r.uniform(9.5, 11.5)), 2.1
                me = _hull(L, W, depth=0.45, free=0.6, sheer=0.45, bow=1.15)
                parts = []
                _box(parts, -L * 0.18, L * 0.12, W * 0.36, 0.55, 1.25, 2)
                mats = [pin[var], deck, white, mast]
            elif kind == "voilier":
                L, W = float(r.uniform(7.5, 10.5)), 2.8
                me = _hull(L, W, depth=0.9, free=0.8, sheer=0.2, bow=1.2)
                parts = []
                _box(parts, -L * 0.15, L * 0.18, W * 0.3, 0.8, 1.3, 2)
                _box(parts, L * 0.10, L * 0.10 + 0.12, 0.06, 1.2, L * 1.15, 3)
                mats = [white, deck, white, mast]
            else:
                L, W = float(r.uniform(5.0, 7.5)), 2.3
                me = _hull(L, W, depth=0.5, free=0.6, sheer=0.2, bow=1.25)
                parts = []
                if var % 2 == 0:
                    _box(parts, -L * 0.10, L * 0.15, W * 0.36, 0.6, 1.5, 2)
                else:
                    _box(parts, -L * 0.42, L * 0.30, W * 0.44, 0.55, 0.78, 2)   # taud
                _box(parts, -L / 2 - 0.35, -L / 2, 0.16, 0.0, 0.85, 3)          # moteur hors-bord
                mats = [white, deck, canvas[var] if var % 2 else white, mast if var % 2 == 0 else canvas[0]]
            bm = bmesh.new()
            bm.from_mesh(me)
            for pv, pf, mi in parts:
                vs = [bm.verts.new(p) for p in pv]
                for fc in pf:
                    face = bm.faces.new([vs[k] for k in fc])
                    face.material_index = mi
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(me)
            bm.free()
            for mm in mats:
                me.materials.append(mm)
            cache[key] = me
        ob = bpy.data.objects.new(f"bateau_{i}", cache[key])
        ob.location = (x, y, water_z - 0.02)
        ob.rotation_euler = (float(r.uniform(-0.02, 0.02)), 0, ang)
        parent_col.objects.link(ob)
    return len(boats)

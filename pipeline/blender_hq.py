"""Scène Blender « haute qualité » d'un secteur Atlas + rendus Cycles.

Usage : Blender -b -P blender_hq.py -- <dossier_secteur> [--shots a,b,c] [--samples 256] [--res 1920x1080] [--no-render]
Entrées : raw/ (mnt, ortho, contexte) et build/hq/ (hq_prepare.py). Assets : pipeline/assets (Poly Haven, CC0).
Sorties : build/hq/arcachon_hq.blend, build/hq/renders/<plan>.png
"""
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

ARGS = sys.argv[sys.argv.index("--") + 1:]
SEC = Path(ARGS[0]).resolve()
OPT = {a.split("=")[0].lstrip("-"): (a.split("=", 1)[1] if "=" in a else True) for a in ARGS[1:]}
ASSETS = Path(__file__).resolve().parent / "assets"
RAW, HQ = SEC / "raw", SEC / "build" / "hq"
CFG = json.loads((SEC / "sector.json").read_text())
SIZE = float(CFG["size_m"])
WATER = float(CFG.get("water_level_m", 0.6))
T0 = time.time()


def log(m):
    print(f"[hq {time.time() - T0:6.1f}s] {m}", flush=True)


# ------------------------------------------------------------------ utilitaires nœuds
def sock(node, name, out=False):
    coll = node.outputs if out else node.inputs
    for s in coll:
        if s.identifier == name or s.name == name:
            if s.enabled or True:
                return s
    raise KeyError(f"{node.bl_idname}: {name} ({[s.name for s in coll]})")


class NT:
    """Petit DSL pour construire des arbres de nœuds shader."""

    def __init__(self, mat):
        mat.use_nodes = True
        self.t = mat.node_tree
        self.t.nodes.clear()
        self.x = 0

    def n(self, kind, **props):
        node = self.t.nodes.new(kind)
        node.location = (self.x, 0)
        self.x += 40
        for k, v in props.items():
            setattr(node, k, v)
        return node

    def link(self, a, b):
        self.t.links.new(a, b)

    def val(self, src, default=0.0):
        """Accepte un socket ou une constante."""
        return src

    def math(self, op, a, b=None, clamp=False):
        m = self.n("ShaderNodeMath", operation=op, use_clamp=clamp)
        for i, v in enumerate([a, b]):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                m.inputs[i].default_value = v
            else:
                self.link(v, m.inputs[i])
        return m.outputs[0]

    def mix(self, fac, a, b):
        m = self.n("ShaderNodeMix", data_type="RGBA", blend_type="MIX", clamp_factor=True)
        for nm, v in (("Factor_Float", fac), ("A_Color", a), ("B_Color", b)):
            s = sock(m, nm)
            if isinstance(v, (tuple, list)):
                s.default_value = (*v, 1.0) if len(v) == 3 else v
            elif isinstance(v, (int, float)):
                s.default_value = v
            else:
                self.link(v, s)
        return sock(m, "Result_Color", out=True)

    def mixf(self, fac, a, b):
        m = self.n("ShaderNodeMix", data_type="FLOAT", clamp_factor=True)
        for nm, v in (("Factor_Float", fac), ("A_Float", a), ("B_Float", b)):
            s = sock(m, nm)
            if isinstance(v, (int, float)):
                s.default_value = v
            else:
                self.link(v, s)
        return sock(m, "Result_Float", out=True)

    def img(self, path, vec, colorspace="sRGB", interp="Linear"):
        im = bpy.data.images.load(str(path), check_existing=True)
        im.colorspace_settings.name = colorspace
        n = self.n("ShaderNodeTexImage", image=im, interpolation=interp)
        if vec is not None:
            self.link(vec, n.inputs["Vector"])
        return n

    def scaled(self, vec, s):
        m = self.n("ShaderNodeMapping")
        self.link(vec, m.inputs["Vector"])
        m.inputs["Scale"].default_value = (s, s, s)
        return m.outputs[0]

    def attr(self, name):
        a = self.n("ShaderNodeAttribute", attribute_name=name, attribute_type="GEOMETRY")
        return a

    def uv(self, name):
        u = self.n("ShaderNodeUVMap", uv_map=name)
        sep = self.n("ShaderNodeSeparateXYZ")
        self.link(u.outputs[0], sep.inputs[0])
        return u.outputs[0], sep.outputs[0], sep.outputs[1]

    def n_sep_sum(self, color):
        sep = self.n("ShaderNodeSeparateColor")
        self.link(color, sep.inputs[0])
        return self.math("ADD", self.math("ADD", sep.outputs[0], sep.outputs[1]), sep.outputs[2])

    def bsdf(self):
        b = self.n("ShaderNodeBsdfPrincipled")
        out = self.n("ShaderNodeOutputMaterial")
        self.link(b.outputs[0], out.inputs["Surface"])
        return b, out

    def set_in(self, node, name, v):
        s = sock(node, name)
        if isinstance(v, (int, float)):
            s.default_value = v
        elif isinstance(v, (tuple, list)):
            s.default_value = (*v, 1.0) if len(v) == 3 and len(s.default_value) == 4 else v
        else:
            self.link(v, s)


def tex_set(name):
    d = ASSETS / "textures" / name
    return {k: next(iter(d.glob(f"{k}.*")), None) for k in ("diff", "nor", "rough", "disp")}


# ------------------------------------------------------------------ maillages rapides
def make_mesh(name, v, f, uvs=None, mats=None, mat_index=None, face_attrs=None, smooth=False):
    me = bpy.data.meshes.new(name)
    v = np.asarray(v, np.float32)
    f = np.asarray(f, np.int32)
    me.vertices.add(len(v))
    me.vertices.foreach_set("co", v.ravel())
    me.loops.add(f.size)
    me.loops.foreach_set("vertex_index", f.ravel())
    me.polygons.add(len(f))
    me.polygons.foreach_set("loop_start", np.arange(0, f.size, 3, dtype=np.int32))
    me.update(calc_edges=True)
    for uvname, arr in (uvs or {}).items():
        l = me.uv_layers.new(name=uvname)
        l.uv.foreach_set("vector", np.asarray(arr, np.float32).ravel())
    for m in mats or []:
        me.materials.append(m)
    if mat_index is not None:
        me.polygons.foreach_set("material_index", np.asarray(mat_index, np.int32))
    for an, (typ, arr) in (face_attrs or {}).items():
        a = me.attributes.new(an, typ, "FACE")
        if typ == "FLOAT_COLOR":
            rgba = np.ones((len(arr), 4), np.float32)
            rgba[:, :3] = arr
            a.data.foreach_set("color", rgba.ravel())
        else:
            a.data.foreach_set("value", np.asarray(arr, np.float32))
    if smooth:
        me.shade_smooth()
    me.validate()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def grid_mesh(name, H, x0, y0, sx, sy, z_off=0.0, smooth=True):
    """Grille régulière : H[ligne 0 = nord]. UV 0..1 (v=1 au nord)."""
    rows, cols = H.shape
    xs = x0 + np.arange(cols) * sx
    ys = y0 + (rows - 1 - np.arange(rows)) * sy
    X, Y = np.meshgrid(xs, ys)
    v = np.column_stack([X.ravel(), Y.ravel(), H.ravel() + z_off])
    idx = np.arange(rows * cols).reshape(rows, cols)
    a, b, c, d = idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:], idx[:-1, :-1]  # a=sud-ouest
    f = np.vstack([np.column_stack([a.ravel(), b.ravel(), c.ravel()]), np.column_stack([a.ravel(), c.ravel(), d.ravel()])])
    U = (np.arange(cols) / (cols - 1))[None, :].repeat(rows, 0).ravel()
    V = (1 - np.arange(rows) / (rows - 1))[:, None].repeat(cols, 1).ravel()
    uv = np.column_stack([U, V])[f.ravel()]
    return make_mesh(name, v, f, uvs={"UVMap": uv}, smooth=smooth)


# ------------------------------------------------------------------ matériaux
def mat_ground():
    m = bpy.data.materials.new("sol")
    t = NT(m)
    b, _ = t.bsdf()
    uvn = t.n("ShaderNodeUVMap", uv_map="UVMap")
    tc = t.n("ShaderNodeTexCoord")
    obj = tc.outputs["Object"]
    ortho = t.img(RAW / "ortho.jpg", uvn.outputs[0])
    ma = t.img(HQ / "ground_mask_a.png", uvn.outputs[0], "Non-Color")
    mb = t.img(HQ / "ground_mask_b.png", uvn.outputs[0], "Non-Color")
    sa = t.n("ShaderNodeSeparateColor"); t.link(ma.outputs[0], sa.inputs[0])
    sb = t.n("ShaderNodeSeparateColor"); t.link(mb.outputs[0], sb.inputs[0])
    road, walk, sand = sa.outputs[0], sa.outputs[1], sa.outputs[2]
    grass = sb.outputs[0]
    sets = {"asphalt": (tex_set("asphalt_02"), 1 / 4.0), "walk": (tex_set("patterned_paving"), 1 / 2.5),
            "sand": (tex_set("coast_sand_01"), 1 / 3.0), "grass": (tex_set("leafy_grass"), 1 / 2.5)}
    col, height, rough = {}, {}, {}
    for k, (ts, s) in sets.items():
        vec = t.scaled(obj, s)
        col[k] = t.img(ts["diff"], vec).outputs[0]
        height[k] = t.img(ts["disp"], vec, "Non-Color").outputs[0] if ts["disp"] else None
        rough[k] = t.img(ts["rough"], vec, "Non-Color").outputs[0] if ts["rough"] else None
    # base : orthophoto légèrement désaturée (les ombres sont déjà dedans)
    hsv = t.n("ShaderNodeHueSaturation")
    t.link(ortho.outputs[0], hsv.inputs["Color"]); hsv.inputs["Saturation"].default_value = 0.9
    c = hsv.outputs[0]
    # grains et matières réelles par-dessus selon les masques OSM
    dark = lambda x, k: t.mix(1.0 - k, (0.0, 0.0, 0.0), x)
    asphalt_tinted = t.mix(0.2, dark(col["asphalt"], 0.5), c)
    c = t.mix(road, c, asphalt_tinted)
    ss = json.loads((HQ / "street_style.json").read_text()) if (HQ / "street_style.json").exists() else {}
    walk_rgb = (0.45, 0.30, 0.26)
    cols_ = [c_ for c_ in ss.get("sidewalk_colors", []) if c_]
    if cols_:
        arr = np.array([[int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)] for h in cols_])
        walk_rgb = tuple(float(x) for x in np.median(arr, 0))
    walk_tex = t.n("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY", clamp_factor=True)
    sock(walk_tex, "Factor_Float").default_value = 1.0
    t.link(col["walk"], sock(walk_tex, "A_Color")); sock(walk_tex, "B_Color").default_value = (*[min(1, x * 1.6) for x in walk_rgb], 1)
    c = t.mix(walk, c, dark(t.mix(0.12, sock(walk_tex, "Result_Color", out=True), c), 0.8))
    warm = t.n("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY", clamp_factor=True)
    sock(warm, "Factor_Float").default_value = 1.0
    t.link(t.mix(0.5, col["sand"], c), sock(warm, "A_Color"))
    sock(warm, "B_Color").default_value = (0.82, 0.70, 0.55, 1)
    c = t.mix(sand, c, sock(warm, "Result_Color", out=True))
    c = t.mix(t.math("MULTIPLY", grass, 0.7), c, t.mix(0.4, col["grass"], c))
    # sable mouillé : bande de 1 m au-dessus du niveau de l'eau (plus sombre, brillant)
    sep = t.n("ShaderNodeSeparateXYZ"); t.link(obj, sep.inputs[0])
    zrel = t.math("SUBTRACT", sep.outputs[2], WATER)
    wet = t.math("MULTIPLY", sand, t.math("SUBTRACT", 1.0, t.math("DIVIDE", t.math("MAXIMUM", zrel, 0.0), 1.1), clamp=True))
    c = t.mix(t.math("MULTIPLY", wet, 0.55), c, (0.22, 0.17, 0.12))
    # marquages au sol (peinture blanche, un peu usée)
    mark = sb.outputs[2]
    wear = t.n("ShaderNodeTexNoise"); t.link(obj, wear.inputs["Vector"]); wear.inputs["Scale"].default_value = 2.5
    mark = t.math("MULTIPLY", mark, t.math("ADD", 0.55, t.math("MULTIPLY", wear.outputs["Fac"], 0.5)), clamp=True)
    c = t.mix(mark, c, (0.82, 0.82, 0.80))
    t.set_in(b, "Base Color", c)
    r = t.mixf(road, 0.92, rough["asphalt"] or 0.8)
    r = t.mixf(sand, r, 0.95)
    r = t.mixf(wet, r, 0.22)
    r = t.mixf(mark, r, 0.6)
    t.set_in(b, "Roughness", r)
    # relief fin
    h = t.math("MULTIPLY", height["asphalt"], road)
    h = t.math("ADD", h, t.math("MULTIPLY", height["walk"], walk))
    h = t.math("ADD", h, t.math("MULTIPLY", height["sand"], sand))
    bump = t.n("ShaderNodeBump")
    bump.inputs["Distance"].default_value = 0.03
    t.link(h, bump.inputs["Height"])
    t.set_in(b, "Normal", bump.outputs[0])
    return m


def mat_ctx():
    m = bpy.data.materials.new("sol_contexte")
    t = NT(m)
    b, _ = t.bsdf()
    uvn = t.n("ShaderNodeUVMap", uv_map="UVMap")
    o = t.img(RAW / "ctx_ortho.jpg", uvn.outputs[0])
    tc = t.n("ShaderNodeTexCoord")
    dist = t.n("ShaderNodeVectorMath", operation="DISTANCE")
    t.link(tc.outputs["Object"], dist.inputs[0])
    dist.inputs[1].default_value = (SIZE / 2, SIZE / 2, 0)
    haze = t.math("MINIMUM", t.math("MAXIMUM", t.math("DIVIDE", t.math("SUBTRACT", sock(dist, "Value", out=True), 700), 2800), 0.0), 0.85)
    t.set_in(b, "Base Color", t.mix(haze, o.outputs[0], (0.42, 0.47, 0.55)))
    t.set_in(b, "Roughness", 0.95)
    return m


def mat_wall(name, texname, use_tint, tex_scale, shutters=True):
    m = bpy.data.materials.new(name)
    t = NT(m)
    b, _ = t.bsdf()
    uv0, u, v = t.uv("UV0")
    _, seglen, walltop = t.uv("UV1")
    tint = t.attr("tint").outputs["Color"]
    seed = t.attr("seed").outputs["Fac"]
    # données de façade (photo Panoramax / BD TOPO), cf. hq_prepare.FA_DEFAULT
    fl_h = t.attr("fa_floor").outputs["Fac"]
    shop_a = t.attr("fa_shop").outputs["Fac"]
    shm = t.attr("fa_shut").outputs["Fac"]
    shut_c = t.attr("fa_shut_col").outputs["Color"]
    trim_on = t.attr("fa_trim").outputs["Fac"]
    trim_c = t.attr("fa_trim_col").outputs["Color"]
    ts = tex_set(texname)
    vec = t.scaled(uv0, tex_scale)
    base = t.img(ts["diff"], vec).outputs[0]
    if use_tint:
        # enduit : on garde le grain de la texture, la couleur vient de la teinte du bâtiment
        bw = t.n("ShaderNodeRGBToBW"); t.link(base, bw.inputs[0])
        g = t.math("ADD", t.math("MULTIPLY", bw.outputs[0], 0.35), 0.66)  # grain sans éteindre la couleur relevée
        mulc = t.n("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY", clamp_factor=True)
        sock(mulc, "Factor_Float").default_value = 1.0
        comb = t.n("ShaderNodeCombineColor")
        for i in range(3):
            t.link(g, comb.inputs[i])
        t.link(tint, sock(mulc, "A_Color")); t.link(comb.outputs[0], sock(mulc, "B_Color"))
        base = sock(mulc, "Result_Color", out=True)
    # crasse en pied de mur + variation basse fréquence
    noise = t.n("ShaderNodeTexNoise"); t.link(uv0, noise.inputs["Vector"]); noise.inputs["Scale"].default_value = 0.35
    dirt = t.math("MULTIPLY", t.math("SUBTRACT", 1.0, t.math("MINIMUM", t.math("DIVIDE", v, 1.2), 1.0)), 0.35)
    dirt = t.math("ADD", dirt, t.math("MULTIPLY", noise.outputs["Fac"], 0.12))
    base = t.mix(dirt, base, (0.18, 0.16, 0.14))
    # ---- fenêtres procédurales
    S, WW, SILL, WH = 2.9, 1.15, 0.95, 1.55
    F = t.mixf(t.math("GREATER_THAN", fl_h, 0.5), 3.0, fl_h)  # hauteur d'étage réelle si connue
    nw = t.math("FLOOR", t.math("DIVIDE", seglen, S))
    span = t.math("MULTIPLY", nw, S)
    xm = t.math("ADD", u, t.math("MULTIPLY", span, 0.5))
    col = t.math("SUBTRACT", t.math("MULTIPLY", t.math("FRACT", t.math("DIVIDE", xm, S)), S), S / 2)
    acol = t.math("ABSOLUTE", col)
    in_span = t.math("LESS_THAN", t.math("ABSOLUTE", u), t.math("SUBTRACT", t.math("MULTIPLY", span, 0.5), 0.1))
    has = t.math("GREATER_THAN", nw, 0.5)
    vf = t.math("MULTIPLY", t.math("FRACT", t.math("DIVIDE", v, F)), F)
    in_y = t.math("MULTIPLY", t.math("GREATER_THAN", vf, SILL), t.math("LESS_THAN", vf, SILL + WH))
    in_y = t.math("MULTIPLY", in_y, t.math("GREATER_THAN", v, 0.5))
    in_y = t.math("MULTIPLY", in_y, t.math("LESS_THAN", v, t.math("SUBTRACT", walltop, 0.6)))
    zone = t.math("MULTIPLY", t.math("MULTIPLY", in_span, has), in_y)
    win = t.math("MULTIPLY", zone, t.math("LESS_THAN", acol, WW / 2))
    # rez-de-chaussée commercial (immeubles > 9 m, ~65 %) : grandes vitrines
    shop = t.math("MULTIPLY", t.math("LESS_THAN", v, 3.0), t.math("GREATER_THAN", walltop, 9.0))
    shop_rand = t.math("GREATER_THAN", t.math("FRACT", t.math("MULTIPLY", seed, 3.7)), 0.35)
    known = t.math("GREATER_THAN", shop_a, -0.5)
    shop = t.math("MULTIPLY", t.math("LESS_THAN", v, 3.0), t.mixf(known, t.math("GREATER_THAN", walltop, 9.0), 1.0))
    shop = t.math("MULTIPLY", shop, t.mixf(known, shop_rand, shop_a))
    shop = t.math("MULTIPLY", shop, t.math("MULTIPLY", in_span, has))
    shop_win = t.math("MULTIPLY", shop, t.math("LESS_THAN", acol, S / 2 - 0.3))
    shop_win = t.math("MULTIPLY", shop_win, t.math("MULTIPLY", t.math("GREATER_THAN", v, 0.25), t.math("LESS_THAN", v, 2.7)))
    zone = t.math("MULTIPLY", zone, t.math("SUBTRACT", 1.0, shop))
    win = t.math("MAXIMUM", t.math("MULTIPLY", win, t.math("SUBTRACT", 1.0, shop)), shop_win)
    # cadre : bande de 9 cm autour du vitrage
    vf_in = t.math("MULTIPLY", t.math("GREATER_THAN", vf, SILL + 0.09), t.math("LESS_THAN", vf, SILL + WH - 0.09))
    glass = t.math("MULTIPLY", t.math("MULTIPLY", win, t.math("LESS_THAN", acol, WW / 2 - 0.09)), vf_in)
    shop_glass = t.math("MULTIPLY", shop_win, t.math("MULTIPLY", t.math("LESS_THAN", acol, S / 2 - 0.4), t.math("GREATER_THAN", v, 0.35)))
    shop_glass = t.math("MULTIPLY", shop_glass, t.math("GREATER_THAN", t.math("FRACT", t.math("DIVIDE", xm, 1.25)), 0.04))  # meneaux
    glass = t.math("MAXIMUM", glass, shop_glass)
    frame = t.math("SUBTRACT", win, glass)
    # volets (villas surtout)
    shut = t.math("MULTIPLY", t.math("GREATER_THAN", acol, WW / 2 + 0.03), t.math("LESS_THAN", acol, WW / 2 + 0.03 + WW / 2))
    shut = t.math("MULTIPLY", shut, zone)
    # volets : -1 hasard (ancien comportement), 0 aucun, 1 battants, 2 roulants (pas de volets latéraux)
    rand_on = t.math("GREATER_THAN", seed, 0.3 if shutters else 2.0)
    shut_known = t.math("GREATER_THAN", shm, -0.5)
    side_on = t.mixf(shut_known, rand_on, t.math("COMPARE", shm, 1.0))
    shut = t.math("MULTIPLY", shut, side_on)
    roll = t.math("MULTIPLY", t.math("COMPARE", shm, 2.0), zone)
    ramp = t.n("ShaderNodeValToRGB")
    ramp.color_ramp.interpolation = "CONSTANT"
    pal = [(0.08, 0.18, 0.34), (0.30, 0.40, 0.30), (0.32, 0.07, 0.09), (0.88, 0.88, 0.85), (0.38, 0.46, 0.52), (0.10, 0.36, 0.38)]
    els = ramp.color_ramp.elements
    while len(els) < len(pal):
        els.new(0.5)
    for i, cc in enumerate(pal):
        els[i].position = i / len(pal)
        els[i].color = (*cc, 1)
    t.link(t.math("FRACT", t.math("MULTIPLY", seed, 7.31)), ramp.inputs[0])
    # lames de volet
    slats = t.math("GREATER_THAN", t.math("FRACT", t.math("MULTIPLY", vf, 9.0)), 0.18)
    has_col = t.math("GREATER_THAN", t.math("ADD", t.n_sep_sum(shut_c), 0.0), 0.02)
    pal_col = t.mix(t.math("MULTIPLY", shut_known, has_col), ramp.outputs[0], shut_c)
    shut_col = t.mix(t.math("MULTIPLY", t.math("SUBTRACT", 1.0, slats), 0.5), pal_col, (0.02, 0.02, 0.02))
    # chaînages brique : harpes d'angle (alternance 55/35 cm) + encadrements de baies
    harp_w = t.mixf(t.math("LESS_THAN", t.math("FRACT", t.math("DIVIDE", v, 0.5)), 0.5), 0.35, 0.55)
    corner = t.math("GREATER_THAN", t.math("ABSOLUTE", u), t.math("SUBTRACT", t.math("MULTIPLY", seglen, 0.5), harp_w))
    corner = t.math("MULTIPLY", corner, t.math("LESS_THAN", v, t.math("SUBTRACT", walltop, 0.05)))
    ring_x = t.math("MULTIPLY", t.math("LESS_THAN", acol, WW / 2 + 0.2), t.math("GREATER_THAN", t.math("ADD", vf, 0.0), SILL - 0.2))
    ring_y = t.math("LESS_THAN", vf, SILL + WH + 0.25)
    surround = t.math("MULTIPLY", t.math("MULTIPLY", ring_x, ring_y), t.math("MULTIPLY", in_span, has))
    surround = t.math("MULTIPLY", surround, t.math("MULTIPLY", t.math("GREATER_THAN", v, 0.5), t.math("LESS_THAN", v, t.math("SUBTRACT", walltop, 0.6))))
    surround = t.math("MULTIPLY", surround, t.math("SUBTRACT", 1.0, shop))
    trim = t.math("MULTIPLY", trim_on, t.math("MAXIMUM", corner, t.math("SUBTRACT", surround, win)), clamp=True)
    mortar = t.math("GREATER_THAN", t.math("FRACT", t.math("DIVIDE", v, 0.075)), 0.14)
    brick_c = t.mix(mortar, (0.78, 0.76, 0.72), trim_c)
    c = t.mix(trim, base, brick_c)
    c = t.mix(shut, c, shut_col)
    c = t.mix(frame, c, (0.86, 0.85, 0.82))
    # vitrage : sombre et réfléchissant ; quelques pièces éclairées
    wn = t.n("ShaderNodeTexWhiteNoise", noise_dimensions="3D")
    cell = t.n("ShaderNodeCombineXYZ")
    t.link(t.math("FLOOR", t.math("DIVIDE", xm, S)), cell.inputs[0])
    t.link(t.math("FLOOR", t.math("DIVIDE", v, F)), cell.inputs[1])
    t.link(t.math("MULTIPLY", seed, 97.0), cell.inputs[2])
    t.link(cell.outputs[0], wn.inputs["Vector"])
    lit = t.math("MULTIPLY", glass, t.math("GREATER_THAN", sock(wn, "Value", out=True), 0.86))
    lit = t.math("MAXIMUM", lit, t.math("MULTIPLY", shop_glass, t.math("MULTIPLY", t.math("SUBTRACT", 1.0, t.math("DIVIDE", v, 3.0)), 0.25)))
    glass_col = t.mix(t.math("MULTIPLY", sock(wn, "Value", out=True), 0.4), (0.015, 0.02, 0.025), (0.05, 0.05, 0.045))
    roll_band = t.math("MULTIPLY", t.math("MULTIPLY", roll, glass), t.math("GREATER_THAN", vf, SILL + WH * 0.55))
    roll_col = t.mix(t.math("MULTIPLY", t.math("GREATER_THAN", t.math("FRACT", t.math("MULTIPLY", vf, 22.0)), 0.25), 0.6), (0.18, 0.18, 0.19), (0.30, 0.30, 0.31))
    glass_col = t.mix(roll_band, glass_col, roll_col)
    c = t.mix(glass, c, glass_col)
    t.set_in(b, "Base Color", c)
    rough = t.mixf(glass, 0.85, 0.04)
    if ts["rough"]:
        tr = t.img(ts["rough"], vec, "Non-Color").outputs[0]
        rough = t.mixf(glass, tr, 0.04)
    t.set_in(b, "Roughness", rough)
    t.set_in(b, "Emission Color", (1.0, 0.72, 0.42))
    t.set_in(b, "Emission Strength", t.math("MULTIPLY", lit, float(OPT.get("windows_glow", 1.5))))
    # relief : fenêtres en retrait, volets en saillie, grain de la texture
    h = t.math("SUBTRACT", t.math("MULTIPLY", shut, 0.03), t.math("MULTIPLY", win, 0.12))
    h = t.math("ADD", h, t.math("MULTIPLY", t.math("SUBTRACT", 1.0, win), 0.0))
    bump = t.n("ShaderNodeBump"); bump.inputs["Distance"].default_value = 1.0; bump.inputs["Strength"].default_value = 1.0
    t.link(h, bump.inputs["Height"])
    if ts["nor"]:
        nm = t.n("ShaderNodeNormalMap"); nm.inputs["Strength"].default_value = 0.6
        t.link(t.img(ts["nor"], vec, "Non-Color").outputs[0], nm.inputs["Color"])
        t.link(nm.outputs[0], bump.inputs["Normal"])
    t.set_in(b, "Normal", bump.outputs[0])
    return m


def mat_roof(name, texname, scale, var=0.12, tint=None, ortho=0.55):
    m = bpy.data.materials.new(name)
    t = NT(m)
    b, _ = t.bsdf()
    uv0, _, _ = t.uv("UV0")
    seed = t.attr("seed").outputs["Fac"]
    ts = tex_set(texname)
    vec = t.scaled(uv0, scale)
    c = t.img(ts["diff"], vec).outputs[0]
    # variation par bâtiment (tuiles plus ou moins neuves / lichens)
    hsv = t.n("ShaderNodeHueSaturation")
    t.link(c, hsv.inputs["Color"])
    t.link(t.math("ADD", 0.5, t.math("MULTIPLY", t.math("SUBTRACT", seed, 0.5), var * 0.4)), hsv.inputs["Hue"])
    t.link(t.math("ADD", 0.8, t.math("MULTIPLY", seed, 0.4)), hsv.inputs["Value"])
    c = hsv.outputs[0]
    if tint:
        c = t.mix(0.6, c, tint)
    if ortho:
        # couleur réelle du toit vue d'avion (orthophoto IGN 20 cm) × grain de la texture (tuiles, ardoises)
        tc = t.n("ShaderNodeTexCoord")
        sep = t.n("ShaderNodeSeparateXYZ"); t.link(tc.outputs["Object"], sep.inputs[0])
        cmb = t.n("ShaderNodeCombineXYZ")
        t.link(t.math("DIVIDE", sep.outputs[0], SIZE), cmb.inputs[0])
        t.link(t.math("DIVIDE", sep.outputs[1], SIZE), cmb.inputs[1])
        oc = t.img(RAW / "ortho.jpg", cmb.outputs[0]).outputs[0]
        bw = t.n("ShaderNodeRGBToBW"); t.link(c, bw.inputs[0])
        grain = t.math("MINIMUM", t.math("MAXIMUM", t.math("DIVIDE", bw.outputs[0], 0.42), 0.55), 1.5)
        gcol = t.n("ShaderNodeCombineColor")
        for i in range(3):
            t.link(grain, gcol.inputs[i])
        mul = t.n("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY", clamp_factor=True)
        sock(mul, "Factor_Float").default_value = 1.0
        t.link(oc, sock(mul, "A_Color")); t.link(gcol.outputs[0], sock(mul, "B_Color"))
        c = t.mix(ortho, c, sock(mul, "Result_Color", out=True))
    t.set_in(b, "Base Color", c)
    if ts["rough"]:
        t.set_in(b, "Roughness", t.img(ts["rough"], vec, "Non-Color").outputs[0])
    if ts["nor"]:
        nm = t.n("ShaderNodeNormalMap"); nm.inputs["Strength"].default_value = 1.2
        t.link(t.img(ts["nor"], vec, "Non-Color").outputs[0], nm.inputs["Color"])
        t.set_in(b, "Normal", nm.outputs[0])
    return m


def mat_lambrequin():
    """Frise de bois découpé sous les rives (signature des villas arcachonnaises) : festons + jours ronds."""
    m = bpy.data.materials.new("lambrequin")
    t = NT(m)
    b, out = t.bsdf()
    uv0, u, v = t.uv("UV0")
    P_ = 0.22
    x = t.math("SUBTRACT", t.math("FRACT", t.math("DIVIDE", u, P_)), 0.5)
    arc = t.math("SQRT", t.math("MAXIMUM", t.math("SUBTRACT", 1.0, t.math("MULTIPLY", t.math("MULTIPLY", x, x), 4.0)), 0.0))
    edge = t.math("ADD", 0.04, t.math("MULTIPLY", t.math("SUBTRACT", 1.0, arc), 0.12))  # bas festonné
    solid = t.math("GREATER_THAN", v, edge)
    hx = t.math("MULTIPLY", x, P_)
    hy = t.math("SUBTRACT", v, 0.2)
    hole = t.math("LESS_THAN", t.math("ADD", t.math("MULTIPLY", hx, hx), t.math("MULTIPLY", hy, hy)), 0.0016)
    alpha = t.math("MULTIPLY", solid, t.math("SUBTRACT", 1.0, hole))
    t.set_in(b, "Base Color", (0.93, 0.91, 0.86))
    t.set_in(b, "Roughness", 0.6)
    t.set_in(b, "Alpha", alpha)
    return m


def mat_simple(name, color, rough, metal=0.0):
    m = bpy.data.materials.new(name)
    t = NT(m)
    b, _ = t.bsdf()
    t.set_in(b, "Base Color", color)
    t.set_in(b, "Roughness", rough)
    t.set_in(b, "Metallic", metal)
    return m


def mat_water():
    """Eau opaque et réfléchissante (pas de réfraction/volume : reflets propres, pas de taches au débruitage)."""
    m = bpy.data.materials.new("eau_bassin")
    t = NT(m)
    b, out = t.bsdf()
    tc = t.n("ShaderNodeTexCoord")
    # teinte : plus claire et turquoise près du rivage (eau peu profonde), sombre au large
    geo = t.n("ShaderNodeVectorMath", operation="DISTANCE")
    t.link(tc.outputs["Object"], geo.inputs[0])
    t.set_in(b, "Base Color", (0.015, 0.055, 0.065))
    t.set_in(b, "Roughness", 0.1)
    t.set_in(b, "IOR", 1.333)
    t.set_in(b, "Specular IOR Level", 0.6)
    n1 = t.n("ShaderNodeTexNoise"); t.link(tc.outputs["Object"], n1.inputs["Vector"]); n1.inputs["Scale"].default_value = 0.9; n1.inputs["Detail"].default_value = 10
    n2 = t.n("ShaderNodeTexNoise"); t.link(tc.outputs["Object"], n2.inputs["Vector"]); n2.inputs["Scale"].default_value = 4.0
    h = t.math("ADD", n1.outputs["Fac"], t.math("MULTIPLY", n2.outputs["Fac"], 0.4))
    bump = t.n("ShaderNodeBump"); bump.inputs["Distance"].default_value = 0.08; bump.inputs["Strength"].default_value = 0.5
    t.link(h, bump.inputs["Height"])
    t.set_in(b, "Normal", bump.outputs[0])
    return m


# ------------------------------------------------------------------ scène
def setup_render():
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = d.type != "CPU"
        sc.cycles.device = "GPU"
        log(f"Cycles GPU : {[d.name for d in prefs.devices if d.use]}")
    except Exception as e:
        log(f"GPU indisponible ({e}), CPU")
    sc.cycles.samples = int(OPT.get("samples", 256))
    sc.cycles.adaptive_threshold = 0.015
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 8
    sc.cycles.transmission_bounces = 6
    sc.cycles.volume_bounces = 1
    sc.cycles.caustics_reflective = False
    sc.cycles.caustics_refractive = False
    sc.render.use_persistent_data = True
    w, h = (int(x) for x in str(OPT.get("res", "1920x1080")).split("x"))
    sc.render.resolution_x, sc.render.resolution_y = w, h
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_depth = "16"
    vs = sc.view_settings
    for vt in (str(OPT.get("view", "Khronos PBR Neutral")), "AgX"):
        try:
            vs.view_transform = vt
            break
        except TypeError:
            pass
    try:
        vs.look = str(OPT.get("look", "None"))
    except TypeError:
        pass
    log(f"Rendu couleur : {vs.view_transform} / {vs.look}")
    vs.exposure = float(OPT.get("exposure", -1.0))


def hdri_sun_dir(path):
    """Direction (azimut, élévation) du point le plus lumineux de l'HDRI → aligne le soleil."""
    im = bpy.data.images.load(str(path), check_existing=True)
    w, h = im.size
    px = np.empty(w * h * 4, np.float32)
    im.pixels.foreach_get(px)
    lum = px.reshape(h, w, 4)[..., :3].sum(-1)
    y, x = np.unravel_index(np.argmax(lum), lum.shape)  # y=0 en bas
    # Blender équirect : u = atan2(dy, -dx)/2π + 0.5  ⇒  azimut (depuis +x, sens trigo) = -2π·u
    az = (-(x + 0.5) / w * 2 * math.pi) % (2 * math.pi)
    el = (y / h) * math.pi - math.pi / 2
    return az, el, im


def setup_world(hdri_name, sun_az_deg, sun_el_deg, strength=1.0, sun_strength=4.0, exposure=-1.0, sun_color=(1.0, 0.78, 0.55)):
    bpy.context.scene.view_settings.exposure = float(OPT.get("exposure", exposure))
    path = next((ASSETS / "hdri").glob(f"{hdri_name}_*.hdr"))
    az_img, el_img, im = hdri_sun_dir(path)
    world = bpy.data.worlds.new("ciel") if "ciel" not in bpy.data.worlds else bpy.data.worlds["ciel"]
    bpy.context.scene.world = world
    t = NT(world)
    tc = t.n("ShaderNodeTexCoord")
    mp = t.n("ShaderNodeMapping")
    t.link(tc.outputs["Generated"], mp.inputs["Vector"])
    # rotation pour amener le soleil de l'HDRI sur l'azimut voulu
    want = math.radians(sun_az_deg)
    mp.inputs["Rotation"].default_value = (0, 0, az_img - want)
    env = t.n("ShaderNodeTexEnvironment", image=im)
    t.link(mp.outputs[0], env.inputs["Vector"])
    bg = t.n("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = strength
    # on bride seulement le disque solaire de l'HDRI (le soleil vient de la lampe)
    clampn = t.n("ShaderNodeVectorMath", operation="MINIMUM")
    t.link(env.outputs[0], clampn.inputs[0])
    clampn.inputs[1].default_value = (6.0, 6.0, 6.0)
    t.link(clampn.outputs[0], bg.inputs["Color"])
    out = t.n("ShaderNodeOutputWorld")
    t.link(bg.outputs[0], out.inputs["Surface"])
    # soleil réel (ombres nettes) aligné sur l'HDRI
    sun = bpy.data.objects.get("Soleil")
    if not sun:
        sd = bpy.data.lights.new("Soleil", "SUN")
        sun = bpy.data.objects.new("Soleil", sd)
        bpy.context.scene.collection.objects.link(sun)
    sun.data.energy = sun_strength
    sun.data.color = sun_color
    sun.data.angle = math.radians(0.8)
    el = math.radians(sun_el_deg)
    d = Vector((math.cos(el) * math.cos(want), math.cos(el) * math.sin(want), math.sin(el)))
    sun.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    log(f"Ciel {hdri_name} (soleil HDRI az {math.degrees(az_img):.0f}° él {math.degrees(el_img):.0f}°) → az {sun_az_deg}° él {sun_el_deg}°")


def load_model_collection(model):
    """Importe un glTF Poly Haven dans une collection cachée (pour instanciation)."""
    cname = f"asset_{model}"
    if cname in bpy.data.collections:
        return bpy.data.collections[cname]
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(ASSETS / "models" / model / f"{model}.gltf"))
    new = [o for o in bpy.data.objects if o not in before]
    col = bpy.data.collections.new(cname)
    bpy.context.scene.collection.children.link(col)
    for o in new:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        col.objects.link(o)
    # origine au pied, hauteur réelle
    zs = [(o.matrix_world @ Vector(c)).z for o in new if o.type == "MESH" for c in o.bound_box]
    col["height"] = max(zs) - min(zs) if zs else 1.0
    col["zmin"] = min(zs) if zs else 0.0
    bpy.context.view_layer.layer_collection.children[cname].exclude = True
    return col


def instance(col, name, loc, rot_z, scale, parent_col):
    e = bpy.data.objects.new(name, None)
    e.instance_type = "COLLECTION"
    e.instance_collection = col
    e.location = (loc[0], loc[1], loc[2] - col["zmin"] * scale)
    e.rotation_euler = (0, 0, rot_z)
    e.scale = (scale, scale, scale)
    parent_col.objects.link(e)
    return e


def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = "METRIC"
    # -- terrain du secteur (1 m)
    mnt = np.load(RAW / "mnt.npy").astype(np.float32)
    res = SIZE / mnt.shape[0]
    log(f"Terrain {mnt.shape} à {res:.2f} m")
    terr = grid_mesh("terrain", mnt, 0, 0, SIZE / (mnt.shape[1] - 1), SIZE / (mnt.shape[0] - 1))
    terr.data.materials.append(mat_ground())
    # -- contexte 8 km
    ctx = json.loads((RAW / "ctx.json").read_text())
    cm = np.load(RAW / "ctx_mnt.npy").astype(np.float32)[::2, ::2]
    bb = ctx["bbox_l93"]
    ox, oy = bb[0] - ctx["origin_l93"][0], bb[1] - ctx["origin_l93"][1]
    span = bb[2] - bb[0]
    st = span / (cm.shape[0] - 1)
    # creuse le contexte sous le secteur (évite les chevauchements)
    xs = ox + np.arange(cm.shape[1]) * st
    ys = oy + (cm.shape[0] - 1 - np.arange(cm.shape[0])) * st
    X, Y = np.meshgrid(xs, ys)
    inside = (X > -st) & (X < SIZE + st) & (Y > -st) & (Y < SIZE + st)
    cm[inside] -= 0.4
    ctxo = grid_mesh("contexte", cm, ox, oy, st, st)
    ctxo.data.materials.append(mat_ctx())
    # -- eau du Bassin : volume de 30 km
    bpy.ops.mesh.primitive_cube_add(size=1, location=(SIZE / 2, SIZE / 2, WATER - 10))
    water = bpy.context.object
    water.name = "eau"
    water.scale = (30000, 30000, 20)
    water.data.materials.append(mat_water())
    # -- bâtiments
    d = np.load(HQ / "buildings.npz")
    names = [str(x) for x in d["mats"]]
    mats = {
        "mur_enduit": mat_wall("mur_enduit", "painted_plaster_wall", True, 1 / 2.0),
        "mur_brique": mat_wall("mur_brique", "brick_4", False, 1 / 1.5),
        "mur_pierre": mat_wall("mur_pierre", "grey_plaster", True, 1 / 2.5, shutters=False),
        "toit_tuile": mat_roof("toit_tuile", "clay_roof_tiles_02", 1 / 2.0),
        "toit_ardoise": mat_roof("toit_ardoise", "roof_slates_02", 1 / 2.0),
        "toit_plat": mat_roof("toit_plat", "gravel_concrete", 1 / 3.0, var=0.05, tint=(0.28, 0.28, 0.27)),
        "mur_leger": mat_simple("mur_leger", (0.62, 0.62, 0.6), 0.5, 0.3),
        "lambrequin": mat_lambrequin(),
    }
    v, f = d["v"], d["f"]
    uv0 = d["uv0"]
    uv1 = d["uv1"]
    fa = d["fa"] if "fa" in d.files else np.tile(np.array([0, -1, -1, 0, 0, 0, 0, .55, .24, .17], np.float32), (len(f), 1))
    make_mesh("batiments", v, f, uvs={"UV0": uv0, "UV1": uv1}, mats=[mats[n] for n in names], mat_index=d["mat"],
              face_attrs={"tint": ("FLOAT_COLOR", d["col"]), "seed": ("FLOAT", d["seed"]),
                          "fa_floor": ("FLOAT", fa[:, 0]), "fa_shop": ("FLOAT", fa[:, 1]), "fa_shut": ("FLOAT", fa[:, 2]),
                          "fa_shut_col": ("FLOAT_COLOR", fa[:, 3:6]), "fa_trim": ("FLOAT", fa[:, 6]),
                          "fa_trim_col": ("FLOAT_COLOR", fa[:, 7:10])})
    # -- bordures de trottoir (granit, 15 cm)
    curbs = json.loads((HQ / "curbs.json").read_text()) if (HQ / "curbs.json").exists() else []
    cv, cf = [], []
    for line in curbs:
        P = np.array(line, np.float32)
        for i in range(len(P) - 1):
            a, b = P[i], P[i + 1]
            dxy = b[:2] - a[:2]
            L = float(np.linalg.norm(dxy))
            if L < 0.05:
                continue
            nrm = np.array([-dxy[1], dxy[0]]) / L * 0.1
            k = len(cv)
            for q, z in ((a, a[2]), (b, b[2])):
                for o in (-1, 1):
                    cv.append([q[0] + nrm[0] * o, q[1] + nrm[1] * o, z - 0.05])
                    cv.append([q[0] + nrm[0] * o, q[1] + nrm[1] * o, z + 0.14])
            # sommets : a(-1,bas)0 a(-1,haut)1 a(+1,bas)2 a(+1,haut)3 b… 4..7
            cf += [[k + 1, k + 5, k + 7], [k + 1, k + 7, k + 3],  # dessus
                   [k + 0, k + 4, k + 5], [k + 0, k + 5, k + 1],  # côté -
                   [k + 2, k + 3, k + 7], [k + 2, k + 7, k + 6]]  # côté +
    if cv:
        co = make_mesh("bordures", cv, cf)
        co.data.materials.append(mat_simple("granit", (0.30, 0.29, 0.28), 0.8))
        log(f"Bordures : {len(cf)} triangles")
    log(f"Bâtiments : {len(f)} triangles")
    # -- jetées (Thiers, Legallais) : platelage + pieux
    piers = json.loads((HQ / "piers.json").read_text()) if (HQ / "piers.json").exists() else []
    wood = mat_simple("bois_jetee", (0.42, 0.34, 0.26), 0.7)
    for p in piers:
        ring = np.array(p["ring"])[:-1]
        if len(ring) < 3:
            continue
        deck_z = 4.6
        import bmesh
        me = bpy.data.meshes.new("jetee")
        bm = bmesh.new()
        vs = [bm.verts.new((x, y, deck_z)) for x, y in ring]
        face = bm.faces.new(vs)
        bmesh.ops.solidify(bm, geom=[face], thickness=0.5)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new(p["name"] or "jetee", me)
        sc.collection.objects.link(ob)
        me.materials.append(wood)
    log(f"Jetées : {len(piers)}")
    # -- arbres réels (LiDAR)
    trees = json.loads((HQ / "trees.json").read_text())
    tcol = bpy.data.collections.new("arbres")
    sc.collection.children.link(tcol)
    pine = load_model_collection("pine_tree_01")
    broad = load_model_collection("island_tree_01")
    lim = int(OPT.get("max_trees", 100000))
    for i, (x, y, z, h, sp, rz) in enumerate(trees[:lim]):
        col = pine if sp == "pine" else broad
        instance(col, f"arbre_{i}", (x, y, z), rz, max(0.4, h / col["height"]), tcol)
    log(f"Arbres : {min(len(trees), lim)} (pin {pine['height']:.1f} m, feuillu {broad['height']:.1f} m d'origine)")
    # -- mobilier
    props = json.loads((HQ / "props.json").read_text())
    pcol = bpy.data.collections.new("mobilier")
    sc.collection.children.link(pcol)
    cache = {}
    for i, pr in enumerate(props):
        model, x, y, z, rz = pr[:5]
        if model not in cache:
            try:
                cache[model] = load_model_collection(model)
            except Exception as e:
                log(f"  modèle {model} indisponible : {e}")
                cache[model] = None
        col = cache[model]
        if col is None:
            continue
        sc_ = 1.0
        if len(pr) > 5:  # hauteur mesurée (arbustes LiDAR)
            sc_ = float(np.clip(pr[5] / max(col["height"], 0.1), 0.4, 3.0))
        if model == "ship_pinnace":  # pinasse ≈ 10 m
            sc_ = 1.0
        instance(col, f"{model}_{i}", (x, y, z), rz, sc_, pcol)
    log(f"Mobilier : {len(props)}")


def perimeter_points(ring, step):
    pts = []
    n = len(ring)
    for i in range(n):
        a, b = np.array(ring[i]), np.array(ring[(i + 1) % n])
        L = np.linalg.norm(b - a)
        for s in np.arange(0, L, step):
            pts.append(a + (b - a) * (s / L))
    return pts


def add_piles():
    """Pieux sous les jetées (appelé après build_scene)."""
    piers = json.loads((HQ / "piers.json").read_text()) if (HQ / "piers.json").exists() else []
    if not piers:
        return
    pile = mat_simple("pieux", (0.28, 0.25, 0.22), 0.8)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.18, depth=1, vertices=10, location=(0, 0, -1000))
    base = bpy.context.object
    base.name = "pieu_modele"
    base.data.materials.append(pile)
    n = 0
    for p in piers:
        for q in perimeter_points(np.array(p["ring"])[:-1], 5.0):
            ob = bpy.data.objects.new("pieu", base.data)
            ob.location = (q[0], q[1], (4.6 + (-4.0)) / 2)
            ob.scale = (1, 1, 8.6)
            bpy.context.scene.collection.objects.link(ob)
            n += 1
    bpy.data.objects.remove(base)
    log(f"Pieux : {n}")


# ------------------------------------------------------------------ plans
def ground_z(x, y):
    mnt = ground_z.mnt
    i = int(np.clip((1 - y / SIZE) * (mnt.shape[0] - 1), 0, mnt.shape[0] - 1))
    j = int(np.clip(x / SIZE * (mnt.shape[1] - 1), 0, mnt.shape[1] - 1))
    return float(mnt[i, j])


def camera(name, loc, target, lens=35, dof=None):
    cd = bpy.data.cameras.new(name)
    cd.lens = lens
    cd.clip_start = 0.1
    cd.clip_end = 30000
    if dof:
        cd.dof.use_dof = True
        cd.dof.focus_distance = dof[0]
        cd.dof.aperture_fstop = dof[1]
    cam = bpy.data.objects.new(name, cd)
    bpy.context.scene.collection.objects.link(cam)
    cam.location = loc
    cam.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return cam


SHOTS = {
    # ciel, azimut soleil (0 = est, 90 = nord, 180 = ouest), élévation, force ciel, force soleil, exposition
    "vue_d_ensemble": dict(sky=("kloppenheim_06_puresky", 200, 16, 1.0, 5.0, -0.5),
                           cam=((1150, 120, 330), (420, 760, 0), 34)),
    "aerien_coucher_soleil": dict(sky=("belfast_sunset_puresky", 150, 7, 1.2, 5.0, 0.0),
                                  cam=((980, 60, 240), (420, 980, 5), 30)),
    "front_de_mer_depuis_le_bassin": dict(sky=("kloppenheim_06_puresky", 205, 14, 1.0, 5.0, -0.5),
                                          cam=((700, 1550, 120), (560, 640, 12), 38)),
    "ville_d_hiver": dict(sky=("kloppenheim_06_puresky", 215, 18, 1.0, 5.0, -0.4),
                          cam=((170, 130, 75), (560, 660, 5), 30)),
    "boulevard_de_la_plage": dict(sky=("kloppenheim_06_puresky", 215, 14, 1.2, 6.0, -0.1),
                                  cam=("street", (470, 768), (720, 769), 26)),
}


def render_shots(names):
    out = HQ / "renders"
    out.mkdir(exist_ok=True)
    sc = bpy.context.scene
    for nm in names:
        s = SHOTS[nm]
        setup_world(*s["sky"])
        c = s["cam"]
        if c[0] == "street":
            (x, y), (tx, ty), lens = c[1], c[2], c[3]
            loc = (x, y, ground_z(x, y) + 1.7)
            tgt = (tx, ty, ground_z(tx, ty) + 3.0)
            cam = camera(nm, loc, tgt, lens)
        else:
            cam = camera(nm, c[0], c[1], c[2])
        sc.camera = cam
        sc.render.filepath = str(out / f"{nm}.png")
        t = time.time()
        log(f"Rendu {nm}…")
        bpy.ops.render.render(write_still=True)
        log(f"  {nm} : {time.time() - t:.0f} s → {sc.render.filepath}")
        print(f"ATLAS_RENDER {nm} {sc.render.filepath}", flush=True)


if __name__ == "__main__":
    ground_z.mnt = np.load(RAW / "mnt.npy")
    build_scene()
    add_piles()
    setup_render()
    bpy.ops.wm.save_as_mainfile(filepath=str(HQ / "arcachon_hq.blend"))
    log("Scène sauvegardée")
    if not OPT.get("no-render"):
        shots = str(OPT.get("shots", ",".join(SHOTS))).split(",")
        render_shots(shots)
    print("ATLAS_HQ_OK", flush=True)

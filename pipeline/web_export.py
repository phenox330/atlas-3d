"""Exporte la scène HQ de Blender vers la vue 3D de l'app (web/viewer3d.js) : même géométrie que les rendus Cycles.

Usage : Blender -b <secteur>/build/hq/arcachon_hq.blend -P web_export.py -- <dossier_secteur> [--only=sol,batiments,divers,voitures,modeles,arbres] [--nobake] [--rebake]
(appelé aussi par blender_hq.py après la construction de la scène)
Sorties dans build/hq/web/ :
  scene.json        points de vue (les mêmes que les rendus), ciel, soleil, brume, emprises, instances
  *.f32             champs de hauteur (terrain 1 m, abords 1 m, couronne 2 m, contexte 16 m)
  batiments.glb     toutes les façades (enveloppe + menuiseries), un maillage par matériau, teinte par face en couleur de sommet
  divers.glb        jetées, pieux, bordures, enseignes, bateaux
  voitures.glb      un maillage par modèle × teinte (proche + lointain), instances dans scene.json
  models/*.glb      mobilier Poly Haven (proche + lointain décimé)
  imp_*.png         arbres et arbustes : atlas de vues (8 azimuts × 4 hauteurs) rendus par Cycles, affichés en panneaux
Les textures (orthophotos, masques de sol, matières) sont préparées par hq_prepare.export_viewer."""
import json
import math
import shutil
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import glbw

T0 = time.time()
IMPOSTORS = {"pine_tree_01": 512, "island_tree_01": 512, "tree_small_02": 384, "shrub_01": 192, "shrub_02": 256, "shrub_03": 192}
ELS = [8.0, 30.0, 52.0, 74.0]
NAZ = 8
WALLS = ("mur_enduit", "mur_brique", "mur_pierre")
NEED_UV = WALLS + ("toit_tuile", "toit_ardoise", "toit_plat", "lambrequin", "vitre", "volet", "garde_corps", "toile")
C = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], np.float64)   # Atlas (z haut) → glTF (y haut)


def log(m):
    print(f"[web {time.time() - T0:6.1f}s] {m}", flush=True)


def srgb8(lin):
    return (np.clip(np.asarray(lin, np.float32), 0, 1) ** (1 / 2.2) * 255 + 0.5).astype(np.uint8)


def heightfield(name, web):
    ob = bpy.data.objects.get(name)
    if ob is None:
        return None
    me = ob.data
    n = len(me.vertices)
    co = np.empty(n * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    cols = int(np.argmax(co[:, 1] != co[0, 1]))
    rows = n // cols
    co[:, 2].reshape(rows, cols).astype("<f4").tofile(web / f"{name}.f32")
    return dict(file=f"{name}.f32", rows=rows, cols=cols, x0=float(co[0, 0]), y1=float(co[0, 1]),
                sx=float(co[1, 0] - co[0, 0]), sy=float(co[0, 1] - co[cols, 1]))


def export_buildings(hq, web):
    d = np.load(hq / "buildings.npz")
    v, f, mat, col, seed, fa = d["v"], d["f"], d["mat"], d["col"], d["seed"], d["fa"]
    uv0, uv1 = d["uv0"], d["uv1"]
    names = [str(x) for x in d["mats"]]
    meshes = []
    for i, nm in enumerate(names):
        k = np.flatnonzero(mat == i)
        if not len(k):
            continue
        ix = f[k].ravel()
        rgba = np.empty((len(k), 4), np.uint8)
        rgba[:, :3] = srgb8(col[k])
        rgba[:, 3] = np.clip(seed[k] / 2.0 * 255 + 0.5, 0, 255).astype(np.uint8)
        attrs = {"POSITION": glbw.to_gl(v[ix]), "COLOR_0": np.repeat(rgba, 3, axis=0)}
        if nm in NEED_UV:
            attrs["TEXCOORD_0"] = uv0[ix]
        if nm in WALLS:
            attrs["TEXCOORD_1"] = uv1[ix]
            tr = np.empty((len(k), 4), np.uint8)
            tr[:, :3] = srgb8(fa[k, 7:10])
            tr[:, 3] = (fa[k, 6] > 0.5) * 255
            attrs["_TRIM"] = np.repeat(tr, 3, axis=0)
        attrs, idx = glbw.dedupe(attrs)
        meshes.append(dict(name=nm, material=nm, attrs=attrs, indices=idx))
        log(f"  {nm} : {len(k)} triangles, {len(attrs['POSITION'])} sommets")
    n = glbw.write(web / "batiments.glb", meshes)
    log(f"Bâtiments → batiments.glb ({n / 1e6:.0f} Mo)")


def mesh_arrays(ob, dg, world=True):
    """Triangles d'un objet évalué : positions, normales de coin, indice de matériau par triangle, noms des matériaux."""
    eo = ob.evaluated_get(dg)
    try:
        me = eo.to_mesh()
    except RuntimeError:
        return None
    if me is None or len(me.polygons) == 0:
        eo.to_mesh_clear()
        return None
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    co = np.empty(len(me.vertices) * 3, np.float32); me.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
    tl = np.empty(nt * 3, np.int32); me.loop_triangles.foreach_get("loops", tl)
    tm = np.empty(nt, np.int32); me.loop_triangles.foreach_get("material_index", tm)
    lv = np.empty(len(me.loops), np.int32); me.loops.foreach_get("vertex_index", lv)
    cn = np.empty(len(me.loops) * 3, np.float32); me.corner_normals.foreach_get("vector", cn); cn = cn.reshape(-1, 3)
    P, N = co[lv[tl]], cn[tl]
    if world:
        M = np.array(ob.matrix_world, np.float64)
        P = (P @ M[:3, :3].T + M[:3, 3]).astype(np.float32)
        N = N @ np.linalg.inv(M[:3, :3])
        N = (N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)).astype(np.float32)
    slots = [s.material for s in eo.material_slots]
    eo.to_mesh_clear()
    return P, N, tm, slots


def pbr_of(m):
    b = next((n for n in (m.node_tree.nodes if m and m.node_tree else []) if n.type == "BSDF_PRINCIPLED"), None)
    if b is None:
        return (0.5, 0.5, 0.5), 0.6, 0.0, 0.0
    g = lambda k, dflt: dflt if b.inputs[k].is_linked else b.inputs[k].default_value
    bc = g("Base Color", (0.5, 0.5, 0.5, 1))
    return tuple(float(x) for x in bc[:3]), float(g("Roughness", 0.6)), float(g("Metallic", 0.0)), float(g("Coat Weight", 0.0))


def export_misc(web, dg):
    """Objets fixes : fusionnés par matériau, en coordonnées monde."""
    objs = [o for o in bpy.context.scene.collection.objects if o.type == "MESH" and (o.name.startswith(("pieu", "Jetée", "jetee", "bordures")))]
    for cn in ("enseignes", "bateaux"):
        c = bpy.data.collections.get(cn)
        objs += list(c.objects) if c else []
    for ob in objs:   # lettrage : courbes peu subdivisées, sinon des millions de triangles pour des lettres de 30 cm
        if ob.type == "FONT":
            ob.data.resolution_u = 2
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    by = {}
    for ob in objs:
        r = mesh_arrays(ob, dg)
        if r is None:
            continue
        P, N, tm, slots = r
        for mi in np.unique(tm):
            m = slots[mi] if mi < len(slots) else None
            k = np.repeat(tm == mi, 3)
            e = by.setdefault(m.name if m else "defaut", dict(P=[], N=[], mat=m))
            e["P"].append(P[k]); e["N"].append(N[k])
    meshes = []
    for nm, e in by.items():
        P, N = np.vstack(e["P"]), np.vstack(e["N"])
        base, rough, metal, _ = pbr_of(e["mat"])
        attrs, idx = glbw.dedupe({"POSITION": glbw.to_gl(P), "NORMAL": glbw.to_gl(N)})
        meshes.append(dict(name=nm, material=nm, attrs=attrs, indices=idx,
                           pbr={"pbrMetallicRoughness": {"baseColorFactor": [*base, 1.0], "roughnessFactor": rough, "metallicFactor": metal}}))
    glbw.write(web / "divers.glb", meshes)
    log(f"Divers ({len(objs)} objets, {len(meshes)} matériaux) → divers.glb")


def decimated(me, ratio, dg_scene):
    ob = bpy.data.objects.new("tmp_decim", me)
    dg_scene.collection.objects.link(ob)
    md = ob.modifiers.new("d", "DECIMATE")
    md.ratio = ratio
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    r = mesh_arrays(ob, dg, world=False)
    bpy.data.objects.remove(ob)
    return r


def export_cars(web, dg):
    vc = bpy.data.collections.get("voitures")
    if vc is None:
        return []
    uniq, meshes, inst = {}, [], []

    def pack(r, name):
        P, N, tm, slots = r
        nt = len(tm)
        rgba = np.zeros((nt, 4), np.uint8); prm = np.zeros((nt, 4), np.uint8)
        for mi in np.unique(tm):
            base, rough, metal, coat = pbr_of(slots[mi] if mi < len(slots) else None)
            k = tm == mi
            rgba[k, :3] = srgb8(base); rgba[k, 3] = 255
            prm[k] = [int(rough * 255), int(metal * 255), int(min(coat, 1) * 255), 0]
        attrs, idx = glbw.dedupe({"POSITION": glbw.to_gl(P), "NORMAL": glbw.to_gl(N), "COLOR_0": np.repeat(rgba, 3, axis=0),
                                  "_PBR": np.repeat(prm, 3, axis=0)})
        meshes.append(dict(name=name, material="voiture", attrs=attrs, indices=idx))

    for ob in vc.objects:
        me = ob.data
        if me.name not in uniq:
            r = mesh_arrays(ob, dg, world=False)
            if r is None:
                continue
            uniq[me.name] = len(uniq)
            pack(r, f"v{uniq[me.name]}")
            nt = len(r[2])
            rf = decimated(me, min(1.0, 900 / max(nt, 1)), bpy.context.scene)
            pack(rf if rf is not None else r, f"v{uniq[me.name]}_far")
        M = C @ np.array(ob.matrix_world, np.float64) @ C.T
        inst.append([uniq[me.name]] + [round(float(x), 4) for x in M.T.ravel()])
    glbw.write(web / "voitures.glb", meshes)
    log(f"Voitures : {len(inst)} instances de {len(uniq)} modèles → voitures.glb")
    return inst


def export_models(web, names):
    """Mobilier Poly Haven : glb proche (textures) + glb lointain (décimé, matériaux par nom)."""
    (web / "models").mkdir(exist_ok=True)
    vl = bpy.context.view_layer
    out = {}
    for model in names:
        cname = f"asset_{model}"
        col = bpy.data.collections.get(cname)
        lc = vl.layer_collection.children.get(cname)
        if col is None or lc is None:
            continue
        lc.exclude = False
        bpy.ops.object.select_all(action="DESELECT")
        objs = [o for o in col.objects if o.type == "MESH"]
        for o in objs:
            o.select_set(True)
        vl.objects.active = objs[0]
        try:
            bpy.ops.export_scene.gltf(filepath=str(web / "models" / f"{model}.glb"), use_selection=True, export_format="GLB",
                                      export_apply=True, export_yup=True, export_image_format="JPEG", export_jpeg_quality=85)
            tris = sum(len(p.vertices) - 2 for o in objs for p in o.data.polygons)
            ratio = min(1.0, 700 / max(tris, 1))
            mods = []
            for o in objs:
                md = o.modifiers.new("web_far", "DECIMATE"); md.ratio = ratio; mods.append((o, md))
            bpy.ops.export_scene.gltf(filepath=str(web / "models" / f"{model}_far.glb"), use_selection=True, export_format="GLB",
                                      export_apply=True, export_yup=True, export_materials="PLACEHOLDER")
            for o, md in mods:
                o.modifiers.remove(md)
            out[model] = dict(tris=tris)
            log(f"  modèle {model} : {tris} triangles (lointain ×{ratio:.3f})")
        except Exception as e:
            log(f"  modèle {model} : export impossible ({e})")
        lc.exclude = True
    return out


def setup_gpu(sc):
    sc.render.engine = "CYCLES"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = d.type != "CPU"
        sc.cycles.device = "GPU"
    except Exception as e:
        log(f"GPU indisponible ({e})")


def dilate(px, n=12):
    """Étend la couleur sous les pixels transparents (sinon liseré sombre au filtrage)."""
    rgb, a = px[..., :3].copy(), px[..., 3]
    known = a > 0.02
    for _ in range(n):
        acc = np.zeros_like(rgb); cnt = np.zeros(a.shape, np.float32)
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            kk = np.roll(known, (dy, dx), (0, 1))
            acc += np.roll(rgb, (dy, dx), (0, 1)) * kk[..., None]; cnt += kk
        new = (~known) & (cnt > 0)
        rgb[new] = acc[new] / cnt[new][:, None]
        known = known | new
    out = px.copy(); out[..., :3] = rgb
    return out


def bake_impostor(model, res, web, tmp):
    """Atlas de vues d'un arbre : NAZ azimuts × len(ELS) hauteurs, éclairage de ciel uniforme (la vue 3D rajoute le soleil)."""
    col = bpy.data.collections.get(f"asset_{model}")
    if col is None:
        return None
    pts = np.array([o.matrix_world @ Vector(c) for o in col.objects if o.type == "MESH" for c in o.bound_box], np.float64)
    lo, hi = pts.min(0), pts.max(0)
    cen = (lo + hi) / 2
    R = float(np.linalg.norm(hi - lo) / 2) * 1.02
    sc = bpy.data.scenes.new(f"bake_{model}")
    e = bpy.data.objects.new(f"bake_inst_{model}", None)
    e.instance_type = "COLLECTION"; e.instance_collection = col
    sc.collection.objects.link(e)
    w = bpy.data.worlds.new(f"bake_w_{model}")
    w.use_nodes = True
    bg = next(n for n in w.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs[0].default_value = (1, 1, 1, 1); bg.inputs[1].default_value = 1.0
    sc.world = w
    setup_gpu(sc)
    sc.cycles.samples = 40
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 4
    sc.render.use_persistent_data = True
    sc.render.film_transparent = True
    sc.render.resolution_x = sc.render.resolution_y = res
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.image_settings.color_depth = "8"
    sc.view_settings.view_transform = "Standard"
    try:
        sc.view_settings.look = "None"
    except TypeError:
        pass
    sc.view_settings.exposure = 0.0
    cd = bpy.data.cameras.new(f"bake_cam_{model}")
    cd.type = "ORTHO"; cd.ortho_scale = 2 * R; cd.clip_start = 0.01; cd.clip_end = R * 8
    cam = bpy.data.objects.new(f"bake_cam_{model}", cd)
    sc.collection.objects.link(cam)
    sc.camera = cam
    atlas = np.zeros((res * len(ELS), res * NAZ, 4), np.float32)
    for ei, el in enumerate(ELS):
        for ai in range(NAZ):
            az = ai * 2 * math.pi / NAZ
            D = Vector((math.cos(math.radians(el)) * math.cos(az), math.cos(math.radians(el)) * math.sin(az), math.sin(math.radians(el))))
            cam.location = Vector(cen) + D * (R * 4)
            cam.rotation_euler = (-D).to_track_quat("-Z", "Y").to_euler()
            fp = tmp / f"{model}_{ei}_{ai}.png"
            sc.render.filepath = str(fp)
            bpy.ops.render.render(write_still=True, scene=sc.name)
            im = bpy.data.images.load(str(fp))
            px = np.empty(res * res * 4, np.float32); im.pixels.foreach_get(px)
            bpy.data.images.remove(im)
            atlas[ei * res:(ei + 1) * res, ai * res:(ai + 1) * res] = px.reshape(res, res, 4)
    atlas = dilate(atlas)
    im = bpy.data.images.new(f"imp_{model}", res * NAZ, res * len(ELS), alpha=True)
    im.pixels.foreach_set(atlas.ravel())
    im.filepath_raw = str(web / f"imp_{model}.png"); im.file_format = "PNG"
    im.save()
    bpy.data.images.remove(im)
    bpy.data.scenes.remove(sc)
    log(f"  atlas {model} : {NAZ}×{len(ELS)} vues de {res} px, rayon {R:.1f} m")
    return dict(atlas=f"imp_{model}.png", cols=NAZ, rows=len(ELS), els=ELS, center=[float(x) for x in cen], radius=R)


def instances_of(colname):
    c = bpy.data.collections.get(colname)
    out = {}
    for o in (c.objects if c else []):
        if o.instance_type == "COLLECTION" and o.instance_collection:
            out.setdefault(o.instance_collection.name.replace("asset_", ""), []).append(
                [round(o.location.x, 2), round(o.location.y, 2), round(o.location.z, 2), round(o.rotation_euler.z, 3), round(o.scale.x, 3)])
    return out


def run(bh, opt=None):
    opt = opt or {}
    only = set(str(opt["only"]).split(",")) if opt.get("only") else None
    want = lambda k: only is None or k in only
    web = bh.HQ / "web"
    web.mkdir(exist_ok=True)
    tmp = web / "_tmp"
    tmp.mkdir(exist_ok=True)
    prev = json.loads((web / "scene.json").read_text()) if (web / "scene.json").exists() else {}
    main_scene = bpy.context.scene
    dg = bpy.context.evaluated_depsgraph_get()
    S = dict(prev)
    S.update(size=bh.SIZE, water=bh.WATER, built=time.time())
    # -- sol
    if want("sol"):
        S["fields"] = {n: heightfield(n, web) for n in ("terrain", "abords", "couronne", "contexte") if bpy.data.objects.get(n)}
        ctx = json.loads((bh.RAW / "ctx.json").read_text())
        bb = ctx["bbox_l93"]
        S["ctx"] = dict(ox=bb[0] - ctx["origin_l93"][0], oy=bb[1] - ctx["origin_l93"][1], span=bb[2] - bb[0])
        for key in ("mid", "near"):
            p = bh.RAW / f"{key}.json"
            if p.exists():
                j = json.loads(p.read_text()); b = j["bbox_l93"]
                S[key] = dict(ox=b[0] - j["origin_l93"][0], oy=b[1] - j["origin_l93"][1], span=b[2] - b[0])
        ss = json.loads((bh.HQ / "street_style.json").read_text()) if (bh.HQ / "street_style.json").exists() else {}
        walk = (0.20, 0.07, 0.06)
        cols_ = [c_ for c_ in ss.get("sidewalk_colors", []) if c_ and len(c_.lstrip("#")) == 6]
        if cols_:
            arr = np.array([[(int(h.lstrip("#")[i:i + 2], 16) / 255) ** 2.2 for i in (0, 2, 4)] for h in cols_])
            med = np.median(arr, 0) * 1.2
            walk = tuple(float(x) for x in np.clip(med * 0.62 + med.mean() * 0.38, 0.03, 0.6))
        S["walk_rgb"] = walk
        log("Sol : champs de hauteur écrits")
    if want("batiments"):
        export_buildings(bh.HQ, web)
    if want("divers"):
        export_misc(web, dg)
    if want("voitures"):
        S["cars"] = export_cars(web, dg)
    # -- arbres, arbustes, mobilier
    if want("instances") or want("modeles") or want("arbres"):
        inst = instances_of("arbres")
        for k, v in instances_of("mobilier").items():
            inst.setdefault(k, []).extend(v)
        S["instances"] = inst
        models = [m for m in inst if m not in IMPOSTORS]
        if want("modeles"):
            S["models"] = export_models(web, models)
        if want("arbres") and not opt.get("nobake"):
            imp = dict(S.get("impostors") or {})
            for m in inst:
                if m in IMPOSTORS:
                    if m in imp and (web / imp[m]["atlas"]).exists() and not opt.get("rebake"):
                        continue          # le modèle ne change pas d'un rendu à l'autre : --rebake pour refaire les atlas
                    r = bake_impostor(m, IMPOSTORS[m], web, tmp)
                    if r:
                        imp[m] = r
            S["impostors"] = imp
    # -- ciel, soleil, points de vue (identiques aux rendus)
    hdris, shots = {}, {}
    for nm, s in bh.SHOTS.items():
        hdri, az, el, strength, sun_strength, exposure, sun_color = s["sky"]
        if hdri not in hdris:
            path = next((bh.ASSETS / "hdri").glob(f"{hdri}_*.hdr"))
            az_img, el_img, _ = bh.hdri_sun_dir(path)
            if not (web / path.name).exists():
                shutil.copy(path, web / path.name)
            hdris[hdri] = dict(file=path.name, sun_az=math.degrees(az_img), sun_el=math.degrees(el_img), horizon=list(bh.hdri_sun_dir.horizon))
        c = s["cam"]
        if c[0] == "street":
            (x, y), (tx, ty), lens = c[1], c[2], c[3]
            loc, tgt = (x, y, bh.ground_z(x, y) + 1.7), (tx, ty, bh.ground_z(tx, ty) + 3.0)
        else:
            loc, tgt, lens = c
        hz = s.get("haze", ((0.62, 0.70, 0.80), 0.55, 300, 9000))
        shots[nm] = dict(cam=list(loc), target=list(tgt), lens=lens, street=c[0] == "street", hdri=hdri, sun_az=az, sun_el=el, sky=strength,
                         sun=sun_strength, exposure=exposure, sun_color=list(sun_color), haze=dict(amount=hz[1], start=hz[2], depth=hz[3]))
    S["hdris"], S["shots"] = hdris, shots
    shutil.rmtree(tmp, ignore_errors=True)
    (web / "scene.json").write_text(json.dumps(S, separators=(",", ":")))
    log("Vue 3D : scene.json écrit")
    print("ATLAS_WEB_OK", flush=True)


if __name__ == "__main__":
    import blender_hq as bh
    bh.ground_z.mnt = np.load(bh.RAW / "mnt.npy")
    run(bh, bh.OPT)

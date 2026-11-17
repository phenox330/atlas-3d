# Atlas : matériaux Unreal équivalents aux shaders Blender (façades à fenêtres procédurales, toits, sol
# orthophoto + masques OSM, eau, feuillage). Les calculs sont en HLSL (nœuds Custom) pour rester lisibles.
import os
import unreal

PROJ = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
TEXDIR = os.path.join(PROJ, "Import", "textures")
MAT = "/Game/Atlas/Mat"
TEX = "/Game/Atlas/Tex"
mel = unreal.MaterialEditingLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
eal = unreal.EditorAssetLibrary


def log(m):
    unreal.log("[Atlas] " + m)


# ---------------------------------------------------------------- textures
def import_tex(path, dest, kind="color"):
    name = os.path.splitext(os.path.basename(path))[0]
    t = unreal.AssetImportTask()
    t.filename, t.destination_path, t.destination_name = path, dest, name
    t.automated, t.replace_existing, t.save = True, True, False
    at.import_asset_tasks([t])
    tex = unreal.load_asset(f"{dest}/{name}")
    if kind == "normal":
        tex.set_editor_property("srgb", False)
        tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
        tex.set_editor_property("flip_green_channel", True)  # Poly Haven = OpenGL
    elif kind == "linear":
        tex.set_editor_property("srgb", False)
        tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
    eal.save_loaded_asset(tex)
    return tex


def tex_set(name):
    d = os.path.join(TEXDIR, name)
    dest = f"{TEX}/{name}"
    return {"diff": import_tex(os.path.join(d, "diff.jpg"), dest),
            "nor": import_tex(os.path.join(d, "nor.jpg"), dest, "normal"),
            "rough": import_tex(os.path.join(d, "rough.jpg"), dest, "linear")}


# ---------------------------------------------------------------- graphe
class G:
    def __init__(self, name, two_sided=False, blend=None):
        p = f"{MAT}/{name}"
        if eal.does_asset_exist(p):  # réutiliser (supprimer un matériau utilisé fait planter l'éditeur)
            self.m = unreal.load_asset(p)
            mel.delete_all_material_expressions(self.m)
        else:
            self.m = at.create_asset(name, MAT, unreal.Material, unreal.MaterialFactoryNew())
        if two_sided:
            self.m.set_editor_property("two_sided", True)
        if blend is not None:
            self.m.set_editor_property("blend_mode", blend)
        self.y = 0

    def e(self, cls, **props):
        x = mel.create_material_expression(self.m, cls, -900, self.y)
        self.y += 140
        for k, v in props.items():
            x.set_editor_property(k, v)
        return x

    def texobj(self, pname, tex):
        return self.e(unreal.MaterialExpressionTextureObjectParameter, parameter_name=pname, texture=tex)

    def scalar(self, pname, v):
        return self.e(unreal.MaterialExpressionScalarParameter, parameter_name=pname, default_value=v)

    def uv(self, i):
        return self.e(unreal.MaterialExpressionTextureCoordinate, coordinate_index=i)

    def custom(self, code, inputs, outputs=()):
        """inputs : [(nom, expression, sortie)] ; outputs : [(nom, type)] en plus de la sortie principale (float3)."""
        c = mel.create_material_expression(self.m, unreal.MaterialExpressionCustom, -300, 0)
        c.set_editor_property("code", code)
        c.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT3)
        ins = []
        for n, _, _ in inputs:
            ci = unreal.CustomInput()
            ci.set_editor_property("input_name", n)
            ins.append(ci)
        c.set_editor_property("inputs", ins)
        outs = []
        for n, ty in outputs:
            co = unreal.CustomOutput()
            co.set_editor_property("output_name", n)
            co.set_editor_property("output_type", ty)
            outs.append(co)
        if outs:
            c.set_editor_property("additional_outputs", outs)
        for n, src, out in inputs:
            mel.connect_material_expressions(src, out, c, n)
        return c

    def out(self, expr, outname, prop):
        mel.connect_material_property(expr, outname, prop)

    def done(self):
        mel.layout_material_expressions(self.m)
        mel.recompile_material(self.m)
        eal.save_loaded_asset(self.m)
        return self.m


F1 = unreal.CustomMaterialOutputType.CMOT_FLOAT1
F3 = unreal.CustomMaterialOutputType.CMOT_FLOAT3
MP = unreal.MaterialProperty

# ---------------------------------------------------------------- façades
WALL_HLSL = r"""
float u = UV0.x, v = 1.0 - UV0.y;          // glTF retourne V
float seglen = UV1.x, walltop = 1.0 - UV1.y;
float seed = VCA; float3 tint = VC;
float2 tuv = UV0 * TexScale;
float3 base = Diff.Sample(DiffSampler, tuv).rgb;
float2 nxy = Nor.Sample(NorSampler, tuv).rg * 2 - 1; float3 nrm = float3(nxy, sqrt(saturate(1 - dot(nxy, nxy))));  // BC5 : Z reconstruit
float rgh = Rgh.Sample(RghSampler, tuv).r;
if (UseTint > 0.5) { float g = dot(base, float3(0.299,0.587,0.114)) * 0.45 + 0.42; base = tint * g; }
float dirt = (1 - min(v / 1.2, 1)) * 0.35 + frac(sin(dot(floor(UV0 * 0.7), float2(12.9898,78.233))) * 43758.5453) * 0.05;
base = lerp(base, float3(0.18,0.16,0.14), dirt);
const float S = 2.9, WW = 1.15, F = 3.0, SILL = 0.95, WH = 1.55;
float nw = floor(seglen / S), span = nw * S;
float xm = u + span * 0.5;
float col = frac(xm / S) * S - S / 2, acol = abs(col);
float in_span = abs(u) < span * 0.5 - 0.1 ? 1 : 0;
float has = nw > 0.5 ? 1 : 0;
float vf = frac(v / F) * F;
float in_y = (vf > SILL && vf < SILL + WH && v > 0.5 && v < walltop - 0.6) ? 1 : 0;
float zone = in_span * has * in_y;
float win = zone * (acol < WW / 2 ? 1 : 0);
float shop = (v < 3.0 && walltop > 9.0 && frac(seed * 3.7) > 0.35) ? in_span * has : 0;
float shop_win = shop * (acol < S / 2 - 0.3 ? 1 : 0) * ((v > 0.25 && v < 2.7) ? 1 : 0);
zone *= 1 - shop;
win = max(win * (1 - shop), shop_win);
float vf_in = (vf > SILL + 0.09 && vf < SILL + WH - 0.09) ? 1 : 0;
float glass = win * (acol < WW / 2 - 0.09 ? 1 : 0) * vf_in;
float shop_glass = shop_win * (acol < S / 2 - 0.4 ? 1 : 0) * (v > 0.35 ? 1 : 0) * (frac(xm / 1.25) > 0.04 ? 1 : 0);
glass = max(glass, shop_glass);
float frame = win - glass;
float shut = zone * ((acol > WW / 2 + 0.03 && acol < WW + 0.03) ? 1 : 0) * (seed > (Shutters > 0.5 ? 0.3 : 2.0) ? 1 : 0);
float3 pal[6] = {float3(0.08,0.18,0.34), float3(0.30,0.40,0.30), float3(0.32,0.07,0.09), float3(0.88,0.88,0.85), float3(0.38,0.46,0.52), float3(0.10,0.36,0.38)};
float3 sc = pal[(int)min(floor(frac(seed * 7.31) * 6), 5)];
float slats = frac(vf * 9.0) > 0.18 ? 1 : 0;
sc = lerp(sc, float3(0.02,0.02,0.02), (1 - slats) * 0.5);
float3 c = lerp(base, sc, shut);
c = lerp(c, float3(0.86,0.85,0.82), frame);
float wn = frac(sin(dot(float3(floor(xm / S), floor(v / F), seed * 97.0), float3(12.9898, 78.233, 37.719))) * 43758.5453);
float lit = max(glass * (wn > 0.86 ? 1 : 0), shop_glass * (1 - v / 3.0) * 0.25);
c = lerp(c, lerp(float3(0.015,0.02,0.025), float3(0.05,0.05,0.045), wn * 0.4), glass);
Rough = lerp(rgh, 0.04, glass);
Emis = float3(1.0, 0.72, 0.42) * lit * Glow;
// relief : fenêtres en retrait (assombrissement du pourtour), normale de texture hors vitrage
Nrm = normalize(lerp(float3(nrm.xy * 0.6, nrm.z), float3(0,0,1), win));
return c;
"""


def wall_master():
    g = G("M_Facade")
    d = tex_set("painted_plaster_wall")
    vc = g.e(unreal.MaterialExpressionVertexColor)
    c = g.custom(WALL_HLSL, [
        ("UV0", g.uv(0), ""), ("UV1", g.uv(1), ""), ("VC", vc, ""), ("VCA", vc, "A"),
        ("Diff", g.texobj("Diff", d["diff"]), ""), ("Nor", g.texobj("Nor", d["nor"]), ""), ("Rgh", g.texobj("Rgh", d["rough"]), ""),
        ("TexScale", g.scalar("TexScale", 0.5), ""), ("UseTint", g.scalar("UseTint", 1.0), ""),
        ("Shutters", g.scalar("Shutters", 1.0), ""), ("Glow", g.scalar("Glow", 0.9), "")],
        [("Rough", F1), ("Emis", F3), ("Nrm", F3)])
    g.out(c, "", MP.MP_BASE_COLOR)
    g.out(c, "Rough", MP.MP_ROUGHNESS)
    g.out(c, "Emis", MP.MP_EMISSIVE_COLOR)
    g.out(c, "Nrm", MP.MP_NORMAL)
    return g.done()


ROOF_HLSL = r"""
float2 tuv = UV0 * TexScale;
float3 c = Diff.Sample(DiffSampler, tuv).rgb;
float seed = VCA;
c *= 0.8 + seed * 0.4;
c = lerp(c, c.gbr, (seed - 0.5) * Var * 0.4);
c = lerp(c, TintCol, TintAmt);
Rough = Rgh.Sample(RghSampler, tuv).r;
float2 nxy = Nor.Sample(NorSampler, tuv).rg * 2 - 1; float3 n = float3(nxy, sqrt(saturate(1 - dot(nxy, nxy))));
Nrm = normalize(float3(n.xy * 1.2, n.z));
return c;
"""


def roof_master():
    g = G("M_Toit")
    d = tex_set("clay_roof_tiles_02")
    vc = g.e(unreal.MaterialExpressionVertexColor)
    tint = g.e(unreal.MaterialExpressionVectorParameter, parameter_name="TintCol", default_value=unreal.LinearColor(0.28, 0.28, 0.27, 1))
    c = g.custom(ROOF_HLSL, [
        ("UV0", g.uv(0), ""), ("VCA", vc, "A"),
        ("Diff", g.texobj("Diff", d["diff"]), ""), ("Nor", g.texobj("Nor", d["nor"]), ""), ("Rgh", g.texobj("Rgh", d["rough"]), ""),
        ("TexScale", g.scalar("TexScale", 0.5), ""), ("Var", g.scalar("Var", 0.12), ""),
        ("TintCol", tint, ""), ("TintAmt", g.scalar("TintAmt", 0.0), "")],
        [("Rough", F1), ("Nrm", F3)])
    g.out(c, "", MP.MP_BASE_COLOR)
    g.out(c, "Rough", MP.MP_ROUGHNESS)
    g.out(c, "Nrm", MP.MP_NORMAL)
    return g.done()


def instance(name, parent, tex=None, scalars=None, vectors=None):
    p = f"{MAT}/{name}"
    mi = unreal.load_asset(p) if eal.does_asset_exist(p) else         at.create_asset(name, MAT, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    mel.set_material_instance_parent(mi, parent)
    if tex:
        d = tex_set(tex)
        mel.set_material_instance_texture_parameter_value(mi, "Diff", d["diff"])
        mel.set_material_instance_texture_parameter_value(mi, "Nor", d["nor"])
        mel.set_material_instance_texture_parameter_value(mi, "Rgh", d["rough"])
    for k, v in (scalars or {}).items():
        mel.set_material_instance_scalar_parameter_value(mi, k, v)
    for k, v in (vectors or {}).items():
        mel.set_material_instance_vector_parameter_value(mi, k, v)
    mel.update_material_instance(mi)
    eal.save_loaded_asset(mi)
    return mi


# ---------------------------------------------------------------- sol
GROUND_HLSL = r"""
float2 wp = WP.xy / 100.0;                  // mètres
float3 c = Ortho.Sample(OrthoSampler, UV0).rgb;
c = lerp(dot(c, float3(0.299,0.587,0.114)).xxx, c, 0.9);
float4 ma = MaskA.Sample(MaskASampler, UV0); float4 mb = MaskB.Sample(MaskBSampler, UV0);
float road = ma.r, walk = ma.g, sand = ma.b, grass = mb.r;
float3 asph = Asph.Sample(AsphSampler, wp / 4.0).rgb;
float3 wk = Walk.Sample(WalkSampler, wp / 2.5).rgb;
float3 sd = Sand.Sample(SandSampler, wp / 3.0).rgb;
float3 gr = Grass.Sample(GrassSampler, wp / 2.5).rgb;
c = lerp(c, lerp(asph * 0.5, c, 0.2), road);
c = lerp(c, lerp(wk, c, 0.4) * 0.4, walk);
c = lerp(c, lerp(sd, c, 0.5) * float3(0.82,0.70,0.55), sand);
c = lerp(c, lerp(gr, c, 0.4), grass * 0.7);
Rough = lerp(lerp(0.85, 0.92, road), 0.95, sand);
return c;
"""


def ground():
    g = G("M_Sol")
    lin = lambda n: import_tex(os.path.join(TEXDIR, n), f"{TEX}/sol", "linear")
    ortho = import_tex(os.path.join(TEXDIR, "ortho.jpg"), f"{TEX}/sol")
    ortho.set_editor_property("max_texture_size", 8192)
    ortho.set_editor_property("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD)
    ma, mb = lin("ground_mask_a.png"), lin("ground_mask_b.png")
    sets = {k: tex_set(k) for k in ("asphalt_02", "patterned_paving", "coast_sand_01", "leafy_grass")}
    c = g.custom(GROUND_HLSL, [
        ("UV0", g.uv(0), ""), ("WP", g.e(unreal.MaterialExpressionWorldPosition), ""),
        ("Ortho", g.texobj("Ortho", ortho), ""), ("MaskA", g.texobj("MaskA", ma), ""), ("MaskB", g.texobj("MaskB", mb), ""),
        ("Asph", g.texobj("Asph", sets["asphalt_02"]["diff"]), ""), ("Walk", g.texobj("Walk", sets["patterned_paving"]["diff"]), ""),
        ("Sand", g.texobj("Sand", sets["coast_sand_01"]["diff"]), ""), ("Grass", g.texobj("Grass", sets["leafy_grass"]["diff"]), "")],
        [("Rough", F1)])
    g.out(c, "", MP.MP_BASE_COLOR)
    g.out(c, "Rough", MP.MP_ROUGHNESS)
    return g.done()


WATER_HLSL = r"""
float2 p = WP.xy / 100.0 * 0.6;
float t = Time;
float2 d = float2(0,0);
d += float2(cos(p.x * 0.35 + p.y * 0.12 + t * 0.9), cos(p.y * 0.41 - p.x * 0.07 + t * 0.7)) * 0.03;
d += float2(cos(p.x * 1.3 - p.y * 0.9 + t * 1.6), cos(p.y * 1.7 + p.x * 0.6 + t * 1.3)) * 0.012;
d += float2(cos(p.x * 4.1 + p.y * 3.3 + t * 2.4), cos(p.y * 4.7 - p.x * 2.9 + t * 2.1)) * 0.006;
Nrm = normalize(float3(d, 1));
return float3(0.012, 0.045, 0.055);
"""


def water():
    g = G("M_Eau")
    c = g.custom(WATER_HLSL, [("WP", g.e(unreal.MaterialExpressionWorldPosition), ""), ("Time", g.e(unreal.MaterialExpressionTime), "")],
                 [("Nrm", F3)])
    g.out(c, "", MP.MP_BASE_COLOR)
    g.out(c, "Nrm", MP.MP_NORMAL)
    g.out(g.e(unreal.MaterialExpressionConstant, r=0.09), "", MP.MP_ROUGHNESS)
    g.out(g.e(unreal.MaterialExpressionConstant, r=0.6), "", MP.MP_SPECULAR)
    return g.done()


def foliage(tex):
    g = G("M_Feuillage", two_sided=True)
    s = g.e(unreal.MaterialExpressionTextureSampleParameter2D, parameter_name="Diff", texture=tex)
    g.out(s, "RGB", MP.MP_BASE_COLOR)
    g.out(g.e(unreal.MaterialExpressionConstant, r=0.75), "", MP.MP_ROUGHNESS)
    return g.done()


# ---------------------------------------------------------------- affectation
def assign(mesh, mapping):
    mats = mesh.get_editor_property("static_materials")
    changed = False
    for i, sm in enumerate(mats):
        slot = str(sm.get_editor_property("material_slot_name"))
        for key, mat in mapping.items():
            if slot.lower().startswith(key):
                mesh.set_material(i, mat)
                changed = True
                log(f"  {mesh.get_name()}[{slot}] <- {mat.get_name()}")
                break
    if changed:
        eal.save_loaded_asset(mesh, only_if_is_dirty=False)
        got = [s.get_editor_property("material_interface").get_name() for s in mesh.get_editor_property("static_materials")]
        log(f"  {mesh.get_name()} => {got}")


def run():
    wall, roof = wall_master(), roof_master()
    m = {
        "mur_enduit": instance("MI_MurEnduit", wall, "painted_plaster_wall", {"TexScale": 1 / 2.0, "UseTint": 1, "Shutters": 1}),
        "mur_brique": instance("MI_MurBrique", wall, "brick_4", {"TexScale": 1 / 1.5, "UseTint": 0, "Shutters": 1}),
        "mur_pierre": instance("MI_MurPierre", wall, "grey_plaster", {"TexScale": 1 / 2.5, "UseTint": 1, "Shutters": 0}),
        "toit_tuile": instance("MI_ToitTuile", roof, "clay_roof_tiles_02", {"TexScale": 1 / 2.0}),
        "toit_ardoise": instance("MI_ToitArdoise", roof, "roof_slates_02", {"TexScale": 1 / 2.0}),
        "toit_plat": instance("MI_ToitPlat", roof, "gravel_concrete", {"TexScale": 1 / 3.0, "Var": 0.05, "TintAmt": 0.6}),
    }
    sol, eau = ground(), water()
    geo = {a.split(".")[0].split("/")[-1]: unreal.load_asset(a) for a in eal.list_assets("/Game/Atlas/Geo", recursive=True)}
    for name, asset in geo.items():
        if isinstance(asset, unreal.StaticMesh):
            if name == "batiments":
                assign(asset, m)
            elif name == "terrain":
                assign(asset, {"": sol})
            elif name == "eau":
                assign(asset, {"": eau})
    # feuillage de l'arbre feuillu : matériau opaque deux faces (le glTF le déclare en transparence)
    leaves = [a for a in eal.list_assets("/Game/Atlas/Assets/island_tree_01", recursive=True) if "leaves_diff" in a.lower()]
    if leaves:
        fm = foliage(unreal.load_asset(leaves[0]))
        for a in eal.list_assets("/Game/Atlas/Assets/island_tree_01", recursive=True):
            o = unreal.load_asset(a)
            if isinstance(o, unreal.StaticMesh):
                assign(o, {"island_tree_01_leaves": fm})
    else:
        log("texture de feuilles introuvable")
    # les acteurs déjà posés gardent une copie des matériaux : on les remet sur ceux du mesh
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    n = 0
    for a in eas.get_all_level_actors():
        if isinstance(a, unreal.StaticMeshActor):
            c = a.static_mesh_component
            sm = c.static_mesh
            if sm is None:
                continue
            mats = sm.get_editor_property("static_materials")
            ov = c.get_editor_property("override_materials")
            if ov or sm.get_name() in ("batiments", "terrain", "eau", "island_tree_01"):
                c.set_editor_property("override_materials", [])
                for i, s in enumerate(mats):
                    c.set_material(i, s.get_editor_property("material_interface"))
                n += 1
    unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()
    log(f"matériaux appliqués ({n} acteurs mis à jour)")


if __name__ == "__main__":
    run()

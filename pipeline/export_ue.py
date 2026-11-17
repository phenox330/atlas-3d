"""Exporte la scène HQ (terrain, bâtiments, jetées) en glTF pour Unreal + liste d'instances (arbres, mobilier).
Usage : Blender -b <secteur>/build/hq/arcachon_hq.blend -P export_ue.py -- <dossier_sortie>
Les matériaux procéduraux Blender ne passent pas tels quels : Unreal reçoit les slots nommés
(mur_enduit, toit_tuile…) à remplacer par des matériaux Unreal/Megascans."""
import json, sys
from pathlib import Path
import bpy

out = Path(sys.argv[sys.argv.index("--") + 1]); out.mkdir(parents=True, exist_ok=True)
keep = {"terrain", "batiments", "eau", "bordures"}
bpy.ops.object.select_all(action="DESELECT")
for o in bpy.data.objects:
    if o.type == "MESH" and (o.name in keep or o.name.startswith(("Jetée", "jetee", "pieu"))):
        o.select_set(True)
# teinte et graine par bâtiment (attributs de face) → couleur de sommet COLOR_0 (rgb = teinte, a = graine)
import numpy as np
bat = bpy.data.objects.get("batiments")
if bat is not None and "tint" in bat.data.attributes:
    me = bat.data
    nf = len(me.polygons)
    tint = np.zeros(nf * 4, np.float32); me.attributes["tint"].data.foreach_get("color", tint)
    tint = tint.reshape(nf, 4)
    seed = np.zeros(nf, np.float32)
    if "seed" in me.attributes:
        me.attributes["seed"].data.foreach_get("value", seed)
    tint[:, 3] = seed
    loop_face = np.zeros(len(me.loops), np.int64)
    starts = np.zeros(nf, np.int64); totals = np.zeros(nf, np.int64)
    me.polygons.foreach_get("loop_start", starts); me.polygons.foreach_get("loop_total", totals)
    loop_face = np.repeat(np.arange(nf), totals)
    if "Col" in me.color_attributes:
        me.color_attributes.remove(me.color_attributes["Col"])
    ca = me.color_attributes.new("Col", "FLOAT_COLOR", "CORNER")
    ca.data.foreach_set("color", tint[loop_face].ravel())
    me.color_attributes.active_color = ca
    me.color_attributes.render_color_index = me.color_attributes.find("Col")
# objets → maillages en coordonnées monde (transformations appliquées), pieux fusionnés en un seul mesh
for o in list(bpy.context.selected_objects):
    if o.data.users > 1:
        o.data = o.data.copy()
bpy.context.view_layer.objects.active = bpy.context.selected_objects[0]
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
pieux = [o for o in bpy.context.selected_objects if o.name.startswith("pieu")]
if len(pieux) > 1:
    sel = [o.name for o in bpy.context.selected_objects if not o.name.startswith("pieu")]
    bpy.ops.object.select_all(action="DESELECT")
    for o in pieux:
        o.select_set(True)
    bpy.context.view_layer.objects.active = pieux[0]
    bpy.ops.object.join()
    pieux[0].name = "pieux"
    pieux[0].select_set(True)
    for n in sel:
        bpy.data.objects[n].select_set(True)
bpy.ops.export_scene.gltf(filepath=str(out / "arcachon_geo.glb"), use_selection=True, export_format="GLB",
                          export_apply=True, export_yup=True,
                          export_vertex_color="ACTIVE", export_all_vertex_colors=False)
inst = []
for o in bpy.data.objects:
    if o.instance_type == "COLLECTION" and o.instance_collection:
        inst.append({"asset": o.instance_collection.name.replace("asset_", ""), "loc": list(o.location),
                     "rot_z": o.rotation_euler.z, "scale": o.scale.x})
(out / "instances.json").write_text(json.dumps(inst))
print("ATLAS_UE_EXPORT_OK", len(inst), "instances")

"""Blender headless : importe sector.glb, pose soleil + caméra, sauve sector.blend et un rendu d'aperçu.
Usage : Blender -b -P blender_sector.py -- <dossier_build>
"""
import json, math, sys
from pathlib import Path
import bpy

out = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
man = json.loads((out / "manifest.json").read_text())
size = man["size_m"]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(out / "sector.glb"))
scene = bpy.context.scene

# Soleil d'après-midi sur le Bassin (azimut sud-ouest)
sun_data = bpy.data.lights.new("Soleil", type="SUN")
sun_data.energy = 4.0
sun_data.angle = math.radians(1.5)
sun = bpy.data.objects.new("Soleil", sun_data)
sun.rotation_euler = (math.radians(50), 0, math.radians(215))
scene.collection.objects.link(sun)

# Ciel
world = bpy.data.worlds.new("Ciel")
world.use_nodes = True
bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
bg.inputs[0].default_value = (0.55, 0.7, 0.9, 1)
bg.inputs[1].default_value = 0.8
scene.world = world

# Caméra oblique 3/4 sur le secteur (repère Blender : Z haut, glTF -Z nord => Blender +Y nord)
cam_data = bpy.data.cameras.new("Camera")
cam_data.lens = 28
cam_data.clip_end = size * 10
cam = bpy.data.objects.new("Camera", cam_data)
scene.collection.objects.link(cam)
cx, cy = size / 2, size / 2
cam.location = (cx - size * 0.55, cy - size * 0.75, size * 0.55)
target = bpy.data.objects.new("Cible", None)
target.location = (cx, cy, 0)
scene.collection.objects.link(target)
c = cam.constraints.new("TRACK_TO")
c.target = target
c.track_axis = "TRACK_NEGATIVE_Z"
c.up_axis = "UP_Y"
scene.camera = cam

engines = [i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
for e in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES"):
    if e in engines:
        try:
            scene.render.engine = e
            break
        except TypeError:
            pass
scene.render.resolution_x, scene.render.resolution_y = 1600, 900
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = str(out / "preview_blender.png")
bpy.ops.wm.save_as_mainfile(filepath=str(out / "sector.blend"))
bpy.ops.render.render(write_still=True)
print("ATLAS_BLENDER_OK", out / "sector.blend")

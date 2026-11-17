# Atlas : construit le niveau /Game/Atlas/Arcachon (géométrie + arbres + mobilier + ciel).
# Lancement sans fenêtre : UnrealEditor-Cmd <projet>.uproject -run=pythonscript -script=atlas_build.py -unattended
import json, math, os
import unreal

PROJ = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
IMP = os.path.join(PROJ, "Import")
LEVEL = "/Game/Atlas/Arcachon"
MARK = os.path.join(PROJ, "Saved", "atlas_built.txt")


def log(m):
    unreal.log("[Atlas] " + m)


def import_file(path, dest):
    t = unreal.AssetImportTask()
    t.filename, t.destination_path = path, dest
    t.automated, t.replace_existing, t.save = True, True, True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
    objs = [unreal.load_asset(p) for p in t.imported_object_paths]
    return [o for o in objs if isinstance(o, unreal.StaticMesh)]


def build():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for d in ("/Game/Atlas",):
        if unreal.EditorAssetLibrary.does_directory_exist(d):
            unreal.EditorAssetLibrary.delete_directory(d)
    unreal.EditorLoadingAndSavingUtils.new_blank_map(False)
    # géométrie : import en scène pour garder les transformations des nœuds (pieux, eau)
    geo = import_file(os.path.join(IMP, "arcachon_geo.glb"), "/Game/Atlas/Geo")
    log("meshes geo : " + ", ".join(m.get_name() for m in geo))
    for m in geo:
        a = eas.spawn_actor_from_object(m, unreal.Vector(0, 0, 0))
        a.set_folder_path("Atlas/Geo")
        b = m.get_bounds()
        log(f"  {m.get_name()} origine={b.origin} etendue={b.box_extent}")
    # instances
    inst = json.load(open(os.path.join(IMP, "instances.json")))
    cache = {}
    for i, it in enumerate(inst):
        name = it["asset"]
        if name not in cache:
            p = os.path.join(IMP, name, name + ".gltf")
            ms = import_file(p, "/Game/Atlas/Assets/" + name) if os.path.exists(p) else []
            # variantes (ex. 3 pins) : on en prend une par instance, recentrée sur son pivot
            cache[name] = [(m, m.get_bounds().origin) for m in ms]
            log(f"asset {name} : {len(ms)} mesh(es)")
        if not cache[name]:
            continue
        m, c = cache[name][i % len(cache[name])]
        x, y, z = it["loc"]
        s = it["scale"]
        yaw = -math.degrees(it["rot_z"])
        r = math.radians(yaw)
        ox = (c.x * math.cos(r) - c.y * math.sin(r)) * s
        oy = (c.x * math.sin(r) + c.y * math.cos(r)) * s
        a = eas.spawn_actor_from_object(m, unreal.Vector(x * 100 - ox, -y * 100 - oy, z * 100),
                                        unreal.Rotator(0, 0, yaw))
        a.set_actor_scale3d(unreal.Vector(s, s, s))
        a.set_folder_path("Atlas/" + name)
        if i % 1000 == 0:
            log(f"instances {i}/{len(inst)}")
    # ciel, soleil (fin d'après-midi, depuis le sud-ouest), brume
    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 50000), unreal.Rotator(0, -14, 135))
    lc = sun.get_component_by_class(unreal.DirectionalLightComponent)
    lc.set_editor_property("atmosphere_sun_light", True)
    lc.set_editor_property("intensity", 10.0)
    eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
    sky = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(50000, -50000, 2000))
    sky.get_component_by_class(unreal.SkyLightComponent).set_editor_property("real_time_capture", True)
    fog = eas.spawn_actor_from_class(unreal.ExponentialHeightFog, unreal.Vector(50000, -50000, 0))
    fog.get_component_by_class(unreal.ExponentialHeightFogComponent).set_editor_property("fog_density", 0.004)
    eas.spawn_actor_from_class(unreal.VolumetricCloud, unreal.Vector(0, 0, 0))
    ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
    ppv.set_editor_property("unbound", True)
    # caméras des plans (mêmes points de vue que les rendus Blender), en cm, Y inversé
    for nom, (cam, tgt, *_r) in SHOTS.items():
        loc = unreal.Vector(cam[0] * 100, -cam[1] * 100, cam[2] * 100)
        d = unreal.Vector(tgt[0] * 100, -tgt[1] * 100, tgt[2] * 100) - loc
        rot = unreal.MathLibrary.make_rot_from_x(d)
        ca = eas.spawn_actor_from_class(unreal.CineCameraActor, loc, rot)
        ca.set_actor_label("cam_" + nom)
        ca.set_folder_path("Atlas/Cameras")
    ok = unreal.EditorLoadingAndSavingUtils.save_map(unreal.EditorLevelLibrary.get_editor_world(), LEVEL)
    os.makedirs(os.path.dirname(MARK), exist_ok=True)
    open(MARK, "w").write("ok")
    log(f"Niveau construit et sauvé ({ok}) : {LEVEL}")


SHOTS = json.load(open(os.path.join(IMP, "shots.json"))) if os.path.exists(os.path.join(IMP, "shots.json")) else {}

if __name__ == "__main__":
    import sys
    build()
    if os.environ.get("ATLAS_QUIT_AFTER_BUILD"):
        unreal.SystemLibrary.quit_editor()

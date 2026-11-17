# Aperçus rapides : SceneCapture2D depuis chaque caméra cam_* du niveau → Saved/Atlas/apercu_<plan>.png
import os, unreal
PROJ = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
OUT = os.path.join(PROJ, "Saved", "Atlas"); os.makedirs(OUT, exist_ok=True)
W, H = 1600, 900
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = unreal.EditorLevelLibrary.get_editor_world()
rt = unreal.RenderingLibrary.create_render_target2d(world, W, H, unreal.TextureRenderTargetFormat.RTF_RGBA8_SRGB)
cams = [a for a in eas.get_all_level_actors() if a.get_actor_label().startswith("cam_")]
cap = eas.spawn_actor_from_class(unreal.SceneCapture2D, unreal.Vector(0, 0, 0))
cc = cap.get_component_by_class(unreal.SceneCaptureComponent2D)
cc.set_editor_property("texture_target", rt)
cc.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_TONE_CURVE_HDR)
cc.set_editor_property("capture_every_frame", False)
cc.set_editor_property("fov_angle", 50)
for c in cams:
    cap.set_actor_location_and_rotation(c.get_actor_location(), c.get_actor_rotation(), False, False)
    for _ in range(3):
        cc.capture_scene()
    nom = c.get_actor_label()[4:]
    unreal.RenderingLibrary.export_render_target(world, rt, OUT, "apercu_" + nom + ".png")
    unreal.log("[Atlas] apercu " + nom)
eas.destroy_actor(cap)

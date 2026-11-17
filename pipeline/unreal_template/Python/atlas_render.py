# Rendus Unreal (Movie Render Queue, Lumen) des plans de Import/shots.json → Saved/Atlas/renders/<plan>.png
# Variables : ATLAS_RES="2560x1440", ATLAS_SHOTS="a,b", ATLAS_SPATIAL=8, ATLAS_WARMUP=96, ATLAS_EV=0
import json, math, os
import unreal

PROJ = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
IMP = os.path.join(PROJ, "Import")
OUT = os.path.join(PROJ, "Saved", "Atlas", "renders")
LEVEL = "/Game/Atlas/Arcachon"
W, H = (int(v) for v in os.environ.get("ATLAS_RES", "1920x1080").split("x"))
SPATIAL = int(os.environ.get("ATLAS_SPATIAL", "8"))
WARMUP = int(os.environ.get("ATLAS_WARMUP", "96"))
EV = float(os.environ.get("ATLAS_EV", "0.3"))
SHOTS = json.load(open(os.path.join(IMP, "shots.json")))
sel = [s for s in os.environ.get("ATLAS_SHOTS", ",".join(SHOTS)).split(",") if s in SHOTS]
QUIT = os.environ.get("ATLAS_QUIT") == "1"


def log(m):
    unreal.log("[Atlas] " + m)


eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
les.load_level(LEVEL)
actors = eas.get_all_level_actors()
sun = next(a for a in actors if isinstance(a, unreal.DirectionalLight))
ppv = next(a for a in actors if isinstance(a, unreal.PostProcessVolume))
sky = next(a for a in actors if isinstance(a, unreal.SkyLight))
skc = sky.get_component_by_class(unreal.SkyLightComponent)
skc.set_editor_property("intensity", float(os.environ.get("ATLAS_SKY", "2.5")))
skc.set_editor_property("real_time_capture", True)
skc.set_editor_property("lower_hemisphere_is_black", False)
skc.set_editor_property("lower_hemisphere_color", unreal.LinearColor(0.12, 0.11, 0.09, 1))
sun.get_component_by_class(unreal.DirectionalLightComponent).set_editor_property("intensity", float(os.environ.get("ATLAS_SUN", "10")))
cams = {a.get_actor_label()[4:]: a for a in actors if a.get_actor_label().startswith("cam_")}

# post-process : exposition manuelle, un peu de contraste
st = ppv.get_editor_property("settings")
for k, v in [("override_auto_exposure_method", True), ("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL),
             ("override_auto_exposure_apply_physical_camera_exposure", True), ("auto_exposure_apply_physical_camera_exposure", False),
             ("override_auto_exposure_bias", True), ("auto_exposure_bias", EV),
             ("override_vignette_intensity", True), ("vignette_intensity", 0.25),
             ("override_lumen_scene_lighting_quality", True), ("lumen_scene_lighting_quality", 2.0),
             ("override_lumen_final_gather_quality", True), ("lumen_final_gather_quality", 2.0)]:
    try:
        st.set_editor_property(k, v)
    except Exception as e:
        log(f"pp {k} ignoré ({e})")
ppv.set_editor_property("settings", st)

seq_dir = "/Game/Atlas/Sequences"
at = unreal.AssetToolsHelpers.get_asset_tools()


def make_sequence(name, cam):
    path = f"{seq_dir}/seq_{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        unreal.EditorAssetLibrary.delete_asset(path)
    seq = at.create_asset("seq_" + name, seq_dir, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    seq.set_display_rate(unreal.FrameRate(24, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(1)
    b = seq.add_possessable(cam)
    tr = seq.add_track(unreal.MovieSceneCameraCutTrack)
    sec = tr.add_section()
    sec.set_range(0, 1)
    sec.set_camera_binding_id(seq.get_binding_id(b) if hasattr(seq, "get_binding_id")
                              else unreal.MovieSceneSequenceExtensions.get_portable_binding_id(seq, seq, b))
    unreal.EditorAssetLibrary.save_loaded_asset(seq)
    return seq


def setup_shot(name):
    cam_xyz, tgt, lens, az, el = SHOTS[name]
    cam = cams[name]
    loc = unreal.Vector(cam_xyz[0] * 100, -cam_xyz[1] * 100, cam_xyz[2] * 100)
    d = unreal.Vector(tgt[0] * 100, -tgt[1] * 100, tgt[2] * 100) - loc
    cam.set_actor_location_and_rotation(loc, unreal.MathLibrary.make_rot_from_x(d), False, False)
    cc = cam.get_cine_camera_component()
    fb = cc.get_editor_property("filmback")
    fb.set_editor_property("sensor_width", 36.0)
    fb.set_editor_property("sensor_height", 20.25)
    cc.set_editor_property("filmback", fb)
    cc.set_editor_property("current_focal_length", float(lens))
    fs = cc.get_editor_property("focus_settings")
    fs.set_editor_property("focus_method", unreal.CameraFocusMethod.DISABLE)
    cc.set_editor_property("focus_settings", fs)
    # soleil : azimut (0 = est, 90 = nord) / élévation → rotation Unreal (X = est, Y = sud)
    sun.set_actor_rotation(unreal.Rotator(0, -float(el), 180.0 - float(az)), False)
    return make_sequence(name, cam)


subsys = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
queue = subsys.get_queue()
os.makedirs(OUT, exist_ok=True)
pending = list(sel)
state = {"executor": None}


def next_shot(*_):
    if not pending:
        log("rendus terminés")
        if QUIT:
            unreal.SystemLibrary.quit_editor()
        return
    name = pending.pop(0)
    seq = setup_shot(name)
    les.save_current_level()
    queue.delete_all_jobs()
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.set_editor_property("job_name", name)
    job.set_editor_property("sequence", unreal.SoftObjectPath(seq.get_path_name()))
    job.set_editor_property("map", unreal.SoftObjectPath(LEVEL + "." + LEVEL.split("/")[-1]))
    cfg = job.get_configuration()
    o = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    o.set_editor_property("output_resolution", unreal.IntPoint(W, H))
    o.set_editor_property("output_directory", unreal.DirectoryPath(OUT))
    o.set_editor_property("file_name_format", name)
    o.set_editor_property("override_existing_output", True)
    cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
    cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    aa = cfg.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.set_editor_property("spatial_sample_count", SPATIAL)
    aa.set_editor_property("temporal_sample_count", 1)
    aa.set_editor_property("override_anti_aliasing", True)
    aa.set_editor_property("anti_aliasing_method", unreal.AntiAliasingMethod.AAM_TSR)
    aa.set_editor_property("use_camera_cut_for_warm_up", False)
    aa.set_editor_property("engine_warm_up_count", WARMUP)
    aa.set_editor_property("render_warm_up_count", WARMUP)
    aa.set_editor_property("render_warm_up_frames", True)
    cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    for k, v in [("r.ScreenPercentage", 100), ("r.Lumen.Reflections.Allow", 1), ("r.Shadow.Virtual.Enable", 1),
                 ("r.Nanite.MaxPixelsPerEdge", 0.5), ("r.ForceLOD", 0), ("foliage.ForceLOD", 0)]:
        try:
            cv.add_or_update_console_variable(k, v)
        except Exception:
            pass
    log(f"rendu {name} ({W}x{H}, {SPATIAL} échantillons, chauffe {WARMUP})")
    ex = unreal.MoviePipelinePIEExecutor()
    ex.on_executor_finished_delegate.add_callable(lambda executor, ok: next_shot())
    state["executor"] = ex
    subsys.render_queue_with_executor_instance(ex)


next_shot()

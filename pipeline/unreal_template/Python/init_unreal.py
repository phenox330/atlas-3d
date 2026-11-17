# Atlas : au lancement de l'éditeur, exécute le script indiqué par ATLAS_AUTORUN (ex. atlas_render.py)
# une fois l'éditeur prêt. Sans variable : construit le niveau au premier lancement.
import os, runpy
import unreal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
MARK = os.path.join(PROJ, "Saved", "atlas_built.txt")
_h = None
_n = [0]


def _tick(dt):
    global _h
    _n[0] += 1
    if _n[0] < 30:  # laisser l'éditeur finir de charger
        return
    unreal.unregister_slate_post_tick_callback(_h)
    todo = [s for s in os.environ.get("ATLAS_AUTORUN", "").split(",") if s]
    if not todo and not os.path.exists(MARK):
        todo = ["atlas_build.py", "atlas_materials.py"]
    for s in todo:
        try:
            runpy.run_path(os.path.join(HERE, s), run_name="__main__")
        except Exception as e:
            import traceback
            unreal.log_error("[Atlas] échec %s : %s\n%s" % (s, e, traceback.format_exc()))
            if os.environ.get("ATLAS_QUIT") == "1":
                unreal.SystemLibrary.quit_editor()


if os.environ.get("ATLAS_AUTORUN") or not os.path.exists(MARK):
    _h = unreal.register_slate_post_tick_callback(_tick)

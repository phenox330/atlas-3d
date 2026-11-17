"""Génère un projet Unreal 5.8 prêt à ouvrir pour un secteur Atlas (testé le 13/11/2026 sur UE 5.8.4, Mac M3).

Usage : uv run ue_project.py <dossier_secteur> [<dossier_projets_unreal>]
Prérequis : export_ue.py exécuté (build/hq/unreal/arcachon_geo.glb + instances.json), Xcode + Metal Toolchain.
Le projet contient Content/Python (modèle : pipeline/unreal_template) :
  atlas_build.py      importe la géométrie et les modèles Poly Haven, place arbres/mobilier, ciel, caméras
  atlas_materials.py  matériaux Unreal (façades à fenêtres procédurales, toits, sol ortho + masques, eau, feuilles)
  atlas_render.py     rendus Movie Render Queue (Lumen) des plans de Import/shots.json → Saved/Atlas/renders
  init_unreal.py      au lancement : construit le niveau si besoin, ou exécute ATLAS_AUTORUN
Rendu sans intervention : ATLAS_AUTORUN=atlas_render.py ATLAS_QUIT=1 ATLAS_RES=2560x1440 UnrealEditor <projet>.uproject
"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sec = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else HERE.parent / "unreal"
name = "Atlas" + "".join(w.capitalize() for w in sec.name.split("-"))[:40]
proj = root / name
src = sec / "build" / "hq" / "unreal"
assets = HERE / "assets"
tpl = HERE / "unreal_template"
if not (src / "arcachon_geo.glb").exists():
    sys.exit("Lance d'abord export_ue.py (bouton Rendu HQ puis Paquet Unreal)")

for d in ("Content/Python", "Config", "Import/textures"):
    (proj / d).mkdir(parents=True, exist_ok=True)
shutil.copy2(src / "arcachon_geo.glb", proj / "Import" / "arcachon_geo.glb")
shutil.copy2(src / "instances.json", proj / "Import" / "instances.json")
for m in {i["asset"] for i in json.loads((src / "instances.json").read_text())}:
    if (assets / "models" / m).exists():
        shutil.copytree(assets / "models" / m, proj / "Import" / m, dirs_exist_ok=True)
for t in ("painted_plaster_wall", "brick_4", "grey_plaster", "clay_roof_tiles_02", "roof_slates_02", "gravel_concrete",
          "asphalt_02", "patterned_paving", "coast_sand_01", "leafy_grass"):
    (proj / "Import" / "textures" / t).mkdir(exist_ok=True)
    for k in ("diff", "nor", "rough"):
        if (assets / "textures" / t / f"{k}.jpg").exists():
            shutil.copy2(assets / "textures" / t / f"{k}.jpg", proj / "Import" / "textures" / t / f"{k}.jpg")
shutil.copy2(sec / "raw" / "ortho.jpg", proj / "Import" / "textures" / "ortho.jpg")
for m in ("ground_mask_a.png", "ground_mask_b.png"):
    shutil.copy2(sec / "build" / "hq" / m, proj / "Import" / "textures" / m)
for f in (tpl / "Python").glob("*.py"):
    shutil.copy2(f, proj / "Content" / "Python" / f.name)
shutil.copy2(tpl / "DefaultEngine.ini", proj / "Config" / "DefaultEngine.ini")

# plans : mêmes points de vue que blender_hq.py (caméra, cible, focale, azimut et élévation du soleil)
sys.path.insert(0, str(HERE))
import numpy as np  # noqa: E402
SHOTS = {}
src_hq = (HERE / "blender_hq.py").read_text()
ns = {}
exec(src_hq[src_hq.index("SHOTS = {"):src_hq.index("\n}\n", src_hq.index("SHOTS = {")) + 3], ns)
mnt = np.load(sec / "raw" / "mnt.npy")
size = json.loads((sec / "sector.json").read_text())["size_m"]


def gz(x, y):
    i = int(np.clip((1 - y / size) * (mnt.shape[0] - 1), 0, mnt.shape[0] - 1))
    j = int(np.clip(x / size * (mnt.shape[1] - 1), 0, mnt.shape[1] - 1))
    return float(mnt[i, j])


for k, s in ns["SHOTS"].items():
    c, (_, az, el, *_r) = s["cam"], s["sky"]
    if c[0] == "street":
        (x, y), (tx, ty), lens = c[1], c[2], c[3]
        SHOTS[k] = [[x, y, gz(x, y) + 1.7], [tx, ty, gz(tx, ty) + 1.7], lens, az, el]
    else:
        SHOTS[k] = [list(c[0]), list(c[1]), c[2], az, el]
(proj / "Import" / "shots.json").write_text(json.dumps(SHOTS, indent=1))

(proj / f"{name}.uproject").write_text(json.dumps({
    "FileVersion": 3, "EngineAssociation": "5.8", "Category": "", "Description": "Atlas — secteur réel (IGN/OSM)",
    "Plugins": [{"Name": n, "Enabled": True} for n in
                ("PythonScriptPlugin", "EditorScriptingUtilities", "Interchange", "MovieRenderPipeline")],
}, indent=2))

(proj / "LISEZMOI.md").write_text(f"""# {name}

Projet Unreal 5.8 généré par Atlas pour le secteur « {json.loads((sec / 'sector.json').read_text())['name']} ».

1. Ouvrir `{name}.uproject`. Au premier lancement, le niveau /Game/Atlas/Arcachon est construit (import, matériaux,
   {len(json.loads((src / 'instances.json').read_text()))} arbres et objets, ciel, caméras cam_*). Compter quelques minutes (shaders).
2. Rendus : bouton « Rendu Unreal » dans Atlas, ou
   `ATLAS_AUTORUN=atlas_render.py ATLAS_QUIT=1 ATLAS_RES=2560x1440 UnrealEditor {name}.uproject`
   → `Saved/Atlas/renders/<plan>.png`.
3. Reconstruire : supprimer `Saved/atlas_built.txt` (ou ATLAS_AUTORUN=atlas_build.py,atlas_materials.py).
Prérequis Mac : Xcode sélectionné + Metal Toolchain (`xcodebuild -downloadComponent MetalToolchain`).
""")
print("ATLAS_UE_PROJECT_OK", proj)

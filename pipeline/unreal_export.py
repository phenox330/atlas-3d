"""Prépare le paquet Unreal d'un secteur : script Python d'import + fiche de réglages Landscape.
Usage : uv run unreal_export.py <dossier_secteur>
Le script généré s'exécute DANS Unreal (Output Log > Python, ou Tools > Execute Python Script).
"""
import json
import sys
from pathlib import Path

sec_dir = Path(sys.argv[1]).resolve()
build = sec_dir / "build"
man = json.loads((build / "manifest.json").read_text())
cfg = json.loads((sec_dir / "sector.json").read_text())
slug = sec_dir.name.replace("-", "_")
ue = build / "unreal"
ue.mkdir(exist_ok=True)
hm = man["heightmap"]

script = f'''# Généré par Atlas — import du secteur « {cfg["name"]} » dans Unreal Engine 5.x
# Exécuter dans l'éditeur : Tools > Execute Python Script (plugin « Python Editor Script Plugin » activé)
import unreal

DEST = "/Game/Atlas/{slug}"
FILES = {{
    "Terrain": r"{build / "terrain.glb"}",
    "Batiments": r"{build / "buildings.glb"}",
    "Eau": r"{build / "water.glb"}",
}}

tasks = []
for name, path in FILES.items():
    import os
    if not os.path.exists(path):
        continue
    t = unreal.AssetImportTask()
    t.filename = path
    t.destination_path = DEST + "/" + name
    t.automated = True
    t.replace_existing = True
    t.save = True
    tasks.append(t)
unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)

# glTF est en mètres, Unreal en cm : on pose les meshes à l'échelle x100.
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for t in tasks:
    for p in t.imported_object_paths:
        asset = unreal.load_asset(p)
        if isinstance(asset, unreal.StaticMesh):
            a = eas.spawn_actor_from_object(asset, unreal.Vector(0, 0, 0))
            a.set_actor_scale3d(unreal.Vector(100, 100, 100))
            a.set_folder_path("Atlas/{slug}")
unreal.log("Atlas : secteur {slug} importé")
'''
(ue / f"import_{slug}.py").write_text(script)

readme = f"""# Secteur « {cfg['name']} » → Unreal

## Option A — import rapide des maillages (script)
1. Ouvrir le projet Unreal (UE 5.8 recommandé), activer le plugin **Python Editor Script Plugin** et **Interchange glTF**.
2. Tools > Execute Python Script > `{ue / f'import_{slug}.py'}`
3. Les meshes arrivent dans `/Game/Atlas/{slug}` à l'échelle x100 (m → cm).

## Option B — terrain en Landscape (recommandé pour la version jouable)
Landscape Mode > Manage > New > Import from File : `{build / 'heightmap_unreal.png'}`
- Résolution : {hm['resolution']} x {hm['resolution']}
- Scale X / Y : **{hm['unreal_scale_xy_cm']:.3f}**
- Scale Z : **{hm['unreal_scale_z_pct']:.4f}**
- Location Z : **{hm['unreal_location_z_cm']:.1f}** (cm)
- Altitudes réelles : {hm['zmin_m']:.2f} m → {hm['zmax_m']:.2f} m (NGF)
Puis importer seulement `buildings.glb` et `water.glb` (option A, en retirant Terrain).

## Repère
Origine (0,0) = coin sud-ouest du secteur, Lambert-93 {man['origin_l93'][0]:.1f} / {man['origin_l93'][1]:.1f}.
Taille : {man['size_m']:.0f} m. Les routes OSM (polylignes locales en m, z = sol) sont dans `roads.json` pour des splines.
"""
(ue / "README_UNREAL.md").write_text(readme)
print(f"ATLAS_UNREAL_OK {ue}")

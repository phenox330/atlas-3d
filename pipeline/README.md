# Pipeline Atlas

Projet uv (Python 3.14). Les scripts sont lancés par l'app (`server/index.ts`, route `/run`).
Chaque script imprime `@@PROGRESS <0..1> <étape>` pour la barre de progression.

| Script | Rôle |
|---|---|
| `atlas.py fetch\|build\|all <secteur>` | Téléchargement (MNT LiDAR HD, ortho, BD TOPO, OSM via Overpass avec 3 miroirs) et blockout |
| `blender_sector.py` | Scène Blender du blockout |
| `hq_prepare.py` | Haute qualité : toits 2 et 4 pans, façades, arbres depuis le MNH LiDAR, masques de sol OSM, jetées, mobilier |
| `blender_hq.py` | Scène Cycles et rendu des plans (`--samples --res --shots --view --exposure`) |
| `polyhaven.py` | Téléchargement des assets CC0 dans `assets/` (hors git) |
| `export_ue.py` | Blender → glb monde + pieux fusionnés + `instances.json` |
| `ue_project.py` | Génère le projet Unreal 5.8 depuis `unreal_template/` |
| `ue_render.sh` | Rendu Movie Render Queue et Lumen, éditeur masqué |
| `unreal_export.py` | Export blockout pour Unreal (heightmap) |

Blender headless : `Blender -b -P <script> -- <args>`. Les nœuds se cherchent par type, pas par nom.

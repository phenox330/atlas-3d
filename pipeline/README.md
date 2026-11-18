# Pipeline Atlas

Projet uv (Python 3.14). Les scripts sont lancés par l'app (`server/index.ts`, route `/run`).
Chaque script imprime `@@PROGRESS <0..1> <étape>` pour la barre de progression.

| Script | Rôle |
|---|---|
| `atlas.py fetch\|build\|all <secteur>` | Téléchargement (MNT LiDAR HD, ortho, BD TOPO, OSM via Overpass avec 3 miroirs) et blockout |
| `blender_sector.py` | Scène Blender du blockout |
| `facades.py` | Photos Panoramax → vision (`claude -p`) → attributs de façade par bâtiment (`build/hq/facades.json`) |
| `hq_prepare.py` | Haute qualité : toits en pans (LiDAR), façades en relief, arbres depuis le MNH, masques de sol, mobilier, voitures, bateaux, alentours (couronne 3 km), textures de la vue 3D |
| `web_export.py` | Scène Blender → vue 3D de l'app (`build/hq/web/`) : même géométrie, mêmes plans et même ciel que les rendus ; arbres en atlas de vues. Appelé par `blender_hq.py`, ou seul : `Blender -b <secteur>/build/hq/arcachon_hq.blend -P web_export.py -- <secteur> [--only=…] [--rebake]` |
| `glbw.py` | Écriture de fichiers glb sans dépendance (utilisable dans le Python de Blender) |
| `facade_geo.py` | Géométrie des façades : baies percées, menuiseries, volets, portes, vitrines, enseignes, balcons, débords de toit, gouttières |
| `cars.py` | Voitures génériques (5 gabarits) et bateaux au mouillage, générés dans Blender |
| `blender_hq.py` | Scène Cycles et rendu des 8 plans (`--samples --res --shots --view --look --exposure --out --nosave --nocars`) |
| `polyhaven.py` | Téléchargement des assets CC0 dans `assets/` (hors git) |
| `export_ue.py` | Blender → glb monde + pieux fusionnés + `instances.json` |
| `ue_project.py` | Génère le projet Unreal 5.8 depuis `unreal_template/` |
| `ue_render.sh` | Rendu Movie Render Queue et Lumen, éditeur masqué |
| `unreal_export.py` | Export blockout pour Unreal (heightmap) |

Blender headless : `Blender -b -P <script> -- <args>`. Les nœuds se cherchent par type, pas par nom.

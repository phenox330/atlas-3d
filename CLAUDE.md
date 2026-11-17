# Atlas — villes réelles → map de jeu 3D

App locale (http://127.0.0.1:4896) pour recréer le Bassin d'Arcachon puis Bordeaux, workflow inspiré de Paul Paccini :
données ouvertes (IGN LiDAR HD, BD TOPO, ortho, OSM, Panoramax) → Blender → Unreal, l'IA code les générateurs.

## Structure
- `server/index.ts` : API Hono (Bun). Lance les tâches (pipeline, Blender, Claude) et les diffuse en SSE.
- `server/workflow.ts` : étapes du workflow + secteurs prédéfinis (coordonnées approximatives).
- `web/` : interface (vanilla JS, Leaflet, three.js servis depuis node_modules).
- `pipeline/` : projet uv (Python 3.14). `atlas.py fetch|build|all <secteur>`, `blender_sector.py`, `unreal_export.py`.
  Façades : `facades.py` (photos Panoramax → rayons vers les bâtiments → Claude vision via `claude -p` → build/hq/facades.json, cache raw/facades/).
  Haute qualité : `hq_prepare.py` (toits relevés sur la surface LiDAR + pans RANSAC, repli procédural si pas d'écho → qa_lidar.json ;
  façades photo > BD TOPO > hasard, vecteur `fa` par face ; arbres LiDAR MNH, arbustes LiDAR ; marquages, passages piétons,
  bordures, mobilier réel OSM ; masques de sol)
  → `blender_hq.py` (scène Cycles + 5 plans dans SHOTS, options --samples --res --shots --view --exposure)
  → `export_ue.py` (Blender → glb monde + pieux fusionnés + teinte/graine en COLOR_0, instances.json)
  → `ue_project.py` (projet Unreal 5.8 depuis `pipeline/unreal_template/` : atlas_build, atlas_materials, atlas_render)
  → `ue_render.sh` (Movie Render Queue/Lumen, éditeur masqué → build/hq/renders/unreal_*.png). Testé 13/11 sur UE 5.8.4.
  Pièges UE : plugin GLTFImporter inexistant (Interchange) ; ExecutePythonScript ferme l'éditeur → passer par init_unreal.py + ATLAS_AUTORUN ;
  StaticMesh.set_material (pas static_materials) ; normal maps BC5 = Z à reconstruire ; r.GenerateMeshDistanceFields obligatoire pour Lumen ;
  ne jamais supprimer un matériau utilisé (crash) ; Xcode sélectionné + Metal Toolchain requis.
  Assets CC0 Poly Haven : `polyhaven.py` → `pipeline/assets/` (1,1 Go, hors git).
- `projects/<projet>/project.json` (workflow, session Claude) ; `projects/<projet>/sectors/<secteur>/` :
  `sector.json` (center [lon, lat], size_m, mnt_res_m, ortho_res_m, water_level_m), `raw/` (données), `build/` (sorties).

## Conventions
- Repère : Lambert-93 (EPSG:2154), origine = coin sud-ouest du secteur, mètres. glTF : Y haut, -Z = nord.
  Tous les secteurs partagent Lambert-93 → ils se raccordent. Unreal = cm (x100).
- `build/manifest.json` décrit chaque build ; garder ce format (l'UI le lit).
- Le pipeline imprime `@@PROGRESS <0..1> <étape>` pour la barre de progression.
- Blender headless : `/Applications/Blender.app/Contents/MacOS/Blender -b -P <script> -- <args>` (Blender 5.2).
  Chercher les nœuds par type, pas par nom.
- Nouveau script pipeline = l'appeler depuis atlas.py ou ajouter une action dans `server/index.ts` (route `/run`) + un bouton.
- Toujours tester sur un secteur réel avant de dire que c'est fini.

## Licences
IGN = Licence Ouverte Etalab 2.0 (attribution). OSM = ODbL (attribution, base dérivée partageable). Panoramax = libre (vérifier par photo).
Google Photorealistic 3D Tiles : INTERDIT comme source d'assets. Enseignes = noms fictifs. Pas d'éclairage nocturne de monuments protégés.

## Documentation
- `docs/PRD.md` : vision, état, roadmap, décisions ouvertes. Le mettre à jour à chaque jalon.
- `docs/recherche/` : état de l'art. `docs/ressources/` : retours d'expérience externes + leçons retenues pour Atlas.

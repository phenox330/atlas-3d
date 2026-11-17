# Retour d'expérience : « GTA Marseille » codé avec des IA

- **Source** : vidéo YouTube, https://youtu.be/4XzhMRmbUcw (≈ 44 min, en français)
- **Sujet** : un créateur non développeur de jeux reconstruit Marseille et Miami dans Unreal Engine avec des agents IA (Codex, Claude Code), en environ deux semaines.
- **Statut** : notes de lecture. Les chiffres viennent de l'auteur et n'ont pas été vérifiés. La transcription n'est pas reproduite ici (droits d'auteur) : les horodatages renvoient à la vidéo.

## Ce qu'il a fait, en bref

| Brique | Sa méthode | Horodatage |
|---|---|---|
| Tracé de la ville | OSM d'abord (rues, emprises 2D), extrusion = « boîtes à chaussures » | [09:23](https://youtu.be/4XzhMRmbUcw?t=563) |
| Relief et toits | LiDAR HD IGN. La version allégée (≈ 2 pts/m²) donne des routes tordues et des trous ; le **nuage dense** récupère la forme des toits | [10:16](https://youtu.be/4XzhMRmbUcw?t=616) |
| Emprises | Abandon d'OSM pour **BD TOPO** (France) et les données du comté (Miami) | [12:37](https://youtu.be/4XzhMRmbUcw?t=757) |
| Monuments | L'agent récupère photos et plans, découpe les pierres par vision, estime la profondeur, modélise dans Blender, crée des textures. ≈ 20 h d'agent et 11 sous-agents pour la cathédrale de la Major | [14:56](https://youtu.be/4XzhMRmbUcw?t=896) |
| Façades fidèles | Photos de façade : l'agent compte fenêtres, portes et types de volets, bâtiment par bâtiment | [35:43](https://youtu.be/4XzhMRmbUcw?t=2143) |
| Props | Image→3D. Astuce : générer des **vues face, dos, gauche et droite**, puis un modèle image→3D multi-vues. Des milliers d'assets générés en une nuit | [19:20](https://youtu.be/4XzhMRmbUcw?t=1160) |
| Textures avec texte | L'image→3D abîme les écritures : régénérer la carte de texture à part et la recoller. Faire réparer les roues déformées par l'agent | [20:49](https://youtu.be/4XzhMRmbUcw?t=1249) |
| Personnages | Générés en **T-pose** et en sous-vêtements, puis vêtements interchangeables ; squelette et animations via Mixamo | [21:57](https://youtu.be/4XzhMRmbUcw?t=1317) |
| Vie urbaine | IA de comportement : police, pompiers, trafic, bateaux, passants | [23:25](https://youtu.be/4XzhMRmbUcw?t=1405) |
| Signalisation | Bases de données d'emplacement des panneaux | [35:30](https://youtu.be/4XzhMRmbUcw?t=2130) |
| Streaming | Bulle de ≈ 500 m de chargement des textures par niveau de détail ; projet ≈ 1 To | [42:21](https://youtu.be/4XzhMRmbUcw?t=2541) |

## Chiffres annoncés (non vérifiés)

- Première version de la ville : 3,3 milliards de tokens en entrée, soit ≈ 5 000 $ en équivalent API.
- Jeu complet : ≈ 60 000 $ en équivalent API.
- Unreal contre moteur Rust maison : le Rust est plus rapide sur une petite scène (≈ 420 contre 150 fps) et plus léger en mémoire (2,2 contre 6 Go), mais **Unreal gagne sur la ville complète** (60 contre 35 fps) et sur la qualité de lumière et de reflets ([26:00](https://youtu.be/4XzhMRmbUcw?t=1560)).

## Ses mises en garde

- **Pareto** : construire la ville est la petite partie. Les détails (éclaboussures, sable collé aux pieds, traces d'eau) font le jeu ([28:46](https://youtu.be/4XzhMRmbUcw?t=1726)).
- **Agent sans limite** : un sous-agent a tourné 48 h et reconstruit les chips d'un rayon de supermarché une par une. Il faut fixer un curseur de détail ([32:24](https://youtu.be/4XzhMRmbUcw?t=1944)).
- **Agents spécialisés** : son conseil s'il recommençait. Un agent par domaine (bipèdes, voitures, façades, hélicoptère), comme les spécialités d'un studio ([33:17](https://youtu.be/4XzhMRmbUcw?t=1997)).
- **Erreurs LiDAR** : zones surélevées, trous. Une vérification à la main est nécessaire ([35:00](https://youtu.be/4XzhMRmbUcw?t=2100)).
- **Les récifs et les roches** sont ce qu'il n'a pas réussi à modéliser ([17:53](https://youtu.be/4XzhMRmbUcw?t=1073)).

## Ce qu'on reprend dans Atlas

| Leçon | Application à Atlas | Où |
|---|---|---|
| LiDAR dense pour les toits | Lire le nuage de points classé (classe bâtiment) au lieu des seuls rasters MNS/MNH | PRD, jalon B |
| Façades comptées sur photo | Panoramax (pas Street View) → paramètres du générateur de villas pour le front de mer | PRD, jalon B |
| Image→3D multi-vues | Landmarks et props : 4 vues → image→3D → nettoyage Blender MCP | PRD, jalon C |
| Texte régénéré à part | Enseignes fictives et panneaux : texture séparée, jamais via l'image→3D | PRD, jalon C |
| Budget par détail | Tableau des niveaux de détail et plafond par agent | PRD, section 5.3 |
| Agents spécialisés | Un agent par domaine : bâti, eau et marées, végétation, bateaux, lumière | À formaliser (sous-agents Claude Code) |
| QA LiDAR | Détection automatique des trous et pics, puis revue | PRD, jalon A |
| Détails Pareto | Spécifiques au Bassin : sable mouillé, estran, sillage des pinasses, coucher de soleil | PRD, jalon D |
| Rester sur Unreal | Pas de moteur maison pour une ville entière | PRD, section 7 |

## Ce qu'on ne reprend pas

- **Street View et Google** comme source d'assets : interdit par leurs CGU. On utilise Panoramax.
- **Vraies marques et maillots** (il en avait, générés sans le vouloir) : enseignes fictives uniquement.
- **« Budget illimité »** : chaque agent a un plafond.

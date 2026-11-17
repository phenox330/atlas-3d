# Atlas — PRD

> Document vivant. Dernière mise à jour : 17/11/2026 (passe « réalisme » sur arcachon-centre-ville).
> Sources : `CLAUDE.md` (architecture), `server/workflow.ts` (étapes détaillées et prompts),
> `docs/recherche/` (état de l'art), `docs/ressources/` (retours d'expérience externes).

## 1. Vision

Recréer le **Bassin d'Arcachon**, puis **Bordeaux**, en map 3D réaliste et jouable, à partir de
**données ouvertes** (IGN, OSM, Panoramax). Une seule app locale pilote le pipeline, et l'IA
écrit les générateurs plutôt que de modéliser à la main.

La ville doit être **la vraie** : bonne implantation, bonnes hauteurs, bon relief. Elle n'est pas
inventée par un modèle génératif.

## 2. Objectif final (décision ouverte)

Trois débouchés possibles, pas encore tranchés :

| Option | Ce que ça implique |
|---|---|
| **Jeu** (balade, vélo, pinasse) | Gameplay, perf, packaging, licence Unreal au-delà du seuil de CA |
| **Démo / contenu** (vidéo, rendus) | Priorité au rendu et aux plans, pas à la jouabilité |
| **Méthode open source** | Priorité au pipeline reproductible et documenté, pour n'importe quelle ville française |

Ce choix fixe les priorités de la roadmap (section 6). En attendant, on avance sur le tronc
commun aux trois options : données → bâti → rendu.

## 3. Principes

1. **Données ouvertes d'abord, IA ensuite.** L'IA sert aux générateurs, aux props et aux détails. Jamais aux bâtiments entiers.
2. **Un secteur réel à chaque étape.** Rien n'est « fini » avant d'avoir été testé sur un secteur.
3. **Tout passe par l'app.** Une nouvelle étape = un script `pipeline/` + une action `server/index.ts` + un bouton.
4. **Repère commun Lambert-93.** Les secteurs se raccordent sans recalage.
5. **Budget par classe d'objet** (voir 5.3). Le niveau de détail est décidé avant de lancer un agent, pas après.
6. **Le détail fait la différence (Pareto).** 80 % de la ville sort vite. Les 20 % restants (sable mouillé, sillage, lumière du soir) sont ce qui la rend crédible, et c'est là qu'on investit.

## 4. Périmètre

**Dans le périmètre**
- Bassin d'Arcachon : Arcachon (centre-ville, Ville d'Hiver, jetées, port), puis Cap Ferret, dune du Pilat, ports ostréicoles.
- Bordeaux ensuite (îlot test Chartrons ou Saint-Pierre).
- Rendu Blender Cycles (look-dev) et Unreal 5.8 Lumen (cible).

**Hors périmètre (pour l'instant)**
- Tuiles Google Photorealistic 3D et Street View comme source d'assets (CGU : interdit).
- Vraies marques et enseignes : uniquement des noms fictifs.
- Génération de la ville par un *world model* (voir `docs/recherche/`).
- Moteur maison (Rust, etc.) : Unreal reste la cible tant que la scène grossit (voir 7).

## 5. État au 17/11/2026

Priorité actuelle : **rendre le secteur `arcachon-centre-ville` réaliste avant d'étendre la zone.**
Rendus avant/après : `build/hq/renders/avant_2026-11-17/` contre `build/hq/renders/` (hors git).

### 5.1 Ce qui marche
- **3 secteurs** : `jetee-thiers` (500 m), `arcachon-centre-ville` (1 km²), `arcachon-ville-d-hiver`.
- **Données** : MNT et MNH LiDAR HD, ortho 20 cm, BD TOPO, OSM (avec passages piétons, feux, stops, panneaux, bornes), Panoramax découpé en 4×4 cellules (2 428 photos au lieu de 500).
- **Toits relevés au LiDAR** (17/11) : la surface LiDAR est triangulée dans chaque emprise, découpée en pans plans (RANSAC), bornée entre égout et faîtage. Les murs montent jusqu'aux pans. Les terrasses sont détectées et posées à plat avec acrotère. Résultat sur le secteur : 1 546 bâtiments, dont 563 en toit plat.
- **Couleur réelle des toits** (17/11) : l'orthophoto vue d'avion × le grain des tuiles et ardoises.
- **Façades lues sur photo** (17/11, `facades.py`) : 239 bâtiments décrits par vision sur 250 photos. Pour chacun : couleur d'enduit, étages, volets (battants, roulants, aucun) et leur couleur, chaînages brique, lambrequins, vitrine au RDC. Sans photo, on se rabat sur la BD TOPO (matériaux des murs et du toit, nombre d'étages), puis sur le hasard.
- **Shader de façade** : hauteur d'étage réelle, volets roulants, harpes et encadrements en brique, vitrines connues.
- **Lambrequins** : frise de bois découpé (festons + jours) sous les rives.
- **Rue** : 675 tirets axiaux, 198 passages piétons, 36 lignes d'arrêt, bordures granit, trottoir continu jusqu'aux façades (couleur relevée sur les photos), plaques d'égout.
- **Mobilier réel OSM** : bancs, poubelles, terrasses devant les restaurants, lampadaires ; 2 500 arbustes placés depuis la végétation basse du LiDAR.
- **Sable mouillé** sur 1 m au-dessus du niveau de l'eau.
- **QA LiDAR** : `build/hq/qa_lidar.json` liste les emprises sans écho (15 sur le secteur).
- **Rendus** : Blender Cycles (5 plans) et Unreal 5.8.4 (Lumen).

### 5.2 Limites connues
- **Unreal pas encore à parité** : il reçoit la nouvelle géométrie (toits relevés, bordures), mais pas encore les données de façade (volets, chaînages), les lambrequins, ni la couleur orthophoto des toits.
- Sur certains pans raides, l'orthophoto « coule » (façades vues en biais sur la photo d'avion).
- Fenêtres encore en grille régulière : la vision compte les étages, pas encore la position de chaque baie. Pas de balcons en relief.
- Pas de véhicules ni de pinasses : aucun modèle CC0 fidèle (le « ship_pinnace » de Poly Haven est un galion). Il faut passer par l'image→3D (jalon C).
- Arbres d'alignement : modèle Poly Haven générique (pas de platane ni de pin maritime fidèle).
- Bathymétrie absente : plan d'eau plat.
- Nuage de points LiDAR classé (sol / bâtiment / végétation) non branché : le service WFS IGN utilisé ne l'expose pas. On travaille sur le MNH, donc un arbre au-dessus d'un toit peut le bosseler.
- Pas encore de personnage jouable : l'échelle à hauteur d'homme n'est pas validée.

### 5.3 Niveaux de détail et budget
| Classe | Méthode | Budget agent indicatif |
|---|---|---|
| Bâti courant | Procédural par typologie | Générateur commun, pas d'agent par bâtiment |
| Bâti « fidèle » (front de mer, rues clés) | Photos Panoramax → nombre de fenêtres, portes, volets → paramètres du générateur | Plafonné par rue |
| Landmarks (3 à 5) | Vues multi-angles → image→3D → nettoyage Blender, ou modélisation | Plafonné par monument, arrêt manuel au-delà |
| Props | Bibliothèque Poly Haven / Megascans, puis IA image→3D | Par lot |

Aucun agent ne tourne sans plafond explicite (temps ou itérations).

## 6. Roadmap

Les étapes détaillées, avec leurs prompts, sont dans `server/workflow.ts`. Ordre proposé :

### Jalon A : Fondations (en cours)
- [x] Données, blockout, rendu Blender, rendu Unreal
- [x] Passe QA LiDAR : emprises sans écho détectées et renvoyées au procédural (`qa_lidar.json`)
- [x] Marquages, passages piétons, bordures, trottoirs continus
- [ ] **Parité Unreal** : données de façade (UV2/UV3), lambrequins, couleur orthophoto des toits
- [ ] **Valider l'échelle** : un Third Person Character qui marche dans `arcachon-centre-ville`

### Jalon B : Le Bassin crédible
- [ ] Bathymétrie (SHOM, licence à vérifier) + estran LiDAR + niveau de marée paramétrable
- [x] Toits relevés depuis la surface LiDAR (pans plans RANSAC) + couleur orthophoto
- [ ] Nuage de points LiDAR HD **classé** pour retirer les arbres au-dessus des toits
- [x] Façades lues sur les photos Panoramax (vision), avec repli BD TOPO
- [ ] Façades V2 : position réelle des baies, balcons en relief, bow-windows
- [ ] Étendre la lecture des façades aux bâtiments sans photo proche (photos HD, angles latéraux)
- [ ] Vrais pins maritimes, oyats, dunes
- [ ] Port d'Arcachon, pinasses à quai

### Jalon C : Lieux iconiques
- [ ] 3 à 5 landmarks : phare du Cap Ferret, cabanes tchanquées, basilique Notre-Dame, observatoire Sainte-Cécile, dune du Pilat (relief)
- [ ] Pipeline image→3D multi-vues + nettoyage Blender (textures avec texte régénérées à part)

### Jalon D : Vie et détails
- [ ] Mobilier urbain placé depuis des données ouvertes (OSM, bases de signalisation si dispo)
- [ ] Détails « Pareto » : sable mouillé, traces dans le sable, sillage des bateaux, reflets au coucher du soleil
- [ ] PNJ : bateaux qui naviguent, passants, vélos (une IA de comportement simple par type)

### Jalon E : Monde continu
- [ ] Assemblage multi-secteurs, World Partition, Nanite, HLOD
- [ ] Streaming autour du joueur (bulle de chargement)
- [ ] Gameplay selon l'option retenue en section 2

## 7. Risques

| Risque | Parade |
|---|---|
| Poids du projet (une ville complète peut dépasser 1 To) | Données brutes et builds hors git, régénérables ; Git LFS pour les assets finaux |
| Agent qui s'emballe sur un détail (temps, coût) | Budget par classe (5.3), arrêt manuel |
| Erreurs LiDAR (trous, faux reliefs) | Passe QA automatique + revue visuelle par secteur |
| Mac en limite sur Nanite et Lumen | Mac pour créer, PC NVIDIA ou GPU loué pour la référence finale |
| Licences (ODbL, Etalab, liberté de panorama) | Attribution à l'écran, marques fictives, pas d'usage commercial des bâtiments récents protégés |
| Moteur maison tentant | Bon pour une petite scène légère ; Unreal garde l'avantage dès que la scène devient énorme |

## 8. Critères de succès

- **Jalon A** : on marche dans le centre d'Arcachon à la bonne échelle, avec un éditeur fluide sur le Mac.
- **Jalon B** : quelqu'un qui connaît Arcachon reconnaît le front de mer sur un rendu, sans légende.
- **Méthode** : un nouveau secteur s'ajoute avec l'app seule, sans toucher au code.

## 9. Décisions ouvertes

1. Objectif final : jeu, démo ou méthode (section 2).
2. Source de la bathymétrie et sa licence.
3. Outil image→3D (Meshy, Tripo, Rodin, Higgsfield, ou GPU loué pour TRELLIS.2).
4. Machine de référence pour l'éclairage final.

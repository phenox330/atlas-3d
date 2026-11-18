# Atlas — PRD

> Document vivant. Dernière mise à jour : 18/11/2026 (deuxième passe « réalisme » sur arcachon-centre-ville : façades en relief, toits en pans, rues habitées, alentours).
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

## 5. État au 18/11/2026

Priorité actuelle : **rendre le secteur `arcachon-centre-ville` réaliste avant d'étendre la zone.**
Rendus avant/après : `build/hq/renders/avant_2026-11-17/` (avant la passe 1), `avant_2026-11-18/` (avant la passe 2), contre `build/hq/renders/` (hors git).

### 5.1 Ce qui marche
- **3 secteurs** : `jetee-thiers` (500 m), `arcachon-centre-ville` (1 km²), `arcachon-ville-d-hiver`.
- **Données** : MNT et MNH LiDAR HD, ortho 20 cm, BD TOPO, OSM (avec passages piétons, feux, stops, panneaux, bornes), Panoramax découpé en 4×4 cellules (2 428 photos au lieu de 500).
- **Toits en pans** (18/11) : les points LiDAR de chaque emprise sont regroupés en pans plans (plan tangent local + orientation, pour ne pas confondre une pente avec une tranche horizontale). Chaque pan devient un polygone aux bords droits, les décrochés entre pans deviennent des murs, et le haut des murs extérieurs suit exactement les pans. Débords de toit, gouttières, descentes d'eau, souches de cheminée, couvertines d'acrotère. 1 546 bâtiments, dont 581 en toit plat.
- **Façades en relief** (18/11, `facade_geo.py`) : les baies sont percées dans les murs (tableaux, vitrage réfléchissant avec voilages, menuiseries), avec appuis, encadrements brique ou peints, volets battants ouverts ou clos, volets roulants, portes, vitrines avec enseigne et store, balcons filants ou individuels, bandeaux d'étage, soubassement. Pas de baies sur les murs mitoyens. Enduits vieillis (coulures, salissures sous l'égout).
- **Commerces réels** (18/11) : rez-de-chaussée commercial là où OSM place un commerce (195 points) ou le long des rues piétonnes ; 868 enseignes à noms fictifs.
- **Couleur réelle des toits** (17/11) : l'orthophoto vue d'avion × le grain des tuiles et ardoises.
- **Façades lues sur photo** (17/11, `facades.py`) : 239 bâtiments décrits par vision sur 250 photos. Pour chacun : couleur d'enduit, étages, volets (battants, roulants, aucun) et leur couleur, chaînages brique, lambrequins, vitrine au RDC. Sans photo, on se rabat sur la BD TOPO (matériaux des murs et du toit, nombre d'étages), puis sur le hasard.
- **Lambrequins** : frise de bois découpé (festons + jours) sous les rives.
- **Rue** : 675 tirets axiaux, 198 passages piétons, 36 lignes d'arrêt, bordures granit, plaques d'égout. Sol refait (18/11) : chaussée en enrobé avec reprises, trottoirs en enrobé rouge (teinte relevée sur les photos), rues piétonnes pavées de façade à façade, cours en béton clair, jardins détectés sur l'orthophoto. L'orthophoto « sol seul » ne bave plus les toits sur les rues.
- **Rues habitées** (18/11) : environ 2 100 voitures génériques (stationnement le long des rues, parkings OSM, quelques véhicules en circulation), jardinières et bancs dans les rues piétonnes, ardoises et 660 tables de terrasse devant les commerces, 180 bateaux au mouillage devant les plages.
- **Alentours** (18/11) : couronne de 3 km en relief (surface LiDAR à 2 m drapée d'orthophoto 0,5 m), contexte 8 km avec orthophoto à 1 m, abords immédiats (600 m) à 1 m, eau teintée par l'orthophoto (chenaux, hauts-fonds), brume de distance à la teinte du ciel d'horizon.
- **Mobilier réel OSM** : bancs, poubelles, terrasses devant les restaurants, lampadaires ; 2 500 arbustes placés depuis la végétation basse du LiDAR.
- **Sable mouillé** sur 1 m au-dessus du niveau de l'eau.
- **QA LiDAR** : `build/hq/qa_lidar.json` liste les emprises sans écho (15 sur le secteur).
- **Rendus** : Blender Cycles (8 plans, dont 3 à hauteur d'homme, rendu colorimétrique AgX, ciel de jour) et Unreal 5.8.4 (Lumen, état du 13/11).

### 5.2 Limites connues
- **Pas encore photoréaliste à hauteur d'homme.** Les vues aériennes passent pour des photos au premier coup d'œil ; les vues de rue restent au niveau d'une image de synthèse soignée. Ce qui manque : des piétons, des voitures fidèles, la vraie composition de chaque façade.
- **Voitures génériques** : gabarits générés par le code, corrects à plus de 20 m et vus d'avion, pas en gros plan (écartées à moins de 20 m de la caméra dans les plans de rue). Des modèles fidèles demandent une bibliothèque d'assets ou de l'image→3D (jalon C).
- **Aucun piéton.**
- **Façades vraisemblables, pas exactes** : la vision donne étages, couleurs, volets et matériaux sur 239 bâtiments ; la position de chaque baie reste une trame régulière.
- **Unreal en retard** : il n'a aucune des nouveautés du 18/11 (baies en relief, toits en pans, voitures, enseignes, nouveau sol). Parité à refaire.
- **Vue 3D web** (18/11) : même scène que les rendus Cycles, exportée de Blender (`web_export.py` → `web/viewer3d.js`) : façades détaillées, matières, voitures, mobilier, arbres, alentours, mêmes points de vue et même ciel. Limites du temps réel : pas de lumière rebondie calculée (remplacée par une carte de part de ciel visible), arbres en panneaux à vues multiples (flous de très près), reflets des vitrages limités au ciel.
- Toits : lucarnes et petits volumes parfois fondus dans le pan voisin ; un arbre au-dessus d'un toit peut encore le fausser (nuage LiDAR classé non branché, le service WFS IGN utilisé ne l'expose pas).
- Couronne de 3 km : relief LiDAR drapé de photo aérienne, convaincant de loin, mou de près.
- Arbres d'alignement : modèles Poly Haven génériques (pas de platane ni de pin maritime fidèle).
- Bathymétrie absente : plan d'eau plat, estran noyé au niveau `water_level_m`.
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
- [ ] **Parité Unreal** : baies en relief, toits en pans, nouveaux matériaux (vitrage, peint, volet), voitures, enseignes, sol
- [ ] **Valider l'échelle** : un Third Person Character qui marche dans `arcachon-centre-ville`

### Jalon B : Le Bassin crédible
- [ ] Bathymétrie (SHOM, licence à vérifier) + estran LiDAR + niveau de marée paramétrable
- [x] Toits en pans plans aux bords droits (LiDAR) + couleur orthophoto + débords, gouttières, cheminées
- [ ] Nuage de points LiDAR HD **classé** pour retirer les arbres au-dessus des toits
- [x] Façades lues sur les photos Panoramax (vision), avec repli BD TOPO
- [x] Façades en relief : baies percées, volets, portes, vitrines, balcons, enseignes (18/11)
- [ ] Façades V3 : position réelle de chaque baie lue sur photo, bow-windows, lucarnes
- [ ] Étendre la lecture des façades aux bâtiments sans photo proche (photos HD, angles latéraux)
- [ ] Vrais pins maritimes, oyats, dunes
- [ ] Port d'Arcachon, pinasses à quai

### Jalon C : Lieux iconiques
- [ ] 3 à 5 landmarks : phare du Cap Ferret, cabanes tchanquées, basilique Notre-Dame, observatoire Sainte-Cécile, dune du Pilat (relief)
- [ ] Pipeline image→3D multi-vues + nettoyage Blender (textures avec texte régénérées à part)

### Jalon D : Vie et détails
- [x] Mobilier urbain et commerces placés depuis OSM ; voitures génériques en stationnement (18/11)
- [ ] Voitures fidèles et piétons (bibliothèque d'assets ou image→3D)
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

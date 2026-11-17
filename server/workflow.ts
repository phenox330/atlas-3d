// Workflow « à la Paccini » adapté au Bassin d'Arcachon puis Bordeaux.
// kind : auto = bouton dans l'app ; claude = l'agent code l'outil ; manuel = toi dans Blender/Unreal.
export type Step = {
  id: string; phase: string; title: string; desc: string;
  kind: "auto" | "claude" | "manuel"; tool: string; prompt?: string;
};

export const WORKFLOW: Step[] = [
  { id: "donnees", phase: "1. Données", kind: "auto", tool: "Atlas",
    title: "Télécharger les données ouvertes",
    desc: "MNT LiDAR HD (IGN), orthophoto 20 cm, bâti BD TOPO (emprises + hauteurs), OSM (routes, végétation, eau), photos de rue Panoramax. Licence Etalab / ODbL : attribution obligatoire." },
  { id: "blockout", phase: "1. Données", kind: "auto", tool: "Atlas",
    title: "Blockout : terrain + bâti extrudé",
    desc: "Terrain texturé par l'ortho, bâtiments extrudés à leur hauteur réelle, routes en polylignes, heightmap Unreal. Vérifier dans la visionneuse 3D." },
  { id: "echelle", phase: "2. Échelle", kind: "manuel", tool: "Unreal",
    title: "Valider l'échelle à hauteur d'homme",
    desc: "Importer le secteur dans Unreal, poser le Third Person Character dans la rue. Les trottoirs, portes et hauteurs doivent sembler justes. On fige l'échelle AVANT d'habiller." },
  { id: "eau", phase: "3. Bassin", kind: "claude", tool: "Blender / Unreal Water",
    title: "Eau, estran et marées",
    desc: "Le Bassin est surtout de l'eau et des vasières. Plan d'eau à 0 m NGF pour l'instant ; ensuite bathymétrie SHOM + estran LiDAR, niveau de marée paramétrable (Unreal Water plugin).",
    prompt: "Dans ce projet Atlas, propose puis implémente dans pipeline/atlas.py le téléchargement d'une bathymétrie ouverte du Bassin d'Arcachon (SHOM / data.shom.fr, vérifie la licence) et fusionne-la avec le MNT LiDAR HD pour obtenir un terrain continu terre + fond marin. Ajoute un paramètre water_level_m dans sector.json. Teste sur le secteur courant." },
  { id: "routes", phase: "4. Rues", kind: "claude", tool: "Blender Geometry Nodes",
    title: "Routes, trottoirs et front de mer",
    desc: "Transformer roads.json en vraies chaussées : largeur selon le type OSM, trottoirs, bordures, promenade du front de mer.",
    prompt: "Écris pipeline/blender_roads.py : un script Blender headless qui lit build/roads.json du secteur, crée des chaussées (largeur selon highway: primary 9 m, secondary 7 m, residential 5,5 m, footway 2 m, pedestrian 4 m), trottoirs surélevés de 15 cm et bordures, posés sur le terrain. Exporte build/roads.glb et ajoute-le à la scène. Teste en headless avec /Applications/Blender.app/Contents/MacOS/Blender -b." },
  { id: "villas", phase: "5. Bâti", kind: "claude", tool: "Blender (procédural)",
    title: "Générateur de villas arcachonnaises",
    desc: "Le cœur du réalisme : remplacer les blocs par des bâtiments générés selon le style local — villas Ville d'Hiver (XIXe, bois découpé, toits pentus, bow-windows), maisons de pêcheurs/ostréiculteurs, immeubles balnéaires du front de mer.",
    prompt: "Écris pipeline/blender_buildings.py : un générateur procédural de façades pour Blender (Python bpy, headless) qui prend chaque emprise BD TOPO de raw/batiments.geojson et produit un bâtiment stylé selon une typologie : 'villa_ville_hiver' (R+1/R+2, toit à forte pente en tuiles, lambrequins bois, balcons), 'maison_ostreicole' (R, bardage bois peint, toit 2 pans), 'immeuble_balneaire' (R+3 à R+6, balcons filants, enduit clair). Choisis la typologie selon hauteur, surface et usage. Fenêtres et portes en vrais trous, matériaux PBR simples. Exporte build/buildings_styled.glb. Commence par 3 bâtiments et montre un rendu." },
  { id: "devantures", phase: "5. Bâti", kind: "claude", tool: "Blender",
    title: "Devantures et rez-de-chaussée commerciaux",
    desc: "Vitrines, stores bannes, enseignes FICTIVES (pas de vraies marques), terrasses de cafés sur le front de mer.",
    prompt: "Ajoute au générateur de bâtiments des rez-de-chaussée commerciaux sur les bâtiments qui bordent les rues commerçantes OSM (shop=*, amenity=cafe/restaurant dans raw/osm.json) : vitrines, stores bannes rayés, enseignes avec des noms fictifs générés. Jamais de vraie marque." },
  { id: "photo3d", phase: "6. Détails", kind: "manuel", tool: "Panoramax + IA photo→3D",
    title: "Props depuis les photos de rue",
    desc: "Choisir dans l'onglet Photos les objets typiques (lampadaires, bancs, pinasses, cabanes, bornes) puis les convertir en 3D. Local sur Mac : pas encore branché (TRELLIS.2 demande un GPU NVIDIA 24 Go) ; en attendant Higgsfield 3D / Meshy, puis nettoyage dans Blender." },
  { id: "port", phase: "7. Lieux", kind: "manuel", tool: "Blender / Unreal",
    title: "Ports, jetées et pontons",
    desc: "Jetée Thiers, jetée d'Eyrac, port d'Arcachon, pontons, pinasses à quai, port ostréicole de Gujan (Larros)." },
  { id: "monuments", phase: "7. Lieux", kind: "manuel", tool: "Blender (modélisation main) + scans",
    title: "Monuments et lieux iconiques",
    desc: "Dune du Pilat, phare du Cap Ferret, cabanes tchanquées de l'île aux Oiseaux, basilique Notre-Dame, observatoire Sainte-Cécile. Faits main ou scannés, pas générés." },
  { id: "vegetation", phase: "8. Nature", kind: "claude", tool: "Unreal PCG + Megascans",
    title: "Pins, dunes et végétation",
    desc: "Forêt de pins maritimes, oyats sur les dunes, jardins des villas. Megascans gratuits sur Fab ; placement depuis OSM (natural=wood, landuse=forest) et l'ortho.",
    prompt: "Exporte depuis raw/osm.json les zones de végétation (natural=wood, landuse=forest/grass, leisure=park, natural=scrub/sand/beach) en polygones locaux build/landcover.json, avec une densité suggérée par type, pour un graphe PCG Unreal. Écris aussi un mode d'emploi pour brancher ce fichier sur un PCG Graph avec des pins maritimes Megascans." },
  { id: "lumiere", phase: "9. Ambiance", kind: "manuel", tool: "Unreal (Lumen, MegaLights)",
    title: "Éclairage : jour → coucher de soleil → nuit",
    desc: "Le coucher de soleil sur le Bassin est LA signature. Sky Atmosphere + Lumen, puis éclairage public la nuit (MegaLights). Référence finale à valider sur un PC NVIDIA." },
  { id: "optim", phase: "10. Jeu", kind: "manuel", tool: "Unreal",
    title: "Assemblage des secteurs et optimisation",
    desc: "World Partition (un secteur = une cellule), Nanite sur le bâti, LOD, streaming. Tous les secteurs partagent le repère Lambert-93 → ils se raccordent." },
  { id: "gameplay", phase: "10. Jeu", kind: "manuel", tool: "Unreal",
    title: "Gameplay : marcher, vélo, bateau",
    desc: "Promenade à pied, vélo sur les pistes cyclables, pinasse sur le Bassin." },
];

// Secteurs prédéfinis — coordonnées approximatives, à ajuster sur la carte.
export const PRESETS: Record<string, { name: string; center: [number, number]; size_m: number }[]> = {
  "bassin-arcachon": [
    { name: "Arcachon — Jetée Thiers", center: [-1.1680, 44.6625], size_m: 500 },
    { name: "Arcachon — Ville d'Hiver", center: [-1.1735, 44.6575], size_m: 600 },
    { name: "Arcachon — Port", center: [-1.1545, 44.6610], size_m: 600 },
    { name: "Le Moulleau", center: [-1.2160, 44.6370], size_m: 500 },
    { name: "Dune du Pilat", center: [-1.2135, 44.5893], size_m: 2000 },
    { name: "Cap Ferret — Phare", center: [-1.2482, 44.6431], size_m: 600 },
    { name: "Île aux Oiseaux — cabanes tchanquées", center: [-1.1650, 44.7015], size_m: 800 },
    { name: "Gujan-Mestras — port de Larros", center: [-1.0820, 44.6400], size_m: 500 },
    { name: "Andernos — jetée", center: [-1.1060, 44.7395], size_m: 500 },
  ],
  "bordeaux": [
    { name: "Bordeaux — Place de la Bourse", center: [-0.5700, 44.8414], size_m: 500 },
    { name: "Bordeaux — Saint-Pierre", center: [-0.5715, 44.8395], size_m: 400 },
    { name: "Bordeaux — Chartrons", center: [-0.5690, 44.8530], size_m: 500 },
  ],
};

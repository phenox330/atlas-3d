# Atlas

Recréer des villes françaises **réelles** en map 3D, à partir de données ouvertes :
d'abord le Bassin d'Arcachon, puis Bordeaux.

IGN LiDAR HD, BD TOPO, ortho, OSM et Panoramax passent par Blender puis par Unreal Engine 5.8.
L'IA écrit les générateurs (bâti, végétation, jetées) et ne modélise pas les bâtiments à la main.
Tout se pilote depuis une app locale.

## Démarrer

Prérequis : [Bun](https://bun.sh), [uv](https://docs.astral.sh/uv/), Blender 5.2. Unreal Engine 5.8 est optionnel (rendu Lumen).

    bun install && (cd pipeline && uv sync)
    bun start            # http://127.0.0.1:4896

Les données brutes, les builds, les assets Poly Haven et les projets Unreal ne sont **pas** versionnés.
Le pipeline les régénère depuis l'app.

## Documentation

| Document | Contenu |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | Vision, état actuel, roadmap, risques, décisions ouvertes |
| [`CLAUDE.md`](CLAUDE.md) | Architecture, conventions, pièges Unreal (lu par les agents IA) |
| [`pipeline/README.md`](pipeline/README.md) | Scripts du pipeline Python et Blender |
| [`docs/recherche/`](docs/recherche/) | État de l'art : villes réelles en 3D, world models, outils IA 3D, Unreal contre Unity |
| [`docs/ressources/`](docs/ressources/) | Retours d'expérience externes et leçons retenues |
| [`server/workflow.ts`](server/workflow.ts) | Étapes détaillées du workflow, avec les prompts pour l'agent |

## Données et licences

- Code : [MIT](LICENSE).
- Données IGN : Licence Ouverte Etalab 2.0, attribution « © IGN » obligatoire.
- OpenStreetMap : ODbL, « © les contributeurs OpenStreetMap ». Les bases dérivées se partagent sous la même licence.
- Panoramax : licence propre à chaque photo, à vérifier.
- Assets Poly Haven : CC0.
- **Interdit** comme source : Google Photorealistic 3D Tiles et Street View (CGU).
- Toutes les enseignes sont des noms fictifs.

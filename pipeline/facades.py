"""Façades fidèles : photos de rue Panoramax → description par vision (Claude) → attributs par bâtiment.

Technique reprise de la vidéo « GTA Marseille » : l'agent regarde les photos de façade et relève
étages, couleurs, volets, matériaux, au lieu de les tirer au hasard. Ici : Panoramax (licence libre),
jamais Street View.

Pour chaque photo (caméra embarquée, regard vers l'avant) on lance des rayons à gauche et à droite
pour trouver le bâtiment visible de chaque côté. On garde, par bâtiment, la photo la plus proche,
puis on plafonne le nombre d'appels (budget). Résultats en cache : relancer ne coûte rien.

Sortie : build/hq/facades.json  {cleabs: {...attributs..., "photo": id, "dist": m}}
         build/hq/street_style.json  médianes globales (trottoir, bordures…)
Usage : uv run facades.py <dossier_secteur> [--max 160] [--workers 4]
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).parent))
from atlas import Sector, get, log, progress, to_l93  # noqa: E402

PROMPT = """Lis l'image {img} : photo de rue prise par une caméra grand angle embarquée (regard vers l'avant), à Arcachon.
Décris la façade du bâtiment le plus proche visible à GAUCHE et celle du plus proche à DROITE (null si aucun bâtiment net de ce côté).
Réponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"left": FACADE|null, "right": FACADE|null,
  "street": {{"sidewalk_color": "#hex", "curb": true|false, "road_center_line": true|false}}}}
FACADE = {{
 "style": "villa"|"maison"|"immeuble"|"commerce"|"autre",
 "floors": entier (niveaux hors combles, RDC compris),
 "wall_color": "#hex (couleur dominante de l'enduit ou du mur, corrigée de l'éclairage)",
 "wall_material": "enduit"|"brique"|"pierre"|"bois"|"beton",
 "brick_trim": true|false (chaînages d'angle et encadrements de fenêtres en brique),
 "trim_color": "#hex"|null,
 "shutter_type": "persiennes"|"battants"|"roulants"|"aucun",
 "shutter_color": "#hex"|null,
 "roof_material": "tuile"|"ardoise"|"zinc"|"plat"|"inconnu",
 "roof_color": "#hex"|null,
 "lambrequins": true|false (frises de bois découpé sous les toits),
 "balcony": true|false,
 "shop_ground_floor": true|false,
 "awning_color": "#hex"|null,
 "confidence": 0..1 }}"""

SIDE_ANGLES = (25, 35, 45, 55, 65)  # degrés de part et d'autre de l'axe de la caméra
MAX_DIST = 32.0


def load_buildings(sec: Sector):
    fc = json.loads((sec.raw / "batiments.geojson").read_text())
    keys, geoms = [], []
    for f in fc["features"]:
        g = shapely.transform(shape(f["geometry"]), lambda c: c - np.array([sec.x0, sec.y0]), include_z=False)
        if g.is_empty or g.area < 12:
            continue
        keys.append(f["properties"].get("cleabs") or str(f.get("id")))
        geoms.append(g)
    return keys, geoms


def first_hit(tree, geoms, origin: Point, ang_deg: float):
    a = math.radians(ang_deg)
    end = (origin.x + math.sin(a) * MAX_DIST, origin.y + math.cos(a) * MAX_DIST)
    ray = LineString([(origin.x, origin.y), end])
    best, bd = None, 1e9
    for i in tree.query(ray):
        g = geoms[i]
        if g.contains(origin):
            continue
        inter = ray.intersection(g.boundary)
        if inter.is_empty:
            continue
        d = origin.distance(inter)
        if d < bd:
            best, bd = int(i), d
    return best, bd


def candidates(sec: Sector, photos, keys, geoms):
    tree = STRtree(geoms)
    out = []
    for p in photos:
        x, y = to_l93.transform(p["lon"], p["lat"])
        o = Point(x - sec.x0, y - sec.y0)
        if not (0 <= o.x <= sec.size and 0 <= o.y <= sec.size):
            continue
        h = float(p.get("heading") or 0)
        sides = {}
        for side, sgn in (("left", -1), ("right", 1)):
            votes = {}
            for da in SIDE_ANGLES:
                i, d = first_hit(tree, geoms, o, h + sgn * da)
                if i is not None:
                    v = votes.setdefault(i, [0, 1e9])
                    v[0] += 1
                    v[1] = min(v[1], d)
            if votes:
                i, (n, d) = max(votes.items(), key=lambda kv: (kv[1][0], -kv[1][1]))
                if n >= 2:
                    sides[side] = (keys[i], d)
        if sides:
            out.append((p, sides))
    return out


def ask(img: Path) -> dict:
    cmd = ["claude", "-p", PROMPT.format(img=img), "--model", "sonnet", "--allowedTools", "Read",
           "--strict-mcp-config", "--output-format", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    res = json.loads(r.stdout).get("result", "")
    s, e = res.find("{"), res.rfind("}")
    return json.loads(res[s:e + 1])


def main(folder: Path, max_calls: int = 160, workers: int = 4):
    sec = Sector(folder)
    (sec.out / "hq").mkdir(parents=True, exist_ok=True)
    cache = sec.raw / "facades"
    imgs = sec.raw / "panoramax_img"
    cache.mkdir(exist_ok=True)
    imgs.mkdir(exist_ok=True)
    photos = json.loads((sec.raw / "panoramax.json").read_text())
    keys, geoms = load_buildings(sec)
    progress(0.05, "rayons photo → façades")
    cands = candidates(sec, photos, keys, geoms)
    # pour chaque bâtiment : la photo la plus proche
    best = {}
    for p, sides in cands:
        for side, (k, d) in sides.items():
            if k not in best or d < best[k][2]:
                best[k] = (p, side, d)
    # photos à interroger : celles qui couvrent le plus de bâtiments, les plus proches d'abord
    need = {}
    for k, (p, side, d) in best.items():
        need.setdefault(p["id"], [p, 0, 1e9])
        need[p["id"]][1] += 1
        need[p["id"]][2] = min(need[p["id"]][2], d)
    order = sorted(need.values(), key=lambda v: (-v[1], v[2]))
    todo = [v[0] for v in order[:max_calls]]
    log(f"Façades : {len(best)} bâtiments vus sur {len(cands)} photos ; {len(todo)} photos retenues (plafond {max_calls})")

    def job(p):
        cf = cache / f"{p['id']}.json"
        if cf.exists():
            return p["id"], json.loads(cf.read_text())
        jp = imgs / f"{p['id']}.jpg"
        if not jp.exists():
            jp.write_bytes(get(p["sd"]).content)
        d = ask(jp.resolve())
        cf.write_text(json.dumps(d, ensure_ascii=False))
        return p["id"], d

    answers, done = {}, 0
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(job, p) for p in todo]
        for fu in as_completed(futs):
            done += 1
            try:
                pid, d = fu.result()
                answers[pid] = d
            except Exception as e:  # une photo ratée ne bloque pas le reste
                log(f"  photo ignorée : {e}")
            progress(0.1 + 0.85 * done / max(1, len(todo)), f"vision {done}/{len(todo)}")
    out, street = {}, []
    for k, (p, side, d) in best.items():
        a = answers.get(p["id"])
        if not a:
            continue
        if a.get("street"):
            street.append(a["street"])
        f = a.get(side)
        if f and float(f.get("confidence", 0)) >= 0.45:
            out[k] = {**f, "photo": p["id"], "side": side, "dist": round(d, 1)}
    (sec.out / "hq" / "facades.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    cols = [s.get("sidewalk_color") for s in street if s.get("sidewalk_color")]
    style = {"sidewalk_colors": cols, "curb_ratio": float(np.mean([bool(s.get("curb")) for s in street])) if street else 1.0,
             "center_line_ratio": float(np.mean([bool(s.get("road_center_line")) for s in street])) if street else 0.5}
    (sec.out / "hq" / "street_style.json").write_text(json.dumps(style, ensure_ascii=False, indent=1))
    log(f"Façades décrites : {len(out)} bâtiments ({len(answers)} photos analysées)")
    progress(1, "terminé")


if __name__ == "__main__":
    args = sys.argv[1:]
    mx = int(args[args.index("--max") + 1]) if "--max" in args else 160
    wk = int(args[args.index("--workers") + 1]) if "--workers" in args else 4
    main(Path(args[0]), mx, wk)

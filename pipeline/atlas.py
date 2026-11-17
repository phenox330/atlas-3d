"""Atlas pipeline — données ouvertes IGN/OSM/Panoramax -> secteur 3D (GLB + heightmap Unreal).

Usage : uv run atlas.py <commande> <dossier_secteur>
  fetch   : télécharge MNT LiDAR HD, ortho, bâti BD TOPO, OSM, Panoramax
  build   : génère terrain.glb, buildings.glb, sector.glb, heightmap Unreal
  all     : fetch puis build
  options : --force (retélécharge), --only=<source> (ex. --only=osm)

Le secteur est décrit par <dossier>/sector.json :
  {"name": "...", "center": [lon, lat], "size_m": 500, "mnt_res_m": 1.0, "ortho_res_m": 0.25}
Repère de travail : Lambert-93 (EPSG:2154), origine au coin sud-ouest du secteur.
Sorties en mètres, Y vers le haut (glTF). Unreal attend des cm : le script d'import gère x100.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import requests
import trimesh
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Polygon, MultiPolygon, shape
import shapely

UA = {"User-Agent": "atlas-3d/0.1 (local game-map pipeline)"}
WMS = "https://data.geopf.fr/wms-r/wms"
WFS = "https://data.geopf.fr/wfs/ows"
OVERPASS = ["https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
PANORAMAX = "https://api.panoramax.xyz/api/search"
MNT_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
MNT_FALLBACK = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"  # RGE ALTI 1 m si LiDAR HD absent
ORTHO_LAYER = "HR.ORTHOIMAGERY.ORTHOPHOTOS"
WMS_MAX = 4096  # px max par requête, on tuile au-delà

to_l93 = Transformer.from_crs(4326, 2154, always_xy=True)
to_wgs = Transformer.from_crs(2154, 4326, always_xy=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def progress(p: float, step: str) -> None:
    print(f"@@PROGRESS {p:.2f} {step}", flush=True)


# ---------------------------------------------------------------- secteur
class Sector:
    def __init__(self, folder: Path):
        self.dir = folder
        self.cfg = json.loads((folder / "sector.json").read_text())
        lon, lat = self.cfg["center"]
        cx, cy = to_l93.transform(lon, lat)
        s = float(self.cfg.get("size_m", 500))
        self.size = s
        self.x0, self.y0 = cx - s / 2, cy - s / 2
        self.x1, self.y1 = cx + s / 2, cy + s / 2
        self.raw = folder / "raw"
        self.out = folder / "build"
        self.raw.mkdir(exist_ok=True)
        self.out.mkdir(exist_ok=True)

    @property
    def bbox_l93(self):
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def bbox_wgs(self):
        lo0, la0 = to_wgs.transform(self.x0, self.y0)
        lo1, la1 = to_wgs.transform(self.x1, self.y1)
        return (min(lo0, lo1), min(la0, la1), max(lo0, lo1), max(la0, la1))


def get(url, params=None, timeout=120, retries=3, **kw):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            log(f"  HTTP {r.status_code} sur {url} (essai {i + 1})")
        except requests.RequestException as e:
            log(f"  erreur réseau {e} (essai {i + 1})")
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"échec téléchargement {url}")


# ---------------------------------------------------------------- fetch
def wms_tiles(sec: Sector, layer: str, res: float, fmt: str):
    """Découpe le bbox en tuiles WMS <= WMS_MAX px. Renvoie liste (ix, iy, bbox, w, h)."""
    npx = int(round(sec.size / res))
    n = math.ceil(npx / WMS_MAX)
    step = sec.size / n
    tiles = []
    for iy in range(n):
        for ix in range(n):
            bx0 = sec.x0 + ix * step
            by0 = sec.y0 + iy * step
            w = h = int(round(step / res))
            tiles.append((ix, iy, (bx0, by0, bx0 + step, by0 + step), w, h))
    return n, tiles


def wms_params(layer, bbox, w, h, fmt):
    return {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": layer,
        "STYLES": "", "CRS": "EPSG:2154", "BBOX": ",".join(f"{v:.2f}" for v in bbox),
        "WIDTH": w, "HEIGHT": h, "FORMAT": fmt,
    }


def fetch_mnt(sec: Sector):
    res = float(sec.cfg.get("mnt_res_m", 1.0))
    n, tiles = wms_tiles(sec, MNT_LAYER, res, "bil")
    tw = tiles[0][3]
    full = np.zeros((n * tw, n * tw), dtype=np.float32)
    source = "LiDAR HD"
    for ix, iy, bbox, w, h in tiles:
        a = None
        for layer in (MNT_LAYER, MNT_FALLBACK):
            r = get(WMS, wms_params(layer, bbox, w, h, "image/x-bil;bits=32"))
            if "bil" not in r.headers.get("content-type", ""):
                continue
            a = np.frombuffer(r.content, dtype="<f4").reshape(h, w).copy()
            if (a < -1000).mean() < 0.5:
                if layer == MNT_FALLBACK:
                    source = "RGE ALTI (repli)"
                break
        if a is None:
            raise RuntimeError("MNT indisponible")
        # ligne 0 = nord : on range en partant du nord
        row = (n - 1 - iy) * tw
        full[row:row + h, ix * tw:ix * tw + w] = a
    nodata = full < -1000
    if nodata.any():
        # mer / trous : valeur légèrement sous 0 (estran et eau gérés au build)
        full[nodata] = -2.0
    np.save(sec.raw / "mnt.npy", full)
    log(f"  MNT {full.shape[1]}x{full.shape[0]} px à {res} m — {source} — z {full.min():.1f}..{full.max():.1f} m")
    return source


def fetch_ortho(sec: Sector):
    res = float(sec.cfg.get("ortho_res_m", 0.25))
    n, tiles = wms_tiles(sec, ORTHO_LAYER, res, "jpeg")
    tw = tiles[0][3]
    img = Image.new("RGB", (n * tw, n * tw))
    for ix, iy, bbox, w, h in tiles:
        r = get(WMS, wms_params(ORTHO_LAYER, bbox, w, h, "image/jpeg"))
        from io import BytesIO
        t = Image.open(BytesIO(r.content)).convert("RGB")
        img.paste(t, (ix * tw, (n - 1 - iy) * tw))
    # limite texture moteur : 8192
    if img.width > 8192:
        img = img.resize((8192, 8192), Image.LANCZOS)
    img.save(sec.raw / "ortho.jpg", quality=92)
    log(f"  Ortho {img.width}x{img.height} px")


def fetch_buildings(sec: Sector):
    feats, start = [], 0
    while True:
        r = get(WFS, {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": "BDTOPO_V3:batiment", "SRSNAME": "EPSG:2154",
            "BBOX": ",".join(f"{v:.2f}" for v in sec.bbox_l93) + ",EPSG:2154",
            "OUTPUTFORMAT": "application/json", "COUNT": 5000, "STARTINDEX": start,
        })
        d = r.json()
        feats += d["features"]
        if len(d["features"]) < 5000:
            break
        start += 5000
    (sec.raw / "batiments.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    log(f"  BD TOPO : {len(feats)} bâtiments")
    return f"{len(feats)} bâtiments"


def fetch_osm(sec: Sector):
    lo0, la0, lo1, la1 = sec.bbox_wgs
    b = f"{la0},{lo0},{la1},{lo1}"
    q = f"""[out:json][timeout:120];(
      way["highway"]({b}); way["natural"]({b}); way["landuse"]({b}); way["leisure"]({b});
      way["waterway"]({b}); way["man_made"]({b}); way["railway"]({b}); way["amenity"]({b});
      node["natural"="tree"]({b}); node["amenity"]({b}); node["shop"]({b});
      node["highway"]({b}); node["traffic_sign"]({b}); node["barrier"]({b}); node["man_made"]({b});
      node["leisure"]({b}); node["tourism"]({b});
      relation["natural"="water"]({b});
    );out body geom;"""
    last = None
    for url in OVERPASS:
        try:
            log(f"  Overpass {url.split('/')[2]}…")
            r = requests.post(url, data={"data": q}, headers=UA, timeout=90)
            if r.status_code == 200:
                (sec.raw / "osm.json").write_text(r.text)
                n = len(r.json()['elements'])
                log(f"  OSM : {n} éléments")
                return f"{n} éléments"
            last = r.status_code
        except requests.RequestException as e:
            last = e
    raise RuntimeError(f"Overpass indisponible ({last})")


def fetch_panoramax(sec: Sector, grid: int = 4, per_cell: int = 500):
    """Photos de rue Panoramax. L'API plafonne une recherche à ~500 résultats : on découpe la
    zone en grid × grid cellules pour couvrir toutes les rues, puis on dédoublonne."""
    lo0, la0, lo1, la1 = sec.bbox_wgs
    seen, items = set(), []
    for iy in range(grid):
        for ix in range(grid):
            a0, a1 = lo0 + (lo1 - lo0) * ix / grid, lo0 + (lo1 - lo0) * (ix + 1) / grid
            b0, b1 = la0 + (la1 - la0) * iy / grid, la0 + (la1 - la0) * (iy + 1) / grid
            try:
                d = get(PANORAMAX, {"bbox": f"{a0},{b0},{a1},{b1}", "limit": per_cell}).json()
            except Exception as e:
                log(f"  Panoramax cellule {ix},{iy} ignorée : {e}")
                continue
            for f in d.get("features", []):
                if f["id"] in seen:
                    continue
                seen.add(f["id"])
                a = f.get("assets", {})
                items.append({
                    "id": f["id"], "lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1],
                    "date": f["properties"].get("datetime"),
                    "heading": f["properties"].get("view:azimuth"),
                    "thumb": a.get("thumb", {}).get("href"), "sd": a.get("sd", {}).get("href"),
                    "hd": a.get("hd", {}).get("href"),
                })
    (sec.raw / "panoramax.json").write_text(json.dumps(items))
    log(f"  Panoramax : {len(items)} photos de rue")
    return f"{len(items)} photos"


RAW_FILES = {"MNT LiDAR HD": "mnt.npy", "Orthophoto": "ortho.jpg", "Bâti BD TOPO": "batiments.geojson",
             "OpenStreetMap": "osm.json", "Panoramax": "panoramax.json"}


def cmd_fetch(sec: Sector, force: bool = False, only: str | None = None):
    mp = sec.raw / "meta.json"
    meta = json.loads(mp.read_text()) if mp.exists() else {}
    steps = [("MNT LiDAR HD", fetch_mnt), ("Orthophoto", fetch_ortho), ("Bâti BD TOPO", fetch_buildings),
             ("OpenStreetMap", fetch_osm), ("Panoramax", fetch_panoramax)]
    for i, (name, fn) in enumerate(steps):
        progress(i / len(steps), name)
        if only and only.lower() not in name.lower():
            continue
        if not force and (sec.raw / RAW_FILES[name]).exists():
            log(f"{name} : déjà en cache (force pour retélécharger)")
            continue
        log(f"Téléchargement {name}…")
        try:
            r = fn(sec)
            if name.startswith("MNT"):
                meta["mnt_source"] = r
            meta[name] = r if isinstance(r, str) else "ok"
        except Exception as e:  # une source KO ne bloque pas les autres
            log(f"  ÉCHEC {name} : {e}")
            meta[name] = f"échec : {e}"
    meta["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["bbox_l93"] = sec.bbox_l93
    (sec.raw / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    progress(1, "terminé")


# ---------------------------------------------------------------- build
def l93_to_local(x, y, sec: Sector):
    return x - sec.x0, y - sec.y0


def sample_height(mnt: np.ndarray, sec: Sector, x: float, y: float) -> float:
    """x,y locaux (m) -> altitude bilinéaire."""
    h, w = mnt.shape
    fx = np.clip(x / sec.size * (w - 1), 0, w - 1)
    fy = np.clip((1 - y / sec.size) * (h - 1), 0, h - 1)
    x0, y0 = int(fx), int(fy)
    x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
    tx, ty = fx - x0, fy - y0
    a = mnt[y0, x0] * (1 - tx) + mnt[y0, x1] * tx
    b = mnt[y1, x0] * (1 - tx) + mnt[y1, x1] * tx
    return float(a * (1 - ty) + b * ty)


def build_terrain(sec: Sector, mnt: np.ndarray, max_verts=512):
    """Maillage terrain décimé (grille) avec UV pour l'ortho. glTF : Y haut, -Z = nord."""
    step = max(1, math.ceil(max(mnt.shape) / max_verts))
    g = mnt[::step, ::step]
    rows, cols = g.shape
    xs = np.linspace(0, sec.size, cols)
    ys = np.linspace(sec.size, 0, rows)  # ligne 0 = nord
    X, Y = np.meshgrid(xs, ys)
    verts = np.column_stack([X.ravel(), g.ravel(), -Y.ravel()])
    u = X.ravel() / sec.size
    v = Y.ravel() / sec.size
    idx = np.arange(rows * cols).reshape(rows, cols)
    a, b, c, d = idx[:-1, :-1], idx[:-1, 1:], idx[1:, :-1], idx[1:, 1:]
    faces = np.vstack([np.column_stack([a.ravel(), c.ravel(), b.ravel()]),
                       np.column_stack([b.ravel(), c.ravel(), d.ravel()])])
    mesh = trimesh.Trimesh(verts, faces, process=False)
    ortho = sec.raw / "ortho.jpg"
    if ortho.exists():
        img = Image.open(ortho)
        if img.width > 4096:
            img = img.resize((4096, 4096), Image.LANCZOS)
        mat = trimesh.visual.material.PBRMaterial(baseColorTexture=img, roughnessFactor=0.95, metallicFactor=0)
        mesh.visual = trimesh.visual.TextureVisuals(uv=np.column_stack([u, v]), material=mat)
    return mesh


ROOF_COLORS = {"Résidentiel": (178, 92, 64), "Commercial et services": (120, 120, 125),
               "Industriel": (110, 115, 120), "Religieux": (95, 95, 110)}


def extrude(poly: Polygon, base: float, height: float):
    m = trimesh.creation.extrude_polygon(poly, height)
    m.apply_translation([0, 0, base])
    # repère local (x, y, z) -> glTF (x, z, -y)
    m.vertices = np.column_stack([m.vertices[:, 0], m.vertices[:, 2], -m.vertices[:, 1]])
    return m


def build_buildings(sec: Sector, mnt: np.ndarray):
    p = sec.raw / "batiments.geojson"
    if not p.exists():
        return None, {}
    fc = json.loads(p.read_text())
    meshes, stats = [], {"total": 0, "sans_hauteur": 0, "ignores": 0}
    clip = Polygon([(0, 0), (sec.size, 0), (sec.size, sec.size), (0, sec.size)])
    for f in fc["features"]:
        stats["total"] += 1
        pr = f["properties"]
        try:
            geom = shape(f["geometry"])
            geom = shapely.transform(geom, lambda c: c - np.array([sec.x0, sec.y0]))
            geom = geom.intersection(clip)
            if geom.is_empty:
                stats["ignores"] += 1
                continue
            polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
            h = pr.get("hauteur")
            if not h:
                stats["sans_hauteur"] += 1
                etages = pr.get("nombre_d_etages")
                h = etages * 3.0 if etages else 6.0
            for poly in polys:
                if not isinstance(poly, Polygon) or poly.area < 4:
                    continue
                poly = poly.buffer(0)
                c = poly.representative_point()
                ground = sample_height(mnt, sec, c.x, c.y)
                base = ground - 0.5  # léger enfouissement pour éviter les jours
                m = extrude(poly, base, float(h) + 0.5)
                col = ROOF_COLORS.get(pr.get("usage_1"), (205, 190, 165))
                if pr.get("construction_legere"):
                    col = (150, 140, 120)
                m.visual.face_colors = [*col, 255]
                m.metadata["cleabs"] = pr.get("cleabs")
                meshes.append(m)
        except Exception:
            stats["ignores"] += 1
    if not meshes:
        return None, stats
    return trimesh.util.concatenate(meshes), stats


def export_heightmap(sec: Sector, mnt: np.ndarray):
    """Heightmap 16 bits pour Unreal Landscape (taille recommandée la plus proche)."""
    sizes = [127, 253, 505, 1009, 2017, 4033, 8129]
    target = min(sizes, key=lambda s: abs(s - mnt.shape[0]))
    img = Image.fromarray(mnt).resize((target, target), Image.BILINEAR)
    a = np.asarray(img, dtype=np.float32)
    zmin, zmax = float(a.min()), float(a.max())
    span = max(zmax - zmin, 1.0)
    u16 = ((a - zmin) / span * 65535).astype(np.uint16)
    Image.fromarray(u16).save(sec.out / "heightmap_unreal.png")
    # Unreal : Z scale (%) = span_m * 100 / 512 ; XY scale (cm) = size_m*100/(target-1)
    info = {"resolution": target, "zmin_m": zmin, "zmax_m": zmax, "span_m": span,
            "unreal_scale_xy_cm": sec.size * 100 / (target - 1),
            "unreal_scale_z_pct": span * 100 / 512, "unreal_location_z_cm": (zmin + span / 2) * 100}
    return info


def build_roads(sec: Sector, mnt: np.ndarray):
    """Exporte les routes OSM en polylignes locales (pour splines Unreal / Blender)."""
    p = sec.raw / "osm.json"
    if not p.exists():
        return 0
    els = json.loads(p.read_text())["elements"]
    roads = []
    for e in els:
        tags = e.get("tags", {})
        if e["type"] != "way" or "highway" not in tags or "geometry" not in e:
            continue
        pts = []
        for g in e["geometry"]:
            x, y = to_l93.transform(g["lon"], g["lat"])
            lx, ly = l93_to_local(x, y, sec)
            pts.append([round(lx, 2), round(ly, 2), round(sample_height(mnt, sec, lx, ly), 2)])
        roads.append({"id": e["id"], "type": tags["highway"], "name": tags.get("name"),
                      "lanes": tags.get("lanes"), "surface": tags.get("surface"), "points": pts})
    (sec.out / "roads.json").write_text(json.dumps(roads, ensure_ascii=False))
    return len(roads)


def build_water(sec: Sector, level: float):
    """Plan d'eau au niveau moyen (≈ 0 m NGF). La bathymétrie SHOM viendra remplacer ça."""
    s = sec.size
    v = np.array([[0, level, 0], [s, level, 0], [s, level, -s], [0, level, -s]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    m = trimesh.Trimesh(v, f, process=False)
    mat = trimesh.visual.material.PBRMaterial(baseColorFactor=[40, 90, 110, 200], roughnessFactor=0.1,
                                              metallicFactor=0, alphaMode="BLEND")
    m.visual = trimesh.visual.TextureVisuals(material=mat)
    return m


def cmd_build(sec: Sector):
    mntp = sec.raw / "mnt.npy"
    if not mntp.exists():
        raise SystemExit("Pas de MNT : lance d'abord fetch")
    mnt = np.load(mntp)
    progress(0.05, "terrain")
    log("Terrain…")
    terrain = build_terrain(sec, mnt)
    terrain.export(sec.out / "terrain.glb")
    progress(0.35, "bâtiments")
    log("Bâtiments (extrusion BD TOPO)…")
    bld, stats = build_buildings(sec, mnt)
    if bld is not None:
        bld.export(sec.out / "buildings.glb")
    log(f"  {stats}")
    progress(0.7, "routes")
    n_roads = build_roads(sec, mnt)
    log(f"  {n_roads} routes OSM exportées")
    progress(0.8, "heightmap Unreal")
    hm = export_heightmap(sec, mnt)
    progress(0.9, "scène")
    scene = trimesh.Scene()
    scene.add_geometry(terrain, node_name="terrain")
    if bld is not None:
        scene.add_geometry(bld, node_name="buildings")
    water_level = float(sec.cfg.get("water_level_m", 0.0))
    if mnt.min() < water_level + 1:
        water = build_water(sec, water_level)
        water.export(sec.out / "water.glb")
        scene.add_geometry(water, node_name="water")
    scene.export(sec.out / "sector.glb")
    manifest = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "size_m": sec.size,
                "origin_l93": [sec.x0, sec.y0], "buildings": stats, "roads": n_roads,
                "heightmap": hm, "water_level_m": float(sec.cfg.get("water_level_m", 0.0)), "files": sorted(p.name for p in sec.out.iterdir())}
    (sec.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    log(f"Secteur prêt : {sec.out / 'sector.glb'}")
    progress(1, "terminé")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd, folder = sys.argv[1], Path(sys.argv[2])
    force = "--force" in sys.argv
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    s = Sector(folder)
    if cmd in ("fetch", "all"):
        cmd_fetch(s, force=force, only=only)
    if cmd in ("build", "all"):
        cmd_build(s)

"""Téléchargement et cache des assets Poly Haven (CC0 : usage commercial libre, sans attribution).
Usage : uv run polyhaven.py  (télécharge le kit Arcachon dans pipeline/assets/)
"""
import json
import sys
from pathlib import Path

import requests

UA = {"User-Agent": "atlas-3d/0.1"}
ASSETS = Path(__file__).parent / "assets"

KIT = {
    "hdris": ["belfast_sunset_puresky", "kloppenheim_06_puresky", "venice_sunset"],
    "textures": ["clay_roof_tiles_02", "roof_slates_02", "painted_plaster_wall", "grey_plaster", "brick_4",
                 "asphalt_02", "coast_sand_01", "leafy_grass", "patterned_paving", "gravel_concrete"],
    "models": ["pine_tree_01", "island_tree_01", "street_lamp_01", "painted_wooden_bench"],
}
TEX_MAPS = {"diff": ("Diffuse", "diff"), "nor": ("nor_gl", "nor_gl"), "rough": ("Rough", "rough"), "disp": ("Displacement", "disp")}


def dl(url: str, dest: Path):
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, headers=UA, timeout=300)
    r.raise_for_status()
    dest.write_bytes(r.content)
    print(f"  ↓ {dest.relative_to(ASSETS)} ({len(r.content) // 1024} Ko)", flush=True)


def files(asset_id):
    return requests.get(f"https://api.polyhaven.com/files/{asset_id}", headers=UA, timeout=60).json()


def get_hdri(i, res="4k"):
    f = files(i)["hdri"][res]["hdr"]
    dl(f["url"], ASSETS / "hdri" / f"{i}_{res}.hdr")


def get_texture(i, res="2k"):
    f = files(i)
    for short, (key, _) in TEX_MAPS.items():
        if key not in f:
            continue
        node = f[key][res]
        fmt = "jpg" if "jpg" in node else "png"
        dl(node[fmt]["url"], ASSETS / "textures" / i / f"{short}.{fmt}")


def get_model(i, res="2k"):
    g = files(i)["gltf"][res]["gltf"]
    base = ASSETS / "models" / i
    dl(g["url"], base / f"{i}.gltf")
    for rel, inc in g.get("include", {}).items():
        dl(inc["url"], base / rel)


if __name__ == "__main__":
    for h in KIT["hdris"]:
        get_hdri(h)
    for t in KIT["textures"]:
        get_texture(t)
    for m in KIT["models"]:
        get_model(m)
    manifest = {k: v for k, v in KIT.items()}
    (ASSETS / "kit.json").write_text(json.dumps(manifest, indent=2))
    print("ATLAS_ASSETS_OK", ASSETS)

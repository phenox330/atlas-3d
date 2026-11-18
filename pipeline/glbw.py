"""Écriture minimale de fichiers glb (numpy + json + struct, sans dépendance : utilisable dans le Python de Blender).
Un fichier = une liste de maillages nommés, chacun avec ses attributs de sommet et un nom de matériau.
La vue 3D web (web/viewer3d.js) attribue les matériaux d'après ce nom."""
import json, struct
import numpy as np

_CT = {np.dtype("float32"): 5126, np.dtype("uint32"): 5125, np.dtype("uint16"): 5123, np.dtype("uint8"): 5121}
_TYPE = {1: "SCALAR", 2: "VEC2", 3: "VEC3", 4: "VEC4"}


def to_gl(a):
    """Repère Atlas (x est, y nord, z haut) → glTF (x, z, -y)."""
    a = np.asarray(a, np.float32)
    return np.column_stack([a[:, 0], a[:, 2], -a[:, 1]])


def dedupe(attrs, idx=None, digits=3):
    """Fusionne les sommets identiques (position au mm + tous les attributs). attrs : dict nom → tableau (n, k)."""
    keys = list(attrs)
    cols = []
    for k in keys:
        a = np.asarray(attrs[k])
        a = a.reshape(len(a), -1)
        cols.append(np.round(a.astype(np.float64) * (10 ** digits)).astype(np.int32) if a.dtype.kind == "f" else a.astype(np.int32))
    key = np.ascontiguousarray(np.hstack(cols))
    view = key.view(np.dtype((np.void, key.dtype.itemsize * key.shape[1]))).ravel()
    _, first, inv = np.unique(view, return_index=True, return_inverse=True)
    out = {k: np.asarray(attrs[k])[first] for k in keys}
    inv = inv.astype(np.uint32)
    return out, (inv if idx is None else inv[np.asarray(idx).ravel()])


def write(path, meshes):
    """meshes : liste de dict {name, material, attrs: {POSITION, NORMAL?, TEXCOORD_0?, COLOR_0?, _XXX?}, indices?, extras?}.
    Les uint8/uint16 sont écrits normalisés (couleurs)."""
    blob = bytearray()
    views, accs, gl_meshes, nodes, mats, mat_ix = [], [], [], [], [], {}

    def add(arr, target=None, normalized=False, minmax=False):
        arr = np.ascontiguousarray(arr)
        while len(blob) % 4:
            blob.append(0)
        off = len(blob)
        blob.extend(arr.tobytes())
        v = {"buffer": 0, "byteOffset": off, "byteLength": arr.nbytes}
        if target:
            v["target"] = target
        views.append(v)
        comps = 1 if arr.ndim == 1 else arr.shape[1]
        a = {"bufferView": len(views) - 1, "componentType": _CT[arr.dtype], "count": len(arr), "type": _TYPE[comps]}
        if normalized:
            a["normalized"] = True
        if minmax:
            a["min"] = [float(x) for x in arr.min(0)]
            a["max"] = [float(x) for x in arr.max(0)]
        accs.append(a)
        return len(accs) - 1

    for m in meshes:
        at = {}
        for k, arr in m["attrs"].items():
            arr = np.asarray(arr)
            if arr.dtype.kind == "f":
                arr = arr.astype(np.float32)
            if len(arr) == 0:
                break
            at[k] = add(arr, 34962, normalized=arr.dtype.kind == "u", minmax=k == "POSITION")
        if "POSITION" not in at:
            continue
        prim = {"attributes": at, "mode": 4}
        if m.get("indices") is not None:
            prim["indices"] = add(np.asarray(m["indices"], np.uint32).ravel(), 34963)
        mn = m.get("material")
        if mn:
            if mn not in mat_ix:
                mat_ix[mn] = len(mats)
                mats.append({"name": mn, **(m.get("pbr") or {})})
            prim["material"] = mat_ix[mn]
        gl_meshes.append({"name": m["name"], "primitives": [prim]})
        node = {"name": m["name"], "mesh": len(gl_meshes) - 1}
        if m.get("extras"):
            node["extras"] = m["extras"]
        nodes.append(node)
    doc = {"asset": {"version": "2.0", "generator": "atlas"}, "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
           "nodes": nodes, "meshes": gl_meshes, "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(blob)}]}
    if mats:
        doc["materials"] = mats
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    while len(blob) % 4:
        blob.append(0)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(blob)))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A)); fh.write(js)
        fh.write(struct.pack("<II", len(blob), 0x004E4942)); fh.write(bytes(blob))
    return len(blob)

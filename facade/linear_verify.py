"""linear_verify: read-back and photo check of the I3 linear elements (ADR-001 D6, increment I3).

(a) parameters - beam ends / level, column origin / bottom / height read back -> s / v of the facade
(b) 3D         - bodies of the beam / column SEGMENTS (Get3DGeometryOfElements answers nothing for the beam or
                 column itself, the geometry belongs to its segments) projected on the facade plane: s0, s1, v0, v1 and
                 the projection w (= profile)
(c) profile    - cross-section area of the FS profile read back from GetProfiles = polygon of the spec (+-1 %)
(d) photo      - every `photo_edges` entry: the corner of the profile (e.g. bottom front edge of the cornice) projected
                 with the CAMERA of the rectified photo (an out-of-plane edge is not where an orthographic projection puts
                 it) vs the edge found in the photo (strongest brightness step of the expected sign near the prediction)

Pure helpers + one entry point used by verify.py.
"""
from __future__ import annotations

import math

import numpy as np

from geom import outside_of
from spec_validate import _joined, polygon_area
from tapir_client import TapirClient

EDGE_SEARCH = 0.06        # m - photo edge searched within +-6 cm of the projected model edge


def geometry_of(ac: TapirClient, guids: list[str]) -> list[dict]:
    """Get3DGeometryOfElements, with beams / columns expanded to their segments (one merged body list per element)."""
    if not guids:
        return []
    sub = ac.tapir("GetSubelementsOfHierarchicalElements", {"elements": [{"elementId": {"guid": g}} for g in guids]})["subelements"]
    groups = []
    for g, sb in zip(guids, sub):
        els = [e for v in (sb or {}).values() if isinstance(v, list) for e in v] if isinstance(sb, dict) else []
        groups.append(els or [{"elementId": {"guid": g}}])
    flat = [e for grp in groups for e in grp]
    geos = ac.tapir("Get3DGeometryOfElements", {"elements": flat}, timeout=120)["geometries3D"]
    out, k = [], 0
    for grp in groups:
        bodies = []
        for _ in grp:
            bodies += geos[k].get("bodies", [])
            k += 1
        out.append({"bodies": bodies})
    return out


def camera_of(spec: dict) -> dict | None:
    src = next((s for s in spec["sources"] if s.get("rect")), None)
    return (src or {}).get("rect", {}).get("camera")


def to_photo(svw: np.ndarray, cam: dict | None) -> np.ndarray:
    """(N, 3) facade s, v, w -> (N, 2) position in the rectified photo plane (m). Without a camera: orthographic."""
    if not cam:
        return svw[:, :2].copy()
    t = cam["D"] / (cam["D"] - svw[:, 2])
    s = svw[:, 0] if cam.get("s") is None else cam["s"] + (svw[:, 0] - cam["s"]) * t    # camera s unknown: s kept (ODHAD)
    return np.column_stack([s, cam["v"] + (svw[:, 1] - cam["v"]) * t])


def corner_v(bodies_svw, corner: str, s_win, cam) -> float | None:
    """Projected v' of a profile corner of the model (bottom/top x front/back) inside the s window."""
    pts = np.vstack([b for b, _ in bodies_svw]) if bodies_svw else np.zeros((0, 3))
    pts = pts[(pts[:, 0] >= s_win[0] - 1e-6) & (pts[:, 0] <= s_win[1] + 1e-6)] if len(pts) else pts
    if not len(pts):
        # the window lies inside a long beam that has vertices only at its ends: use all vertices, s does not matter
        pts = np.vstack([b for b, _ in bodies_svw])
    vert, side = corner.split("_")
    vv = pts[:, 1].min() if vert == "bottom" else pts[:, 1].max()
    sel = pts[np.abs(pts[:, 1] - vv) < 1e-3]
    ww = sel[:, 2].max() if side == "front" else sel[:, 2].min()
    p = np.array([[float(np.mean(s_win)), vv, ww]])
    return float(to_photo(p, cam)[0, 1])


def photo_edge(lum: np.ndarray, rect: dict, s_win, v_pred: float, brighter: str, search: float = EDGE_SEARCH) -> tuple[float, float]:
    """v of the strongest brightness step of the expected sign near v_pred, mean over the s window; (v, contrast)."""
    S, u0, v1 = rect["S"], rect["u0"], rect["v1"]
    x0, x1 = int(round((s_win[0] - u0) * S)), int(round((s_win[1] - u0) * S))
    y0, y1 = int(round((v1 - (v_pred + search)) * S)), int(round((v1 - (v_pred - search)) * S))
    y0, y1 = max(1, y0), min(lum.shape[0] - 2, y1)
    prof = lum[y0 - 1:y1 + 2, x0:x1].mean(axis=1)              # rows top -> bottom
    # brightness step at the boundary between row i and i+1 (row i above): above - below
    steps = prof[:-1] - prof[1:]
    if brighter == "below":
        steps = -steps
    i = int(np.argmax(steps))
    y_edge = y0 - 1 + i + 1.0                                 # boundary between rows (pixel edge)
    return float(v1 - y_edge / S), float(steps[i])


def photo_edge_any(lum: np.ndarray, rect: dict, edge: str, at: float, rng, brighter: str | None, search: float) -> tuple[float, float]:
    """Edge of an opening / object in the photo near the model edge `at`: left / right edges are vertical (s searched,
    mean over the v range), top / bottom horizontal (v searched, mean over the s range). brighter = side that is brighter
    (left / right / above / below); None = strongest step of either sign. Returns (position m, contrast)."""
    S, u0, v1 = rect["S"], rect["u0"], rect["v1"]
    if edge in ("top", "bottom"):
        return photo_edge(lum, rect, rng, at, brighter or "above", search) if brighter else _either(lum, rect, rng, at, search, "v")
    x0, x1 = int(round((at - search - u0) * S)), int(round((at + search - u0) * S))
    y0, y1 = int(round((v1 - rng[1]) * S)), int(round((v1 - rng[0]) * S))
    x0, x1 = max(1, x0), min(lum.shape[1] - 2, x1)
    prof = lum[y0:y1, x0 - 1:x1 + 2].mean(axis=0)             # columns left -> right
    steps = prof[:-1] - prof[1:]                               # left minus right: > 0 when the left side is brighter
    if brighter is None:
        steps = np.abs(steps)
    elif brighter == "right":
        steps = -steps
    i = int(np.argmax(steps))
    x_edge = x0 - 1 + i + 1.0
    return float(x_edge / S + u0), float(steps[i])


def _either(lum, rect, rng, at, search, axis) -> tuple[float, float]:
    a = photo_edge(lum, rect, rng, at, "above", search)
    b = photo_edge(lum, rect, rng, at, "below", search)
    return a if a[1] >= b[1] else b


def verify_linear(ac: TapirClient, spec: dict, want_linear: list[dict], guids: dict, details: dict, frames: dict, stories: dict,
                  sysinfo: dict | None, lum: np.ndarray | None, row, trow, problems: list) -> dict:
    """Rows for every linear element; returns {'proj': item -> [(svw, polygons)], 'photo': [...], 'count': {...}}."""
    rect = next((s for s in spec["sources"] if s.get("rect")), {}).get("rect")
    cam = camera_of(spec)
    present = [it for it in want_linear if it["item"] in details]
    for it in want_linear:
        if it["item"] not in details:
            problems.append(f"{it['item']}: linear element missing in the project")
    geos = geometry_of(ac, [guids[it["item"]] for it in present])
    proj, photo = {}, []
    for it, geo in zip(present, geos):
        lid, sp = it["item"], it["spec_item"]
        plane = it.get("plane", "F")
        fr = frames[plane]
        w0 = it.get("w0", 0.0)
        prof = spec["systems"][spec["systems"][sp["system"]]["profile"]]
        d = details[lid]
        det = d["details"]
        floor = d["floorIndex"]
        if floor != it["payload"].get("floorIndex", spec["reference"]["home_story"]["floorIndex"]):
            problems.append(f"{lid}: home story {floor} != spec (story of its foot) {it['payload'].get('floorIndex')}")
        s0, s1 = sp["s"]["v"]
        pw = max(p[0] for p in prof["polygon_wv"])
        pw_back = min(p[0] for p in prof["polygon_wv"])
        ph = max(p[1] for p in prof["polygon_wv"])
        if sp["axis"] == "h":
            v0, v1 = sp["v"]["v"], sp["v"]["v"] + ph
        else:
            v0, v1 = sp["v"]["v"]
        # (a) parameters
        if it["kind"] == "beam":
            ends = fr.to_svw(np.array([[det["begCoordinate"]["x"], det["begCoordinate"]["y"], 0.0],
                                       [det["endCoordinate"]["x"], det["endCoordinate"]["y"], 0.0]]))
            row(lid, "s0", s0, float(ends[:, 0].min()), "params")
            row(lid, "s1", s1, float(ends[:, 0].max()), "params")
            row(lid, "v0 (beam level)", v0, stories[floor] + det["level"] - fr.z0, "params")
        else:
            o = fr.to_svw(np.array([[det["origin"]["x"], det["origin"]["y"], 0.0]]))[0]
            right = outside_of(spec, plane) == "right_of_ab"
            row(lid, "s origin", s0 if right else s1, float(o[0]), "params")
            row(lid, "v0 (column bottom)", v0, stories[floor] + det["bottomOffset"] - fr.z0, "params")
            row(lid, "v1 (column top)", v1, stories[floor] + det["bottomOffset"] + det["height"] - fr.z0, "params")
            ang = math.degrees(math.remainder(det["axisRotationAngle"] - math.atan2(fr.n[1], fr.n[0]), 2 * math.pi))
            row(lid, "rotation - outward normal [deg/100]", 0.0, ang / 100.0, "params")
        trow(lid, "profile on the element", it["profile_guid"], it["profile_guid"] if it["profile_guid"].lower() in str(det).lower() else None)
        # (b) 3D
        bodies = []
        for body in geo.get("bodies", []):
            xyz = np.array([[v["x"], v["y"], v["z"]] for v in body["vertices"]], float)
            if len(xyz):
                bodies.append((fr.to_svw(xyz), body["polygons"]))
        proj[lid] = bodies
        if not bodies:
            problems.append(f"{lid}: no 3D body (beam/column segments)")
            continue
        allp = np.vstack([b for b, _ in bodies])
        # at a corner where two planes join, Archicad connects the beams of both facades (and trims / extends them up to
        # the profile depth); tolerance there = the profile reach
        reach = max(abs(p[0]) for p in prof["polygon_wv"])
        ja = _joined(spec, plane, "a") and s0 <= 1e-6 or sp.get("wrap") == "a"
        jb = _joined(spec, plane, "b") and s1 >= fr.length - 1e-3 or sp.get("wrap") == "b"
        row(lid, "s0" + (" (joined corner)" if ja else ""), s0, float(allp[:, 0].min()), "3D", tolerance=reach if ja else None)
        row(lid, "s1" + (" (joined corner)" if jb else ""), s1, float(allp[:, 0].max()), "3D", tolerance=reach if jb else None)
        row(lid, "v0", v0, float(allp[:, 1].min()), "3D")
        row(lid, "v1", v1, float(allp[:, 1].max()), "3D")
        row(lid, "w back (on the wall face)", w0 + pw_back, float(allp[:, 2].min()), "3D")
        row(lid, "w front (projection)", w0 + pw, float(allp[:, 2].max()), "3D")
        # (d) photo edges
        for e in sp.get("photo_edges", []) if plane == "F" else []:          # the photo shows the reference plane only
            v_model = corner_v(bodies, e["corner"], e["s"], cam)
            if lum is None or rect is None or v_model is None:
                continue
            v_photo, contrast = photo_edge(lum, rect, e["s"], v_model, e["brighter"], e.get("search", EDGE_SEARCH))
            tol = e.get("tol", 0.03)
            row(lid, f"photo: {e['name']} ({e['corner']}, kamera)", v_photo, v_model, "photo", tolerance=tol)
            photo.append({"item": lid, "edge": e["name"], "corner": e["corner"], "s": e["s"], "v_photo": round(v_photo, 4),
                          "v_photo_hand": e.get("photo_v"), "v_model_projected": round(v_model, 4),
                          "delta_cm": round((v_model - v_photo) * 100, 2), "tol_cm": tol * 100, "contrast": round(contrast, 1)})
    # (c) profile area per profile system
    if sysinfo:
        for sid, pinfo in sysinfo.get("profiles", {}).items():
            a_spec = polygon_area(spec["systems"][sid]["polygon_wv"])
            rel = (pinfo["area_model"] - a_spec) / a_spec
            trow(sid, f"profile area {pinfo['name']} [m2] (GetProfiles vs polygon, tol 1 %)", round(a_spec, 6), round(pinfo["area_model"], 6),
                 "OK" if abs(rel) <= 0.01 else "FAIL", f"{rel * 100:+.3f} %")
    # independent count: beams / columns on the facade layer
    return {"proj": proj, "photo": photo}


def count_on_layer(ac: TapirClient, layer_index: int) -> dict:
    out = {}
    for typ in ("Beam", "Column"):
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        n = 0
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["layerIndex"]})["detailsOfElements"]
            n = sum(1 for d in dd if d.get("layerIndex") == layer_index)
        out[typ.lower()] = n
    return out

"""FS_Railing: GDL railing object as the fallback for the native railing (ADR-001 R1).

The Archicad API cannot change the infill type of a native railing (ADR-001 "Stav I5/I6 C++": ModifyRailingDetails
changes the height only), so railings with an infill the base favourites do not have (horizontal bars - pilot B HAW Hof)
are placed as the GDL object FS_Railing (facade/gdl/templates/FS_Railing.xml), one object per straight segment.

    expected_parts(params)       independent model of the template: [(kind, [x0, x1, y0, y1, z0, z1])] in local coordinates
    parts_of_geometry(geo, ...)  Get3DGeometryOfElements -> one box per GDL primitive (connected polygon components)
    compare_parts(want, got)     match within a tolerance
    ensure_favorites(ac, ...)    'FS rail.*' Object favourites from a sample (create -> classify -> favourite -> delete)
    plan_railing(...)            build: railing of a spec system with railing_kind 'gdl' -> CreateObjects items

Local system of the template: x along the segment, z up (ZZYZX = H = top of the handrail above the foot), y towards the
fall side; y = 0 is the post axis (on the slab) or the slab edge face (side mounted).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

import numpy as np

TEMPLATE = "FS_Railing"
TEMPLATE_XML = Path(__file__).resolve().parent / "gdl" / "templates" / f"{TEMPLATE}.xml"
RAIL_CLASS = ("Zábradlie", "Zábradlí")      # classification item (SK / CZ template) -> IFC export as IfcRailing
INFILL = {"none": 0, "horizontal_bars": 1, "vertical_balusters": 2, "glass": 3, "panel": 4}
EPS = 1e-5

# steel / glass / perforated sheet surfaces of the CZ template library (pilot projects); a missing one -> 0 (object surface)
STEEL = "Kov - nízkouhlíková ocel"
_STEEL_PARTS = {"matPost": STEEL, "matRail": STEEL}
# the same surface under its SK name (SK template of pilot B, checked 2026-10-20)
SURFACE_ALT = {STEEL: ("Kov - nízkouhlíková oceľ",)}


def surface_name(name: str, available) -> str | None:
    """Name of the surface as the project has it (CZ name, else its SK equivalent); None when neither exists."""
    return next((n for n in (name,) + SURFACE_ALT.get(name, ()) if n in available), None)

# Favourites (all values m). Rationale in the template README; normative minima are OVERIT NA STN.
FAVORITES = {
    "FS rail.horiz_5": {
        "params": {"A": 3.0, "ZZYZX": 1.05, "iPostProf": 2, "postW": 0.012, "postD": 0.06, "iPostMode": 1, "postSp": 1.2,
                   "pInset": 0.006, "iMount": 0, "bRail": True, "iRailProf": 0, "railW": 0.042, "railH": 0.042,
                   "iInfill": 1, "nBars": 5, "iBarDist": 0, "iBarProf": 0, "barD": 0.012, "barT": 0.012, "botGap": 0.10},
        "surfaces": {**_STEEL_PARTS, "matInfill": STEEL},
        "note": "5 vodorovných tyčí Ø 12, stĺpiky plochá oceľ 60 × 12 á max. 1,2 m, madlo rúrka Ø 42, H 1,05"},
    "FS rail.glass": {
        "params": {"A": 3.0, "ZZYZX": 1.0, "iPostProf": 0, "postW": 0.04, "postD": 0.04, "iPostMode": 1, "postSp": 1.2,
                   "pInset": 0.02, "iMount": 0, "bRail": True, "iRailProf": 0, "railW": 0.042, "railH": 0.042,
                   "iInfill": 3, "glassT": 0.016, "infGap": 0.015, "infOffs": 0.0, "botGap": 0.05, "topGap": 0.05},
        "surfaces": {**_STEEL_PARTS, "matInfill": "Sklo - čiré rychlé"},
        "note": "sklo 16 mm (VSG 2 × 8) medzi stĺpikmi 40 × 40 á max. 1,2 m, madlo Ø 42, H 1,0"},
    "FS rail.balusters": {
        "params": {"A": 3.0, "ZZYZX": 1.0, "iPostProf": 0, "postW": 0.04, "postD": 0.04, "iPostMode": 1, "postSp": 1.2,
                   "pInset": 0.02, "iMount": 0, "bRail": True, "iRailProf": 0, "railW": 0.042, "railH": 0.042,
                   "iInfill": 2, "balSp": 0.11, "iBalProf": 1, "balD": 0.012, "bBotRail": True, "botRW": 0.04, "botRH": 0.02,
                   "botGap": 0.05},
        "surfaces": {**_STEEL_PARTS, "matInfill": STEEL},
        "note": "zvislé tyčky 12 × 12 á max. 0,11 m (svetlo ≤ 0,098), spodný pás 40 × 20, stĺpiky 40 × 40, madlo Ø 42, H 1,0"},
    "FS rail.panel_perf": {
        "params": {"A": 3.0, "ZZYZX": 1.0, "iPostProf": 0, "postW": 0.04, "postD": 0.04, "iPostMode": 1, "postSp": 1.2,
                   "pInset": 0.02, "iMount": 0, "bRail": True, "iRailProf": 0, "railW": 0.042, "railH": 0.042,
                   "iInfill": 4, "panT": 0.003, "iPanType": 1, "infGap": 0.015, "infOffs": 0.0, "botGap": 0.05, "topGap": 0.05},
        "surfaces": {**_STEEL_PARTS, "matInfill": "Kov - perforovaný malé kruhy"},
        "note": "perforovaný plech 3 mm medzi stĺpikmi 40 × 40 á max. 1,2 m, madlo Ø 42, H 1,0"},
}


# ---- template defaults --------------------------------------------------------------------------------------------
def template_defaults() -> dict:
    """Parameter -> default value of the template XML (typed)."""
    xml = TEMPLATE_XML.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r'<(\w+) Name="(\w+)">\s*<Description>.*?</Description>\s*<Value>(<!\[CDATA\[.*?\]\]>|[^<]*)</Value>', xml, re.S):
        typ, name, val = m.groups()
        if typ == "String":
            out[name] = val[len('<![CDATA["'):-len('"]]>')]
        elif typ in ("Integer", "Material"):
            out[name] = int(val)
        elif typ == "Boolean":
            out[name] = bool(int(val))
        else:
            out[name] = float(val)
    return out


def full_params(overrides: dict | None = None) -> dict:
    p = template_defaults()
    p.update(overrides or {})
    return p


# ---- independent model of the template ----------------------------------------------------------------------------
def layout(p: dict) -> dict:
    """Shared quantities (mirror of the master script, written from the template description)."""
    H = p["ZZYZX"]
    sec_x = p["postW"]
    sec_y = p["postW"] if p["iPostProf"] == 1 else p["postD"]
    yc, z_post0 = (p["sideGap"] + sec_y / 2, -p["mountDrop"]) if p["iMount"] == 1 else (0.0, 0.0)
    z_rail_bot = z_post_top = H
    if p["bRail"]:
        if p["iRailProf"] == 0:
            z_rail_bot, z_post_top = H - p["railW"], H - p["railW"] / 2
        else:
            z_rail_bot = z_post_top = H - p["railH"]
    x0, x1 = p["pInset"], p["A"] - p["pInset"]
    span = max(x1 - x0, EPS)
    n_bays = max(1, p["nPost"] - 1) if p["iPostMode"] == 0 else max(1, math.ceil(span / max(p["postSp"], 0.01) - EPS))
    n_bays = min(n_bays, 500)
    px = [x0 + i * span / n_bays for i in range(n_bays + 1)]
    return {"H": H, "sec_x": sec_x, "sec_y": sec_y, "yc": yc, "z_post0": z_post0, "z_rail_bot": z_rail_bot,
            "z_post_top": z_post_top, "px": px, "n_bays": n_bays}


def bar_heights(p: dict, lo: dict) -> list[float]:
    if p["iInfill"] != 1:
        return []
    if p["iBarDist"] == 0:
        n = max(1, p["nBars"])
        g = (lo["z_rail_bot"] - p["botGap"] - n * p["barD"]) / n
        return [p["botGap"] + p["barD"] / 2 + i * (p["barD"] + g) for i in range(n)]
    out = []
    for tok in str(p["barZList"]).split(";"):
        try:
            out.append(float(tok.strip()))
        except ValueError:
            continue
    return out[:100]


def expected_parts(p: dict) -> list[tuple[str, list[float]]]:
    """[(kind, [x0, x1, y0, y1, z0, z1])] of every GDL primitive; kinds post / handrail / bar / bottom_rail / baluster /
    glass / panel."""
    lo = layout(p)
    yc, px, nb = lo["yc"], lo["px"], lo["n_bays"]
    parts = []
    for i, x in enumerate(px):
        if (i == 0 and not p["bPostStart"]) or (i == nb and not p["bPostEnd"]):
            continue
        hx, hy = lo["sec_x"] / 2, lo["sec_y"] / 2
        parts.append(("post", [x - hx, x + hx, yc - hy, yc + hy, lo["z_post0"], lo["z_post_top"]]))
    if p["bRail"]:
        hy = p["railW"] / 2
        parts.append(("handrail", [-p["railOvhS"], p["A"] + p["railOvhE"], yc - hy, yc + hy, lo["z_rail_bot"], lo["H"]]))
    if p["iInfill"] == 1:
        for z in bar_heights(p, lo):
            hy = p["barD"] / 2 if p["iBarProf"] == 0 else p["barT"] / 2
            parts.append(("bar", [px[0], px[-1], yc - hy, yc + hy, z - p["barD"] / 2, z + p["barD"] / 2]))
    elif p["iInfill"] == 2:
        zb0 = p["botGap"]
        if p["bBotRail"]:
            parts.append(("bottom_rail", [px[0], px[-1], yc - p["botRW"] / 2, yc + p["botRW"] / 2, p["botGap"], p["botGap"] + p["botRH"]]))
            zb0 += p["botRH"]
        h = p["balD"] / 2
        for j in range(nb):
            fa = px[j] + lo["sec_x"] / 2
            lc = px[j + 1] - px[j] - lo["sec_x"]
            if lc <= EPS:
                continue
            ks = math.ceil(lc / max(p["balSp"], 0.01) - EPS)
            for m in range(1, ks):
                xb = fa + m * lc / ks
                parts.append(("baluster", [xb - h, xb + h, yc - h, yc + h, zb0, lo["z_rail_bot"]]))
    elif p["iInfill"] in (3, 4):
        t = p["glassT"] if p["iInfill"] == 3 else p["panT"]
        z0, z1 = p["botGap"], lo["z_rail_bot"] - p["topGap"]
        for j in range(nb):
            xa = px[j] + lo["sec_x"] / 2 + p["infGap"]
            xe = px[j + 1] - lo["sec_x"] / 2 - p["infGap"]
            if xe - xa > EPS and z1 - z0 > EPS:
                yo = yc + p["infOffs"]
                parts.append(("glass" if p["iInfill"] == 3 else "panel", [xa, xe, yo - t / 2, yo + t / 2, z0, z1]))
    return parts


def info_dims(p: dict) -> dict:
    """A, B, ZZYZX information parameters (CreateObjects would otherwise take them from the Object tool defaults)."""
    bb = bbox_of_parts(expected_parts(p))
    return {"A": p["A"], "B": round(bb[3] - bb[2], 6) if bb else 0.0, "ZZYZX": p["ZZYZX"]}


def bbox_of_parts(parts) -> list[float] | None:
    if not parts:
        return None
    b = [x for _, x in parts]
    return [min(v[0] for v in b), max(v[1] for v in b), min(v[2] for v in b), max(v[3] for v in b), min(v[4] for v in b), max(v[5] for v in b)]


# ---- 3D geometry read back ----------------------------------------------------------------------------------------
def parts_of_geometry(geo: dict, origin: dict, angle: float) -> list[list[float]]:
    """Get3DGeometryOfElements of one object -> box per connected polygon component (= GDL primitive), in local x, y, z."""
    boxes = []
    c, s = math.cos(angle), math.sin(angle)
    for body in geo.get("bodies", []):
        verts = body["vertices"]
        parent = list(range(len(verts)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for poly in body.get("polygons", []):
            ids = poly["vertexIds"]
            r0 = find(ids[0])
            for k in ids[1:]:
                rk = find(k)
                if rk != r0:
                    parent[rk] = r0
        groups: dict[int, list[int]] = {}
        for i in range(len(verts)):
            groups.setdefault(find(i), []).append(i)
        for idx in groups.values():
            q = np.array([[verts[i]["x"] - origin["x"], verts[i]["y"] - origin["y"], verts[i]["z"] - origin.get("z", 0.0)] for i in idx])
            lx = q[:, 0] * c + q[:, 1] * s
            ly = -q[:, 0] * s + q[:, 1] * c
            boxes.append([float(lx.min()), float(lx.max()), float(ly.min()), float(ly.max()), float(q[:, 2].min()), float(q[:, 2].max())])
    return boxes


def compare_parts(want: list[tuple[str, list[float]]], got: list[list[float]], tol: float = 0.005) -> dict:
    """Greedy match of expected and read boxes. Returns {ok, n_want, n_got, max_delta, unmatched_want, unmatched_got}."""
    left = list(range(len(got)))
    max_d, miss = 0.0, []
    for kind, wb in sorted(want, key=lambda t: (t[1][4], t[1][0])):
        best, bd = None, 1e9
        for j in left:
            d = max(abs(a - b) for a, b in zip(wb, got[j]))
            if d < bd:
                best, bd = j, d
        if best is not None and bd <= tol:
            left.remove(best)
            max_d = max(max_d, bd)
        else:
            miss.append((kind, [round(v, 4) for v in wb], round(bd, 4)))
    return {"ok": not miss and not left and len(want) == len(got), "n_want": len(want), "n_got": len(got), "max_delta": max_d,
            "unmatched_want": miss, "unmatched_got": [[round(v, 4) for v in got[j]] for j in left]}


# ---- Archicad: classification and favourites ----------------------------------------------------------------------
def rail_classification(ac) -> dict | None:
    """ClassificationId of the railing item (SK 'Zábradlie' / CZ 'Zábradlí'): it maps the object to IfcRailing."""
    found: dict[str, list] = {}
    for cs in ac.api("API.GetAllClassificationSystems")["classificationSystems"]:
        tree = ac.api("API.GetAllClassificationsInSystem", {"classificationSystemId": cs["classificationSystemId"]})["classificationItems"]
        stack = list(tree)
        while stack:
            it = stack.pop()["classificationItem"]
            found.setdefault(it["id"], []).append({"classificationSystemId": cs["classificationSystemId"],
                                                   "classificationItemId": it["classificationItemId"]})
            stack += it.get("children", [])
    return next((found[n][0] for n in RAIL_CLASS if len(found.get(n, [])) == 1), None)


def surface_writes(ac, surfaces: dict, log: list | None = None) -> dict:
    """Material parameter -> surface attribute index (0 when the surface is not in the project)."""
    by_name = {a["name"]: a["index"] for a in ac.tapir("GetAttributesByType", {"attributeType": "Surface"})["attributes"]}
    out = {}
    for k, name in surfaces.items():
        nm = surface_name(name, by_name)
        out[k] = by_name[nm] if nm else 0
        if nm is None and log is not None:
            log.append(f"surface '{name}' not in the project - {k} = 0 (object surface)")
    return out


def gdl_writes(p: dict) -> list[dict]:
    """SetGDLParametersOfElements items; Boolean parameters must be JSON booleans (an integer is refused)."""
    bools = {k for k, v in template_defaults().items() if isinstance(v, bool)}
    return [{"name": k, "value": bool(v) if k in bools else v} for k, v in p.items()]


def favorite_params(ac, name: str, log: list | None = None) -> dict:
    fav = FAVORITES[name]
    p = dict(fav["params"])
    p.update(surface_writes(ac, fav.get("surfaces", {}), log))
    p.update({k: v for k, v in info_dims(full_params(p)).items()})
    return p


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def ensure_favorites(ac, names=None, origin=(6000.0, 500.0), log: list | None = None, template_hash: str = "",
                     manifest_path: Path | None = None, force: bool = False, prepare=None) -> dict:
    """'FS rail.*' Object favourites of FS_Railing (ADR D4 sample route): CreateObjects far from every model ->
    SetGDLParametersOfElements -> classification 'Zábradlie/Zábradlí' -> Create/UpdateFavoritesFromElements -> delete
    the sample. Idempotent by a hash (params + template hash) in manifest_path. Returns {name: {hash, params, action}}.
    prepare(guid): called on the fresh sample before it is edited - pilot B: the Object tool puts it on a HIDDEN layer of
    the template ('Interiér - sanita a zariadenia'), where it can be neither changed nor deleted, so build moves it to
    the facade layer first (the favourite then carries the facade layer)."""
    log = log if log is not None else []
    names = list(names or FAVORITES)
    man = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path and manifest_path.exists() else {"favorites": {}}
    existing = set(ac.tapir("GetFavoritesByType", {"elementType": "Object"}).get("favorites", []))
    cls = rail_classification(ac)
    out = {}
    for k, name in enumerate(names):
        p = favorite_params(ac, name, log)
        h = _hash([p, template_hash])
        if not force and name in existing and man["favorites"].get(name, {}).get("hash") == h:
            out[name] = {"hash": h, "params": p, "action": "kept"}
            continue
        res = ac.tapir("CreateObjects", {"objectsData": [{"libraryPartName": TEMPLATE, "coordinates": {"x": origin[0] + 5.0 * k, "y": origin[1], "z": 0.0},
                                                          "dimensions": {"x": p["A"], "y": p["B"], "z": p["ZZYZX"]}}]})
        el = res["elements"][0]
        if "elementId" not in el:
            raise RuntimeError(f"{name}: sample not created: {el}")
        g = el["elementId"]["guid"]
        try:
            if prepare is not None:
                prepare(g)
            r = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": g}, "gdlParameters": gdl_writes(p)}]})
            errs = [x for x in r["executionResults"] if not x.get("success")]
            if errs:
                raise RuntimeError(f"{name}: SetGDLParametersOfElements {errs}")
            if cls:
                r = ac.tapir("SetClassificationsOfElements", {"elementClassifications": [{"elementId": {"guid": g}, "classificationId": cls}]})
                if any(not x.get("success") for x in r["executionResults"]):
                    raise RuntimeError(f"{name}: SetClassificationsOfElements {r}")
            cmd = "UpdateFavoritesFromElements" if name in existing else "CreateFavoritesFromElements"
            r = ac.tapir(cmd, {"favoritesFromElements": [{"elementId": {"guid": g}, "favorite": name}]})
            if any(not x.get("success") for x in r["executionResults"]):
                raise RuntimeError(f"{name}: {cmd} {r}")
        finally:
            ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}}]})
        man["favorites"][name] = {"hash": h, "params": p, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "classified": bool(cls)}
        out[name] = {"hash": h, "params": p, "action": "updated" if name in existing else "created"}
        log.append(f"favourite {out[name]['action']}: {name}")
    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


# ---- build: a railing of a 'railing_kind: gdl' system -> FS_Railing objects ---------------------------------------
def fall_sides(pts_xy: list[tuple[float, float]], outward: tuple[float, float]) -> list[bool]:
    """Per segment: True when the fall side is LEFT of the travel direction. A path that encloses an area (balcony,
    walkway: the slab is between the path and its closing chord) falls to the outside of that polygon; a straight path
    falls towards the outward normal of the facade."""
    pts = np.asarray(pts_xy, float)
    area = 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, np.roll(pts, -1, axis=0)))
    out = []
    for a, b in zip(pts[:-1], pts[1:]):
        d = (b - a) / np.linalg.norm(b - a)
        left = np.array([-d[1], d[0]])
        if abs(area) > 0.01 and len(pts) > 2:
            out.append(area < 0)                   # CCW (area > 0): the slab is on the left -> falls to the right
        else:
            out.append(float(left @ np.asarray(outward, float)) > 0)
    return out


def plan_railing(r: dict, sysdef: dict, pts_xy: list[tuple[float, float]], z_abs: float, floor: int, outward: tuple[float, float],
                 base_params: dict, fall_left: list[bool] | None = None) -> list[dict]:
    """One FS_Railing object per straight segment of the path. Returns [{seg, payload, params}]: the payload of
    CreateObjects (absolute z, floorIndex, plan angle) and every GDL parameter to write (favourite content + segment).
    Posts: spacing = system post_spacing (maximum, equal bays) or railing segment_bays (count per segment), post axis on
    the path vertices (pInset 0), the shared vertex of two segments carries one post (the first segment's); the handrail
    runs on by half its width over an inner vertex that turns (closed corner), not over a straight joint."""
    pts = [tuple(map(float, q)) for q in pts_xy]
    sides = fall_left or fall_sides(pts, outward)
    n = len(pts) - 1
    p0 = full_params(base_params)
    p0.update(sysdef.get("gdl_params", {}))
    ovh = (p0["railW"] / 2) if p0["bRail"] else 0.0
    bays = (r.get("segment_bays") or {}).get("v")

    def corner(i: int) -> bool:                    # inner vertex i turns (a straight joint gets no handrail overrun)
        u = np.array(pts[i]) - np.array(pts[i - 1])
        w = np.array(pts[i + 1]) - np.array(pts[i])
        return abs(u[0] * w[1] - u[1] * w[0]) > 1e-6 * np.linalg.norm(u) * np.linalg.norm(w)

    items = []
    for k in range(n):
        a, b = np.array(pts[k]), np.array(pts[k + 1])
        L = float(np.linalg.norm(b - a))
        start_post = k == 0                        # path order: segment k > 0 shares its start vertex with segment k - 1
        ovh_start = ovh if k > 0 and corner(k) else 0.0
        ovh_end = ovh if k < n - 1 and corner(k + 1) else 0.0
        if sides[k]:
            org, d = a, (b - a) / L
            bs, be, os_, oe = start_post, True, ovh_start, ovh_end
        else:                                      # fall side right of a->b: place b->a so that local +y is the fall side
            org, d = b, (a - b) / L
            bs, be, os_, oe = True, start_post, ovh_end, ovh_start
        p = dict(p0)
        p.update({"A": round(L, 6), "ZZYZX": sysdef["height"]["v"], "iPostMode": 1, "postSp": sysdef["post_spacing"]["v"],
                  "pInset": 0.0, "bPostStart": bs, "bPostEnd": be, "railOvhS": round(os_, 6), "railOvhE": round(oe, 6)})
        if bays:                                   # posts per segment given (pilot B: on the consoles) -> by count
            p.update({"iPostMode": 0, "nPost": int(bays[k]) + 1})
        p.update(info_dims(p))
        payload = {"libraryPartName": TEMPLATE, "coordinates": {"x": round(float(org[0]), 6), "y": round(float(org[1]), 6), "z": round(z_abs, 6)},
                   "floorIndex": floor, "angle": round(math.atan2(d[1], d[0]), 9)}
        if sysdef.get("favorite"):
            payload["favoriteName"] = sysdef["favorite"]
        items.append({"seg": k, "payload": payload, "params": p})
    return items

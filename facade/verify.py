"""verify: read the built facade back from Archicad and compare it with the spec (ADR-001 D6).

(a) numbers  - element parameters read back (GetDetailsOfElements, GDL wall-hole size) -> s/v rects; one wall per zone
(b) geometry - 3D bodies (Get3DGeometryOfElements) projected orthogonally on the plane of the element: bbox of every
               opening element, outline and holes of the outside face of every zone wall (at the offset w of the zone;
               incl. the L-shaped hole of a door whose sidelight stands on a wall parapet and the notch of an arched
               opening whose head stands above the wall in an arch gable)
(c) types    - I2: fill types read back through gdl_param_map.json (leaves, transom, sidelight, glazing bars, frame
               surface, instance values, wall inset), the build-up (Composite) of every zone wall, the FS favourites and
               the FS_* properties of every element. A field the library part cannot carry is an EXCEPTION (listed).
(d) image    - the projection of the reference plane drawn over the rectified photo (overlay PNG); further planes
               (side facade at a corner) have no photo and are listed as such
(e) linear   - I3: cornice, string course, sill ledges, bands (linear_verify.py): parameters, 3D of the segments,
               profile area, edges projected with the camera of the photo vs the photo
(f) objects  - pilot A: GDL objects (FS_Surround, FS_ArchGable): library part, origin, angle, GDL parameters read back =
               sent, 3D bbox = expected from the template (gdl_expect) +-1 cm, arch radii, photo edges
(h) I5/I6    - curtain walls (raster, profiles, panels per cell, door panels, 3D, bars of the raster on the photo), balcony
               slabs (level, thickness, outline, arc) and railings (height, base on the slab, posts, balusters, spacing):
               cw_verify.py; the hole of a glazed field is an Empty Window of the zone wall, checked like an opening
(g) photo    - edges of openings and objects named in the spec (photo_edges) vs the photo, +-tol (3 cm); and an automatic
               check of every edge of every 'meranie' / 'kota' opening of the reference plane (strongest brightness step
               of either sign within +-5 cm; reported, not a failure - the explicit edges are the contract)

Outputs to <out>/<spec.id>/: deviations.csv, types.csv, overlay_<id>.png, model_<id>.png, overlay_edges_<id>.png,
report.json, report.md. Read-only in Archicad (still refuses to run on another project than --project).

    python verify.py <spec.json> [--project ...] [--port 19723] [--out C:/FacadePilot/out]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gdl_expect  # noqa: E402
import systems as fs_systems  # noqa: E402
from build import DEFAULT_OUT, DEFAULT_PROJECT, EMPTY_OPENING, gdl_params, opening_units, plan, state_path, unit_reveal  # noqa: E402
from photo_measure import Photo, reveal_pair, visible_reveal  # noqa: E402
from geom import REFERENCE_PLANE, frames_of, outside_of, zone_w  # noqa: E402
from linear_verify import camera_of, geometry_of, photo_edge_any, to_photo, verify_linear  # noqa: E402
from cw_verify import verify_balconies, verify_cw, verify_railings  # noqa: E402
from spec_validate import TYPE_KEYS, fs_system, load_gdl_map, object_bbox, object_values, validate  # noqa: E402
from tapir_client import TapirClient, TapirError  # noqa: E402

FACE_TOL = 0.005          # m - a face lies in the outside plane
PROTRUSION = 0.10         # m - allowed overhang of sills / boards / handles beyond a wall face
AUTO_SEARCH = 0.05        # m - automatic photo check of opening edges
AUTO_CONTRAST = 12.0      # minimum brightness step (0..255) of a readable edge
AUTO_REVEAL_MIN = 0.03    # m - the automatic check applies the frame x masonry pair only when the visible reveal >= its tolerance
COLORS = {"wall": (230, 30, 30), "hole": (230, 30, 30), "window": (0, 200, 255), "door": (255, 0, 200), "linear": (60, 255, 60),
          "opening": (255, 255, 255), "cw": (0, 255, 255), "slab": (180, 255, 120), "railing": (255, 120, 255),
          "object": (255, 160, 40), "spec": (255, 230, 0), "text": (255, 255, 255), "shadow": (0, 0, 0), "exc": (255, 140, 0)}
HOLE_AREA = "hole area on outside face [m2]"


def _bodies_svw(geometry: dict, fr) -> list[tuple[np.ndarray, list]]:
    out = []
    for body in geometry.get("bodies", []):
        xyz = np.array([[v["x"], v["y"], v["z"]] for v in body["vertices"]], float)
        if len(xyz):
            out.append((fr.to_svw(xyz), body["polygons"]))
    return out


def _bbox(bodies) -> list[float] | None:
    pts = [svw for svw, _ in bodies if len(svw)]
    if not pts:
        return None
    allp = np.vstack(pts)
    return [float(allp[:, 0].min()), float(allp[:, 0].max()), float(allp[:, 1].min()), float(allp[:, 1].max()),
            float(allp[:, 2].min()), float(allp[:, 2].max())]


def _outside_faces(bodies, w_face: float):
    """Faces of the wall lying in the plane w = w_face (outside face): (outline, holes) in s/v."""
    faces = []
    for svw, polys in bodies:
        for p in polys:
            ring = svw[p["vertexIds"]]
            if np.all(np.abs(ring[:, 2] - w_face) < FACE_TOL):
                faces.append((ring[:, :2], [svw[h["vertexIds"]][:, :2] for h in p.get("holes", [])]))
    return faces


def _joined_end(spec: dict, plane: str, end: str) -> bool:
    if plane == REFERENCE_PLANE:
        return end in spec["reference"].get("joins", {})
    return any(p["id"] == plane and end in p.get("joins", {}) for p in spec.get("planes", []))


def _inside(pt, ring: np.ndarray) -> bool:
    """Point in polygon (ray casting), ring (N, 2)."""
    x, y = pt
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, np.roll(ring, -1, axis=0)):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _poly_area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _fmt(v):
    return round(v, 4) if isinstance(v, float) else v


def _model_edge(bb: list[float], edge: str) -> float:
    return {"left": bb[0], "right": bb[1], "bottom": bb[2], "top": bb[3]}[edge]


def _project_edge(bodies, edge: str, rng, cam) -> float:
    """Model edge as the photo sees it: the extreme vertex of the bodies on that side, inside the range along the edge,
    taken at the front-most w there, projected with the camera of the photo."""
    pts = np.vstack([b for b, _ in bodies])
    axis_along = 1 if edge in ("left", "right") else 0
    sel = pts[(pts[:, axis_along] >= min(rng) - 0.05) & (pts[:, axis_along] <= max(rng) + 0.05)]
    sel = sel if len(sel) else pts
    k = 0 if edge in ("left", "right") else 1
    ext = sel[:, k].min() if edge in ("left", "bottom") else sel[:, k].max()
    on = sel[np.abs(sel[:, k] - ext) < 1e-3]
    w = float(on[:, 2].max())
    p = np.array([[ext if k == 0 else float(np.mean(rng)), ext if k == 1 else float(np.mean(rng)), w]])
    return float(to_photo(p, cam)[0, k])


def verify(spec: dict, project: str = DEFAULT_PROJECT, port: int = 19723, out_root: Path = DEFAULT_OUT,
           source_root: Path | None = None, verbose: bool = True) -> dict:
    t0 = time.monotonic()
    errors, warnings = validate(spec)
    if errors:
        raise TapirError("spec is invalid: " + "; ".join(errors))
    out_dir = out_root / spec["id"]
    state = json.loads(state_path(spec, out_root).read_text(encoding="utf-8"))
    ac = TapirClient(port=port, timeout=90)
    path = ac.guard_project(project)
    frames = frames_of(spec)
    tol = spec["tolerances"]["build"]
    stories = {s["index"]: s["level"] for s in ac.tapir("GetStories")["stories"]}
    i2 = any(fs_system(s) for s in spec["systems"].values()) or any(s["type"] == "gdl" for s in spec["systems"].values())
    sysinfo = fs_systems.resolve(ac, spec, path, out_root) if i2 else None
    want = plan(spec, stories, sysinfo)
    units = {u["o"]["id"]: u for u in opening_units(spec)}
    gmap = load_gdl_map()
    known = state["elements"]
    problems: list[str] = []
    rows: list[dict] = []
    trows: list[dict] = []
    edges: list[dict] = []
    auto_edges: list[dict] = []
    exceptions: list[dict] = list(sysinfo["exceptions"]) if sysinfo else []
    exceptions += want.get("exceptions", [])
    walls = {w["item"]: w for w in want["walls"]}

    def row(item, quantity, spec_v, model_v, method, tolerance=None):
        tolerance = tol if tolerance is None else tolerance
        d = None if model_v is None else model_v - spec_v
        ok = d is not None and abs(d) <= tolerance + 1e-9
        rows.append({"item": item, "quantity": quantity, "spec_m": round(spec_v, 4),
                     "model_m": None if model_v is None else round(model_v, 4),
                     "delta_cm": None if d is None else round(d * 100, 2), "tol_cm": round(tolerance * 100, 2),
                     "method": method, "ok": ok})
        if not ok:
            problems.append(f"{item} {quantity} ({method}): spec {spec_v:.3f}, model {model_v}")

    def trow(item, what, spec_v, model_v, status=None, note=""):
        if status is None:
            status = "OK" if fs_systems.same(spec_v, model_v, tol) else "FAIL"
        trows.append({"item": item, "what": what, "spec": _fmt(spec_v), "model": _fmt(model_v), "status": status, "note": note})
        if status == "FAIL":
            problems.append(f"{item} {what}: spec {spec_v!r}, model {model_v!r}")

    # ---- elements present?
    guids = {i: e["guid"] for i, e in known.items()}
    det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids.values()],
                                            "fields": ["type", "id", "floorIndex", "layerIndex", "details"]})["detailsOfElements"]
    details = {i: d for i, d in zip(guids, det) if isinstance(d, dict) and "type" in d}
    for i in guids:
        if i not in details:
            problems.append(f"{i}: element {guids[i]} from the state is missing in the project")
    for oid, u in units.items():                          # the hole of a glazed field is the state item '<id>.otvor'
        if u["o"]["kind"] == "opening" and f"{oid}.otvor" in guids:
            guids[oid] = guids[f"{oid}.otvor"]
            if f"{oid}.otvor" in details:
                details[oid] = details[f"{oid}.otvor"]
    wall_guids = {w: guids.get(w) for w in walls}

    # independent count: every window/door in the project hosted by one of our zone walls (one element per unit)
    hosted = {"window": 0, "door": 0}
    hosted_by = {}
    for typ in ("Window", "Door"):
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["details"]})["detailsOfElements"]
            for e, d in zip(els, dd):
                owner = d.get("details", {}).get("ownerElementId", {}).get("guid")
                if owner in wall_guids.values():
                    cat = "opening" if d.get("details", {}).get("libPart", {}).get("name") == EMPTY_OPENING else typ.lower()
                    hosted[cat] = hosted.get(cat, 0) + 1
                    hosted_by[e["elementId"]["guid"]] = owner
    n_spec = {"window": sum(u["o"]["kind"] == "window" for u in units.values()), "door": sum(u["o"]["kind"] == "door" for u in units.values())}
    if any(u["o"]["kind"] == "opening" for u in units.values()) or "opening" in hosted:
        n_spec["opening"] = sum(u["o"]["kind"] == "opening" for u in units.values())
    count_ok = hosted == n_spec
    if not count_ok:
        problems.append(f"opening count in the zone walls {hosted} != spec {n_spec} (merged openings count once)")

    # ---- (a) parameters read back
    layer_index = state.get("layer", {}).get("index")
    all_items = want["walls"] + want["openings"] + want["linear"] + want["objects"] + want["cw"] + want["slabs"] + want["railings"]
    for item in all_items:
        d = details.get(item["item"])
        if not d:
            continue
        if d["layerIndex"] != layer_index:
            problems.append(f"{item['item']}: layer index {d['layerIndex']} != {layer_index} ({spec['reference']['layer']})")
        if d["id"] != item["element_id"]:
            problems.append(f"{item['item']}: Element ID '{d['id']}' != '{item['element_id']}'")
    foot = {}
    for wid, wall in walls.items():
        d = details.get(wid)
        if not d:
            continue
        wd = d["details"]
        z = wall["zone"]
        fr = wall["geom"]["frame"]
        zs0, zs1, zv0, zv1 = z["rect"]["v"]
        floor = d["floorIndex"]
        foot[wid] = stories[floor] + wd["bottomOffset"] - fr.z0
        sb, se = fr.to_svw(np.array([[wd["begCoordinate"]["x"], wd["begCoordinate"]["y"], 0.0],
                                     [wd["endCoordinate"]["x"], wd["endCoordinate"]["y"], 0.0]]))[:, [0, 2]]
        row(wid, "s0", zs0, float(min(sb[0], se[0])), "params")
        row(wid, "s1", zs1, float(max(sb[0], se[0])), "params")
        row(wid, "w of reference line (zone face)", zone_w(z), float(sb[1]), "params")
        row(wid, "w of reference line at the end", zone_w(z), float(se[1]), "params")
        row(wid, "v0 (foot)", zv0, foot[wid], "params")
        row(wid, "v1 (top)", zv1, foot[wid] + wd["height"], "params")
        row(wid, "thickness", z["thickness"]["v"], wd["begThickness"], "params")
        if floor != wall["payload"]["floorIndex"]:
            problems.append(f"{wid}: wall home story {floor} != spec (story of its foot) {wall['payload']['floorIndex']}")
        tl = wall.get("top_link")                          # rule 3: the top linked to a story, not a fixed height
        if tl:
            trow(wid, "top link: relativeTopStory (story of the wall top - home story)", tl["relativeTopStory"], wd.get("relativeTopStory"),
                 "OK" if wd.get("relativeTopStory") == tl["relativeTopStory"] else "FAIL")
            row(wid, "top link: topOffset above the linked story", tl["topOffset"], wd.get("topOffset"), "params")
        else:
            trow(wid, "top link: none (top story of the spec - fixed height, reported)", 0, wd.get("relativeTopStory"),
                 "OK" if not wd.get("relativeTopStory") else "FAIL")
    unit_guids = [guids[i] for i in units if i in details]
    gdl = gdl_params(ac, unit_guids)
    for oid, u in units.items():
        d = details.get(oid)
        wall = walls[u["zone"]]
        if not d or u["zone"] not in foot:
            continue
        od = d["details"]
        if od.get("ownerElementId", {}).get("guid") != wall_guids[u["zone"]]:
            problems.append(f"{oid}: owner is not the wall of its zone {u['zone']}")
        g = wall["geom"]
        s0, s1, v0, v1 = u["rect"]
        sc = (g["beg_s"] - od["centerOffset"]) if g["reverse"] else (g["beg_s"] + od["centerOffset"])
        p = gdl.get(guids[oid], {})
        hole_w = p.get("ac_wallhole_width", od["width"])
        hole_h = p.get("ac_wallhole_height", od["height"])
        bottom = foot[u["zone"]] + od["sillHeight"] - (p.get("ac_lower_oversize") or 0.0)
        row(oid, "s0", s0, sc - hole_w / 2, "params")
        row(oid, "s1", s1, sc + hole_w / 2, "params")
        row(oid, "v0", v0, bottom, "params")
        row(oid, "v1", v1, bottom + hole_h, "params")
        sl = u["values"].get("sidelight_w")
        if sl and "gs_sidelight_width_right" in p:                 # sidelight edge / parapet from the parameters
            edge_model = sc + hole_w / 2 - (p["gs_sidelight_width_right"] - p.get("gs_frame_width", 0.0))
            label = u["members"][0]["id"] if u["members"] else oid
            row(label, "s0 (sidelight edge)", s1 - sl["v"], edge_model, "params")
            sill = u["values"].get("sidelight_sill")
            if sill:
                row(label, "v0 (sidelight parapet)", v0 + sill["v"], bottom + p.get("gs_sidelight_parapet_hgt_right", 0.0), "params")
        if "shoulder_h" in u["values"] and "gs_shoulder_h" in p:
            row(oid, "v springing of the arch (shoulder)", v0 + u["values"]["shoulder_h"]["v"], bottom + p["gs_shoulder_h"], "params")

    # ---- (b) 3D geometry projected on the plane of the element
    order = list(walls) + list(units)
    present = [i for i in order if i in details]
    geos = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": guids[i]}} for i in present]}, timeout=120)["geometries3D"]
    proj = {}
    for i, g in zip(present, geos):
        plane = walls[i]["geom"]["plane"] if i in walls else units[i]["plane"]
        proj[i] = _bodies_svw(g, frames[plane])
    wall_faces = {}
    unit_holes = {}
    for wid, wall in walls.items():
        if wid not in proj:
            continue
        z = wall["zone"]
        zs0, zs1, zv0, zv1 = z["rect"]["v"]
        wb = _bbox(proj[wid])
        zw = zone_w(z)
        plane = wall["geom"]["plane"]
        # an end at a corner where the plane joins another one: Archicad joins the two walls (L junction, modeller rule 8)
        # and may trim / extend the body inside the corner square - tolerance = the wall thickness there
        j0 = _joined_end(spec, plane, "a") and zs0 <= 1e-6
        j1 = _joined_end(spec, plane, "b") and zs1 >= wall["geom"]["frame"].length - 1e-3
        th = z["thickness"]["v"]
        row(wid, "s0" + (" (joined corner)" if j0 else ""), zs0, wb[0], "3D", tolerance=th if j0 else tol)
        row(wid, "s1" + (" (joined corner)" if j1 else ""), zs1, wb[1], "3D", tolerance=th if j1 else tol)
        row(wid, "v0", zv0, wb[2], "3D")
        row(wid, "v1", zv1, wb[3], "3D")
        row(wid, "w outside face", zw, wb[5], "3D")
        row(wid, "w inside face", zw - z["thickness"]["v"], wb[4], "3D")
        faces = _outside_faces(proj[wid], zw)
        wall_faces[wid] = faces
        if not faces:
            problems.append(f"{wid}: no face in the outside plane w = {zw} (wall on the wrong side?)")
            continue
        holes = [h for _, hs in faces for h in hs]
        net = sum(_poly_area(o) - sum(_poly_area(h) for h in hs) for o, hs in faces)
        fs0 = min(float(o[:, 0].min()) for o, _ in faces) if j0 else zs0       # face extent at a joined corner
        fs1 = max(float(o[:, 0].max()) for o, _ in faces) if j1 else zs1
        hole_area = (fs1 - fs0) * (zv1 - zv0) - net                          # holes + notches (door at the foot, arched head)
        spec_area = 0.0
        mine_units = {oid: u for oid, u in units.items() if u["zone"] == wid}
        for u in mine_units.values():
            s0, s1, v0, v1 = u["hole"]
            area = (s1 - s0) * (v1 - v0)
            if "sidelight_w" in u["values"] and "sidelight_sill" in u["values"]:
                area -= u["values"]["sidelight_w"]["v"] * u["values"]["sidelight_sill"]["v"]      # wall parapet under the sidelight
            spec_area += area
        row(wid, HOLE_AREA, spec_area, hole_area, "3D", tolerance=0.05)
        outline_pts = np.vstack([o for o, _ in faces])
        # every unit has its own hole: bbox = unit rect; an L-hole has its step at the sidelight edge / parapet;
        # a unit whose head stands above the wall is a notch in the outline (its two lower corners are outline vertices)
        for oid, u in mine_units.items():
            s0, s1, v0, v1 = u["hole"]
            if u["o"].get("head_in") or v0 <= zv0 + 1e-3 or v1 >= zv1 - 1e-3:
                # the opening reaches the foot / the top of the wall: a notch of the outline, not a hole
                corners = (("bottom left", (s0, v0)), ("bottom right", (s1, v0)), ("top left", (s0, v1)), ("top right", (s1, v1)))
                for name, cpt in corners:
                    dist = float(np.min(np.linalg.norm(outline_pts - np.array(cpt), axis=1)))
                    row(oid, f"3D notch corner {name} (outline vertex)", 0.0, dist, "3D")
                continue
            # the hole of the unit contains the centre of its host rect (an L-hole of a sidelight door too); touching
            # neighbours and the parapet under a sidelight do not
            hr = u["o"]["rect"]["v"]
            centre = (0.5 * (hr[0] + hr[1]), 0.5 * (hr[2] + min(hr[3], v1)))
            mine = [h for h in holes if _inside(centre, h)]
            if len(mine) != 1:
                problems.append(f"{oid}: {len(mine)} holes in the wall face over the opening (expected 1)")
                continue
            h = mine[0]
            unit_holes[oid] = np.column_stack([h, np.full(len(h), zone_w(z))])
            # units whose frames touch (rule 30 warning) cut ONE hole: compare it with the union of their rects
            group = [k for k, uu in mine_units.items()
                     if _inside((0.5 * (uu["o"]["rect"]["v"][0] + uu["o"]["rect"]["v"][1]),
                                 0.5 * (uu["o"]["rect"]["v"][2] + min(uu["o"]["rect"]["v"][3], uu["hole"][3]))), h)]
            label = "+".join(group) if len(group) > 1 else oid
            gs0 = min(mine_units[k]["hole"][0] for k in group)
            gs1 = max(mine_units[k]["hole"][1] for k in group)
            gv0 = min(mine_units[k]["hole"][2] for k in group)
            gv1 = max(mine_units[k]["hole"][3] for k in group)
            if len(group) > 1 and oid != group[0]:
                continue                                         # the shared hole is checked once
            if len(group) > 1:
                for k in group:                                  # every unit's own corners are vertices of the shared hole
                    ks0, ks1, kv0, kv1 = mine_units[k]["hole"]
                    sill_r = mine_units[k]["values"].get("sidelight_sill", {}).get("v", 0.0)    # right sidelight on a parapet
                    for cpt in ((ks0, kv0), (ks1, kv0 + sill_r)):
                        if abs(cpt[0] - gs0) > 1e-3 and abs(cpt[0] - gs1) > 1e-3:
                            continue                             # inner corner on a shared jamb - not a vertex
                        row(k, f"3D shared hole: corner {cpt[0]:.2f}/{cpt[1]:.2f} is a vertex", 0.0,
                            float(np.min(np.linalg.norm(h - np.array(cpt), axis=1))), "3D")
            row(label, "hole s0", gs0, float(h[:, 0].min()), "3D")
            row(label, "hole s1", gs1, float(h[:, 0].max()), "3D")
            row(label, "hole v0", gv0, float(h[:, 1].min()), "3D")
            row(label, "hole v1", gv1, float(h[:, 1].max()), "3D")
            if "sidelight_w" in u["values"] and u["values"].get("sidelight_sill", {}).get("v", 0) > 0:
                step = np.array([s1 - u["values"]["sidelight_w"]["v"], v0 + u["values"]["sidelight_sill"]["v"]])
                dist = float(np.min(np.linalg.norm(h - step, axis=1)))
                label = u["members"][0]["id"] if u["members"] else oid
                row(label, "hole step (sidelight parapet corner)", 0.0, dist, "3D")
    w_ranges = {}
    for oid, u in units.items():
        if oid not in proj or u["o"]["kind"] == "opening":     # the Empty Window of a glazed field has no 3D body
            continue
        bb = _bbox(proj[oid])
        if bb is None:
            problems.append(f"{oid}: no 3D body")
            continue
        s0, s1, v0, v1 = u["rect"]
        row(oid, "s0", s0, bb[0], "3D")
        row(oid, "s1", s1, bb[1], "3D")
        row(oid, "v0", v0, bb[2], "3D")
        row(oid, "v1", v1, bb[3], "3D")
        w_ranges[oid] = [round(bb[4], 3), round(bb[5], 3)]
        z = walls[u["zone"]]["zone"]
        # sills, boards and handles may stand a little out of the wall faces; a whole unit outside is an error
        if bb[5] > zone_w(z) + PROTRUSION or bb[4] < zone_w(z) - z["thickness"]["v"] - PROTRUSION:
            problems.append(f"{oid}: 3D body sticks out of the wall by more than {PROTRUSION} m (w {bb[4]:.3f}..{bb[5]:.3f})")

    # ---- photo of the reference plane
    src = next(s for s in spec["sources"] if s.get("rect"))
    root = Path(source_root or spec.get("generated", {}).get("root", "."))
    rect = src["rect"]
    cam = camera_of(spec)
    lum = np.asarray(Image.open(root / rect["image"]).convert("L"), dtype=float)
    ph_lum = Photo.from_lum(lum, rect["S"], rect["u0"], rect["v1"])

    # ---- (e) linear elements (I3)
    lin = verify_linear(ac, spec, want["linear"], guids, details, frames, stories, sysinfo, lum if want["linear"] else None, row, trow, problems)

    # ---- (f) GDL objects
    obj_proj = {}
    present_obj = [it for it in want["objects"] if it["item"] in details]
    for it in want["objects"]:
        if it["item"] not in details:
            problems.append(f"{it['item']}: object missing in the project")
    ogeo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": guids[it["item"]]}} for it in present_obj]},
                    timeout=120)["geometries3D"] if present_obj else []
    opar = gdl_params(ac, [guids[it["item"]] for it in present_obj])
    for it, g in zip(present_obj, ogeo):
        oid, ob = it["item"], it["spec_item"]
        od = details[oid]["details"]
        fr = frames[it["plane"]]
        trow(oid, "library part (FS template)", it["template"], od.get("libPart", {}).get("name"))
        p = opar.get(guids[oid], {})
        for wr in it["gdl"]:
            trow(oid, f"GDL {wr['name']}", wr["value"], p.get(wr["name"]), None if not isinstance(wr["value"], float) else
                 ("OK" if abs((p.get(wr["name"]) or 0) - wr["value"]) <= 1e-6 else "FAIL"))
        org = od.get("origin", {})
        o_svw = fr.to_svw(np.array([[org.get("x", math.nan), org.get("y", math.nan), 0.0]]))[0]
        row(oid, "origin s", ob["origin"]["v"][0], float(o_svw[0]), "params")
        row(oid, "origin w", ob["origin"]["v"][2], float(o_svw[2]), "params")
        if od.get("angle") is not None:
            dang = math.degrees(math.remainder(od["angle"] - it["payload"]["angle"], 2 * math.pi))
            row(oid, "angle - plane angle [deg/100]", 0.0, dang / 100.0, "params")
        bodies = _bodies_svw(g, fr)
        obj_proj[oid] = bodies
        bb = _bbox(bodies)
        if bb is None:
            problems.append(f"{oid}: no 3D body")
            continue
        want_bb = object_bbox(spec, ob)
        for k, name in enumerate(("s0", "s1", "v0", "v1", "w back", "w front")):
            row(oid, name, want_bb[k], bb[k], "3D")
        radii = gdl_expect.arch_radii(it["template"], object_values(ob))
        if radii:
            allp = np.vstack([b for b, _ in bodies])
            r = np.hypot(allp[:, 0] - ob["origin"]["v"][0], allp[:, 1] - ob["origin"]["v"][1])
            dev = np.minimum(np.abs(r - radii[0]), np.abs(r - radii[1]))
            row(oid, f"arch: every vertex on rIn {radii[0]} or rOut {radii[1]} (max deviation)", 0.0, float(dev.max()), "3D")
        if it["plane"] == REFERENCE_PLANE:
            for e in ob.get("photo_edges", []):
                _edge_row(oid, e, bodies, cam, lum, rect, row, edges)
    for oid, u in units.items():                                       # explicit edges of openings (reference plane)
        if u["plane"] != REFERENCE_PLANE or oid not in proj:
            continue
        zw = zone_w(walls[u["zone"]]["zone"])
        if oid in unit_holes:                                          # the hole of the model in the wall face (3D)
            hole = [(unit_holes[oid], [])]
        else:                                                          # arched head (notch): the spec hole
            hole = [(np.array([[u["hole"][0], v, zw], [u["hole"][1], v, zw]]), []) for v in (u["hole"][2], u["hole"][3])]
        for e in u["o"].get("photo_edges", []):
            _edge_row(oid, e, hole, cam, lum, rect, row, edges)
        if u["o"]["rect"]["conf"] in ("meranie", "kota") and not u["o"].get("head_in"):   # automatic, informative
            s0, s1, v0, v1 = u["hole"]
            zw = zone_w(walls[u["zone"]]["zone"])
            pts = np.array([[s0, v0, zw], [s1, v1, zw]])
            pp = to_photo(pts, cam)
            rev = unit_reveal(spec, u)
            for edge, at, rng in (("left", pp[0, 0], (v0 + 0.15 * (v1 - v0), v1 - 0.15 * (v1 - v0))),
                                  ("right", pp[1, 0], (v0 + 0.15 * (v1 - v0), v1 - 0.15 * (v1 - v0))),
                                  ("top", pp[1, 1], (s0 + 0.15 * (s1 - s0), s1 - 0.15 * (s1 - s0))),
                                  ("bottom", pp[0, 1], (s0 + 0.15 * (s1 - s0), s1 - 0.15 * (s1 - s0)))):
                mid = (0.5 * (s0 + s1), 0.5 * (v0 + v1))
                rw = visible_reveal(cam, edge, s0 if edge == "left" else s1 if edge == "right" else mid[0],
                                    v1 if edge == "top" else v0 if edge == "bottom" else mid[1], zw, rev)
                ae = auto_edge_check(lum, rect, ph_lum, edge, at, rng, rw)
                if ae is None:
                    continue
                ae = dict(item=oid, **ae)
                auto_edges.append(ae)

    # ---- (h) I5/I6: curtain walls, balconies, railings
    cwr = verify_cw(ac, spec, want["cw"], guids, details, frames, sysinfo, lum, rect, cam, row, trow, problems) if want["cw"] else {"proj": {}, "photo": []}
    balr = verify_balconies(ac, spec, want["slabs"], guids, details, frames, stories, row, trow, problems) if want["slabs"] else {"proj": {}}
    railr = verify_railings(ac, spec, want["railings"], guids, frames, row, trow, problems) if want["railings"] else {"proj": {}}
    edges += cwr["photo"]

    # independent counts on the facade layer: beams / columns (linear), objects, curtain walls, slabs, railings
    n_lin_spec = {"beam": sum(it["kind"] == "beam" for it in want["linear"]), "column": sum(it["kind"] == "column" for it in want["linear"]),
                  "object": len(want["objects"])}
    n_lin_model = count_on_layer(ac, layer_index) if (want["linear"] or want["objects"]) else {"beam": 0, "column": 0, "object": 0}
    if n_lin_model != n_lin_spec:
        problems.append(f"linear elements / objects on the facade layer {n_lin_model} != spec {n_lin_spec}")
    n_i6_spec = {"curtainwall": len(want["cw"]), "slab": len(want["slabs"]), "railing": len(want["railings"])}
    n_i6_model = count_on_layer(ac, layer_index, ("CurtainWall", "Slab", "Railing"))
    if n_i6_model != n_i6_spec:
        problems.append(f"curtain walls / slabs / railings on the facade layer {n_i6_model} != spec {n_i6_spec}")

    # ---- (c) types, build-up, favourites, properties (I2)
    if sysinfo:
        attrs = sysinfo["attrs"]
        exc_keys = {(e.get("item"), e["field"]) for e in exceptions} | {(e["system"], e["field"]) for e in exceptions if "item" not in e}
        for oid, u in units.items():
            g = guids.get(oid)
            if g not in gdl or u["o"]["kind"] == "opening":
                continue
            p, od = gdl[g], details[oid]["details"]
            o = u["o"]
            osys = spec["systems"][o["system"]]
            lp = gmap["libparts"].get(osys.get("libpart", ""), None)
            if lp is None:
                continue
            trow(oid, "library part", osys["libpart"], od["libPart"]["name"])
            for f in TYPE_KEYS:
                if f not in osys:
                    continue
                got = fs_systems.read_field(lp["fields"][f]["read"], p, attrs)
                exc = (o["system"], f) in exc_keys and not fs_systems.same(got, osys[f]["v"])
                trow(oid, f, osys[f]["v"], got, "EXC" if exc else None, lp["fields"][f].get("unsupported", "")[:90] if exc else "")
            for k, val in u["values"].items():
                rule = lp["instance"].get(k, {})
                if "unsupported" in rule:
                    trow(oid, k, val["v"], None, "EXC", rule["unsupported"][:90])
                    continue
                trow(oid, k, val["v"], fs_systems.read_field(rule["read"], p, attrs))
            if "reveal" in o:
                trow(oid, "wall inset (reveal, revealDepthOffset)", o["reveal"]["v"], od.get("revealDepthOffset"))
                trow(oid, "wall inset measured from the wall side (revealDepthLocation)", "Side", od.get("revealDepthLocation"))
                trow(oid, "wall inset from the outside (refSide)", True, od.get("refSide"))
            if osys.get("favorite"):
                trow(oid, "placed from favourite (hash in state)", sysinfo["favorites"][o["system"]]["hash"],
                     next(it["fav_hash"] for it in want["openings"] if it["item"] == oid))
        # build-up of every zone wall
        for wid, wall in walls.items():
            wd = details.get(wid, {}).get("details")
            wsys = spec["systems"][wall["zone"]["system"]]
            if "composite" in wsys and wd:
                trow(wid, "structure", "Composite", wd.get("structureType"))
                trow(wid, "composite", wsys["composite"]["name"],
                     next((n for n, a in attrs.composite.items() if a["attributeId"]["guid"] == (wd.get("compositeId") or {}).get("guid")), None))
                diffs = fs_systems.composite_differences(ac, wsys["composite"], attrs)
                trow(wid, "composite skins (material, thickness, order outside -> inside)",
                     " | ".join(f"{sk['material']} {sk['thickness']['v']}" for sk in wsys["composite"]["skins"]),
                     "as spec" if not diffs else "; ".join(diffs), "OK" if not diffs else "FAIL")
                trow(wid, "surfaces from the skins (no override)", False,
                     bool(wd.get("referenceMaterial", {}).get("overridden") or wd.get("oppositeMaterial", {}).get("overridden")))
        # FS favourites exist
        kind_of = lambda s: {"linear": s.get("tool", "")}.get(s["type"], s["type"])
        for kind, names in [(fs_systems.FAV_TYPES[k], [s["favorite"] for s in spec["systems"].values() if fs_system(s) and kind_of(s) == k])
                            for k in ("wall", "window", "door", "beam", "column", "curtain_wall", "slab", "railing")]:
            if names:
                have = set(ac.tapir("GetFavoritesByType", {"elementType": kind}).get("favorites", []))
                for n in names:
                    trow("favorites", f"{kind} favourite '{n}' exists", True, n in have)
        for tpl, gi in sysinfo.get("gdl", {}).items():
            trow("gdl", f"template {tpl} loaded (library part guid = template guid)", gi["template_guid"].upper(), (gi.get("guid") or "").upper())
        # properties FS_* on every element
        props = sysinfo["props"]
        names = list(fs_systems.FS_PROPS)
        if len(props) != len(names):
            problems.append(f"FS properties missing: {sorted(set(names) - set(props))}")
        else:
            items = [it for it in all_items if it["item"] in guids]
            pv = ac.tapir("GetPropertyValuesOfElements", {"elements": [{"elementId": {"guid": guids[it["item"]]}} for it in items],
                                                          "properties": [{"propertyId": {"guid": props[n]}} for n in names]})["propertyValuesForElements"]
            for it, vals in zip(items, pv):
                for n, v in zip(names, vals.get("propertyValues", [])):
                    got = v.get("propertyValue", {}).get("value")
                    trow(it["item"], n, it["props"][n], got)

    # ---- outputs
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "deviations.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["item"])
        wr.writeheader()
        wr.writerows(rows)
    if trows:
        with (out_dir / "types.csv").open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(trows[0].keys()))
            wr.writeheader()
            wr.writerows(trows)
    ref_walls = {w: f for w, f in wall_faces.items() if walls[w]["geom"]["plane"] == REFERENCE_PLANE}
    # slabs / railings stand out of the plane: through the camera when it is complete (s known), else in the photo plane
    # (M016: camera without s, its vertical parallax contradicts the photo - the balconies are ODHAD, the overlay is informative)
    via_cam = bool(cam and cam.get("s") is not None)
    extra = [("cw", {k: v for k, v in cwr["proj"].items()}, False), ("slab", balr["proj"], via_cam), ("railing", railr["proj"], via_cam)]
    # G2 cause 4: 'Δ' in the overlay is model - spec; whether the element was checked against the PHOTO is stated apart
    photo_status = photo_check_status(lin["photo"] + edges, auto_edges)
    overlay_png, model_png = draw(spec, src, root / rect["image"], proj, ref_walls, rows, trows, units, out_dir, lin["proj"], obj_proj, want, extra,
                                  photo_status)
    # I7: extent of every element of the reference plane on the photo (photo plane, through the camera) - the iteration
    # loop needs it to tell an edge hidden by / coinciding with another element from a mis-read one
    lin_pl = {it["item"]: it.get("plane", REFERENCE_PLANE) for it in want["linear"]}
    obj_pl = {it["item"]: it.get("plane", REFERENCE_PLANE) for it in want["objects"]}
    proj_boxes = {"unit": _photo_boxes({k: v for k, v in proj.items() if k in units and units[k]["plane"] == REFERENCE_PLANE}, cam),
                  "linear": _photo_boxes({k: v for k, v in lin["proj"].items() if lin_pl.get(k, REFERENCE_PLANE) == REFERENCE_PLANE}, cam),
                  "object": _photo_boxes({k: v for k, v in obj_proj.items() if obj_pl.get(k, REFERENCE_PLANE) == REFERENCE_PLANE}, cam),
                  "cw": _photo_boxes(cwr["proj"], cam)}
    photo_edges = lin["photo"] + edges
    edges_png = draw_edges(spec, src, root / rect["image"], lin, obj_proj, edges, out_dir) if photo_edges else None

    # the 1 cm contract: rows with the build tolerance (hole areas and joined corners have their own, reported separately)
    geo_rows = [r for r in rows if r["delta_cm"] is not None and r["quantity"] != HOLE_AREA and r["method"] in ("params", "3D")
                and abs(r["tol_cm"] - tol * 100) < 1e-6]
    joined_rows = [r for r in rows if "(joined corner)" in r["quantity"]]
    max_delta = {m: max([abs(r["delta_cm"]) for r in geo_rows if r["method"] == m] or [0.0]) for m in ("params", "3D")}
    other_planes = sorted({it.get("plane", REFERENCE_PLANE) for it in all_items if isinstance(it.get("plane"), str)} - {REFERENCE_PLANE}
                          | {w["geom"]["plane"] for w in want["walls"]} - {REFERENCE_PLANE})
    lin_ids = {it["item"] for it in want["linear"]}
    result = {
        "spec": spec["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.monotonic() - t0, 2),
        "count": {"spec": n_spec, "model_in_wall": hosted, "ok": count_ok, "linear_spec": n_lin_spec, "linear_model": n_lin_model,
                  "i6_spec": n_i6_spec, "i6_model": n_i6_model,
                  "walls": len(walls)},
        "linear": {"items": len(want["linear"]), "photo_edges": lin["photo"],
                   "max_delta_cm": {m: max([abs(r["delta_cm"]) for r in rows if r["method"] == m and r["item"] in lin_ids
                                            and r["delta_cm"] is not None] or [0.0]) for m in ("params", "3D", "photo")},
                   "overlay_png": str(edges_png) if edges_png else None},
        "i6": {"cw": len(want["cw"]), "slabs": len(want["slabs"]), "railings": len(want["railings"]), "cw_photo": cwr["photo"]},
        "objects": {"items": len(want["objects"]), "max_delta_cm": max([abs(r["delta_cm"]) for r in rows if r["item"] in {o["item"] for o in want["objects"]}
                                                                        and r["method"] in ("params", "3D") and r["delta_cm"] is not None] or [0.0])},
        "photo_edges": photo_edges, "auto_edges": auto_edges, "photo_status": photo_status,
        "photo_unverified": sorted([oid for oid, u in units.items() if u["plane"] == REFERENCE_PLANE and not photo_status.get(oid)]
                                   + [it["item"] for it in want["objects"] + want["slabs"] + want["railings"]
                                      if it.get("plane", REFERENCE_PLANE) == REFERENCE_PLANE and not photo_status.get(it["item"])]),
        "planes_without_photo": other_planes,
        "max_delta_cm": max_delta, "tolerance_cm": tol * 100, "joined_corner_rows": joined_rows,
        "rows_failed": sum(not r["ok"] for r in rows), "rows": len(rows),
        "types": {"rows": len(trows), "ok": sum(r["status"] == "OK" for r in trows),
                  "fail": sum(r["status"] == "FAIL" for r in trows), "exc": sum(r["status"] == "EXC" for r in trows)},
        "problems": problems, "exceptions": exceptions, "spec_warnings": warnings,
        "opening_w_range_m": w_ranges, "proj_boxes": proj_boxes,
        "overlay_png": str(overlay_png), "model_png": str(model_png), "edges_png": str(edges_png) if edges_png else None,
        "deviations_csv": str(out_dir / "deviations.csv"), "types_csv": str(out_dir / "types.csv") if trows else None,
        "pass": count_ok and not problems,
    }
    (out_dir / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report.md").write_text(report_md(spec, result, rows, trows), encoding="utf-8")
    if verbose:
        print(f"verify {spec['id']}: {len(walls)} walls, openings model {hosted} / spec {n_spec}; max delta params {max_delta['params']:.2f} cm, "
              f"3D {max_delta['3D']:.2f} cm (tol {tol * 100:.1f} cm); {result['rows_failed']}/{len(rows)} rows out of tolerance; "
              f"photo edges {sum(abs(e['delta_cm']) <= e['tol_cm'] for e in photo_edges)}/{len(photo_edges)} in tolerance; "
              f"types {result['types']}; {'PASS' if result['pass'] else 'FAIL'} ({result['seconds']} s)")
        for p in problems:
            print("  PROBLEM:", p)
        for e in exceptions:
            print(f"  EXCEPTION: {e.get('item', e['system'])}.{e['field']}: spec {e['spec']!r} - not realisable ({e['reason'][:80]})")
        print("  overlay:", overlay_png)
    return result


def auto_edge_check(lum: np.ndarray, rect: dict, ph_lum: Photo, edge: str, at: float, rng, rw: float | None) -> dict | None:
    """Automatic (informative) check of one opening edge on the photo: the strongest step within +-AUTO_SEARCH of the
    projected model edge -> OK (<= 3 cm) / MIMO / nečitateľné (step < AUTO_CONTRAST).
    Parallax (G2 finding 1, cause 2): on the edge whose reveal faces the camera (rw = visible width, photo_measure) the
    masonry corner and the frame are two steps ~rw apart. A step OUTWARD of the model step at that spacing means the model
    may sit on the frame -> 'nejednoznačné', never OK (the I7 loop measures the pair). A weak masonry step at the model
    with the frame inward at ~rw is readable through the pair. None: the window lies outside the photo."""
    try:
        pos, contrast = photo_edge_any(lum, rect, edge, at, rng, None, AUTO_SEARCH)
    except (ValueError, IndexError):
        return None
    readable = contrast >= AUTO_CONTRAST
    d = pos - at
    ae = {"edge": edge, "model": round(at, 4), "photo": round(pos, 4), "delta_cm": round(d * 100, 1),
          "contrast": round(contrast, 1), "status": ("OK" if abs(d) <= 0.03 else "MIMO") if readable else "nečitateľné"}
    if rw is None or rw < AUTO_REVEAL_MIN:     # a visible reveal narrower than the tolerance cannot make the check wrong by more
        return ae
    pr = reveal_pair(ph_lum, edge, at, rw, rng, near=AUTO_SEARCH)
    ae["reveal_cm"] = round(rw * 100, 1)
    if pr["model"] and pr["outer"]:
        o_ = pr["outer"][0]
        ae.update(photo=pr["model"]["pos"], delta_cm=round((pr["model"]["pos"] - at) * 100, 1), contrast=pr["model"]["step"],
                  status="nejednoznačné", second=o_["pos"], second_step=o_["step"],
                  note=f"druhý skok {o_['pos']:.3f} (skok {o_['step']:.0f}) o {abs(o_['pos'] - pr['model']['pos']) * 100:.1f} cm "
                       f"ďalej od kamery (viditeľné ostenie ~{rw * 100:.1f} cm) - model môže byť na ráme")
    elif pr["model"] and pr["inner"] and pr["inner"][0]["step"] >= AUTO_CONTRAST and (
            not readable or abs(pos - pr["inner"][0]["pos"]) <= 0.01):
        # the pair (masonry at the model, readable frame ~rw inward) decides when the masonry step is too weak for the
        # narrow check or when the narrow window caught that frame just inside it - never when the strongest step is
        # something else (then the old verdict stays: a frame and a poster inside are no pair)
        dm = pr["model"]["pos"] - at
        ae.update(photo=pr["model"]["pos"], delta_cm=round(dm * 100, 1), contrast=pr["model"]["step"],
                  status="OK" if abs(dm) <= 0.03 else "MIMO", frame=pr["inner"][0]["pos"],
                  note=f"murovaná hrana (skok {pr['model']['step']:.0f}) čitateľná cez pár: rám {pr['inner'][0]['pos']:.3f} o "
                       f"{abs(pr['inner'][0]['pos'] - pr['model']['pos']) * 100:.1f} cm vnútri")
    return ae


def _photo_boxes(bodies_by: dict, cam) -> dict:
    """item -> [s0', s1', v0', v1', w front] of its 3D bodies projected with the camera of the photo (photo plane, m)."""
    out = {}
    for k, bodies in bodies_by.items():
        pts = [b for b, _ in bodies if len(b)]
        if not pts:
            continue
        allp = np.vstack(pts)
        pp = to_photo(allp, cam)
        out[k] = [round(float(pp[:, 0].min()), 4), round(float(pp[:, 0].max()), 4), round(float(pp[:, 1].min()), 4),
                  round(float(pp[:, 1].max()), 4), round(float(allp[:, 2].max()), 4)]
    return out


def _edge_row(item: str, e: dict, bodies, cam, lum, rect, row, edges: list) -> None:
    at = _project_edge(bodies, e["edge"], e["range"], cam)
    pos, contrast = photo_edge_any(lum, rect, e["edge"], at, e["range"], e["brighter"], e.get("search", 0.05))
    t = e.get("tol", 0.03)
    row(item, f"photo: {e['name']} ({e['edge']}, kamera)", pos, at, "photo", tolerance=t)
    edges.append({"item": item, "edge": e["name"], "side": e["edge"], "range": e["range"], "photo": round(pos, 4),
                  "photo_hand": e.get("photo"), "model_projected": round(at, 4), "delta_cm": round((at - pos) * 100, 2),
                  "tol_cm": t * 100, "contrast": round(contrast, 1)})


def count_on_layer(ac: TapirClient, layer_index: int, types=("Beam", "Column", "Object")) -> dict:
    out = {}
    for typ in types:
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        n = 0
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["layerIndex"]})["detailsOfElements"]
            n = sum(1 for d in dd if d.get("layerIndex") == layer_index)
        out[typ.lower()] = n
    return out


# ---- drawing ----------------------------------------------------------------------------------
def _type_label(oid: str, trows: list[dict]) -> str:
    t = {r["what"]: r for r in trows if r["item"] == oid}
    if not t:
        return ""
    parts = []
    if "leaves" in t:
        parts.append(f"{t['leaves']['model']}kr")
    if "transom" in t:
        parts.append("NS" if t["transom"]["model"] else ("NS chýba!" if t["transom"]["status"] == "EXC" else "bez NS"))
    if t.get("sidelight", {}).get("model") not in (None, "none"):
        parts.append("BS")
    if t.get("sash_bars", {}).get("model"):
        parts.append("priečka")
    key = "wall inset (reveal, revealDepthOffset)"
    if key in t and t[key]["model"] is not None:
        parts.append(f"osadenie {t[key]['model']:.2f}")
    return " ".join(parts)


def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def photo_check_status(photo_edges: list[dict], auto_edges: list[dict]) -> dict:
    """item -> text of its check against the PHOTO (not against the spec): 'foto max x cm' over the named edges and the
    readable automatic edges, '+ nejednoznačné' when a frame x reveal pair is open; items without any photo check are
    absent (the overlay then says 'neoverené voči fotke' - never 'Δ0,0', G2 finding 3/4)."""
    acc: dict[str, dict] = {}
    for e in photo_edges:
        a = acc.setdefault(e["item"], {"d": [], "amb": 0, "unread": 0})
        a["d"].append(abs(e["delta_cm"]))
    for e in auto_edges:
        a = acc.setdefault(e["item"], {"d": [], "amb": 0, "unread": 0})
        if e["status"] in ("OK", "MIMO"):
            a["d"].append(abs(e["delta_cm"]))
        elif e["status"] == "nejednoznačné":
            a["amb"] += 1
        else:
            a["unread"] += 1
    out = {}
    for item, a in acc.items():
        parts = [f"foto max {max(a['d']):.1f} cm ({len(a['d'])} hrán)"] if a["d"] else []
        if a["amb"]:
            parts.append(f"{a['amb']} nejednoznačné")
        if a["unread"] and not a["d"]:
            parts.append("nečitateľné")
        out[item] = ", ".join(parts)
    return out


def draw(spec, src, image_path: Path, proj, wall_faces, rows, trows, units, out_dir: Path, lin_proj=None, obj_proj=None, want=None, extra=None,
         photo_status: dict | None = None):
    r = src["rect"]
    S, u0, v1 = r["S"], r["u0"], r["v1"]
    px = lambda s, v: ((s - u0) * S, (v1 - v) * S)
    base = Image.open(image_path).convert("RGB")
    size = base.size
    font, small = _font(16), _font(13)
    ref_units = {oid: u for oid, u in units.items() if u["plane"] == REFERENCE_PLANE}
    kinds = {oid: u["o"]["kind"] for oid, u in ref_units.items()}
    worst = {}
    for rr in rows:
        if rr["method"] == "3D" and rr["delta_cm"] is not None and rr["quantity"] != HOLE_AREA:
            worst[rr["item"]] = max(worst.get(rr["item"], 0.0), abs(rr["delta_cm"]))
    cam = camera_of(spec)
    lin_planes = {it["item"]: it.get("plane", REFERENCE_PLANE) for it in (want or {}).get("linear", [])}
    obj_planes = {it["item"]: it.get("plane", REFERENCE_PLANE) for it in (want or {}).get("objects", [])}
    other = sorted({w["geom"]["plane"] for w in (want or {}).get("walls", [])} - {REFERENCE_PLANE})

    def paint(canvas: Image.Image) -> Image.Image:
        img = canvas.copy()
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        for oid, kind in kinds.items():
            for svw, polys in proj.get(oid, []):
                for p in polys:
                    ring = svw[p["vertexIds"]]
                    pts = [px(s, v) for s, v in ring[:, :2]]
                    dr.line(pts + [pts[0]], fill=COLORS[kind] + (150,), width=1)
        for col, bodies_by in ((COLORS["linear"], {k: v for k, v in (lin_proj or {}).items() if lin_planes.get(k, REFERENCE_PLANE) == REFERENCE_PLANE}),
                               (COLORS["object"], {k: v for k, v in (obj_proj or {}).items() if obj_planes.get(k, REFERENCE_PLANE) == REFERENCE_PLANE})):
            for _, bodies in bodies_by.items():
                for svw, polys in bodies:
                    pp = to_photo(svw, cam)
                    for p in polys:
                        pts = [px(s_, v_) for s_, v_ in pp[p["vertexIds"]]]
                        dr.line(pts + [pts[0]], fill=col + (200,), width=1)
        planes_of = {it["item"]: it.get("plane", it.get("layout", {}).get("plane", REFERENCE_PLANE))
                     for it in (want or {}).get("cw", []) + (want or {}).get("slabs", []) + (want or {}).get("railings", [])}
        for kind, bodies_by, via_cam in extra or []:              # I5/I6: CW frames in the photo plane, slabs / railings through the camera
            for k, bodies in bodies_by.items():
                if planes_of.get(k, REFERENCE_PLANE) != REFERENCE_PLANE:
                    continue
                for svw, polys in bodies:
                    pp = to_photo(svw, cam) if via_cam else svw[:, :2]
                    for p in polys:
                        pts = [px(s_, v_) for s_, v_ in pp[p["vertexIds"]]]
                        dr.line(pts + [pts[0]], fill=COLORS[kind] + (150,), width=1)
        for faces in wall_faces.values():
            for outline, holes in faces:
                pts = [px(s, v) for s, v in outline]
                dr.line(pts + [pts[0]], fill=COLORS["wall"] + (255,), width=3)
                for h in holes:
                    hp = [px(s, v) for s, v in h]
                    dr.line(hp + [hp[0]], fill=COLORS["hole"] + (255,), width=2)
        for oid, u in ref_units.items():
            for o in [u["o"]] + u["members"]:
                s0, s1, v0_, v1_ = o["rect"]["v"]
                corners = [px(s0, v0_), px(s1, v0_), px(s1, v1_), px(s0, v1_)]
                for (xa, ya), (xb, yb) in zip(corners, corners[1:] + corners[:1]):
                    n = max(1, int(max(abs(xb - xa), abs(yb - ya)) / 6))
                    for k in range(0, n, 2):
                        t0, t1 = k / n, min(1.0, (k + 1) / n)
                        dr.line([(xa + (xb - xa) * t0, ya + (yb - ya) * t0), (xa + (xb - xa) * t1, ya + (yb - ya) * t1)],
                                fill=COLORS["spec"] + (255,), width=2)
        for oid, u in ref_units.items():
            s0, _, _, vv1 = u["rect"]
            x, y = px(s0, vv1)
            ids = "+".join([oid] + [m["id"] for m in u["members"]])
            wv = max([worst.get(i, float("nan")) for i in [oid] + [m["id"] for m in u["members"]]] or [float("nan")])
            pst = "; ".join(x for x in [(photo_status or {}).get(i) for i in [oid] + [m["id"] for m in u["members"]]] if x) or "neoverené voči fotke"
            label = f"{ids} {u['o']['kind'][0].upper()} model−spec Δ{wv:.1f} cm · {pst}"
            typ = _type_label(oid, trows)
            exc = "chýba" in typ
            for text, dy, col in ((label, -36 if typ else -19, COLORS["text"]), (typ, -19, COLORS["exc"] if exc else COLORS["spec"])):
                if text:
                    dr.text((x + 3, y + dy), text, fill=(0, 0, 0, 255), font=small, stroke_width=3, stroke_fill=(0, 0, 0, 255))
                    dr.text((x + 3, y + dy), text, fill=col + (255,), font=small)
        tagged = []
        for kind_, bodies_by in (("object", {k: v for k, v in (obj_proj or {}).items() if obj_planes.get(k, REFERENCE_PLANE) == REFERENCE_PLANE}),
                                 *[(kd, bb) for kd, bb, _ in (extra or []) if kd in ("slab", "railing")]):
            for k, bodies in bodies_by.items():
                if planes_of.get(k, REFERENCE_PLANE) != REFERENCE_PLANE or (photo_status or {}).get(k) or not bodies:
                    continue
                allp = np.vstack([b for b, _ in bodies])
                pp = to_photo(allp, cam) if kind_ == "object" else allp[:, :2]
                x, y = px(float(pp[:, 0].min()), float(pp[:, 1].max()))
                txt = f"{k}: neoverené voči fotke"
                dr.text((x + 3, y - 17), txt, fill=(0, 0, 0, 255), font=small, stroke_width=3, stroke_fill=(0, 0, 0, 255))
                dr.text((x + 3, y - 17), txt, fill=COLORS["exc"] + (255,), font=small)
                tagged.append(k)
        lines = [(f"{spec['id']}: model (3D projekcia) × rektifikovaná fotka", COLORS["text"]),
                 ("text otvoru: Δ = model − spec (3D); 'foto max' = kontrola voči fotke; 'neoverené voči fotke' = bez fotokontroly", COLORS["text"]),
                 ("červená: líce stien zón + otvory v stene (3D)", COLORS["wall"]),
                 ("modrá: okná · fialová: dvere (3D) · zelená: rímsy, pásy (3D cez kameru fotky)", COLORS["window"]),
                 ("oranžová: GDL objekty (šambrány, oblúkový štít) cez kameru fotky", COLORS["object"]),
                 ("azúrová: Curtain Wall výkladov (rámy, v rovine fotky) · bledozelená: balkónové dosky · ružová: zábradlia (cez kameru)", COLORS["cw"]),
                 ("žltá prerušovaná: spec (z fotky); žltý text: typ čítaný späť", COLORS["spec"])]
        if other:
            lines.append((f"roviny bez fotky (nie sú v obraze): {', '.join(other)}", COLORS["exc"]))
        y = 8                                                            # legend on top (sky / roof), the parter stays visible
        dr.rectangle([0, 0, 640, 22 * len(lines) + 14], fill=(0, 0, 0, 170))
        for text, col in lines:
            dr.text((8, y), text, fill=col + (255,), font=font)
            y += 22
        return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    overlay = paint(base)
    model = paint(Image.new("RGB", size, (70, 70, 70)))
    o_path = out_dir / f"overlay_{spec['id']}.png"
    m_path = out_dir / f"model_{spec['id']}.png"
    overlay.save(o_path)
    model.save(m_path)
    return o_path, m_path


def draw_edges(spec, src, image_path: Path, lin: dict, obj_proj: dict, edges: list, out_dir: Path, k: int = 3) -> Path:
    """Zoomed crops of the photo, one per checked edge: model through the camera (green / orange), the edge found in the
    photo (cyan) and the projected model edge (magenta); labels with delta. Linear edges are horizontal; opening and
    object edges horizontal (top / bottom) or vertical (left / right)."""
    r = src["rect"]
    S, u0, v1 = r["S"], r["u0"], r["v1"]
    cam = camera_of(spec)
    base = Image.open(image_path).convert("RGB")
    font = _font(14)
    crops = []
    items = [dict(e, side="h", s_rng=e["s"], at=e["v_model_projected"], photo=e["v_photo"]) for e in lin["photo"]]
    items += [dict(e, at=e["model_projected"]) for e in edges]
    for e in items:
        if e["side"] in ("h", "top", "bottom"):
            rng = e.get("s_rng") or e["range"]
            sa, sb, va, vb = rng[0] - 0.3, rng[1] + 0.3, e["at"] - 0.35, e["at"] + 0.35
        else:
            sa, sb, va, vb = e["at"] - 0.25, e["at"] + 0.25, e["range"][0] - 0.1, e["range"][1] + 0.1
        x0, x1 = max(0, int((sa - u0) * S)), min(base.size[0], int((sb - u0) * S))
        y0, y1 = max(0, int((v1 - vb) * S)), min(base.size[1], int((v1 - va) * S))
        if x1 <= x0 or y1 <= y0:
            continue
        im = base.crop((x0, y0, x1, y1)).resize(((x1 - x0) * k, (y1 - y0) * k), Image.LANCZOS)
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        P = lambda s_, v_: (((s_ - u0) * S - x0) * k, ((v1 - v_) * S - y0) * k)
        for bodies, col in ((lin["proj"].get(e["item"], []), COLORS["linear"]), (obj_proj.get(e["item"], []), COLORS["object"])):
            for svw, polys in bodies:
                pp = to_photo(svw, cam)
                for p in polys:
                    pts = [P(s_, v_) for s_, v_ in pp[p["vertexIds"]]]
                    dr.line(pts + [pts[0]], fill=col + (230,), width=2)
        for val, col in ((e["photo"], (0, 255, 255)), (e["at"], (255, 0, 255))):
            if e["side"] in ("h", "top", "bottom"):
                rng = e.get("s_rng") or e["range"]
                dr.line([P(rng[0], val), P(rng[1], val)], fill=col + (255,), width=3)
            else:
                dr.line([P(val, e["range"][0]), P(val, e["range"][1])], fill=col + (255,), width=3)
        t1 = f"{e['item']}: {e['edge']}"
        t2 = f"foto {e['photo']:.3f} · model (kamera) {e['at']:.3f} m · Δ {e['delta_cm']:+.1f} cm (tol {e['tol_cm']:.0f})"
        im2 = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
        w_t = max(im2.size[0], 8 * max(len(t1), len(t2)) // 1)
        w_t = min(w_t, max(im2.size[0], 420))
        tile = Image.new("RGB", (w_t, im2.size[1] + 40), (25, 25, 25))
        tile.paste(im2, (0, 40))
        dr2 = ImageDraw.Draw(tile)
        dr2.text((4, 3), t1[:70], fill=(255, 255, 255), font=font)
        ok = abs(e["delta_cm"]) <= e["tol_cm"]
        dr2.text((4, 21), t2, fill=(140, 255, 140) if ok else (255, 120, 120), font=font)
        crops.append(tile)
    legend = "zelená/oranžová: model cez kameru fotky · azúrová: hrana nájdená vo fotke · fialová: hrana modelu"
    # sheet: rows of crops
    width = 1800
    rows_, cur, cur_w = [], [], 0
    for c in crops:
        if cur and cur_w + c.size[0] + 6 > width:
            rows_.append(cur)
            cur, cur_w = [], 0
        cur.append(c)
        cur_w += c.size[0] + 6
    if cur:
        rows_.append(cur)
    h = sum(max(c.size[1] for c in rw) + 6 for rw in rows_) + 34
    sheet = Image.new("RGB", (max(width, max((sum(c.size[0] + 6 for c in rw) for rw in rows_), default=800)), h), (40, 40, 40))
    y = 0
    for rw in rows_:
        x = 0
        for c in rw:
            sheet.paste(c, (x, y))
            x += c.size[0] + 6
        y += max(c.size[1] for c in rw) + 6
    ImageDraw.Draw(sheet).text((6, y + 6), legend, fill=(255, 255, 255), font=font)
    path = out_dir / f"overlay_edges_{spec['id']}.png"
    sheet.save(path)
    old = out_dir / f"overlay_linear_{spec['id']}.png"           # I3 name: same sheet, kept for the existing tests / links
    sheet.save(old)
    return path


def report_md(spec, result, rows, trows) -> str:
    t = result["types"]
    pe = result["photo_edges"]
    ae = result["auto_edges"]
    lines = [f"# Kontrola fasády {spec['id']} (verify, {result['at']})", "",
             f"Výsledok: **{'PASS' if result['pass'] else 'FAIL'}** – tolerancia build {result['tolerance_cm']:.1f} cm"
             + (f"; **{len(result['exceptions'])} výnimky** (API/knižnica nedovolí, pozri nižšie)" if result["exceptions"] else ""), "",
             "| Kontrola | Spec | Model | OK |", "|---|---|---|---|",
             f"| steny (jedna na zónu) | {len(spec['zones'])} | {result['count']['walls']} | {'áno' if len(spec['zones']) == result['count']['walls'] else 'NIE'} |",
             f"| počet výplní v stenách zón (zlúčené otvory = 1 prvok) | {result['count']['spec']} | {result['count']['model_in_wall']} | {'áno' if result['count']['ok'] else 'NIE'} |",
             f"| max. Δ parametre (čítané späť) | – | {result['max_delta_cm']['params']:.2f} cm | {'áno' if result['max_delta_cm']['params'] <= result['tolerance_cm'] else 'NIE'} |",
             f"| max. Δ 3D projekcia (prvky + otvory v stenách) | – | {result['max_delta_cm']['3D']:.2f} cm | {'áno' if result['max_delta_cm']['3D'] <= result['tolerance_cm'] else 'NIE'} |",
             f"| typy, skladby, favority, šablóny, vlastnosti (riadky) | {t['rows']} | OK {t['ok']} / FAIL {t['fail']} / výnimka {t['exc']} | {'áno' if not t['fail'] else 'NIE'} |",
             f"| lineárne prvky / objekty na vrstve fasády | {result['count'].get('linear_spec')} | {result['count'].get('linear_model')} | "
             f"{'áno' if result['count'].get('linear_spec') == result['count'].get('linear_model') else 'NIE'} |",
             f"| CW / balkónové dosky / zábradlia na vrstve fasády (I5/I6) | {result['count'].get('i6_spec')} | {result['count'].get('i6_model')} | "
             f"{'áno' if result['count'].get('i6_spec') == result['count'].get('i6_model') else 'NIE'} |",
             f"| hrany × fotka (pomenované v spec) | {len(pe)} | v tolerancii {sum(abs(e['delta_cm']) <= e['tol_cm'] for e in pe)} | "
             f"{'áno' if all(abs(e['delta_cm']) <= e['tol_cm'] for e in pe) else 'NIE'} |",
             f"| hrany otvorov × fotka automaticky (meranie, informatívne) | {len(ae)} | OK {sum(e['status'] == 'OK' for e in ae)} / mimo "
             f"{sum(e['status'] == 'MIMO' for e in ae)} / nejednoznačné (rám × ostenie) {sum(e['status'] == 'nejednoznačné' for e in ae)} / "
             f"nečitateľné {sum(e['status'] == 'nečitateľné' for e in ae)} | – |",
             "", f"Overlay: `{result['overlay_png']}`  ", f"Model bez fotky: `{result['model_png']}`  ",
             f"Hrany × fotka (výrezy): `{result.get('edges_png')}`  ",
             f"Odchýlky po prvkoch: `{result['deviations_csv']}`" + (f"  \nTypy: `{result['types_csv']}`" if result.get("types_csv") else ""), ""]
    if result["planes_without_photo"]:
        lines += [f"Roviny bez rektifikovanej fotky (overlay voči fotke nevykonateľný, vykázané): {', '.join(result['planes_without_photo'])}", ""]
    lines += ["## Zóny stien", "", "| Zóna | Rovina | Úloha | s | v | w líca | hrúbka | systém |", "|---|---|---|---|---|---|---|---|"]
    for z in spec["zones"]:
        lines.append(f"| {z['id']} | {z.get('plane', 'F')} | {z.get('role', '')} | {z['rect']['v'][0]}–{z['rect']['v'][1]} | "
                     f"{z['rect']['v'][2]}–{z['rect']['v'][3]} | {zone_w(z):+.3f} | {z['thickness']['v']} | {z['system']} |")
    lines.append("")
    if pe:
        lines += ["## Hrany × fotka (projekcia cez kameru fotky)", "",
                  "| Prvok | Hrana | Foto m | Foto (ručne) m | Model m | Δ cm | Tol cm |", "|---|---|---|---|---|---|---|"]
        for e in pe:
            ph = e.get("v_photo", e.get("photo"))
            hand = e.get("v_photo_hand", e.get("photo_hand"))
            md = e.get("v_model_projected", e.get("model_projected"))
            lines.append(f"| {e['item']} | {e['edge']} | {ph} | {hand if hand is not None else '–'} | {md} | {e['delta_cm']:+.1f} | {e['tol_cm']:.0f} |")
        lines.append("")
    if ae:
        lines += ["## Hrany otvorov × fotka – automaticky (najsilnejší skok jasu ±5 cm; na strane viditeľného ostenia pár "
                  "murovaná hrana × rám – paralaxa; informatívne)", "",
                  "| Otvor | Hrana | Model m | Foto m | Δ cm | Kontrast | Ostenie cm | Stav | Poznámka |", "|---|---|---|---|---|---|---|---|---|"]
        for e in ae:
            lines.append(f"| {e['item']} | {e['edge']} | {e['model']} | {e['photo']} | {e['delta_cm']:+.1f} | {e['contrast']} | "
                         f"{e.get('reveal_cm', '–')} | {e['status']} | {e.get('note', '')} |")
        lines.append("")
    if result["problems"]:
        lines += ["## Problémy", ""] + [f"- {p}" for p in result["problems"]] + [""]
    if result["exceptions"]:
        lines += ["## Výnimky (spec chce, model nevie – vykázané, nie zamlčané)", ""]
        lines += [f"- `{e.get('item', e['system'])}.{e['field']}` = {json.dumps(e['spec'], ensure_ascii=False)}: {e['reason']}" for e in result["exceptions"]] + [""]
    if result["spec_warnings"]:
        lines += ["## Upozornenia zo spec", ""] + [f"- {w}" for w in result["spec_warnings"]] + [""]
    if spec.get("simplifications"):
        lines += ["## Zjednodušenia a vykázané prvky pilota", ""] + [f"- `{s['ref']}`: {s['text']}" for s in spec["simplifications"]] + [""]
    if trows:
        lines += ["## Typy výplní, skladby, favority, šablóny, vlastnosti (čítané späť)", "", "| Prvok | Čo | Spec | Model | Stav | Pozn. |", "|---|---|---|---|---|---|"]
        for r in trows:
            lines.append(f"| {r['item']} | {r['what']} | {r['spec']} | {r['model']} | {r['status']} | {r['note']} |")
        lines.append("")
    lines += ["## Predpoklady (pod úrovňou `meranie`, na bránu G1)", "", f"Tabuľka s návrhmi: `G1_predpoklady.md`", "",
              "| Hodnota | Stav | Istota | Prečo |", "|---|---|---|---|"]
    for a in spec.get("assumptions", []):
        lines.append(f"| `{a['ref']}` = {json.dumps(a['value'], ensure_ascii=False)} | {a['status']} | {a.get('conf', '')} | {a['why']} |")
    lines += ["", "## Odchýlky", "", "| Prvok | Veličina | Spec m | Model m | Δ cm | Metóda | OK |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['item']} | {r['quantity']} | {r['spec_m']} | {r['model_m']} | {r['delta_cm']} | {r['method']} | {'áno' if r['ok'] else 'NIE'} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--project", default=DEFAULT_PROJECT)
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    return 0 if verify(spec, args.project, args.port, args.out)["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

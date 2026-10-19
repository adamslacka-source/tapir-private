"""verify: read the built facade back from Archicad and compare it with the spec (ADR-001 D6).

(a) numbers  - element parameters read back (GetDetailsOfElements, GDL wall-hole size) -> s/v rects
(b) geometry - 3D bodies (Get3DGeometryOfElements) projected orthogonally on the facade plane:
               bbox of every opening element, outline and holes of the wall's outside face (incl. the
               L-shaped hole of a door whose sidelight stands on a wall parapet)
(c) types    - I2: fill types read back through gdl_param_map.json (leaves, transom, sidelight, glazing
               bars, frame surface, instance values, wall inset), the wall build-up (Composite), the FS
               favourites and the FS_* properties of every element. A field the library part cannot carry is
               an EXCEPTION (listed, never passed silently).
(d) image    - the projection drawn over the rectified photo of the source (overlay PNG)
(e) linear   - I3: cornice, string course, sill ledges, surrounds (linear_verify.py): parameters, 3D of the beam /
               column segments, profile area, and their edges projected with the camera of the photo vs the photo
               (overlay_linear_<id>.png = zoomed crops)

Outputs to <out>/<spec.id>/: deviations.csv, types.csv, overlay_<id>.png, model_<id>.png, report.json, report.md.
Read-only in Archicad (still refuses to run on another project than --project).

    python verify.py <spec.json> [--project ...] [--port 19723] [--out C:/FacadePilot/out]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import systems as fs_systems  # noqa: E402
from build import DEFAULT_OUT, DEFAULT_PROJECT, gdl_params, opening_units, plan, state_path  # noqa: E402
from geom import frame_of  # noqa: E402
from linear_verify import count_on_layer, to_photo, camera_of, verify_linear  # noqa: E402
from spec_validate import TYPE_KEYS, fs_system, load_gdl_map, validate  # noqa: E402
from tapir_client import TapirClient, TapirError  # noqa: E402

FACE_TOL = 0.005          # m - a face lies in the outside plane
PROTRUSION = 0.10         # m - allowed overhang of sills / boards / handles beyond a wall face
COLORS = {"wall": (230, 30, 30), "hole": (230, 30, 30), "window": (0, 200, 255), "door": (255, 0, 200), "linear": (60, 255, 60),
          "spec": (255, 230, 0), "text": (255, 255, 255), "shadow": (0, 0, 0), "exc": (255, 140, 0)}
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


def _poly_area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _fmt(v):
    return round(v, 4) if isinstance(v, float) else v


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
    fr = frame_of(spec)
    tol = spec["tolerances"]["build"]
    zone = spec["zones"][0]
    zs0, zs1, zv0, zv1 = zone["rect"]["v"]
    stories = {s["index"]: s["level"] for s in ac.tapir("GetStories")["stories"]}
    i2 = any(fs_system(s) for s in spec["systems"].values())
    sysinfo = fs_systems.resolve(ac, spec, path, out_root) if i2 else None
    want = plan(spec, stories, sysinfo)
    units = {u["o"]["id"]: u for u in opening_units(spec)}
    gmap = load_gdl_map()
    reverse = spec["reference"]["outside"] == "right_of_ab"
    beg_s = zs1 if reverse else zs0
    known = state["elements"]
    problems: list[str] = []
    rows: list[dict] = []
    trows: list[dict] = []
    exceptions: list[dict] = list(sysinfo["exceptions"]) if sysinfo else []
    exceptions += want.get("exceptions", [])

    def row(item, quantity, spec_v, model_v, method, tolerance=tol):
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
    wall_item = want["wall"]["item"]
    wall_guid = guids.get(wall_item)

    # independent count: every window/door in the project hosted by our wall (one element per unit)
    hosted = {"window": 0, "door": 0}
    for typ in ("Window", "Door"):
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["details"]})["detailsOfElements"]
            hosted[typ.lower()] = sum(1 for d in dd if d.get("details", {}).get("ownerElementId", {}).get("guid") == wall_guid)
    n_spec = {"window": sum(u["o"]["kind"] == "window" for u in units.values()), "door": sum(u["o"]["kind"] == "door" for u in units.values())}
    count_ok = hosted == n_spec
    if not count_ok:
        problems.append(f"opening count in the wall {hosted} != spec {n_spec} (merged openings count once)")

    # ---- (a) parameters read back
    layer_index = state.get("layer", {}).get("index")
    for item in [want["wall"]] + want["openings"] + want["linear"]:
        d = details.get(item["item"])
        if not d:
            continue
        if d["layerIndex"] != layer_index:
            problems.append(f"{item['item']}: layer index {d['layerIndex']} != {layer_index} ({spec['reference']['layer']})")
        if d["id"] != item["element_id"]:
            problems.append(f"{item['item']}: Element ID '{d['id']}' != '{item['element_id']}'")
    wd = details.get(wall_item, {}).get("details")
    if wd:
        floor = details[wall_item]["floorIndex"]
        foot_v = stories[floor] + wd["bottomOffset"] - fr.z0
        beg = np.array([wd["begCoordinate"]["x"], wd["begCoordinate"]["y"], 0.0])
        end = np.array([wd["endCoordinate"]["x"], wd["endCoordinate"]["y"], 0.0])
        sb, se = fr.to_svw(np.vstack([beg, end]))[:, [0, 2]]
        row(wall_item, "s0", zs0, float(min(sb[0], se[0])), "params")
        row(wall_item, "s1", zs1, float(max(sb[0], se[0])), "params")
        row(wall_item, "w of reference line", 0.0, float(max(abs(sb[1]), abs(se[1]))), "params")
        row(wall_item, "v0 (foot)", zv0, foot_v, "params")
        row(wall_item, "v1 (top)", zv1, foot_v + wd["height"], "params")
        row(wall_item, "thickness", zone["thickness"]["v"], wd["begThickness"], "params")
        if floor != spec["reference"]["home_story"]["floorIndex"]:
            problems.append(f"wall home story {floor} != spec {spec['reference']['home_story']['floorIndex']}")
    unit_guids = [guids[i] for i in units if i in details]
    gdl = gdl_params(ac, unit_guids)
    for oid, u in units.items():
        d = details.get(oid)
        if not d or not wd:
            continue
        od = d["details"]
        if od.get("ownerElementId", {}).get("guid") != wall_guid:
            problems.append(f"{oid}: owner is not the wall {wall_guid}")
        s0, s1, v0, v1 = u["rect"]
        sc = (beg_s - od["centerOffset"]) if reverse else (beg_s + od["centerOffset"])
        p = gdl.get(guids[oid], {})
        hole_w = p.get("ac_wallhole_width", od["width"])
        hole_h = p.get("ac_wallhole_height", od["height"])
        bottom = foot_v + od["sillHeight"] - (p.get("ac_lower_oversize") or 0.0)
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

    # ---- (b) 3D geometry projected on the facade plane
    order = [wall_item] + list(units)                     # linear elements: linear_verify (segments)
    present = [i for i in order if i in details]
    geos = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": guids[i]}} for i in present]}, timeout=120)["geometries3D"]
    proj = {i: _bodies_svw(g, fr) for i, g in zip(present, geos)}
    wall_faces = []
    if wall_item in proj:
        wb = _bbox(proj[wall_item])
        row(wall_item, "s0", zs0, wb[0], "3D")
        row(wall_item, "s1", zs1, wb[1], "3D")
        row(wall_item, "v0", zv0, wb[2], "3D")
        row(wall_item, "v1", zv1, wb[3], "3D")
        row(wall_item, "w outside face", 0.0, wb[5], "3D")
        row(wall_item, "w inside face", -zone["thickness"]["v"], wb[4], "3D")
        wall_faces = _outside_faces(proj[wall_item], 0.0)
        if not wall_faces:
            problems.append("wall: no face in the outside plane w = 0 (wall on the wrong side?)")
        holes = [h for _, hs in wall_faces for h in hs]
        hole_area = sum(_poly_area(h) for h in holes)
        spec_area = 0.0
        for u in units.values():
            s0, s1, v0, v1 = u["rect"]
            area = (s1 - s0) * (v1 - v0)
            if "sidelight_w" in u["values"] and "sidelight_sill" in u["values"]:
                area -= u["values"]["sidelight_w"]["v"] * u["values"]["sidelight_sill"]["v"]      # wall parapet under the sidelight
            spec_area += area
        row(wall_item, HOLE_AREA, spec_area, hole_area, "3D", tolerance=0.05)
        # every unit has its own hole: bbox = unit rect; an L-hole has its step at the sidelight edge / parapet
        for oid, u in units.items():
            s0, s1, v0, v1 = u["rect"]
            mine = [h for h in holes if h[:, 0].min() < s1 and h[:, 0].max() > s0 and h[:, 1].min() < v1 and h[:, 1].max() > v0]
            if len(mine) != 1:
                problems.append(f"{oid}: {len(mine)} holes in the wall face over the opening (expected 1)")
                continue
            h = mine[0]
            row(oid, "hole s0", s0, float(h[:, 0].min()), "3D")
            row(oid, "hole s1", s1, float(h[:, 0].max()), "3D")
            row(oid, "hole v0", v0, float(h[:, 1].min()), "3D")
            row(oid, "hole v1", v1, float(h[:, 1].max()), "3D")
            if "sidelight_w" in u["values"] and u["values"].get("sidelight_sill", {}).get("v", 0) > 0:
                step = np.array([s1 - u["values"]["sidelight_w"]["v"], v0 + u["values"]["sidelight_sill"]["v"]])
                dist = float(np.min(np.linalg.norm(h - step, axis=1)))
                label = u["members"][0]["id"] if u["members"] else oid
                row(label, "hole step (sidelight parapet corner)", 0.0, dist, "3D")
    w_ranges = {}
    for oid, u in units.items():
        if oid not in proj:
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
        # sills, boards and handles may stand a little out of the wall faces; a whole unit outside is an error
        if bb[5] > PROTRUSION or bb[4] < -zone["thickness"]["v"] - PROTRUSION:
            problems.append(f"{oid}: 3D body sticks out of the wall by more than {PROTRUSION} m (w {bb[4]:.3f}..{bb[5]:.3f})")

    # ---- (e) linear elements (I3)
    src = next(s for s in spec["sources"] if s.get("rect"))
    root = Path(source_root or spec.get("generated", {}).get("root", "."))
    lum = None
    if want["linear"]:
        lum = np.asarray(Image.open(root / src["rect"]["image"]).convert("L"), dtype=float)
    lin = verify_linear(ac, spec, want["linear"], guids, details, fr, stories, sysinfo, lum, row, trow, problems)
    n_lin_spec = {"beam": sum(it["kind"] == "beam" for it in want["linear"]), "column": sum(it["kind"] == "column" for it in want["linear"])}
    n_lin_model = count_on_layer(ac, layer_index) if want["linear"] else {"beam": 0, "column": 0}
    if n_lin_model != n_lin_spec:
        problems.append(f"linear elements on the facade layer {n_lin_model} != spec {n_lin_spec}")

    # ---- (c) types, build-up, favourites, properties (I2)
    if sysinfo:
        attrs = sysinfo["attrs"]
        exc_keys = {(e.get("item"), e["field"]) for e in exceptions} | {(e["system"], e["field"]) for e in exceptions if "item" not in e}
        for oid, u in units.items():
            g = guids.get(oid)
            if g not in gdl:
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
        # wall build-up
        wsys = spec["systems"][zone["system"]]
        if "composite" in wsys and wd:
            trow(wall_item, "structure", "Composite", wd.get("structureType"))
            trow(wall_item, "composite", wsys["composite"]["name"],
                 next((n for n, a in attrs.composite.items() if a["attributeId"]["guid"] == (wd.get("compositeId") or {}).get("guid")), None))
            diffs = fs_systems.composite_differences(ac, wsys["composite"], attrs)
            trow(wall_item, "composite skins (material, thickness, order outside -> inside)",
                 " | ".join(f"{sk['material']} {sk['thickness']['v']}" for sk in wsys["composite"]["skins"]),
                 "as spec" if not diffs else "; ".join(diffs), "OK" if not diffs else "FAIL")
            trow(wall_item, "surfaces from the skins (no override)", False,
                 bool(wd.get("referenceMaterial", {}).get("overridden") or wd.get("oppositeMaterial", {}).get("overridden")))
        # FS favourites exist
        kind_of = lambda s: {"linear": s.get("tool", "")}.get(s["type"], s["type"])
        for kind, names in [(fs_systems.FAV_TYPES[k], [s["favorite"] for s in spec["systems"].values() if fs_system(s) and kind_of(s) == k])
                            for k in ("wall", "window", "door", "beam", "column")]:
            if names:
                have = set(ac.tapir("GetFavoritesByType", {"elementType": kind}).get("favorites", []))
                for n in names:
                    trow("favorites", f"{kind} favourite '{n}' exists", True, n in have)
        # properties FS_* on every element
        props = sysinfo["props"]
        names = list(fs_systems.FS_PROPS)
        if len(props) != len(names):
            problems.append(f"FS properties missing: {sorted(set(names) - set(props))}")
        else:
            items = [it for it in [want["wall"]] + want["openings"] + want["linear"] if it["item"] in guids]
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
    overlay_png, model_png = draw(spec, src, root / src["rect"]["image"], proj, wall_faces, rows, trows, units, out_dir, lin["proj"])
    linear_png = draw_linear(spec, src, root / src["rect"]["image"], lin, out_dir) if want["linear"] else None

    geo_rows = [r for r in rows if r["delta_cm"] is not None and r["quantity"] != HOLE_AREA and r["method"] in ("params", "3D")]
    max_delta = {m: max([abs(r["delta_cm"]) for r in geo_rows if r["method"] == m] or [0.0]) for m in ("params", "3D")}
    result = {
        "spec": spec["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.monotonic() - t0, 2),
        "count": {"spec": n_spec, "model_in_wall": hosted, "ok": count_ok, "linear_spec": n_lin_spec, "linear_model": n_lin_model},
        "linear": {"items": len(want["linear"]), "photo_edges": lin["photo"],
                   "max_delta_cm": {m: max([abs(r["delta_cm"]) for r in rows if r["method"] == m and r["item"] in {it["item"] for it in want["linear"]}
                                            and r["delta_cm"] is not None] or [0.0]) for m in ("params", "3D", "photo")},
                   "overlay_png": str(linear_png) if linear_png else None},
        "max_delta_cm": max_delta, "tolerance_cm": tol * 100,
        "rows_failed": sum(not r["ok"] for r in rows), "rows": len(rows),
        "types": {"rows": len(trows), "ok": sum(r["status"] == "OK" for r in trows),
                  "fail": sum(r["status"] == "FAIL" for r in trows), "exc": sum(r["status"] == "EXC" for r in trows)},
        "problems": problems, "exceptions": exceptions, "spec_warnings": warnings,
        "opening_w_range_m": w_ranges,
        "overlay_png": str(overlay_png), "model_png": str(model_png), "deviations_csv": str(out_dir / "deviations.csv"),
        "types_csv": str(out_dir / "types.csv") if trows else None,
        "pass": count_ok and not problems,
    }
    (out_dir / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report.md").write_text(report_md(spec, result, rows, trows), encoding="utf-8")
    if verbose:
        print(f"verify {spec['id']}: openings model {hosted} / spec {n_spec}; max delta params {max_delta['params']:.2f} cm, "
              f"3D {max_delta['3D']:.2f} cm (tol {tol * 100:.1f} cm); {result['rows_failed']}/{len(rows)} rows out of tolerance; "
              f"types {result['types']}; {'PASS' if result['pass'] else 'FAIL'} ({result['seconds']} s)")
        for p in problems:
            print("  PROBLEM:", p)
        for e in exceptions:
            print(f"  EXCEPTION: {e.get('item', e['system'])}.{e['field']}: spec {e['spec']!r} - not realisable ({e['reason'][:80]})")
        print("  overlay:", overlay_png)
    return result


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


def draw(spec, src, image_path: Path, proj, wall_faces, rows, trows, units, out_dir: Path, lin_proj=None):
    r = src["rect"]
    S, u0, v1 = r["S"], r["u0"], r["v1"]
    px = lambda s, v: ((s - u0) * S, (v1 - v) * S)
    base = Image.open(image_path).convert("RGB")
    size = base.size
    try:
        font = ImageFont.truetype("arial.ttf", 16)
        small = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font = small = ImageFont.load_default()

    kinds = {oid: u["o"]["kind"] for oid, u in units.items()}
    worst = {}
    for rr in rows:
        if rr["method"] == "3D" and rr["delta_cm"] is not None and rr["quantity"] != HOLE_AREA:
            worst[rr["item"]] = max(worst.get(rr["item"], 0.0), abs(rr["delta_cm"]))

    def paint(canvas: Image.Image) -> Image.Image:
        img = canvas.copy()
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        # opening bodies: every face edge, orthographic (depth ignored), thin
        for oid, kind in kinds.items():
            for svw, polys in proj.get(oid, []):
                for p in polys:
                    ring = svw[p["vertexIds"]]
                    pts = [px(s, v) for s, v in ring[:, :2]]
                    dr.line(pts + [pts[0]], fill=COLORS[kind] + (150,), width=1)
        # linear elements (I3): every face edge projected with the camera of the photo (as the photo sees them)
        cam = camera_of(spec)
        for lid, bodies in (lin_proj or {}).items():
            for svw, polys in bodies:
                pp = to_photo(svw, cam)
                for p in polys:
                    pts = [px(s_, v_) for s_, v_ in pp[p["vertexIds"]]]
                    dr.line(pts + [pts[0]], fill=COLORS["linear"] + (200,), width=1)
        # wall outside face: outline and holes
        for outline, holes in wall_faces:
            pts = [px(s, v) for s, v in outline]
            dr.line(pts + [pts[0]], fill=COLORS["wall"] + (255,), width=3)
            for h in holes:
                hp = [px(s, v) for s, v in h]
                dr.line(hp + [hp[0]], fill=COLORS["hole"] + (255,), width=2)
        # spec rects, dashed
        for o in spec["openings"]:
            s0, s1, v0_, v1_ = o["rect"]["v"]
            corners = [px(s0, v0_), px(s1, v0_), px(s1, v1_), px(s0, v1_)]
            for (xa, ya), (xb, yb) in zip(corners, corners[1:] + corners[:1]):
                n = max(1, int(max(abs(xb - xa), abs(yb - ya)) / 6))
                for k in range(0, n, 2):
                    t0, t1 = k / n, min(1.0, (k + 1) / n)
                    dr.line([(xa + (xb - xa) * t0, ya + (yb - ya) * t0), (xa + (xb - xa) * t1, ya + (yb - ya) * t1)],
                            fill=COLORS["spec"] + (255,), width=2)
        for oid, u in units.items():
            s0, _, _, vv1 = u["rect"]
            x, y = px(s0, vv1)
            ids = "+".join([oid] + [m["id"] for m in u["members"]])
            wv = max([worst.get(i, float("nan")) for i in [oid] + [m["id"] for m in u["members"]]] or [float("nan")])
            label = f"{ids} {u['o']['kind'][0].upper()} Δ{wv:.1f} cm"
            typ = _type_label(oid, trows)
            exc = "chýba" in typ
            for text, dy, col in ((label, -36 if typ else -19, COLORS["text"]), (typ, -19, COLORS["exc"] if exc else COLORS["spec"])):
                if text:
                    dr.text((x + 3, y + dy), text, fill=(0, 0, 0, 255), font=small, stroke_width=3, stroke_fill=(0, 0, 0, 255))
                    dr.text((x + 3, y + dy), text, fill=col + (255,), font=small)
        # legend
        lines = [(f"{spec['id']}: model (3D projekcia) × rektifikovaná fotka", COLORS["text"]),
                 ("červená: líce steny + otvory v stene (3D)", COLORS["wall"]),
                 ("modrá: okná (3D) · fialová: dvere (3D) · zelená: rímsy, šambrány (3D cez kameru fotky)", COLORS["window"]),
                 ("žltá prerušovaná: spec (z fotky); žltý text: typ čítaný späť", COLORS["spec"]),
                 ("kr krídla · NS nadsvetlík · BS bočný svetlík · osadenie m", COLORS["text"])]
        y = size[1] - 22 * len(lines) - 8
        dr.rectangle([0, y - 6, 560, size[1]], fill=(0, 0, 0, 170))
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


def draw_linear(spec, src, image_path: Path, lin: dict, out_dir: Path, k: int = 4) -> Path:
    """Zoomed crops of the photo with the linear elements projected through the camera (green), the edges found in the
    photo (cyan) and the projected model edges (magenta), one crop per checked edge, stacked; labels with delta."""
    r = src["rect"]
    S, u0, v1 = r["S"], r["u0"], r["v1"]
    cam = camera_of(spec)
    base = Image.open(image_path).convert("RGB")
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    crops = []
    for e in lin["photo"]:
        sa, sb = e["s"][0] - 0.3, e["s"][1] + 0.3
        vm = e["v_model_projected"]
        va, vb = vm - 0.35, vm + 0.35
        x0, x1 = int((sa - u0) * S), int((sb - u0) * S)
        y0, y1 = int((v1 - vb) * S), int((v1 - va) * S)
        im = base.crop((x0, y0, x1, y1)).resize(((x1 - x0) * k, (y1 - y0) * k), Image.LANCZOS)
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        P = lambda s_, v_: (((s_ - u0) * S - x0) * k, ((v1 - v_) * S - y0) * k)
        for svw, polys in lin["proj"].get(e["item"], []):
            pp = to_photo(svw, cam)
            for p in polys:
                pts = [P(s_, v_) for s_, v_ in pp[p["vertexIds"]]]
                dr.line(pts + [pts[0]], fill=COLORS["linear"] + (230,), width=2)
        for v_, col in ((e["v_photo"], (0, 255, 255)), (vm, (255, 0, 255))):
            dr.line([P(e["s"][0], v_), P(e["s"][1], v_)], fill=col + (255,), width=3)
        txt = (f"{e['item']} {e['edge']}: foto {e['v_photo']:.3f} m, model (kamera) {vm:.3f} m, "
               f"Δ {e['delta_cm']:+.1f} cm (tol {e['tol_cm']:.0f})")
        dr.rectangle([0, 0, im.size[0], 28], fill=(0, 0, 0, 170))
        dr.text((6, 4), txt, fill=(255, 255, 255, 255), font=font)
        crops.append(Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB"))
    legend = "zelená: model cez kameru fotky · azúrová: hrana nájdená vo fotke · fialová: hrana modelu"
    w = max(c.size[0] for c in crops) if crops else 800
    h = sum(c.size[1] + 6 for c in crops) + 34
    sheet = Image.new("RGB", (w, h), (40, 40, 40))
    y = 0
    for c in crops:
        sheet.paste(c, (0, y))
        y += c.size[1] + 6
    ImageDraw.Draw(sheet).text((6, y + 6), legend, fill=(255, 255, 255), font=font)
    path = out_dir / f"overlay_linear_{spec['id']}.png"
    sheet.save(path)
    return path


def report_md(spec, result, rows, trows) -> str:
    t = result["types"]
    lines = [f"# Kontrola fasády {spec['id']} (verify, {result['at']})", "",
             f"Výsledok: **{'PASS' if result['pass'] else 'FAIL'}** – tolerancia build {result['tolerance_cm']:.1f} cm"
             + (f"; **{len(result['exceptions'])} výnimky** (API/knižnica nedovolí, pozri nižšie)" if result["exceptions"] else ""), "",
             "| Kontrola | Spec | Model | OK |", "|---|---|---|---|",
             f"| počet výplní v stene (zlúčené otvory = 1 prvok) | {result['count']['spec']} | {result['count']['model_in_wall']} | {'áno' if result['count']['ok'] else 'NIE'} |",
             f"| max. Δ parametre (čítané späť) | – | {result['max_delta_cm']['params']:.2f} cm | {'áno' if result['max_delta_cm']['params'] <= result['tolerance_cm'] else 'NIE'} |",
             f"| max. Δ 3D projekcia (prvky + otvory v stene) | – | {result['max_delta_cm']['3D']:.2f} cm | {'áno' if result['max_delta_cm']['3D'] <= result['tolerance_cm'] else 'NIE'} |",
             f"| typy, skladba, favority, vlastnosti (riadky) | {t['rows']} | OK {t['ok']} / FAIL {t['fail']} / výnimka {t['exc']} | {'áno' if not t['fail'] else 'NIE'} |",
             f"| lineárne prvky na vrstve fasády (nosníky / stĺpy) | {result['count'].get('linear_spec')} | {result['count'].get('linear_model')} | "
             f"{'áno' if result['count'].get('linear_spec') == result['count'].get('linear_model') else 'NIE'} |",
             "", f"Overlay: `{result['overlay_png']}`  ", f"Model bez fotky: `{result['model_png']}`  ",
             f"Lineárne prvky × fotka (výrezy): `{result['linear'].get('overlay_png')}`  ",
             f"Odchýlky po prvkoch: `{result['deviations_csv']}`" + (f"  \nTypy: `{result['types_csv']}`" if result.get("types_csv") else ""), ""]
    if result["linear"]["photo_edges"]:
        lines += ["## Hrany lineárnych prvkov × fotka (projekcia cez kameru fotky)", "",
                  "| Prvok | Hrana | Foto (auto) m | Foto (ručne) m | Model m | Δ cm | Tol cm |", "|---|---|---|---|---|---|---|"]
        for e in result["linear"]["photo_edges"]:
            lines.append(f"| {e['item']} | {e['edge']} | {e['v_photo']} | {e['v_photo_hand'] if e['v_photo_hand'] is not None else '–'} | "
                         f"{e['v_model_projected']} | {e['delta_cm']:+.1f} | {e['tol_cm']:.0f} |")
        lines.append("")
    if result["problems"]:
        lines += ["## Problémy", ""] + [f"- {p}" for p in result["problems"]] + [""]
    if result["exceptions"]:
        lines += ["## Výnimky (spec chce, model nevie – vykázané, nie zamlčané)", ""]
        lines += [f"- `{e.get('item', e['system'])}.{e['field']}` = {json.dumps(e['spec'], ensure_ascii=False)}: {e['reason']}" for e in result["exceptions"]] + [""]
    if result["spec_warnings"]:
        lines += ["## Upozornenia zo spec", ""] + [f"- {w}" for w in result["spec_warnings"]] + [""]
    if trows:
        lines += ["## Typy výplní, skladba, favority, vlastnosti (čítané späť)", "", "| Prvok | Čo | Spec | Model | Stav | Pozn. |", "|---|---|---|---|---|---|"]
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

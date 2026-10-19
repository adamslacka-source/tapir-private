"""verify: read the built facade back from Archicad and compare it with the spec (ADR-001 D6).

(a) numbers  - element parameters read back (GetDetailsOfElements, GDL wall-hole size) -> s/v rects
(b) geometry - 3D bodies (Get3DGeometryOfElements) projected orthogonally on the facade plane:
               bbox of every opening element, outline and holes of the wall's outside face
(c) image    - the projection drawn over the rectified photo of the source (overlay PNG)

Outputs to <out>/<spec.id>/: deviations.csv, overlay_<id>.png, model_<id>.png, report.json, report.md.
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
from build import DEFAULT_OUT, DEFAULT_PROJECT, plan, state_path  # noqa: E402
from geom import frame_of  # noqa: E402
from spec_validate import validate  # noqa: E402
from tapir_client import TapirClient, TapirError  # noqa: E402

FACE_TOL = 0.005          # m - a face lies in the outside plane
PROTRUSION = 0.10         # m - allowed overhang of sills / boards / handles beyond a wall face
COLORS = {"wall": (230, 30, 30), "hole": (230, 30, 30), "window": (0, 200, 255), "door": (255, 0, 200),
          "spec": (255, 230, 0), "text": (255, 255, 255), "shadow": (0, 0, 0)}


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


def verify(spec: dict, project: str = DEFAULT_PROJECT, port: int = 19723, out_root: Path = DEFAULT_OUT,
           source_root: Path | None = None, verbose: bool = True) -> dict:
    t0 = time.monotonic()
    errors, warnings = validate(spec)
    if errors:
        raise TapirError("spec is invalid: " + "; ".join(errors))
    out_dir = out_root / spec["id"]
    state = json.loads(state_path(spec, out_root).read_text(encoding="utf-8"))
    ac = TapirClient(port=port, timeout=90)
    ac.guard_project(project)
    fr = frame_of(spec)
    tol = spec["tolerances"]["build"]
    zone = spec["zones"][0]
    zs0, zs1, zv0, zv1 = zone["rect"]["v"]
    stories = {s["index"]: s["level"] for s in ac.tapir("GetStories")["stories"]}
    want = plan(spec, stories)
    reverse = spec["reference"]["outside"] == "right_of_ab"
    beg_s = zs1 if reverse else zs0
    known = state["elements"]
    problems: list[str] = []
    rows: list[dict] = []

    def row(item, quantity, spec_v, model_v, method, tolerance=tol):
        d = None if model_v is None else model_v - spec_v
        ok = d is not None and abs(d) <= tolerance + 1e-9
        rows.append({"item": item, "quantity": quantity, "spec_m": round(spec_v, 4),
                     "model_m": None if model_v is None else round(model_v, 4),
                     "delta_cm": None if d is None else round(d * 100, 2), "tol_cm": round(tolerance * 100, 2),
                     "method": method, "ok": ok})
        if not ok:
            problems.append(f"{item} {quantity} ({method}): spec {spec_v:.3f}, model {model_v}")

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

    # independent count: every window/door in the project hosted by our wall
    hosted = {"window": 0, "door": 0}
    for typ in ("Window", "Door"):
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["details"]})["detailsOfElements"]
            hosted[typ.lower()] = sum(1 for d in dd if d.get("details", {}).get("ownerElementId", {}).get("guid") == wall_guid)
    n_spec = {"window": sum(o["kind"] == "window" for o in spec["openings"]), "door": sum(o["kind"] == "door" for o in spec["openings"])}
    count_ok = hosted == n_spec
    if not count_ok:
        problems.append(f"opening count in the wall {hosted} != spec {n_spec}")

    # ---- (a) parameters read back
    layer_index = state.get("layer", {}).get("index")
    for item in [want["wall"]] + want["openings"]:
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
    opening_guids = [guids[o["id"]] for o in spec["openings"] if o["id"] in details]
    gdl = {}
    if opening_guids:
        res = ac.tapir("GetGDLParametersOfElements", {"elements": [{"elementId": {"guid": g}} for g in opening_guids]})
        for g, ps in zip(opening_guids, res["gdlParametersOfElements"]):
            gdl[g] = {p["name"]: p.get("value") for p in ps.get("parameters", [])}
    for o in spec["openings"]:
        d = details.get(o["id"])
        if not d or not wd:
            continue
        od = d["details"]
        if od.get("ownerElementId", {}).get("guid") != wall_guid:
            problems.append(f"{o['id']}: owner is not the wall {wall_guid}")
        s0, s1, v0, v1 = o["rect"]["v"]
        sc = (beg_s - od["centerOffset"]) if reverse else (beg_s + od["centerOffset"])
        p = gdl.get(guids[o["id"]], {})
        hole_w = p.get("ac_wallhole_width", od["width"])
        hole_h = p.get("ac_wallhole_height", od["height"])
        bottom = foot_v + od["sillHeight"] - (p.get("ac_lower_oversize") or 0.0)
        row(o["id"], "s0", s0, sc - hole_w / 2, "params")
        row(o["id"], "s1", s1, sc + hole_w / 2, "params")
        row(o["id"], "v0", v0, bottom, "params")
        row(o["id"], "v1", v1, bottom + hole_h, "params")

    # ---- (b) 3D geometry projected on the facade plane
    order = [wall_item] + [o["id"] for o in spec["openings"]]
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
        hole_area = sum(_poly_area(h) for _, holes in wall_faces for h in holes)
        spec_area = sum((o["rect"]["v"][1] - o["rect"]["v"][0]) * (o["rect"]["v"][3] - o["rect"]["v"][2]) for o in spec["openings"])
        row(wall_item, "hole area on outside face [m2]", spec_area, hole_area, "3D", tolerance=0.05)
    w_ranges = {}
    for o in spec["openings"]:
        if o["id"] not in proj:
            continue
        bb = _bbox(proj[o["id"]])
        if bb is None:
            problems.append(f"{o['id']}: no 3D body")
            continue
        s0, s1, v0, v1 = o["rect"]["v"]
        row(o["id"], "s0", s0, bb[0], "3D")
        row(o["id"], "s1", s1, bb[1], "3D")
        row(o["id"], "v0", v0, bb[2], "3D")
        row(o["id"], "v1", v1, bb[3], "3D")
        w_ranges[o["id"]] = [round(bb[4], 3), round(bb[5], 3)]
        # sills, boards and handles may stand a little out of the wall faces; a whole unit outside is an error
        if bb[5] > PROTRUSION or bb[4] < -zone["thickness"]["v"] - PROTRUSION:
            problems.append(f"{o['id']}: 3D body sticks out of the wall by more than {PROTRUSION} m (w {bb[4]:.3f}..{bb[5]:.3f})")

    # ---- outputs
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "deviations.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["item"])
        wr.writeheader()
        wr.writerows(rows)
    src = next(s for s in spec["sources"] if s.get("rect"))
    root = Path(source_root or spec.get("generated", {}).get("root", "."))
    overlay_png, model_png = draw(spec, src, root / src["rect"]["image"], proj, wall_faces, rows, out_dir)

    max_delta = {m: max([abs(r["delta_cm"]) for r in rows if r["method"] == m and r["delta_cm"] is not None
                         and r["quantity"] != "hole area on outside face [m2]"] or [0.0]) for m in ("params", "3D")}
    result = {
        "spec": spec["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.monotonic() - t0, 2),
        "count": {"spec": n_spec, "model_in_wall": hosted, "ok": count_ok},
        "max_delta_cm": max_delta, "tolerance_cm": tol * 100,
        "rows_failed": sum(not r["ok"] for r in rows), "rows": len(rows),
        "problems": problems, "spec_warnings": warnings,
        "opening_w_range_m": w_ranges,
        "overlay_png": str(overlay_png), "model_png": str(model_png), "deviations_csv": str(out_dir / "deviations.csv"),
        "pass": count_ok and not problems,
    }
    (out_dir / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report.md").write_text(report_md(spec, result, rows), encoding="utf-8")
    if verbose:
        print(f"verify {spec['id']}: openings model {hosted} / spec {n_spec}; max delta params {max_delta['params']:.2f} cm, "
              f"3D {max_delta['3D']:.2f} cm (tol {tol * 100:.1f} cm); {result['rows_failed']}/{len(rows)} rows out of tolerance; "
              f"{'PASS' if result['pass'] else 'FAIL'} ({result['seconds']} s)")
        for p in problems:
            print("  PROBLEM:", p)
        print("  overlay:", overlay_png)
    return result


# ---- drawing ----------------------------------------------------------------------------------
def draw(spec, src, image_path: Path, proj, wall_faces, rows, out_dir: Path):
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

    kinds = {o["id"]: o["kind"] for o in spec["openings"]}
    worst = {}
    for rr in rows:
        if rr["method"] == "3D" and rr["delta_cm"] is not None and rr["item"] in kinds:
            worst[rr["item"]] = max(worst.get(rr["item"], 0.0), abs(rr["delta_cm"]))

    def paint(canvas: Image.Image, dim_photo: bool) -> Image.Image:
        img = canvas.copy()
        if dim_photo:
            img = Image.blend(img, Image.new("RGB", size, (255, 255, 255)), 0.35)
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        # opening bodies: every face edge, orthographic (depth ignored), thin
        for oid, kind in kinds.items():
            for svw, polys in proj.get(oid, []):
                for p in polys:
                    ring = svw[p["vertexIds"]]
                    pts = [px(s, v) for s, v in ring[:, :2]]
                    dr.line(pts + [pts[0]], fill=COLORS[kind] + (150,), width=1)
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
            x, y = px(s0, v1_)
            label = f"{o['id']} {o['kind'][0].upper()} Δ{worst.get(o['id'], float('nan')):.1f} cm"
            dr.text((x + 3, y - 19), label, fill=COLORS["shadow"] + (255,), font=small, stroke_width=3, stroke_fill=(0, 0, 0, 255))
            dr.text((x + 3, y - 19), label, fill=COLORS["text"] + (255,), font=small)
        # legend
        lines = [(f"{spec['id']}: model (3D projekcia) × rektifikovaná fotka", COLORS["text"]),
                 ("červená: líce steny + otvory v stene (3D)", COLORS["wall"]),
                 ("modrá: okná (3D) · fialová: dvere (3D)", COLORS["window"]),
                 ("žltá prerušovaná: spec (z fotky)", COLORS["spec"])]
        y = size[1] - 22 * len(lines) - 8
        dr.rectangle([0, y - 6, 470, size[1]], fill=(0, 0, 0, 170))
        for text, col in lines:
            dr.text((8, y), text, fill=col + (255,), font=font)
            y += 22
        return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    overlay = paint(base, dim_photo=False)
    model = paint(Image.new("RGB", size, (70, 70, 70)), dim_photo=False)
    o_path = out_dir / f"overlay_{spec['id']}.png"
    m_path = out_dir / f"model_{spec['id']}.png"
    overlay.save(o_path)
    model.save(m_path)
    return o_path, m_path


def report_md(spec, result, rows) -> str:
    lines = [f"# Kontrola fasády {spec['id']} (verify, {result['at']})", "",
             f"Výsledok: **{'PASS' if result['pass'] else 'FAIL'}** – tolerancia build {result['tolerance_cm']:.1f} cm", "",
             "| Kontrola | Spec | Model | OK |", "|---|---|---|---|",
             f"| počet otvorov v stene | {result['count']['spec']} | {result['count']['model_in_wall']} | {'áno' if result['count']['ok'] else 'NIE'} |",
             f"| max. Δ parametre (čítané späť) | – | {result['max_delta_cm']['params']:.2f} cm | {'áno' if result['max_delta_cm']['params'] <= result['tolerance_cm'] else 'NIE'} |",
             f"| max. Δ 3D projekcia | – | {result['max_delta_cm']['3D']:.2f} cm | {'áno' if result['max_delta_cm']['3D'] <= result['tolerance_cm'] else 'NIE'} |",
             "", f"Overlay: `{result['overlay_png']}`  ", f"Model bez fotky: `{result['model_png']}`  ",
             f"Odchýlky po prvkoch: `{result['deviations_csv']}`", ""]
    if result["problems"]:
        lines += ["## Problémy", ""] + [f"- {p}" for p in result["problems"]] + [""]
    if result["spec_warnings"]:
        lines += ["## Upozornenia zo spec", ""] + [f"- {w}" for w in result["spec_warnings"]] + [""]
    lines += ["## Predpoklady (pod úrovňou `meranie`, na bránu G1)", "", "| Hodnota | Stav | Istota | Prečo |", "|---|---|---|---|"]
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

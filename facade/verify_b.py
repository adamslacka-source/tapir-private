"""verify_b: read pilot B (facade from ONE photo) back from Archicad and compare it with the spec and with the photo.

(a) parameters - build read-back (layer, Element ID, story, placement) and the curtain walls through cw_verify
                 (mullion / transom axes, boundary, frame profiles, panel class per cell, door panels with leaves)
(b) 3D         - every element: 3D bodies (Get3DGeometryOfElements) in facade coordinates (s, v, w); extent = the box
                 the spec expects (walls: zone rect x thickness; profiled beams / columns: profile box; brackets;
                 slabs: outline x thickness; louvres: template box from gdl_expect; railings: base and height,
                 nothing outside the walkway) - tolerance 1 cm (spec.tolerances.build)
(c) story      - home story of every element = the story its foot stands on (modeller rule 2)
(d) photo      - the model projected with the camera of the photo (pinhole of the rectified photo, spec S1.camera):
                 every 'meranie' mullion axis vs the centre of the dark bar on the photo, horizontal edges of the
                 bands / box / roof edge / walkway edges vs the strongest brightness step on the photo, +-3 cm
                 (spec.tolerances.meranie) in the plane of the element; the scale itself is ODHAD (railing 1.05 m),
                 so these are RELATIVE deviations (photo vs model through the same camera)
(e) image      - overlay of the projected model edges over the rectified photo (CC BY 4.0 attribution) + detail

Outputs <out>/<spec.id>/: deviations_b.csv, photo_edges_b.csv, report_b.json, overlay_HOF_B.png, overlay_detail_HOF_B.png.
Read-only in Archicad.

    python verify_b.py <spec.json> --project C:/FacadePilot/Fasada_pilot_B.pln --out C:/FacadePilot/pilot_B/out
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "adapters"))
import build as fb  # noqa: E402
import cw_verify  # noqa: E402
import gdl_expect  # noqa: E402
import systems as fs_systems  # noqa: E402
from geom import REFERENCE_PLANE, frames_of, outside_of  # noqa: E402
from spec_validate import validate  # noqa: E402
from tapir_client import TapirClient, TapirError  # noqa: E402
from from_photo import ATTRIBUTION, dark_center, h_edge  # noqa: E402

KIND_COLOUR = {"curtainwall": (0, 230, 255), "wall": (255, 255, 255), "beam": (255, 40, 40), "column": (255, 160, 0),
               "slab": (60, 255, 60), "railing": (180, 255, 120), "object": (255, 230, 0)}


class Cam:
    def __init__(self, c: dict):
        self.cx, self.cy, self.f, self.D0, self.s_c, self.v_c = (c[k] for k in ("cx", "cy", "f", "D0", "s_c", "v_c"))

    def xy(self, svw: np.ndarray) -> np.ndarray:
        z = self.D0 - svw[:, 2]
        return np.column_stack([self.cx + (svw[:, 0] - self.s_c) * self.f / z, self.cy - (svw[:, 1] - self.v_c) * self.f / z])

    def px_per_m(self, w: float) -> float:
        return self.f / (self.D0 - w)


def bodies_svw(geo: dict, fr) -> list[tuple[np.ndarray, list]]:
    out = []
    for b in geo.get("bodies", []):
        xyz = np.array([[v["x"], v["y"], v["z"]] for v in b["vertices"]], float)
        if len(xyz):
            out.append((fr.to_svw(xyz), b.get("polygons", [])))
    return out


def bbox(bodies) -> list[float] | None:
    if not bodies:
        return None
    p = np.vstack([b for b, _ in bodies])
    return [float(p[:, 0].min()), float(p[:, 0].max()), float(p[:, 1].min()), float(p[:, 1].max()), float(p[:, 2].min()), float(p[:, 2].max())]


def geometry(ac: TapirClient, guids: list[str], chunk: int = 12) -> list[dict]:
    out = []
    for i in range(0, len(guids), chunk):
        part = guids[i:i + chunk]
        out += ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}} for g in part]}, timeout=180)["geometries3D"]
    return out


def expected_box(spec: dict, it: dict) -> list[float] | None:
    """[s0, s1, v0, v1, w0, w1] the spec expects for a planned item (None = not a box)."""
    k, si = it["kind"], it.get("spec_item") or it.get("zone")
    sysd = spec["systems"]
    if k == "wall":
        z = it["zone"]
        s0, s1, v0, v1 = z["rect"]["v"]
        w = z["w"]["v"] if z.get("w") else 0.0
        return [s0, s1, v0, v1, w - z["thickness"]["v"], w]
    if k in ("beam", "column"):
        prof = sysd[sysd[si["system"]]["profile"]]["polygon_wv"]
        xs, ys = [p[0] for p in prof], [p[1] for p in prof]
        s0, s1 = si["s"]["v"]
        w0 = si["w"]["v"] if si.get("w") else 0.0
        if si["axis"] == "h":
            return [s0, s1, si["v"]["v"] + min(ys), si["v"]["v"] + max(ys), w0 + min(xs), w0 + max(xs)]
        if si["axis"] == "w":
            return [s0, s1, si["v"]["v"] + min(ys), si["v"]["v"] + max(ys), si["w_span"]["v"][0], si["w_span"]["v"][1]]
        return [s0, s1, si["v"]["v"][0], si["v"]["v"][1], w0 + min(xs), w0 + max(xs)]
    if k == "slab":
        ring = np.array(si["outline_sw"]["v"], float)
        th = sysd[si["system"]]["thickness"]["v"]
        return [ring[:, 0].min(), ring[:, 0].max(), si["v_top"]["v"] - th, si["v_top"]["v"], ring[:, 1].min(), ring[:, 1].max()]
    if k == "object":
        tpl = sysd[si["system"]]["template"]
        return gdl_expect.facade_bbox(tpl, {kk: v["v"] for kk, v in si["params"].items()}, si["origin"]["v"], outside_of(spec, si.get("plane", REFERENCE_PLANE)))
    return None


def _font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_overlay(rect_bgr: np.ndarray, proj: dict, cam: Cam, path: Path, scale: float, crop: tuple | None = None, title: str = "") -> None:
    img = rect_bgr if crop is None else rect_bgr[crop[1]:crop[3], crop[0]:crop[2]]
    ox, oy = (0, 0) if crop is None else (crop[0], crop[1])
    im = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS).convert("RGBA")
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for kind, bodies in proj:
        col = KIND_COLOUR.get(kind, (255, 0, 255)) + (210,)
        for svw, polys in bodies:
            xy = cam.xy(svw)
            xy = (xy - [ox, oy]) * scale
            edges = set()
            for pg in polys:
                idx = pg.get("pointIds") or pg.get("vertexIds") or pg.get("vertices") or []
                for a, b in zip(idx, idx[1:] + idx[:1]):
                    edges.add((min(a, b), max(a, b)))
            if not edges:                                     # no polygon index: draw the hull of the box
                x0, y0 = xy.min(axis=0)
                x1, y1 = xy.max(axis=0)
                d.rectangle([x0, y0, x1, y1], outline=col, width=1)
                continue
            for a, b in edges:
                if a < len(xy) and b < len(xy):
                    d.line([tuple(xy[a]), tuple(xy[b])], fill=col, width=1)
    im = Image.alpha_composite(im, layer).convert("RGB")
    dr = ImageDraw.Draw(im)
    f = _font(13 if scale < 0.5 else 16)
    dr.rectangle([0, im.height - 40, im.width, im.height], fill=(0, 0, 0))
    dr.text((6, im.height - 38), title, fill=(255, 255, 255), font=f)
    dr.text((6, im.height - 20), ATTRIBUTION, fill=(255, 255, 255), font=f)
    im.save(path)


def verify(spec: dict, project: str, port: int, out_root: Path, overlay: bool = True) -> dict:
    t0 = time.monotonic()
    errors, _ = validate(spec)
    if errors:
        raise TapirError("spec invalid: " + "; ".join(errors[:5]))
    ac = TapirClient(port=port, timeout=120)
    path = ac.guard_project(project)
    stories = ac.tapir("GetStories")["stories"]
    story_levels = {s["index"]: s["level"] for s in stories}
    sysinfo = fs_systems.resolve(ac, spec, path, out_root)
    want = fb.plan(spec, story_levels, sysinfo)
    state = json.loads(fb.state_path(spec, out_root).read_text(encoding="utf-8"))
    known = state["elements"]
    tol = spec["tolerances"]["build"]
    frames = frames_of(spec)
    fr = frames[REFERENCE_PLANE]
    rows, trows, problems, photo = [], [], [], []

    def row(item, quantity, spec_v, model_v, method, tolerance=None):
        tolerance = tol if tolerance is None else tolerance
        dd = None if model_v is None else model_v - spec_v
        ok = dd is not None and abs(dd) <= tolerance + 1e-9
        rows.append({"item": item, "quantity": quantity, "spec_m": round(float(spec_v), 4), "model_m": None if model_v is None else round(float(model_v), 4),
                     "delta_cm": None if dd is None else round(float(dd) * 100, 2), "tol_cm": round(tolerance * 100, 2), "method": method, "ok": ok})
        if not ok:
            problems.append(f"{item} {quantity} ({method}): spec {spec_v:.3f}, model {model_v}")

    def trow(item, what, spec_v, model_v, status=None, note=""):
        if status is None:
            status = "OK" if fs_systems.same(spec_v, model_v, tol) else "FAIL"
        trows.append({"item": item, "what": what, "spec": str(spec_v), "model": str(model_v), "status": status, "note": note})
        if status == "FAIL":
            problems.append(f"{item} {what}: spec {spec_v!r}, model {model_v!r}")

    items = want["walls"] + want["linear"] + want["objects"] + want["cw"] + want["slabs"] + want["railings"]
    guids = {it["item"]: known[it["item"]]["guid"] for it in items if it["item"] in known}
    missing = [it["item"] for it in items if it["item"] not in guids]
    if missing:
        problems.append(f"not built: {missing}")
    det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids.values()],
                                            "fields": ["type", "id", "floorIndex", "layerIndex", "details"]}, timeout=120)["detailsOfElements"]
    details = {}
    for (k, g), d in zip(guids.items(), det):
        if isinstance(d, dict) and "type" in d:
            details[k] = d
    layer = state["layer"]["index"]
    # (c) story + layer + Element ID (railings: GetDetailsOfElements does not support them -> floor from the subelements skipped)
    for it in items:
        k = it["item"]
        d = details.get(k)
        if d is None:
            if it["kind"] != "railing":
                problems.append(f"{k}: not readable")
            continue
        want_floor = it["payload"].get("floorIndex")
        if want_floor is not None and d.get("floorIndex") != want_floor:
            problems.append(f"{k}: home story {d.get('floorIndex')} != {want_floor}")
        if d.get("layerIndex") != layer:
            problems.append(f"{k}: layer {d.get('layerIndex')} != {layer}")
        if d.get("id") != it["element_id"]:
            problems.append(f"{k}: Element ID {d.get('id')} != {it['element_id']}")
    n_story_ok = sum(1 for it in items if it["item"] in details and details[it["item"]].get("floorIndex") == it["payload"].get("floorIndex"))
    # (a) curtain walls
    cw_res = cw_verify.verify_cw(ac, spec, want["cw"], guids, details, frames, sysinfo, None, None, None, row, trow, problems)
    # (b) 3D of every element
    order = [it for it in items if it["item"] in guids]
    geos = geometry(ac, [guids[it["item"]] for it in order])
    proj = []
    for it, geo in zip(order, geos):
        bodies = bodies_svw(geo, fr)
        proj.append((it["kind"], bodies))
        bb = bbox(bodies)
        k = it["item"]
        if bb is None:                                         # a wall merged into its neighbours in 3D: bounding box of the element
            b3 = ac.tapir("Get3DBoundingBoxes", {"elements": [{"elementId": {"guid": guids[k]}}]})["boundingBoxes3D"][0].get("boundingBox3D")
            if b3 is None:
                problems.append(f"{k}: no 3D")
                continue
            c = fr.to_svw(np.array([[b3["xMin"], b3["yMin"], b3["zMin"]], [b3["xMax"], b3["yMax"], b3["zMax"]]]))
            bb = [float(c[:, 0].min()), float(c[:, 0].max()), float(c[:, 1].min()), float(c[:, 1].max()), float(c[:, 2].min()), float(c[:, 2].max())]
            trows.append({"item": k, "what": "3D bodies merged with the neighbouring walls - bounding box used", "spec": "-", "model": "bbox",
                          "status": "OK", "note": ""})
        if it["kind"] == "railing":
            r = it["spec_item"]
            h = spec["systems"][r["system"]]["height"]["v"]
            row(k, "railing 3D base v", r["v"]["v"], bb[2], "3D")
            row(k, "railing 3D top v (height)", r["v"]["v"] + h, bb[3], "3D")
            sl = next(x for x in spec["slabs"] if x["id"] == r["on"])
            ring = np.array(sl["outline_sw"]["v"], float)
            allp = np.vstack([b for b, _ in bodies])
            # the top rail of the favourite runs 0.10 m past the end nodes (end extension of the base favourite)
            out = int(((allp[:, 0] < ring[:, 0].min() - 0.11) | (allp[:, 0] > ring[:, 0].max() + 0.11) |
                       (allp[:, 2] < ring[:, 1].min() - 0.11) | (allp[:, 2] > ring[:, 1].max() + 0.11)).sum())
            trow(k, "railing vertices outside the walkway (+-0.11: post radius + top rail end extension 0.10 of the favourite)", 0, out,
                 "OK" if out == 0 else "FAIL")
            row(k, "railing 3D s end past the walkway (top rail extension, favourite)", 0.10, bb[1] - ring[:, 0].max(), "3D", tolerance=0.01)
            continue
        if it["kind"] == "curtainwall":
            continue                                           # cw_verify did it
        exp = expected_box(spec, it)
        if exp is None:
            continue
        names = ("s0", "s1", "v0", "v1", "w0", "w1")
        for i, nm in enumerate(names):
            # a slab / wall extent along s is clipped by nothing here; an object of FS_Louvers: lamellae inside the box
            row(k, f"3D {nm}", exp[i], bb[i], "3D", tolerance=tol if not (it["kind"] == "object" and nm in ("w0", "w1")) else 0.03)
    # (d) photo
    cam_d = next(s for s in spec["sources"] if s["id"] == "S1")["camera"]
    cam = Cam(cam_d)
    rect_path = Path(next(s for s in spec["sources"] if s["id"] == "S1")["rect_image"])
    rect = cv2.imread(str(rect_path), cv2.IMREAD_COLOR)
    g = rect.astype(np.float32).mean(axis=2)
    red = rect[..., 2].astype(np.float32) - 0.5 * (rect[..., 0].astype(np.float32) + rect[..., 1].astype(np.float32))
    ptol = spec["tolerances"]["meranie"]
    tree_x = 8350
    for cw in spec["cw_fields"]:
        ax = cw.get("u_axes")
        if not ax or ax["conf"] != "meranie":
            continue
        w = cw["photo_w"]["v"] if cw.get("photo_w") else -cw["reveal"]["v"]     # plane the axes were read on
        s0, s1, v0, v1 = cw["rect"]["v"]
        vv = [v0] + cw.get("v_axes", {}).get("v", []) + [v1]
        rows_ = list(zip(vv[:-1], vv[1:]))
        # upper floors: the parapet row below the blinds (the mullion itself is visible there); ground floor: tallest row
        hi = rows_[0] if any(c["panel"] == "opaque" for c in cw.get("cells", [])) else max(rows_, key=lambda r_: r_[1] - r_[0])
        for k_, s in enumerate(ax["v"]):
            xm = cam.xy(np.array([[s, 0.5 * (hi[0] + hi[1]), w]]))[0]
            if xm[0] > tree_x:
                continue
            ya, yb = sorted(cam.xy(np.array([[s, hi[0] + 0.05, w], [s, hi[1] - 0.05, w]]))[:, 1])
            if any(lo - 30 <= xm[0] <= hi + 30 for lo, hi in ((240, 310), (6680, 6750))):
                continue                                       # behind a downpipe: axis by rule, not measurable
            xp, st = dark_center(g, xm[0], int(ya), int(yb), half=10)
            dm = (xm[0] - xp) / cam.px_per_m(w)
            readable = st >= 8.0                               # weaker bar = not readable in this band (reported, not judged)
            photo.append({"item": cw["id"], "edge": f"stĺpik {k_ + 1} (stred lišty)", "model_px": round(float(xm[0]), 1), "photo_px": round(float(xp), 1),
                          "delta_cm": round(float(dm) * 100, 2), "tol_cm": ptol * 100, "contrast": round(st, 1),
                          "ok": abs(dm) <= ptol if readable else None, "readable": readable})
    # horizontal edges: bands (red channel), GF box, roof edge, walkway edges
    lin_by = {it["id"]: it for it in spec["linear"]}
    checks = []
    for lid in ("PAS_2NP_L", "PAS_1NP_L", "PAS_2NP_P", "PAS_1NP_P"):
        it = lin_by[lid]
        h = max(p[1] for p in spec["systems"][spec["systems"][it["system"]]["profile"]]["polygon_wv"])
        wf = it["w"]["v"] + 0.2
        checks += [(lid, "spodná hrana pásu", it["s"]["v"], it["v"]["v"], wf, red, -1), (lid, "horná hrana pásu", it["s"]["v"], it["v"]["v"] + h, wf, red, +1)]
    it = lin_by["BOX_GF_L"]
    h = max(p[1] for p in spec["systems"]["prof.box_gf"]["polygon_wv"])
    wf = it["w"]["v"] + 0.22
    checks += [("BOX_GF_L", "spodná hrana skrinky", it["s"]["v"], it["v"]["v"], wf, g, +1), ("BOX_GF_L", "horná hrana skrinky", it["s"]["v"], it["v"]["v"] + h, wf, g, -1)]
    st_ = lin_by["STRECHA"]
    prof = spec["systems"]["prof.strecha"]["polygon_wv"]
    wfr = st_["w"]["v"] + prof[2][0]
    checks += [("STRECHA", "spodná hrana čela", (2.0, 40.0), st_["v"]["v"] + prof[2][1], wfr, g, +1),
               ("STRECHA", "horná hrana čela (atika)", (2.0, 40.0), st_["v"]["v"] + prof[3][1], wfr, g, -1)]
    for sl in spec["slabs"]:
        if sl["kind"] == "walkway":
            ring = np.array(sl["outline_sw"]["v"])
            checks.append((sl["id"], "vrch čelného plechu", (ring[:, 0].min() + 3, 30.0), sl["v_top"]["v"], ring[:, 1].max(), g, +1))
    edge_rows = []
    for item, name, srange, v, w, ch, sign in checks:
        a, b = srange
        # visible part of the edge: inside the photo, left of the tree, not at the piers / downpipes
        s_lo = max(a + 0.3, float(cam.s_c + (60 - cam.cx) * (cam.D0 - w) / cam.f))
        s_hi = min(b - 0.3, float(cam.s_c + (tree_x - cam.cx) * (cam.D0 - w) / cam.f))
        samples = []
        for s in np.linspace(s_lo, s_hi, 7):
            xm, ym = cam.xy(np.array([[s, v, w]]))[0]
            if any(lo - 60 <= xm <= hi + 60 for lo, hi in ((240, 310), (6680, 6750), (6990, 7520))):
                continue
            yp, stg = h_edge(ch, int(xm), ym, sign, 20, 6)
            dm = (ym - yp) / cam.px_per_m(w)
            samples.append(dm)
            photo.append({"item": item, "edge": name, "model_px": round(float(ym), 1), "photo_px": round(float(yp), 1), "x_px": round(float(xm)),
                          "delta_cm": round(float(dm) * 100, 2), "tol_cm": ptol * 100, "contrast": round(stg, 1), "ok": abs(dm) <= ptol, "sample": True})
        if samples:
            med = float(np.median(samples))
            edge_rows.append({"item": item, "edge": name, "n": len(samples), "median_cm": round(med * 100, 2),
                              "max_cm": round(max(abs(x) for x in samples) * 100, 2), "ok": abs(med) <= ptol})
    # (e) overlays
    out_dir = out_root / spec["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    if overlay:
        title = f"{spec['id']}: model (ArchiCAD 3D, kamera fotky) cez rektifikovanú fotku | azúrová LOP, červená pásy/strecha/konzoly, zelená ochodze, žltá žalúzie"
        paths["overlay"] = out_dir / "overlay_HOF_B.png"
        draw_overlay(rect, proj, cam, paths["overlay"], 0.25, None, title)
        paths["detail"] = out_dir / "overlay_detail_HOF_B.png"
        draw_overlay(rect, proj, cam, paths["detail"], 1.0, (2300, 1350, 4700, 3300), title + " (výrez 1:1)")
        no_louv = [(k, b) for k, b in proj if k != "object"]
        paths["detail_bez_zaluzii"] = out_dir / "overlay_detail_bez_zaluzii_HOF_B.png"
        draw_overlay(rect, no_louv, cam, paths["detail_bez_zaluzii"], 1.0, (5800, 1350, 8300, 4000), title.split("|")[0] + "| bez žalúzií, výrez 1:1")
    with open(out_dir / "deviations_b.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    with open(out_dir / "photo_edges_b.csv", "w", newline="", encoding="utf-8") as fh:
        keys = sorted({k for p in photo for k in p})
        wr = csv.DictWriter(fh, fieldnames=keys)
        wr.writeheader()
        wr.writerows(photo)
    num = [abs(r["delta_cm"]) for r in rows if r["delta_cm"] is not None]
    mull = [p for p in photo if not p.get("sample") and p.get("readable")]
    unread = [p for p in photo if not p.get("sample") and not p.get("readable")]
    ph = [abs(p["delta_cm"]) for p in mull] + [abs(e["median_cm"]) for e in edge_rows]
    result = {"spec": spec["id"], "elements": len(guids), "rows": len(rows), "rows_ok": sum(r["ok"] for r in rows), "max_delta_cm": max(num) if num else None,
              "types_ok": sum(t["status"] == "OK" for t in trows), "types": len(trows), "story_ok": n_story_ok,
              "photo": len(mull) + len(edge_rows), "photo_ok": sum(p["ok"] for p in mull) + sum(e["ok"] for e in edge_rows),
              "photo_max_cm": max(ph) if ph else None, "photo_edges": edge_rows, "photo_unreadable": len(unread),
              "photo_fail": [f"{p['item']} {p['edge']} {p['delta_cm']} cm" for p in mull if not p["ok"]] + [f"{e['item']} {e['edge']} {e['median_cm']} cm" for e in edge_rows if not e["ok"]],
              "photo_samples_max_cm": max(abs(p["delta_cm"]) for p in photo if p.get("sample")) if edge_rows else None,
              "photo_median_cm": float(np.median(ph)) if ph else None, "problems": problems, "paths": {k: str(v) for k, v in paths.items()},
              "seconds": round(time.monotonic() - t0, 1), "cw_photo": cw_res.get("photo")}
    (out_dir / "report_b.json").write_text(json.dumps({"result": result, "types": trows}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--project", default=r"C:\FacadePilot\Fasada_pilot_B.pln")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--out", type=Path, default=Path(r"C:\FacadePilot\pilot_B\out"))
    args = ap.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    r = verify(spec, args.project, args.port, args.out)
    print(json.dumps({k: v for k, v in r.items() if k not in ("problems", "cw_photo")}, indent=1, ensure_ascii=False, default=float))
    for p in r["problems"][:40]:
        print("PROBLEM:", p)
    return 0 if not r["problems"] and r["photo_ok"] == r["photo"] else 1


if __name__ == "__main__":
    sys.exit(main())

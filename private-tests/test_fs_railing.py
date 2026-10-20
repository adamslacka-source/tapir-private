"""
Acceptance test of the GDL railing FS_Railing (ADR-001 R1 fallback to the native railing; task 2026-10-20).

    python test_fs_railing.py --offline   # compile + interpret check, round trip, expected model, build plan (no Archicad)
    python test_fs_railing.py             # + load, place every infill type, read 3D back, favourites, previews, delete
    python test_fs_railing.py --save      # ... and SaveProject at the end

Live part writes ONLY into C:/FacadePilot/Fasada_pilot_A.pln (guard_project): FS_Railing in the embedded library
(FS_GDL), the 'FS rail.*' Object favourites, and temporary test objects at x >= 6000 m, deleted at the end (also after a
failure).

  R1 template compiles (LP_XMLConverter -interpret, no GDL error), round trip GUID + parameters (String parameter too)
  R2 offline: expected model of the 4 favourites (counts of posts / bars / balusters / panes); build plan of a gdl railing
     (pilot B walkway railing as a copy of its spec - pilot B itself is not touched): one object per segment, fall side,
     shared corner post once, spec valid
  R3 load: GetAvailableLibraryParts sees FS_Railing as Object, parent subtype = 'Railing' (built-in)
  R4 per case (every infill type + side mounting / list of bar heights / count mode): CreateObjects at x >= 6000 m,
     SetGDLParametersOfElements, read back = sent, every GDL primitive (connected polygon component of the 3D body)
     matches the independent model +-5 mm (count + box), z ranges of handrail / bars, total height H +-5 mm
  R5 IFC: classification 'Zábradlie/Zábradlí' -> GetIFCTypeOfElements = IfcRailing
  R6 favourites FS rail.horiz_5 / glass / balusters / panel_perf exist; an object from each favourite = the same
     parameters and the same geometry as the model of the favourite; it keeps the classification (IfcRailing)
  R7 3D previews of the 4 favourite objects (GetElementPreviewImage)
  R8 build plan of the gdl railing placed live (moved to x >= 6000 m): z range = foot .. foot + H, posts
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import base64
import copy
import json
import math
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))

import gdl_build  # noqa: E402
import railing_gdl as rg  # noqa: E402

OUT = gdl_build.OUT
PROJECT = gdl_build.PROJECT
TOL = 0.005
X0 = 6000.0
RAILING_SUBTYPE = "09BEB35E-7BF8-448C-B408-AC04D159B0D1"
PILOT_B_SPEC = Path(r"C:\FacadePilot\pilot_B\out\HOF_B.facade-spec.json")
FAV_MANIFEST = OUT / "rail_favorites.json"

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return ok


CASES = {
    "horiz_5 (favourite content)": rg.FAVORITES["FS rail.horiz_5"]["params"],
    "glass (favourite content)": rg.FAVORITES["FS rail.glass"]["params"],
    "balusters (favourite content)": rg.FAVORITES["FS rail.balusters"]["params"],
    "panel_perf (favourite content)": rg.FAVORITES["FS rail.panel_perf"]["params"],
    "none, side mounted, round posts, rect handrail, by count, no end post, overhangs": {
        "A": 2.5, "ZZYZX": 1.1, "iPostProf": 1, "postW": 0.05, "iPostMode": 0, "nPost": 4, "pInset": 0.1, "bPostEnd": False,
        "iMount": 1, "sideGap": 0.03, "mountDrop": 0.2, "iRailProf": 1, "railW": 0.06, "railH": 0.04, "railOvhS": 0.1,
        "railOvhE": 0.05, "iInfill": 0},
    "bars from a list (pilot B: 3 top + 7 bottom), rect bars, flat handrail": {
        "A": 4.0, "ZZYZX": 1.05, "iPostProf": 2, "postW": 0.012, "postD": 0.06, "postSp": 1.261, "pInset": 0.006,
        "iRailProf": 2, "railW": 0.05, "railH": 0.008, "iInfill": 1, "iBarDist": 1, "iBarProf": 1, "barD": 0.02, "barT": 0.008,
        "barZList": "0.10;0.17;0.24;0.31;0.38;0.45;0.52;0.66;0.80;0.94"},
}


def counts(parts) -> dict:
    out: dict[str, int] = {}
    for k, _ in parts:
        out[k] = out.get(k, 0) + 1
    return out


def pilot_b_gdl_spec():
    spec = json.loads(PILOT_B_SPEC.read_text(encoding="utf-8"))
    spec = copy.deepcopy(spec)
    s = spec["systems"]["rail.ochodza"]
    if s.get("railing_kind") == "gdl":                         # pilot B is wired (2026-10-20): its own spec as it is
        return spec
    s.pop("base_favorite", None)
    s.pop("post_distribution", None)
    s["railing_kind"] = "gdl"
    s["favorite"] = "FS rail.horiz_5"
    s["gdl_params"] = {"iBarDist": 1, "barZList": "0.10;0.17;0.24;0.31;0.38;0.45;0.52;0.66;0.80;0.94"}
    return spec


# -- offline ------------------------------------------------------------------------------------------------------
def offline() -> None:
    conv = gdl_build.converter_version()
    try:
        b = gdl_build.compile_template(rg.TEMPLATE, conv=conv)
        check("R1 FS_Railing compiled, GDL interpreted without error, round trip GUID + parameters", True,
              f"{b['seconds']} s, {len(b['params'])} parameters, warnings {len(b['warnings'])}, String barZList in round trip: "
              f"{'barZList' in b['params']}")
    except gdl_build.GdlBuildError as exc:
        check("R1 FS_Railing compiled", False, str(exc)[:400])
    want = {"FS rail.horiz_5": {"post": 4, "handrail": 1, "bar": 5}, "FS rail.glass": {"post": 4, "handrail": 1, "glass": 3},
            "FS rail.balusters": {"post": 4, "handrail": 1, "bottom_rail": 1, "baluster": 24},  # 3 bays, clear 0.947 / 0.11 -> 9 gaps -> 8 per bay,
            "FS rail.panel_perf": {"post": 4, "handrail": 1, "panel": 3}}
    for name, w in want.items():
        got = counts(rg.expected_parts(rg.full_params(rg.FAVORITES[name]["params"])))
        check(f"R2 model {name}: parts {w}", got == w, f"{got}")

    # build plan of a gdl railing (copy of the pilot B spec; nothing of pilot B is written)
    import build as fb
    from spec_validate import validate
    if not PILOT_B_SPEC.exists():
        check("R2 pilot B spec available (read only)", False, str(PILOT_B_SPEC))
        return
    spec = pilot_b_gdl_spec()
    errors, _ = validate(spec)
    check("R2 spec with railing_kind 'gdl' valid", not errors, "; ".join(errors[:3]))
    plan = gdl_plan(spec)
    rails = plan["railings"]
    n_seg = sum(len(r_["path_sw"]["v"]) - 1 for r_ in spec["railings"])
    check(f"R2 plan: one FS_Railing object per path segment ({n_seg}, kind object)",
          len(rails) == n_seg and all(it["kind"] == "object" and it["template"] == rg.TEMPLATE for it in rails),
          ", ".join(f"{it['item']} A={next(w['value'] for w in it['gdl'] if w['name'] == 'A'):.3f}" for it in rails))
    fr = fb.frames_of(spec)["F"] if "F" in fb.frames_of(spec) else next(iter(fb.frames_of(spec).values()))
    ok, det = True, []
    for it in rails:
        ang = it["payload"]["angle"]
        left = (-math.sin(ang), math.cos(ang))                   # local +y in plan
        gw = {w["name"]: w["value"] for w in it["gdl"]}
        long_seg = not it["item"].endswith(".1")                # segment 1 = the side railing to the facade
        n = fr.n
        if long_seg:                                              # front of the walkway falls away from the facade (+n)
            good = left[0] * n[0] + left[1] * n[1] > 0.99
        else:                                                     # side segment falls to -s (away from the walkway)
            good = -(left[0] * fr.d[0] + left[1] * fr.d[1]) > 0.99
        ok &= good
        det.append(f"{it['item']}: +y·{'n' if long_seg else '-d'} {'ok' if good else 'WRONG'}, posts start/end {gw['bPostStart']}/{gw['bPostEnd']}")
    check("R2 plan: local +y of every segment = fall side (front: out of the facade, side: away from the walkway)", ok, "; ".join(det))
    pc = []
    for rid in ("ZAB_1NP", "ZAB_2NP"):
        segs = [it for it in rails if it["item"].startswith(rid)]
        n_posts = sum(counts(rg.expected_parts(rg.full_params({w["name"]: w["value"] for w in it["gdl"]})))["post"] for it in segs)
        L = [next(w["value"] for w in it["gdl"] if w["name"] == "A") for it in segs]
        r_ = next(x for x in spec["railings"] if x["id"] == rid)
        bays = (r_.get("segment_bays") or {}).get("v") or [math.ceil(l / 1.261 - 1e-5) for l in L]
        want_posts = sum(bays) + 1
        pc.append((rid, n_posts, want_posts))
    check("R2 plan: posts = sum of bays + 1 (shared vertices once)", all(a == b for _, a, b in pc), f"{pc}")


def gdl_plan(spec: dict) -> dict:
    import build as fb
    story_levels = {s.get("floorIndex", i): s["level"]["v"] for i, s in enumerate(spec.get("stories", []))} or {0: 0.0}
    sysinfo = {"favorites": {}, "composites": {}, "gdl": {rg.TEMPLATE: {"hash": "test"}}, "classes": {}, "attrs": None,
               "profiles": {}, "rail_gdl": {}}
    # only the railings are needed: plan_railings directly
    frames = fb.frames_of(spec)
    home = fb.Home(spec, story_levels)
    return {"railings": fb.plan_railings(spec, frames, home, sysinfo)}


# -- live ---------------------------------------------------------------------------------------------------------
def place(ac, x: float, p: dict, extra: dict | None = None) -> tuple[str, dict]:
    from tapir_client import results_ok
    obj = {"libraryPartName": rg.TEMPLATE, "coordinates": {"x": x, "y": 0.0, "z": 0.0}}
    if extra:
        obj.update(extra)
    res = ac.tapir("CreateObjects", {"objectsData": [obj]})
    el = res["elements"][0]
    if "elementId" not in el:
        raise RuntimeError(f"CreateObjects: {el}")
    g = el["elementId"]["guid"]
    if p:
        r = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": g}, "gdlParameters": rg.gdl_writes(p)}]})
        if errs := results_ok(r["executionResults"]):
            ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}}]})
            raise RuntimeError(f"SetGDLParametersOfElements: {errs}")
    return g, ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}]})["detailsOfElements"][0]


def read_params(ac, g: str) -> dict:
    back = ac.tapir("GetGDLParametersOfElements", {"elements": [{"elementId": {"guid": g}}]})["gdlParametersOfElements"][0]
    return {q["name"]: q.get("value") for q in back.get("parameters", [])}


def diff_params(sent: dict, got: dict) -> list[str]:
    bad = []
    for k, v in sent.items():
        g = got.get(k)
        if isinstance(v, str):
            ok = g == v
        elif isinstance(v, bool):
            ok = bool(g) == v
        else:
            ok = g is not None and abs(float(g) - float(v)) < 1e-6
        if not ok:
            bad.append(f"{k}: {v} -> {g}")
    return bad


def geometry_check(ac, label: str, g: str, det: dict, p_full: dict) -> dict:
    geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}}]})["geometries3D"][0]
    got = rg.parts_of_geometry(geo, det["details"]["origin"], det["details"].get("angle", 0.0))
    want = rg.expected_parts(p_full)
    cmp = rg.compare_parts(want, got, TOL)
    check(f"R4 {label}: GDL primitives = model (count {cmp['n_want']}, boxes +-5 mm)", cmp["ok"],
          f"read {cmp['n_got']}, {counts(want)}, max delta {cmp['max_delta'] * 1000:.1f} mm"
          + (f"; missing {cmp['unmatched_want'][:3]}; extra {cmp['unmatched_got'][:3]}" if not cmp["ok"] else ""))
    bb = rg.bbox_of_parts([("x", b) for b in got])
    zr = []
    for kind in ("handrail", "bar"):
        ws = [b for k, b in want if k == kind]
        for wb in ws:
            m = min(got, key=lambda b: max(abs(x - y) for x, y in zip(b, wb)))
            zr.append(f"{kind} z {m[4]:.3f}..{m[5]:.3f} (model {wb[4]:.3f}..{wb[5]:.3f})")
    wb = rg.bbox_of_parts(want)
    top_ok = bb is not None and abs(bb[5] - p_full["ZZYZX"]) <= TOL
    bot_ok = bb is not None and abs(bb[4] - wb[4]) <= TOL
    check(f"R4 {label}: total height H = {p_full['ZZYZX']:.3f} +-5 mm, lowest point {wb[4]:.3f}", top_ok and bot_ok,
          f"3D z {bb[4]:.4f}..{bb[5]:.4f}; " + "; ".join(zr[:4]) + (" ..." if len(zr) > 4 else ""))
    return {"want": want, "got": got, "cmp": cmp}


def live(port: int, save: bool) -> None:
    from tapir_client import TapirClient, results_ok

    t0 = time.monotonic()
    ac = TapirClient(port=port, timeout=120)
    ac.guard_project(PROJECT)
    check("GetAddOnVersion = 1.7.2 (private build)", ac.addon_version() == "1.7.2")
    dep = gdl_build.deploy(ac, (rg.TEMPLATE,))
    part = dep["parts"].get(rg.TEMPLATE, {})
    man = gdl_build.load_manifest()["parts"][rg.TEMPLATE]
    check("R3 deploy + GetAvailableLibraryParts: FS_Railing is an Object with the template GUID",
          part.get("typeId") == "Object" and part.get("guid", "").upper() == man["guid"],
          f"changed {dep['changed']}, index {part.get('index')}")

    created: list[str] = []
    try:
        x = X0
        cls = rg.rail_classification(ac)
        for label, ov in CASES.items():
            p_full = rg.full_params(ov)
            p_send = dict(ov)
            p_send.update(rg.info_dims(p_full))
            g, det = place(ac, x, p_send)
            created.append(g)
            if label.startswith("horiz_5"):
                lp = det["details"]["libPart"]
                check("R3 library part FS_Railing, parent subtype = Railing (built-in, under Model Element)",
                      lp["name"] == rg.TEMPLATE and lp["parentUnID"]["guid"].upper() == RAILING_SUBTYPE, f"parent {lp['parentUnID']['guid']}")
                ifc0 = ac.tapir("GetIFCTypeOfElements", {"elements": [{"elementId": {"guid": g}}]})["elementIFCTypes"][0]
                r = ac.tapir("SetClassificationsOfElements", {"elementClassifications": [{"elementId": {"guid": g}, "classificationId": cls}]}) if cls else None
                ifc1 = ac.tapir("GetIFCTypeOfElements", {"elements": [{"elementId": {"guid": g}}]})["elementIFCTypes"][0]
                check("R5 IFC: subtype alone -> proxy; classification Zábradlie/Zábradlí -> IfcRailing",
                      cls is not None and not results_ok(r["executionResults"]) and ifc1.get("ifcType") == "IfcRailing",
                      f"without classification {ifc0.get('ifcType')}, with {ifc1.get('ifcType')} / {ifc1.get('typeObjectIFCType')}")
            diff = diff_params(p_send, read_params(ac, g))
            check(f"R4 {label}: GDL read back = sent ({len(p_send)} parameters)", not diff, "; ".join(diff[:5]))
            geometry_check(ac, label, g, det, p_full)
            x += 10.0

        # favourites
        log: list[str] = []
        th = gdl_build.load_manifest()["parts"][rg.TEMPLATE]["hash"]
        favs = rg.ensure_favorites(ac, origin=(X0 + 200.0, 0.0), log=log, template_hash=th, manifest_path=FAV_MANIFEST)
        names = set(ac.tapir("GetFavoritesByType", {"elementType": "Object"}).get("favorites", []))
        check("R6 favourites exist (Object): " + ", ".join(rg.FAVORITES), all(n in names for n in rg.FAVORITES),
              "; ".join(f"{n}: {v['action']}" for n, v in favs.items()) + (f" | {'; '.join(log)}" if log else ""))
        previews = []
        for k, name in enumerate(rg.FAVORITES):
            p_fav = favs[name]["params"]
            g, det = place(ac, X0 + 100.0 + 10.0 * k, {}, {"favoriteName": name})
            created.append(g)
            got_p = read_params(ac, g)
            diff = diff_params(p_fav, got_p)
            check(f"R6 {name}: object from the favourite has the favourite parameters ({len(p_fav)})", not diff, "; ".join(diff[:5]))
            dims = det["details"].get("dimensions", {})
            check(f"R6 {name}: A / ZZYZX of the element from the favourite", abs(dims.get("x", 0) - p_fav["A"]) < 1e-6
                  and abs(dims.get("z", 0) - p_fav["ZZYZX"]) < 1e-6, f"{dims}")
            geometry_check(ac, f"{name} (from favourite)", g, det, rg.full_params(p_fav))
            ifc = ac.tapir("GetIFCTypeOfElements", {"elements": [{"elementId": {"guid": g}}]})["elementIFCTypes"][0]
            check(f"R6 {name}: element from the favourite keeps the classification -> IfcRailing", ifc.get("ifcType") == "IfcRailing", f"{ifc.get('ifcType')}")
            im = ac.tapir("GetElementPreviewImage", {"elementId": {"guid": g}, "imageType": "3D", "format": "png", "width": 640, "height": 480}, timeout=60)
            png = OUT / f"preview_FS_Railing_{name.split('.')[-1]}.png"
            png.write_bytes(base64.b64decode(im["previewImage"]))
            if png.stat().st_size > 1000:
                previews.append(str(png))
        check("R7 3D previews of the 4 favourites written (GetElementPreviewImage)", len(previews) == 4, "; ".join(previews))

        # build plan of the gdl railing (pilot B walkway copy), placed live at x >= 6000 m
        if PILOT_B_SPEC.exists():
            spec = pilot_b_gdl_spec()
            rails = gdl_plan(spec)["railings"]
            xs = [it["payload"]["coordinates"]["x"] for it in rails]
            shift = X0 + 300.0 - min(xs)
            ok, det_s = True, []
            for it in rails:
                pl = copy.deepcopy(it["payload"])
                pl["coordinates"]["x"] += shift
                pl.pop("floorIndex", None)
                pl["coordinates"]["z"] = 0.0                     # pilot A has its own stories: foot at 0, H checked relative
                res = ac.tapir("CreateObjects", {"objectsData": [pl]})
                g = res["elements"][0]["elementId"]["guid"]
                created.append(g)
                r = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": g}, "gdlParameters": it["gdl"]}]})  # noqa: E501
                if errs := results_ok(r["executionResults"]):
                    ok = False
                    det_s.append(f"{it['item']}: {errs}")
                    continue
                det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}]})["detailsOfElements"][0]
                pf = rg.full_params({w["name"]: w["value"] for w in it["gdl"]})
                geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}}]})["geometries3D"][0]
                got = rg.parts_of_geometry(geo, det["details"]["origin"], det["details"].get("angle", 0.0))
                cmp = rg.compare_parts(rg.expected_parts(pf), got, TOL)
                bb = rg.bbox_of_parts([("x", b) for b in got])
                z_low = -pf["mountDrop"] if pf["iMount"] == 1 else 0.0       # side mounted: the post runs below the foot
                good = cmp["ok"] and abs(bb[5] - pf["ZZYZX"]) <= TOL and abs(bb[4] - z_low) <= TOL \
                    and abs(math.remainder(det["details"]["angle"] - it["payload"]["angle"], 2 * math.pi)) < 1e-6
                ok &= good
                det_s.append(f"{it['item']}: {counts(rg.expected_parts(pf))}, z {bb[4]:.3f}..{bb[5]:.3f}, angle {math.degrees(det['details']['angle']):.1f}")
            check("R8 build plan (railing_kind gdl, pilot B walkway copy) placed: geometry = model, foot 0 (side mounted: -mountDrop) .. H, angle = plan", ok, "; ".join(det_s))
    finally:
        if created:
            ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}} for g in created]})
            left = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in created]})["detailsOfElements"]
            check("test objects deleted", all("error" in d for d in left), f"{len(created)} objects")
    print(f"  live part {time.monotonic() - t0:.1f} s")
    if save:
        ac2 = TapirClient(port=port, timeout=300)
        ac2.guard_project(PROJECT)
        check("SaveProject", bool(ac2.tapir("SaveProject", {}).get("success")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    offline()
    if not args.offline:
        live(args.port, args.save)
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

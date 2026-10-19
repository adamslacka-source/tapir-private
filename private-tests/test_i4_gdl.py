"""
Acceptance test of increment I4 (ADR-001 D5): parametric GDL facade objects from XML templates, without C++.

    python test_i4_gdl.py --offline     # converter, round trip, templates compiled + GDL interpreted (no Archicad)
    python test_i4_gdl.py               # + load into the embedded library, place, change, read 3D back, delete
    python test_i4_gdl.py --with-i3     # + the I3 test (which runs I2 and I1) as a regression
    python test_i4_gdl.py --save        # ... and SaveProject at the end

Live part writes ONLY into C:/FacadePilot/Fasada_pilot_A.pln (guard_project): the 5 FS_* library parts in the
embedded library (folder FS_GDL) and temporary test objects at x >= 3000 m, deleted again at the end (also after a
failure).

Acceptance (ADR-001 I4 + task 2026-10-19):
  K1 LP_XMLConverter of AC29 answers (version, help); libpart2xml -> xml2libpart round trip of a library .gsm is lossless
  K2 every template compiles with the GDL scripts interpreted and no GDL error; a broken script is reported as an error
  K3 load: AddFilesToEmbeddedLibrary + ReloadLibraries succeed, GetAvailableLibraryParts sees the 4 parts as Object with
     the template GUID and the 'Model Element' parent; a second deploy = 0 changes (manifest hash); a forced re-deploy
     while an instance is placed keeps the instance (parameters and 3D)
  K4 per template: CreateObjects at x >= 3000 m, SetGDLParametersOfElements (two parameter sets, the second changes the
     size), GDL read back = sent, 3D bbox (Get3DGeometryOfElements) = expected from the parameters +-5 mm, number of
     blocks (vertices / 8) = expected
  K5 FS_Surround of O5 on the M015_L1 facade (pilot A: it replaced the I3 elements O5_head + O5_jl + O5_jr): its 3D
     extent (s, v, w) = expected from its parameters (+-5 mm), projecting outwards; a test object with the same
     parameters rotated to the facade has the same extent
  K6 3D preview image of one object written (GetElementPreviewImage)
  K7 (--with-i3) the I3 test (incl. I2, I1) still passes
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import base64
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))

import gdl_build  # noqa: E402

OUT = gdl_build.OUT
PROJECT = gdl_build.PROJECT
TOL = 0.005
X0 = 3000.0
LIB_GSM = Path(r"C:\Program Files\Graphisoft\Archicad 29\Knihovna Archicadu 26\Šablony seznamů 26\Data vlastností 26"
               r"\Vlastnosti 26\FP00F33 26.gsm")
MODEL_ELEMENT = "103E8D2C-8230-42E1-9597-46F84CCE28C0"

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return ok


# -- expected geometry: an independent model of each template (local x, y, z; origin = insertion point) -------------
def _louvre_layout(p):
    fw = p["frameW"] if p["bFrame"] else 0.0
    span = p["ZZYZX"] if p["iOrient"] == 0 else p["A"]
    lo, hi = fw, span - fw
    if p["iSpacing"] == 0:
        n = max(1, p["nLam"])
        pitch = (hi - lo) / n
    else:
        pitch = max(p["pitch"], 0.001)
        n = max(1, int((hi - lo) / pitch + 1e-5))
    start = lo + ((hi - lo) - n * pitch) / 2 + pitch / 2
    return fw, [start + i * pitch for i in range(n)]


def expect_louvers(p):
    fw, centres = _louvre_layout(p)
    a = math.radians(p["lamAng"])
    d, t = p["lamD"], p["lamT"]
    yc = p["offs"] + d / 2
    hy = d / 2 * abs(math.cos(a)) + t / 2 * abs(math.sin(a))
    boxes = []
    for c in centres:
        if p["iOrient"] == 0:
            hz = d / 2 * abs(math.sin(a)) + t / 2 * abs(math.cos(a))
            boxes.append((fw, p["A"] - fw, yc - hy, yc + hy, c - hz, c + hz))
        else:
            hx = t / 2 * abs(math.cos(a)) + d / 2 * abs(math.sin(a))
            boxes.append((c - hx, c + hx, yc - hy, yc + hy, fw, p["ZZYZX"] - fw))
    if p["bFrame"]:
        boxes.append((0, p["A"], p["offs"], p["offs"] + p["frameD"], 0, p["ZZYZX"]))
    bb = [min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes),
          min(b[4] for b in boxes), max(b[5] for b in boxes)]
    return bb, len(centres) + (4 if p["bFrame"] else 0)


def expect_spandrel(p):
    return [0, p["A"], p["proj"], p["proj"] + p["thk"], 0, p["ZZYZX"]], max(1, p["nDiv"])


def expect_cladding(p):
    nx = int(p["A"] / max(p["rx"], 0.05) - 1e-5) + 1
    ny = int(p["ZZYZX"] / max(p["ry"], 0.05) - 1e-5) + 1
    return [0, p["A"], p["offs"], p["offs"] + p["thk"], 0, p["ZZYZX"]], nx * ny


def expect_arch(p):
    n = max(int(p["nSeg"]), 2)
    return [-p["rOut"], p["rOut"], -p["depth"], 0, 0, p["rOut"]], ("vertices", 4 * (n + 1) + (2 * (n + 1) if p["bInfill"] else 0))


def expect_surround(p):
    wl = p["bandW"] if p["bLeft"] else 0.0
    wr = p["bandW"] if p["bRight"] else 0.0
    x0, x1 = -wl, p["opW"] + wr
    if p["bTop"]:
        x0, x1 = min(x0, -wl - p["headOvh"]), max(x1, p["opW"] + wr + p["headOvh"])
    z0 = -p["bandW"] if p["bBottom"] else 0.0
    z1 = p["opH"] + (p["bandW"] if p["bTop"] else 0.0)
    return [x0, x1, 0, p["bandD"], z0, z1], sum(bool(p[k]) for k in ("bTop", "bBottom", "bLeft", "bRight"))


CASES = {
    "FS_Louvers": (expect_louvers, [
        {"A": 2.0, "B": 0.2, "ZZYZX": 1.5, "iOrient": 0, "iSpacing": 0, "nLam": 6, "pitch": 0.25, "lamD": 0.2, "lamT": 0.02,
         "lamAng": 0.0, "offs": 0.1, "bFrame": True, "frameW": 0.05, "frameD": 0.2},
        {"A": 3.0, "B": 0.2, "ZZYZX": 2.4, "iOrient": 1, "iSpacing": 1, "nLam": 6, "pitch": 0.3, "lamD": 0.15, "lamT": 0.03,
         "lamAng": 30.0, "offs": 0.12, "bFrame": False, "frameW": 0.05, "frameD": 0.2},
        {"A": 1.8, "B": 0.25, "ZZYZX": 2.1, "iOrient": 0, "iSpacing": 1, "nLam": 6, "pitch": 0.25, "lamD": 0.25, "lamT": 0.025,
         "lamAng": 45.0, "offs": 0.0, "bFrame": True, "frameW": 0.06, "frameD": 0.25}]),
    "FS_SpandrelPanel": (expect_spandrel, [
        {"A": 1.2, "B": 0.05, "ZZYZX": 0.9, "thk": 0.05, "proj": 0.0, "nDiv": 1, "joint": 0.01},
        {"A": 2.7, "B": 0.08, "ZZYZX": 1.1, "thk": 0.08, "proj": 0.15, "nDiv": 3, "joint": 0.02}]),
    "FS_CladdingPanels": (expect_cladding, [
        {"A": 3.0, "B": 0.02, "ZZYZX": 2.0, "rx": 0.6, "ry": 0.6, "joint": 0.008, "thk": 0.02, "offs": 0.05},
        {"A": 4.1, "B": 0.03, "ZZYZX": 2.5, "rx": 1.0, "ry": 0.5, "joint": 0.01, "thk": 0.03, "offs": 0.08}]),
    "FS_Surround": (expect_surround, [
        {"opW": 1.0, "opH": 1.5, "bandW": 0.12, "bandD": 0.03, "bTop": True, "bBottom": False, "bLeft": True, "bRight": True, "headOvh": 0.0},
        {"opW": 1.4, "opH": 2.2, "bandW": 0.15, "bandD": 0.05, "bTop": True, "bBottom": True, "bLeft": True, "bRight": True, "headOvh": 0.05}]),
    "FS_ArchGable": (expect_arch, [
        {"rIn": 1.2, "rOut": 1.65, "depth": 0.57, "nSeg": 24, "bInfill": False, "infRecess": 0.15},
        {"rIn": 0.8, "rOut": 1.1, "depth": 0.45, "nSeg": 16, "bInfill": True, "infRecess": 0.12}]),
}


# -- offline ------------------------------------------------------------------------------------------------------
def offline() -> None:
    conv = gdl_build.converter_version()
    check("K1 LP_XMLConverter answers (AC29)", conv.startswith("LP_XMLConverter 29."), conv)
    h = gdl_build.run_converter(["help"], timeout=30).stdout
    check("K1 help lists xml2libpart / libpart2xml / convertlibrary",
          all(k in h for k in ("xml2libpart", "libpart2xml", "convertlibrary", "-interpret")))
    rt = OUT / "roundtrip"
    rt.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LIB_GSM, rt / "lib.gsm")
    a = gdl_build.run_converter(["libpart2xml", "-l", "UTF8", str(rt / "lib.gsm"), str(rt / "lib.xml")])
    b = gdl_build.run_converter(["xml2libpart", "-l", "UTF8", str(rt / "lib.xml"), str(rt / "back.gsm")])
    c = gdl_build.run_converter(["libpart2xml", "-l", "UTF8", str(rt / "back.gsm"), str(rt / "back.xml")])
    same = all(p.returncode == 0 for p in (a, b, c)) and (rt / "lib.xml").read_bytes() == (rt / "back.xml").read_bytes()
    head = (rt / "lib.xml").read_text(encoding="utf-8-sig")[:400]
    ver = head.split('Version="')[1].split('"')[0] if 'Version="' in head else "?"
    check("K1 round trip library .gsm -> XML -> .gsm -> XML is lossless", same, f"XML Symbol Version {ver}")

    for name in gdl_build.TEMPLATE_NAMES:
        try:
            bld = gdl_build.compile_template(name, conv=conv)
            check(f"K2 {name}: compiled, GDL interpreted without error, round trip GUID + parameters", True,
                  f"{bld['seconds']} s, {len(bld['params'])} parameters, warnings {len(bld['warnings'])}")
        except gdl_build.GdlBuildError as exc:
            check(f"K2 {name}: compiled", False, str(exc)[:300])
    xml = gdl_build.template_path("FS_Louvers").read_text(encoding="utf-8").replace(
        "BLOCK A - 2 * fw, lamD, lamT", "BLOCK A - 2 * fw, lamD lamT")
    bad = OUT / "src" / "_broken"
    if bad.exists():
        shutil.rmtree(bad)
    bad.mkdir(parents=True)
    (bad / "FS_Broken.xml").write_text(xml, encoding="utf-8")
    r = gdl_build.run_converter(["convertlibrary", "-l", "UTF8", "-reportlevel", "2", "-interpret", str(bad), str(OUT / "bin" / "_broken")])
    errs = [ln for ln in r.stdout.splitlines() if ": error:" in ln and "Missing ancestor" not in ln]
    check("K2 a broken 3D script is reported as a GDL error", bool(errs), errs[0].split(": error:")[-1].strip() if errs else r.stdout[-200:])
    for name, (fn, cases) in CASES.items():
        for p in cases:
            n = fn(p)[1]
            assert (n[1] if isinstance(n, tuple) else n) >= 1


# -- live ---------------------------------------------------------------------------------------------------------
def bbox_of(geo: dict, origin: dict) -> tuple[list[float] | None, int]:
    pts = [(v["x"] - origin["x"], v["y"] - origin["y"], v["z"] - origin.get("z", 0.0))
           for b in geo.get("bodies", []) for v in b["vertices"]]
    if not pts:
        return None, 0
    return [min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts),
            min(p[2] for p in pts), max(p[2] for p in pts)], len(pts) // 8


def gdl_writes(p: dict) -> list[dict]:
    return [{"name": k, "value": v} for k, v in p.items()]


def live(port: int, save: bool, with_i3: bool) -> None:
    from tapir_client import TapirClient, results_ok
    import numpy as np
    from geom import frame_of

    t0 = time.monotonic()
    ac = TapirClient(port=port, timeout=120)
    ac.guard_project(PROJECT)
    check("GetAddOnVersion = 1.7.2 (private build)", ac.addon_version() == "1.7.2")

    first = gdl_build.deploy(ac)
    check("K3 deploy: AddFilesToEmbeddedLibrary + ReloadLibraries without error",
          first["reload"] is None or first["reload"].get("success") is True,
          f"changed {first['changed']}, unchanged {first['unchanged']}")
    second = gdl_build.deploy(ac)
    check("K3 second deploy = 0 changes (manifest hash)", not second["changed"], f"unchanged {second['unchanged']}")
    forced = gdl_build.deploy(ac, ("FS_Louvers",), force=True)
    check("K3 forced re-deploy (overwriteExisting) + ReloadLibraries succeed", forced["changed"] == ["FS_Louvers"]
          and forced["reload"].get("success") is True)
    man = gdl_build.load_manifest()["parts"]
    parts = gdl_build.loaded_parts(ac, gdl_build.TEMPLATE_NAMES)
    ok = [n for n in gdl_build.TEMPLATE_NAMES if n in parts and parts[n].get("typeId") == "Object"
          and parts[n].get("guid", "").upper() == man[n]["guid"]]
    check(f"K3 GetAvailableLibraryParts sees {len(gdl_build.TEMPLATE_NAMES)} parts as Object with the template GUID",
          len(ok) == len(gdl_build.TEMPLATE_NAMES),
          ", ".join(f"{n} #{parts[n]['index']}" for n in ok))

    created: list[str] = []
    try:
        k = 0
        previews: dict[str, str] = {}
        for name, (fn, cases) in CASES.items():
            res = ac.tapir("CreateObjects", {"objectsData": [{"libraryPartName": name,
                                                              "coordinates": {"x": X0 + 10 * k, "y": 0.0, "z": 0.0}}]})
            el = res["elements"][0]
            if not check(f"K4 {name}: CreateObjects at x = {X0 + 10 * k:.0f} m", "elementId" in el, json.dumps(el)[:200]):
                k += 1
                continue
            g = el["elementId"]["guid"]
            created.append(g)
            det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}]})["detailsOfElements"][0]
            check(f"K4 {name}: library part and parent subtype (Model Element)",
                  det["details"]["libPart"]["name"] == name and det["details"]["libPart"]["parentUnID"]["guid"].upper() == MODEL_ELEMENT,
                  f"floor {det['floorIndex']}")
            origin = det["details"]["origin"]
            for ci, p in enumerate(cases, 1):
                r = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": g}, "gdlParameters": gdl_writes(p)}]})
                errs = results_ok(r["executionResults"])
                back = ac.tapir("GetGDLParametersOfElements", {"elements": [{"elementId": {"guid": g}}]})["gdlParametersOfElements"][0]
                # Angle parameters: SetGDLParametersOfElements takes degrees (the GDL unit, geometry confirms it),
                # GetGDLParametersOfElements answers radians - gap noted in ADR-001 (I4)
                got = {q["name"]: (math.degrees(q["value"]) if q.get("type") == "Angle" else q.get("value"))
                       for q in back.get("parameters", [])}
                diff = [f"{kk}: {v} -> {got.get(kk)}" for kk, v in p.items()
                        if not (abs(float(got.get(kk, math.nan)) - float(v)) < 1e-6 if not isinstance(v, bool) else bool(got.get(kk)) == v)]
                check(f"K4 {name} set {ci}: SetGDLParametersOfElements + read back = sent", not errs and not diff, "; ".join(errs + diff))
                geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}}]})["geometries3D"][0]
                bb, blocks = bbox_of(geo, origin)
                want, nblocks = fn(p)
                if bb is None:
                    check(f"K4 {name} set {ci}: 3D bbox = spec +-5 mm", False, f"empty geometry: {json.dumps(geo)[:200]}")
                    continue
                dmax = max(abs(a - b) for a, b in zip(bb, want))
                if isinstance(nblocks, tuple):                     # prism template: number of vertices
                    nv = sum(len(b_["vertices"]) for b_ in geo.get("bodies", []))
                    check(f"K4 {name} set {ci}: 3D bbox = spec +-5 mm, vertices = {nblocks[1]}", dmax <= TOL and nv == nblocks[1],
                          f"x {bb[0]:.3f}..{bb[1]:.3f} y {bb[2]:.3f}..{bb[3]:.3f} z {bb[4]:.3f}..{bb[5]:.3f}; "
                          f"max delta {dmax * 1000:.1f} mm; vertices {nv}")
                    continue
                check(f"K4 {name} set {ci}: 3D bbox = spec +-5 mm, blocks = {nblocks}", dmax <= TOL and blocks == nblocks,
                      f"x {bb[0]:.3f}..{bb[1]:.3f} y {bb[2]:.3f}..{bb[3]:.3f} z {bb[4]:.3f}..{bb[5]:.3f}; "
                      f"max delta {dmax * 1000:.1f} mm; blocks {blocks}")
            previews[name] = g
            k += 1

        # K3: re-deploy (overwriteExisting + reload) while an instance is placed - the instance keeps its parameters and 3D
        gs = previews.get("FS_SpandrelPanel")
        if gs:
            red = gdl_build.deploy(ac, ("FS_SpandrelPanel",), force=True)
            det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": gs}}]})["detailsOfElements"][0]
            geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": gs}}]})["geometries3D"][0]
            bb, blocks = bbox_of(geo, det["details"]["origin"])
            want, nb = expect_spandrel(CASES["FS_SpandrelPanel"][1][-1])
            check("K3 re-deploy with a placed instance: instance keeps parameters and 3D (+-5 mm)",
                  red["reload"].get("success") is True and bb is not None and max(abs(a - b) for a, b in zip(bb, want)) <= TOL and blocks == nb,
                  f"blocks {blocks}, bbox {[round(v, 3) for v in bb] if bb else None}")

        # K5: FS_Surround of O5 on the facade (pilot A replaced the I3 head beam + two jamb columns by it)
        import gdl_expect
        state = json.loads((Path(r"C:\FacadePilot\out\M015_L1") / "state.json").read_text(encoding="utf-8"))["elements"]
        spec = json.loads((Path(r"C:\FacadePilot\out\M015_L1") / "M015_L1.facade-spec.json").read_text(encoding="utf-8"))
        fr = frame_of(spec)
        ob = next(o for o in spec["objects"] if o["id"] == "O5_sur")
        sp = {k: v["v"] for k, v in ob["params"].items() if k in gdl_expect.TEMPLATES["FS_Surround"]}
        want_bb = gdl_expect.facade_bbox("FS_Surround", sp, ob["origin"]["v"], spec["reference"]["outside"])
        geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": state["O5_sur"]["guid"]}}]})["geometries3D"][0]
        svw = fr.to_svw(np.array([[v["x"], v["y"], v["z"]] for b in geo["bodies"] for v in b["vertices"]], float))
        got_bb = [svw[:, 0].min(), svw[:, 0].max(), svw[:, 1].min(), svw[:, 1].max(), svw[:, 2].min(), svw[:, 2].max()]
        d_fac = max(abs(a - b) for a, b in zip(got_bb, want_bb))
        check("K5 FS_Surround O5 on the facade: 3D extent s/v/w = expected from its parameters +-5 mm, projects outwards",
              d_fac <= TOL and got_bb[4] >= -TOL and got_bb[5] > got_bb[4],
              f"s {got_bb[0]:.3f}..{got_bb[1]:.3f} v {got_bb[2]:.3f}..{got_bb[3]:.3f} w {got_bb[4]:.3f}..{got_bb[5]:.3f}; max delta {d_fac * 1000:.1f} mm")
        ext_fac = np.array([got_bb[1] - got_bb[0], got_bb[3] - got_bb[2], got_bb[5] - got_bb[4]])
        ang = math.degrees(math.atan2(fr.d[1], fr.d[0])) + (180.0 if spec["reference"]["outside"] == "right_of_ab" else 0.0)
        res = ac.tapir("CreateObjects", {"objectsData": [{"libraryPartName": "FS_Surround", "angle": math.radians(ang),
                                                          "coordinates": {"x": X0 + 100, "y": 0.0, "z": 0.0}}]})
        g = res["elements"][0]["elementId"]["guid"]
        created.append(g)
        ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": g}, "gdlParameters": gdl_writes(sp)}]})
        geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}}]})["geometries3D"][0]
        q = np.array([[v["x"], v["y"], v["z"]] for b in geo["bodies"] for v in b["vertices"]], float)
        rel = q[:, :2] - q[:, :2].min(axis=0)
        ext_obj = np.array([np.ptp(rel @ fr.d), np.ptp(q[:, 2]), np.ptp(rel @ fr.n)])
        wdir = float(np.mean((q[:, :2] - [X0 + 100, 0.0]) @ fr.n))
        dd = np.abs(ext_obj - ext_fac)
        check("K5 test object with the O5 parameters, rotated to the facade: same extent s/v/w as the facade object +-5 mm, projects outwards",
              float(dd.max()) <= TOL and wdir > 0,
              f"facade {ext_fac[0]:.3f} x {ext_fac[1]:.3f} x {ext_fac[2]:.3f}, object {ext_obj[0]:.3f} x {ext_obj[1]:.3f} x {ext_obj[2]:.3f} m, "
              f"angle {ang:.2f} deg; params {sp}")

        # K6: 3D preview images (louvres with the 45 deg set, the others with their last set)
        ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": previews["FS_Louvers"]},
                                                                               "gdlParameters": gdl_writes(CASES["FS_Louvers"][1][2])}]})
        written = []
        for name, pg in previews.items():
            im = ac.tapir("GetElementPreviewImage", {"elementId": {"guid": pg}, "imageType": "3D", "format": "png",
                                                     "width": 512, "height": 512}, timeout=60)
            png = OUT / f"preview_{name}.png"
            png.write_bytes(base64.b64decode(im["previewImage"]))
            if png.stat().st_size > 1000:
                written.append(png.name)
        check("K6 3D previews written (GetElementPreviewImage)", len(written) == len(previews), f"{OUT}: {', '.join(written)}")
    finally:
        if created:
            r = ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}} for g in created]})
            left = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in created]})["detailsOfElements"]
            check("test objects deleted", all("error" in d for d in left), f"{len(created)} objects")

    print(f"  live part {time.monotonic() - t0:.1f} s")
    if with_i3:
        t1 = time.monotonic()
        p = subprocess.run([sys.executable, str(TESTS / "test_i3_m015.py"), "--port", str(port)], capture_output=True, text=True,
                           timeout=2400, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        summ = [ln for ln in p.stdout.splitlines() if "checks passed" in ln]
        check("K7 I3 test (incl. I2, I1) still passes", p.returncode == 0, (summ[-1].strip() if summ else p.stdout[-300:]) + f" ({time.monotonic() - t1:.0f} s)")
        if p.returncode != 0:
            print("\n".join(ln for ln in p.stdout.splitlines() if "FAIL" in ln))
    if save:
        ac2 = TapirClient(port=port, timeout=300)
        ac2.guard_project(PROJECT)
        check("SaveProject", bool(ac2.tapir("SaveProject", {}).get("success")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--with-i3", action="store_true")
    args = ap.parse_args()
    offline()
    if not args.offline:
        live(args.port, args.save, args.with_i3)
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

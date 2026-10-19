"""
Acceptance test of increment I3 (ADR-001, modelling part): linear elements, gate with transom, native wall inset on
the Senec facade M015_L1.

    python test_i3_m015.py --offline     # spec + validator rules only, no Archicad
    python test_i3_m015.py               # full: I2 test (incl. I1) -> build -> build again -> verify -> independent checks
    python test_i3_m015.py --save        # ... and SaveProject at the end

Live part writes ONLY into C:/FacadePilot/Fasada_pilot_A.pln (build refuses any other open project): elements recorded
in C:/FacadePilot/out/M015_L1/state.json, FS attributes / favourites / profiles, and a temporary calibration wall with
one window and one door at x >= 3000 m that is deleted again at the end of the check (also after a failure).

Acceptance (ADR-001 I3 + task 2026-10-19):
  C1 cross-section area of every FS profile read back from Archicad (GetProfiles) = polygon of the spec +-1 %
  C2 height of the cornice, string course and sill ledges / surround heads (3D geometry of the beam segments) = spec +-1 cm
  C3 overlay: cornice edges projected with the camera of the photo vs the photo +-3 cm; overlay PNGs written now
  C4 O2 is 'Double Door with Transom 27' with a transom; GDL read back = spec (transom height, leaves, surface); no exception left
  C5 wall inset native (revealDepthLocation Side + revealDepthOffset = spec) and geometrically identical to the I2
     calibration (Core, offset - outer skins) within 1 cm - checked on a temporary window and door
  C6 the I1 and I2 tests still pass
  C7 second build = 0 changes
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import copy
import csv
import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

from spec_validate import polygon_area, validate  # noqa: E402
from from_senec import build_spec  # noqa: E402

OUT = Path(r"C:\FacadePilot\out")
PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
HOUSE = "M015_L1"
CAL_X = 3000.0

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return ok


def offline() -> dict:
    spec = build_spec(HOUSE)
    errors, warnings = validate(spec)
    check("I3 spec from the adapter is valid", not errors, "; ".join(errors))
    kinds = sorted({it["kind"] for it in spec.get("linear", [])})
    objs = spec.get("objects", [])
    # pilot A: the surround (head + 2 jambs, 3 elements per window in I3) is ONE GDL object FS_Surround per window
    check("spec has cornice, string course, sill ledges (5 linear elements) and 3 surrounds as FS_Surround objects",
          len(spec.get("linear", [])) == 5 and kinds == ["cordon", "cornice", "sill_ledge"]
          and sorted(o["host"] for o in objs if o["kind"] == "surround") == ["O5", "O6", "O7"]
          and all(spec["systems"][o["system"]]["template"] == "FS_Surround" for o in objs),
          f"{len(spec.get('linear', []))} {kinds}; objects {[o['id'] for o in objs]}")
    l1 = next(it for it in spec["linear"] if it["id"] == "L1")
    check("cornice at v = 7.30 (EAVE of the pilot) over the whole facade", abs(l1["v"]["v"] - 7.30) < 1e-9 and l1["s"]["v"][0] == 0, str(l1["s"]["v"]))
    gate = spec["systems"]["door.gate"]
    check("gate system = Double Door with Transom 27 placed by library part name", gate.get("libpart") == "Double Door with Transom 27" and gate.get("base_libpart") is True)
    cam = next(s for s in spec["sources"] if s.get("rect"))["rect"].get("camera")
    check("photo source carries the camera of the rectification", bool(cam) and cam["D"] > 0, f"s {cam['s']}, v {cam['v']}, D {cam['D']}" if cam else "")

    bad = copy.deepcopy(spec)
    sl = next(it for it in bad["linear"] if it["id"] == "O5_sill")
    sl["v"]["v"] = 5.0                                           # sill ledge pushed up in front of the window
    check("validator: linear element in front of an opening is an error", any("in front of opening O5" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    next(o for o in bad["objects"] if o["id"] == "O6_sur")["origin"]["v"][0] = 3.20   # surround shifted onto O6's left jamb
    check("validator: surround not at the jamb of its opening is an error", any("origin s at the jamb" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["systems"]["prof.kordon"]["polygon_wv"][0] = [-0.05, 0]
    check("validator: profile reaching into the wall (x < 0) is an error", any("x >= 0" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    next(o for o in bad["objects"] if o["id"] == "O6_sur")["params"]["opW"]["v"] = 1.17
    check("validator: surround width != width of its opening is an error", any("opening width opW" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["systems"]["prof.rimsa_okap"]["h"]["v"] = 0.12
    check("validator: profile h != polygon extent is an error", any("polygon extent" in e for e in validate(bad)[0]))
    return spec


def _segments_geometry(ac, guids):
    from linear_verify import geometry_of
    return geometry_of(ac, guids)


def calibration_inset(ac, spec) -> None:
    """C5: on a temporary composite wall, Side R must give the same 3D as the I2 calibration (Core, R - outer skins)."""
    import systems as fs_systems
    from tapir_client import results_ok
    attrs = fs_systems.Attrs(ac)
    wsys = spec["systems"][spec["zones"][0]["system"]]
    comp = attrs.composite[wsys["composite"]["name"]]
    outer = 0.0
    for sk in wsys["composite"]["skins"]:
        if sk["type"] == "Core":
            break
        outer += sk["thickness"]["v"]
    made = []
    try:
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})
        r = ac.tapir("CreateWalls", {"wallsData": [{"begCoordinate": {"x": CAL_X + 12, "y": 0}, "endCoordinate": {"x": CAL_X, "y": 0}, "zCoordinate": 0, "height": 4.0,
                                                    "structureType": "Composite", "compositeId": comp["attributeId"], "referenceLineLocation": "Outside"}]})
        wall = r["elements"][0]["elementId"]["guid"]
        made.append(wall)
        for kind, cmd, key, mod, mkey, fav, R in (("window", "CreateWindows", "windowsData", "ModifyWindows", "windowsWithDetails", "FS win.2kr", 0.12),
                                                  ("door", "CreateDoors", "doorsData", "ModifyDoors", "doorsWithDetails", "FS door.gate", 0.30)):
            r = ac.tapir(cmd, {key: [{"ownerWallId": {"guid": wall}, "centerOffset": 3.0 if kind == "window" else 8.0, "width": 1.0, "height": 2.0,
                                      "sillHeight": 0.9 if kind == "window" else 0.0, "favoriteName": fav}]})
            g = r["elements"][0]["elementId"]["guid"]
            made.append(g)

            def geo():
                gg = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}}]})["geometries3D"][0]
                return np.array([[v["x"], v["y"], v["z"]] for b in gg["bodies"] for v in b["vertices"]])

            r1 = ac.tapir(mod, {mkey: [{"elementId": {"guid": g}, "refSide": True, "revealDepthLocation": "Core", "revealDepthOffset": round(R - outer, 6)}]})
            a = geo()
            r2 = ac.tapir(mod, {mkey: [{"elementId": {"guid": g}, "refSide": True, "revealDepthLocation": "Side", "revealDepthOffset": R}]})
            b = geo()
            ok = not results_ok(r1["executionResults"]) and not results_ok(r2["executionResults"]) and a.shape == b.shape and len(a) > 0
            diff = float(np.abs(a - b).max()) if ok else float("nan")
            check(f"C5 {kind}: Side {R} = I2 calibration Core {R - outer:.2f} (max vertex difference <= 1 cm)", ok and diff <= 0.01,
                  f"max {diff * 1000:.1f} mm over {len(a)} vertices")
    finally:
        if made:
            ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": x}} for x in reversed(made)]})


def live(spec: dict, port: int, save: bool) -> None:
    from build import build, existing, gdl_params
    from tapir_client import TapirClient
    from verify import verify
    import systems as fs_systems

    spec_path = OUT / HOUSE / f"{HOUSE}.facade-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    # C6: the I2 test (which runs the I1 test first, with a forced rebuild of every element)
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(TESTS / "test_i2_m015.py"), "--port", str(port)], capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    lines = [ln for ln in p.stdout.splitlines() if "checks passed" in ln or ln.startswith("[FAIL]") or ln.startswith("    I1 | [FAIL]")]
    i1 = [ln for ln in p.stdout.splitlines() if ln.startswith("    I1 |") and "checks passed" in ln]
    check("C6 I1 test still passes", bool(i1) and "PASS" in i1[-1], i1[-1].strip() if i1 else p.stdout[-300:])
    check("C6 I2 test still passes", p.returncode == 0, "; ".join(lines[-4:]) if lines else p.stderr[-300:])

    first = build(spec, PROJECT, port, OUT, save=False, force=False, verbose=False)
    second = build(spec, PROJECT, port, OUT, save=False, force=False, verbose=False)
    check("build without read-back mismatches", not first["mismatches"] and not second["mismatches"], "; ".join(first["mismatches"] + second["mismatches"]))
    check("C7 second build = 0 changes (elements, profiles, favourites)", second["n_changes"] == 0, f"{second['changes']} in {second['seconds']} s")

    ver = verify(spec, PROJECT, port, OUT, verbose=False)
    check("verify PASS (openings, linear, types, properties)", ver["pass"], "; ".join(ver["problems"]))
    check("linear elements / objects on the facade layer = spec (5 beams, 0 columns, 3 FS_Surround objects)",
          ver["count"]["linear_model"] == ver["count"]["linear_spec"] == {"beam": 5, "column": 0, "object": 3}, str(ver["count"]["linear_model"]))
    tol = spec["tolerances"]["build"] * 100
    check("delta of all elements <= 1 cm (parameters and 3D)", max(ver["max_delta_cm"].values()) <= tol + 1e-9, str(ver["max_delta_cm"]))

    ac = TapirClient(port=port, timeout=120)
    ac.guard_project(PROJECT)
    st = json.loads((OUT / HOUSE / "state.json").read_text(encoding="utf-8"))["elements"]

    # C1: profile areas, read independently
    attrs = fs_systems.Attrs(ac)
    for sid, s in spec["systems"].items():
        if s["type"] != "profile":
            continue
        a = attrs.profile.get(s["name"])
        if not check(f"C1 profile {s['name']} exists", a is not None):
            continue
        det = ac.tapir("GetProfiles", {"attributeIds": [{"attributeId": a["attributeId"]}]})["profiles"][0]
        area, want = fs_systems.profile_area(det), polygon_area(s["polygon_wv"])
        check(f"C1 profile {s['name']}: area {area:.6f} m2 = polygon {want:.6f} m2 +-1 %", abs(area - want) <= 0.01 * want,
              f"{(area - want) / want * 100:+.3f} %; {det.get('width')} x {det.get('height')} m, beam {det.get('beamType')}, column {det.get('coluType')}")

    # C2: heights from the 3D geometry of the beam segments, computed here independently of verify
    from geom import frame_of
    fr = frame_of(spec)
    horiz = [it for it in spec["linear"] if it["axis"] == "h"]
    geos = _segments_geometry(ac, [st[it["id"]]["guid"] for it in horiz])
    for it, g in zip(horiz, geos):
        pts = np.array([[v["x"], v["y"], v["z"]] for b in g["bodies"] for v in b["vertices"]])
        if not check(f"C2 {it['id']} has 3D (beam segments)", len(pts) > 0):
            continue
        svw = fr.to_svw(pts)
        prof = spec["systems"][spec["systems"][it["system"]]["profile"]]
        v0, v1 = it["v"]["v"], it["v"]["v"] + max(p[1] for p in prof["polygon_wv"])
        d0, d1 = svw[:, 1].min() - v0, svw[:, 1].max() - v1
        check(f"C2 {it['id']} ({it['kind']}) bottom {v0:.3f} / top {v1:.3f} m = 3D +-1 cm",
              abs(d0) <= 0.01 and abs(d1) <= 0.01, f"delta {d0 * 100:+.2f} / {d1 * 100:+.2f} cm; w {svw[:, 2].min():.3f}..{svw[:, 2].max():.3f}")

    # C3: overlay of the cornice (+ the other photo edges for the record)
    edges = ver["linear"]["photo_edges"]
    l1 = [e for e in edges if e["item"] == "L1"]
    check("C3 cornice L1: both edges on the photo within +-3 cm (camera projection)", len(l1) == 2 and all(abs(e["delta_cm"]) <= 3.0 for e in l1),
          "; ".join(f"{e['edge']}: photo {e['v_photo']:.3f}, model {e['v_model_projected']:.3f}, delta {e['delta_cm']:+.1f} cm" for e in l1))
    others = [e for e in edges if e["item"] != "L1"] + ver["photo_edges"][len(edges):]
    check("C3 other edges (string course +-3 cm, sill ledges +-5 cm, surrounds and masonry openings of the 2nd floor +-3 cm)",
          all(abs(e["delta_cm"]) <= e["tol_cm"] for e in others) and len(others) == 2 + 3 + 3 * 3 + 3 * 3,
          f"max |delta| {max(abs(e['delta_cm']) for e in others):.1f} cm over {len(others)} edges")
    for key in ("overlay_png",):
        pth = Path(ver[key])
        check(f"C3 {pth.name} written now", pth.exists() and pth.stat().st_mtime > time.time() - 900, str(pth))
    lp = Path(ver["linear"]["overlay_png"] or "")
    check(f"C3 {lp.name} (zoomed crops) written now", lp.exists() and lp.stat().st_mtime > time.time() - 900, str(lp))

    # C4: gate O2
    g = st["O2"]["guid"]
    det = existing(ac, [g])[g]["details"]
    par = gdl_params(ac, [g])[g]
    check("C4 O2 library part = Double Door with Transom 27", det["libPart"]["name"] == "Double Door with Transom 27", det["libPart"]["name"])
    # 0.89 until I7; since I7 the spec value (top of the gate read on the masonry opening, the transom bar kept in place;
    # facade/corrections/M015_L1.json) - the check compares with the spec, it is not weaker
    o2_th = next(o for o in spec["openings"] if o["id"] == "O2")["transom_h"]["v"]
    check(f"C4 O2 transom {o2_th} m (spec) read back (gs_transom_height), transom present (ifc_TransomThickness > 0)",
          abs(par["gs_transom_height"] - o2_th) < 1e-6 and par["ifc_TransomThickness"] > 0 and par["gs_door_transom"] is True,
          f"{par['gs_transom_height']}, transom offset {par.get('ifc_TransomOffset')}")
    check("C4 O2 two leaves", par["ifc_optypestr"].startswith("Double Door"), par["ifc_optypestr"])
    trows = list(csv.DictReader(open(ver["types_csv"], encoding="utf-8")))
    o2 = [r for r in trows if r["item"] == "O2"]
    check("C4 O2 GDL read back = spec (all type rows OK)", o2 and all(r["status"] == "OK" for r in o2), f"{len(o2)} rows: " + ", ".join(f"{r['what']}={r['model']}" for r in o2[:6]))
    check("C4 no exception left (I2 gate transom exception removed)", not ver["exceptions"], str(ver["exceptions"]))

    # C5: native inset on every opening + calibration
    dets = existing(ac, [st[o["id"]]["guid"] for o in spec["openings"] if not o.get("merged_into")])
    bad = []
    for o in spec["openings"]:
        if o.get("merged_into") or "reveal" not in o:
            continue
        d = dets[st[o["id"]]["guid"]]["details"]
        if d.get("revealDepthLocation") != "Side" or d.get("refSide") is not True or abs(d.get("revealDepthOffset", -1) - o["reveal"]["v"]) > 0.01:
            bad.append(f"{o['id']}: {d.get('revealDepthLocation')} {d.get('revealDepthOffset')} refSide {d.get('refSide')}")
    check("C5 every opening: revealDepthLocation Side, offset = spec reveal (delta <= 1 cm)", not bad, "; ".join(bad) or "6/6")
    check("C5 build.py has no outer-skin subtraction any more", "outer_skins" not in (FACADE / "build.py").read_text(encoding="utf-8"))
    calibration_inset(ac, spec)

    # linear: element IDs carry '?' (profile projection is ODHAD), FS properties, FS favourites
    lin_ids = [it["id"] for it in spec["linear"]]
    dd = existing(ac, [st[i]["guid"] for i in lin_ids])
    ids_ok = all(dd[st[i]["guid"]]["id"] == f"{HOUSE}.{i}?" for i in lin_ids)
    check("linear Element IDs '<spec>.<id>?' (ODHAD projection)", ids_ok, ", ".join(dd[st[i]["guid"]]["id"] for i in lin_ids[:3]) + " ...")
    _, props = fs_systems.property_ids(ac)
    pv = ac.tapir("GetPropertyValuesOfElements", {"elements": [{"elementId": {"guid": st[i]["guid"]}} for i in lin_ids],
                                                  "properties": [{"propertyId": {"guid": props[n]}} for n in ("FS_zdroj", "FS_istota", "FS_spec")]})["propertyValuesForElements"]
    empty = [f"{i}.{n}" for i, vals in zip(lin_ids, pv) for n, v in zip(("FS_zdroj", "FS_istota", "FS_spec"), vals.get("propertyValues", []))
             if not v.get("propertyValue", {}).get("value")]
    check("every linear element has FS_zdroj, FS_istota, FS_spec", not empty, "14 x 3" if not empty else str(empty))
    favs = {k: set(ac.tapir("GetFavoritesByType", {"elementType": k}).get("favorites", [])) for k in ("Beam", "Door")}
    want = {"Beam": ["FS lin.rimsa_okap", "FS lin.kordon", "FS lin.parapet_rimsa"], "Door": ["FS door.gate"]}
    miss = [n for k, ns in want.items() for n in ns if n not in favs[k]]
    check("FS favourites of the linear systems and FS door.gate exist", not miss, "4/4" if not miss else str(miss))
    sur_ids = [o["id"] for o in spec["objects"]]
    sd = existing(ac, [st[i]["guid"] for i in sur_ids])
    lp_ok = [i for i in sur_ids if sd.get(st[i]["guid"], {}).get("details", {}).get("libPart", {}).get("name") == "FS_Surround"]
    check("surrounds O5..O7 are FS_Surround objects (one element per window)", lp_ok == sur_ids, str(lp_ok))
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
    spec = offline()
    if not args.offline:
        live(spec, args.port, args.save)
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""
Acceptance test of increment I2 (ADR-001): systems catalogue on the Senec facade M015_L1.

    python test_i2_m015.py --offline     # spec + validator rules only, no Archicad
    python test_i2_m015.py               # full: I1 test -> build -> build again -> verify -> independent read-back -> G1
    python test_i2_m015.py --save        # ... and SaveProject at the end

Live part writes ONLY into C:/FacadePilot/Fasada_pilot_A.pln (build refuses any other open project), only
elements recorded in C:/FacadePilot/out/M015_L1/state.json and FS attributes / favourites / properties.

Acceptance (ADR-001 I2 + task 2026-10-19):
  B1 every fill type of the spec -> its 'FS *' favourite exists (GetFavoritesByType)
  B2 GDL parameters read back = spec (leaves, transom, sidelight, glazing bars, instance values, wall inset);
     a field the library part cannot carry must be an explicit, listed EXCEPTION - never a silent pass
  B3 the wall has the build-up of the spec (Composite, skins in order, thickness)
  B4 every facade element has FS_zdroj and FS_istota (and FS_spec)
  B5 the I1 test still passes (delta of openings <= 1 cm)
  B6 second build = 0 changes (elements AND systems)
  B7 new overlay + G1 table exist
"""
from __future__ import annotations

import sys
if hasattr (sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure (encoding='utf-8', errors='replace')

import argparse
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

from spec_validate import fs_system, validate  # noqa: E402
from from_senec import build_spec  # noqa: E402

OUT = Path(r"C:\FacadePilot\out")
PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
HOUSE = "M015_L1"
# I2 had the gate transom as a documented exception (Tapir gap T7); I3 builds the gate from 'Double Door with
# Transom 27' placed by libraryPartName (T7 fixed), so no exception is expected any more.
KNOWN_EXCEPTIONS: set = set()

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def offline() -> dict:
    spec = build_spec(HOUSE)
    errors, warnings = validate(spec)
    check("I2 spec from the adapter is valid", not errors, "; ".join(errors))
    fav = {s["favorite"] for s in spec["systems"].values() if fs_system(s) and s["type"] in ("wall", "window", "door")}
    check("spec has 4 FS systems (wall, window, shop door, gate)", fav == {"FS wall.omietka_tehla_450", "FS win.2kr", "FS door.shop_sl", "FS door.gate"}, str(sorted(fav)))
    check("O4 is merged into O3 (one frame on the photo)", next(o for o in spec["openings"] if o["id"] == "O4").get("merged_into") == "O3")

    bad = copy.deepcopy(spec)
    bad["systems"]["wall.omietka_tehla_450"]["composite"]["skins"][1]["thickness"]["v"] = 0.30
    check("validator: composite thickness != zone thickness is an error", any("skins sum" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["openings"][3]["rect"]["v"][0] = 6.40
    bad["openings"][3]["rect"]["v"][1] = 7.17
    check("validator: merged sidelight not adjacent is an error", any("must start where" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["systems"]["win.2kr"]["libpart"] = "Window 99"
    check("validator: library part not in gdl_param_map is an error", any("not in gdl_param_map" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["systems"]["win.2kr"]["favorite"] = "Plastové okno 2kr"
    check("validator: FS favourite name without 'FS ' is an error", any("must start with 'FS '" in e for e in validate(bad)[0]))
    return spec


def live(spec: dict, port: int, save: bool) -> None:
    from build import build, existing, gdl_params
    from g1 import write_g1
    from tapir_client import TapirClient
    from verify import verify
    import systems as fs_systems

    spec_path = OUT / HOUSE / f"{HOUSE}.facade-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    # B5: the I1 acceptance test, unchanged, on the I2 spec (forced rebuild of every element + second build + verify)
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(TESTS / "test_i1_m015.py"), "--port", str(port)], capture_output=True, text=True,
                       timeout=600, encoding="utf-8", errors="replace", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    tail = [ln for ln in p.stdout.splitlines() if ln.startswith("[") or "checks passed" in ln]
    for ln in tail:
        print("    I1 |", ln)
    check("B5 I1 test still passes (incl. delta <= 1 cm)", p.returncode == 0, tail[-1] if tail else p.stderr[-300:])

    first = build(spec, PROJECT, port, OUT, save=False, force=False)
    second = build(spec, PROJECT, port, OUT, save=False, force=False)
    check("build without read-back mismatches", not first["mismatches"] and not second["mismatches"], "; ".join(first["mismatches"] + second["mismatches"]))
    check("B6 second build = 0 changes (elements + systems)", second["n_changes"] == 0, f"{second['changes']} in {second['seconds']} s")

    ver = verify(spec, PROJECT, port, OUT)
    tol = spec["tolerances"]["build"] * 100
    check("verify PASS (count, geometry, types, properties)", ver["pass"], "; ".join(ver["problems"]))
    check("B5 delta <= 1 cm (parameters and 3D incl. wall holes)", max(ver["max_delta_cm"].values()) <= tol + 1e-9, f"{ver['max_delta_cm']}")
    check("one element per unit (O3+O4 = 1 door): 3 windows + 3 doors", ver["count"]["model_in_wall"] == {"window": 3, "door": 3}, str(ver["count"]))

    ac = TapirClient(port=port, timeout=90)
    ac.guard_project(PROJECT)
    # B1: favourites, read independently
    want = {"Wall": ["FS wall.omietka_tehla_450"], "Window": ["FS win.2kr"], "Door": ["FS door.shop_sl", "FS door.gate"]}
    missing = []
    for kind, names in want.items():
        have = set(ac.tapir("GetFavoritesByType", {"elementType": kind}).get("favorites", []))
        missing += [n for n in names if n not in have]
    check("B1 FS favourites exist for every system of the spec", not missing, f"missing {missing}" if missing else "4/4")

    # B2: types from verify + explicit, independent spot checks of the GDL parameters
    trows = list(__import__("csv").DictReader(open(ver["types_csv"], encoding="utf-8")))
    fails = [r for r in trows if r["status"] == "FAIL"]
    excs = {(r["item"], r["what"]) for r in trows if r["status"] == "EXC"}
    check("B2 GDL read back = spec, no FAIL rows", not fails, f"{len(trows)} rows, {sum(r['status'] == 'OK' for r in trows)} OK, {len(excs)} EXC"
          + (f"; FAIL {[(r['item'], r['what']) for r in fails]}" if fails else ""))
    exc_list = {(e.get("item", e["system"]), e["field"]) for e in ver["exceptions"]}
    check("B2 no exceptions left (gate transom realised since I3)", exc_list == KNOWN_EXCEPTIONS, str(sorted(exc_list)))
    st = json.loads((OUT / HOUSE / "state.json").read_text(encoding="utf-8"))["elements"]
    gp = gdl_params(ac, [st[i]["guid"] for i in ("O1", "O2", "O3", "O5")])
    g = {i: gp[st[i]["guid"]] for i in ("O1", "O2", "O3", "O5")}
    spot = {
        "O5 2 sashes (Double Window 27, both opening types set)": sum(1 for k in ("gs_optype_05", "gs_optype_01") if g["O5"].get(k)) == 2,
        "O5 glazing bar: 'Divided' at 0.47": g["O5"]["iMullionStyle_05"] == 6 and abs(g["O5"]["gs_horiz_div_height_05"] - 0.47) < 1e-6,
        "O5 no transom": g["O5"]["gs_UTrans"] is False,
        "O3 one leaf + fixed sidelight + transom": g["O3"]["ifc_optypestr"] == "Swing Fixed" and g["O3"]["ifc_TransomThickness"] > 0,
        "O3 sidelight 0.84 visible (O4 width)": abs(g["O3"]["gs_sidelight_width_right"] - g["O3"]["gs_frame_width"] - 0.84) < 1e-6,
        "O3 sidelight parapet 0.48 (O4 sill - O3 sill)": abs(g["O3"]["gs_sidelight_parapet_hgt_right"] - 0.48) < 1e-6,
        "O3 transom 0.94 divided": abs(g["O3"]["gs_transom_height"] - 0.94) < 1e-6 and g["O3"]["iMullionStyle_02"] == 14,
        "O1 transom 0.55, sidelight 0.60 on 0.63 parapet": abs(g["O1"]["gs_transom_height"] - 0.55) < 1e-6 and
            abs(g["O1"]["gs_sidelight_width_right"] - g["O1"]["gs_frame_width"] - 0.60) < 1e-6 and abs(g["O1"]["gs_sidelight_parapet_hgt_right"] - 0.63) < 1e-6,
        "O2 two leaves": g["O2"]["ifc_optypestr"].startswith("Double Door"),
        "O2 transom 0.89 (Double Door with Transom 27)": abs(g["O2"].get("gs_transom_height", 0) - 0.89) < 1e-6 and g["O2"]["ifc_TransomThickness"] > 0,
    }
    # wall inset: native since I3 (revealDepthLocation Side; AC_wido_sill is not refreshed by Archicad for Side)
    dets = existing(ac, [st[i]["guid"] for i in ("O1", "O3", "O5")])
    ins = [dets[st[i]["guid"]]["details"] for i in ("O1", "O3", "O5")]
    spot.update({
        "wall inset O1 0.30 / O3 0.25 / O5 0.12 (revealDepthOffset, Side, refSide)": [round(d["revealDepthOffset"], 4) for d in ins] == [0.30, 0.25, 0.12]
            and all(d["revealDepthLocation"] == "Side" and d["refSide"] is True for d in ins),
    })
    for name, ok in spot.items():
        check(f"B2 spot: {name}", ok)

    # B3: wall build-up, read independently
    wd = existing(ac, [st["Z1"]["guid"]])[st["Z1"]["guid"]]["details"]
    attrs = fs_systems.Attrs(ac)
    comp_name = next((n for n, a in attrs.composite.items() if a["attributeId"]["guid"] == (wd.get("compositeId") or {}).get("guid")), None)
    comp = spec["systems"]["wall.omietka_tehla_450"]["composite"]
    diffs = fs_systems.composite_differences(ac, comp, attrs) if comp_name == comp["name"] else ["wall uses another composite"]
    check("B3 wall is Composite with the spec build-up (outside -> inside)", wd["structureType"] == "Composite" and comp_name == comp["name"] and not diffs,
          f"{wd['structureType']}, {comp_name}, " + (" | ".join(f"{s['material']} {s['thickness']['v']}" for s in comp["skins"]) if not diffs else "; ".join(diffs)))
    check("B3 wall thickness = zone thickness", abs(wd["begThickness"] - spec["zones"][0]["thickness"]["v"]) < 1e-6, f"{wd['begThickness']}")

    # B4: properties on every element, read independently
    _, props = fs_systems.property_ids(ac)
    names = ["FS_zdroj", "FS_istota", "FS_spec"]
    ok_props = len(props) == 3
    empty = []
    if ok_props:
        pv = ac.tapir("GetPropertyValuesOfElements", {"elements": [{"elementId": {"guid": e["guid"]}} for e in st.values()],
                                                      "properties": [{"propertyId": {"guid": props[n]}} for n in names]})["propertyValuesForElements"]
        for item, vals in zip(st, pv):
            for n, v in zip(names, vals.get("propertyValues", [])):
                if not v.get("propertyValue", {}).get("value"):
                    empty.append(f"{item}.{n}")
    check("B4 every facade element has FS_zdroj, FS_istota, FS_spec", ok_props and not empty, f"{len(st)} elements x 3" if not empty else f"empty {empty}")

    # B7: overlay + G1
    g1 = write_g1(spec, ver["exceptions"], OUT)
    n_rows = sum(1 for ln in g1.read_text(encoding="utf-8").splitlines() if ln.startswith("| ") and ln[2:3].isdigit())
    check("B7 overlay PNG written now", Path(ver["overlay_png"]).exists() and Path(ver["overlay_png"]).stat().st_mtime > time.time() - 600, ver["overlay_png"])
    check("B7 G1 table written", n_rows >= len(spec["assumptions"]), f"{g1} ({n_rows} rows)")
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
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS")
          + f"; documented exceptions: {sorted(KNOWN_EXCEPTIONS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

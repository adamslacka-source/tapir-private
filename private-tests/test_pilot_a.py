"""
Acceptance test of pilot A (ADR-001, section 9 "Pilot A - zóny, M062, M016"): several wall zones per facade (stone
cladding of the ground floor, rizalit, corner = second plane), GDL objects (FS_Surround, FS_ArchGable), and the houses
M015_L1, M062_L2, M016_L2 of the Senec pilot built from the pilot data through the adapter.

    python test_pilot_a.py --offline          # adapter + validator for the three houses, no Archicad
    python test_pilot_a.py                    # + I1..I4 regression (test_i4_gdl.py --with-i3), then per house:
                                              #   build, build again, verify, independent read-back, G1
    python test_pilot_a.py --skip-regression  # without the I1..I4 run
    python test_pilot_a.py --save             # ... and SaveProject at the end

Writes ONLY into C:/FacadePilot/Fasada_pilot_A.pln (build refuses any other open project): elements recorded in
C:/FacadePilot/out/<house>/state.json, FS attributes / favourites / profiles, the FS_* GDL parts.

Acceptance per house (task 2026-10-19):
  P1 number of openings in the model (windows + doors hosted by the zone walls; merged openings count once) = spec,
     and independently: walls / windows + doors / beams + columns / objects on the facade layer = spec
  P2 delta of parameters and 3D <= 1 cm against the spec (joined corners, hole areas: own tolerances, listed)
  P3 overlay: every edge named in the spec (photo_edges of openings, objects, linear elements) within its tolerance
     (3 cm; 5 cm for sill ledges); every 'meranie' / 'kota' opening edge of the photo plane that the automatic check
     cannot confirm (<= 3 cm) is reported in the spec (simplifications) - never passed silently; overlay PNGs written now
  P4 build-ups of the zones read back: every zone wall is Composite with the composite of its system, skins = spec
  P5 second build = 0 changes
  P6 GDL objects: library part = template, 3D extent = expected, arch radii (FS_ArchGable)
  P7 the I1..I4 tests still pass
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

from spec_validate import validate  # noqa: E402
from from_senec import build_spec  # noqa: E402

OUT = Path(r"C:\FacadePilot\out")
PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
HOUSES = {
    # house: (zones, planes, units window/door, linear, objects, zone roles)
    "M015_L1": {"zones": 2, "planes": 0, "units": {"window": 3, "door": 3}, "linear": 5, "objects": 3, "roles": {"main", "parter"}},
    "M062_L2": {"zones": 2, "planes": 0, "units": {"window": 6, "door": 2}, "linear": 5, "objects": 0, "roles": {"main", "parter"}},
    "M016_L2": {"zones": 4, "planes": 1, "units": {"window": 10, "door": 3}, "linear": 16, "objects": 1, "roles": {"main", "rizalit", "side"}},
}

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return ok


def ids_in(ref: str) -> set[str]:
    """Opening ids named in a simplification ref: 'O1', 'O5..O7' and lists."""
    out = set()
    for a, b in re.findall(r"O(\d+)\.\.O(\d+)", ref):
        out |= {f"O{i}" for i in range(int(a), int(b) + 1)}
    out |= set(re.findall(r"\bO\d+\b", ref))
    return out


# -- offline ------------------------------------------------------------------------------------------------------
def offline() -> dict:
    specs = {}
    for house, want in HOUSES.items():
        spec = build_spec(house)
        errors, warnings = validate(spec)
        check(f"{house}: spec from the adapter is valid", not errors, "; ".join(errors[:3]))
        units = {"window": 0, "door": 0}
        for o in spec["openings"]:
            if not o.get("merged_into"):
                units[o["kind"]] += 1
        roles = {z.get("role") for z in spec["zones"]}
        check(f"{house}: {want['zones']} wall zones ({', '.join(sorted(want['roles']))}), {len(spec.get('planes', []))} further plane(s)",
              len(spec["zones"]) == want["zones"] and roles == want["roles"] and len(spec.get("planes", [])) == want["planes"],
              f"{[(z['id'], z.get('plane', 'F'), z.get('role'), z['rect']['v']) for z in spec['zones']]}")
        check(f"{house}: units {want['units']}, {want['linear']} linear, {want['objects']} objects",
              units == want["units"] and len(spec.get("linear", [])) == want["linear"] and len(spec.get("objects", [])) == want["objects"],
              f"units {units}, linear {len(spec.get('linear', []))}, objects {len(spec.get('objects', []))}")
        calls = spec["generated"].get("pilot_calls", {})
        check(f"{house}: every element call of the pilot house function is mapped or skipped with a reason (none lost silently)",
              bool(calls) and all(v["how"].startswith(("model:", "skip:")) for v in calls.values()), ", ".join(f"{k}: {v['how'][:5]}" for k, v in calls.items()))
        specs[house] = spec

    # adapter features: grid(...), several panels, positional z0, math.* in the pilot text, partial zone rects, planes
    m062 = specs["M062_L2"]
    win = [o for o in m062["openings"] if o["zone"] == "Z2"]
    check("adapter: grid(WIN, [(W0, W1)], 0.12) of M062 -> 3 windows of the 2nd floor in the upper zone", len(win) == 3,
          str([o["rect"]["v"] for o in win]))
    check("adapter: M062 eaves sbox read through math.radians in a tuple assignment (OV, SL, DEP)",
          "sbox:eaves" in m062["generated"]["pilot_calls"], str(list(m062["generated"]["pilot_calls"])))
    m016 = specs["M016_L2"]
    check("adapter: M016 - side plane S joins the front at b (nárožie), corner pilaster with an L profile wraps it",
          m016["reference"].get("joins") == {"b": "S"} and m016["planes"][0]["joins"] == {"a": "F"}
          and any(it["id"] == "K1" and it.get("wrap") == "b" for it in m016["linear"]))
    o6 = next(o for o in m016["openings"] if o["id"] == "O6")
    check("M016 gable: arched door O6 (Arch Double Door 27) with its head in the arch gable A1 (FS_ArchGable)",
          o6.get("head_in") == "A1" and m016["systems"][o6["system"]]["libpart"] == "Arch Double Door 27"
          and m016["systems"][m016["objects"][0]["system"]]["template"] == "FS_ArchGable")

    # validator rules of the zones (negative cases)
    import copy
    bad = copy.deepcopy(specs["M015_L1"])
    bad["zones"][1]["rect"]["v"][3] = 4.0                                     # parter zone overlaps the render zone
    check("validator: overlapping wall zones of one plane are an error", any("overlap" in e and "zones" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(specs["M015_L1"])
    bad["openings"][0].pop("zone")
    check("validator: with several zones an opening must name its zone", any("must name its zone" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(specs["M016_L2"])
    bad["planes"][0]["b"] = [-110.0, 75.0]; bad["planes"][0]["a"] = [-120.0, 64.0]
    check("validator: a plane that does not share the corner point is an error (rule 8)", any("does not meet plane" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(specs["M016_L2"])
    next(it for it in bad["linear"] if it["id"] == "K1").pop("wrap")
    check("validator: a corner profile without wrap / beyond the corner is an error",
          any("corner profile" in e or "outside 0..L" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(specs["M016_L2"])
    bad["objects"][0]["params"]["rIn"]["v"] = 1.1
    check("validator: arched head of O6 must fit the ring of A1 (rIn = half width)", any("half width = rIn" in e for e in validate(bad)[0]))
    return specs


# -- live ---------------------------------------------------------------------------------------------------------
def regression(port: int) -> None:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(TESTS / "test_i4_gdl.py"), "--with-i3", "--port", str(port)], capture_output=True, text=True,
                       timeout=3600, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    summ = [ln.strip() for ln in p.stdout.splitlines() if "checks passed" in ln]
    k7 = [ln.strip() for ln in p.stdout.splitlines() if "K7" in ln]
    check("P7 I4 test passes (incl. --with-i3: I3 -> I2 -> I1)", p.returncode == 0,
          (summ[-1] if summ else p.stdout[-300:]) + (f"; {k7[-1][:140]}" if k7 else "") + f" ({time.monotonic() - t0:.0f} s)")
    if p.returncode != 0:
        print("\n".join(ln for ln in p.stdout.splitlines() if "FAIL" in ln))


def live_house(house: str, spec: dict, port: int) -> None:
    from build import build, existing
    from g1 import write_g1
    from tapir_client import TapirClient
    from verify import verify
    import systems as fs_systems

    want = HOUSES[house]
    spec_path = OUT / house / f"{house}.facade-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    t0 = time.monotonic()
    first = build(spec, PROJECT, port, OUT, verbose=False)
    second = build(spec, PROJECT, port, OUT, verbose=False)
    check(f"{house}: build without read-back mismatches", not first["mismatches"] and not second["mismatches"],
          "; ".join(first["mismatches"] + second["mismatches"])[:300])
    check(f"P5 {house}: second build = 0 changes", second["n_changes"] == 0, f"{second['changes']} in {second['seconds']} s")
    ver = verify(spec, PROJECT, port, OUT, verbose=False)
    check(f"{house}: verify PASS", ver["pass"], "; ".join(ver["problems"])[:400])

    # P1 counts
    check(f"P1 {house}: windows / doors in the zone walls = spec (merged = 1)", ver["count"]["ok"] and ver["count"]["model_in_wall"] == want["units"],
          f"model {ver['count']['model_in_wall']}, spec {ver['count']['spec']}")
    ac = TapirClient(port=port, timeout=120)
    ac.guard_project(PROJECT)
    st = json.loads((OUT / house / "state.json").read_text(encoding="utf-8"))
    layer = st["layer"]["index"]
    on_layer = {}
    for typ in ("Wall", "Window", "Door", "Beam", "Column", "Object"):
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        n = 0
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["layerIndex"]})["detailsOfElements"]
            n = sum(1 for d in dd if d.get("layerIndex") == layer)
        on_layer[typ] = n
    n_lin_beam = sum(1 for it in spec.get("linear", []) if spec["systems"][it["system"]]["tool"] == "beam")
    exp = {"Wall": want["zones"], "Window": want["units"]["window"], "Door": want["units"]["door"], "Beam": n_lin_beam,
           "Column": want["linear"] - n_lin_beam, "Object": want["objects"]}
    check(f"P1 {house}: elements on the layer '{spec['reference']['layer']}' (independent count) = spec", on_layer == exp, f"{on_layer}")

    # P2 deltas
    tol = spec["tolerances"]["build"] * 100
    check(f"P2 {house}: delta of parameters and 3D <= {tol:.0f} cm (contract rows)", max(ver["max_delta_cm"].values()) <= tol + 1e-9,
          f"{ver['max_delta_cm']}; joined corners (own tolerance): " + ", ".join(f"{r['item']} {r['quantity']} {r['delta_cm']} cm" for r in ver["joined_corner_rows"]))

    # P3 photo
    pe = ver["photo_edges"]
    check(f"P3 {house}: every named edge on the photo within tolerance", bool(pe) and all(abs(e["delta_cm"]) <= e["tol_cm"] for e in pe),
          f"{len(pe)} edges, max |delta| {max(abs(e['delta_cm']) for e in pe):.1f} cm")
    reported = set()
    for s in spec.get("simplifications", []):
        reported |= ids_in(s["ref"])
    not_ok = [e for e in ver["auto_edges"] if e["status"] != "OK"]
    unreported = sorted({e["item"] for e in not_ok} - reported)
    check(f"P3 {house}: opening edges (meranie) the automatic check cannot confirm are reported (vykázané) in the spec",
          not unreported, f"{len(ver['auto_edges']) - len(not_ok)}/{len(ver['auto_edges'])} auto edges OK; not OK, reported: "
          + ", ".join(sorted({f"{e['item']}.{e['edge']}" for e in not_ok})) + (f"; NOT reported: {unreported}" if unreported else ""))
    for key in ("overlay_png", "edges_png"):
        pth = Path(ver[key] or "")
        check(f"P3 {house}: {pth.name} written now", pth.exists() and pth.stat().st_mtime > time.time() - 900, str(pth))

    # P4 build-ups read back independently
    attrs = fs_systems.Attrs(ac)
    zw = existing(ac, [st["elements"][z["id"]]["guid"] for z in spec["zones"]])
    bad = []
    for z in spec["zones"]:
        d = zw[st["elements"][z["id"]]["guid"]]["details"]
        comp = spec["systems"][z["system"]]["composite"]
        name = next((n for n, a in attrs.composite.items() if a["attributeId"]["guid"] == (d.get("compositeId") or {}).get("guid")), None)
        diffs = fs_systems.composite_differences(ac, comp, attrs) if name == comp["name"] else [f"composite {name}"]
        if d.get("structureType") != "Composite" or diffs or abs(d["begThickness"] - z["thickness"]["v"]) > 1e-6:
            bad.append(f"{z['id']}: {d.get('structureType')} {name} {diffs} {d['begThickness']}")
    check(f"P4 {house}: every zone wall has the build-up of its system (Composite, skins, thickness)", not bad,
          "; ".join(bad) if bad else ", ".join(f"{z['id']} {spec['systems'][z['system']]['composite']['name']}" for z in spec["zones"]))

    # P6 objects
    if spec.get("objects"):
        obj_rows = [r for r in csv_rows(ver["deviations_csv"]) if r["item"] in {o["id"] for o in spec["objects"]} and r["method"] == "3D"]
        check(f"P6 {house}: GDL objects - 3D extent / arch radii = expected (+-1 cm)", obj_rows and all(r["ok"] == "True" for r in obj_rows),
              f"{len(obj_rows)} rows, max |delta| {max(abs(float(r['delta_cm'])) for r in obj_rows):.2f} cm")
        tr = csv_rows(ver["types_csv"])
        lp = [r for r in tr if r["what"] == "library part (FS template)"]
        check(f"P6 {house}: GDL objects placed from their FS template", lp and all(r["status"] == "OK" for r in lp), ", ".join(f"{r['item']}={r['model']}" for r in lp))

    g1 = write_g1(spec, ver["exceptions"], OUT)
    check(f"{house}: G1 table written", g1.exists(), str(g1))
    print(f"  {house}: {time.monotonic() - t0:.1f} s")


def csv_rows(path: str) -> list[dict]:
    import csv
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--skip-regression", action="store_true")
    args = ap.parse_args()
    specs = offline()
    if not args.offline:
        if not args.skip_regression:
            regression(args.port)
        for house, spec in specs.items():
            live_house(house, spec, args.port)
        if args.save:
            from tapir_client import TapirClient
            ac = TapirClient(port=args.port, timeout=300)
            ac.guard_project(PROJECT)
            check("SaveProject", bool(ac.tapir("SaveProject", {}).get("success")))
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

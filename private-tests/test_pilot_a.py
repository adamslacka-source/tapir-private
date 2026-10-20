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

I5/I6 layer (2026-10-19, ADR-001 section 9 "Pilot A - CW, balkóny, zábradlia"): the houses are built from
build_spec(house, i6=True) - shop windows / glazed entrances as Curtain Walls (FS cw.*, door = CW door panel) in Empty
Window holes, balcony slabs (FS slab.*) and railings (FS rail.*) of M016. The base spec (without i6) stays the contract
of I1..I4 and is still checked offline with the expectations of before.
  P1 also: Empty Window holes of the glazed fields = cw_fields; curtain walls / slabs / railings on the facade layer = spec
  P8 curtain walls: every mullion / transom axis and the boundary read back = spec +-1 cm, frame profiles and panel
     classes per cell as the system (door cell = a door panel with leaves), every 'meranie' axis on the photo +-3 cm,
     the Curtain Wall tool defaults do not keep any FS class (restoreDefaults, independent probe)
  P9 balconies: level, thickness, outline corners, arc and crown of the slab +-1 cm (crown: tessellation tolerance)
  P10 railings: base on the slab top and height +-1 cm, nothing outside the slab, posts + inner posts and balusters
     = expected from the reference line, measured baluster spacing = favourite; baluster spacing of the spec that the
     favourite cannot carry is an EXCEPTION (listed, R1)
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
    # house: (zones, planes, units window/door, linear, objects, zone roles) - base spec (I1..I4 contract)
    "M015_L1": {"zones": 2, "planes": 0, "units": {"window": 3, "door": 3}, "linear": 5, "objects": 3, "roles": {"main", "parter"}},
    "M062_L2": {"zones": 2, "planes": 0, "units": {"window": 6, "door": 2}, "linear": 5, "objects": 0, "roles": {"main", "parter"}},
    "M016_L2": {"zones": 4, "planes": 1, "units": {"window": 10, "door": 3}, "linear": 16, "objects": 1, "roles": {"main", "rizalit", "side"}},
}
I6 = {
    # house: units left as windows / doors, glazed fields (CW) with door panels, balconies, railings
    "M015_L1": {"units": {"window": 3, "door": 1}, "cw": {"O1", "O1b", "O3", "O4"}, "cw_doors": {"O1", "O3"}, "balconies": 0, "railings": 0},
    "M062_L2": {"units": {"window": 5, "door": 1}, "cw": {"O1", "O2", "O3"}, "cw_doors": {"O2"}, "balconies": 0, "railings": 0},
    "M016_L2": {"units": {"window": 8, "door": 2}, "cw": {"O1", "O4", "O7"}, "cw_doors": {"O4"}, "balconies": 2, "railings": 2},
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
    out |= set(re.findall(r"\bO\d+[a-z]?\b", ref))          # O1b = second glazed field of O1 (I5)
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
    # G2 findings 3-4 (2026-10-19): the photo shows straight jambs of the door up to the head 9.09 and a separate lunette
    # in a segmental arch above it (was: one arched door, springing 8.00 from domy.py) -> rectangular door + lunette = infill
    # of the segmental ring A1 (FS_ArchGable 1.1) springing on the door head, threshold 6.78 = top of balcony BK2
    o6 = next(o for o in m016["openings"] if o["id"] == "O6")
    a1 = m016["objects"][0]
    import gdl_expect
    pa1 = {k: v["v"] for k, v in a1["params"].items()}
    z2 = next(z for z in m016["zones"] if z["id"] == "Z2")
    check("M016 gable: rectangular door O6 (Double Door with Transom 27) 6.78-9.09 from the photo, lunette = infill of the "
          "segmental ring A1 (FS_ArchGable 1.1) springing on the door head = top of the rizalit Z2",
          not o6.get("head_in") and m016["systems"][o6["system"]]["libpart"] == "Double Door with Transom 27"
          and o6["rect"]["v"][2:] == [6.78, 9.09] and o6["rect"]["conf"] == "meranie"
          and m016["systems"][a1["system"]]["template"] == "FS_ArchGable" and a1.get("over") == "O6" and pa1["bInfill"] is True
          and abs(a1["origin"]["v"][1] + pa1["vSpring"] - 9.09) < 1e-3 and z2["rect"]["v"][3] == 9.09
          and abs(gdl_expect.arch_chord(pa1) - 1.2) < 0.01,
          f"O6 {o6['rect']['v']} {o6['system']}, A1 centre v {a1['origin']['v'][1]}, vSpring {pa1.get('vSpring')}, rIn {pa1['rIn']}, Z2 top {z2['rect']['v'][3]}")

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
    bad["objects"][0]["params"]["rIn"]["v"] = 1.3
    check("validator: the lunette of A1 must fit the door O6 under it (inner chord half width = half width of O6)",
          any("inner chord half width" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(specs["M016_L2"])
    bad["objects"][0]["origin"]["v"][1] += 0.05
    check("validator: the springing of A1 (centre + vSpring) must be the top of the zone Z2", any("springing" in e and "not on top of zone" in e for e in validate(bad)[0]))
    return offline_i6(specs)


def offline_i6(base_specs: dict) -> dict:
    """I5/I6 layer: specs with curtain walls, balconies, railings (adapter + validator, positive and negative cases)."""
    import copy
    from geom import cw_cell_panels
    specs = {}
    for house, want in I6.items():
        spec = build_spec(house, i6=True)
        errors, warnings = validate(spec)
        check(f"{house} I6: spec with curtain walls / balconies / railings is valid", not errors, "; ".join(errors[:3]))
        units = {"window": 0, "door": 0}
        for o in spec["openings"]:
            if not o.get("merged_into"):
                units[o["kind"]] += 1
        cws = {c["id"]: c for c in spec.get("cw_fields", [])}
        doors = {cid for cid, c in cws.items() if any(spec["systems"][c["system"]]["panels"][k].get("libpart") for k in cw_cell_panels(c).values())}
        replaced = {r for c in cws.values() for r in c.get("replaces", [])}
        base_ids = {o["id"] for o in base_specs[house]["openings"]}
        check(f"{house} I6: openings {want['units']} + glazed fields {sorted(want['cw'])} (door panels {sorted(want['cw_doors'])}); "
              "every replaced opening was an opening of the base spec and is gone",
              units == want["units"] and set(cws) == want["cw"] and doors == want["cw_doors"] and replaced <= base_ids
              and not replaced & {o["id"] for o in spec["openings"]}, f"units {units}, cw {sorted(cws)}, doors {sorted(doors)}, replaces {sorted(replaced)}")
        check(f"{house} I6: {want['balconies']} balconies, {want['railings']} railings",
              len(spec.get("balconies", [])) == want["balconies"] and len(spec.get("railings", [])) == want["railings"])
        for r in spec.get("railings", []):
            check(f"{house} I6: railing {r['id']} - arc as {r['chords']} chords, max {r['approx_error'] * 100:.1f} cm from the arc (<= 2.5 cm), "
                  f"{len(r['path_sw']['v'])} nodes, base = top of {r['on']}",
                  r["approx_error"] <= 0.025 and len(r["path_sw"]["v"]) == r["chords"] + 3
                  and abs(r["v"]["v"] - next(b for b in spec["balconies"] if b["id"] == r["on"])["v_top"]["v"]) < 1e-9)
        calls = spec["generated"].get("pilot_calls", {})
        check(f"{house} I6: every pilot call still mapped or skipped with a reason",
              bool(calls) and all(v["how"].startswith(("model:", "skip:")) for v in calls.values()))
        specs[house] = spec

    # validator rules of the I6 layer (negative cases)
    m15, m16 = specs["M015_L1"], specs["M016_L2"]
    bad = copy.deepcopy(m15)
    next(c for c in bad["cw_fields"] if c["id"] == "O3")["cells"] = [{"col": 0, "row": 1, "panel": "door"}]
    check("validator I6: a door panel above the bottom row of a field is an error", any("bottom row" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m15)
    next(c for c in bad["cw_fields"] if c["id"] == "O4")["rect"]["v"][0] = 6.20
    check("validator I6: a glazed field overlapping another field is an error", any("overlaps glazed field" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m15)
    next(c for c in bad["cw_fields"] if c["id"] == "O4")["v_axes"]["v"] = [3.05]
    check("validator I6: a transom axis closer than 0.15 m to the edge of the field is an error", any("v_axes" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m15)
    next(c for c in bad["cw_fields"] if c["id"] == "O4")["reveal"]["v"] = 0.45
    check("validator I6: a curtain wall deeper than its wall is an error", any("does not fit the wall" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m15)
    bad["openings"].append(dict(copy.deepcopy(next(o for o in base_specs["M015_L1"]["openings"] if o["id"] == "O4")), id="O4x", merged_into=None))
    bad["openings"][-1].pop("merged_into")
    check("validator I6: a glazed field over an opening is an error", any("overlaps opening" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m16)
    bad["balconies"][0]["v_top"]["v"] = 3.40
    check("validator I6: a balcony slab above the sill of its door is an error", any("above the sill" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m16)
    bad["railings"][0]["path_sw"]["v"][4] = [7.27, 1.30]
    check("validator I6: a railing point off its balcony slab is an error", any("outside the slab" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m16)
    bad["railings"][0]["v"]["v"] = 3.0
    check("validator I6: a railing whose base is not the slab top is an error", any("!= top of balcony" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m16)
    bad["systems"]["rail.tycky_m016"]["favorite"] = "rail.tycky"
    check("validator I6: a railing / CW / slab system without an 'FS ' favourite is an error", any("needs an 'FS ' favourite" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(m16)
    bad["balconies"][1]["v_top"]["v"] = 2.60
    check("validator I6: a balcony slab through a linear element (band) is an error", any("passes through linear" in e for e in validate(bad)[0]))
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
    w6 = I6[house]
    want_units = dict(w6["units"], opening=len(w6["cw"]))
    check(f"P1 {house}: windows / doors / Empty Window holes of the glazed fields in the zone walls = spec (merged = 1)",
          ver["count"]["ok"] and ver["count"]["model_in_wall"] == want_units, f"model {ver['count']['model_in_wall']}, spec {ver['count']['spec']}")
    ac = TapirClient(port=port, timeout=120)
    ac.guard_project(PROJECT)
    st = json.loads((OUT / house / "state.json").read_text(encoding="utf-8"))
    layer = st["layer"]["index"]
    on_layer = {}
    for typ in ("Wall", "Window", "Door", "Beam", "Column", "Object", "CurtainWall", "Slab", "Railing"):
        els = ac.tapir("GetElementsByType", {"elementType": typ})["elements"]
        n = 0
        if els:
            dd = ac.tapir("GetDetailsOfElements", {"elements": els, "fields": ["layerIndex"]})["detailsOfElements"]
            n = sum(1 for d in dd if d.get("layerIndex") == layer)
        on_layer[typ] = n
    n_lin_beam = sum(1 for it in spec.get("linear", []) if spec["systems"][it["system"]]["tool"] == "beam")
    exp = {"Wall": want["zones"], "Window": w6["units"]["window"] + len(w6["cw"]), "Door": w6["units"]["door"], "Beam": n_lin_beam,
           "Column": want["linear"] - n_lin_beam, "Object": want["objects"], "CurtainWall": len(w6["cw"]), "Slab": w6["balconies"],
           "Railing": w6["railings"]}
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

    rows = csv_rows(ver["deviations_csv"])
    trs = csv_rows(ver["types_csv"])
    # P8 curtain walls
    for cw in spec.get("cw_fields", []):
        cid = cw["id"]
        n_ax = len(cw.get("u_axes", {}).get("v", [])) + len(cw.get("v_axes", {}).get("v", []))
        ax = [r for r in rows if r["item"] == cid and r["quantity"].startswith(("CW mullion axis", "CW transom axis"))]
        bd = [r for r in rows if r["item"] == cid and r["quantity"].startswith("CW boundary")]
        d3 = [r for r in rows if r["item"] == cid and r["quantity"].startswith(("CW 3D", "CW frames 3D"))]
        check(f"P8 {house} {cid}: raster read back - {n_ax} axes, boundary and 3D (extent, frame plane) = spec +-1 cm",
              len(ax) == n_ax and len(bd) == 4 and len(d3) == 6 and all(r["ok"] == "True" for r in ax + bd + d3),
              f"max |delta| {max([abs(float(r['delta_cm'])) for r in ax + bd + d3] or [0]):.2f} cm")
        ct = [r for r in trs if r["item"] == cid and r["what"].startswith(("CW frame profile", "CW boundary profile", "CW panel class", "CW door panel"))]
        n_door = sum(1 for k in cw_kinds(spec, cw) if spec["systems"][cw["system"]]["panels"][k].get("libpart"))
        check(f"P8 {house} {cid}: frame profiles, panel class per cell, {n_door} door panel(s) with leaves",
              len(ct) == 3 + n_door and all(r["status"] == "OK" for r in ct), "; ".join(f"{r['what'][:30]}={r['model']}" for r in ct if r["status"] != "OK"))
        n_meas = sum(len(cw[k]["v"]) for k in ("u_axes", "v_axes") if k in cw and cw[k]["conf"] in ("meranie", "kota"))
        ph = [e for e in ver["photo_edges"] if e["item"] == cid and e["edge"].startswith("photo: ")]
        check(f"P8 {house} {cid}: every 'meranie' axis ({n_meas}) is on its bar in the photo (+-3 cm)",
              len(ph) == n_meas and all(abs(e["delta_cm"]) <= e["tol_cm"] for e in ph), ", ".join(f"{e['edge'][7:]} {e['delta_cm']:+.1f}" for e in ph))
    # P9 balconies, P10 railings
    for b in spec.get("balconies", []):
        br = [r for r in rows if r["item"] == b["id"]]
        check(f"P9 {house} {b['id']}: slab level, thickness, outline corners, arc, crown read back (+-1 cm; crown: tessellation)",
              len(br) >= 10 and all(r["ok"] == "True" for r in br), f"{len(br)} rows, max |delta| {max([abs(float(r['delta_cm'])) for r in br] or [0]):.2f} cm")
    for r_ in spec.get("railings", []):
        rr = [r for r in rows if r["item"] == r_["id"]]
        rt = [r for r in trs if r["item"] == r_["id"]]
        h = next((r for r in rr if r["quantity"].startswith("3D height")), None)
        check(f"P10 {house} {r_['id']}: height {spec['systems'][r_['system']]['height']['v']} m +-1 cm, base on the slab, nothing outside the slab, "
              "baluster spacing = favourite", h is not None and len(rr) == 4 and all(r["ok"] == "True" for r in rr),
              "; ".join(f"{r['quantity'][:40]} {r['model_m']}" for r in rr))
        cnt = [r for r in rt if r["what"].startswith(("posts + inner posts", "balusters", "infill", "top rail end"))]
        check(f"P10 {house} {r_['id']}: posts + inner posts, balusters (bars) = expected, infill = balusters, rail ends in the wall <= 0.10 m",
              len(cnt) == 4 and all(r["status"] == "OK" for r in cnt), ", ".join(f"{r['what'][:22]} {r['spec']}/{r['model']}" for r in cnt))
        exc = [e for e in ver["exceptions"] if e.get("item") == r_["id"] and e["field"] == "baluster_spacing"]
        sp = spec["systems"][r_["system"]]
        need = abs(sp["baluster_spacing"]["v"] - sp["favorite_baluster_spacing"]) > 0.005
        check(f"P10 {house} {r_['id']}: baluster spacing of the spec the favourite cannot carry is reported as an EXCEPTION (R1)",
              bool(exc) == need, f"spec {sp['baluster_spacing']['v']}, favourite {sp['favorite_baluster_spacing']}, exceptions {len(exc)}")

    # P11 (G2 finding 11): project stories from the spec, every element on the story of its foot, walls top-linked
    from build import Home, plan
    stories = {x["index"]: x["level"] for x in ac.tapir("GetStories")["stories"]}
    want_st = {x["floorIndex"]: round(spec["reference"]["z0"]["v"] + x["level"]["v"], 4) for x in spec["stories"]}
    check(f"P11 {house}: project stories = spec stories (one set for the street, story 0 unchanged = morph model)",
          all(abs(stories.get(i, 99) - lv) < 1e-4 for i, lv in want_st.items()) and max(stories) == max(want_st), f"{want_st} vs {stories}")
    home = Home(spec, stories)
    items = [it for it in sum((plan(spec, stories, fs_systems.resolve(ac, spec, PROJECT, OUT))[k] for k in
                               ("walls", "linear", "objects", "cw", "slabs", "railings")), [])]
    st_items = {k: v for k, v in st["elements"].items()}
    det = existing(ac, [st_items[it["item"]]["guid"] for it in items if it["item"] in st_items and it["kind"] != "railing"])
    bad_floor = [f"{it['item']} {det[st_items[it['item']]['guid']]['floorIndex']}!={it['payload']['floorIndex']}" for it in items
                 if it["kind"] != "railing" and it["item"] in st_items and st_items[it["item"]]["guid"] in det
                 and det[st_items[it["item"]]["guid"]]["floorIndex"] != it["payload"]["floorIndex"]]
    floors = sorted({it["payload"]["floorIndex"] for it in items})
    check(f"P11 {house}: every element on the story of its foot (walls, linear, objects, CW, slabs; railings via verify)", not bad_floor and len(floors) >= 2,
          f"stories used {floors}; " + "; ".join(bad_floor[:5]))
    tl = [r for r in trs if r["what"].startswith("top link")]
    linked = [r for r in tl if r["what"].startswith("top link: relativeTopStory")]
    check(f"P11 {house}: walls top-linked to the story under their top (rule 3), fixed height only on the top story (reported)",
          len([r for r in tl if "relativeTopStory" in r["what"] or "none" in r["what"]]) == want["zones"] and all(r["status"] == "OK" for r in tl)
          and all(r["ok"] == "True" for r in rows if r["quantity"].startswith("top link")) and bool(linked),
          ", ".join(f"{r['item']} {r['model']}" for r in tl))

    g1 = write_g1(spec, ver["exceptions"], OUT)
    check(f"{house}: G1 table written", g1.exists(), str(g1))
    print(f"  {house}: {time.monotonic() - t0:.1f} s")


def cw_kinds(spec: dict, cw: dict) -> list[str]:
    from geom import cw_cell_panels
    return list(cw_cell_panels(cw).values())


def cw_defaults_clean(port: int, specs: dict) -> None:
    """P8: no FS class stays in the Curtain Wall tool defaults - a probe that adds the class names (and restores the
    defaults) must report every one of them as 'created'."""
    from tapir_client import TapirClient
    import systems as fs_systems
    ac = TapirClient(port=port, timeout=120)
    ac.guard_project(PROJECT)
    frames, panels = [], []
    for spec in specs.values():
        for sid, s_ in spec["systems"].items():
            if s_["type"] == "curtain_wall":
                n = fs_systems.cw_class_names(sid, s_)
                frames.append({"name": n["frame"]})
                panels += [{"name": v} for v in n["panels"].values()]
    probe = ac.tapir("SetCurtainWallSystemDefaults", {"frameClasses": frames, "panelClasses": panels, "restoreDefaults": True})
    left = [c.get("name") for c in probe.get("frameClasses", []) + probe.get("panelClasses", []) if c.get("result") != "created"]
    check("P8 Curtain Wall tool defaults restored: no FS class left in them (independent probe)",
          probe.get("defaultsRestored") is True and bool(frames) and not left, f"{len(frames)} frame + {len(panels)} panel classes probed; left: {left}")


def morphs_unmoved(port: int) -> None:
    """P11: the morph model of the street (Morph / Mesh / Roof, story 0) did not move when the project stories were set
    from the spec - against the snapshot taken before the first SetStories (morph_guard.py)."""
    before = OUT / "_stories_check" / "before.json"
    if not before.exists():
        check("P11 morph model unmoved (snapshot before SetStories)", False, f"{before} missing")
        return
    import morph_guard
    r = morph_guard.compare(json.loads(before.read_text(encoding="utf-8")), morph_guard.snapshot(port))
    check("P11 morph model unmoved by the stories: Morph / Mesh / Roof 3D boxes max delta 0.0 cm, none missing, all on story 0",
          all(v["missing"] == 0 and v["max_delta_cm"] == 0.0 and v["before"] == v["after"] and v["floors_after"] == [0] for v in r.values()),
          "; ".join(f"{k}: {v['compared']}/{v['before']} max {v['max_delta_cm']} cm, floors {v['floors_after']}" for k, v in r.items()))


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
        cw_defaults_clean(args.port, specs)
        morphs_unmoved(args.port)
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

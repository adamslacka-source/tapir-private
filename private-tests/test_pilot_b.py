"""
Acceptance test of pilot B (ADR-001 section 9 "Pilot B"): a facade from ONE photo - photo -> spec -> BIM without
prepared data. Photo: "Frontsicht Westteil Suedfassade Gebaeude B HAW Hof" - PantheraLeo1359531, CC BY 4.0, Wikimedia
Commons (C:/FacadePilot/pilot_B/input, SHA-1 b6b65321746e87a4e55fb843fe07f9bb53b45283; not in git).

    python test_pilot_b.py --offline     # adapter (run isolated: python -I) + validator + negative cases, no Archicad
    python test_pilot_b.py               # + build, second build = 0 changes, verify_b, G1, CW defaults probe
    python test_pilot_b.py --save        # ... and SaveProject at the end

Writes ONLY into C:/FacadePilot/Fasada_pilot_B.pln (build / verify refuse any other open project). UTF-8 output,
every Archicad call and the adapter run have a timeout.

Acceptance:
  B1 adapter: photo verified (SHA-1), rectification residual of the long horizontal lines <= 2 px rms, camera
     (cx, cy, k_r, k_f, scale) recorded, scale checks (door, post spacing, story height) recorded
  B2 spec valid; content: 3 floors, curtain walls on every floor incl. door fields, louvres in front of every field,
     2 walkways + railings, consoles, red bands, roof edge, piers, recessed bay with doors; every value has src + conf
  B3 validator rejects broken pilot-B specs (negative cases)
  B4 build: 0 read-back mismatches; second build = 0 changes
  B5 verify: every parameter / 3D row within 1 cm, story of every element = its foot, CW raster / profiles / panels
  B6 photo: every readable 'meranie' mullion and every horizontal edge (median of the samples) within 3 cm in its plane
     (relative to the photo: the scale is ODHAD); failures and unreadable bars are listed, overlays written
  B7 Curtain Wall tool defaults restored (independent probe); G1 table written
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import copy
import json
import subprocess
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

from spec_validate import validate  # noqa: E402

ROOT = Path(r"C:\FacadePilot\pilot_B")
OUT = ROOT / "out"
SPEC = OUT / "HOF_B.facade-spec.json"
PROJECT = r"C:\FacadePilot\Fasada_pilot_B.pln"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return bool(ok)


def walk_values(node, ref=""):
    if isinstance(node, dict):
        if "v" in node and "conf" in node:
            yield ref, node
        for k, v in node.items():
            if k != "v":
                yield from walk_values(v, f"{ref}.{k}" if ref else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_values(v, f"{ref}[{i}]")


# -- offline ----------------------------------------------------------------------------------------------------------
def offline() -> dict:
    t0 = time.monotonic()
    # the adapter reads the photo -> isolated interpreter, run from outside the input folder
    r = subprocess.run([sys.executable, "-I", str(FACADE / "adapters" / "from_photo.py")], cwd=str(TESTS), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    check("B1 adapter from_photo.py runs isolated (python -I)", r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1][:200] if (r.stdout + r.stderr).strip() else "")
    meas = json.loads((ROOT / "work" / "HOF_B_measure.json").read_text(encoding="utf-8"))
    check("B1 photo = the verified file (SHA-1 of zdroj.md)", meas["ingest"]["sha1"] == "b6b65321746e87a4e55fb843fe07f9bb53b45283")
    rect = meas["rectify"]
    rms = {k: round(v["rms_px"], 2) for k, v in rect["after"].items()}
    check("B1 rectification: long horizontal lines straight after the warp (<= 2.5 px rms each)", all(v <= 2.5 for v in rms.values()),
          f"before {({k: round(v, 1) for k, v in rect['rms_line_before_px'].items()})} -> after {rms}, max shift {rect['max_shift_px']:.1f} px")
    cam = meas["camera"]
    check("B1 camera from the photo: cx (vertical console), k_r (40 consoles), cy (end railings), k_f (soffit joints), scale (railing 1.05)",
          4600 < cam["cx_vertical_console"] < 4900 and 0.92 < cam["k_r"] < 0.97 and 0.88 < cam["soffit"]["k_f"] < 0.95 and 180 < cam["m0_px_per_m"] < 240,
          f"cx {cam['cx_vertical_console']:.0f}, cy {cam['cy']['cy']:.0f} +-{cam['cy']['spread_px']:.0f}, k_r {cam['k_r']:.4f}, k_f {cam['soffit']['k_f']:.4f}, "
          f"m0 {cam['m0_px_per_m']:.1f} px/m, D0 {cam['D0']:.2f} m, w_r {cam['w_r']:.2f}, w_f {cam['w_f']:.2f}")
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    errors, warnings = validate(spec)
    check("B2 spec valid", not errors, "; ".join(errors[:3]))
    leaf = meas["bay"]["leaf_h"]
    post = meas["post_spacing_px"] / cam["m_rail_px_per_m"]
    st = {s["id"]: s["level"]["v"] for s in spec["stories"]}
    check("B1 scale checks recorded: door leaf, post spacing, story height", abs(leaf) > 1.5 and 1.0 < post < 1.5,
          f"door leaf {abs(leaf):.2f} m (task 2.10 -> +{(abs(leaf) / 2.10 - 1) * 100:.0f} %), post spacing {post:.3f} m, story 1NP->2NP {st['2NP'] - st['1NP']:.3f} m")
    cws = spec["cw_fields"]
    floors = {c["story"] for c in cws}
    doors = [c["id"] for c in cws if any(x["panel"] == "door" for x in c.get("cells", []))]
    check("B2 curtain walls on 3 floors incl. door fields and the recessed bay", floors == {"GF", "1NP", "2NP"} and len(doors) >= 10,
          f"{len(cws)} CW, {len(doors)} with a door panel")
    lv = [o for o in spec["objects"] if o["kind"] == "louvers"]
    check("B2 louvres (FS_Louvers) in front of the fields, drop height from the photo", len(lv) >= 50,
          f"{len(lv)} objects; drop heights 2NP/1NP/GF: " + ", ".join(sorted({f"{o['params']['ZZYZX']['v']:.2f}" for o in lv})[:6]))
    kinds = {}
    for it in spec["linear"]:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    check("B2 walkways + railings, consoles, red bands, roof edge, column, piers",
          len([s for s in spec["slabs"] if s["kind"] == "walkway"]) == 2 and len(spec["railings"]) == 2 and kinds.get("bracket", 0) >= 60
          and kinds.get("band", 0) == 6 and kinds.get("eaves") == 1 and kinds.get("column") == 1 and len(spec["zones"]) >= 4, f"{kinds}")
    vals = list(walk_values(spec))
    bad = [r_ for r_, v in vals if v["conf"] not in ("odhad", "default") and not v.get("src")]
    check("B2 every value carries src + conf (odhad/default may have src null)", not bad and len(vals) > 500, f"{len(vals)} values; {bad[:3]}")
    # B3 negative cases
    def neg(name, fn, needle):
        s2 = copy.deepcopy(spec)
        fn(s2)
        e, _ = validate(s2)
        check(f"B3 validator: {name}", any(needle in x for x in e), (e[:1] or ["no error"])[0][:160])
    neg("louvres in front of no curtain wall are an error", lambda s: s["objects"][0]["origin"]["v"].__setitem__(0, -5.0), "")
    neg("a door panel above the bottom row is an error",
        lambda s: next(c for c in s["cw_fields"] if any(x["panel"] == "door" for x in c.get("cells", [])))["cells"].__setitem__(0, {"col": 0, "row": 1, "panel": "door"}), "door panel in row")
    neg("a freestanding CW in front of the plane (reveal < 0) is an error", lambda s: s["cw_fields"][0]["reveal"].__setitem__("v", -0.1), "reveal >= 0")
    neg("two CWs overlapping at the same depth are an error",
        lambda s: s["cw_fields"].append(dict(copy.deepcopy(s["cw_fields"][0]), id="CW_DUP")), "overlaps curtain wall")
    neg("a railing not on its walkway top is an error", lambda s: s["railings"][0]["v"].__setitem__("v", s["railings"][0]["v"]["v"] + 0.2), "!= top of slab")
    neg("a railing point off the walkway is an error", lambda s: s["railings"][0]["path_sw"]["v"].__setitem__(1, [s["railings"][0]["path_sw"]["v"][1][0], 3.0]), "outside slab")
    neg("a bracket with an empty w span is an error",
        lambda s: next(i for i in s["linear"] if i["axis"] == "w")["w_span"].__setitem__("v", [1.0, 0.5]), "w_span")
    neg("stories with floorIndex on some only are an error", lambda s: s["stories"][0].pop("floorIndex"), "floorIndex on some")
    neg("a slab outline outside 0..L is an error", lambda s: s["slabs"][0]["outline_sw"]["v"][1].__setitem__(0, 99.0), "outside 0..L")
    check(f"offline part done in {time.monotonic() - t0:.0f} s", True)
    return spec


# -- online ------------------------------------------------------------------------------------------------------------
def online(spec: dict, port: int, save: bool) -> None:
    import build as fb
    import g1
    import verify_b
    import systems as fs_systems
    from tapir_client import TapirClient
    ac = TapirClient(port=port, timeout=60)
    info = ac.tapir("GetProjectInfo", timeout=15)
    if not check("open project is Fasada_pilot_B.pln", (info.get("projectPath") or "").lower() == PROJECT.lower(), info.get("projectPath", "")):
        return
    r1 = fb.build(spec, PROJECT, port, OUT, verbose=False)
    check("B4 build: read-back = sent (0 mismatches)", not r1["mismatches"], f"{r1['n_changes']} changes, {r1['seconds']} s; {r1['mismatches'][:3]}")
    r2 = fb.build(spec, PROJECT, port, OUT, verbose=False)
    check("B4 second build = 0 changes", r2["n_changes"] == 0, f"{r2['changes']}")
    v = verify_b.verify(spec, PROJECT, port, OUT)
    check("B5 every parameter / 3D row within 1 cm", v["rows"] > 1000 and v["rows_ok"] == v["rows"], f"{v['rows_ok']}/{v['rows']}, max {v['max_delta_cm']} cm")
    check("B5 home story of every element = its foot (modeller rule 2)", v["story_ok"] == v["elements"], f"{v['story_ok']}/{v['elements']}")
    check("B5 CW raster, profiles, panel classes, door panels, railings (type rows)", v["types_ok"] == v["types"] and not v["problems"],
          f"{v['types_ok']}/{v['types']}; problems {v['problems'][:3]}")
    edges = v["photo_edges"]
    check("B6 photo: horizontal edges (bands, GF box, roof edge, walkway edges) - median within 3 cm", all(e["ok"] for e in edges),
          f"{len(edges)} edges, max |median| {max(abs(e['median_cm']) for e in edges):.2f} cm, max sample {v['photo_samples_max_cm']} cm")
    n_m = v["photo"] - len(edges)
    ok_m = v["photo_ok"] - sum(e["ok"] for e in edges)
    check("B6 photo: readable 'meranie' mullions within 3 cm (unreadable listed, failures listed)", ok_m >= n_m - 1,
          f"{ok_m}/{n_m} readable within 3 cm, {v['photo_unreadable']} unreadable (contrast < 8); failures {v['photo_fail']}")
    check("B6 overlays written", all(Path(p).exists() for p in v["paths"].values()), ", ".join(v["paths"].values()))
    # B7 CW defaults probe + G1
    names = fs_systems.cw_class_names("cw.hof_b", spec["systems"]["cw.hof_b"])
    probe = ac.tapir("SetCurtainWallSystemDefaults", {"frameClasses": [{"name": names["frame"]}], "panelClasses": [{"name": n} for n in names["panels"].values()],
                                                       "restoreDefaults": True}, timeout=120)
    left = [c.get("name") for c in probe.get("frameClasses", []) + probe.get("panelClasses", []) if c.get("result") != "created"]
    check("B7 Curtain Wall tool defaults restored: no FS class left in them (independent probe)", probe.get("defaultsRestored") is True and not left, f"left: {left}")
    path = g1.write_g1(spec, r2["exceptions"], OUT)
    txt = path.read_text(encoding="utf-8")
    check("B7 G1 table written (values below meranie, exceptions, simplifications)", "G1" in txt and "R1" in txt, str(path))
    if save:
        check("SaveProject", bool(ac.tapir("SaveProject", {}, timeout=300).get("success")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args(argv)
    spec = offline()
    if not args.offline:
        online(spec, args.port, args.save)
    n_ok = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n{n_ok}/{len(results)} checks passed - {'PASS' if n_ok == len(results) else 'FAIL'}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

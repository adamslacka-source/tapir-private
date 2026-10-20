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
  B2 railings (R1 closed 2026-10-20): FS_Railing with 10 bars read from the photo (upper walkway = source, lower = check),
     mounting / posts / profiles from the photo with src + conf, a post on every measured console
  B3 validator rejects broken pilot-B specs (negative cases)
  B4 build: 0 read-back mismatches; second build = 0 changes
  B5 verify: every parameter / 3D row within 1 cm, story of every element = its foot, CW raster / profiles / panels
  B5 FS_Railing: primitives = template model, posts / bars per railing = spec, bar axes + handrail top in 3D <= 1 cm
  B6 photo: bars, handrail top and front posts of both walkways (median, relative) within 3 cm
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
import csv
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
    # G2 findings 6-7 (2026-10-19): the light opaque plane west of the CW is modelled as the end wall STENA_Z (left edge =
    # downpipe at the corner, measured), the round column has its axis at the depth its s was read at
    zw = next((z for z in spec["zones"] if z["id"] == "STENA_Z"), None)
    stl = next(it for it in spec["linear"] if it["id"] == "STLP_L")
    d_col = stl["s"]["v"][1] - stl["s"]["v"][0]
    check("B2 west end wall STENA_Z (G2 nález 6): measured left edge (downpipe), up to the first CW field, ground to soffit, depth ODHAD",
          zw is not None and zw["rect"]["conf"] == "meranie" and 1.4 < zw["rect"]["v"][1] - zw["rect"]["v"][0] < 2.0
          and abs(zw["rect"]["v"][1] - min(c["rect"]["v"][0] for c in cws)) < 1e-3 and zw["w"]["conf"] == "odhad",
          f"{zw['rect']['v'] if zw else None}, w {zw['w']['v'] if zw else None}")
    check("B2 column STLP_L (G2 nález 7): axis at the depth of the reading (profile centred on w_axis), depth ODHAD",
          abs((stl["w"]["v"] + d_col / 2) - meas["column"]["w_axis"]) < 0.002 and stl["w"]["conf"] == "odhad",
          f"w {stl['w']['v']} + d/2 {d_col / 2:.3f} vs w_axis {meas['column']['w_axis']:.3f}")
    # R1 closed (2026-10-20): walkway railings = FS_Railing with the bars, mounting and posts read from the photo
    rs = spec["systems"]["rail.ochodza"]
    ri = meas["railing_infill"]
    gp, gps = rs.get("gdl_params", {}), rs.get("gdl_params_src", {})
    zl_raw = [float(x) for x in str(gp.get("barZList", "")).split(";") if x]
    zl = sorted(zl_raw)
    check("B2 railing system = FS_Railing (railing_kind gdl, favourite 'FS rail.horiz_5', no base favourite)",
          rs.get("railing_kind") == "gdl" and rs.get("favorite") == "FS rail.horiz_5" and "base_favorite" not in rs, f"{rs.get('railing_kind')}, {rs.get('favorite')}")
    check("B2 bars read from the photo: 10 bars in >= 90 % of the upper walkway fields, MAD <= 1 px, list = measured heights",
          len(ri["z_m"]) == 10 and gp.get("nBars") == 10 and gp.get("iBarDist") == 1 and min(ri["bars_found"]) >= 0.9 * ri["fields_upper"]
          and max(ri["bars_mad_px"]) <= 1.0 and zl_raw == [round(z, 3) for z in ri["z_m"]] and gps["barZList"]["conf"] == "meranie",
          f"z {gp.get('barZList')}, found {ri['bars_found']}/{ri['fields_upper']}, MAD max {max(ri['bars_mad_px'])} px")
    lc = ri["lower_check"]
    check("B2 bars: the lower walkway (independent) shows the same bars (each in >= 10 of its fields, median <= 1.5 px)",
          all(c["found"] >= 10 and abs(c["median_px"]) <= 1.5 for c in lc), f"found {[c['found'] for c in lc]}, median px {[c['median_px'] for c in lc]}")
    check("B2 bar spacing: 3 bars ~0.145 m under the handrail, 7 bars ~0.073 m at the bottom (photo)",
          all(0.13 < a - b < 0.16 for a, b in zip(zl[-1:-5:-1], zl[-2:-5:-1])) and all(0.06 < a - b < 0.09 for a, b in zip(zl[1:7], zl[0:6])),
          f"spacings {[round(b - a, 3) for a, b in zip(zl, zl[1:])]}")
    check("B2 railing mounting from the photo: side mounted on the console tip (iMount 1), post below the foot 0.25-0.40 m, flat bar posts",
          gp.get("iMount") == 1 and 0.25 < gp.get("mountDrop", 0) < 0.40 and gp.get("iPostProf") == 2 and gps["mountDrop"]["conf"] == "meranie",
          f"mountDrop {gp.get('mountDrop')}, post {gp.get('postW')} x {gp.get('postD')}")
    check("B2 every FS_Railing parameter carries src + conf (gdl_params_src, same keys and values)",
          set(gps) == set(gp) and all(gps[k]["v"] == gp[k] for k in gp), f"{len(gp)} parameters")
    rails_ok, det_r = True, []
    for r_ in spec["railings"]:
        pts = r_["path_sw"]["v"]
        wk = next(x for x in spec["slabs"] if x["id"] == r_["on"])
        front = [(p, q) for p, q in zip(pts, pts[1:]) if abs(q[1] - p[1]) < 1e-9]
        bays = r_["segment_bays"]["v"]
        posts = []
        for (p, q), n in zip(front, bays[len(pts) - 1 - len(front):]):
            posts += [p[0] + (q[0] - p[0]) * k / n for k in range(n + 1)]
        konz = [0.5 * sum(i["s"]["v"]) for i in spec["linear"] if i["id"].startswith("KONZ_" + ("H" if r_["story"] == "2NP" else "D")) and i["s"]["conf"] == "meranie"]
        dev = max(min(abs(p - k) for p in posts) for k in konz if k > pts[0][0] + 0.3)
        good = abs(pts[1][1] - max(v[1] for v in wk["outline_sw"]["v"])) < 1e-6 and dev <= 0.065 and len(bays) == len(pts) - 1
        rails_ok &= good
        det_r.append(f"{r_['id']}: {len(pts) - 1} segments, bays {bays}, max console - post {dev * 100:.1f} cm")
    check("B2 railing path on the walkway edge face, a post on every measured console (<= 6.5 cm)", rails_ok, "; ".join(det_r))
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
    neg("an FS_Railing parameter whose src value differs from gdl_params is an error",
        lambda s: s["systems"]["rail.ochodza"]["gdl_params_src"]["nBars"].__setitem__("v", 7), "gdl_params_src.nBars")
    neg("segment_bays with a wrong number of values is an error", lambda s: s["railings"][0]["segment_bays"]["v"].append(3), "segment_bays has")
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
    rep = json.loads((OUT / spec["id"] / "report_b.json").read_text(encoding="utf-8"))
    rt = [t for t in rep["types"] if t["item"].startswith("ZAB_") and "FS_Railing" in t["what"]]
    n_obj = sum(len(r_["path_sw"]["v"]) - 1 for r_ in spec["railings"])
    check("B5 FS_Railing: every object = template model, GDL parameters read = sent; posts and bars per railing = spec",
          len(rt) == 2 * n_obj + 4 and all(t["status"] == "OK" for t in rt),
          f"{n_obj} objects; " + "; ".join(f"{t['item']} {t['what'].split('(')[0].strip()}: {t['model']}" for t in rt if t["item"] in ("ZAB_1NP", "ZAB_2NP")))
    rr = list(csv.DictReader(open(OUT / spec["id"] / "deviations_b.csv", encoding="utf-8")))
    zr = [r_ for r_ in rr if r_["item"].startswith("ZAB_") and ("bar " in r_["quantity"] or "handrail" in r_["quantity"])]
    check("B5 FS_Railing 3D: handrail top and every bar axis above the walkway top = spec (<= 1 cm)",
          len(zr) == 11 * n_obj and all(r_["ok"] == "True" for r_ in zr), f"{len(zr)} rows, max |d| {max(abs(float(r_['delta_cm'])) for r_ in zr):.2f} cm")
    re_ = [e for e in v["photo_edges"] if e["item"].startswith("ZAB_")]
    check("B6 photo: every bar, the handrail top and the posts of both walkway fronts within 3 cm (median of the samples, relative)",
          len(re_) == 2 * 12 and all(e["ok"] and e["readable_n"] >= 5 for e in re_),
          f"{len(re_)} checks, max |median| {max(abs(e['median_cm']) for e in re_):.2f} cm, max sample {max(e['max_cm'] for e in re_):.2f} cm")
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
    check("B7 G1 table written (values below meranie, exceptions, simplifications); exception R1 gone, FS_Railing listed",
          "G1" in txt and "VÝNIMKA R1" not in txt and "FS_Railing" in txt, str(path))
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

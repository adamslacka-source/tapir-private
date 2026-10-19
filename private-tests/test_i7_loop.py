"""
Acceptance test of I7 (ADR-001 section 9): the automatic loop build -> verify -> classify -> fix -> build / verify
(facade/iterate.py) on both pilots, live in Archicad 29.

    python test_i7_loop.py --offline            # pure parts: systemic check, corrections file, rigid shift on the photo
    python test_i7_loop.py                      # + live: pilot A real loop (run 1, run 2 = 0 fixes), artificial spec
                                                #   error, artificial build error, pilot B real loop (run 1, run 2), switch back
    python test_i7_loop.py --regression         # ... and test_pilot_a.py / test_pilot_b.py after the loop
    python test_i7_loop.py --skip-b             # pilot A only

Writes only into C:/FacadePilot/Fasada_pilot_A.pln and Fasada_pilot_B.pln (every build / verify guards the open
project); switches A -> B -> A with SaveProject + OpenProject (GetProjectInfo before every write). Artificial errors use
a temporary corrections file (C:/FacadePilot/out/_i7_test) - the corrections of the repo are not touched by them, and the
model is rebuilt from the repo spec afterwards.

Acceptance (task I7):
  K1 artificial spec error: M015 window O6 moved 0.20 m to the right in the types (its sill and surround follow) ->
     verify finds it, class 'spec' (model = spec, the photo edge elsewhere), the loop re-measures the opening on the photo
     (rigid shift of the pair of jambs) and corrects the spec; the next iteration is within tolerance; the corrected
     spec is within +-2 cm of the original value; afterwards the model is rebuilt from the repo spec
  K2 artificial build error: one curtain wall of the shop window of M062 moved 0.10 m along the facade through the API
     (MoveElements, outside the spec) -> class 'build', the loop rebuilds it from the spec, verify passes, the spec is
     not changed
  K3 real deviations: pilot A (M015 parter O1-O4, M062 shop window O1-O3 and every other edge over tolerance) and
     pilot B (CW_1NP_15 bar 5.1 cm, double bar): every deviation gets one class with evidence (crop PNG); corrected
     ones are within tolerance in the next verify; ambiguous ones have a crop
  K4 second run of the loop = 0 fixes (both pilots); with --regression test_pilot_a.py and test_pilot_b.py pass
  K5 report_G2.md per pilot written (fixes, what remains with crops, numbers before / after)
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import copy
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

import iterate as I  # noqa: E402
import spec_corrections as sc  # noqa: E402

OUT = Path(r"C:\FacadePilot\out")
TMP = OUT / "_i7_test"
PROJECT_A = I.PROJECT_A
PROJECT_B = I.PROJECT_B
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return bool(ok)


# -- offline ------------------------------------------------------------------------------------------------------------
def offline() -> None:
    rng = np.random.default_rng(7)
    same = [{"host": f"H{i}", "axis": "s", "delta": 0.04 + rng.normal(0, 0.004)} for i in range(8)]
    r = I.systemic_check(same, 0.03)["s"]
    check("O1 systemic: 8 hosts all +4 cm -> triggered, offset accepted (error drops, nothing leaves the tolerance)",
          r["triggered"] and r["accepted"] and abs(r["offset_m"] - 0.04) < 0.005, json.dumps(r))
    mixed = [{"host": f"H{i}", "axis": "s", "delta": (-1) ** i * 0.02} for i in range(8)]
    r = I.systemic_check(mixed, 0.03)["s"]
    check("O1 systemic: mixed signs -> not triggered", not r["triggered"], json.dumps(r))
    # corrections file: apply, stale, id selector
    root = {"openings": {"O6": {"rect": {"v": [1.0, 2.0, 3.0, 4.0]}}}, "i6": {"cw_fields": [{"id": "A", "rect": {"v": [0, 1, 0, 1]}}]}}
    data = {"schema": sc.SCHEMA, "corrections": [
        {"id": "c1", "target": "types", "path": ["openings", "O6", "rect"], "before": {"v": [1.0, 2.0, 3.0, 4.0]}, "after": {"v": [1.1, 2.1, 3.0, 4.0]}},
        {"id": "c2", "target": "types", "path": ["i6", "cw_fields", {"id": "A"}, "rect", "v", 3], "before": 1, "after": 1.05},
        {"id": "c3", "target": "types", "path": ["openings", "O6", "rect"], "before": {"v": [9, 9, 9, 9]}, "after": {"v": [0, 0, 0, 0]}},
        {"id": "c4", "target": "types", "path": ["openings", "O7"], "before": None, "after": {"x": 1}, "status": "reverted"}]}
    a, s = sc.apply(root, data, "types")
    check("O2 corrections: applied in order, id selector, stale entry skipped (never silently), reverted entry ignored",
          a == ["c1", "c2"] and s == ["c3"] and root["openings"]["O6"]["rect"]["v"] == [1.1, 2.1, 3.0, 4.0]
          and root["i6"]["cw_fields"][0]["rect"]["v"][3] == 1.05 and "O7" not in root["openings"], f"applied {a}, stale {s}")
    # rigid shift on the real photo of M015: O6 jambs predicted 0.20 m to the right -> d = -0.20
    from from_senec import build_spec
    from photo_measure import Photo
    spec = build_spec("M015_L1", i6=True)
    rr = next(x for x in spec["sources"] if x.get("rect"))["rect"]
    ph = Photo(Path(spec["generated"]["root"]) / rr["image"], rr["S"], rr["u0"], rr["v1"])
    o6 = next(o for o in spec["openings"] if o["id"] == "O6")
    s0, s1 = o6["rect"]["v"][:2]
    pe = {e["edge"]: e for e in o6["photo_edges"]}
    res = I.rigid_shift(ph, [{"edge": "left", "at": s0 + 0.2, "rng": pe["left"]["range"], "brighter": "left"},
                             {"edge": "right", "at": s1 + 0.2, "rng": pe["right"]["range"], "brighter": "right"}], I.RIGID_HALF)
    check("O3 rigid shift on the photo: O6 jambs predicted +0.20 m -> found d = -0.20 m (+-1 cm), unique", res["status"] == "certain" and abs(res["d"] + 0.2) <= 0.01,
          json.dumps(res))


# -- live helpers -------------------------------------------------------------------------------------------------------
def client(port: int):
    from tapir_client import TapirClient
    return TapirClient(port=port, timeout=120)


def switch(port: int, target: str) -> bool:
    """SaveProject of the open project, OpenProject target, wait until GetProjectInfo says so (max 5 min)."""
    ac = client(port)
    cur = ac.project_path()
    if cur.lower() == target.lower():
        return True
    if cur.lower() not in (PROJECT_A.lower(), PROJECT_B.lower()):
        check(f"open project is a pilot before switching ({cur})", False)
        return False
    ac.tapir("SaveProject", {}, timeout=300)
    ac.tapir("OpenProject", {"projectFilePath": target}, timeout=600)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300:
        try:
            if client(port).project_path().lower() == target.lower():
                return check(f"switched to {Path(target).name} (SaveProject + OpenProject)", True, f"{time.monotonic() - t0:.0f} s")
        except Exception:                                      # noqa: BLE001 - Archicad busy while opening
            pass
        time.sleep(3)
    return check(f"switched to {Path(target).name}", False, "timeout 300 s")


def hist(i7_dir: Path) -> list[dict]:
    return json.loads((i7_dir / "iterations.json").read_text(encoding="utf-8"))["runs"]


# -- K3 / K4 pilot A real -----------------------------------------------------------------------------------------------
def real_a(port: int) -> None:
    logs1 = I.run_a(["M015_L1", "M062_L2", "M016_L2"], port, I.MAX_ITER)
    logs2 = I.run_a(["M015_L1", "M062_L2", "M016_L2"], port, I.MAX_ITER)
    for lg1, lg2 in zip(logs1, logs2):
        sid = lg1["spec"]
        f1 = lg1["first_findings"]
        classes = {c: sum(f["cls"] == c for f in f1) for c in ("build", "spec", "ambiguous", "systemic")}
        check(f"K3 {sid}: every deviation over tolerance has one class with evidence", all(f["cls"] in ("build", "spec", "ambiguous") and f.get("reason") for f in f1),
              f"{len(f1)} findings {classes}")
        photo = [f for f in f1 + lg2["final_findings"] if f["source"] in ("edge", "auto", "bar")]
        check(f"K3 {sid}: every photo deviation has a crop PNG", all(f.get("crop") and Path(f["crop"]).exists() for f in photo), f"{len(photo)} crops")
        check(f"K3 {sid}: verify PASS after the loop", lg1["verify_pass"] and lg2["verify_pass"])
        check(f"K4 {sid}: second run of the loop = 0 fixes, nothing left in class spec / build", lg2["fixes_total"] == 0
              and not [f for f in lg2["final_findings"] if f["cls"] in ("spec", "build")], f"run 1: {lg1['fixes_total']} fixes; run 2: {lg2['fixes_total']}")
        corr = sc.load(sc.path_of(sid) if sc.path_of(sid).exists() else None, sid)
        act = [c for c in corr["corrections"] if c.get("status", "active") == "active"]
        bad = [c["id"] for c in act if not (c.get("crop") and Path(c["crop"]).exists() and c.get("evidence") and c.get("kept") is True
                                            and c.get("error_after_cm") is not None and c["error_after_cm"] <= 3.0)]
        check(f"K3 {sid}: every correction of the spec has evidence + crop, was evaluated and lowered the error into tolerance",
              not bad, f"{len(act)} active: " + ", ".join(f"{c['id']} {'.'.join(str(x['id']) if isinstance(x, dict) else str(x) for x in c['path'])} "
                                                             f"{c.get('error_before_cm')}->{c.get('error_after_cm')} cm" for c in act) + (f"; bad {bad}" if bad else ""))
    # the named parter openings: M015 O1..O4 (O1/O3/O4 are glazed fields), M062 shop window O1..O3
    named = {"M015_L1": {"O1", "O2", "O3", "O4"}, "M062_L2": {"O1", "O2", "O3"}}
    for lg in logs1:
        if lg["spec"] not in named:
            continue
        runs = hist(OUT / lg["spec"] / "i7")
        first_all = runs[0]["first_findings"]
        sel = [f for f in first_all if f["item"] in named[lg["spec"]]]
        corr = sc.load(sc.path_of(lg["spec"]) if sc.path_of(lg["spec"]).exists() else None, lg["spec"])
        fixed = {c["finding"]["item"] for c in corr["corrections"] if c.get("status", "active") == "active" and c.get("finding")}
        ok = all(f["cls"] == "ambiguous" and f.get("crop") or f["cls"] == "spec" and f["item"] in fixed for f in sel)
        check(f"K3 {lg['spec']} parter {sorted(named[lg['spec']])}: each edge read 'on the frame' is corrected from the photo (certain) "
              "or ambiguous with a crop", bool(sel) and ok,
              "; ".join(f"{f['item']} {f.get('edge')} {f.get('delta_cm'):+.1f} cm -> {f['cls']}" for f in sel))
    p = OUT / "report_G2.md"
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    check("K5 report_G2.md of pilot A written (fixes, what remains, numbers before / after per house)",
          all(h in txt for h in ("M015_L1", "M062_L2", "M016_L2")) and "Čo ostáva" in txt and "pred (1. beh" in txt, str(p))


# -- K1 artificial spec error -------------------------------------------------------------------------------------------
def artificial_spec(port: int) -> None:
    house = "M015_L1"
    TMP.mkdir(parents=True, exist_ok=True)
    tmp_corr = TMP / f"{house}.spec_error.json"
    real = sc.path_of(house)
    if real.exists():
        shutil.copy(real, tmp_corr)
    elif tmp_corr.exists():
        tmp_corr.unlink()
    log_dir = OUT / house / "i7_test_spec"
    shutil.rmtree(log_dir, ignore_errors=True)
    from from_senec import TYPES_DIR
    orig = json.loads((TYPES_DIR / f"{house}.json").read_text(encoding="utf-8"))["openings"]["O6"]["rect"]["v"]

    def inject(types):
        types["openings"]["O6"]["rect"]["v"][0] = round(types["openings"]["O6"]["rect"]["v"][0] + 0.20, 4)
        types["openings"]["O6"]["rect"]["v"][1] = round(types["openings"]["O6"]["rect"]["v"][1] + 0.20, 4)

    n_before = len(sc.load(tmp_corr if tmp_corr.exists() else None, house)["corrections"])
    lg = I.Loop(I.PilotA(house, port, corr_path=tmp_corr, types_hook=inject), I.MAX_ITER, log_dir=log_dir).run()
    it1 = lg["iterations"][0]
    o6 = [f for f in it1["findings"] if f["host"] == "O6" or f["item"] in ("O6", "O6_sur", "O6_sill")]
    spec_cls = [f for f in o6 if f["cls"] == "spec"]
    check("K1 verify finds the moved window O6 (deviations of O6 / its surround over tolerance)", bool(o6),
          "; ".join(f"{f['item']} {f.get('edge')} {f.get('delta_cm')} cm {f['source']}" for f in o6))
    check("K1 class 'spec' (model = spec, photo edge elsewhere) for the O6 group", bool(spec_cls) and not [f for f in o6 if f["cls"] == "build"],
          "; ".join(f"{f['item']} {f.get('edge')} -> {f['cls']}: {f['reason'][:90]}" for f in o6))
    corr = sc.load(tmp_corr, house)
    new = [c for c in corr["corrections"][n_before:] if c["path"] == ["openings", "O6", "rect"]]
    after = new[0]["after"]["v"] if new else None
    check("K1 the loop corrected the spec from the photo (rigid shift of the jambs), src I7.1, conf meranie",
          bool(new) and new[0]["src"] == "I7.1" and new[0]["conf"] == "meranie" and new[0].get("evidence", {}).get("method") == "rigid_shift",
          f"{new[0]['before']['v']} -> {after}" if new else "no correction of openings.O6.rect")
    check("K1 corrected spec within +-2 cm of the original value (types file)", after is not None and abs(after[0] - orig[0]) <= 0.02 and abs(after[1] - orig[1]) <= 0.02,
          f"original {orig[:2]}, corrected {after[:2] if after else None}")
    ev = [e for it in lg["iterations"][1:] for e in it["evaluated"] if e["item"] == "O6"]
    check("K1 next iteration within tolerance (error lowered, edges of O6 in tolerance)", bool(ev) and ev[0]["kept"] and ev[0]["after_cm"] <= 3.0,
          json.dumps(ev[:1], ensure_ascii=False))
    left = [f for f in lg["final_findings"] if (f["host"] == "O6" or f["item"].startswith("O6")) and f["cls"] in ("spec", "build")]
    check("K1 after the loop: no O6 deviation left, verify PASS", not left and lg["verify_pass"], f"left {left}")
    # restore: the model from the repo spec (no injection, repo corrections)
    pa = I.PilotA(house, port)
    spec = pa.load_spec(sc.load(pa.corr_path if pa.corr_path.exists() else None, house))
    b = pa.build(spec, set())
    v = pa.verify(spec)
    check("K1 model rebuilt from the repo spec afterwards (O6 back, verify PASS)", v["pass"] and not b["mismatches"], f"{b['n_changes']} changes")


# -- K2 artificial build error ------------------------------------------------------------------------------------------
def artificial_build(port: int) -> None:
    house = "M062_L2"
    from geom import frames_of
    TMP.mkdir(parents=True, exist_ok=True)
    tmp_corr = TMP / f"{house}.build_error.json"
    real = sc.path_of(house)
    if real.exists():
        shutil.copy(real, tmp_corr)
    elif tmp_corr.exists():
        tmp_corr.unlink()
    log_dir = OUT / house / "i7_test_build"
    shutil.rmtree(log_dir, ignore_errors=True)
    pa = I.PilotA(house, port, corr_path=tmp_corr)
    spec = pa.load_spec(sc.load(tmp_corr if tmp_corr.exists() else None, house))
    pa.build(spec, set())
    st = json.loads((OUT / house / "state.json").read_text(encoding="utf-8"))["elements"]
    g0 = st["O1.cw"]["guid"]
    fr = frames_of(spec)["F"]
    ac = client(port)
    ac.guard_project(PROJECT_A)
    mv = {"elementsWithMoveVectors": [{"elementId": {"guid": g0}, "moveVector": {"x": float(fr.d[0]) * 0.10, "y": float(fr.d[1]) * 0.10, "z": 0.0}}]}
    res = ac.tapir("MoveElements", mv)
    check("K2 curtain wall O1 of M062 moved 0.10 m along the facade through the API (outside the spec)",
          all(r.get("success") for r in res.get("executionResults", [])), json.dumps(res)[:200])
    snap = json.dumps(sc.load(tmp_corr if tmp_corr.exists() else None, house)["corrections"], sort_keys=True)
    lg = I.Loop(pa, I.MAX_ITER, log_dir=log_dir).run()
    it1 = lg["iterations"][0]
    bld = [f for f in it1["findings"] if f["cls"] == "build" and f["item"] == "O1"]
    check("K2 verify finds it as class 'build' (rows of the curtain wall O1 out of tolerance)", bool(bld),
          f"{len(bld)} rows, e.g. " + "; ".join(f"{f.get('quantity')} {f.get('delta_cm')} cm" for f in bld[:3]))
    fx = [f for f in it1["fixes"] if f["cause"] == "build" and "O1.cw" in f["rebuild"]]
    check("K2 the loop rebuilds the curtain wall from the spec (O1.cw)", bool(fx), json.dumps(fx[:1], ensure_ascii=False)[:200])
    st2 = json.loads((OUT / house / "state.json").read_text(encoding="utf-8"))["elements"]
    ev = [e for it in lg["iterations"][1:] for e in it["evaluated"] if e["item"] == "O1"]
    check("K2 restored: new element, rows of O1 in tolerance, verify PASS", st2["O1.cw"]["guid"] != g0 and bool(ev) and ev[0]["kept"] and lg["verify_pass"],
          f"guid {g0[:8]} -> {st2['O1.cw']['guid'][:8]}; {json.dumps(ev[:1], ensure_ascii=False)[:160]}")
    snap2 = json.dumps(sc.load(tmp_corr if tmp_corr.exists() else None, house)["corrections"], sort_keys=True)
    check("K2 the spec was not changed (class build fixes the model, not the spec)", snap == snap2)


# -- pilot B ------------------------------------------------------------------------------------------------------------
def real_b(port: int) -> None:
    lg1 = I.run_b(port, I.MAX_ITER)
    lg2 = I.run_b(port, I.MAX_ITER)
    f1 = lg1["first_findings"]
    check("K3 HOF_B: every deviation (and every unreadable bar) has one class with evidence and a crop",
          all(f["cls"] in ("build", "spec", "ambiguous") and f.get("reason") and (f["source"] in ("rows", "hedge") or f.get("crop") and Path(f["crop"]).exists()) for f in f1),
          f"{len(f1)} findings: " + ", ".join(f"{c}: {sum(f['cls'] == c for f in f1)}" for c in ("build", "spec", "ambiguous")))
    cw15 = [f for f in f1 if f["item"] == "CW_1NP_15" and f["source"] in ("bar", "unreadable")]
    ok = bool(cw15) and all((f["cls"] == "spec" and f.get("fix")) or (f["cls"] == "ambiguous" and f.get("crop")) for f in cw15)
    check("K3 HOF_B CW_1NP_15 bar 1 (5.1 cm, double bar): corrected with evidence or ambiguous with a crop", ok,
          "; ".join(f"{f['edge']} {f['delta_cm']} cm -> {f['cls']}: {f['reason'][:150]}" for f in cw15))
    check("K4 HOF_B: second run of the loop = 0 fixes, verify (rows) PASS", lg2["fixes_total"] == 0 and lg2["verify_pass"],
          f"run 1: {lg1['fixes_total']}, run 2: {lg2['fixes_total']}")
    p = I.OUT_B / "report_G2.md"
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    check("K5 report_G2.md of pilot B written (CC BY attribution, what remains with crops)", "HOF_B" in txt and "CC BY 4.0" in txt and "Čo ostáva" in txt, str(p))


def regression(script: str, args: list[str]) -> None:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(TESTS / script)] + args, capture_output=True, text=True, timeout=7200, encoding="utf-8",
                       errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    summ = [ln.strip() for ln in p.stdout.splitlines() if "checks passed" in ln]
    check(f"K4 {script} {' '.join(args)} passes after the loop", p.returncode == 0, (summ[-1] if summ else p.stdout[-300:]) + f" ({time.monotonic() - t0:.0f} s)")
    if p.returncode != 0:
        print("\n".join(ln for ln in p.stdout.splitlines() if "FAIL" in ln)[:3000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--regression", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    args = ap.parse_args()
    offline()
    if not args.offline:
        ac = client(args.port)
        start = ac.project_path()
        check("open project is pilot A at the start", start.lower() == PROJECT_A.lower(), start)
        if start.lower() == PROJECT_A.lower():
            real_a(args.port)
            artificial_spec(args.port)
            artificial_build(args.port)
            I.write_g2_a()
            if args.regression:
                regression("test_pilot_a.py", ["--port", str(args.port), "--save"])
            else:
                client(args.port).tapir("SaveProject", {}, timeout=300)
            if not args.skip_b and switch(args.port, PROJECT_B):
                real_b(args.port)
                if args.regression:
                    regression("test_pilot_b.py", ["--port", str(args.port), "--save"])
                switch(args.port, PROJECT_A)
            end = client(args.port).project_path()
            check("pilot A is open again at the end", end.lower() == PROJECT_A.lower(), end)
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail[:300]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

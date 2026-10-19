"""
Acceptance test of increment I1 (ADR-001): BIM facade M015_L1 of the Senec pilot.

    python test_i1_m015.py --offline     # validator only, no Archicad
    python test_i1_m015.py               # full: adapter -> validate -> build (forced) -> build again -> verify
    python test_i1_m015.py --save        # ... and SaveProject at the end

Live part writes ONLY into C:/FacadePilot/Fasada_pilot_A.pln (the build refuses any other open project)
and only touches elements recorded in C:/FacadePilot/out/M015_L1/state.json.

Acceptance (ADR-001, I1):
  A1 number of openings in the model = spec
  A2 delta of position / size of every opening, model vs spec <= 1 cm (read back from Archicad)
  A3 overlay PNG over the rectified photo exists
  A4 second build run = 0 changes
  A5 run time < 2 min (forced build + second build + verify)
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

FACADE = Path(__file__).resolve().parent.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

from spec_validate import validate  # noqa: E402
from from_senec import build_spec  # noqa: E402

OUT = Path(r"C:\FacadePilot\out")
PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
HOUSE = "M015_L1"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def offline() -> dict:
    spec = build_spec(HOUSE)
    errors, warnings = validate(spec)
    check("spec from the Senec adapter is valid", not errors, "; ".join(errors))
    check("spec has 7 openings (3 doors, 4 windows) as in otvory.py / domy.py",
          len(spec["openings"]) == 7 and sum(o["kind"] == "door" for o in spec["openings"]) == 3)
    check("touching O3/O4 reported as warning (rule 30)", any("O3 and O4 touch" in w for w in warnings))

    bad = copy.deepcopy(spec)
    bad["openings"][1]["rect"]["v"] = [1.2, 2.0, 0.2, 2.5]                 # overlaps O1
    check("validator: overlap is an error", any("overlap" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["openings"][4]["rect"].update({"v": [1.32, 2.22, 0.0, 1.5], "conf": "odhad"})
    check("validator: estimated window with sill 0 is an error (rule 13)", any("rule 13" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["openings"][0]["system"] = "door.nonexistent"
    check("validator: unknown system is an error", any("not in systems" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["openings"][0]["rect"]["v"] = [7.0, 8.0, 0.1, 2.5]
    check("validator: opening outside the wall is an error", any("not inside wall zone" in e for e in validate(bad)[0]))
    bad = copy.deepcopy(spec)
    bad["zones"][0]["thickness"]["src"] = "S99"
    check("validator: unknown source is an error", any("not in sources" in e for e in validate(bad)[0]))
    return spec


def live(spec: dict, port: int, save: bool) -> None:
    from build import build
    from verify import verify

    spec_path = OUT / HOUSE / f"{HOUSE}.facade-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    t0 = time.monotonic()
    first = build(spec, PROJECT, port, OUT, save=False, force=True)
    check("build (forced rebuild) without read-back mismatches", not first["mismatches"],
          f"{first['n_changes']} changes in {first['seconds']} s; {first['mismatches']}")
    second = build(spec, PROJECT, port, OUT, save=False, force=False)
    check("A4 second build = 0 changes", second["n_changes"] == 0, f"{second['changes']} in {second['seconds']} s")
    ver = verify(spec, PROJECT, port, OUT)
    elapsed = time.monotonic() - t0
    check("A1 openings in the wall = spec", ver["count"]["ok"], f"model {ver['count']['model_in_wall']}, spec {ver['count']['spec']}")
    tol = spec["tolerances"]["build"] * 100
    check("A2 delta <= 1 cm (parameters read back)", ver["max_delta_cm"]["params"] <= tol + 1e-9, f"max {ver['max_delta_cm']['params']:.2f} cm")
    check("A2 delta <= 1 cm (3D geometry projected)", ver["max_delta_cm"]["3D"] <= tol + 1e-9, f"max {ver['max_delta_cm']['3D']:.2f} cm")
    check("verify without problems", not ver["problems"], "; ".join(ver["problems"]))
    check("A3 overlay PNG exists", Path(ver["overlay_png"]).exists() and Path(ver["overlay_png"]).stat().st_size > 10000, ver["overlay_png"])
    check("A5 time < 2 min (build + build + verify)", elapsed < 120, f"{elapsed:.1f} s")
    if save:
        from tapir_client import TapirClient
        ac = TapirClient(port=port, timeout=300)
        ac.guard_project(PROJECT)
        check("SaveProject", bool(ac.tapir("SaveProject", {}).get("success")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    spec = offline()
    if not args.offline:
        live(spec, args.port, args.save)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

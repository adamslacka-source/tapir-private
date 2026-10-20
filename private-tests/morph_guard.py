"""Guard of the morph model of pilot A when the project stories change (G2 cause 5): snapshot of the 3D bounding boxes
and home stories of every Morph / Mesh / Roof (the street model of the project, not built by the facade pipeline) and
comparison with a later state. Read-only; never touches an element.

    python morph_guard.py snapshot [--out C:/FacadePilot/out/_stories_check/before.json]
    python morph_guard.py compare before.json [after.json]      # max |delta| per type, 0.0 cm expected
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "facade"))
from tapir_client import TapirClient  # noqa: E402

PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
TYPES = ("Morph", "Mesh", "Roof")
DIR = Path(r"C:\FacadePilot\out\_stories_check")


def snapshot(port: int = 19723) -> dict:
    ac = TapirClient(port=port, timeout=180)
    ac.guard_project(PROJECT)
    out = {"stories": ac.tapir("GetStories")["stories"], "elements": {}}
    for t in TYPES:
        els = ac.tapir("GetElementsByType", {"elementType": t})["elements"]
        for i in range(0, len(els), 250):
            part = els[i:i + 250]
            bb = ac.tapir("Get3DBoundingBoxes", {"elements": part}, timeout=180)["boundingBoxes3D"]
            dd = ac.tapir("GetDetailsOfElements", {"elements": part, "fields": ["floorIndex"]}, timeout=180)["detailsOfElements"]
            for e, b, d in zip(part, bb, dd):
                box = b.get("boundingBox3D")
                out["elements"][e["elementId"]["guid"]] = {"type": t, "floor": d.get("floorIndex"),
                                                          "box": [box[k] for k in ("xMin", "yMin", "zMin", "xMax", "yMax", "zMax")] if box else None}
        ac.api("API.IsAlive")
    return out


def compare(a: dict, b: dict) -> dict:
    res = {}
    for t in TYPES:
        ga = {g: e for g, e in a["elements"].items() if e["type"] == t}
        gb = {g: e for g, e in b["elements"].items() if e["type"] == t}
        both = [g for g in ga if g in gb and ga[g]["box"] and gb[g]["box"]]
        d = max([max(abs(x - y) for x, y in zip(ga[g]["box"], gb[g]["box"])) for g in both] or [0.0])
        res[t] = {"before": len(ga), "after": len(gb), "compared": len(both), "missing": len(set(ga) - set(gb)),
                  "max_delta_cm": round(d * 100, 4), "floors_before": sorted({e["floor"] for e in ga.values()}),
                  "floors_after": sorted({e["floor"] for e in gb.values()})}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snapshot", "compare"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--out", default=str(DIR / "before.json"))
    ap.add_argument("--port", type=int, default=19723)
    args = ap.parse_args()
    if args.cmd == "snapshot":
        s = snapshot(args.port)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(s), encoding="utf-8")
        print(f"{len(s['elements'])} elements, stories {[(x['index'], x['level'], x['name']) for x in s['stories']]} -> {args.out}")
        return 0
    a = json.loads(Path(args.files[0]).read_text(encoding="utf-8"))
    b = json.loads(Path(args.files[1]).read_text(encoding="utf-8")) if len(args.files) > 1 else snapshot(args.port)
    r = compare(a, b)
    print(json.dumps(r, indent=1))
    ok = all(v["missing"] == 0 and v["max_delta_cm"] == 0.0 and v["before"] == v["after"] for v in r.values())
    print("MORPHS UNMOVED" if ok else "MORPHS MOVED / MISSING")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

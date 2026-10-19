"""Adapter: pilot Senec (Lichnerova, Fasady_model) -> facade-spec/1 (I1 + I2 systems).

Reads the pilot's scripts as TEXT (Python ast) - nothing in the pilot folder is executed, imported
or written. Sources used:
  skripty/fasada3d/domy*.py   function <house>(): Path(...) with z0, eaves height, opening list
                              (s0, s1, v0, v1, depth[, "sklo"]) passed to panel(...)
  skripty/otvory.py           OPENINGS[<facade>]: kind (window/door), certainty, note
  skripty/fasady.py           FACADES[<facade>]: ends a/b, photo, metric rectification frame
  kontrola/fasada_<facade>_rektif.jpg   rectified photo (size only)
  senec_types/<house>.json    (in this repo) I2 fill types + wall build-up read from the rectified photo;
                              I3 camera of the photo, profiles, linear elements (cornice, string course) and the
                              surrounds + sill ledges generated around the listed openings (apply_linear)

I1 limits: one wall plane per facade = one literal panel(...) call with a literal opening list;
anything else (grid(...), several panels) is refused with a clear error.

    python from_senec.py M015_L1 [--root <Fasady_model>] [--out C:/FacadePilot/out]
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from spec_validate import low_values, validate  # noqa: E402

DEFAULT_ROOT = Path(r"C:\Users\adams\OneDrive\AT_Verejny_priestor_Senec\Fasady_model")
DEFAULT_OUT = Path(r"C:\FacadePilot\out")
TYPES_DIR = HERE / "senec_types"      # I2: fill types / build-up read from the photo, per house

# I1 system choice: existing Favorites of the pilot project (Czech SK/CZ template), chosen by rule.
# Every choice is conf 'default' - the photo does not tell the product. I2 replaces this catalogue with
# senec_types/<house>.json when it exists (fill types and build-up read from the photo).
SYSTEMS = {
    "wall.basic_450": {"type": "wall", "favorite": "Cihla plná - nosná, 300", "structure": "Basic", "reference_line": "Outside",
                       "conf": "default", "src": None,
                       "note": "favorite only for the building material and pens; thickness comes from the zone (Basic wall)"},
    "win.2kr": {"type": "window", "favorite": "Dřevěné okno - dvoukřídlé 01", "conf": "default", "src": None,
                "note": "upper-floor window; photo shows two sashes, frame material NEOVERENÉ"},
    "win.fixed": {"type": "window", "favorite": "Hliníkové okno - pevné", "conf": "default", "src": None,
                  "note": "shop window (fixed glazing)"},
    "door.entrance": {"type": "door", "favorite": "Hliníkové dveře - vchodové 01", "nominal": "wallhole", "conf": "default", "src": None,
                      "note": "shop entrance; library part Door 27, nominal switched to wall hole (gs_nominal_size = 1)"},
    "door.gate": {"type": "door", "favorite": "Dřevěné dveře - vchodové 01", "nominal": "wallhole", "conf": "default", "src": None,
                  "note": "two-leaf gate with fanlight in reality; no two-leaf entrance favorite exists -> single-leaf Door 27 (type mismatch, I2)"},
}
WALL_THICKNESS = 0.45      # ODHAD: historic brick front wall, not visible on the photo
CERTAINTY = {"dobra": "meranie", "ODHAD": "odhad"}


class AdapterError(RuntimeError):
    pass


# ---- reading the pilot scripts without executing them ------------------------------------------
class _Resolver:
    """Evaluates literal expressions inside a function body with simple local constants."""

    def __init__(self, consts: dict):
        self.consts = consts

    def value(self, node):
        if isinstance(node, ast.Name):
            if node.id in self.consts:
                return self.consts[node.id]
            raise AdapterError(f"unknown name '{node.id}' (line {node.lineno})")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr == "L":
            path = self.consts.get(node.value.id)
            if isinstance(path, dict) and "L" in path:
                return path["L"]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self.value(node.operand)
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(e) for e in node.elts]
        if isinstance(node, ast.Constant):
            return node.value
        raise AdapterError(f"not a literal expression: {ast.unparse(node)} (line {getattr(node, 'lineno', '?')})")


def _find_house_function(root: Path, house: str) -> tuple[Path, ast.FunctionDef]:
    candidates = sorted((root / "skripty" / "fasada3d").glob("domy*.py"))
    for path in candidates:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.lower() == house.lower():
                return path, node
    raise AdapterError(f"house function '{house.lower()}' not found in {[p.name for p in candidates]}")


def _read_house(root: Path, house: str) -> dict:
    path, fn = _find_house_function(root, house)
    consts: dict = {}
    res = _Resolver(consts)
    panels = []
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], (ast.Name, ast.Tuple)):
            target, value = stmt.targets[0], stmt.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Path":
                pts = res.value(value.args[0])
                z0 = next((res.value(k.value) for k in value.keywords if k.arg == "z0"), None)
                if z0 is None or len(pts) != 2:
                    raise AdapterError(f"{path.name}:{stmt.lineno}: I1 needs Path([a, b], z0=...) with two points")
                consts[target.id] = {"pts": pts, "z0": z0, "L": math.dist(pts[0], pts[1]), "line": stmt.lineno}
                continue
            try:
                if isinstance(target, ast.Tuple):
                    vals = res.value(value)
                    for t, v in zip(target.elts, vals):
                        consts[t.id] = v
                else:
                    consts[target.id] = res.value(value)
            except AdapterError:
                pass                                     # non-literal helper variable - irrelevant unless used below
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and getattr(stmt.value.func, "id", None) == "panel":
            panels.append(stmt.value)
    if len(panels) != 1:
        raise AdapterError(f"{path.name}:{fn.name}: I1 supports exactly one panel(...) call, found {len(panels)}")
    call = panels[0]
    if len(call.args) < 8:
        raise AdapterError(f"{path.name}:{call.lineno}: panel(...) call with unexpected arguments")
    path_name = call.args[1].id
    facade_path = consts[path_name]
    s0, s1, v0, v1 = (res.value(a) for a in call.args[2:6])
    ops = res.value(call.args[7])
    return {"file": path, "function": fn.name, "line": fn.lineno, "path": facade_path, "rect": [s0, s1, v0, v1],
            "eave_name": ast.unparse(call.args[5]), "ops": ops, "panel_line": call.lineno}


def _read_otvory(root: Path) -> dict:
    path = root / "skripty" / "otvory.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"W": "window", "D": "door"}

    class _Names(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id in names:
                return ast.copy_location(ast.Constant(names[node.id]), node)
            return node

    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "OPENINGS":
            return ast.literal_eval(_Names().visit(node.value))
    raise AdapterError("OPENINGS not found in otvory.py")


def _read_fasady(root: Path) -> dict:
    path = root / "skripty" / "fasady.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "FACADES":
            out = {}
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(v, ast.Call) and getattr(v.func, "id", None) == "dict":
                    out[ast.literal_eval(k)] = {kw.arg: ast.literal_eval(kw.value) for kw in v.keywords}
            return out
    raise AdapterError("FACADES not found in fasady.py")


def _image_size(path: Path) -> list[int]:
    from PIL import Image
    with Image.open(path) as im:
        return [im.width, im.height]


# ---- conversion ----------------------------------------------------------------------------
def _system_for(kind: str, rect: list[float], note: str) -> str:
    if kind == "door":
        return "door.gate" if ("brana" in note or "dvojkridl" in note) else "door.entrance"
    if rect[2] >= 3.0:
        return "win.2kr"
    return "win.fixed"


def build_spec(house: str, root: Path = DEFAULT_ROOT) -> dict:
    h = _read_house(root, house)
    otv = _read_otvory(root)
    fac = _read_fasady(root)
    (ax, ay), (bx, by) = h["path"]["pts"]
    fid = next((k for k, f in fac.items()
                if math.dist(f["a"], (ax, ay)) < 1e-6 and math.dist(f["b"], (bx, by)) < 1e-6), None)
    if fid is None:
        raise AdapterError(f"no facade in fasady.py with ends {h['path']['pts']}")
    f = fac[fid]
    if fid not in otv:
        raise AdapterError(f"no OPENINGS['{fid}'] in otvory.py")
    rel = lambda p: str(Path(p).relative_to(root)).replace("\\", "/")

    image = root / "kontrola" / f"fasada_{fid}_rektif.jpg"
    S = f["S"]
    u0 = f["u_vis"][0] - 0.5
    v1 = round(f.get("v_top", f["z1"] - f["ground"] + 0.6), 6)
    size = _image_size(image)
    expected_w = int((f["u_vis"][1] + 0.5 - u0) * S)
    if abs(size[0] - expected_w) > 2:
        raise AdapterError(f"{image.name}: width {size[0]} px does not match the frame from fasady.py ({expected_w} px)")

    sources = [
        {"id": "S1", "type": "photo", "file": f"01_podklady/foto/{f['photo']}",
         "rect": {"image": rel(image), "S": S, "u0": u0, "v1": v1, "size": size,
                  "ppm_photo": f.get("ppm"), "scale_src": f.get("scale_src"),
                  "v_origin": f"ground line yg of the photo (survey z {f['ground']} when rectified); v of the openings is relative to it"},
         "note": "openings and eaves read from this photo; transcribed in S2 and S3"},
        {"id": "S2", "type": "script", "file": f"{rel(h['file'])}#{h['function']}",
         "note": f"line {h['line']}: Path ends, z0 (after pata_oprava from the survey), eaves {h['eave_name']}, opening list (panel line {h['panel_line']}); depths are ODHAD"},
        {"id": "S3", "type": "script", "file": f"skripty/otvory.py#OPENINGS[{fid}]", "note": "kind, certainty and note of every opening"},
        {"id": "S4", "type": "script", "file": f"skripty/fasady.py#FACADES[{fid}]",
         "note": "facade ends a/b (morph front, matches OSM corners within 0.1 m) and rectification frame"},
        {"id": "S5", "type": "archicad", "file": "C:/FacadePilot/Fasada_pilot_A.pln",
         "note": f"morph {f['morph']} ({f['guid']}) sits on floorIndex 0 (1.NP); the stories are template stories, not surveyed"},
    ]

    L = h["path"]["L"]
    s0, s1, v0, v1w = h["rect"]
    if abs(s1 - L) > 1e-9 and s1 != L:
        raise AdapterError("panel does not span the whole facade")
    zone = {"id": "Z1", "kind": "wall", "rect": {"v": [round(s0, 4), round(L, 4), v0, v1w], "src": "S1", "conf": "meranie",
                                                 "note": f"s = whole facade a-b; top = eaves {h['eave_name']} = {v1w} m read from the photo"},
            "thickness": {"v": WALL_THICKNESS, "src": None, "conf": "odhad", "note": "not visible on the photo; historic brick front wall"},
            "system": "wall.basic_450"}

    openings = []
    for i, op in enumerate(h["ops"], 1):
        r = [float(x) for x in op[:4]]
        depth = float(op[4]) if len(op) > 4 else None
        glazing = op[5] if len(op) > 5 else None
        match = [o for o in otv[fid] if all(abs(a - b) < 0.005 for a, b in zip(o[:4], r))]
        if len(match) != 1:
            raise AdapterError(f"opening {r} of {house}: {len(match)} matches in OPENINGS['{fid}']")
        _, _, _, _, kind, cert, note = match[0]
        conf = CERTAINTY.get(cert)
        if conf is None:
            raise AdapterError(f"unknown certainty '{cert}' in otvory.py")
        item = {"id": f"O{i}", "kind": kind, "zone": "Z1",
                "rect": {"v": r, "src": "S1", "conf": conf}, "kind_src": "S3", "kind_conf": conf,
                "system": _system_for(kind, r, note), "note": note}
        if depth is not None:
            item["reveal"] = {"v": depth, "src": "S2", "conf": "odhad", "note": "depth from oblique views (ODHAD); not used by the I1 build"}
        if glazing:
            item["glazing"] = glazing
        openings.append(item)

    spec = {
        "schema": "facade-spec/1",
        "id": house,
        "title": f"{house} – {fid} (pilot Senec, Lichnerova)",
        "generated": {"by": "facade/adapters/from_senec.py", "at": dt.datetime.now().isoformat(timespec="seconds"),
                      "root": str(root), "facade": fid},
        "reference": {"a": [ax, ay], "b": [bx, by], "src": "S4", "conf": "meranie",
                      "z0": {"v": h["path"]["z0"], "src": "S2", "conf": "meranie", "note": "terrain at the facade base from survey BODY.stx (pata_oprava 2026-10-09)"},
                      "outside": "right_of_ab", "layer": f"FASADA – {house}",
                      "home_story": {"floorIndex": 0, "src": "S5", "conf": "pravidlo",
                                     "note": "same home story as the morph of the house; wall height is fixed (no top link) because the project stories are template stories"}},
        "sources": sources,
        "stories": [{"id": "1NP", "level": {"v": 0.0, "src": None, "conf": "odhad", "note": "ground floor taken at the facade foot; real floor level unknown"}}],
        "zones": [zone],
        "openings": openings,
        "systems": SYSTEMS,
        "tolerances": {"build": 0.01, "kota": 0.01, "meranie": 0.05, "pravidlo": 0.10, "odhad": None},
    }
    types_file = TYPES_DIR / f"{house}.json"
    if types_file.exists():
        apply_types(spec, json.loads(types_file.read_text(encoding="utf-8")))
        spec["generated"]["types"] = str(types_file)
    spec["assumptions"] = assumptions_of(spec)
    return spec


def assumptions_of(spec: dict) -> list[dict]:
    """Every value below 'meranie' -> one row for gate G1 (ADR 3.2); generic walk over the spec."""
    rows = [{"ref": ref, "value": value, "conf": conf, "why": note, "status": "pending"}
            for ref, value, conf, _src, note in low_values(spec)]
    hs = spec["reference"]["home_story"]
    rows.insert(0, {"ref": "reference.home_story", "value": hs["floorIndex"], "conf": hs["conf"], "why": hs["note"], "status": "pending"})
    return rows


def apply_types(spec: dict, types: dict) -> None:
    """I2: fill types and wall build-up read from the photo (senec_types/<house>.json) -> systems + openings."""
    spec["sources"].append(dict(types["source"]))
    spec["systems"] = dict(types["systems"])
    for z in spec["zones"]:
        if z["id"] in types.get("zones", {}):
            z.update(types["zones"][z["id"]])
    for key in ("g1_proposals", "simplifications"):
        if key in types:
            spec[key] = types[key]
    by_id = {o["id"]: o for o in spec["openings"]}
    for oid, extra in types.get("openings", {}).items():
        if oid not in by_id:
            raise AdapterError(f"types file: opening {oid} is not in the facade")
        by_id[oid].update(extra)
        if "reveal" in by_id[oid]:
            by_id[oid]["reveal"]["note"] = "depth from oblique views (ODHAD); I2: wall inset of the frame from the outside face"
    apply_linear(spec, types)


def apply_linear(spec: dict, types: dict) -> None:
    """I3: camera of the photo, linear elements (cornice, string course) and the window surrounds of the types file."""
    if "source_i3" in types:
        spec["sources"].append(dict(types["source_i3"]))
    if "camera" in types:
        photo = next(s for s in spec["sources"] if s.get("rect"))
        photo["rect"]["camera"] = dict(types["camera"])
    L = round(spec["zones"][0]["rect"]["v"][1], 4)
    items = []
    for it in types.get("linear", []):
        it = json.loads(json.dumps(it))
        it["s"]["v"] = [L if x == "L" else x for x in it["s"]["v"]]
        items.append(it)
    sur = types.get("surrounds")
    if sur:
        by_id = {o["id"]: o for o in spec["openings"]}
        band, below, over = sur["band"], sur["ledge_top_below_sill"], sur["ledge_overhang"]
        ledge_h = spec["systems"][spec["systems"][sur["ledge_system"]]["profile"]]["h"]
        pos = {"src": types.get("source_i3", {}).get("id"), "conf": "pravidlo"}
        for oid in sur["openings"]:
            s0, s1, v0, v1 = by_id[oid]["rect"]["v"]
            b, lt = band["v"], round(v0 - below["v"], 4)
            ref = f"{sur['position_note']}; band {b} ({band['src']}, {band['conf']})"
            items += [
                {"id": f"{oid}_sill", "kind": "sill_ledge", "system": sur["ledge_system"], "axis": "h", "host": oid,
                 "s": dict(pos, v=[round(s0 - b - over["v"], 4), round(s1 + b + over["v"], 4)],
                           note=f"surround outside + overhang {over['v']} ({over['src']}, {over['conf']})"),
                 "v": dict(pos, v=round(lt - ledge_h["v"], 4),
                           note=f"ledge top = window bottom - {below['v']} ({below['src']}, {below['conf']}), height {ledge_h['v']}"),
                 "photo_edges": [{"name": "spodná hrana podokennej rímsy", "corner": "bottom_front", "s": [s0 + 0.1, s1 - 0.1],
                                  "brighter": "above", "tol": 0.05, "search": 0.04}],
                 "note": "podokenná rímska"},
                {"id": f"{oid}_head", "kind": "surround_head", "system": sur["head_system"], "axis": "h", "host": oid,
                 "s": dict(pos, v=[round(s0 - b, 4), round(s1 + b, 4)], note=ref),
                 "v": dict(pos, v=round(v1, 4), note="on top of the opening"),
                 "photo_edges": [{"name": "horná hrana šambrány", "corner": "top_front", "s": [s0 + 0.1, s1 - 0.1],
                                  "brighter": "above", "tol": 0.05, "search": 0.04}],
                 "note": "šambrána – nadpražie"},
                {"id": f"{oid}_jl", "kind": "surround_jamb", "system": sur["jamb_system"], "axis": "v", "host": oid,
                 "s": dict(pos, v=[round(s0 - b, 4), round(s0, 4)], note=ref),
                 "v": dict(pos, v=[lt, round(v1, 4)], note="from the sill ledge to the head"),
                 "note": "šambrána – ľavé ostenie"},
                {"id": f"{oid}_jr", "kind": "surround_jamb", "system": sur["jamb_system"], "axis": "v", "host": oid,
                 "s": dict(pos, v=[round(s1, 4), round(s1 + b, 4)], note=ref),
                 "v": dict(pos, v=[lt, round(v1, 4)], note="from the sill ledge to the head"),
                 "note": "šambrána – pravé ostenie"},
            ]
    if items:
        spec["linear"] = items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("house")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    spec = build_spec(args.house, args.root)
    errors, warnings = validate(spec)
    for w in warnings:
        print("WARNING:", w)
    for e in errors:
        print("ERROR:", e)
    out = args.out / args.house / f"{args.house}.facade-spec.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}: {len(spec['openings'])} openings, {len(spec['assumptions'])} assumptions, "
          f"{'VALID' if not errors else 'INVALID'}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

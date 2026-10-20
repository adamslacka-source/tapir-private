"""Adapter: pilot Senec (Lichnerova, Fasady_model) -> facade-spec/1 (I1 + I2 systems, I3 linear, pilot A zones / objects).

Reads the pilot's scripts as TEXT (Python ast) - nothing in the pilot folder is executed, imported or written. The
function of the house is walked statement by statement with a tiny evaluator of literal expressions (numbers, names of
earlier literal assignments, + - * /, lists / tuples, slices, `<path>.L`, the pilot helper grid(...) re-implemented
here, `for` loops over literal sequences). Anything else is recorded as "not read" - never evaluated. Sources used:
  skripty/fasada3d/domy*.py   function <house>(): Path(...) with z0 (keyword or positional), panel(...) calls (one
                              wall zone per panel; opening lists given literally, by name or by grid(...)), sbox / sill /
                              window_frame / arch_shell / splate / sprism / balcony / arched_dormer calls (each one has
                              to be mapped or deliberately skipped by the types file: nothing is lost silently)
  skripty/otvory.py           OPENINGS[<facade>]: kind (window/door), certainty, note
  skripty/fasady.py           FACADES[<facade>]: ends a/b, photo, metric rectification frame
  kontrola/fasada_<facade>_rektif.jpg   rectified photo (size only)
  senec_types/<house>.json    (in this repo) everything read by hand from the photo or decided for the model:
                              fill types and wall build-ups (I2), camera, profiles, linear elements (I3), further planes
                              (corner = nárožie), zone split / zone systems / zone offsets, corrected opening rects,
                              surrounds (GDL FS_Surround) and sills generated around listed openings, GDL objects
                              (arch gable), the mapping of every pilot call, simplifications and G1 proposals

    python from_senec.py M015_L1 [--root <Fasady_model>] [--out C:/FacadePilot/out]
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from spec_validate import low_values, validate  # noqa: E402

DEFAULT_ROOT = Path(r"C:\Users\adams\OneDrive\AT_Verejny_priestor_Senec\Fasady_model")
DEFAULT_OUT = Path(r"C:\FacadePilot\out")
TYPES_DIR = HERE / "senec_types"      # fill types / build-up / zones / objects read from the photo, per house

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
PILOT_FNS = {"panel", "sbox", "sill", "window_frame", "arch_shell", "splate", "sprism", "hplate", "balcony", "arched_dormer",
             "historic_surround", "revolve"}
TAG_DEFAULTS = {"sill": "sill", "window_frame": "frame", "balcony": "balcony", "arched_dormer": "dormer",
                "historic_surround": "surround", "panel": "wall"}
TAG_ARG = {"sbox": 8, "sprism": 5, "splate": 5, "hplate": 5, "arch_shell": 9, "panel": 8, "window_frame": 10, "revolve": 4}


SAFE_MATH = {"radians": math.radians, "degrees": math.degrees, "sin": math.sin, "cos": math.cos, "tan": math.tan, "sqrt": math.sqrt}


class AdapterError(RuntimeError):
    pass


# ---- reading the pilot scripts without executing them ------------------------------------------
class _Resolver:
    """Evaluates literal expressions with the constants seen so far (module level + function body)."""

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
            ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
                   ast.Div: lambda a, b: a / b}
            for k, f in ops.items():
                if isinstance(node.op, k):
                    if isinstance(left, dict) or isinstance(right, dict):
                        break
                    return f(left, right)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(e) for e in node.elts]
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Subscript):
            seq = self.value(node.value)
            if isinstance(seq, list):
                sl = node.slice
                if isinstance(sl, ast.Slice):
                    lo = self.value(sl.lower) if sl.lower else None
                    hi = self.value(sl.upper) if sl.upper else None
                    return seq[lo:hi]
                return seq[self.value(sl)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)                 and node.func.value.id == "math" and node.func.attr in SAFE_MATH and not node.keywords:
            return SAFE_MATH[node.func.attr](*(self.value(a) for a in node.args))     # pure numeric functions only
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "math" and node.attr == "pi":
            return math.pi
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "grid" and not node.keywords:
            cols, rows, depth = (self.value(a) for a in node.args)          # pilot helper, re-implemented (domy_blok1.py)
            return [[a, b, v0, v1, depth] for a, b in cols for v0, v1 in rows]
        raise AdapterError(f"not a literal expression: {ast.unparse(node)} (line {getattr(node, 'lineno', '?')})")


def _module_consts(tree: ast.Module) -> dict:
    consts = {}
    res = _Resolver(consts)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = res.value(node.value)
            except AdapterError:
                pass
    return consts


def _find_house_function(root: Path, house: str) -> tuple[Path, ast.Module, ast.FunctionDef]:
    candidates = sorted((root / "skripty" / "fasada3d").glob("domy*.py"))
    for path in candidates:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.lower() == house.lower():
                return path, tree, node
    raise AdapterError(f"house function '{house.lower()}' not found in {[p.name for p in candidates]}")


def _bind(target, value, consts: dict) -> None:
    if isinstance(target, ast.Name):
        consts[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (list, tuple)) or len(value) != len(target.elts):
            raise AdapterError(f"cannot unpack into {ast.unparse(target)}")
        for t, v in zip(target.elts, value):
            _bind(t, v, consts)
    else:
        raise AdapterError(f"unsupported assignment target {ast.unparse(target)}")


def _read_house(root: Path, house: str) -> dict:
    path, tree, fn = _find_house_function(root, house)
    consts = _module_consts(tree)
    res = _Resolver(consts)
    out = {"file": path, "function": fn.name, "line": fn.lineno, "paths": {}, "panels": [], "calls": [], "not_read": [],
           "odhady": [], "T": consts.get("T")}

    def record_call(call: ast.Call):
        name = call.func.id
        try:
            args = [None, None] + [res.value(a) for a in call.args[2:]]       # (mesh, path) are not values
            kw = {k.arg: res.value(k.value) for k in call.keywords}
        except AdapterError as exc:
            out["not_read"].append({"line": call.lineno, "call": name, "text": ast.unparse(call)[:140], "why": str(exc)})
            return
        if len(call.args) < 2 or not isinstance(call.args[1], ast.Name):
            out["not_read"].append({"line": call.lineno, "call": name, "text": ast.unparse(call)[:140], "why": "no path argument"})
            return
        pname = call.args[1].id
        tag = kw.get("tag")
        if tag is None and len(args) > TAG_ARG.get(name, 99):
            tag = args[TAG_ARG[name]]
        tag = tag or TAG_DEFAULTS.get(name, name)
        if name == "panel":
            if len(args) < 8:
                raise AdapterError(f"{path.name}:{call.lineno}: panel(...) with unexpected arguments")
            out["panels"].append({"path": pname, "s0": args[2], "s1": args[3], "v0": args[4], "v1": args[5], "t": args[6],
                                  "ops": args[7], "tag": tag, "line": call.lineno, "ops_text": ast.unparse(call.args[7])[:80]})
        else:
            out["calls"].append({"fn": name, "path": pname, "args": args[2:], "tag": tag, "line": call.lineno})

    def run(stmts):
        for st in stmts:
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                target, value = st.targets[0], st.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Path" and isinstance(target, ast.Name):
                    pts = res.value(value.args[0])
                    z0 = next((res.value(k.value) for k in value.keywords if k.arg == "z0"), None)
                    if z0 is None and len(value.args) > 1:
                        z0 = res.value(value.args[1])
                    if z0 is None or len(pts) != 2:
                        raise AdapterError(f"{path.name}:{st.lineno}: needs Path([a, b], z0) with two points")
                    out["paths"][target.id] = {"pts": pts, "z0": z0, "L": math.dist(pts[0], pts[1]), "line": st.lineno}
                    consts[target.id] = out["paths"][target.id]
                    continue
                if isinstance(target, ast.Name) and target.id == "info" and isinstance(value, ast.Dict):
                    for k, v in zip(value.keys, value.values):
                        if isinstance(k, ast.Constant) and k.value == "odhady":
                            try:
                                out["odhady"] += res.value(v)
                            except AdapterError:
                                pass
                    continue
                if isinstance(target, ast.Subscript) and ast.unparse(target) == "info['odhady']":
                    try:
                        out["odhady"] += res.value(value)              # strings, '+' with module constants
                    except AdapterError:
                        pass
                    continue
                try:
                    _bind(target, res.value(value), consts)
                except AdapterError:
                    pass                                 # non-literal helper variable - irrelevant unless used below
            elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and getattr(st.value.func, "id", None) in PILOT_FNS:
                record_call(st.value)
            elif isinstance(st, ast.For):
                try:
                    seq = res.value(st.iter)
                except AdapterError as exc:
                    out["not_read"].append({"line": st.lineno, "call": "for", "text": ast.unparse(st)[:140].replace("\n", " "), "why": str(exc)})
                    continue
                for item in seq:
                    _bind(st.target, item, consts)
                    run(st.body)

    run(fn.body)
    return out


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


def _match_otvor(entries: list, r: list[float]):
    """Entry of otvory.py for a pilot opening: same s ends (+-5 mm) and overlapping v; (entry, differences)."""
    hits = [o for o in entries if abs(o[0] - r[0]) < 0.005 and abs(o[1] - r[1]) < 0.005 and o[2] < r[3] and r[2] < o[3]]
    if len(hits) != 1:
        return None, None
    o = hits[0]
    diff = [f"{n} {a} (otvory.py) / {b} (domy)" for n, a, b in zip(("v0", "v1"), o[2:4], r[2:4]) if abs(a - b) >= 0.005]
    return o, diff


def _ref_value(v, length: float):
    """'L' / 'L+0.2' / 'L-0.45' in a types file -> number (plane length + offset)."""
    if isinstance(v, str):
        m = re.fullmatch(r"L(?:([+-])(\d+(?:\.\d+)?))?", v.replace(" ", ""))
        if not m:
            raise AdapterError(f"types file: bad length expression {v!r}")
        return round(length + (float(m.group(2)) * (1 if m.group(1) == "+" else -1) if m.group(1) else 0.0), 4)
    return v


def build_spec(house: str, root: Path = DEFAULT_ROOT, i6: bool = False, corrections=None, types_hook=None) -> dict:
    """facade-spec/1 of a pilot house. i6=True adds the I5/I6 layer of the types file (section 'i6'): shop windows and
    glazed entrances as curtain wall fields instead of fixed windows / doors, balcony slabs and railings.
    corrections: the I7 corrections file (facade/corrections/<house>.json when None, no corrections when False, else a
    path) - its 'types' entries are applied to the types file before the spec is derived, its 'spec' entries to the
    result (facade/spec_corrections.py). types_hook(types): test hook (I7 acceptance: an artificial error in the types)."""
    import spec_corrections as sc
    h = _read_house(root, house)
    otv = _read_otvory(root)
    fac = _read_fasady(root)
    types_file = TYPES_DIR / f"{house}.json"
    types = json.loads(types_file.read_text(encoding="utf-8")) if types_file.exists() else {}
    if types_hook is not None:
        types_hook(types)
    corr = sc.load(None if corrections is False else (sc.path_of(house) if corrections is None else Path(corrections)), house)
    c_applied, c_stale = sc.apply(types, corr, "types")
    if i6 and not types.get("i6"):
        raise AdapterError(f"{types_file.name}: no 'i6' section (curtain walls, balconies, railings)")
    if i6 and types["i6"].get("pilot_mapping"):
        types["pilot_mapping"] = dict(types.get("pilot_mapping", {}), **types["i6"]["pilot_mapping"])

    # the reference facade = the path whose ends are a facade of fasady.py
    fid, ref_path = None, None
    for pname, p in h["paths"].items():
        (ax, ay), (bx, by) = p["pts"]
        for k, f in fac.items():
            if math.dist(f["a"], (ax, ay)) < 1e-6 and math.dist(f["b"], (bx, by)) < 1e-6:
                fid, ref_path = k, pname
    if fid is None:
        raise AdapterError(f"no facade in fasady.py with the ends of any path of {house}: {[p['pts'] for p in h['paths'].values()]}")
    f = fac[fid]
    if fid not in otv:
        raise AdapterError(f"no OPENINGS['{fid}'] in otvory.py")
    rel = lambda p: str(Path(p).relative_to(root)).replace("\\", "/")
    T = h["T"] if h["T"] is not None else 0.0

    image = root / "kontrola" / f"fasada_{fid}_rektif.jpg"
    S = f["S"]
    u0 = f["u_vis"][0] - 0.5
    v1 = round(f.get("v_top", f["z1"] - f["ground"] + 0.6), 6)
    size = _image_size(image)
    expected_w = int((f["u_vis"][1] + 0.5 - u0) * S)
    if abs(size[0] - expected_w) > 2:
        raise AdapterError(f"{image.name}: width {size[0]} px does not match the frame from fasady.py ({expected_w} px)")

    P = h["paths"][ref_path]
    sources = [
        {"id": "S1", "type": "photo", "file": f"01_podklady/foto/{f['photo']}",
         "rect": {"image": rel(image), "S": S, "u0": u0, "v1": v1, "size": size,
                  "ppm_photo": f.get("ppm"), "scale_src": f.get("scale_src"),
                  "v_origin": f"ground line yg of the photo (survey z {f['ground']} when rectified); v of the openings is relative to it"},
         "note": "openings and eaves read from this photo; transcribed in S2 and S3"},
        {"id": "S2", "type": "script", "file": f"{rel(h['file'])}#{h['function']}",
         "note": f"line {h['line']}: Path ends, z0 (after pata_oprava from the survey), panels (wall zones), opening lists "
                 f"(panel lines {', '.join(str(p['line']) for p in h['panels'])}); depths are ODHAD"},
        {"id": "S3", "type": "script", "file": f"skripty/otvory.py#OPENINGS[{fid}]", "note": "kind, certainty and note of every opening"},
        {"id": "S4", "type": "script", "file": f"skripty/fasady.py#FACADES[{fid}]",
         "note": "facade ends a/b (morph front, matches OSM corners within 0.1 m) and rectification frame"},
        {"id": "S5", "type": "archicad", "file": "C:/FacadePilot/Fasada_pilot_A.pln",
         "note": f"morph {f['morph']} ({f['guid']}) sits on floorIndex 0 (1.NP); the stories are template stories, not surveyed"},
    ]
    L = P["L"]
    (ax, ay), (bx, by) = P["pts"]

    # ---- planes: the reference (photo) + the ones the types file declares (e.g. the side facade at the corner)
    planes, plane_of_path = [], {ref_path: "F"}
    for pl in types.get("planes", []):
        pp = h["paths"].get(pl["path"])
        if pp is None:
            raise AdapterError(f"types file: plane {pl['id']} uses path '{pl['path']}', not in {house}")
        if abs(pp["z0"] - P["z0"]) > 1e-6:
            raise AdapterError(f"plane {pl['id']}: z0 {pp['z0']} differs from the reference {P['z0']} (one z0 per spec)")
        plane_of_path[pl["path"]] = pl["id"]
        planes.append({"id": pl["id"], "a": pp["pts"][0], "b": pp["pts"][1], "outside": pl.get("outside", "right_of_ab"),
                       "src": "S2", "conf": pl.get("conf", "meranie"), "photo": None,
                       **({"joins": pl["joins"]} if pl.get("joins") else {}),
                       "note": f"{pl.get('note', '')} (path {pl['path']}, {rel(h['file'])} line {pp['line']}); no rectified photo".strip()})
    length_of = {"F": L} | {pl["id"]: h["paths"][p]["L"] for p, pl in zip([x["path"] for x in types.get("planes", [])], planes)}

    # ---- zones: one per panel on a known plane; the wall face offset is the panel thickness minus the pilot slab T
    zones, openings, skipped = [], [], []
    oi = 0
    for pi, pn in enumerate(h["panels"]):
        plane = plane_of_path.get(pn["path"])
        if plane is None:
            skipped.append({"ref": f"pilot panel line {pn['line']}", "text": f"panel on path '{pn['path']}' ({pn['tag']}, "
                            f"s {pn['s0']:.2f}..{pn['s1']:.2f}, v {pn['v0']:.2f}..{pn['v1']:.2f}) is not on a plane of the spec - not modelled"})
            continue
        zid = f"Z{len(zones) + 1}"
        length = length_of[plane]
        if pn["s0"] < -1e-6 or pn["s1"] > length + 1e-6:
            raise AdapterError(f"panel line {pn['line']}: s {pn['s0']}..{pn['s1']} outside the path")
        w = round(pn["t"] - T, 6)
        zone = {"id": zid, "kind": "wall",
                "rect": {"v": [round(pn["s0"], 4), round(pn["s1"], 4), pn["v0"], pn["v1"]], "src": "S1" if plane == "F" else "S2",
                         "conf": "meranie" if plane == "F" else "odhad",
                         "note": f"panel '{pn['tag']}' line {pn['line']}" + (f"; s = whole facade a-b; top = {pn['v1']} m read from the photo" if plane == "F" else "")},
                "thickness": {"v": round(WALL_THICKNESS + max(w, 0.0), 6), "src": None, "conf": "odhad",
                              "note": "not visible on the photo; historic brick front wall" + (f" + offset {w}" if w > 0 else "")},
                "system": "wall.basic_450"}
        if plane != "F":
            zone["plane"] = plane
        if abs(w) > 1e-9:
            zone["w"] = {"v": w, "src": "S2", "conf": "odhad", "note": f"panel thickness {pn['t']} - pilot slab T {T} (line {pn['line']})"}
        zones.append(zone)
        entries = otv[fid] if plane == "F" else []
        for op in pn["ops"]:
            oi += 1
            r = [float(x) for x in op[:4]]
            depth = float(op[4]) if len(op) > 4 else None
            glazing = op[5] if len(op) > 5 else None
            item = {"id": f"O{oi}", "zone": zid, "rect": {"v": r, "src": "S1" if plane == "F" else "S2", "conf": "odhad"}}
            if plane == "F":
                o, diff = _match_otvor(entries, r)
                if o is None:
                    raise AdapterError(f"opening {r} of {house}: no unique match in OPENINGS['{fid}']")
                _, _, _, _, kind, cert, note = o
                conf = CERTAINTY.get(cert)
                if conf is None:
                    raise AdapterError(f"unknown certainty '{cert}' in otvory.py")
                item.update({"kind": kind, "rect": {"v": r, "src": "S1", "conf": conf}, "kind_src": "S3", "kind_conf": conf,
                             "system": _system_for(kind, r, note), "note": note})
                if diff:
                    item["rect"]["note"] = "NEJEDNOZNAČNÉ: " + "; ".join(diff) + " - the panel of domy.py is used"
            else:
                item.update({"kind": "window", "system": "win.fixed", "note": f"{pn['tag']} panel line {pn['line']} (no otvory.py entry)"})
            if depth is not None:
                item["reveal"] = {"v": round(depth, 6), "src": "S2", "conf": "odhad", "note": "depth from oblique views (ODHAD); not used by the I1 build"}
            if glazing:
                item["glazing"] = glazing
            if len(zones) == 1 and len(h["panels"]) == 1:
                item["zone"] = zid
            openings.append(item)

    spec = {
        "schema": "facade-spec/1",
        "id": house,
        "title": f"{house} – {fid} (pilot Senec, Lichnerova)",
        "generated": {"by": "facade/adapters/from_senec.py", "at": dt.datetime.now().isoformat(timespec="seconds"),
                      "root": str(root), "facade": fid},
        "reference": {"a": [ax, ay], "b": [bx, by], "src": "S4", "conf": "meranie",
                      "z0": {"v": P["z0"], "src": "S2", "conf": "meranie", "note": "terrain at the facade base from survey BODY.stx (pata_oprava 2026-10-09)"},
                      "outside": "right_of_ab", "layer": f"FASADA – {house}",
                      "home_story": {"floorIndex": 0, "src": "S5", "conf": "pravidlo",
                                     "note": "default only: every element gets the story of its foot from the project stories (stories[], "
                                             "senec_types/_project_stories.json); walls are top-linked to the story under their top"}},
        "sources": sources,
        "stories": project_stories(P["z0"]),
        "zones": zones,
        "openings": openings,
        "systems": SYSTEMS,
        "tolerances": {"build": 0.01, "kota": 0.01, "meranie": 0.05, "pravidlo": 0.10, "odhad": None},
    }
    if planes:
        spec["planes"] = planes
    if types.get("reference_joins"):
        spec["reference"]["joins"] = dict(types["reference_joins"])
    if h["odhady"]:
        spec["generated"]["pilot_odhady"] = h["odhady"]
    if types:
        apply_types(spec, types, root, h, length_of)
    if i6:
        apply_i6(spec, types["i6"])
        spec["generated"]["layer"] = "i6"
        spec["generated"]["types"] = str(types_file)
    spec.setdefault("simplifications", [])
    spec["simplifications"] += [{"ref": s["ref"], "text": s["text"], "proposal": "mimo rozsah pilota A (vykázané)"} for s in skipped]
    s_applied, s_stale = sc.apply(spec, corr, "spec")
    sc.finish_spec(spec, corr, c_applied + s_applied, c_stale + s_stale)
    spec["assumptions"] = assumptions_of(spec)
    return spec


def project_stories(z0: float) -> list[dict]:
    """Stories of the pilot A project (one set for the street, senec_types/_project_stories.json) relative to the facade foot
    z0 of this house: level = absolute level - z0, so that every house sets the same project stories (build.story_payload).
    G2 finding 11: every element gets the story of its foot, walls are top-linked (modeller rules 2 and 3)."""
    data = json.loads((TYPES_DIR / "_project_stories.json").read_text(encoding="utf-8"))
    out = []
    for st in data["stories"]:
        out.append({"id": st["id"], "floorIndex": st["floorIndex"], "name": st["name"],
                    "level": {"v": round(st["level_abs"] - z0, 6), "src": st["src"], "conf": st["conf"],
                              "note": f"absolútne {st['level_abs']:+.3f} (projekt), päta fasády {z0:+.3f}; {st['note']}"}})
    return out


def assumptions_of(spec: dict) -> list[dict]:
    """Every value below 'meranie' -> one row for gate G1 (ADR 3.2); generic walk over the spec."""
    rows = [{"ref": ref, "value": value, "conf": conf, "why": note, "status": "pending"}
            for ref, value, conf, _src, note in low_values(spec)]
    hs = spec["reference"]["home_story"]
    rows.insert(0, {"ref": "reference.home_story", "value": hs["floorIndex"], "conf": hs["conf"], "why": hs["note"], "status": "pending"})
    return rows


def _load_systems_from(types: dict) -> dict:
    """systems_from: {house: [system ids]} - copy systems VERBATIM from another types file (one FS favourite / composite
    shared by several houses must have exactly the same content, otherwise one build would rewrite the other's favourite)."""
    out = {}
    for other, sids in types.get("systems_from", {}).items():
        src = json.loads((TYPES_DIR / f"{other}.json").read_text(encoding="utf-8"))["systems"]
        for sid in sids:
            if sid not in src:
                raise AdapterError(f"systems_from: {sid} is not a system of {other}")
            out[sid] = json.loads(json.dumps(src[sid]))
    return out


def apply_types(spec: dict, types: dict, root: Path | None = None, h: dict | None = None, length_of: dict | None = None) -> None:
    """Types file -> sources, systems, zones (overrides, split), openings (overrides), linear, objects, mapping of the pilot calls."""
    length_of = length_of or {"F": spec["zones"][0]["rect"]["v"][1]}
    if "source" in types:
        spec["sources"].append(dict(types["source"]))
    for extra in types.get("sources", []):
        spec["sources"].append(dict(extra))
    systems = _load_systems_from(types)
    systems.update(types.get("systems", {}))
    if systems:
        spec["systems"] = systems
    zones = {z["id"]: z for z in spec["zones"]}
    for zid, extra in types.get("zones", {}).items():
        if zid not in zones:
            raise AdapterError(f"types file: zone {zid} does not exist (zones: {list(zones)})")
        extra = json.loads(json.dumps(extra))
        if "rect" in extra:                           # partial rect: None keeps the value read from the pilot panel
            old = zones[zid]["rect"]["v"]
            extra["rect"]["v"] = [o if n is None else n for n, o in zip(extra["rect"]["v"], old)]
            extra["rect"]["note"] = f"{extra['rect'].get('note', '')} (pilot panel {old})".strip()
        zones[zid].update(extra)
    for sp in types.get("zone_split", []):
        _split_zone(spec, sp)
    for key in ("g1_proposals", "simplifications"):
        if key in types:
            spec[key] = list(types[key]) if isinstance(types[key], list) else dict(types[key])
    by_id = {o["id"]: o for o in spec["openings"]}
    for oid, extra in types.get("openings", {}).items():
        if oid not in by_id:
            raise AdapterError(f"types file: opening {oid} is not in the facade")
        extra = json.loads(json.dumps(extra))
        if "rect" in extra and "rect_pilot" not in extra:
            extra["rect_pilot"] = list(by_id[oid]["rect"]["v"])
        by_id[oid].update(extra)
        if "reveal" in by_id[oid] and "note" not in extra.get("reveal", {}):
            by_id[oid]["reveal"]["note"] = "depth from oblique views (ODHAD); I2: wall inset of the frame from the outside face"
    _assign_zones(spec)
    apply_linear(spec, types, length_of)
    _map_pilot_calls(spec, types, h)


def apply_i6(spec: dict, i6: dict) -> None:
    """I5/I6 layer: systems (curtain wall, slab, railing), glazed fields replacing openings (their reveal is kept unless
    given), balconies, railings (reference line derived from the balcony outline: inset, arc as chords), simplifications
    and G1 proposals that change with it."""
    import geom
    spec["systems"].update(json.loads(json.dumps(i6.get("systems", {}))))
    by_id = {o["id"]: o for o in spec["openings"]}
    removed = set()
    cws = []
    for cw in json.loads(json.dumps(i6.get("cw_fields", []))):
        for oid in cw.get("replaces", []):
            if oid not in by_id:
                raise AdapterError(f"i6: cw field {cw['id']} replaces {oid}, which is not an opening")
            removed.add(oid)
        first = by_id[cw["replaces"][0]] if cw.get("replaces") else None
        if "reveal" not in cw and first is not None and "reveal" in first:
            cw["reveal"] = dict(first["reveal"], note=f"wall inset of the frames = reveal of {first['id']} ({first['reveal'].get('note', '')})")
        if "zone" not in cw and first is not None:
            cw["zone"] = first["zone"]
        if "photo_edges" not in cw and first is not None and first.get("photo_edges") and cw["rect"]["v"] == first["rect"]["v"]:
            cw["photo_edges"] = first["photo_edges"]          # explicit edges of the hole stay the contract of the field
        cws.append(cw)
    spec["openings"] = [o for o in spec["openings"] if o["id"] not in removed]
    for o in spec["openings"]:
        if o.get("merged_into") in removed:
            raise AdapterError(f"i6: opening {o['id']} is merged into the replaced opening {o['merged_into']}")
    for key in ("linear", "objects"):
        for it in spec.get(key, []):
            if it.get("host") in removed:
                raise AdapterError(f"i6: {key}[{it['id']}] belongs to the replaced opening {it['host']}")
    if cws:
        spec["cw_fields"] = cws
    bals = json.loads(json.dumps(i6.get("balconies", [])))
    if bals:
        spec["balconies"] = bals
    bal_by = {b["id"]: b for b in bals}
    rails = []
    for r in i6.get("railings", []):
        b = bal_by.get(r["on"])
        if b is None:
            raise AdapterError(f"i6: railing {r['id']} on unknown balcony {r['on']}")
        path = geom.railing_path(spec, b, r["inset"], r["chords"])
        o = geom.balcony_outline(spec, b)
        err = geom.chord_error(o["arc"][3] - r["inset"], o["arc"][4], r["chords"])
        confs = [b[k]["conf"] for k in ("s", "w_side", "w_crown")]
        conf = max(confs, key=("kota", "meranie", "pravidlo", "odhad", "default").index)
        rails.append({"id": r["id"], "on": b["id"], "system": r["system"], "plane": b.get("plane", "F"),
                      "path_sw": {"v": [[round(x, 4), round(y, 4)] for x, y in path], "src": b["s"]["src"], "conf": conf,
                                  "note": f"free edges of {b['id']} {r['inset']} m inside the slab edge; front arc as {r['chords']} chords "
                                          f"(max {err * 100:.1f} cm from the arc; CreateRailings has straight segments only)"},
                      "v": dict(b["v_top"], note=f"on top of balcony {b['id']}"), "inset": r["inset"], "chords": r["chords"],
                      "approx_error": round(err, 4), **({"note": r["note"]} if r.get("note") else {})})
    if rails:
        spec["railings"] = rails
    drop = set(i6.get("drop_simplifications", []))
    spec["simplifications"] = [x for x in spec.get("simplifications", []) if x["ref"] not in drop] + list(i6.get("simplifications", []))
    if i6.get("g1_proposals"):
        spec.setdefault("g1_proposals", {}).update(i6["g1_proposals"])
    spec["title"] += " + I5/I6 (CW, balkóny, zábradlia)"


def _split_zone(spec: dict, sp: dict) -> None:
    """Splits a wall zone horizontally at v (e.g. the stone cladding of the ground floor up to the string course):
    the zone keeps the upper part, a new zone takes the lower part with its own system / offset / thickness."""
    z = next((z for z in spec["zones"] if z["id"] == sp["zone"]), None)
    if z is None:
        raise AdapterError(f"zone_split: zone {sp['zone']} does not exist")
    s0, s1, v0, v1 = z["rect"]["v"]
    at = sp["at_v"]["v"]
    if not v0 < at < v1:
        raise AdapterError(f"zone_split: v {at} not inside zone {z['id']} {z['rect']['v']}")
    low = json.loads(json.dumps(sp["below"]))
    low.update({"kind": "wall", "rect": {"v": [s0, s1, v0, at], "src": sp["at_v"]["src"], "conf": sp["at_v"]["conf"],
                                         "note": f"split of {z['id']} at v {at}: {sp['at_v'].get('note', '')}".strip()}})
    if z.get("plane"):
        low.setdefault("plane", z["plane"])
    z["rect"] = {"v": [s0, s1, at, v1], "src": z["rect"]["src"], "conf": z["rect"]["conf"],
                 "note": f"{z['rect'].get('note', '')}; bottom = split at v {at} ({sp['at_v']['src']}, {sp['at_v']['conf']})"}
    spec["zones"].append(low)


def _assign_zones(spec: dict) -> None:
    """Every opening -> the wall zone that contains it (after a split an opening may change its zone)."""
    zones = spec["zones"]
    for o in spec["openings"]:
        cur = next((z for z in zones if z["id"] == o.get("zone")), None)
        plane = (cur or {}).get("plane", "F")
        s0, s1, v0, v1 = o["rect"]["v"]
        hits = [z for z in zones if z.get("plane", "F") == plane and z["rect"]["v"][0] - 1e-6 <= s0 and s1 <= z["rect"]["v"][1] + 1e-6
                and z["rect"]["v"][2] - 1e-6 <= v0 < z["rect"]["v"][3]]
        if len(hits) == 1:
            o["zone"] = hits[0]["id"]
        elif cur is None:
            raise AdapterError(f"opening {o['id']} {o['rect']['v']}: {len(hits)} zones contain it")


def apply_linear(spec: dict, types: dict, length_of: dict | None = None) -> None:
    """I3: camera of the photo, linear elements (cornice, string course, band, plinth...) and the elements generated
    around listed openings: sills (beams) and surrounds (pilot A: one GDL FS_Surround object + a sill ledge beam)."""
    length_of = length_of or {"F": round(spec["zones"][0]["rect"]["v"][1], 4)}
    if "source_i3" in types:
        spec["sources"].append(dict(types["source_i3"]))
    if "camera" in types:
        photo = next(s for s in spec["sources"] if s.get("rect"))
        photo["rect"]["camera"] = dict(types["camera"])
    items, objects = [], []
    for it in types.get("linear", []):
        it = json.loads(json.dumps(it))
        length = length_of.get(it.get("plane", "F"), length_of["F"])
        it["s"]["v"] = [_ref_value(x, round(length, 4)) for x in it["s"]["v"]]
        items.append(it)
    by_id = {o["id"]: o for o in spec["openings"]}
    zones = {z["id"]: z for z in spec["zones"]}
    ins = types.get("sills")
    if ins:
        prof_h = spec["systems"][spec["systems"][ins["system"]]["profile"]]["h"]["v"]
        for oid in ins["openings"]:
            o = by_id[oid]
            z = zones[o["zone"]]
            s0, s1, v0, _ = o["rect"]["v"]
            side = ins["side"]
            it = {"id": f"{oid}_sill", "kind": "sill", "system": ins["system"], "axis": "h", "host": oid,
                  "s": {"v": [round(s0 - side["v"], 4), round(s1 + side["v"], 4)], "src": side["src"], "conf": side["conf"],
                        "note": f"opening {oid} + {side['v']} each side ({side.get('note', '')})"},
                  "v": {"v": round(v0 - prof_h, 4), "src": o["rect"]["src"], "conf": o["rect"]["conf"],
                        "note": f"top of the sill = bottom of {oid}"},
                  "note": "parapetný plech / podokenná rímska pod oknom"}
            win = ins.get("photo_edges", {}).get(oid)
            if win:
                it["photo_edges"] = [{"name": "spodná hrana parapetu", "corner": "bottom_front", "s": win, "brighter": "above", "tol": 0.05, "search": 0.04}]
            if z.get("plane"):
                it["plane"] = z["plane"]
            if z.get("w"):
                it["w"] = dict(z["w"])
            items.append(it)
    sur = types.get("surrounds")
    if sur:
        band, below, over = sur["band"], sur["ledge_top_below_sill"], sur["ledge_overhang"]
        depth = sur["band_depth"]
        ledge_h = spec["systems"][spec["systems"][sur["ledge_system"]]["profile"]]["h"]
        pos = {"src": types.get("source_i3", {}).get("id"), "conf": "pravidlo"}
        right = spec["reference"]["outside"] == "right_of_ab"
        for oid in sur["openings"]:
            o = by_id[oid]
            s0, s1, v0, v1 = o["rect"]["v"]
            b, lt = band["v"], round(v0 - below["v"], 4)
            zw = zones[o["zone"]].get("w", {}).get("v", 0.0) if o.get("zone") else 0.0
            items.append(
                {"id": f"{oid}_sill", "kind": "sill_ledge", "system": sur["ledge_system"], "axis": "h", "host": oid,
                 "s": dict(pos, v=[round(s0 - b - over["v"], 4), round(s1 + b + over["v"], 4)],
                           note=f"surround outside + overhang {over['v']} ({over['src']}, {over['conf']})"),
                 "v": dict(pos, v=round(lt - ledge_h["v"], 4),
                           note=f"ledge top = window bottom - {below['v']} ({below['src']}, {below['conf']}), height {ledge_h['v']}"),
                 "photo_edges": [{"name": "spodná hrana podokennej rímsy", "corner": "bottom_front", "s": [s0 + 0.1, s1 - 0.1],
                                  "brighter": "above", "tol": 0.05, "search": 0.04}],
                 "note": "podokenná rímska"})
            src = {"src": band["src"], "conf": band["conf"]}
            par = {"opW": dict(o["rect"], v=round(s1 - s0, 4), note=f"width of {oid}"),
                   "opH": dict(pos, v=round(v1 - lt, 4), note=f"from the top of the sill ledge ({lt}) to the top of {oid} ({v1}): the jambs stand on the ledge"),
                   "bandW": dict(src, v=b, note=band.get("note", "")),
                   "bandD": dict(depth),
                   "bTop": dict(src, v=True), "bBottom": dict(src, v=False), "bLeft": dict(src, v=True), "bRight": dict(src, v=True),
                   "headOvh": dict(src, v=0.0, note="the head band runs over the jambs, no overhang (photo)")}
            if sur.get("surface"):
                par["matBand"] = dict(sur["surface"])
            objects.append({"id": f"{oid}_sur", "kind": "surround", "system": sur["system"], "host": oid,
                            "origin": dict(pos, v=[round(s1 if right else s0, 4), lt, zw],
                                           note=f"local origin of FS_Surround = lower {'right' if right else 'left'} inner corner "
                                                f"(local x runs {'against' if right else 'along'} s), on the sill ledge top"),
                            "params": par,
                            "photo_edges": [
                                {"name": "horná hrana šambrány", "edge": "top", "range": [s0 + 0.1, s1 - 0.1], "brighter": "above", "tol": 0.03, "search": 0.04},
                                {"name": "ľavá vonkajšia hrana šambrány", "edge": "left", "range": [v0 + 0.2, v1 - 0.2], "brighter": "left", "tol": 0.03, "search": 0.04},
                                {"name": "pravá vonkajšia hrana šambrány", "edge": "right", "range": [v0 + 0.2, v1 - 0.2], "brighter": "right", "tol": 0.03, "search": 0.04}],
                            "note": "šambrána okna 2.NP (ostenia + nadpražie) - jeden GDL objekt FS_Surround"})
    for ob in types.get("objects", []):
        objects.append(json.loads(json.dumps(ob)))
    if items:
        spec["linear"] = items
    if objects:
        spec["objects"] = objects


def _map_pilot_calls(spec: dict, types: dict, h: dict | None) -> None:
    """Every element call of the pilot house function has to be mapped (to spec items) or deliberately skipped with a
    reason in the types file (pilot_mapping: {tag or 'fn:tag': 'model: ...' | 'skip: ...'}); skipped ones and the
    expressions that could not be read become simplifications for G1. An unmapped call is an error."""
    if h is None:
        return
    mapping = types.get("pilot_mapping")
    if mapping is None:
        return
    rows, unmapped, seen = [], [], {}
    for c in h["calls"]:
        key = next((k for k in (f"{c['fn']}:{c['tag']}", c["tag"], c["fn"]) if k in mapping), None)
        if key is None:
            unmapped.append(f"{c['fn']}('{c['tag']}') line {c['line']}")
            continue
        seen.setdefault(key, []).append(c["line"])
    if unmapped:
        raise AdapterError(f"pilot calls not mapped in the types file (pilot_mapping): {unmapped}")
    for key, lines in seen.items():
        how = mapping[key]
        if how.startswith("skip:"):
            rows.append({"ref": f"pilot {key} (lines {', '.join(map(str, sorted(set(lines))))})", "text": how[5:].strip(),
                         "proposal": "vykázané; doplniť v ďalšom inkremente"})
    for nr in h["not_read"]:
        if nr["call"] in mapping.get("_not_read_ok", []):
            continue
        rows.append({"ref": f"pilot line {nr['line']}", "text": f"nečítané (nie je literál): {nr['text']} - {nr['why']}",
                     "proposal": "vykázané"})
    spec.setdefault("simplifications", [])
    spec["simplifications"] += rows
    spec["generated"]["pilot_calls"] = {k: {"lines": sorted(set(v)), "how": mapping[k]} for k, v in seen.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("house")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--i6", action="store_true", help="add the I5/I6 layer (curtain walls, balconies, railings)")
    args = ap.parse_args(argv)
    spec = build_spec(args.house, args.root, i6=args.i6)
    errors, warnings = validate(spec)
    for w in warnings:
        print("WARNING:", w)
    for e in errors:
        print("ERROR:", e)
    out = args.out / args.house / f"{args.house}.facade-spec.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}: {len(spec['zones'])} zones, {len(spec['openings'])} openings, {len(spec.get('linear', []))} linear, "
          f"{len(spec.get('objects', []))} objects, {len(spec['assumptions'])} assumptions, {'VALID' if not errors else 'INVALID'}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

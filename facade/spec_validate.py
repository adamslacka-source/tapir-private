"""Validator of facade-spec/1 (I1 + I2 + I3 subset): JSON Schema + consistency rules (ADR-001 ch. 4).

A broken rule is an ERROR (the build refuses the spec). Warnings are printed and kept in the
result, they do not stop the build (e.g. two fills that only touch - rule 30 of the modeller).

    python spec_validate.py <spec.json>          exit code 0 = valid
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from geom import frame_of, rect_overlap_area, rects_touch  # noqa: E402

SCHEMA_PATH = HERE / "spec" / "facade-spec-1.schema.json"
GDL_MAP_PATH = HERE / "gdl_param_map.json"
LOW_CONF = ("odhad", "default")
G1_CONF = ("pravidlo", "odhad", "default")          # below 'meranie' -> gate G1 (ADR 3.1)
CONF_ORDER = ("kota", "meranie", "pravidlo", "odhad", "default")
VALUE_KEYS = ("transom_h", "sidelight_w", "sidelight_sill", "transom_bars", "transom_bar_h", "bar_h", "reveal")
TYPE_KEYS = ("leaves", "transom", "sidelight", "sash_bars", "leaf_panel", "frame_surface")
EPS = 1e-6


def load_gdl_map() -> dict:
    return json.loads(GDL_MAP_PATH.read_text(encoding="utf-8"))


def lowest_conf(*confs: str) -> str:
    """The least certain of the given certainty levels."""
    return max(confs, key=CONF_ORDER.index)


def low_values(spec: dict, levels=G1_CONF):
    """(ref, value, conf, src, note) of every value of the spec below 'meranie' - generic walk."""
    def is_value(d):
        return isinstance(d, dict) and {"v", "src", "conf"} <= set(d)

    def walk(node, ref):
        if is_value(node):
            if node["conf"] in levels:
                yield ref, node["v"], node["conf"], node["src"], node.get("note", "")
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("sources", "assumptions", "generated", "tolerances"):
                    continue
                yield from walk(v, f"{ref}.{k}" if ref else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                key = i
                if isinstance(v, dict):
                    key = v.get("id", v.get("material", i))
                yield from walk(v, f"{ref}[{key}]")

    yield from walk({k: v for k, v in spec.items() if k != "systems"}, "")
    for sid, s in spec.get("systems", {}).items():
        if s.get("conf") in levels:
            key = next((k for k in ("base_favorite", "libpart", "name", "favorite") if s.get(k)), "favorite")
            yield f"systems[{sid}].{key}", s.get(key), s["conf"], s.get("src"), s.get("note", "")
        yield from walk({k: v for k, v in s.items() if k not in ("conf", "src", "note")}, f"systems[{sid}]")


def fs_system(s: dict) -> bool:
    """The system gets an 'FS ' favourite made by systems.py (from a base favourite, a library part or a linear sample)."""
    return bool(s.get("base_favorite") or s.get("base_libpart") or (s.get("type") == "linear" and s.get("favorite")))


def polygon_area(pts) -> float:
    a = 0.0
    for (x0, y0), (x1, y1) in zip(pts, list(pts[1:]) + list(pts[:1])):
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(spec: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    validator = jsonschema.Draft202012Validator(load_schema())
    for err in sorted(validator.iter_errors(spec), key=lambda e: list(e.path)):
        errors.append(f"schema: {'/'.join(map(str, err.path)) or '<root>'}: {err.message}")
    if errors:
        return errors, warnings

    source_ids = {s["id"] for s in spec["sources"]}
    item_ids: set[str] = set()

    def check_src(where: str, src, conf):
        if src is not None and src not in source_ids:
            errors.append(f"{where}: src '{src}' is not in sources")
        if src is None and conf not in LOW_CONF:
            errors.append(f"{where}: conf '{conf}' needs a source (src is null)")

    ref = spec["reference"]
    check_src("reference", ref["src"], ref["conf"])
    check_src("reference.z0", ref["z0"]["src"], ref["z0"]["conf"])
    frame = frame_of(spec)
    if frame.length < 0.1:
        errors.append("reference: a and b are (almost) identical")

    for st in spec["stories"]:
        check_src(f"stories[{st['id']}].level", st["level"]["src"], st["level"]["conf"])

    systems = spec["systems"]
    zones = {}
    for z in spec["zones"]:
        zid = z["id"]
        if zid in item_ids:
            errors.append(f"zones[{zid}]: duplicate id")
        item_ids.add(zid)
        zones[zid] = z
        s0, s1, v0, v1 = z["rect"]["v"]
        check_src(f"zones[{zid}].rect", z["rect"]["src"], z["rect"]["conf"])
        check_src(f"zones[{zid}].thickness", z["thickness"]["src"], z["thickness"]["conf"])
        if not (s1 > s0 and v1 > v0):
            errors.append(f"zones[{zid}]: empty rect {z['rect']['v']}")
        if s0 < -EPS or s1 > frame.length + 0.01:
            errors.append(f"zones[{zid}]: rect s {s0}..{s1} outside 0..L = {frame.length:.3f}")
        if z["thickness"]["v"] <= 0:
            errors.append(f"zones[{zid}]: thickness must be > 0")
        sysdef = systems.get(z["system"])
        if sysdef is None:
            errors.append(f"zones[{zid}]: system '{z['system']}' is not in systems")
        elif sysdef["type"] != "wall":
            errors.append(f"zones[{zid}]: system '{z['system']}' is a {sysdef['type']}, not a wall")
    if len(zones) != 1:
        errors.append(f"I1 supports exactly one wall zone per facade, got {len(zones)}")

    openings = spec["openings"]
    for o in openings:
        oid = o["id"]
        if oid in item_ids:
            errors.append(f"openings[{oid}]: duplicate id")
        item_ids.add(oid)
        s0, s1, v0, v1 = o["rect"]["v"]
        check_src(f"openings[{oid}].rect", o["rect"]["src"], o["rect"]["conf"])
        for k in VALUE_KEYS:
            if k in o:
                check_src(f"openings[{oid}].{k}", o[k]["src"], o[k]["conf"])
        if not (s1 > s0 and v1 > v0):
            errors.append(f"openings[{oid}]: empty rect {o['rect']['v']}")
            continue
        zone = zones.get(o.get("zone")) if o.get("zone") else next(iter(zones.values()), None)
        if zone is None:
            errors.append(f"openings[{oid}]: zone '{o.get('zone')}' does not exist")
        else:
            zs0, zs1, zv0, zv1 = zone["rect"]["v"]
            if s0 < zs0 - EPS or s1 > zs1 + EPS or v0 < zv0 - EPS or v1 > zv1 + EPS:
                errors.append(f"openings[{oid}]: rect {o['rect']['v']} is not inside wall zone {zone['id']} {zone['rect']['v']}")
        sysdef = systems.get(o["system"])
        if sysdef is None:
            errors.append(f"openings[{oid}]: system '{o['system']}' is not in systems")
        elif o["kind"] in ("window", "door") and sysdef["type"] != o["kind"] and not o.get("merged_into"):
            errors.append(f"openings[{oid}]: a {o['kind']} with system '{o['system']}' of type {sysdef['type']}")
        # modeller rule 13: a window whose height is estimated never gets sill 0
        if o["kind"] == "window" and o["rect"]["conf"] in LOW_CONF and v0 <= EPS:
            errors.append(f"openings[{oid}]: window with conf {o['rect']['conf']} has sill 0 (rule 13)")
        if o.get("story") and o["story"] not in {st["id"] for st in spec["stories"]}:
            errors.append(f"openings[{oid}]: story '{o['story']}' is not in stories")

    for i, o1 in enumerate(openings):
        for o2 in openings[i + 1:]:
            r1, r2 = o1["rect"]["v"], o2["rect"]["v"]
            area = rect_overlap_area(r1, r2)
            if area > EPS:
                errors.append(f"openings {o1['id']} and {o2['id']} overlap ({area:.3f} m2)")
            elif rects_touch(r1, r2):
                if o1.get("merged_into") == o2["id"] or o2.get("merged_into") == o1["id"]:
                    warnings.append(f"openings {o1['id']} and {o2['id']} touch - modelled as ONE element (merged_into: one frame on the photo)")
                else:
                    warnings.append(f"openings {o1['id']} and {o2['id']} touch (frames in contact, modeller rule 30) - "
                                    "probably one unit in reality; keep as two until a combined system exists (I2)")

    errors += _check_i2(spec, zones, openings, check_src)
    errors += _check_linear(spec, openings, frame, item_ids, check_src)

    axes = spec.get("axes")
    if axes and axes.get("s"):
        tol = axes.get("snap_tol", 0.05)
        for o in openings:
            if o["kind"] != "window":
                continue
            c = 0.5 * (o["rect"]["v"][0] + o["rect"]["v"][1])
            if min(abs(c - x) for x in axes["s"]) > tol:
                errors.append(f"openings[{o['id']}]: centre s = {c:.3f} is off every axis by more than {tol} m")

    if spec["tolerances"]["build"] > 0.05:
        warnings.append(f"tolerances.build = {spec['tolerances']['build']} m is unusually loose")
    return errors, warnings


def _check_i2(spec: dict, zones: dict, openings: list, check_src) -> list[str]:
    """I2 rules: systems catalogue (FS favourites, composite = zone thickness, mapped library parts), merged openings."""
    errors: list[str] = []
    systems = spec["systems"]
    gmap = load_gdl_map()["libparts"]
    for sid, s in systems.items():
        for k in TYPE_KEYS:
            if k in s:
                check_src(f"systems[{sid}].{k}", s[k]["src"], s[k]["conf"])
        if s.get("base_favorite") and s.get("base_libpart"):
            errors.append(f"systems[{sid}]: base_favorite and base_libpart exclude each other")
        if fs_system(s):
            if not (s.get("favorite") or "").startswith("FS "):
                errors.append(f"systems[{sid}]: the target favorite must start with 'FS ' (got {s.get('favorite')!r})")
            if s["type"] in ("window", "door"):
                lp = gmap.get(s.get("libpart", ""))
                if lp is None:
                    errors.append(f"systems[{sid}]: library part {s.get('libpart')!r} is not in gdl_param_map.json")
                elif lp["kind"] != s["type"]:
                    errors.append(f"systems[{sid}]: library part {s['libpart']} is a {lp['kind']}, the system is a {s['type']}")
                elif s.get("base_libpart"):
                    if not lp.get("base_libpart"):
                        errors.append(f"systems[{sid}]: {s['libpart']} is not marked base_libpart in gdl_param_map.json")
                elif s["base_favorite"] not in lp.get("base_favorites", []):
                    errors.append(f"systems[{sid}]: base favourite {s['base_favorite']!r} is not known to place {s['libpart']}")
        if "composite" in s:
            if s.get("structure") != "Composite":
                errors.append(f"systems[{sid}]: a composite needs structure 'Composite'")
            for sk in s["composite"]["skins"]:
                check_src(f"systems[{sid}].composite[{sk['material']}].thickness", sk["thickness"]["src"], sk["thickness"]["conf"])
            total = sum(sk["thickness"]["v"] for sk in s["composite"]["skins"])
            for z in zones.values():
                if z["system"] == sid and abs(total - z["thickness"]["v"]) > 0.001:
                    errors.append(f"systems[{sid}]: composite skins sum {total:.3f} m != zone {z['id']} thickness {z['thickness']['v']} m")
        elif s.get("structure") == "Composite":
            errors.append(f"systems[{sid}]: structure 'Composite' without a composite")

    by_id = {o["id"]: o for o in openings}
    for o in openings:
        host_id = o.get("merged_into")
        if not host_id:
            continue
        host = by_id.get(host_id)
        if host is None:
            errors.append(f"openings[{o['id']}]: merged_into '{host_id}' does not exist")
            continue
        if host.get("merged_into"):
            errors.append(f"openings[{o['id']}]: host {host_id} is itself merged")
            continue
        side = systems.get(host["system"], {}).get("sidelight", {}).get("v", "none")
        r, h = o["rect"]["v"], host["rect"]["v"]
        if side != "right":
            errors.append(f"openings[{o['id']}]: merged into {host_id}, whose system has no right sidelight (only 'right' is supported)")
        if abs(r[0] - h[1]) > 0.001:
            errors.append(f"openings[{o['id']}]: a right sidelight must start where {host_id} ends (s {r[0]} != {h[1]})")
        if abs(r[3] - h[3]) > 0.001:
            errors.append(f"openings[{o['id']}]: top {r[3]} differs from the host {host_id} top {h[3]} - not one frame")
        if r[2] < h[2] - 0.001:
            errors.append(f"openings[{o['id']}]: sidelight bottom {r[2]} is below the host bottom {h[2]}")
        if "sidelight_w" in host or "sidelight_sill" in host:
            errors.append(f"openings[{host_id}]: sidelight given twice (explicit values and merged {o['id']})")
    return errors


def linear_box(it: dict, prof: dict) -> list[float]:
    """[s0, s1, v0, v1] of a linear element on the facade plane."""
    s0, s1 = it["s"]["v"]
    if it["axis"] == "h":
        return [s0, s1, it["v"]["v"], it["v"]["v"] + max(p[1] for p in prof["polygon_wv"])]
    return [s0, s1] + list(it["v"]["v"])


def _check_linear(spec: dict, openings: list, frame, item_ids: set, check_src) -> list[str]:
    """I3 rules: profile systems (polygon outside the facade plane, w/h = polygon extents), linear systems (tool +
    existing profile), linear elements inside 0..L, vertical band width = profile width, nothing in front of an opening."""
    errors: list[str] = []
    systems = spec["systems"]
    for sid, s in systems.items():
        if s["type"] == "profile":
            pts = s.get("polygon_wv") or []
            if len(pts) < 3 or polygon_area(pts) < 1e-6:
                errors.append(f"systems[{sid}]: profile polygon is empty")
                continue
            if not s.get("name", "").startswith("FS ") or not s.get("material"):
                errors.append(f"systems[{sid}]: a profile needs name 'FS ...' and material")
            if min(p[0] for p in pts) < -EPS or min(p[1] for p in pts) < -EPS:
                errors.append(f"systems[{sid}]: profile polygon must lie in x >= 0 (outside the facade plane) and y >= 0")
            for key, idx in (("w", 0), ("h", 1)):
                if key in s:
                    check_src(f"systems[{sid}].{key}", s[key]["src"], s[key]["conf"])
                    ext = max(p[idx] for p in pts)
                    if abs(ext - s[key]["v"]) > 0.001:
                        errors.append(f"systems[{sid}]: {key} = {s[key]['v']} but the polygon extent is {ext}")
        elif s["type"] == "linear":
            prof = systems.get(s.get("profile", ""))
            if not s.get("tool") or prof is None or prof["type"] != "profile":
                errors.append(f"systems[{sid}]: a linear system needs tool and an existing profile system (got {s.get('profile')!r})")
    rects = {o["id"]: o["rect"]["v"] for o in openings}
    for it in spec.get("linear", []):
        lid = it["id"]
        if lid in item_ids:
            errors.append(f"linear[{lid}]: duplicate id")
        item_ids.add(lid)
        check_src(f"linear[{lid}].s", it["s"]["src"], it["s"]["conf"])
        check_src(f"linear[{lid}].v", it["v"]["src"], it["v"]["conf"])
        sysdef = systems.get(it["system"])
        if sysdef is None or sysdef["type"] != "linear":
            errors.append(f"linear[{lid}]: system '{it['system']}' is not a linear system")
            continue
        prof = systems.get(sysdef.get("profile", ""))
        if prof is None or prof["type"] != "profile" or not prof.get("polygon_wv"):
            continue
        if it["axis"] != {"beam": "h", "column": "v"}[sysdef["tool"]]:
            errors.append(f"linear[{lid}]: axis {it['axis']} does not fit tool {sysdef['tool']}")
            continue
        s0, s1 = it["s"]["v"]
        if not s1 > s0:
            errors.append(f"linear[{lid}]: empty s range {it['s']['v']}")
        if s0 < -EPS or s1 > frame.length + 0.01:
            errors.append(f"linear[{lid}]: s {s0}..{s1} outside 0..L = {frame.length:.3f}")
        if it["axis"] == "h" and not isinstance(it["v"]["v"], (int, float)):
            errors.append(f"linear[{lid}]: a horizontal element needs v = one number (base of the profile)")
            continue
        if it["axis"] == "v":
            if not isinstance(it["v"]["v"], list) or not it["v"]["v"][1] > it["v"]["v"][0]:
                errors.append(f"linear[{lid}]: a vertical element needs v = [v0, v1] with v1 > v0")
                continue
            width = max(p[1] for p in prof["polygon_wv"])
            if abs((s1 - s0) - width) > 0.001:
                errors.append(f"linear[{lid}]: band width {s1 - s0:.3f} != profile width {width:.3f}")
        if it.get("host") and it["host"] not in rects:
            errors.append(f"linear[{lid}]: host opening '{it['host']}' does not exist")
        box = linear_box(it, prof)
        for oid, r in rects.items():                                 # modeller rule 30: nothing in front of an opening
            if rect_overlap_area(box, r) > 1e-4:
                errors.append(f"linear[{lid}]: lies in front of opening {oid} ({rect_overlap_area(box, r):.4f} m2)")
    return errors


def element_id(spec: dict, item: dict, *conf_values: str) -> str:
    """Archicad Element ID: <spec.id>.<item.id>, '?' when a value that shapes the element is odhad/default."""
    return f"{spec['id']}.{item['id']}" + ("?" if any(c in LOW_CONF for c in conf_values) else "")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    errors, warnings = validate(spec)
    for w in warnings:
        print("WARNING:", w)
    for e in errors:
        print("ERROR:", e)
    print("VALID" if not errors else f"INVALID ({len(errors)} errors)")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

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
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from geom import (REFERENCE_PLANE, balcony_outline, cw_grid, frame_of, frames_of, item_plane, outside_of,  # noqa: E402
                  rect_overlap_area, rects_touch, zone_w)
import gdl_expect  # noqa: E402

SCHEMA_PATH = HERE / "spec" / "facade-spec-1.schema.json"
GDL_MAP_PATH = HERE / "gdl_param_map.json"
LOW_CONF = ("odhad", "default")
G1_CONF = ("pravidlo", "odhad", "default")          # below 'meranie' -> gate G1 (ADR 3.1)
CONF_ORDER = ("kota", "meranie", "pravidlo", "odhad", "default")
VALUE_KEYS = ("transom_h", "sidelight_w", "sidelight_sill", "transom_bars", "transom_cols", "transom_bar_h", "bar_h", "reveal", "shoulder_h")
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
            # per-edge certainty of a rect (G2 kolo 2, N1): an edge below 'meranie' is a row of its own when the rect is measured
            elif isinstance(node.get("edge_conf"), dict) and isinstance(node["v"], list) and len(node["v"]) == 4:
                for e, ec in node["edge_conf"].items():
                    if ec.get("conf") in levels:
                        yield f"{ref}.{e}", node["v"][("left", "right", "bottom", "top").index(e)], ec["conf"], ec.get("src"), ec.get("note", "")
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
    """The system gets an 'FS ' favourite made by systems.py (from a base favourite, a library part, a linear / slab sample
    or the curtain wall defaults)."""
    return bool(s.get("base_favorite") or s.get("base_libpart") or
                (s.get("type") in ("linear", "curtain_wall", "slab") and s.get("favorite")))


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
    frames = _check_planes(spec, errors, check_src)

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
        if z.get("w"):
            check_src(f"zones[{zid}].w", z["w"]["src"], z["w"]["conf"])
        pl = z.get("plane", REFERENCE_PLANE)
        if pl not in frames:
            errors.append(f"zones[{zid}]: plane '{pl}' does not exist")
            continue
        length = frames[pl].length
        if not (s1 > s0 and v1 > v0):
            errors.append(f"zones[{zid}]: empty rect {z['rect']['v']}")
        if s0 < -EPS or s1 > length + 0.01:
            errors.append(f"zones[{zid}]: rect s {s0}..{s1} outside 0..L = {length:.3f} of plane {pl}")
        if z["thickness"]["v"] <= 0:
            errors.append(f"zones[{zid}]: thickness must be > 0")
        if zone_w(z) >= z["thickness"]["v"]:
            errors.append(f"zones[{zid}]: offset w {zone_w(z)} >= thickness - the wall would stand in front of its plane")
        sysdef = systems.get(z["system"])
        if sysdef is None:
            errors.append(f"zones[{zid}]: system '{z['system']}' is not in systems")
        elif sysdef["type"] != "wall":
            errors.append(f"zones[{zid}]: system '{z['system']}' is a {sysdef['type']}, not a wall")
    if not zones:
        errors.append("no wall zone")
    zl = list(zones.values())
    for i, z1 in enumerate(zl):                       # zones of one plane: no overlap (modeller rule 9)
        for z2 in zl[i + 1:]:
            if z1.get("plane", REFERENCE_PLANE) != z2.get("plane", REFERENCE_PLANE):
                continue
            area = rect_overlap_area(z1["rect"]["v"], z2["rect"]["v"])
            if area > EPS:
                errors.append(f"zones {z1['id']} and {z2['id']} overlap ({area:.3f} m2) - two walls in one place")
            elif rects_touch(z1["rect"]["v"], z2["rect"]["v"]):
                back1, back2 = zone_w(z1) - z1["thickness"]["v"], zone_w(z2) - z2["thickness"]["v"]
                if abs(back1 - back2) > 0.001 and z1.get("role") != "rizalit" and z2.get("role") != "rizalit":
                    warnings.append(f"zones {z1['id']} and {z2['id']} touch but their inside faces differ "
                                    f"({back1:+.3f} / {back2:+.3f} m) - step on the inside of the wall")

    openings = spec["openings"]
    objects = {ob["id"]: ob for ob in spec.get("objects", [])}
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
        if len(zones) > 1 and not o.get("zone"):
            errors.append(f"openings[{oid}]: several wall zones - the opening must name its zone")
        zone = zones.get(o.get("zone")) if o.get("zone") else next(iter(zones.values()), None)
        if zone is None:
            errors.append(f"openings[{oid}]: zone '{o.get('zone')}' does not exist")
        else:
            zs0, zs1, zv0, zv1 = zone["rect"]["v"]
            top = v1
            if o.get("head_in"):                       # arched head carried by an arch gable above the zone
                top = _check_arch_head(o, zone, objects.get(o["head_in"]), errors)
            if s0 < zs0 - EPS or s1 > zs1 + EPS or v0 < zv0 - EPS or top > zv1 + EPS:
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
            if item_plane(o1, zones) != item_plane(o2, zones):
                continue
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
    pb_errors, pb_warnings = _check_pilot_b(spec, frames, item_ids, check_src)
    errors += pb_errors
    warnings += pb_warnings
    i6_errors, i6_warnings = _check_i6(spec, zones, openings, frames, item_ids, check_src)
    errors += i6_errors
    warnings += i6_warnings
    rect_items = openings + cw_as_openings(spec)                 # a glazed field (CW) is an opening of its wall too
    errors += _check_linear(spec, rect_items, frames, item_ids, check_src, zones)
    errors += _check_objects(spec, rect_items, frames, item_ids, check_src, zones)

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


def _check_planes(spec: dict, errors: list, check_src) -> dict:
    """Further planes (pilot A: nárožie): source, length, and corners - a plane named in joins must share that end point."""
    frames = frames_of(spec)
    ends = {pid: {"a": fr.a, "b": fr.b} for pid, fr in frames.items()}
    seen = {REFERENCE_PLANE}
    for p in spec.get("planes", []):
        if p["id"] in seen:
            errors.append(f"planes[{p['id']}]: duplicate id (F is the reference)")
        seen.add(p["id"])
        check_src(f"planes[{p['id']}]", p["src"], p["conf"])
        if frames[p["id"]].length < 0.1:
            errors.append(f"planes[{p['id']}]: a and b are (almost) identical")
        if p.get("photo") and p["photo"] not in {s["id"] for s in spec["sources"]}:
            errors.append(f"planes[{p['id']}]: photo '{p['photo']}' is not in sources")
    joins = [(REFERENCE_PLANE, spec["reference"].get("joins", {}))] + [(p["id"], p.get("joins", {})) for p in spec.get("planes", [])]
    for pid, jn in joins:
        for end, other in jn.items():
            if other not in frames:
                errors.append(f"plane {pid}: joins.{end} = '{other}' does not exist")
                continue
            here = ends[pid][end]
            if min(float(np.linalg.norm(here - ends[other][e])) for e in ("a", "b")) > 0.01:
                errors.append(f"plane {pid}: end {end} does not meet plane {other} (no common corner point, modeller rule 8)")
    return frames


def _joined(spec: dict, plane: str, end: str) -> bool:
    if plane == REFERENCE_PLANE:
        return end in spec["reference"].get("joins", {})
    return any(p["id"] == plane and end in p.get("joins", {}) for p in spec.get("planes", []))


def _check_arch_head(o: dict, zone: dict, ob: dict | None, errors: list) -> float:
    """An opening whose arched head stands above its wall zone, inside an arch gable object: the arch must fit the ring.
    Returns the top of the part of the opening inside the zone (the springing line)."""
    s0, s1, v0, v1 = o["rect"]["v"]
    oid = o["id"]
    if ob is None or ob["kind"] != "arch_gable":
        errors.append(f"openings[{oid}]: head_in '{o.get('head_in')}' is not an arch gable object")
        return v1
    if "shoulder_h" not in o:
        errors.append(f"openings[{oid}]: an arched head needs shoulder_h (springing above the bottom of the hole)")
        return v1
    spring = v0 + o["shoulder_h"]["v"]
    r_in = ob["params"]["rIn"]["v"]
    os_, ov, _ = ob["origin"]["v"]
    for what, got, want in (("centre s", 0.5 * (s0 + s1), os_), ("half width = rIn", 0.5 * (s1 - s0), r_in),
                            ("springing = arch origin v", spring, ov), ("springing = top of zone", spring, zone["rect"]["v"][3]),
                            ("top = springing + rIn", v1, spring + r_in)):
        if abs(got - want) > 0.01:
            errors.append(f"openings[{oid}]: arched head - {what}: {got:.3f} != {want:.3f}")
    return spring


def linear_box(it: dict, prof: dict) -> list[float]:
    """[s0, s1, v0, v1] of a linear element on the facade plane."""
    s0, s1 = it["s"]["v"]
    if it["axis"] in ("h", "w"):
        return [s0, s1, it["v"]["v"], it["v"]["v"] + max(p[1] for p in prof["polygon_wv"])]
    return [s0, s1] + list(it["v"]["v"])


def _check_linear(spec: dict, openings: list, frames: dict, item_ids: set, check_src, zones: dict | None = None) -> list[str]:
    """I3 rules: profile systems (polygon outside the facade plane, w/h = polygon extents), linear systems (tool +
    existing profile), linear elements inside 0..L of their plane (beyond it only round a joined corner, by at most the
    profile), vertical band width = profile width, nothing in front of an opening of the same plane."""
    errors: list[str] = []
    systems = spec["systems"]
    wrap_profiles: dict = {}
    for it in spec.get("linear", []):
        sd = systems.get(it["system"], {})
        if sd.get("profile"):
            wrap_profiles.setdefault(sd["profile"], set()).add(bool(it.get("wrap")))
    for sid, s in systems.items():
        if s["type"] == "profile":
            pts = s.get("polygon_wv") or []
            if len(pts) < 3 or polygon_area(pts) < 1e-6:
                errors.append(f"systems[{sid}]: profile polygon is empty")
                continue
            if not s.get("name", "").startswith("FS ") or not s.get("material"):
                errors.append(f"systems[{sid}]: a profile needs name 'FS ...' and material")
            corner = s.get("corner_profile") is True
            if min(p[1] for p in pts) < -EPS or (min(p[0] for p in pts) < -EPS and not corner):
                errors.append(f"systems[{sid}]: profile polygon must lie in x >= 0 (outside the facade plane) and y >= 0")
            if corner and wrap_profiles.get(sid, {True}) != {True}:
                errors.append(f"systems[{sid}]: a corner profile (x < 0 round the corner) is only for elements with 'wrap'")
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
    zones = zones or {}
    rects = {o["id"]: (o["rect"]["v"], item_plane(o, zones)) for o in openings}
    for it in spec.get("linear", []):
        lid = it["id"]
        if lid in item_ids:
            errors.append(f"linear[{lid}]: duplicate id")
        item_ids.add(lid)
        check_src(f"linear[{lid}].s", it["s"]["src"], it["s"]["conf"])
        check_src(f"linear[{lid}].v", it["v"]["src"], it["v"]["conf"])
        if it.get("w"):
            check_src(f"linear[{lid}].w", it["w"]["src"], it["w"]["conf"])
        plane = it.get("plane", REFERENCE_PLANE)
        if plane not in frames:
            errors.append(f"linear[{lid}]: plane '{plane}' does not exist")
            continue
        length = frames[plane].length
        sysdef = systems.get(it["system"])
        if sysdef is None or sysdef["type"] != "linear":
            errors.append(f"linear[{lid}]: system '{it['system']}' is not a linear system")
            continue
        prof = systems.get(sysdef.get("profile", ""))
        if prof is None or prof["type"] != "profile" or not prof.get("polygon_wv"):
            continue
        if it["axis"] not in {"beam": ("h", "w"), "column": ("v",)}[sysdef["tool"]]:
            errors.append(f"linear[{lid}]: axis {it['axis']} does not fit tool {sysdef['tool']}")
            continue
        s0, s1 = it["s"]["v"]
        if not s1 > s0:
            errors.append(f"linear[{lid}]: empty s range {it['s']['v']}")
        reach = max(abs(p[0]) for p in prof["polygon_wv"]) + 0.01     # how far a wrapped element may pass the corner
        lo = -reach if it.get("wrap") == "a" and _joined(spec, plane, "a") else -EPS
        hi = length + (reach if it.get("wrap") == "b" and _joined(spec, plane, "b") else 0.01)
        if it.get("wrap") and not _joined(spec, plane, it["wrap"]):
            errors.append(f"linear[{lid}]: wrap '{it['wrap']}' but plane {plane} joins no plane at that end")
        if s0 < lo or s1 > hi:
            errors.append(f"linear[{lid}]: s {s0}..{s1} outside 0..L = {length:.3f} of plane {plane}")
        if it["axis"] == "w":
            if not it.get("w_span") or not it["w_span"]["v"][1] > it["w_span"]["v"][0]:
                errors.append(f"linear[{lid}]: a bracket (axis w) needs w_span [w0, w1] with w1 > w0")
                continue
            check_src(f"linear[{lid}].w_span", it["w_span"]["src"], it["w_span"]["conf"])
            width = max(p[0] for p in prof["polygon_wv"])
            if abs((s1 - s0) - width) > 0.001:
                errors.append(f"linear[{lid}]: bracket width {s1 - s0:.3f} != profile width {width:.3f}")
            if not isinstance(it["v"]["v"], (int, float)):
                errors.append(f"linear[{lid}]: a bracket needs v = one number (base of the profile)")
            continue
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
        for oid, (r, opl) in rects.items():                          # modeller rule 30: nothing in front of an opening
            if opl == plane and rect_overlap_area(box, r) > 1e-4:
                errors.append(f"linear[{lid}]: lies in front of opening {oid} ({rect_overlap_area(box, r):.4f} m2)")
    return errors


def object_values(ob: dict) -> dict:
    """GDL parameter name -> plain value of an object of the spec."""
    return {k: v["v"] for k, v in ob["params"].items()}


def object_bbox(spec: dict, ob: dict) -> list[float]:
    """[s0, s1, v0, v1, w0, w1] of a GDL object on its plane (expected from the template and the parameters)."""
    tpl = spec["systems"][ob["system"]]["template"]
    plane = ob.get("plane", REFERENCE_PLANE)
    return gdl_expect.facade_bbox(tpl, object_values(ob), ob["origin"]["v"], outside_of(spec, plane))


def _check_objects(spec: dict, openings: list, frames: dict, item_ids: set, check_src, zones: dict) -> list[str]:
    """GDL objects: FS template system, parameters with source, inside 0..L; a surround frames its host opening exactly
    (jambs at the opening edges, head on the opening top) and covers no other opening; an arch gable stands on top of
    its wall zone, in the zone face, within its width and as deep as the wall."""
    errors: list[str] = []
    systems = spec["systems"]
    by_id = {o["id"]: o for o in openings}
    for sid, s in systems.items():
        if s["type"] == "gdl" and s.get("template") not in gdl_expect.TEMPLATES:
            errors.append(f"systems[{sid}]: gdl template {s.get('template')!r} has no expected geometry (gdl_expect.TEMPLATES)")
    for ob in spec.get("objects", []):
        bid = ob["id"]
        if bid in item_ids:
            errors.append(f"objects[{bid}]: duplicate id")
        item_ids.add(bid)
        check_src(f"objects[{bid}].origin", ob["origin"]["src"], ob["origin"]["conf"])
        for k, v in ob["params"].items():
            check_src(f"objects[{bid}].params.{k}", v["src"], v["conf"])
        sysdef = systems.get(ob["system"])
        if sysdef is None or sysdef["type"] != "gdl" or sysdef.get("template") not in gdl_expect.TEMPLATES:
            errors.append(f"objects[{bid}]: system '{ob['system']}' is not a known gdl system")
            continue
        plane = ob.get("plane", REFERENCE_PLANE)
        if plane not in frames:
            errors.append(f"objects[{bid}]: plane '{plane}' does not exist")
            continue
        missing = [k for k in gdl_expect.TEMPLATES[sysdef["template"]] if k not in ob["params"]]
        if missing:
            errors.append(f"objects[{bid}]: parameters {missing} missing for template {sysdef['template']}")
            continue
        bb = object_bbox(spec, ob)
        if bb[0] < -EPS or bb[1] > frames[plane].length + 0.01:
            errors.append(f"objects[{bid}]: s {bb[0]:.3f}..{bb[1]:.3f} outside 0..L of plane {plane}")
        p = object_values(ob)
        os_, ov, ow = ob["origin"]["v"]
        if ob["kind"] == "surround":
            host = by_id.get(ob.get("host", ""))
            if host is None:
                errors.append(f"objects[{bid}]: host opening '{ob.get('host')}' does not exist")
                continue
            if item_plane(host, zones) != plane:
                errors.append(f"objects[{bid}]: host {host['id']} lies on another plane")
            s0, s1, v0, v1 = host["rect"]["v"]
            right = outside_of(spec, plane) == "right_of_ab"
            for what, got, want in (("opening width opW", p["opW"], s1 - s0), ("origin s at the jamb", os_, s1 if right else s0),
                                    ("head on the opening top (origin v + opH)", ov + p["opH"], v1)):
                if abs(got - want) > 0.001:
                    errors.append(f"objects[{bid}]: {what} {got:.4f} != {want:.4f} (host {host['id']})")
            if ov > v0 + EPS:
                errors.append(f"objects[{bid}]: jambs start above the opening bottom ({ov} > {v0})")
            hz = zones.get(host.get("zone")) if host.get("zone") else next(iter(zones.values()), None)
            if hz and abs(ow - zone_w(hz)) > 0.001:
                errors.append(f"objects[{bid}]: origin w {ow} is not on the face of zone {hz['id']} (w {zone_w(hz)})")
            for oid, o in by_id.items():                              # modeller rule 30: nothing in front of another opening
                if oid != host["id"] and item_plane(o, zones) == plane and rect_overlap_area(bb[:4], o["rect"]["v"]) > 1e-4:
                    errors.append(f"objects[{bid}]: lies in front of opening {oid}")
        elif ob["kind"] == "arch_gable":
            z = zones.get(ob.get("zone", ""))
            if z is None:
                errors.append(f"objects[{bid}]: zone '{ob.get('zone')}' does not exist")
                continue
            zs0, zs1, _, zv1 = z["rect"]["v"]
            vs = float(p.get("vSpring", 0.0) or 0.0)            # FS_ArchGable 1.1: springing vSpring above the arc centre
            if vs < 0 or vs >= p["rIn"]:
                errors.append(f"objects[{bid}]: vSpring {vs} must be in [0, rIn {p['rIn']})")
            if abs(ov + vs - zv1) > 0.001 or abs(ow - zone_w(z)) > 0.001:
                errors.append(f"objects[{bid}]: springing ({ov + vs:.4f} = centre {ov} + vSpring {vs}, w {ow}) is not on top of zone {z['id']} ({zv1}, w {zone_w(z)})")
            under = by_id.get(ob.get("over", ""))               # the opening under the arch (lunette above its head)
            if ob.get("over") and under is None:
                errors.append(f"objects[{bid}]: over '{ob.get('over')}' is not an opening")
            elif under is not None:
                us0, us1, _, uv1 = under["rect"]["v"]
                for what, got, want in (("inner chord half width = half width of " + under["id"], gdl_expect.arch_chord(p), 0.5 * (us1 - us0)),
                                        ("centre s = centre of " + under["id"], os_, 0.5 * (us0 + us1)),
                                        ("springing = top of " + under["id"], ov + vs, uv1)):
                    if abs(got - want) > 0.01:
                        errors.append(f"objects[{bid}]: arch over {under['id']} - {what}: {got:.3f} != {want:.3f}")
            if bb[0] < zs0 - 0.001 or bb[1] > zs1 + 0.001:
                errors.append(f"objects[{bid}]: arch {bb[0]:.3f}..{bb[1]:.3f} wider than zone {z['id']} {zs0}..{zs1}")
            if p["rIn"] >= p["rOut"]:
                errors.append(f"objects[{bid}]: rIn {p['rIn']} >= rOut {p['rOut']}")
            if abs(p["depth"] - z["thickness"]["v"]) > 0.001:
                errors.append(f"objects[{bid}]: depth {p['depth']} != thickness of zone {z['id']} {z['thickness']['v']}")
    return errors


CW_DOOR_PARTS = ("CW Door 27", "CW Double Door 27",
                 "Dvoukřídlé dveře LOP 26", "Dvoukřídlé dveře asymetrické LOP 26")   # + SK template (CZ library 26, pilot B)          # door panels of the Archicad 29 library (placed in a CW cell, spike 2026-10-19)
MIN_CELL = 0.15                                                 # m - smallest column / row of a curtain wall field


def cw_as_openings(spec: dict) -> list[dict]:
    """The glazed fields as pseudo-openings (id, zone, rect, kind 'cw') for the rules that look at the holes of a wall."""
    return [{"id": cw["id"], "zone": cw["zone"], "rect": cw["rect"], "kind": "cw", "system": cw["system"]} for cw in spec.get("cw_fields", [])
            if cw.get("zone")]                                   # a freestanding CW (pilot B) is no opening of a wall


def point_in_poly(pt, ring, tol: float = 1e-6) -> bool:
    """Point in a closed polygon [(x, y), ...]; a point on an edge (within tol) counts as inside."""
    x, y = pt
    n = len(ring)
    for i in range(n):
        (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / L2))
        if (x - x0 - t * dx) ** 2 + (y - y0 - t * dy) ** 2 <= tol * tol:
            return True
    inside = False
    for i in range(n):
        (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def balcony_ring(spec: dict, b: dict, n_arc: int = 48) -> list[tuple[float, float]]:
    """Closed outline of a balcony in (s, w) with the arc sampled (point-in-slab tests, drawing)."""
    o = balcony_outline(spec, b)
    pts = list(o["pts"][:-2])
    c, r = o["arc"][2], o["arc"][3]
    s0, s1 = b["s"]["v"]
    ws = b["w_side"]["v"]
    a1, a0 = float(np.arctan2(ws - c[1], s1 - c[0])), float(np.arctan2(ws - c[1], s0 - c[0]))
    pts.append((s1, ws))
    pts += [(float(c[0] + r * np.cos(a1 + (a0 - a1) * k / n_arc)), float(c[1] + r * np.sin(a1 + (a0 - a1) * k / n_arc))) for k in range(1, n_arc)]
    pts.append((s0, ws))
    return pts


def _check_i6(spec: dict, zones: dict, openings: list, frames: dict, item_ids: set, check_src) -> tuple[list[str], list[str]]:
    """I5/I6 rules (pilot A): curtain wall systems and glazed fields (CW in a rectangular hole of its zone wall, raster
    inside the field, cells with known panel kinds, a door panel stands on the foot of the field, the CW fits the wall
    thickness, no overlap with openings / other fields), balcony slabs (on the wall faces, front in front of the sides,
    below the door of their host, not through a linear element) and railings (on their balcony, inside its outline)."""
    errors: list[str] = []
    warnings: list[str] = []
    systems = spec["systems"]
    for sid, s in systems.items():
        t = s["type"]
        if t not in ("curtain_wall", "slab", "railing"):
            continue
        if not (s.get("favorite") or "").startswith("FS "):
            errors.append(f"systems[{sid}]: a {t} system needs an 'FS ' favourite (got {s.get('favorite')!r})")
        if t == "curtain_wall":
            for key in ("frame", "boundary"):
                fr = s.get(key)
                if not fr:
                    errors.append(f"systems[{sid}]: curtain wall without {key}")
                    continue
                for k in ("width", "depth", "offset"):
                    check_src(f"systems[{sid}].{key}.{k}", fr[k]["src"], fr[k]["conf"])
                if not (fr["width"]["v"] > 0 and fr["depth"]["v"] > 0 and 0 <= fr["offset"]["v"] <= fr["depth"]["v"]):
                    errors.append(f"systems[{sid}].{key}: width/depth > 0 and 0 <= offset <= depth")
            if s.get("frame") and s.get("boundary") and (abs(s["frame"]["depth"]["v"] - s["boundary"]["depth"]["v"]) > EPS
                                                         or abs(s["frame"]["offset"]["v"] - s["boundary"]["offset"]["v"]) > EPS):
                errors.append(f"systems[{sid}]: frame and boundary must have the same depth and offset (one panel plane)")
            panels = s.get("panels") or {}
            if "glass" not in panels:
                errors.append(f"systems[{sid}]: curtain wall without a 'glass' panel kind")
            for kind, pn in panels.items():
                if pn.get("libpart"):
                    if pn["libpart"] not in CW_DOOR_PARTS:
                        errors.append(f"systems[{sid}].panels.{kind}: door panel {pn['libpart']!r} not in {CW_DOOR_PARTS}")
                elif "thickness" not in pn:
                    errors.append(f"systems[{sid}].panels.{kind}: a panel needs thickness (or a door libpart)")
                else:
                    check_src(f"systems[{sid}].panels.{kind}.thickness", pn["thickness"]["src"], pn["thickness"]["conf"])
        elif t == "slab":
            if "thickness" not in s or s["thickness"]["v"] <= 0:
                errors.append(f"systems[{sid}]: slab needs thickness > 0")
            else:
                check_src(f"systems[{sid}].thickness", s["thickness"]["src"], s["thickness"]["conf"])
        elif t == "railing":
            if not s.get("base_favorite"):
                errors.append(f"systems[{sid}]: a railing system starts from a base favourite (R1: infill and posts come from it)")
            for k in ("height", "post_spacing"):
                if k not in s or s[k]["v"] <= 0:
                    errors.append(f"systems[{sid}]: railing needs {k} > 0")
                else:
                    check_src(f"systems[{sid}].{k}", s[k]["src"], s[k]["conf"])

    # ---- glazed fields (curtain walls)
    rects = [(o["id"], o["rect"]["v"], item_plane(o, zones)) for o in openings]
    cws = spec.get("cw_fields", [])
    for cw in cws:
        cid = cw["id"]
        if cid in item_ids:
            errors.append(f"cw_fields[{cid}]: duplicate id")
        item_ids.add(cid)
        check_src(f"cw_fields[{cid}].rect", cw["rect"]["src"], cw["rect"]["conf"])
        check_src(f"cw_fields[{cid}].reveal", cw["reveal"]["src"], cw["reveal"]["conf"])
        for k in ("u_axes", "v_axes"):
            if k in cw:
                check_src(f"cw_fields[{cid}].{k}", cw[k]["src"], cw[k]["conf"])
        s0, s1, v0, v1 = cw["rect"]["v"]
        sysdef = systems.get(cw["system"])
        if sysdef is None or sysdef["type"] != "curtain_wall":
            errors.append(f"cw_fields[{cid}]: system '{cw['system']}' is not a curtain wall system")
            continue
        if not cw.get("zone"):                                   # pilot B: freestanding curtain wall on its plane
            _check_cw_free(spec, cw, sysdef, frames, cws, errors, warnings)
            continue
        z = zones.get(cw["zone"])
        if z is None:
            errors.append(f"cw_fields[{cid}]: zone '{cw['zone']}' does not exist")
            continue
        zs0, zs1, zv0, zv1 = z["rect"]["v"]
        if not (s1 > s0 and v1 > v0) or s0 < zs0 - EPS or s1 > zs1 + EPS or v0 < zv0 - EPS or v1 > zv1 + EPS:
            errors.append(f"cw_fields[{cid}]: rect {cw['rect']['v']} empty or not inside wall zone {z['id']} {z['rect']['v']}")
            continue
        for key, lo, hi in (("u_axes", s0, s1), ("v_axes", v0, v1)):
            ax = [lo] + list(cw.get(key, {}).get("v", [])) + [hi]
            if any(b_ - a_ < MIN_CELL - EPS for a_, b_ in zip(ax[:-1], ax[1:])):
                errors.append(f"cw_fields[{cid}].{key}: axes {ax[1:-1]} not ascending inside {lo}..{hi} with cells >= {MIN_CELL} m")
        cols, rows = cw_grid(cw)
        panels = sysdef.get("panels") or {}
        doors = []
        seen_cells = set()
        for cell in cw.get("cells", []):
            key = (cell["col"], cell["row"])
            if key in seen_cells:
                errors.append(f"cw_fields[{cid}]: cell {key} given twice")
            seen_cells.add(key)
            if not (0 <= cell["col"] < len(cols) and 0 <= cell["row"] < len(rows)):
                errors.append(f"cw_fields[{cid}]: cell {cell['col']}/{cell['row']} outside the raster {len(cols)} x {len(rows)}")
            elif cell["panel"] not in panels:
                errors.append(f"cw_fields[{cid}]: cell {cell['col']}/{cell['row']} panel '{cell['panel']}' is not a panel kind of {cw['system']}")
            elif panels[cell["panel"]].get("libpart"):
                doors.append(cell)
        for cell in doors:                                          # a door stands on the foot of the field
            if cell["row"] != 0:
                errors.append(f"cw_fields[{cid}]: door panel in row {cell['row']} - a door must be in the bottom row (on the foot of the field)")
        if cw["rect"]["conf"] in LOW_CONF and v0 <= EPS and not doors:
            errors.append(f"cw_fields[{cid}]: glazed field with conf {cw['rect']['conf']} has sill 0 (rule 13)")
        depth = max(sysdef["frame"]["depth"]["v"], sysdef["boundary"]["depth"]["v"]) if sysdef.get("frame") and sysdef.get("boundary") else 0.0
        if cw["reveal"]["v"] < -EPS or cw["reveal"]["v"] + depth > z["thickness"]["v"] + EPS:
            errors.append(f"cw_fields[{cid}]: reveal {cw['reveal']['v']} + frame depth {depth} does not fit the wall {z['id']} ({z['thickness']['v']} m)")
        plane = z.get("plane", REFERENCE_PLANE)
        for oid, r, opl in rects:
            if opl == plane and rect_overlap_area(cw["rect"]["v"], r) > EPS:
                errors.append(f"cw_fields[{cid}]: overlaps opening {oid}")
            elif opl == plane and rects_touch(cw["rect"]["v"], r):
                warnings.append(f"cw_fields[{cid}] and opening {oid} touch - one hole of the wall")
        for oid in cw.get("replaces", []):
            if oid != cid and any(o["id"] == oid for o in openings):
                errors.append(f"cw_fields[{cid}]: replaces opening {oid}, which is still in openings")
        for c2 in cws:
            if c2["id"] == cid or not c2.get("zone") or zones.get(c2["zone"], {}).get("plane", REFERENCE_PLANE) != plane:
                continue
            if rect_overlap_area(cw["rect"]["v"], c2["rect"]["v"]) > EPS:
                errors.append(f"cw_fields[{cid}]: overlaps glazed field {c2['id']}")
            elif rects_touch(cw["rect"]["v"], c2["rect"]["v"]) and cid < c2["id"]:
                warnings.append(f"cw_fields {cid} and {c2['id']} touch - one hole of the wall, two curtain walls (two boundary frames at the shared jamb)")

    # ---- balconies
    lin_boxes = []
    for it in spec.get("linear", []):
        sd = systems.get(it["system"], {})
        prof = systems.get(sd.get("profile", ""), {})
        if prof.get("polygon_wv") and sd.get("tool") in ("beam", "column"):
            lin_boxes.append((it["id"], it.get("plane", REFERENCE_PLANE), linear_box(it, prof)))
    by_open = {o["id"]: o for o in openings}
    bals = {}
    for b in spec.get("balconies", []):
        bid = b["id"]
        if bid in item_ids:
            errors.append(f"balconies[{bid}]: duplicate id")
        item_ids.add(bid)
        bals[bid] = b
        for k in ("s", "w_side", "w_crown", "v_top"):
            check_src(f"balconies[{bid}].{k}", b[k]["src"], b[k]["conf"])
        sysdef = systems.get(b["system"])
        if sysdef is None or sysdef["type"] != "slab":
            errors.append(f"balconies[{bid}]: system '{b['system']}' is not a slab system")
            continue
        plane = b.get("plane", REFERENCE_PLANE)
        if plane not in frames:
            errors.append(f"balconies[{bid}]: plane '{plane}' does not exist")
            continue
        s0, s1 = b["s"]["v"]
        if not s1 > s0 or s0 < -EPS or s1 > frames[plane].length + 0.01:
            errors.append(f"balconies[{bid}]: s {s0}..{s1} empty or outside 0..L of plane {plane}")
            continue
        try:
            o = balcony_outline(spec, b)
        except ValueError as exc:
            errors.append(f"balconies[{bid}]: {exc}")
            continue
        back_w = max(w for _, w in o["pts"][:-2])
        if not (b["w_crown"]["v"] > b["w_side"]["v"] > back_w + 0.05):
            errors.append(f"balconies[{bid}]: needs w_crown > w_side > wall face + 0.05 (got {b['w_crown']['v']}, {b['w_side']['v']}, face {back_w})")
        th = sysdef.get("thickness", {}).get("v", 0.0)
        vb, vt = b["v_top"]["v"] - th, b["v_top"]["v"]
        if vb < -EPS:
            errors.append(f"balconies[{bid}]: slab below the facade foot")
        host = by_open.get(b.get("host", "")) if b.get("host") else None
        if b.get("host") and host is None:
            errors.append(f"balconies[{bid}]: host opening '{b['host']}' does not exist")
        elif host is not None:
            hs0, hs1, hv0, _ = host["rect"]["v"]
            if hs0 < s0 - EPS or hs1 > s1 + EPS:
                errors.append(f"balconies[{bid}]: host {host['id']} ({hs0}..{hs1}) is not in front of the balcony {s0}..{s1}")
            if vt > hv0 + 0.001:
                errors.append(f"balconies[{bid}]: top {vt} above the sill of its door {host['id']} ({hv0})")
            elif hv0 - vt > 0.25:
                warnings.append(f"balconies[{bid}]: step {hv0 - vt:.2f} m from the slab top {vt} to the sill of its door {host['id']} ({hv0})")
        for lid, lpl, box in lin_boxes:                       # nothing passes through the slab
            if lpl == plane and rect_overlap_area([s0, s1, vb, vt], box) > 1e-4:
                errors.append(f"balconies[{bid}]: slab passes through linear element {lid}")

    # ---- railings
    for r in spec.get("railings", []):
        rid = r["id"]
        if rid in item_ids:
            errors.append(f"railings[{rid}]: duplicate id")
        item_ids.add(rid)
        check_src(f"railings[{rid}].path_sw", r["path_sw"]["src"], r["path_sw"]["conf"])
        check_src(f"railings[{rid}].v", r["v"]["src"], r["v"]["conf"])
        sysdef = systems.get(r["system"])
        if sysdef is None or sysdef["type"] != "railing":
            errors.append(f"railings[{rid}]: system '{r['system']}' is not a railing system")
        pts = r["path_sw"]["v"]
        if any(np.hypot(q[0] - p[0], q[1] - p[1]) < 0.05 for p, q in zip(pts[:-1], pts[1:])):
            errors.append(f"railings[{rid}]: path has a segment shorter than 5 cm")
        b = bals.get(r.get("on", "")) if r.get("on") else None
        if r.get("on") and b is None:
            sl = next((x for x in spec.get("slabs", []) if x["id"] == r["on"]), None)
            if sl is None:
                errors.append(f"railings[{rid}]: balcony / slab '{r['on']}' does not exist")
            else:                                             # pilot B: on a walkway slab
                if abs(r["v"]["v"] - sl["v_top"]["v"]) > 0.001:
                    errors.append(f"railings[{rid}]: base v {r['v']['v']} != top of slab {sl['id']} ({sl['v_top']['v']})")
                out = [q for q in pts if not point_in_poly(q, slab_ring(sl), 0.002)]
                if out:
                    errors.append(f"railings[{rid}]: path points {out[:3]} outside slab {sl['id']}")
        elif b is not None:                                   # the railing stands on its slab (modeller: nothing hangs)
            if abs(r["v"]["v"] - b["v_top"]["v"]) > 0.001:
                errors.append(f"railings[{rid}]: base v {r['v']['v']} != top of balcony {b['id']} ({b['v_top']['v']})")
            if r.get("plane", REFERENCE_PLANE) != b.get("plane", REFERENCE_PLANE):
                errors.append(f"railings[{rid}]: not on the plane of balcony {b['id']}")
            try:
                ring = balcony_ring(spec, b)
                out = [p for p in pts if not point_in_poly(p, ring, 0.002)]
                if out:
                    errors.append(f"railings[{rid}]: path points {out[:3]} outside the slab of balcony {b['id']}")
            except ValueError:
                pass
        else:
            warnings.append(f"railings[{rid}]: stands on no balcony of the spec (base v {r['v']['v']})")
    return errors, warnings


def _check_cw_free(spec: dict, cw: dict, sysdef: dict, frames: dict, cws: list, errors: list, warnings: list) -> None:
    """Pilot B: a curtain wall that is not in a wall hole - inside 0..L of its plane, raster, cells, door on the foot,
    reveal >= 0 (outer face of the frames behind the plane), no overlap with another freestanding CW at the same depth."""
    cid = cw["id"]
    s0, s1, v0, v1 = cw["rect"]["v"]
    plane = cw.get("plane", REFERENCE_PLANE)
    if plane not in frames:
        errors.append(f"cw_fields[{cid}]: plane '{plane}' does not exist")
        return
    if not (s1 > s0 and v1 > v0) or s0 < -EPS or s1 > frames[plane].length + 0.01:
        errors.append(f"cw_fields[{cid}]: rect {cw['rect']['v']} empty or outside 0..L of plane {plane}")
        return
    for key, lo, hi in (("u_axes", s0, s1), ("v_axes", v0, v1)):
        ax = [lo] + list(cw.get(key, {}).get("v", [])) + [hi]
        if any(b_ - a_ < MIN_CELL - EPS for a_, b_ in zip(ax[:-1], ax[1:])):
            errors.append(f"cw_fields[{cid}].{key}: axes {ax[1:-1]} not ascending inside {lo}..{hi} with cells >= {MIN_CELL} m")
    cols, rows = cw_grid(cw)
    panels = sysdef.get("panels") or {}
    for cell in cw.get("cells", []):
        if not (0 <= cell["col"] < len(cols) and 0 <= cell["row"] < len(rows)):
            errors.append(f"cw_fields[{cid}]: cell {cell['col']}/{cell['row']} outside the raster {len(cols)} x {len(rows)}")
        elif cell["panel"] not in panels:
            errors.append(f"cw_fields[{cid}]: panel '{cell['panel']}' is not a panel kind of {cw['system']}")
        elif panels[cell["panel"]].get("libpart") and cell["row"] != 0:
            errors.append(f"cw_fields[{cid}]: door panel in row {cell['row']} - a door must be in the bottom row")
    if cw["reveal"]["v"] < -EPS:
        errors.append(f"cw_fields[{cid}]: a freestanding CW needs reveal >= 0 (frames behind the plane)")
    if cw.get("story") and cw["story"] not in {st["id"] for st in spec["stories"]}:
        errors.append(f"cw_fields[{cid}]: story '{cw['story']}' is not in stories")
    for c2 in cws:
        if c2["id"] <= cid or c2.get("zone") or c2.get("plane", REFERENCE_PLANE) != plane or abs(c2["reveal"]["v"] - cw["reveal"]["v"]) > 0.05:
            continue
        if rect_overlap_area(cw["rect"]["v"], c2["rect"]["v"]) > 1e-4:
            errors.append(f"cw_fields[{cid}]: overlaps curtain wall {c2['id']} (same depth)")


def slab_ring(sl: dict) -> list:
    return [tuple(q) for q in sl["outline_sw"]["v"]]


def _check_pilot_b(spec: dict, frames: dict, item_ids: set, check_src) -> tuple[list[str], list[str]]:
    """Pilot B: per-element stories (all stories carry floorIndex or none), slabs given by an outline (valid polygon,
    slab system, inside 0..L), louvres (FS_Louvers) in front of a freestanding curtain wall field, not behind its face."""
    errors: list[str] = []
    warnings: list[str] = []
    st = spec["stories"]
    with_idx = [s for s in st if "floorIndex" in s]
    if with_idx and len(with_idx) != len(st):
        errors.append("stories: floorIndex on some stories only")
    lv = [s["level"]["v"] for s in st]
    if with_idx and lv != sorted(lv):
        errors.append("stories: levels must ascend")
    story_ids = {s["id"] for s in st}
    for sl in spec.get("slabs", []):
        sid = sl["id"]
        if sid in item_ids:
            errors.append(f"slabs[{sid}]: duplicate id")
        item_ids.add(sid)
        check_src(f"slabs[{sid}].outline_sw", sl["outline_sw"]["src"], sl["outline_sw"]["conf"])
        check_src(f"slabs[{sid}].v_top", sl["v_top"]["src"], sl["v_top"]["conf"])
        sysdef = spec["systems"].get(sl["system"])
        if sysdef is None or sysdef["type"] != "slab":
            errors.append(f"slabs[{sid}]: system '{sl['system']}' is not a slab system")
        ring = slab_ring(sl)
        if len(ring) < 3 or polygon_area([list(q) for q in ring]) < 1e-4:
            errors.append(f"slabs[{sid}]: outline empty")
            continue
        try:
            from shapely.geometry import Polygon
            if not Polygon(ring).is_valid:
                errors.append(f"slabs[{sid}]: outline self-intersects")
        except ImportError:
            pass
        plane = sl.get("plane", REFERENCE_PLANE)
        if plane not in frames:
            errors.append(f"slabs[{sid}]: plane '{plane}' does not exist")
        elif min(q[0] for q in ring) < -EPS or max(q[0] for q in ring) > frames[plane].length + 0.01:
            errors.append(f"slabs[{sid}]: outline outside 0..L of plane {plane}")
        if sl.get("story") and sl["story"] not in story_ids:
            errors.append(f"slabs[{sid}]: story '{sl['story']}' is not in stories")
    free = [cw for cw in spec.get("cw_fields", []) if not cw.get("zone")]
    for ob in spec.get("objects", []):
        if ob["kind"] != "louvers":
            continue
        bb = object_bbox(spec, ob)
        hosts = [cw for cw in free if cw["rect"]["v"][0] - 0.01 <= bb[0] and bb[1] <= cw["rect"]["v"][1] + 0.01
                 and cw["rect"]["v"][2] - 0.01 <= bb[2] and bb[3] <= cw["rect"]["v"][3] + 0.01]
        if not hosts:
            errors.append(f"objects[{ob['id']}]: louvres {[round(x, 3) for x in bb[:4]]} in front of no curtain wall field")
        elif any(bb[4] < -h["reveal"]["v"] - EPS for h in hosts):
            errors.append(f"objects[{ob['id']}]: louvres reach behind the face of their curtain wall (w {bb[4]:.3f})")
    for it in spec.get("linear", []):
        if it.get("story") and it["story"] not in story_ids:
            errors.append(f"linear[{it['id']}]: story '{it['story']}' is not in stories")
    return errors, warnings


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

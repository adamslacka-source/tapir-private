"""Validator of facade-spec/1 (I1 subset): JSON Schema + consistency rules (ADR-001 ch. 4).

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
LOW_CONF = ("odhad", "default")
EPS = 1e-6


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
        if "reveal" in o:
            check_src(f"openings[{oid}].reveal", o["reveal"]["src"], o["reveal"]["conf"])
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
        elif o["kind"] in ("window", "door") and sysdef["type"] != o["kind"]:
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
                warnings.append(f"openings {o1['id']} and {o2['id']} touch (frames in contact, modeller rule 30) - "
                                "probably one unit in reality; keep as two until a combined system exists (I2)")

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

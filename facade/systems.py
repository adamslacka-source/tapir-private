"""systems: facade-spec/1 catalogue `systems` -> Archicad attributes, FS Favorites and FS_* properties (ADR-001 D3/D4, I2).

What it makes (idempotently - a second run changes nothing):
  * Composite attributes of wall systems (CreateComposites; compared with GetComposites first)
  * the property group 'FS' with FS_zdroj / FS_istota / FS_spec (CreatePropertyGroups / CreatePropertyDefinitions)
  * Favorites 'FS <system id>' through a sample element (D4): a temporary host wall far from the model, the sample
    placed from the base favourite, GDL system parameters written and READ BACK, CreateFavoritesFromElements
    (UpdateFavoritesFromElements when the content changed), then sample and host are deleted again.
  * I5/I6 (pilot A): curtain wall systems -> frame / panel classes in the Curtain Wall tool defaults
    (SetCurtainWallSystemDefaults, Tapir T2) saved as the favourite 'FS cw.*', the defaults ALWAYS restored
    (restoreDefaults); the class indices CreateCurtainWalls cells refer to are kept in the manifest. Slab and railing
    favourites 'FS slab.*' / 'FS rail.*' from a sample (railing: base favourite + height + post spacing; the infill comes
    from the base favourite - R1). A sample landing on a hidden / locked layer is moved to the facade layer first
    (otherwise Archicad refuses to change or delete it).
  * I3: a sample door/window of a library part WITHOUT a favourite in the template is placed by libraryPartName
    (base_libpart, Tapir T7); Profile attributes 'FS prof.*' from the cross-section polygons of profile systems
    (CreateProfilesFromPolygons, Tapir T1; area read back from GetProfiles); favourites 'FS lin.*' of linear systems
    from a sample beam / column carrying the profile.

The content of a favourite cannot be read through the API, so idempotence rests on a manifest
(<out>/_systems/manifest.json): favourite name -> hash of everything that went into it. A favourite that exists
but whose hash is unknown or different is updated from a fresh sample.

GDL parameter names come only from gdl_param_map.json (found on placed samples, nothing guessed). A spec value that
the library part cannot carry ('unsupported' in the map) is collected as an EXCEPTION and reported - never passed silently.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from spec_validate import TYPE_KEYS, fs_system, load_gdl_map, polygon_area
from tapir_client import TapirClient, TapirError, results_ok

FS_GROUP = "FS"
FS_PROPS = {
    "FS_zdroj": "Zdroj hodnôt prvku fasády: poloha/rozmer, typ, hĺbka - id zdroja zo spec (facade-spec/1) a istota.",
    "FS_istota": "Najnižšia istota hodnôt, ktoré prvok tvarujú: kota > meranie > pravidlo > odhad > default.",
    "FS_spec": "Spec a položka, z ktorej prvok vznikol (<spec.id>.<id>).",
}
FAV_TYPES = {"wall": "Wall", "window": "Window", "door": "Door", "beam": "Beam", "column": "Column",
             "curtain_wall": "CurtainWall", "slab": "Slab", "railing": "Railing"}
AREA_TOL = 0.01                         # relative - profile area read back vs polygon (ADR I3 acceptance)
SAMPLE_OFFSET = (1000.0, 0.0)          # m - temporary host wall far away from every pilot facade
NUM_TOL = 1e-4
NULL_GUID = "00000000-0000-0000-0000-000000000000"
# classification of an element the favourite leaves unclassified (FS_* availability is per classification item);
# item IDs of the Archicad classification in the SK and the CZ template
KIND_CLASS = {"wall": ("Stena", "Zeď"), "beam": ("Nosník",), "column": ("Stĺp", "Sloup"), "slab": ("Doska", "Deska"),
              "curtainwall": ("Ľahký obvodový plášť", "Lehký obvodový plášť"), "railing": ("Zábradlie", "Zábradlí"),
              "object": ("Tieniaci prvok", "Stínící prvek")}


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def manifest_path(out_root: Path) -> Path:
    return Path(out_root) / "_systems" / "manifest.json"


def load_manifest(out_root: Path, project: str) -> dict:
    p = manifest_path(out_root)
    if p.exists():
        m = json.loads(p.read_text(encoding="utf-8"))
        if m.get("project", "").lower() != project.lower():
            raise TapirError(f"{p}: manifest belongs to '{m.get('project')}', not '{project}'")
        return m
    return {"version": 1, "project": project, "favorites": {}, "prop_availability": [], "scratch": []}


def save_manifest(out_root: Path, m: dict) -> None:
    p = manifest_path(out_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# ---- attributes ----------------------------------------------------------------------------------
class Attrs:
    """Name lookups of the attributes the systems refer to (read-only)."""

    def __init__(self, ac: TapirClient):
        get = lambda t: ac.tapir("GetAttributesByType", {"attributeType": t})["attributes"]
        self.bmat = {a["name"]: a for a in get("BuildingMaterial")}
        self.surface = {a["name"]: a for a in get("Surface")}
        self.surface_by_index = {a["index"]: a["name"] for a in self.surface.values()}
        self.composite = {a["name"]: a for a in get("Composite")}
        self.profile = {a["name"]: a for a in get("Profile")}
        self.line = {a["index"]: a for a in get("Line")}

    def surface_index(self, name: str) -> int:
        if name not in self.surface:
            raise TapirError(f"surface '{name}' does not exist in the project")
        return self.surface[name]["index"]

    def bmat_guid(self, name: str) -> str:
        if name not in self.bmat:
            raise TapirError(f"building material '{name}' does not exist in the project")
        return self.bmat[name]["attributeId"]["guid"]


# ---- GDL map evaluation (shared with build / verify) ---------------------------------------------------
def _key(v) -> str:
    return str(v).lower() if isinstance(v, bool) else str(v)


def write_params(rule_write: dict, value, attrs: Attrs | None, params: dict | None = None) -> list[dict]:
    out = []
    for p, expr in rule_write.items():
        if isinstance(expr, dict):
            if _key(value) not in expr:
                raise TapirError(f"gdl_param_map: no value of {p} for spec value {value!r}")
            pv = expr[_key(value)]
        elif expr == "$v":
            pv = value
        elif expr == "$surface":
            pv = attrs.surface_index(value)
        elif expr.startswith("$v+param:"):
            if params is None or expr[9:] not in params:
                raise TapirError(f"gdl_param_map: {expr} needs the current value of {expr[9:]}")
            pv = round(value + params[expr[9:]], 6)
        else:
            raise TapirError(f"gdl_param_map: unknown write expression {expr!r}")
        out.append({"name": p, "value": pv})
    return out


def read_field(rule: dict, params: dict, attrs: Attrs | None = None):
    if "const" in rule:
        return rule["const"]
    if "count_nonempty" in rule:
        return sum(1 for p in rule["count_nonempty"] if params.get(p) not in ("", None))
    if rule["param"] not in params:
        raise TapirError(f"GDL parameter {rule['param']} not on the element")
    v = params[rule["param"]]
    if "minus_param" in rule:
        v = round(v - params[rule["minus_param"]], 6)
    if "gt" in rule:
        v = v > rule["gt"]
    if "map" in rule:
        v = rule["map"].get(_key(v), f"<unmapped {v}>")
    if rule.get("surface"):
        v = attrs.surface_by_index.get(v, f"<surface {v}>") if attrs else v
    return v


def same(a, b, tol=NUM_TOL) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    return a == b


def libpart_map(sysdef: dict) -> dict:
    gm = load_gdl_map()
    lp = gm["libparts"][sysdef["libpart"]]
    return {"lp": lp, "common": gm["common"][lp["kind"]]}


def favorite_content(sysdef: dict, attrs: Attrs) -> tuple[list[dict], list[dict], list[dict]]:
    """(GDL writes, checks of fixed fields, exceptions) of a window/door system."""
    m = libpart_map(sysdef)
    lp = m["lp"]
    writes = [{"name": k, "value": v} for k, v in {**m["common"].get("system_params", {}), **lp.get("system_params", {})}.items()]
    checks, exceptions = [], []
    for field in TYPE_KEYS:
        if field not in sysdef:
            continue
        want = sysdef[field]["v"]
        rule = lp["fields"].get(field)
        if rule is None:
            raise TapirError(f"{sysdef['libpart']}: field '{field}' is not mapped in gdl_param_map.json")
        if "write" in rule:
            writes += write_params(rule["write"], want, attrs)
        checks.append({"field": field, "want": want, "read": rule["read"], "unsupported": rule.get("unsupported")})
    return writes, checks, exceptions


# ---- composites ------------------------------------------------------------------------------------
def _composite_payload(comp: dict, attrs: Attrs) -> dict:
    solid = attrs.line[1]["attributeId"]                          # line type 1 = solid line
    return {"name": comp["name"], "useWith": ["Wall"],
            "skins": [{"type": sk["type"], "buildingMaterialId": {"attributeId": {"guid": attrs.bmat_guid(sk["material"])}},
                       "framePen": 1, "thickness": sk["thickness"]["v"]} for sk in comp["skins"]],
            "separators": [{"lineTypeId": {"attributeId": solid}, "linePen": 1} for _ in range(len(comp["skins"]) + 1)]}


def composite_differences(ac: TapirClient, comp: dict, attrs: Attrs) -> list[str]:
    """What differs between the Composite attribute in Archicad and the spec ([] = equal)."""
    have = attrs.composite.get(comp["name"])
    if have is None:
        return ["missing"]
    got = ac.tapir("GetComposites", {"attributeIds": [{"attributeId": have["attributeId"]}]})["composites"][0]
    if "skins" not in got:
        return [f"GetComposites: {got}"]
    diffs = []
    if len(got["skins"]) != len(comp["skins"]):
        return [f"{len(got['skins'])} skins != {len(comp['skins'])}"]
    for i, (g, w) in enumerate(zip(got["skins"], comp["skins"])):
        if g["type"] != w["type"]:
            diffs.append(f"skin {i}: type {g['type']} != {w['type']}")
        if g.get("buildingMaterialId", {}).get("attributeId", {}).get("guid") != attrs.bmat_guid(w["material"]):
            diffs.append(f"skin {i}: building material != {w['material']}")
        if abs(g["thickness"] - w["thickness"]["v"]) > 1e-5:
            diffs.append(f"skin {i}: thickness {g['thickness']} != {w['thickness']['v']}")
    return diffs


def ensure_composites(ac: TapirClient, spec: dict, attrs: Attrs, log: list) -> tuple[int, dict]:
    changes, guids = 0, {}
    for sid, s in spec["systems"].items():
        if "composite" not in s:
            continue
        comp = s["composite"]
        diffs = composite_differences(ac, comp, attrs)
        if diffs:
            res = ac.tapir("CreateComposites", {"compositeDataArray": [_composite_payload(comp, attrs)], "overwriteExisting": True})
            ids = res.get("attributeIds") or []
            if not ids or "attributeId" not in ids[0]:
                raise TapirError(f"CreateComposites {comp['name']}: {res}")
            changes += 1
            log.append(f"composite {'created' if diffs == ['missing'] else 'updated'}: {comp['name']} ({'; '.join(diffs)})")
            attrs.__init__(ac)                                     # refresh lookups
            left = composite_differences(ac, comp, attrs)
            if left:
                raise TapirError(f"composite {comp['name']} differs after CreateComposites: {left}")
        guids[sid] = attrs.composite[comp["name"]]["attributeId"]["guid"]
    return changes, guids


# ---- profiles (I3, Tapir T1) --------------------------------------------------------------------------
def _ring_area(coords: list[dict]) -> float:
    a = 0.0
    for p, q in zip(coords, coords[1:]):
        a += p["x"] * q["y"] - q["x"] * p["y"]
    return abs(a) / 2.0


def profile_area(details: dict) -> float:
    """Cross-section area of a Profile from GetProfiles: per skin outer contour minus holes (slot 0 of
    outlineSubPolyEnds is the anchor, contours are closed by a repeated vertex)."""
    total = 0.0
    for skin in details.get("skins", []):
        coords, ends = skin.get("outlineCoords", []), skin.get("outlineSubPolyEnds", [])
        rings = [coords[ends[k - 1] + 1: ends[k] + 1] for k in range(1, len(ends))]
        areas = [_ring_area(r) for r in rings if len(r) >= 4]
        if areas:
            total += areas[0] - sum(areas[1:])
    return total


def profile_systems(spec: dict) -> dict:
    return {sid: s for sid, s in spec["systems"].items() if s["type"] == "profile"}


def _profile_payload(s: dict) -> dict:
    return {"name": s["name"], "polygons": [{"outer": {"polygonCoordinates": [{"x": x, "y": y} for x, y in s["polygon_wv"]]}}],
            "buildingMaterialName": s["material"], **({"edgeSurfaceName": s["edge_surface"]} if s.get("edge_surface") else {}),
            "useWith": {"wall": True, "beam": True, "column": True}, "overwriteExisting": True}


def profile_details(ac: TapirClient, guids: list[str]) -> list[dict]:
    if not guids:
        return []
    return ac.tapir("GetProfiles", {"attributeIds": [{"attributeId": {"guid": g}} for g in guids]})["profiles"]


def ensure_profiles(ac: TapirClient, spec: dict, attrs: Attrs, manifest: dict, log: list, write: bool = True) -> tuple[int, dict]:
    """sid -> {guid, name, hash, area_spec, area_model}. Rebuilt (overwrite, same guid) when the hash of the polygon /
    material / surface is unknown or different, or the area read back differs from the polygon by more than 1 %."""
    profs = profile_systems(spec)
    out, changes = {}, 0
    reg = manifest.setdefault("profiles", {})
    have = {sid: attrs.profile.get(s["name"]) for sid, s in profs.items()}
    det = dict(zip([sid for sid in profs if have[sid]],
                   profile_details(ac, [have[sid]["attributeId"]["guid"] for sid in profs if have[sid]])))
    for sid, s in profs.items():
        h = _hash(_profile_payload(s))
        area_spec = polygon_area(s["polygon_wv"])
        ok = have[sid] is not None and reg.get(s["name"], {}).get("hash") == h and \
            abs(profile_area(det.get(sid, {})) - area_spec) <= AREA_TOL * area_spec
        if not ok:
            if not write:
                raise TapirError(f"profile {s['name']} missing or different - run build first")
            res = ac.tapir("CreateProfilesFromPolygons", {"profiles": [_profile_payload(s)]})["attributes"][0]
            if "attributeId" not in res:
                raise TapirError(f"CreateProfilesFromPolygons {s['name']}: {res}")
            changes += 1
            log.append(f"profile {'created' if res.get('created') else 'overwritten'}: {s['name']} (index {res['index']})")
            reg[s["name"]] = {"hash": h, "guid": res["attributeId"]["guid"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            guid = res["attributeId"]["guid"]
            det[sid] = profile_details(ac, [guid])[0]
        else:
            guid = have[sid]["attributeId"]["guid"]
        area_model = profile_area(det[sid])
        if abs(area_model - area_spec) > AREA_TOL * area_spec:
            raise TapirError(f"profile {s['name']}: area read back {area_model:.6f} m2 != polygon {area_spec:.6f} m2")
        out[sid] = {"guid": guid, "name": s["name"], "hash": h, "area_spec": area_spec, "area_model": area_model,
                    "width": det[sid].get("width"), "height": det[sid].get("height")}
    if changes:
        attrs.__init__(ac)
    return changes, out


# ---- properties -------------------------------------------------------------------------------------
def property_ids(ac: TapirClient) -> tuple[str | None, dict]:
    allp = ac.tapir("GetAllProperties", timeout=60)
    group = next((g["propertyGroupId"]["guid"] for g in allp.get("propertyGroups", []) if g["name"] == FS_GROUP and g.get("isCustom")), None)
    ids = {p["propertyName"]: p["propertyId"]["guid"] for p in allp["properties"]
           if p.get("propertyType") == "Custom" and p.get("propertyGroupName") == FS_GROUP and p["propertyName"] in FS_PROPS}
    return group, ids


def ensure_properties(ac: TapirClient, log: list) -> tuple[int, dict]:
    changes = 0
    group, ids = property_ids(ac)
    if group is None:
        res = ac.tapir("CreatePropertyGroups", {"propertyGroups": [{"propertyGroup": {
            "name": FS_GROUP, "description": "Fasáda z obrázka (ADR-001): zdroj a istota hodnôt prvku"}}]})
        group = res["propertyGroupIds"][0]["propertyGroupId"]["guid"]
        changes += 1
        log.append(f"property group created: {FS_GROUP}")
    missing = [n for n in FS_PROPS if n not in ids]
    if missing:
        res = ac.tapir("CreatePropertyDefinitions", {"propertyDefinitions": [{"propertyDefinition": {
            "name": n, "description": FS_PROPS[n], "type": "string", "isEditable": True, "availability": [],
            "defaultValue": {"basicDefaultValue": {"type": "string", "status": "userUndefined"}},   # without it: APIERR_BADVALUE
            "group": {"propertyGroupId": {"guid": group}}}} for n in missing]})
        for n, r in zip(missing, res["propertyIds"]):
            if "propertyId" not in r:
                raise TapirError(f"CreatePropertyDefinitions {n}: {r}")
            ids[n] = r["propertyId"]["guid"]
        changes += len(missing)
        log.append(f"property definitions created: {missing}")
    return changes, ids


def ensure_availability(ac: TapirClient, prop_ids: dict, element_guids: list[str], manifest: dict, log: list) -> int:
    """FS_* must be available for the classification items of the facade elements (otherwise values cannot be set)."""
    if not element_guids:
        return 0
    systems = ac.api("API.GetAllClassificationSystems")["classificationSystems"]
    res = ac.tapir("GetClassificationsOfElements", {"elements": [{"elementId": {"guid": g}} for g in element_guids],
                                                     "classificationSystemIds": [{"classificationSystemId": s["classificationSystemId"]} for s in systems]})
    items = set()
    for ec in res["elementClassifications"]:
        for c in ec.get("classificationIds", []):
            if c.get("classificationItemId") and c["classificationItemId"]["guid"] != NULL_GUID:   # unclassified = zero guid
                items.add(c["classificationItemId"]["guid"])
    new = sorted(items - set(manifest.get("prop_availability", [])))
    if not new:
        return 0
    res = ac.tapir("UpdatePropertyDefinitions", {"propertyDefinitions": [
        {"propertyId": {"guid": g}, "availability": {"add": [{"classificationItemId": {"guid": i}} for i in new]}} for g in prop_ids.values()]})
    if errs := results_ok(res["executionResults"]):
        raise TapirError(f"UpdatePropertyDefinitions (availability): {errs}")
    manifest["prop_availability"] = sorted(set(manifest.get("prop_availability", [])) | set(new))
    log.append(f"FS_* made available for {len(new)} classification item(s)")
    return 1


# ---- favourites -----------------------------------------------------------------------------------
def favorite_plans(spec: dict, attrs: Attrs, composites: dict, profiles: dict | None = None) -> dict:
    """system id -> what the FS favourite must contain (+ hash). Pure apart from the attribute lookups."""
    plans = {}
    profiles = profiles or {}
    for sid, s in spec["systems"].items():
        if not fs_system(s):
            continue
        if s["type"] == "linear":
            prof = profiles.get(s["profile"])
            if prof is None:
                raise TapirError(f"system {sid}: profile {s['profile']} not prepared")
            content = {"kind": s["tool"], "profile": prof["name"], "profile_hash": prof["hash"]}
            plans[sid] = {"name": s["favorite"], "kind": s["tool"], "content": content, "profile_guid": prof["guid"]}
        elif s["type"] == "curtain_wall":
            content = cw_content(sid, s, spec["reference"]["layer"])     # the CW favourite carries the facade layer
            plans[sid] = {"name": s["favorite"], "kind": "curtain_wall", "content": content}
        elif s["type"] == "slab":
            content = {"kind": "slab", "structure": s.get("structure", "Basic"), "material": s.get("material"), "thickness": s["thickness"]["v"]}
            plans[sid] = {"name": s["favorite"], "kind": "slab", "content": content}
        elif s["type"] == "railing":
            content = {"kind": "railing", "base": s["base_favorite"], "height": s["height"]["v"], "post_spacing": s["post_spacing"]["v"],
                       "post_distribution": s.get("post_distribution", "BestDivision")}
            plans[sid] = {"name": s["favorite"], "kind": "railing", "content": content}
        elif s["type"] == "wall":
            content = {"kind": "wall", "base": s["base_favorite"], "structure": s.get("structure", "Basic"),
                       "compositeId": composites.get(sid), "composite": s.get("composite"),
                       "reference_line": s.get("reference_line", "Outside"), "surface_override": False}
            plans[sid] = {"name": s["favorite"], "kind": "wall", "content": content}
        else:
            writes, checks, _ = favorite_content(s, attrs)
            base = s["base_favorite"] if s.get("base_favorite") else f"libpart:{s['libpart']}"
            content = {"kind": s["type"], "base": base, "libpart": s["libpart"], "writes": writes,
                       "checks": [{k: c[k] for k in ("field", "want")} for c in checks]}
            plans[sid] = {"name": s["favorite"], "kind": s["type"], "content": content, "writes": writes, "checks": checks}
        plans[sid]["hash"] = _hash(plans[sid]["content"])
    return plans


def _sample_size(spec: dict, sid: str) -> tuple[float, float, float]:
    for o in spec["openings"]:
        if o["system"] == sid and not o.get("merged_into"):
            s0, s1, v0, v1 = o["rect"]["v"]
            for m in spec["openings"]:
                if m.get("merged_into") == o["id"]:
                    s1 = max(s1, m["rect"]["v"][1])
            return round(s1 - s0, 4), round(v1 - v0, 4), 0.1
    return 1.0, 2.0, 0.1


def _gdl_of(ac: TapirClient, guid: str) -> dict:
    r = ac.tapir("GetGDLParametersOfElements", {"elements": [{"elementId": {"guid": guid}}]})["gdlParametersOfElements"][0]
    if "parameters" not in r:
        raise TapirError(f"GetGDLParametersOfElements {guid}: {r}")
    return {p["name"]: p.get("value") for p in r["parameters"]}


def ensure_favorites(ac: TapirClient, spec: dict, attrs: Attrs, composites: dict, manifest: dict, out_root: Path,
                     log: list, profiles: dict | None = None) -> tuple[int, dict, list]:
    plans = favorite_plans(spec, attrs, composites, profiles)
    existing = {}
    changes = 0
    for kind in {p["kind"] for p in plans.values()}:
        existing[kind] = set(ac.tapir("GetFavoritesByType", {"elementType": FAV_TYPES[kind]}).get("favorites", []))
    todo = {sid: p for sid, p in plans.items()
            if p["name"] not in existing[p["kind"]] or manifest["favorites"].get(p["name"], {}).get("hash") != p["hash"]}
    exceptions: list[dict] = []
    if not todo:
        for sid, p in plans.items():
            exceptions += manifest["favorites"][p["name"]].get("exceptions", [])
        return 0, plans, exceptions

    cleanup_scratch(ac, manifest, out_root, log)
    for k, (sid, p) in enumerate([(k, v) for k, v in todo.items() if v["kind"] == "curtain_wall"]):   # the tool defaults
        fa = spec["reference"]["a"]
        changes += ensure_cw_favorite(ac, sid, p, existing["curtain_wall"], manifest, log,
                                      (fa[0] + SAMPLE_OFFSET[0] + 20.0, fa[1] + SAMPLE_OFFSET[1] + 3.0 * k), spec["reference"]["home_story"]["floorIndex"],
                                      facade_layer(ac, spec["reference"]["layer"], log))
    todo = {k: v for k, v in todo.items() if v["kind"] != "curtain_wall"}
    if not todo:
        save_manifest(out_root, manifest)
        for sid, p in plans.items():
            exceptions += manifest["favorites"][p["name"]].get("exceptions", [])
        return changes, plans, exceptions
    fr_a = spec["reference"]["a"]
    x0, y0 = fr_a[0] + SAMPLE_OFFSET[0], fr_a[1] + SAMPLE_OFFSET[1]
    floor = spec["reference"]["home_story"]["floorIndex"]
    ac.tapir("ChangeWindow", {"windowType": "3DModel"})          # modeller trap: create from the 3D window
    try:
        # one sample wall per wall system to (re)store; the first one (or a plain basic wall) hosts the window/door samples
        host_guid = None
        for wi, sid in enumerate([sid for sid, p in todo.items() if p["kind"] == "wall"] or [None]):
            yy = y0 - 3.0 * wi
            host = {"begCoordinate": {"x": x0 + 12.0, "y": yy}, "endCoordinate": {"x": x0, "y": yy}, "floorIndex": floor,
                    "zCoordinate": 0.0, "height": 4.0, "referenceLineLocation": "Outside"}
            if sid:
                c = todo[sid]["content"]
                host.update({"favoriteName": c["base"], "structureType": c["structure"], "referenceLineLocation": c["reference_line"]})
                if c["compositeId"]:
                    host["compositeId"] = {"guid": c["compositeId"]}
            else:
                host.update({"structureType": "Basic", "thickness": 0.45})
            res = ac.tapir("CreateWalls", {"wallsData": [host]})
            g = res["elements"][0].get("elementId", {}).get("guid")
            if not g:
                raise TapirError(f"sample wall {sid}: {res}")
            manifest["scratch"].append(g)
            save_manifest(out_root, manifest)
            host_guid = host_guid or g
            if sid:
                r = ac.tapir("ModifyWalls", {"wallsWithDetails": [{"elementId": {"guid": g},
                                                                     "referenceMaterial": {"overridden": False},
                                                                     "oppositeMaterial": {"overridden": False}}]})
                if errs := results_ok(r["executionResults"]):
                    raise TapirError(f"ModifyWalls (surface overrides off): {errs}")
                d = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}]})["detailsOfElements"][0]["details"]
                if d.get("structureType") != todo[sid]["content"]["structure"] or                         (d.get("compositeId") or {}).get("guid") != todo[sid]["content"]["compositeId"]:
                    raise TapirError(f"sample wall {sid}: structure/composite not as sent ({d.get('structureType')}, {d.get('compositeId')})")
                changes += _store_favorite(ac, g, todo[sid], existing["wall"], manifest, log)

        s_at = 0.3
        for k, (sid, p) in enumerate(todo.items()):
            if p["kind"] in ("beam", "column"):                    # I3: linear sample carrying the FS profile
                if p["kind"] == "beam":
                    res = ac.tapir("CreateBeams", {"beamsData": [{"begCoordinate": {"x": x0, "y": y0 + 3.0 + k}, "endCoordinate": {"x": x0 + 2.0, "y": y0 + 3.0 + k},
                                                                  "floorIndex": floor, "zCoordinate": 1.0, "profileId": {"guid": p["profile_guid"]}}]})
                else:
                    res = ac.tapir("CreateColumns", {"columnsData": [{"coordinates": {"x": x0 + 3.0, "y": y0 + 3.0 + k, "z": 0.0}, "floorIndex": floor,
                                                                      "height": 2.0, "profileId": {"guid": p["profile_guid"]}}]})
                g = res["elements"][0].get("elementId", {}).get("guid")
                if not g:
                    raise TapirError(f"sample {sid}: {res}")
                manifest["scratch"].append(g)
                save_manifest(out_root, manifest)
                d = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}], "fields": ["details"]})["detailsOfElements"][0]
                if p["profile_guid"].lower() not in json.dumps(d).lower():
                    raise TapirError(f"sample {sid}: the {p['kind']} does not report profile {p['content']['profile']}")
                changes += _store_favorite(ac, g, p, existing[p["kind"]], manifest, log)
                continue
            if p["kind"] == "wall":
                continue
            if p["kind"] in ("slab", "railing"):                  # I6: slab / railing sample
                g = _slab_or_railing_sample(ac, spec, sid, p, x0, y0 + 6.0 + 3.0 * k, floor, attrs, manifest, out_root, log)
                changes += _store_favorite(ac, g, p, existing[p["kind"]], manifest, log)
                continue
            w, h, sill = _sample_size(spec, sid)
            key, cmd = ("windowsData", "CreateWindows") if p["kind"] == "window" else ("doorsData", "CreateDoors")
            base = p["content"]["base"]
            placement = {"libraryPartName": base[8:]} if base.startswith("libpart:") else {"favoriteName": base}
            res = ac.tapir(cmd, {key: [{"ownerWallId": {"guid": host_guid}, "centerOffset": round(12.0 - s_at - w / 2, 4),
                                        "width": w, "height": h, "sillHeight": sill, **placement}]})
            g = res["elements"][0].get("elementId", {}).get("guid")
            if not g:
                raise TapirError(f"sample {sid}: {res}")
            manifest["scratch"].append(g)
            save_manifest(out_root, manifest)
            s_at += w + 0.4
            d = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}], "fields": ["details"]})["detailsOfElements"][0]
            if d["details"]["libPart"]["name"] != p["content"]["libpart"]:
                raise TapirError(f"{sid}: base favourite '{p['content']['base']}' placed '{d['details']['libPart']['name']}', "
                                 f"not '{p['content']['libpart']}'")
            r = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": [{"elementId": {"guid": g}, "gdlParameters": p["writes"]}]})
            if errs := results_ok(r["executionResults"]):
                raise TapirError(f"{sid}: SetGDLParametersOfElements: {errs}")
            params = _gdl_of(ac, g)
            bad = [f"{w_['name']}={params.get(w_['name'])!r} (sent {w_['value']!r})" for w_ in p["writes"] if not same(params.get(w_["name"]), w_["value"])]
            if bad:
                raise TapirError(f"{sid}: the parameter script did not keep: {bad}")
            p_exc = []
            for c in p["checks"]:
                got = read_field(c["read"], params, attrs)
                if same(got, c["want"]):
                    continue
                if c["unsupported"]:
                    p_exc.append({"system": sid, "field": c["field"], "spec": c["want"], "model": got, "reason": c["unsupported"]})
                else:
                    raise TapirError(f"{sid}: {c['field']} reads {got!r} on {p['content']['libpart']}, spec wants {c['want']!r}")
            p["exceptions"] = p_exc
            exceptions += p_exc
            changes += _store_favorite(ac, g, p, existing[p["kind"]], manifest, log)
    finally:
        cleanup_scratch(ac, manifest, out_root, log)
    for sid, p in plans.items():
        if sid not in todo:
            exceptions += manifest["favorites"][p["name"]].get("exceptions", [])
    return changes, plans, exceptions


# ---- I5/I6: curtain wall systems, slab and railing samples ---------------------------------------------------------
def cw_class_names(sid: str, s: dict) -> dict:
    """Names of the frame class and of the panel classes of a curtain wall system (unique per system)."""
    return {"frame": f"FS {sid} rám", "panels": {k: f"FS {sid} {k}" for k in s.get("panels", {})}}


def cw_content(sid: str, s: dict, layer: str | None = None) -> dict:
    names = cw_class_names(sid, s)
    val = lambda d: {k: (v["v"] if isinstance(v, dict) and "v" in v else v) for k, v in d.items() if k != "note"}
    return {"kind": "curtain_wall", "frame": val(s["frame"]), "boundary": val(s["boundary"]),
            "panels": {k: val(v) for k, v in s.get("panels", {}).items()}, "names": names, "layer": layer}


def _cw_input(content: dict) -> dict:
    fr, bd = content["frame"], content["boundary"]
    frame = {"name": content["names"]["frame"], "width": fr["width"], "depth": fr["depth"], "offset": fr["offset"]}
    boundary = {"width": bd["width"], "depth": bd["depth"], "offset": bd["offset"]}
    for d, src in ((frame, fr), (boundary, bd)):
        if src.get("material"):
            d["buildingMaterialName"] = src["material"]
        if src.get("surface"):
            d["surfaceName"] = src["surface"]
    panels = []
    for kind, pn in content["panels"].items():
        item = {"name": content["names"]["panels"][kind]}
        if pn.get("libpart"):
            item["libraryPartName"] = pn["libpart"]
        else:
            item["thickness"] = pn["thickness"]
            if pn.get("material"):
                item["buildingMaterialName"] = pn["material"]
            if pn.get("surface"):
                item["surfaceName"] = pn["surface"]
        panels.append(item)
    return {"frameClasses": [frame], "panelClasses": panels, "boundaryFrame": boundary}


def ensure_cw_favorite(ac: TapirClient, sid: str, p: dict, existing: set, manifest: dict, log: list, sample_xy: tuple = (0.0, 0.0),
                       floor: int = 0, layer_index: int | None = None) -> int:
    """Curtain wall system -> classes in the CW tool defaults -> favourite; the defaults are restored by the same call
    (restoreDefaults) and that is checked. Every field Archicad did not keep (notKept) is an error."""
    base = _cw_input(p["content"])
    first = ac.tapir("SetCurtainWallSystemDefaults", dict(base, restoreDefaults=True), timeout=120)
    _cw_check(sid, first, p["content"])
    idx = {c["name"]: c["classIndex"] for c in first["frameClasses"] + first["panelClasses"]}
    glass = idx[p["content"]["names"]["panels"]["glass"]]
    frame = idx[p["content"]["names"]["frame"]]
    full = dict(base, primaryPattern={"sizes": [1.0], "logic": "FixedSizes", "origin": "StartWithPattern"},
                secondaryPattern={"sizes": [1.0], "logic": "FixedSizes", "origin": "StartWithPattern"},
                cells=[{"columnIndex": 0, "rowIndex": 0, "panelClassIndex": glass, "leftFrameClassIndex": frame, "bottomFrameClassIndex": frame}],
                favorite={"name": p["name"], "overwriteExisting": True}, restoreDefaults=True)
    second = ac.tapir("SetCurtainWallSystemDefaults", full, timeout=120)
    exc = _cw_check(sid, second, p["content"])
    idx2 = {c["name"]: c["classIndex"] for c in second["frameClasses"] + second["panelClasses"]}
    if idx2 != idx:
        raise TapirError(f"{sid}: class indices changed between the two calls ({idx} / {idx2})")
    if second.get("favorite", {}).get("result") not in ("created", "overwritten"):
        raise TapirError(f"{sid}: favourite {p['name']} not saved: {second.get('favorite')}")
    # The favourite has to carry the facade layer: SetDetailsOfElements on a curtain wall (layer change) re-lays its row
    # pattern from the story level (rows shifted by the base offset, extra panels - found 2026-10-19), so a CW must be
    # created on its final layer. A sample from the favourite is moved to the layer (its pattern does not matter) and
    # the favourite is updated from it; class list and indices stay those of the defaults.
    x, y = sample_xy
    res = ac.tapir("CreateCurtainWalls", {"curtainWallsData": [{"favoriteName": p["name"], "coordinates": [{"x": x, "y": y}, {"x": x + 2.0, "y": y}],
                                                                "zCoordinate": 0.0, "floorIndex": floor, "height": 2.0}]})
    g = res["elements"][0].get("elementId", {}).get("guid")
    if not g:
        raise TapirError(f"{sid}: sample curtain wall: {res}")
    manifest["scratch"].append(g)
    try:
        move_to_layer(ac, [g], layer_index, log)
        res = ac.tapir("UpdateFavoritesFromElements", {"favoritesFromElements": [{"elementId": {"guid": g}, "favorite": p["name"]}]})
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"{sid}: UpdateFavoritesFromElements: {errs}")
    finally:
        ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}}]})
        manifest["scratch"].remove(g)
    manifest["favorites"][p["name"]] = {"hash": p["hash"], "kind": "curtain_wall", "content": p["content"], "class_index": idx,
                                       "exceptions": exc, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    log.append(f"favourite {'updated' if p['name'] in existing else 'created'}: {p['name']} (curtain wall classes {idx}, defaults restored)")
    return 1


SURFACE_FIELDS = {"surface", "outerSurface", "innerSurface", "edgeSurface"}


def _cw_check(sid: str, res: dict, content: dict | None = None) -> list[dict]:
    """Errors for anything Archicad did not keep - except the surfaces: Archicad 29 does not keep a surface on a CW frame /
    panel class (notKept, checked 2026-10-19 with and without a building material and on a generic frame); the colour
    then comes from the building material. Those become EXCEPTIONS (spec colour, model material colour)."""
    if res.get("defaultsRestored") is not True:
        raise TapirError(f"{sid}: Curtain Wall defaults NOT restored: {json.dumps(res, ensure_ascii=False)[:300]}")
    items = res.get("frameClasses", []) + res.get("panelClasses", []) + [dict(res.get("boundaryFrame", {}), name="boundary")]
    bad = [c for c in items if "error" in c or ("classIndex" not in c and c.get("name") != "boundary") or set(c.get("notKept", [])) - SURFACE_FIELDS]
    if bad:
        raise TapirError(f"{sid}: SetCurtainWallSystemDefaults did not keep: {json.dumps(bad, ensure_ascii=False)[:400]}")
    exc = []
    for c in items:
        if c.get("notKept"):
            want = None
            if content:
                if c["name"] == "boundary":
                    want = content["boundary"].get("surface")
                elif c["name"] == content["names"]["frame"]:
                    want = content["frame"].get("surface")
                else:
                    kind = next((k for k, n in content["names"]["panels"].items() if n == c["name"]), None)
                    want = content["panels"].get(kind, {}).get("surface") if kind else None
            exc.append({"system": sid, "field": f"surface of {c['name']}", "spec": want, "model": "farba stavebného materiálu",
                        "reason": "SetCurtainWallSystemDefaults: Archicad 29 nezachová povrch triedy rámu/panelu (notKept: "
                                  + ", ".join(c["notKept"]) + ") - farba z materiálu"})
    return exc


def cw_class_index(manifest: dict, spec: dict, sid: str) -> dict:
    """class name -> class index of a curtain wall system (from the manifest written when its favourite was made)."""
    name = spec["systems"][sid]["favorite"]
    entry = manifest.get("favorites", {}).get(name, {})
    if "class_index" not in entry:
        raise TapirError(f"{sid}: class indices of {name} unknown - run build first")
    return entry["class_index"]


def layer_flags(ac: TapirClient, index: int) -> dict:
    a = next(x for x in ac.tapir("GetAttributesByType", {"attributeType": "Layer"})["attributes"] if x["index"] == index)
    return ac.tapir("GetLayers", {"attributeIds": [{"attributeId": a["attributeId"]}]})["layers"][0]


class LayerUsable:
    """Context: a hidden or locked layer is shown / unlocked (CreateLayers overwrite by name) and put back exactly."""

    def __init__(self, ac: TapirClient, index: int, log: list):
        self.ac, self.index, self.log, self.old = ac, index, log, None

    def __enter__(self):
        L = layer_flags(self.ac, self.index)
        if L.get("isHidden") or L.get("isLocked"):
            self.old = L
            self.ac.tapir("CreateLayers", {"layerDataArray": [{"name": L["name"], "isHidden": False, "isLocked": False,
                                                                "isWireframe": L.get("isWireframe", False), "intersectionGroupNr": L["intersectionGroupNr"]}],
                                           "overwriteExisting": True})
            self.log.append(f"layer '{L['name']}' temporarily shown/unlocked (an element landed on it)")
        return self

    def __exit__(self, *exc):
        if self.old:
            L = self.old
            self.ac.tapir("CreateLayers", {"layerDataArray": [{"name": L["name"], "isHidden": L.get("isHidden", False), "isLocked": L.get("isLocked", False),
                                                                "isWireframe": L.get("isWireframe", False), "intersectionGroupNr": L["intersectionGroupNr"]}],
                                           "overwriteExisting": True})
        return False


def facade_layer(ac: TapirClient, name: str, log: list) -> int:
    """Index of the facade layer (created with intersection group 1, modeller rules 17 / 26)."""
    def find():
        return next((a["index"] for a in ac.tapir("GetAttributesByType", {"attributeType": "Layer"})["attributes"] if a["name"] == name), None)
    idx = find()
    if idx is None:
        ac.tapir("CreateLayers", {"layerDataArray": [{"name": name, "intersectionGroupNr": 1}]})
        log.append(f"layer created: {name}")
        idx = find()
    if idx is None:
        raise TapirError(f"layer '{name}' was not created")
    return idx


def move_to_layer(ac: TapirClient, guids: list[str], layer_index: int, log: list) -> None:
    """Puts fresh elements on the facade layer; an element on a hidden / locked layer is not editable, so that layer
    is shown for the move and put back."""
    if not guids:
        return
    det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids], "fields": ["layerIndex"]})["detailsOfElements"]
    by_layer: dict = {}
    for g, d in zip(guids, det):
        if d.get("layerIndex") != layer_index:
            by_layer.setdefault(d.get("layerIndex"), []).append(g)
    for li, gs in by_layer.items():
        with LayerUsable(ac, li, log):
            res = ac.tapir("SetDetailsOfElements", {"elementsWithDetails": [{"elementId": {"guid": g}, "details": {"layerIndex": layer_index}} for g in gs]})
            if errs := results_ok(res["executionResults"]):
                raise TapirError(f"SetDetailsOfElements (layer): {errs}")


def _slab_or_railing_sample(ac: TapirClient, spec: dict, sid: str, p: dict, x: float, y: float, floor: int, attrs: Attrs,
                            manifest: dict, out_root: Path, log: list) -> str:
    c = p["content"]
    if c["kind"] == "slab":
        data = {"level": 0.0, "floorIndex": floor, "structureType": c["structure"], "thickness": c["thickness"],
                "polygonCoordinates": [{"x": x, "y": y}, {"x": x + 2.0, "y": y}, {"x": x + 2.0, "y": y + 1.0}, {"x": x, "y": y + 1.0}]}
        if c.get("material"):
            data["buildingMaterialId"] = {"guid": attrs.bmat_guid(c["material"])}
        res = ac.tapir("CreateSlabs", {"slabsData": [data]})
    else:
        res = ac.tapir("CreateRailings", {"railingsData": [{"favoriteName": c["base"], "coordinates": [{"x": x, "y": y}, {"x": x + 3.0, "y": y}],
                                                            "zCoordinate": 0.0, "floorIndex": floor, "height": c["height"],
                                                            "postSpacing": c["post_spacing"], "postDistribution": c["post_distribution"]}]})
    g = res["elements"][0].get("elementId", {}).get("guid")
    if not g:
        raise TapirError(f"sample {sid}: {res}")
    manifest["scratch"].append(g)
    save_manifest(out_root, manifest)
    move_to_layer(ac, [g], facade_layer(ac, spec["reference"]["layer"], log), log)   # the favourite keeps a usable layer
    if c["kind"] == "slab":
        d = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}}]})["detailsOfElements"][0]["details"]
        if d.get("structureType") != c["structure"] or abs(d.get("thickness", -1) - c["thickness"]) > 1e-6:
            raise TapirError(f"sample {sid}: slab {d.get('structureType')} {d.get('thickness')} != {c['structure']} {c['thickness']}")
    else:
        st = ac.tapir("ModifyRailingDetails", {"railings": [{"elementId": {"guid": g}}]})["results"][0].get("state", {})
        if not st.get("balusters") or any(abs(h - c["height"]) > 1e-6 for h in st.get("segmentHeights", [])):
            raise TapirError(f"sample {sid}: railing from '{c['base']}' has no balusters or a wrong height: {st}")
        p["content"]["sample_state"] = {k: st.get(k) for k in ("posts", "innerPosts", "balusters", "balusterSets")}
    return g


def _store_favorite(ac: TapirClient, guid: str, p: dict, existing: set, manifest: dict, log: list) -> int:
    name = p["name"]
    if name in existing:
        res = ac.tapir("UpdateFavoritesFromElements", {"favoritesFromElements": [{"elementId": {"guid": guid}, "favorite": name}]})
        verb = "updated"
    else:
        res = ac.tapir("CreateFavoritesFromElements", {"favoritesFromElements": [{"elementId": {"guid": guid}, "favorite": name}]})
        verb = "created"
    if errs := results_ok(res["executionResults"]):
        raise TapirError(f"favourite {name}: {errs}")
    manifest["favorites"][name] = {"hash": p["hash"], "kind": p["kind"], "content": p["content"],
                                   "exceptions": p.get("exceptions", []), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    log.append(f"favourite {verb}: {name}")
    return 1


def cleanup_scratch(ac: TapirClient, manifest: dict, out_root: Path, log: list) -> None:
    """Deletes the sample elements and the host wall recorded in the manifest (also after an interrupted run)."""
    guids = manifest.get("scratch", [])
    if not guids:
        return
    det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids], "fields": ["type", "layerIndex"]})["detailsOfElements"]
    alive = [g for g, d in zip(guids, det) if isinstance(d, dict) and "type" in d]
    for li in {d.get("layerIndex") for d in det if isinstance(d, dict) and "type" in d}:
        if li is not None and (layer_flags(ac, li).get("isHidden") or layer_flags(ac, li).get("isLocked")):
            with LayerUsable(ac, li, log):                         # a sample left on a hidden layer by an interrupted run
                ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}} for g, d in zip(guids, det)
                                                         if isinstance(d, dict) and d.get("layerIndex") == li]})
            alive = [g for g, d in zip(guids, det) if isinstance(d, dict) and "type" in d and d.get("layerIndex") != li]
    if alive:
        res = ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}} for g in alive]})
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"DeleteElements (samples): {errs}")
    manifest["scratch"] = []
    save_manifest(out_root, manifest)


# ---- GDL objects (pilot A: FS_Surround, FS_ArchGable from the I4 templates) ------------------------------------
def gdl_railing_systems(spec: dict) -> dict:
    """Railing systems placed as the GDL object FS_Railing (railing_kind 'gdl', ADR-001 R1 fallback)."""
    return {sid: s for sid, s in spec["systems"].items() if s["type"] == "railing" and s.get("railing_kind") == "gdl"}


def gdl_templates(spec: dict) -> list[str]:
    names = {s["template"] for s in spec["systems"].values() if s["type"] == "gdl"}
    if gdl_railing_systems(spec):
        names.add("FS_Railing")
    return sorted(names)


def ensure_rail_gdl_favorites(ac: TapirClient, spec: dict, gdl: dict, out_root: Path, log: list, write: bool = True) -> int:
    """'FS rail.*' Object favourites of the gdl railing systems: the standard ones (railing_gdl.FAVORITES) are made from
    a sample far from the model; any other favourite name must already exist in the project."""
    rails = gdl_railing_systems(spec)
    if not rails:
        return 0
    import railing_gdl
    names = sorted({s["favorite"] for s in rails.values()})
    existing = set(ac.tapir("GetFavoritesByType", {"elementType": "Object"}).get("favorites", []))
    std = [n for n in names if n in railing_gdl.FAVORITES]
    n = 0
    if write and std:
        # the sample goes onto the facade layer before it is edited (the Object tool default layer of the template is hidden:
        # an element there can be neither changed nor deleted) - the favourite carries the facade layer
        layer = spec["reference"]["layer"]
        li = facade_layer(ac, layer, log)
        res = railing_gdl.ensure_favorites(ac, std, origin=(SAMPLE_OFFSET[0], SAMPLE_OFFSET[1] + 40.0), log=log,
                                           template_hash=gdl["FS_Railing"]["hash"] + "|" + layer, manifest_path=out_root / "rail_gdl_favorites.json",
                                           prepare=lambda g: move_to_layer(ac, [g], li, log))
        n = sum(v["action"] != "kept" for v in res.values())
        existing |= set(std)
    missing = [nm for nm in names if nm not in existing]
    if missing:
        raise TapirError(f"gdl railing favourites missing in the project: {missing}")
    return n


def ensure_gdl(ac: TapirClient, spec: dict, project: str, log: list, write: bool = True) -> tuple[int, dict]:
    """Templates used by the gdl systems are compiled and loaded into the embedded library (gdl_build.deploy, idempotent
    through its manifest hash). Returns (number of templates (re)loaded, template -> {hash, libpart index / guid})."""
    names = gdl_templates(spec)
    if not names:
        return 0, {}
    import gdl_build
    if write:
        r = gdl_build.deploy(ac, tuple(names), project=project)
        if r["changed"]:
            log.append(f"GDL templates loaded: {r['changed']}")
        n = len(r["changed"])
        parts = r["parts"]
    else:
        n = 0
        parts = gdl_build.loaded_parts(ac, names)
    man = gdl_build.load_manifest()["parts"]
    out = {}
    for name in names:
        if name not in parts or name not in man:
            raise TapirError(f"GDL template {name} is not loaded in the project - run build first")
        out[name] = {"hash": man[name]["hash"], "index": parts[name].get("index"), "guid": parts[name].get("guid"),
                     "template_guid": man[name]["guid"]}
    return n, out


def classification_of_systems(ac: TapirClient, spec: dict) -> dict:
    """gdl system id -> ClassificationId of its 'classification' (item name, unique in the classification system of the
    project). An object placed by CreateObjects from a library part is unclassified and then cannot carry the FS_*
    properties (their availability is per classification item)."""
    want = {sid: s["classification"] for sid, s in spec["systems"].items() if s["type"] == "gdl" and s.get("classification")}
    for sid, s in gdl_railing_systems(spec).items():         # FS_Railing: railing item -> IFC export as IfcRailing
        want[sid] = s.get("classification") or list(KIND_CLASS["railing"])
    if not want:
        return {}
    found: dict[str, list] = {}
    for cs in ac.api("API.GetAllClassificationSystems")["classificationSystems"]:
        tree = ac.api("API.GetAllClassificationsInSystem", {"classificationSystemId": cs["classificationSystemId"]})["classificationItems"]
        stack = list(tree)
        while stack:
            it = stack.pop()["classificationItem"]
            found.setdefault(it["id"], []).append({"classificationSystemId": cs["classificationSystemId"],
                                                   "classificationItemId": it["classificationItemId"]})
            stack += it.get("children", [])
    out = {}
    for sid, name in want.items():
        if isinstance(name, list):                           # candidates (SK / CZ template): the first unique one
            name = next((n for n in name if len(found.get(n, [])) == 1), name[0])
        hits = found.get(name, [])
        if len(hits) != 1:
            raise TapirError(f"systems[{sid}]: classification item '{name}' found {len(hits)} times in the project")
        out[sid] = hits[0]
    return out


def classify_unclassified(ac: TapirClient, items: list[tuple[str, str]], log: list) -> int:
    """(guid, kind) of elements: the unclassified ones get the classification item of their kind (KIND_CLASS)."""
    if not items:
        return 0
    systems_ = ac.api("API.GetAllClassificationSystems")["classificationSystems"]
    res = ac.tapir("GetClassificationsOfElements", {"elements": [{"elementId": {"guid": g}} for g, _ in items],
                                                     "classificationSystemIds": [{"classificationSystemId": s["classificationSystemId"]} for s in systems_]})
    todo = []
    for (g, kind), ec in zip(items, res["elementClassifications"]):
        ids = [c for c in ec.get("classificationIds", []) if c.get("classificationItemId") and c["classificationItemId"]["guid"] != NULL_GUID]
        if not ids and kind in KIND_CLASS:
            todo.append((g, kind))
    if not todo:
        return 0
    found: dict[str, list] = {}
    for cs in systems_:
        tree = ac.api("API.GetAllClassificationsInSystem", {"classificationSystemId": cs["classificationSystemId"]})["classificationItems"]
        stack = list(tree)
        while stack:
            it = stack.pop()["classificationItem"]
            found.setdefault(it["id"], []).append({"classificationSystemId": cs["classificationSystemId"], "classificationItemId": it["classificationItemId"]})
            stack += it.get("children", [])
    data = []
    for g, kind in todo:
        hits = next((found[n] for n in KIND_CLASS[kind] if len(found.get(n, [])) == 1), None)
        if hits:
            data.append({"elementId": {"guid": g}, "classificationId": hits[0]})
    if data:
        r = ac.tapir("SetClassificationsOfElements", {"elementClassifications": data})
        if errs := results_ok(r["executionResults"]):
            raise TapirError(f"SetClassificationsOfElements (by kind): {errs}")
        log.append(f"classified {len(data)} unclassified element(s) by kind")
    return len(data)


# ---- entry points ------------------------------------------------------------------------------------
def ensure(ac: TapirClient, spec: dict, project: str, out_root: Path, log: list) -> dict:
    """Make every system of the spec exist in Archicad. Returns sysinfo for build.plan()."""
    manifest = load_manifest(out_root, project)
    attrs = Attrs(ac)
    n_comp, composites = ensure_composites(ac, spec, attrs, log)
    n_prop, props = ensure_properties(ac, log)
    n_prof, profiles = ensure_profiles(ac, spec, attrs, manifest, log)
    save_manifest(out_root, manifest)
    n_fav, plans, exceptions = ensure_favorites(ac, spec, attrs, composites, manifest, out_root, log, profiles)
    save_manifest(out_root, manifest)
    n_gdl, gdl = ensure_gdl(ac, spec, project, log, write=True)
    n_fav += ensure_rail_gdl_favorites(ac, spec, gdl, out_root, log, write=True)
    return {"composites": composites, "props": props, "favorites": {sid: {"name": p["name"], "hash": p["hash"]} for sid, p in plans.items()},
            "profiles": profiles, "exceptions": exceptions, "gdl": gdl, "classes": classification_of_systems(ac, spec),
            "changes": {"composites": n_comp, "properties": n_prop, "profiles": n_prof, "favorites": n_fav, "gdl": n_gdl},
            "manifest": manifest, "attrs": attrs}


def resolve(ac: TapirClient, spec: dict, project: str, out_root: Path) -> dict:
    """Read-only twin of ensure() for verify: same sysinfo, fails when something is missing."""
    manifest = load_manifest(out_root, project)
    attrs = Attrs(ac)
    composites = {}
    for sid, s in spec["systems"].items():
        if "composite" in s:
            if s["composite"]["name"] not in attrs.composite:
                raise TapirError(f"composite {s['composite']['name']} missing - run build first")
            composites[sid] = attrs.composite[s["composite"]["name"]]["attributeId"]["guid"]
    _, props = property_ids(ac)
    _, profiles = ensure_profiles(ac, spec, attrs, manifest, [], write=False)
    plans = favorite_plans(spec, attrs, composites, profiles)
    exceptions = []
    for sid, p in plans.items():
        exceptions += manifest["favorites"].get(p["name"], {}).get("exceptions", [])
    _, gdl = ensure_gdl(ac, spec, project, [], write=False)
    ensure_rail_gdl_favorites(ac, spec, gdl, out_root, [], write=False)
    return {"composites": composites, "props": props, "favorites": {sid: {"name": p["name"], "hash": p["hash"]} for sid, p in plans.items()},
            "profiles": profiles, "exceptions": exceptions, "manifest": manifest, "attrs": attrs, "gdl": gdl,
            "classes": classification_of_systems(ac, spec)}

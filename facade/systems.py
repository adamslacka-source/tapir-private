"""systems: facade-spec/1 catalogue `systems` -> Archicad attributes, FS Favorites and FS_* properties (ADR-001 D3/D4, I2).

What it makes (idempotently - a second run changes nothing):
  * Composite attributes of wall systems (CreateComposites; compared with GetComposites first)
  * the property group 'FS' with FS_zdroj / FS_istota / FS_spec (CreatePropertyGroups / CreatePropertyDefinitions)
  * Favorites 'FS <system id>' through a sample element (D4): a temporary host wall far from the model, the sample
    placed from the base favourite, GDL system parameters written and READ BACK, CreateFavoritesFromElements
    (UpdateFavoritesFromElements when the content changed), then sample and host are deleted again.

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

from spec_validate import TYPE_KEYS, load_gdl_map
from tapir_client import TapirClient, TapirError, results_ok

FS_GROUP = "FS"
FS_PROPS = {
    "FS_zdroj": "Zdroj hodnôt prvku fasády: poloha/rozmer, typ, hĺbka - id zdroja zo spec (facade-spec/1) a istota.",
    "FS_istota": "Najnižšia istota hodnôt, ktoré prvok tvarujú: kota > meranie > pravidlo > odhad > default.",
    "FS_spec": "Spec a položka, z ktorej prvok vznikol (<spec.id>.<id>).",
}
FAV_TYPES = {"wall": "Wall", "window": "Window", "door": "Door"}
SAMPLE_OFFSET = (1000.0, 0.0)          # m - temporary host wall far away from every pilot facade
NUM_TOL = 1e-4


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
    writes = [{"name": k, "value": v} for k, v in m["common"].get("system_params", {}).items()]
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
            if c.get("classificationItemId"):
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
def favorite_plans(spec: dict, attrs: Attrs, composites: dict) -> dict:
    """system id -> what the FS favourite must contain (+ hash). Pure apart from the attribute lookups."""
    plans = {}
    for sid, s in spec["systems"].items():
        if not s.get("base_favorite"):
            continue
        if s["type"] == "wall":
            content = {"kind": "wall", "base": s["base_favorite"], "structure": s.get("structure", "Basic"),
                       "compositeId": composites.get(sid), "composite": s.get("composite"),
                       "reference_line": s.get("reference_line", "Outside"), "surface_override": False}
            plans[sid] = {"name": s["favorite"], "kind": "wall", "content": content}
        else:
            writes, checks, _ = favorite_content(s, attrs)
            content = {"kind": s["type"], "base": s["base_favorite"], "libpart": s["libpart"], "writes": writes,
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
                     log: list) -> tuple[int, dict, list]:
    plans = favorite_plans(spec, attrs, composites)
    existing = {}
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
    fr_a = spec["reference"]["a"]
    x0, y0 = fr_a[0] + SAMPLE_OFFSET[0], fr_a[1] + SAMPLE_OFFSET[1]
    floor = spec["reference"]["home_story"]["floorIndex"]
    changes = 0
    ac.tapir("ChangeWindow", {"windowType": "3DModel"})          # modeller trap: create from the 3D window
    try:
        # host wall = sample of the wall system when one is needed, else a plain basic wall
        wall_sid = next((sid for sid, p in todo.items() if p["kind"] == "wall"), None)
        host = {"begCoordinate": {"x": x0 + 12.0, "y": y0}, "endCoordinate": {"x": x0, "y": y0}, "floorIndex": floor,
                "zCoordinate": 0.0, "height": 4.0, "referenceLineLocation": "Outside"}
        if wall_sid:
            c = todo[wall_sid]["content"]
            host.update({"favoriteName": c["base"], "structureType": c["structure"], "referenceLineLocation": c["reference_line"]})
            if c["compositeId"]:
                host["compositeId"] = {"guid": c["compositeId"]}
        else:
            host.update({"structureType": "Basic", "thickness": 0.45})
        res = ac.tapir("CreateWalls", {"wallsData": [host]})
        host_guid = res["elements"][0].get("elementId", {}).get("guid")
        if not host_guid:
            raise TapirError(f"sample host wall: {res}")
        manifest["scratch"].append(host_guid)
        save_manifest(out_root, manifest)
        if wall_sid:
            r = ac.tapir("ModifyWalls", {"wallsWithDetails": [{"elementId": {"guid": host_guid},
                                                                 "referenceMaterial": {"overridden": False},
                                                                 "oppositeMaterial": {"overridden": False}}]})
            if errs := results_ok(r["executionResults"]):
                raise TapirError(f"ModifyWalls (surface overrides off): {errs}")
            d = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": host_guid}}]})["detailsOfElements"][0]["details"]
            if d.get("structureType") != todo[wall_sid]["content"]["structure"] or \
                    (d.get("compositeId") or {}).get("guid") != todo[wall_sid]["content"]["compositeId"]:
                raise TapirError(f"sample wall: structure/composite not as sent ({d.get('structureType')}, {d.get('compositeId')})")
            changes += _store_favorite(ac, host_guid, todo[wall_sid], existing["wall"], manifest, log)

        s_at = 0.3
        for sid, p in todo.items():
            if p["kind"] == "wall":
                continue
            w, h, sill = _sample_size(spec, sid)
            key, cmd = ("windowsData", "CreateWindows") if p["kind"] == "window" else ("doorsData", "CreateDoors")
            res = ac.tapir(cmd, {key: [{"ownerWallId": {"guid": host_guid}, "centerOffset": round(12.0 - s_at - w / 2, 4),
                                        "width": w, "height": h, "sillHeight": sill, "favoriteName": p["content"]["base"]}]})
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
    det = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids], "fields": ["type"]})["detailsOfElements"]
    alive = [g for g, d in zip(guids, det) if isinstance(d, dict) and "type" in d]
    if alive:
        res = ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": g}} for g in alive]})
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"DeleteElements (samples): {errs}")
    manifest["scratch"] = []
    save_manifest(out_root, manifest)


# ---- entry points ------------------------------------------------------------------------------------
def ensure(ac: TapirClient, spec: dict, project: str, out_root: Path, log: list) -> dict:
    """Make every system of the spec exist in Archicad. Returns sysinfo for build.plan()."""
    manifest = load_manifest(out_root, project)
    attrs = Attrs(ac)
    n_comp, composites = ensure_composites(ac, spec, attrs, log)
    n_prop, props = ensure_properties(ac, log)
    n_fav, plans, exceptions = ensure_favorites(ac, spec, attrs, composites, manifest, out_root, log)
    save_manifest(out_root, manifest)
    return {"composites": composites, "props": props, "favorites": {sid: {"name": p["name"], "hash": p["hash"]} for sid, p in plans.items()},
            "exceptions": exceptions, "changes": {"composites": n_comp, "properties": n_prop, "favorites": n_fav},
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
    plans = favorite_plans(spec, attrs, composites)
    exceptions = []
    for sid, p in plans.items():
        exceptions += manifest["favorites"].get(p["name"], {}).get("exceptions", [])
    return {"composites": composites, "props": props, "favorites": {sid: {"name": p["name"], "hash": p["hash"]} for sid, p in plans.items()},
            "exceptions": exceptions, "manifest": manifest, "attrs": attrs}

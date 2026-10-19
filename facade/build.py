"""build: facade-spec/1 -> Archicad elements.

I1: one wall + windows + doors from existing Favorites.
I2: first the catalogue `systems` (systems.py: Composite, FS Favorites, FS_* properties), then the elements are
    placed from the FS Favorites; per element the instance values of the spec are written (transom height,
    sidelight, glazing bar, wall inset of the frame) and the properties FS_zdroj / FS_istota / FS_spec.
    Openings with `merged_into` are built as ONE element with their host (e.g. door + shop window = sidelight).
I3: the wall inset of the frames is native (revealDepthLocation 'Side' + revealDepthOffset, Tapir T8) - no
    subtraction of the outer skins; `linear` elements (cornice, string course, sill ledges, surrounds) are profiled
    Beams (horizontal: profile x = out of the facade, y = up) and Columns (vertical: profile x = out, y = along the
    facade) with the FS profile, placed from their FS favourite.

Idempotent: every created element is recorded in <out>/<spec.id>/state.json with a hash of what was sent
(including the hash of the favourite it comes from). A second run with the same spec changes nothing. Only
GUIDs from the state file are ever deleted - elements this script did not create are never touched.

    python build.py <spec.json> [--project C:/FacadePilot/Fasada_pilot_A.pln] [--port 19723] [--save] [--force]

Writes only when the open project is exactly --project (never the template in C:/ArchicadTemp).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import systems as fs_systems  # noqa: E402
import math  # noqa: E402

from geom import frame_of  # noqa: E402
from spec_validate import element_id, fs_system, load_gdl_map, lowest_conf, validate  # noqa: E402
from tapir_client import TapirClient, TapirError, results_ok  # noqa: E402

DEFAULT_PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
DEFAULT_OUT = Path(r"C:\FacadePilot\out")
STATE_VERSION = 1
ELEMENT_ID_PROPERTY = {"type": "BuiltIn", "nonLocalizedName": "General_ElementID"}
NOMINAL_WALLHOLE = {"name": "gs_nominal_size", "value": 1}      # Door 27 family: 1 = wall hole, 8 = egress (favorite default)
INSTANCE_KEYS = ("transom_h", "sidelight_w", "sidelight_sill", "transom_bars", "transom_bar_h", "bar_h")


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def state_path(spec: dict, out_root: Path) -> Path:
    return out_root / spec["id"] / "state.json"


def load_state(path: Path, spec: dict, project: str) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("spec_id") != spec["id"]:
            raise TapirError(f"{path}: state belongs to spec '{state.get('spec_id')}', not '{spec['id']}'")
        if state.get("project", "").lower() != project.lower():
            raise TapirError(f"{path}: state belongs to project '{state.get('project')}', not '{project}'")
        return state
    return {"version": STATE_VERSION, "spec_id": spec["id"], "project": project, "elements": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---- units: openings built as one element ------------------------------------------------------------
def opening_units(spec: dict) -> list[dict]:
    """One entry per element to build: host opening + the openings merged into it, union rect, derived values."""
    units = []
    for o in spec["openings"]:
        if o.get("merged_into"):
            continue
        members = [m for m in spec["openings"] if m.get("merged_into") == o["id"]]
        s0, s1, v0, v1 = o["rect"]["v"]
        values = {k: o[k] for k in INSTANCE_KEYS if k in o}
        for m in members:                                    # right sidelight (validated in spec_validate._check_i2)
            ms0, ms1, mv0, _ = m["rect"]["v"]
            s1 = max(s1, ms1)
            values["sidelight_w"] = {"v": round(ms1 - ms0, 6), "src": m["rect"]["src"], "conf": m["rect"]["conf"],
                                     "note": f"width of {m['id']} (merged)"}
            values["sidelight_sill"] = {"v": round(mv0 - o["rect"]["v"][2], 6), "src": m["rect"]["src"], "conf": m["rect"]["conf"],
                                        "note": f"bottom of {m['id']} above the bottom of {o['id']}"}
        units.append({"o": o, "members": members, "rect": [s0, s1, v0, v1], "values": values})
    return units


def _zdroj(parts: list[tuple[str, dict | None]]) -> tuple[str, str]:
    """FS_zdroj text and FS_istota (lowest certainty) from (label, {src, conf}) pairs."""
    txt, confs = [], []
    for label, val in parts:
        if not val:
            continue
        txt.append(f"{label}: {val.get('src') or '-'} ({val['conf']})")
        confs.append(val["conf"])
    return "; ".join(txt), lowest_conf(*confs) if confs else "default"


# ---- plan: spec -> Archicad payloads ----------------------------------------------------------
def plan(spec: dict, story_levels: dict[int, float], sysinfo: dict | None = None) -> dict:
    """Pure function: what has to exist in Archicad for this spec (payloads + hashes)."""
    sysinfo = sysinfo or {"favorites": {}, "composites": {}}
    gmap = load_gdl_map()
    fr = frame_of(spec)
    ref = spec["reference"]
    zone = spec["zones"][0]
    zs0, zs1, zv0, zv1 = zone["rect"]["v"]
    wsys = spec["systems"][zone["system"]]
    floor = ref["home_story"]["floorIndex"]
    if floor not in story_levels:
        raise TapirError(f"home story floorIndex {floor} does not exist in the project")
    # Archicad puts the body of a wall with reference line 'Outside' on the RIGHT of begin -> end
    # (measured on AC29, 2026-10-19). The body has to go inwards, so the wall runs b -> a when the
    # outside is right of a -> b. centerOffset of an opening is measured from the wall begin.
    reverse = ref["outside"] == "right_of_ab"
    beg_s, end_s = (zs1, zs0) if reverse else (zs0, zs1)
    wall_payload = {
        "begCoordinate": dict(zip("xy", fr.xy(beg_s))),
        "endCoordinate": dict(zip("xy", fr.xy(end_s))),
        "floorIndex": floor,
        "zCoordinate": round(fr.z0 + zv0 - story_levels[floor], 6),
        "height": round(zv1 - zv0, 6),
        "structureType": wsys.get("structure", "Basic"),
        "referenceLineLocation": wsys.get("reference_line", "Outside"),
    }
    if wall_payload["structureType"] == "Basic":
        wall_payload["thickness"] = zone["thickness"]["v"]
    wfav = sysinfo["favorites"].get(zone["system"])
    if fs_system(wsys) and not wfav:
        raise TapirError(f"system {zone['system']}: FS favourite not prepared (systems.ensure)")
    if wsys.get("favorite"):
        wall_payload["favoriteName"] = wsys["favorite"]
    if wall_payload["structureType"] == "Composite":
        wall_payload["compositeId"] = {"guid": sysinfo["composites"][zone["system"]]}
    if wall_payload["referenceLineLocation"] != "Outside":
        raise TapirError("only referenceLineLocation 'Outside' is supported (the opening offsets assume it)")
    wall_id = element_id(spec, zone, zone["rect"]["conf"], zone["thickness"]["conf"])
    zdroj, istota = _zdroj([("poloha/rozmer", zone["rect"]), ("hrúbka", zone["thickness"]), ("skladba", wsys)])
    wall = {"item": zone["id"], "kind": "wall", "payload": wall_payload, "element_id": wall_id, "layer": ref["layer"],
            "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{zone['id']}"},
            "fav_hash": (wfav or {}).get("hash")}
    wall["hash"] = _hash([wall_payload, wall_id, ref["layer"], wall["props"], wall["fav_hash"]])

    openings, exceptions = [], []
    for u in opening_units(spec):
        o = u["o"]
        if o["kind"] not in ("window", "door"):
            raise TapirError(f"openings[{o['id']}]: kind '{o['kind']}' is not supported yet")
        s0, s1, v0, v1 = u["rect"]
        sc = 0.5 * (s0 + s1)
        osys = spec["systems"][o["system"]]
        payload = {
            "centerOffset": round((beg_s - sc) if reverse else (sc - beg_s), 6),
            "width": round(s1 - s0, 6),
            "height": round(v1 - v0, 6),
            "sillHeight": round(v0 - zv0, 6),
        }
        if osys.get("favorite"):
            payload["favoriteName"] = osys["favorite"]
        ofav = sysinfo["favorites"].get(o["system"])
        if fs_system(osys) and not ofav:
            raise TapirError(f"system {o['system']}: FS favourite not prepared (systems.ensure)")
        gdl = [NOMINAL_WALLHOLE] if (o["kind"] == "door" and osys.get("nominal") == "wallhole") else []
        inst, reveal, reveal_spec = [], None, None
        if osys.get("libpart"):                               # I2 system: instance values through gdl_param_map
            lp = gmap["libparts"][osys["libpart"]]
            for k, val in u["values"].items():
                rule = lp["instance"].get(k)
                if rule is None:
                    raise TapirError(f"openings[{o['id']}].{k}: {osys['libpart']} has no mapped parameter for it")
                if "unsupported" in rule:
                    exceptions.append({"system": o["system"], "item": o["id"], "field": k, "spec": val["v"], "model": None,
                                       "reason": f"{osys['libpart']}: {rule['unsupported']}"})
                    continue
                inst.append({"field": k, "value": val["v"], "write": rule["write"], "read": rule["read"]})
            rev = gmap["common"][o["kind"]]["reveal"]
            if "reveal" in o:                                # I3: native, measured from the outside face (Side)
                reveal = dict(rev["element_fields"], **{rev["depth_field"]: round(o["reveal"]["v"], 6)})
                reveal_spec = o["reveal"]["v"]
        confs = [o["rect"]["conf"]] + [m["rect"]["conf"] for m in u["members"]]
        eid = element_id(spec, o, *confs)
        zdroj, istota = _zdroj([("poloha/rozmer", o["rect"])] + [(f"poloha/rozmer {m['id']}", m["rect"]) for m in u["members"]]
                               + [("typ", osys), ("hĺbka osadenia", o.get("reveal"))]
                               + [(k, v) for k, v in u["values"].items()])
        item = {"item": o["id"], "kind": o["kind"], "payload": payload, "gdl": gdl, "inst": inst, "reveal": reveal, "reveal_spec": reveal_spec,
                "element_id": eid, "layer": ref["layer"], "members": [m["id"] for m in u["members"]],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota,
                          "FS_spec": f"{spec['id']}." + "+".join([o["id"]] + [m["id"] for m in u["members"]])},
                "fav_hash": (ofav or {}).get("hash")}
        item["hash"] = _hash([payload, gdl, inst, reveal, eid, ref["layer"], item["props"], item["fav_hash"], wall["hash"]])
        openings.append(item)
    linear = plan_linear(spec, fr, floor, story_levels[floor], sysinfo) if spec.get("linear") else []
    return {"wall": wall, "openings": openings, "linear": linear, "exceptions": exceptions}


def plan_linear(spec: dict, fr, floor: int, level: float, sysinfo: dict) -> list[dict]:
    """Payloads of the I3 linear elements: Beam a->b (or b->a) with the profile on the outside, Column rotated so that
    its local x is the outward normal; z relative to the home story."""
    out = []
    ref = spec["reference"]
    right = ref["outside"] == "right_of_ab"
    for it in spec.get("linear", []):
        sysdef = spec["systems"][it["system"]]
        prof = spec["systems"][sysdef["profile"]]
        pinfo = sysinfo.get("profiles", {}).get(sysdef["profile"])
        if pinfo is None:
            raise TapirError(f"linear[{it['id']}]: profile {sysdef['profile']} not prepared (systems.ensure)")
        fav = sysinfo["favorites"].get(it["system"])
        if fav is None:
            raise TapirError(f"linear[{it['id']}]: favourite of {it['system']} not prepared (systems.ensure)")
        s0, s1 = it["s"]["v"]
        if it["axis"] == "h":
            a, b = (s0, s1) if right else (s1, s0)                 # Archicad puts the profile x on the RIGHT of beg -> end
            payload = {"begCoordinate": dict(zip("xy", fr.xy(a))), "endCoordinate": dict(zip("xy", fr.xy(b))),
                       "floorIndex": floor, "zCoordinate": round(fr.z0 + it["v"]["v"] - level, 6),
                       "profileId": {"guid": pinfo["guid"]}, "favoriteName": sysdef["favorite"]}
            tool = "beam"
        else:
            v0, v1 = it["v"]["v"]
            # profile x -> outward normal n, profile y -> n turned +90 deg = +d (outside right of a->b) or -d
            origin = s0 if right else s1
            payload = {"coordinates": dict(zip("xy", fr.xy(origin)), z=round(fr.z0 + v0 - level, 6)), "floorIndex": floor,
                       "height": round(v1 - v0, 6), "axisRotationAngle": round(math.atan2(fr.n[1], fr.n[0]), 9),
                       "profileId": {"guid": pinfo["guid"]}, "favoriteName": sysdef["favorite"]}
            tool = "column"
        eid = element_id(spec, it, it["s"]["conf"], it["v"]["conf"], prof["conf"])
        parts = [("poloha s", it["s"]), ("výška v", it["v"]), ("profil vyloženie", prof.get("w")), ("profil výška/šírka", prof.get("h")),
                 ("profil/systém", {"src": prof.get("src"), "conf": prof["conf"]})]
        zdroj, istota = _zdroj(parts)
        item = {"item": it["id"], "kind": tool, "payload": payload, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{it['id']}"},
                "fav_hash": fav["hash"], "profile_guid": pinfo["guid"], "spec_item": it}
        item["hash"] = _hash([payload, eid, ref["layer"], item["props"], item["fav_hash"], pinfo["hash"]])
        out.append(item)
    return out


def linear_readback(item: dict, det: dict, tol: float) -> list[str]:
    """Placement of a beam / column read back = payload (z relative to the story, profile, column height/rotation)."""
    bad = []
    p = item["payload"]
    if item["profile_guid"].lower() not in json.dumps(det).lower():
        bad.append(f"{item['item']}: profile {item['profile_guid']} not on the element")
    if item["kind"] == "beam":
        checks = [("begCoordinate.x", det["begCoordinate"]["x"], p["begCoordinate"]["x"]),
                  ("begCoordinate.y", det["begCoordinate"]["y"], p["begCoordinate"]["y"]),
                  ("endCoordinate.x", det["endCoordinate"]["x"], p["endCoordinate"]["x"]),
                  ("endCoordinate.y", det["endCoordinate"]["y"], p["endCoordinate"]["y"]),
                  ("level", det["level"], p["zCoordinate"])]
    else:
        checks = [("origin.x", det["origin"]["x"], p["coordinates"]["x"]), ("origin.y", det["origin"]["y"], p["coordinates"]["y"]),
                  ("bottomOffset", det["bottomOffset"], p["coordinates"]["z"]), ("height", det["height"], p["height"]),
                  ("axisRotationAngle", math.remainder(det["axisRotationAngle"] - p["axisRotationAngle"], 2 * math.pi), 0.0)]
    for name, got, sent in checks:
        if abs(got - sent) > tol:
            bad.append(f"{item['item']}: {name} {got:.4f} != sent {sent:.4f}")
    return bad


# ---- Archicad helpers -----------------------------------------------------------------------------
def ensure_layer(ac: TapirClient, name: str, log: list) -> tuple[int, bool]:
    def find():
        for a in ac.tapir("GetAttributesByType", {"attributeType": "Layer"})["attributes"]:
            if a["name"] == name:
                return a["index"]
        return None

    index = find()
    if index is not None:
        return index, False
    # same intersection group as the construction layers (modeller rules 17, 26): never 0
    res = ac.tapir("CreateLayers", {"layerDataArray": [{"name": name, "intersectionGroupNr": 1}]})
    log.append(f"layer created: {name} ({json.dumps(res, ensure_ascii=False)[:120]})")
    index = find()
    if index is None:
        raise TapirError(f"layer '{name}' was not created")
    return index, True


def existing(ac: TapirClient, guids: list[str]) -> dict[str, dict]:
    """guid -> details (type, floorIndex, layerIndex, id, details) for the GUIDs that still exist."""
    if not guids:
        return {}
    res = ac.tapir("GetDetailsOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids],
                                            "fields": ["type", "id", "floorIndex", "layerIndex", "details"]}, timeout=60)
    out = {}
    for g, d in zip(guids, res["detailsOfElements"]):
        if isinstance(d, dict) and "type" in d:
            out[g] = d
    return out


def _created_guids(response: dict, what: str) -> list[str]:
    guids, errors = [], []
    for e in response["elements"]:
        if "elementId" in e:
            guids.append(e["elementId"]["guid"])
        else:
            errors.append(json.dumps(e, ensure_ascii=False))
    if errors:
        raise TapirError(f"{what}: {len(errors)} failed: {errors}")
    return guids


def gdl_params(ac: TapirClient, guids: list[str]) -> dict[str, dict]:
    if not guids:
        return {}
    res = ac.tapir("GetGDLParametersOfElements", {"elements": [{"elementId": {"guid": g}} for g in guids]}, timeout=90)
    out = {}
    for g, ps in zip(guids, res["gdlParametersOfElements"]):
        if "parameters" not in ps:
            raise TapirError(f"GetGDLParametersOfElements {g}: {ps}")
        out[g] = {p["name"]: p.get("value") for p in ps["parameters"]}
    return out


def instance_writes(item: dict, params: dict) -> list[dict]:
    writes = []
    for i in item.get("inst", []):
        writes += fs_systems.write_params(i["write"], i["value"], None, params)
    return writes


def finish_elements(ac: TapirClient, items: list[tuple[str, dict]], layer_index: int, elementid_prop: dict,
                    prop_ids: dict) -> None:
    """Layer, Element ID, GDL (nominal + instance values), wall inset and FS_* properties of fresh elements."""
    if not items:
        return
    res = ac.tapir("SetDetailsOfElements", {"elementsWithDetails": [
        {"elementId": {"guid": g}, "details": {"layerIndex": layer_index}} for g, _ in items]})
    if errs := results_ok(res["executionResults"]):
        raise TapirError(f"SetDetailsOfElements (layer): {errs}")
    res = ac.tapir("SetPropertyValuesOfElements", {"elementPropertyValues": [
        {"elementId": {"guid": g}, "propertyId": elementid_prop, "propertyValue": {"value": it["element_id"]}} for g, it in items]})
    if errs := results_ok(res["executionResults"]):
        raise TapirError(f"SetPropertyValuesOfElements (Element ID): {errs}")
    need_params = [g for g, it in items if it.get("inst")]
    params = gdl_params(ac, need_params)
    gdl = []
    for g, it in items:
        writes = list(it.get("gdl", [])) + (instance_writes(it, params[g]) if g in params else [])
        if writes:
            gdl.append({"elementId": {"guid": g}, "gdlParameters": writes})
    if gdl:
        res = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": gdl})
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"SetGDLParametersOfElements: {errs}")
    for kind, command, key in (("window", "ModifyWindows", "windowsWithDetails"), ("door", "ModifyDoors", "doorsWithDetails")):
        mods = [dict(it["reveal"], elementId={"guid": g}) for g, it in items if it["kind"] == kind and it.get("reveal")]
        if mods:
            res = ac.tapir(command, {key: mods})
            if errs := results_ok(res["executionResults"]):
                raise TapirError(f"{command} (wall inset): {errs}")
    if prop_ids:
        vals = [{"elementId": {"guid": g}, "propertyId": {"guid": prop_ids[n]}, "propertyValue": {"value": v}}
                for g, it in items for n, v in it.get("props", {}).items()]
        res = ac.tapir("SetPropertyValuesOfElements", {"elementPropertyValues": vals})
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"SetPropertyValuesOfElements (FS_*): {errs}")


# ---- main build ------------------------------------------------------------------------------------
def build(spec: dict, project: str = DEFAULT_PROJECT, port: int = 19723, out_root: Path = DEFAULT_OUT,
          save: bool = False, force: bool = False, verbose: bool = True) -> dict:
    t0 = time.monotonic()
    errors, warnings = validate(spec)
    if errors:
        raise TapirError("spec is invalid:\n  " + "\n  ".join(errors))
    log: list[str] = [f"WARNING: {w}" for w in warnings]
    ac = TapirClient(port=port, timeout=90)
    path = ac.guard_project(project)
    version = ac.addon_version()
    stories = ac.tapir("GetStories")["stories"]
    story_levels = {s["index"]: s["level"] for s in stories}

    sysinfo = fs_systems.ensure(ac, spec, path, out_root, log)
    want = plan(spec, story_levels, sysinfo)

    spath = state_path(spec, out_root)
    state = load_state(spath, spec, path)
    changes = {"layer_created": 0, "created": 0, "deleted": 0, "kept": 0}
    changes.update({f"systems_{k}": v for k, v in sysinfo["changes"].items()})

    layer_index, layer_new = ensure_layer(ac, spec["reference"]["layer"], log)
    changes["layer_created"] += int(layer_new)
    state["layer"] = {"name": spec["reference"]["layer"], "index": layer_index}

    known = state["elements"]
    alive = existing(ac, [e["guid"] for e in known.values()])
    elementid_prop = ac.api("API.GetPropertyIds", {"properties": [ELEMENT_ID_PROPERTY]})["properties"][0]["propertyId"]

    def drop(item_id: str, reason: str):
        entry = known.pop(item_id)
        if entry["guid"] in alive:
            res = ac.tapir("DeleteElements", {"elements": [{"elementId": {"guid": entry["guid"]}}]})
            if errs := results_ok(res["executionResults"]):
                raise TapirError(f"DeleteElements {item_id}: {errs}")
            changes["deleted"] += 1
            alive.pop(entry["guid"], None)
            log.append(f"deleted {item_id} {entry['guid']} ({reason})")
        else:
            log.append(f"forgot {item_id} {entry['guid']} (not in the project any more; {reason})")

    wanted_ids = {want["wall"]["item"]} | {o["item"] for o in want["openings"]} | {it["item"] for it in want["linear"]}
    for item_id in [k for k in known if k not in wanted_ids]:
        drop(item_id, "not in the spec any more (or merged into another opening)")

    def up_to_date(item: dict) -> bool:
        entry = known.get(item["item"])
        return bool(entry) and not force and entry["hash"] == item["hash"] and entry["guid"] in alive

    # ---- wall
    wall = want["wall"]
    created_now: list[tuple[str, dict]] = []
    if up_to_date(wall):
        changes["kept"] += 1
        wall_guid = known[wall["item"]]["guid"]
    else:
        for o in want["openings"]:                         # openings die with their wall
            if o["item"] in known:
                drop(o["item"], "owner wall is rebuilt")
        if wall["item"] in known:
            drop(wall["item"], "changed" if not force else "--force")
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})     # modeller trap: create from the 3D window
        wall_guid = _created_guids(ac.tapir("CreateWalls", {"wallsData": [wall["payload"]]}), "CreateWalls")[0]
        known[wall["item"]] = {"guid": wall_guid, "kind": "wall", "hash": wall["hash"], "element_id": wall["element_id"]}
        created_now.append((wall_guid, wall))
        changes["created"] += 1
        save_state(spath, state)
        log.append(f"created wall {wall['element_id']} {wall_guid}")

    # ---- openings
    todo = {"window": [], "door": []}
    for o in want["openings"]:
        entry = known.get(o["item"])
        owner_ok = entry and entry["guid"] in alive and \
            alive[entry["guid"]]["details"].get("ownerElementId", {}).get("guid") == wall_guid
        if up_to_date(o) and owner_ok:
            changes["kept"] += 1
            continue
        if entry:
            drop(o["item"], "changed")
        todo[o["kind"]].append(o)
    for kind, command, key in (("window", "CreateWindows", "windowsData"), ("door", "CreateDoors", "doorsData")):
        if not todo[kind]:
            continue
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})
        data = [dict(o["payload"], ownerWallId={"guid": wall_guid}) for o in todo[kind]]
        guids = _created_guids(ac.tapir(command, {key: data}, timeout=120), command)
        for g, o in zip(guids, todo[kind]):
            known[o["item"]] = {"guid": g, "kind": kind, "hash": o["hash"], "element_id": o["element_id"]}
            created_now.append((g, o))
            changes["created"] += 1
            log.append(f"created {kind} {o['element_id']} {g}")
        save_state(spath, state)

    # ---- linear elements (I3): independent of the wall, rebuilt only when their own hash changes
    todo_lin = {"beam": [], "column": []}
    for item in want["linear"]:
        if up_to_date(item):
            changes["kept"] += 1
            continue
        if item["item"] in known:
            drop(item["item"], "changed" if not force else "--force")
        todo_lin[item["kind"]].append(item)
    for kind, command, key in (("beam", "CreateBeams", "beamsData"), ("column", "CreateColumns", "columnsData")):
        if not todo_lin[kind]:
            continue
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})
        guids = _created_guids(ac.tapir(command, {key: [it["payload"] for it in todo_lin[kind]]}, timeout=120), command)
        for g, it in zip(guids, todo_lin[kind]):
            known[it["item"]] = {"guid": g, "kind": kind, "hash": it["hash"], "element_id": it["element_id"]}
            created_now.append((g, it))
            changes["created"] += 1
            log.append(f"created {kind} {it['element_id']} {g}")
        save_state(spath, state)

    # FS_* availability for the classification items of the facade elements, then finish every element whose
    # state entry is not marked finished (fresh ones, and any left over by an interrupted earlier run)
    changes["systems_availability"] = fs_systems.ensure_availability(
        ac, sysinfo["props"], [e["guid"] for e in known.values()], sysinfo["manifest"], log)
    fs_systems.save_manifest(out_root, sysinfo["manifest"])
    by_item = {it["item"]: it for it in [wall] + want["openings"] + want["linear"]}
    unfinished = [(known[i]["guid"], by_item[i]) for i in by_item if not known[i].get("finished")]
    finish_elements(ac, unfinished, layer_index, elementid_prop, sysinfo["props"])
    for g, it in unfinished:
        known[it["item"]]["finished"] = True
        if (g, it) not in created_now:
            changes["finished_late"] = changes.get("finished_late", 0) + 1
    save_state(spath, state)

    # ---- quick read-back of what was sent (full comparison is verify.py)
    readback = existing(ac, [e["guid"] for e in known.values()])
    params = gdl_params(ac, [known[o["item"]]["guid"] for o in want["openings"] if o.get("inst")])
    mismatches = []
    tol = spec["tolerances"]["build"]
    for item in [wall] + want["openings"] + want["linear"]:
        g = known[item["item"]]["guid"]
        d = readback.get(g)
        if d is None:
            mismatches.append(f"{item['item']}: element {g} missing after build")
            continue
        det = d["details"]
        if d["layerIndex"] != layer_index:
            mismatches.append(f"{item['item']}: layer {d['layerIndex']} != {layer_index}")
        if d["id"] != item["element_id"]:
            mismatches.append(f"{item['item']}: Element ID '{d['id']}' != '{item['element_id']}'")
        if item["kind"] in ("beam", "column"):
            mismatches += linear_readback(item, det, tol)
            continue
        keys = (("height", "height"), ("bottomOffset", "zCoordinate")) if item["kind"] == "wall" else \
            tuple((k, k) for k in ("centerOffset", "width", "height", "sillHeight"))
        for k_read, k_sent in keys:
            if abs(det[k_read] - item["payload"][k_sent]) > tol:
                mismatches.append(f"{item['item']}: {k_read} {det[k_read]:.4f} != sent {item['payload'][k_sent]:.4f}")
        if item["kind"] == "wall" and "compositeId" in item["payload"]:
            if det.get("structureType") != "Composite" or (det.get("compositeId") or {}).get("guid") != item["payload"]["compositeId"]["guid"]:
                mismatches.append(f"{item['item']}: structure {det.get('structureType')} / composite {det.get('compositeId')} != sent")
        p = params.get(g)
        for i in item.get("inst", []) if p else []:
            got = fs_systems.read_field(i["read"], p)
            if not fs_systems.same(got, i["value"], tol):
                mismatches.append(f"{item['item']}: {i['field']} reads {got!r} != spec {i['value']!r}")
        if item.get("reveal"):
            if abs(det.get("revealDepthOffset", -1) - item["reveal_spec"]) > tol or det.get("refSide") is not True \
                    or det.get("revealDepthLocation") != "Side":
                mismatches.append(f"{item['item']}: wall inset {det.get('revealDepthOffset')} / {det.get('revealDepthLocation')} / "
                                  f"refSide {det.get('refSide')} != {item['reveal_spec']} / Side / True")

    if save:
        res = ac.tapir("SaveProject", {}, timeout=300)
        log.append(f"SaveProject: {res}")
    n_sys = sum(v for k, v in changes.items() if k.startswith("systems_"))
    state["last_build"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "addon": version, "changes": changes,
                           "seconds": round(time.monotonic() - t0, 2)}
    save_state(spath, state)
    result = {"changes": changes,
              "n_changes": changes["layer_created"] + changes["created"] + changes["deleted"] + changes.get("finished_late", 0) + n_sys,
              "seconds": round(time.monotonic() - t0, 2), "mismatches": mismatches, "log": log,
              "exceptions": sysinfo["exceptions"] + want["exceptions"], "state": str(spath), "api_calls": ac.calls, "addon": version}
    if verbose:
        for line in log:
            print(" ", line)
        print(f"build {spec['id']}: {result['n_changes']} changes {changes}, {result['seconds']} s, "
              f"{ac.calls} API calls, read-back mismatches: {len(mismatches)}")
        for m in mismatches:
            print("  MISMATCH:", m)
        for e in result["exceptions"]:
            print(f"  EXCEPTION: {e.get('item', e['system'])}.{e['field']}: spec {e['spec']!r}, model {e['model']!r} - {e['reason'][:110]}")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--project", default=DEFAULT_PROJECT)
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--save", action="store_true", help="SaveProject at the end")
    ap.add_argument("--force", action="store_true", help="rebuild every element of the spec")
    args = ap.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    result = build(spec, args.project, args.port, args.out, args.save, args.force)
    return 0 if not result["mismatches"] else 1


if __name__ == "__main__":
    sys.exit(main())

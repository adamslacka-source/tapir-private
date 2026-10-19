"""build: facade-spec/1 -> Archicad elements (I1: one wall + windows + doors from existing Favorites).

Idempotent: every created element is recorded in <out>/<spec.id>/state.json with a hash of what
was sent. A second run with the same spec changes nothing. Only GUIDs from the state file are
ever deleted - elements this script did not create are never touched.

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
from geom import frame_of  # noqa: E402
from spec_validate import element_id, validate  # noqa: E402
from tapir_client import TapirClient, TapirError, results_ok  # noqa: E402

DEFAULT_PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
DEFAULT_OUT = Path(r"C:\FacadePilot\out")
STATE_VERSION = 1
ELEMENT_ID_PROPERTY = {"type": "BuiltIn", "nonLocalizedName": "General_ElementID"}
NOMINAL_WALLHOLE = {"name": "gs_nominal_size", "value": 1}      # Door 27 family: 1 = wall hole, 8 = egress (favorite default)


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


# ---- plan: spec -> Archicad payloads ----------------------------------------------------------
def plan(spec: dict, story_levels: dict[int, float]) -> dict:
    """Pure function: what has to exist in Archicad for this spec (payloads + hashes)."""
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
        "thickness": zone["thickness"]["v"],
        "structureType": wsys.get("structure", "Basic"),
        "referenceLineLocation": wsys.get("reference_line", "Outside"),
    }
    if wsys.get("favorite"):
        wall_payload["favoriteName"] = wsys["favorite"]
    if wall_payload["referenceLineLocation"] != "Outside":
        raise TapirError("I1 supports only referenceLineLocation 'Outside' (the opening offsets assume it)")
    wall_id = element_id(spec, zone, zone["rect"]["conf"], zone["thickness"]["conf"])
    wall = {"item": zone["id"], "kind": "wall", "payload": wall_payload, "element_id": wall_id, "layer": ref["layer"]}
    wall["hash"] = _hash([wall_payload, wall_id, ref["layer"]])

    openings = []
    for o in spec["openings"]:
        if o["kind"] not in ("window", "door"):
            raise TapirError(f"openings[{o['id']}]: kind '{o['kind']}' is not supported in I1")
        s0, s1, v0, v1 = o["rect"]["v"]
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
        gdl = [NOMINAL_WALLHOLE] if (o["kind"] == "door" and osys.get("nominal") == "wallhole") else []
        eid = element_id(spec, o, o["rect"]["conf"])
        item = {"item": o["id"], "kind": o["kind"], "payload": payload, "gdl": gdl, "element_id": eid, "layer": ref["layer"]}
        item["hash"] = _hash([payload, gdl, eid, ref["layer"], wall["hash"]])
        openings.append(item)
    return {"wall": wall, "openings": openings}


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


def finish_elements(ac: TapirClient, items: list[tuple[str, dict]], layer_index: int, elementid_prop: dict) -> None:
    """Layer, Element ID and GDL fixes of freshly created elements."""
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
    gdl = [{"elementId": {"guid": g}, "gdlParameters": it["gdl"]} for g, it in items if it.get("gdl")]
    if gdl:
        res = ac.tapir("SetGDLParametersOfElements", {"elementsWithGDLParameters": gdl})
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"SetGDLParametersOfElements: {errs}")


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
    want = plan(spec, story_levels)

    spath = state_path(spec, out_root)
    state = load_state(spath, spec, path)
    changes = {"layer_created": 0, "created": 0, "deleted": 0, "kept": 0}

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

    wanted_ids = {want["wall"]["item"]} | {o["item"] for o in want["openings"]}
    for item_id in [k for k in known if k not in wanted_ids]:
        drop(item_id, "not in the spec any more")

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

    # layer, Element ID and GDL fixes: every element whose state entry is not marked finished
    # (fresh ones, and any left over by an interrupted earlier run)
    by_item = {it["item"]: it for it in [wall] + want["openings"]}
    unfinished = [(known[i]["guid"], by_item[i]) for i in by_item if not known[i].get("finished")]
    finish_elements(ac, unfinished, layer_index, elementid_prop)
    for g, it in unfinished:
        known[it["item"]]["finished"] = True
        if (g, it) not in created_now:
            changes["finished_late"] = changes.get("finished_late", 0) + 1
    save_state(spath, state)

    # ---- quick read-back of what was sent (full comparison is verify.py)
    readback = existing(ac, [e["guid"] for e in known.values()])
    mismatches = []
    for item in [wall] + want["openings"]:
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
        keys = (("height", "height"), ("bottomOffset", "zCoordinate")) if item["kind"] == "wall" else             tuple((k, k) for k in ("centerOffset", "width", "height", "sillHeight"))
        for k_read, k_sent in keys:
            if abs(det[k_read] - item["payload"][k_sent]) > spec["tolerances"]["build"]:
                mismatches.append(f"{item['item']}: {k_read} {det[k_read]:.4f} != sent {item['payload'][k_sent]:.4f}")

    if save:
        res = ac.tapir("SaveProject", {}, timeout=300)
        log.append(f"SaveProject: {res}")
    state["last_build"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "addon": version, "changes": changes,
                           "seconds": round(time.monotonic() - t0, 2)}
    save_state(spath, state)
    result = {"changes": changes, "n_changes": changes["layer_created"] + changes["created"] + changes["deleted"] + changes.get("finished_late", 0),
              "seconds": round(time.monotonic() - t0, 2), "mismatches": mismatches, "log": log,
              "state": str(spath), "api_calls": ac.calls, "addon": version}
    if verbose:
        for line in log:
            print(" ", line)
        print(f"build {spec['id']}: {result['n_changes']} changes {changes}, {result['seconds']} s, "
              f"{ac.calls} API calls, read-back mismatches: {len(mismatches)}")
        for m in mismatches:
            print("  MISMATCH:", m)
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

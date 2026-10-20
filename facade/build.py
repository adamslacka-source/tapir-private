"""build: facade-spec/1 -> Archicad elements.

I1: one wall + windows + doors from existing Favorites.
I2: first the catalogue `systems` (systems.py: Composite, FS Favorites, FS_* properties), then the elements are
    placed from the FS Favorites; per element the instance values of the spec are written (transom height,
    sidelight, glazing bar, wall inset of the frame) and the properties FS_zdroj / FS_istota / FS_spec.
    Openings with `merged_into` are built as ONE element with their host (e.g. door + shop window = sidelight).
I3: the wall inset of the frames is native (revealDepthLocation 'Side' + revealDepthOffset, Tapir T8) - no
    subtraction of the outer skins; `linear` elements (cornice, string course, sill ledges) are profiled
    Beams (horizontal: profile x = out of the facade, y = up) and Columns (vertical: profile x = out, y = along the
    facade) with the FS profile, placed from their FS favourite.
Pilot A: one Wall per wall ZONE (own build-up, own plane, outside face offset w: stone cladding of the ground floor,
    rizalit, side facade at a corner); every opening goes into the wall of its zone; linear elements lie on their
    plane at offset w; GDL `objects` (FS_Surround, FS_ArchGable) are placed by CreateObjects from the FS template
    loaded by systems.ensure (gdl_build) with the angle of their plane, then their GDL parameters are written.

I5/I6 (pilot A): a glazed field (cw_fields) is a Curtain Wall from its FS favourite 'FS cw.*' (frame / panel classes made
    by systems.py) placed in a rectangular hole of the wall of its zone - an 'Empty Window' (a CW does not cut a wall
    through the API, checked 2026-10-19); the raster = primary / secondary pattern of the field (FixedSizes), the cells
    get their panel class (glass, door panel 'CW Door 27' / 'CW Double Door 27'), the outer face of the frames lies
    `reveal` behind the outside face of the zone wall. Balconies are slabs from 'FS slab.*' (back edge on the wall
    faces, circular front as a polygon arc), railings are CreateRailings from 'FS rail.*' along the reference line of
    the spec (height and post spacing sent again: the favourite does not carry the post spacing reliably).

Pilot B (photo): a curtain wall field without a zone is freestanding (no wall hole; outer face of the frames `reveal`
    behind the plane); `slabs` (walkways, slab edge strips) are slabs from their (s, w) outline; `linear` with axis 'w'
    is a beam perpendicular to the facade (bracket); GDL louvres (FS_Louvers) are objects. When every story of the
    spec carries a floorIndex, the project stories are set from the spec (SetStories) and every element gets the story
    its foot stands on (modeller rule 2), z relative to that story.

Idempotent: every created element is recorded in <out>/<spec.id>/state.json with a hash of what was sent
(including the hash of the favourite / template it comes from). A second run with the same spec changes nothing.
Only GUIDs from the state file are ever deleted - elements this script did not create are never touched.

    python build.py <spec.json> [--project C:/FacadePilot/Fasada_pilot_A.pln] [--port 19723] [--save] [--force]

Writes only when the open project is exactly --project (never the template in C:/ArchicadTemp).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gdl_expect  # noqa: E402
import systems as fs_systems  # noqa: E402
from geom import REFERENCE_PLANE, balcony_outline, cw_cell_panels, cw_grid, frames_of, item_plane, outside_of, zone_w  # noqa: E402
from spec_validate import element_id, fs_system, load_gdl_map, lowest_conf, object_values, validate  # noqa: E402
from tapir_client import TapirClient, TapirError, results_ok  # noqa: E402

DEFAULT_PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
DEFAULT_OUT = Path(r"C:\FacadePilot\out")
STATE_VERSION = 1
ELEMENT_ID_PROPERTY = {"type": "BuiltIn", "nonLocalizedName": "General_ElementID"}
NOMINAL_WALLHOLE = {"name": "gs_nominal_size", "value": 1}      # Door 27 family: 1 = wall hole, 8 = egress (favorite default)
EMPTY_OPENING = "Empty Window"          # library part of the hole of a curtain wall field (window type, no frame, no 3D body)
ANGLE_PARAMS = ("lamAng",)              # Angle-type parameters of the FS GDL templates
INSTANCE_KEYS = ("transom_h", "sidelight_w", "sidelight_sill", "transom_bars", "transom_cols", "transom_bar_h", "bar_h", "shoulder_h")


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
def zones_of(spec: dict) -> dict:
    return {z["id"]: z for z in spec["zones"]}


def zone_of(spec: dict, o: dict) -> dict:
    zs = zones_of(spec)
    return zs[o["zone"]] if o.get("zone") else spec["zones"][0]


def opening_units(spec: dict) -> list[dict]:
    """One entry per element to build: host opening + the openings merged into it, union rect, derived values, zone."""
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
        z = zone_of(spec, o)
        # the part of the hole inside the wall: an arched head carried by an arch gable ends at the springing (zone top)
        hole = [s0, s1, v0, min(v1, z["rect"]["v"][3]) if o.get("head_in") else v1]
        units.append({"o": o, "members": members, "rect": [s0, s1, v0, v1], "hole": hole, "values": values, "zone": z["id"],
                      "plane": z.get("plane", REFERENCE_PLANE)})
    zs = zones_of(spec)
    for cw in spec.get("cw_fields", []):                     # I5: the hole of a glazed field (Empty Window in the zone wall)
        if not cw.get("zone"):                                # pilot B: freestanding curtain wall, no hole
            continue
        z = zs[cw["zone"]]
        o = {"id": cw["id"], "kind": "opening", "rect": cw["rect"], "zone": cw["zone"], "system": None, "cw": cw["id"],
             "photo_edges": cw.get("photo_edges", [])}
        units.append({"o": o, "members": [], "rect": list(cw["rect"]["v"]), "hole": list(cw["rect"]["v"]), "values": {}, "zone": z["id"],
                      "plane": z.get("plane", REFERENCE_PLANE)})
    return units


def unit_reveal(spec: dict, u: dict) -> float | None:
    """Wall inset of the frame of a unit (opening: `reveal`; hole of a glazed field: `reveal` of the field) - the depth of
    the reveal whose visible width on an oblique photo is the parallax of the edge (photo_measure.visible_reveal)."""
    o = u["o"]
    if o.get("cw"):
        cw = next((c for c in spec.get("cw_fields", []) if c["id"] == o["cw"]), {})
        return (cw.get("reveal") or {}).get("v")
    return (o.get("reveal") or {}).get("v")


def _zdroj(parts: list[tuple[str, dict | None]]) -> tuple[str, str]:
    """FS_zdroj text and FS_istota (lowest certainty) from (label, {src, conf}) pairs."""
    txt, confs = [], []
    for label, val in parts:
        if not val:
            continue
        txt.append(f"{label}: {val.get('src') or '-'} ({val['conf']})")
        confs.append(val["conf"])
    return "; ".join(txt), lowest_conf(*confs) if confs else "default"


def wall_geometry(spec: dict, zone: dict, frames: dict) -> dict:
    """Begin / end s of the wall of a zone and whether it runs b -> a. Archicad puts the body of a wall with reference
    line 'Outside' on the RIGHT of begin -> end (measured on AC29, 2026-10-19): the body has to go inwards, so the wall
    runs b -> a when the outside is right of a -> b. centerOffset of an opening is measured from the wall begin."""
    plane = zone.get("plane", REFERENCE_PLANE)
    zs0, zs1, _, _ = zone["rect"]["v"]
    reverse = outside_of(spec, plane) == "right_of_ab"
    return {"plane": plane, "frame": frames[plane], "reverse": reverse, "beg_s": zs1 if reverse else zs0,
            "end_s": zs0 if reverse else zs1, "w": zone_w(zone)}


class Home:
    """Story of an element from the absolute z of its foot: per-element stories when every spec story has a floorIndex,
    else the reference home story for everything (pilot A)."""

    def __init__(self, spec: dict, story_levels: dict[int, float]):
        self.default = spec["reference"]["home_story"]["floorIndex"]
        if self.default not in story_levels:
            raise TapirError(f"home story floorIndex {self.default} does not exist in the project")
        st = spec["stories"]
        self.per = all("floorIndex" in s for s in st)
        self.levels = story_levels
        self.z0 = spec["reference"]["z0"]["v"]
        if self.per:
            missing = [s["floorIndex"] for s in st if s["floorIndex"] not in story_levels]
            if missing:
                raise TapirError(f"stories {missing} do not exist in the project (ensure_stories)")
            self.idx = sorted((s["floorIndex"] for s in st), key=lambda i: story_levels[i])

    def __call__(self, z_abs: float) -> tuple[int, float]:
        if not self.per:
            return self.default, self.levels[self.default]
        f = self.idx[0]
        for i in self.idx:
            if self.levels[i] <= z_abs + 1e-3:
                f = i
        return f, self.levels[f]

    def top_link(self, floor: int, z_top: float) -> dict | None:
        """Modeller rule 3 (top of a wall linked to a story, not a fixed height): the highest spec story at or below the
        top, at least home + 1; topOffset = top - level of that story (negative when the top lies under home + 1). None
        when there is no spec story above the home story (top story: fixed height, reported) or without spec stories."""
        if not self.per:
            return None
        above = [i for i in self.idx if self.levels[i] > self.levels[floor] + 1e-6]
        if not above:
            return None
        top = above[0]
        for i in above:
            if self.levels[i] <= z_top + 1e-3:
                top = i
        return {"relativeTopStory": int(self.idx.index(top) - self.idx.index(floor)), "topOffset": round(z_top - self.levels[top], 6),
                "top_story": top}


def story_payload(spec: dict, current: list[dict]) -> list[dict] | None:
    """SetStories input that keeps the stories below the spec ones (basements of the template) and replaces the rest
    with the spec stories; None when the project already has exactly them."""
    if not all("floorIndex" in s for s in spec["stories"]):
        return None                                      # pilot A: stories of the template, nothing set
    st = sorted(spec["stories"], key=lambda s: s["floorIndex"])
    z0 = spec["reference"]["z0"]["v"]
    first = st[0]["floorIndex"]
    keep = [c for c in sorted(current, key=lambda c: c["index"]) if c["index"] < first]
    want = [{"index": c["index"], "dispOnSections": c.get("dispOnSections", True), "level": c["level"], "name": c["name"]} for c in keep]
    want += [{"index": s["floorIndex"], "dispOnSections": True, "level": round(z0 + s["level"]["v"], 6), "name": s.get("name", s["id"])} for s in st]
    have = [(c["index"], round(c["level"], 4), c["name"]) for c in sorted(current, key=lambda c: c["index"])]
    if have == [(w["index"], round(w["level"], 4), w["name"]) for w in want]:
        return None
    return want


# ---- plan: spec -> Archicad payloads ----------------------------------------------------------
def plan(spec: dict, story_levels: dict[int, float], sysinfo: dict | None = None) -> dict:
    """Pure function: what has to exist in Archicad for this spec (payloads + hashes)."""
    sysinfo = sysinfo or {"favorites": {}, "composites": {}}
    gmap = load_gdl_map()
    frames = frames_of(spec)
    ref = spec["reference"]
    home = Home(spec, story_levels)
    walls = []
    for zone in spec["zones"]:
        g = wall_geometry(spec, zone, frames)
        fr = g["frame"]
        zs0, zs1, zv0, zv1 = zone["rect"]["v"]
        wsys = spec["systems"][zone["system"]]
        floor, level = home(fr.z0 + zv0)
        wall_payload = {
            "begCoordinate": dict(zip("xy", fr.xy(g["beg_s"], g["w"]))),
            "endCoordinate": dict(zip("xy", fr.xy(g["end_s"], g["w"]))),
            "floorIndex": floor,
            "zCoordinate": round(fr.z0 + zv0 - level, 6),    # CreateWalls: offset from floorIndex (other Create* take absolute z)
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
        confs = [zone["rect"]["conf"], zone["thickness"]["conf"]] + ([zone["w"]["conf"]] if zone.get("w") else [])
        wall_id = element_id(spec, zone, *confs)
        zdroj, istota = _zdroj([("poloha/rozmer", zone["rect"]), ("hrúbka", zone["thickness"]), ("odsadenie líca", zone.get("w")),
                                ("skladba", wsys)])
        top_link = home.top_link(floor, fr.z0 + zv1)
        wall = {"item": zone["id"], "kind": "wall", "payload": wall_payload, "element_id": wall_id, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{zone['id']}"},
                "fav_hash": (wfav or {}).get("hash"), "geom": g, "zone": zone, "z_story": level, "top_link": top_link}
        wall["hash"] = _hash([wall_payload, wall_id, ref["layer"], wall["props"], wall["fav_hash"]] + ([top_link] if top_link else []))
        walls.append(wall)
    wall_by_zone = {w["item"]: w for w in walls}

    openings, exceptions = [], []
    for u in opening_units(spec):
        o = u["o"]
        if o["kind"] == "opening":                          # hole of a glazed field
            wall = wall_by_zone[u["zone"]]
            g = wall["geom"]
            s0, s1, v0, v1 = u["rect"]
            sc = 0.5 * (s0 + s1)
            payload = {"centerOffset": round((g["beg_s"] - sc) if g["reverse"] else (sc - g["beg_s"]), 6), "width": round(s1 - s0, 6),
                       "height": round(v1 - v0, 6), "sillHeight": round(v0 - wall["zone"]["rect"]["v"][2], 6), "libraryPartName": EMPTY_OPENING}
            eid = element_id(spec, {"id": f"{o['id']}.otvor"}, o["rect"]["conf"])
            zdroj, istota = _zdroj([("poloha/rozmer", o["rect"])])
            item = {"item": f"{o['id']}.otvor", "unit": o["id"], "kind": "opening", "payload": payload, "gdl": [], "inst": [], "reveal": None,
                    "reveal_spec": None, "element_id": eid, "layer": ref["layer"], "members": [], "owner": wall["item"],
                    "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{o['id']} (otvor CW)"}, "fav_hash": None}
            item["hash"] = _hash([payload, eid, ref["layer"], item["props"], wall["hash"]])
            openings.append(item)
            continue
        if o["kind"] not in ("window", "door"):
            raise TapirError(f"openings[{o['id']}]: kind '{o['kind']}' is not supported yet")
        wall = wall_by_zone[u["zone"]]
        g = wall["geom"]
        zv0 = wall["zone"]["rect"]["v"][2]
        s0, s1, v0, v1 = u["rect"]
        sc = 0.5 * (s0 + s1)
        osys = spec["systems"][o["system"]]
        payload = {
            "centerOffset": round((g["beg_s"] - sc) if g["reverse"] else (sc - g["beg_s"]), 6),
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
        if not osys.get("libpart"):
            # existing favourite without an FS system (I1 style): the same reveal settings as the FS systems, otherwise the
            # favourite's 'No Reveal - Edge' laps the wall 4-6 cm over the frame and the hole is smaller than the photo opening
            gdl += [{"name": k, "value": v} for k, v in gmap["common"][o["kind"]].get("system_params", {}).items()
                    if k != NOMINAL_WALLHOLE["name"]]
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
                "element_id": eid, "layer": ref["layer"], "members": [m["id"] for m in u["members"]], "owner": wall["item"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota,
                          "FS_spec": f"{spec['id']}." + "+".join([o["id"]] + [m["id"] for m in u["members"]])},
                "fav_hash": (ofav or {}).get("hash")}
        item["hash"] = _hash([payload, gdl, inst, reveal, eid, ref["layer"], item["props"], item["fav_hash"], wall["hash"]])
        openings.append(item)
    linear = plan_linear(spec, frames, home, sysinfo) if spec.get("linear") else []
    objects = plan_objects(spec, frames, home, sysinfo) if spec.get("objects") else []
    cws = plan_cw(spec, frames, home, sysinfo, wall_by_zone) if spec.get("cw_fields") else []
    slabs = plan_balconies(spec, frames, home, sysinfo) if spec.get("balconies") else []
    slabs += plan_slabs(spec, frames, home, sysinfo) if spec.get("slabs") else []
    rails = plan_railings(spec, frames, home, sysinfo) if spec.get("railings") else []
    for r in rails:
        exceptions += r.pop("exceptions")
    return {"walls": walls, "wall": walls[0], "openings": openings, "linear": linear, "objects": objects, "cw": cws, "slabs": slabs,
            "railings": rails, "exceptions": exceptions}


# ---- I5/I6 ---------------------------------------------------------------------------------------------------------
def cw_layout(spec: dict, cw: dict, frames: dict, wall_by_zone: dict) -> dict:
    """Where the curtain wall of a field goes: direction (outer side of the frames = left of the CW reference line ->
    the CW runs b -> a when the outside is right of a -> b), reference line w, the order of the columns along it."""
    z = wall_by_zone[cw["zone"]]["zone"] if cw.get("zone") else None
    plane = z.get("plane", REFERENCE_PLANE) if z else cw.get("plane", REFERENCE_PLANE)
    sysdef = spec["systems"][cw["system"]]
    reverse = outside_of(spec, plane) == "right_of_ab"
    w_face = (zone_w(z) if z else 0.0) - cw["reveal"]["v"]   # outer face of the frames
    w_ref = w_face - sysdef["frame"]["offset"]["v"]           # panel plane = reference line (offset = part in front of it)
    cols, rows = cw_grid(cw)
    return {"plane": plane, "frame": frames[plane], "reverse": reverse, "w_face": w_face, "w_ref": w_ref, "cols": cols, "rows": rows,
            "zone": z}


def plan_cw(spec: dict, frames: dict, home, sysinfo: dict, wall_by_zone: dict) -> list[dict]:
    out = []
    ref = spec["reference"]
    for cw in spec["cw_fields"]:
        sysdef = spec["systems"][cw["system"]]
        fav = sysinfo["favorites"].get(cw["system"])
        if fav is None:
            raise TapirError(f"cw_fields[{cw['id']}]: favourite of {cw['system']} not prepared (systems.ensure)")
        idx = fs_systems.cw_class_index(sysinfo["manifest"], spec, cw["system"])
        names = fs_systems.cw_class_names(cw["system"], sysdef)
        L = cw_layout(spec, cw, frames, wall_by_zone)
        fr = L["frame"]
        s0, s1, v0, v1 = cw["rect"]["v"]
        a, b = (s1, s0) if L["reverse"] else (s0, s1)
        floor, z_story = home(fr.z0 + v0)
        widths = [round(c1 - c0, 6) for c0, c1 in L["cols"]]
        heights = [round(r1 - r0, 6) for r0, r1 in L["rows"]]
        order = list(reversed(range(len(widths)))) if L["reverse"] else list(range(len(widths)))   # CW column j = spec column order[j]
        panels = cw_cell_panels(cw)
        frame_i = idx[names["frame"]]
        cells = []
        for j, c in enumerate(order):
            for r in range(len(heights)):
                cells.append({"columnIndex": j, "rowIndex": r, "panelClassIndex": idx[names["panels"][panels[(c, r)]]],
                              "leftFrameClassIndex": 3 if j == 0 else frame_i, "bottomFrameClassIndex": 3 if r == 0 else frame_i})
        payload = {"favoriteName": sysdef["favorite"],
                   "coordinates": [dict(zip("xy", fr.xy(a, L["w_ref"]))), dict(zip("xy", fr.xy(b, L["w_ref"])))],
                   "zCoordinate": round(fr.z0 + v0, 6), "floorIndex": floor, "height": round(v1 - v0, 6),
                   "primaryPattern": {"sizes": [widths[c] for c in order], "logic": "FixedSizes", "origin": "StartWithPattern"},
                   "secondaryPattern": {"sizes": heights, "logic": "FixedSizes", "origin": "StartWithPattern"}, "cells": cells}
        confs = [cw["rect"]["conf"], cw["reveal"]["conf"]] + [cw[k]["conf"] for k in ("u_axes", "v_axes") if k in cw]
        eid = element_id(spec, {"id": f"{cw['id']}.cw"}, *confs)
        zdroj, istota = _zdroj([("poloha/rozmer", cw["rect"]), ("stĺpiky", cw.get("u_axes")), ("priečky", cw.get("v_axes")),
                                ("hĺbka osadenia", cw["reveal"]), ("systém", sysdef)])
        item = {"item": f"{cw['id']}.cw", "kind": "curtainwall", "payload": payload, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{cw['id']}"},
                "fav_hash": fav["hash"], "spec_item": cw, "layout": L, "order": order, "class_index": idx, "names": names,
                "z_story": z_story}
        item["hash"] = _hash([payload, eid, ref["layer"], item["props"], fav["hash"], idx])
        out.append(item)
    return out


def balcony_plan_polygon(spec: dict, b: dict, fr) -> tuple[list[dict], list[dict]]:
    """Plan polygon of a balcony (counter-clockwise) + its one arc (PolyArc: positive angle = bulge to the right of
    beg -> end on a counter-clockwise polygon, checked 2026-10-19)."""
    o = balcony_outline(spec, b)
    pts = [fr.xy(s_, w_) for s_, w_ in o["pts"]]
    ia, ib = o["arc"][0], o["arc"][1]
    area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
    if area < 0:                                           # clockwise -> reverse, keep the arc ends
        n = len(pts)
        pts = pts[::-1]
        ia, ib = n - 1 - ia, n - 1 - ib
    beg, end = (ia, ib) if (ib - ia) % len(pts) == 1 else (ib, ia)
    crown = fr.xy(*o["crown"])
    (x0, y0), (x1, y1) = pts[beg], pts[end]
    right = (crown[0] - x0) * (y1 - y0) - (crown[1] - y0) * (x1 - x0) > 0
    ang = o["arc"][4] * (1 if right else -1)
    return [{"x": round(x, 6), "y": round(y, 6)} for x, y in pts], [{"begIndex": beg, "endIndex": end, "arcAngle": round(ang, 9)}]


def plan_balconies(spec: dict, frames: dict, home, sysinfo: dict) -> list[dict]:
    out = []
    ref = spec["reference"]
    for b in spec["balconies"]:
        sysdef = spec["systems"][b["system"]]
        fav = sysinfo["favorites"].get(b["system"])
        if fav is None:
            raise TapirError(f"balconies[{b['id']}]: favourite of {b['system']} not prepared (systems.ensure)")
        fr = frames[b.get("plane", REFERENCE_PLANE)]
        poly, arcs = balcony_plan_polygon(spec, b, fr)
        floor, level = home(fr.z0 + b["v_top"]["v"] - sysdef["thickness"]["v"])
        payload = {"favoriteName": sysdef["favorite"], "level": round(fr.z0 + b["v_top"]["v"], 6), "floorIndex": floor,
                   "structureType": sysdef.get("structure", "Basic"), "thickness": sysdef["thickness"]["v"],
                   "polygonCoordinates": poly, "polygonArcs": arcs}
        confs = [b[k]["conf"] for k in ("s", "w_side", "w_crown", "v_top")] + [sysdef["thickness"]["conf"]]
        eid = element_id(spec, b, *confs)
        zdroj, istota = _zdroj([("rozsah s", b["s"]), ("vyloženie bokov", b["w_side"]), ("vyloženie vrcholu", b["w_crown"]),
                                ("vrch dosky", b["v_top"]), ("hrúbka", sysdef["thickness"])])
        item = {"item": b["id"], "kind": "slab", "payload": payload, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{b['id']}"}, "fav_hash": fav["hash"], "spec_item": b,
                "plane": b.get("plane", REFERENCE_PLANE), "level_rel": round(fr.z0 + b["v_top"]["v"] - level, 6)}
        item["hash"] = _hash([payload, eid, ref["layer"], item["props"], fav["hash"]])
        out.append(item)
    return out


def plan_slabs(spec: dict, frames: dict, home, sysinfo: dict) -> list[dict]:
    """Pilot B: slab from its (s, w) outline on a plane (walkway, slab edge strip); counter-clockwise in plan."""
    out = []
    ref = spec["reference"]
    for sl in spec["slabs"]:
        sysdef = spec["systems"][sl["system"]]
        fav = sysinfo["favorites"].get(sl["system"])
        if fav is None:
            raise TapirError(f"slabs[{sl['id']}]: favourite of {sl['system']} not prepared (systems.ensure)")
        fr = frames[sl.get("plane", REFERENCE_PLANE)]
        pts = [fr.xy(s_, w_) for s_, w_ in sl["outline_sw"]["v"]]
        area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
        if area < 0:
            pts = pts[::-1]
        floor, level = home(fr.z0 + sl["v_top"]["v"])            # a slab belongs to the story its top is the floor of
        # CreateSlabs.level is ABSOLUTE (checked 2026-10-19 on story 1: sent 0 -> read back -3.605 relative to the story)
        payload = {"favoriteName": sysdef["favorite"], "level": round(fr.z0 + sl["v_top"]["v"], 6), "floorIndex": floor,
                   "structureType": sysdef.get("structure", "Basic"), "thickness": sysdef["thickness"]["v"],
                   "polygonCoordinates": [{"x": round(x, 6), "y": round(y, 6)} for x, y in pts]}
        confs = [sl["outline_sw"]["conf"], sl["v_top"]["conf"], sysdef["thickness"]["conf"]]
        eid = element_id(spec, sl, *confs)
        zdroj, istota = _zdroj([("obrys", sl["outline_sw"]), ("vrch dosky", sl["v_top"]), ("hrúbka", sysdef["thickness"])])
        item = {"item": sl["id"], "kind": "slab", "payload": payload, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{sl['id']}"}, "fav_hash": fav["hash"], "spec_item": sl,
                "plane": sl.get("plane", REFERENCE_PLANE), "level_rel": round(fr.z0 + sl["v_top"]["v"] - level, 6)}
        item["hash"] = _hash([payload, eid, ref["layer"], item["props"], fav["hash"]])
        out.append(item)
    return out


def plan_railings(spec: dict, frames: dict, home, sysinfo: dict) -> list[dict]:
    out = []
    ref = spec["reference"]
    for r in spec["railings"]:
        sysdef = spec["systems"][r["system"]]
        fav = sysinfo["favorites"].get(r["system"])
        if fav is None:
            raise TapirError(f"railings[{r['id']}]: favourite of {r['system']} not prepared (systems.ensure)")
        fr = frames[r.get("plane", REFERENCE_PLANE)]
        floor, _ = home(fr.z0 + r["v"]["v"])
        payload = {"favoriteName": sysdef["favorite"], "coordinates": [dict(zip("xy", fr.xy(s_, w_))) for s_, w_ in r["path_sw"]["v"]],
                   "zCoordinate": round(fr.z0 + r["v"]["v"], 6), "floorIndex": floor, "height": sysdef["height"]["v"],
                   "postSpacing": sysdef["post_spacing"]["v"], "postDistribution": sysdef.get("post_distribution", "BestDivision")}
        confs = [r["path_sw"]["conf"], r["v"]["conf"], sysdef["height"]["conf"], sysdef["post_spacing"]["conf"]]
        eid = element_id(spec, r, *confs)
        zdroj, istota = _zdroj([("trasa", r["path_sw"]), ("päta", r["v"]), ("výška", sysdef["height"]), ("rozostup stĺpikov", sysdef["post_spacing"]),
                                ("výplň", sysdef.get("infill"))])
        exc = []
        bs, fbs = sysdef.get("baluster_spacing"), sysdef.get("favorite_baluster_spacing")
        if bs and fbs is not None and abs(bs["v"] - fbs) > 0.005:
            exc.append({"system": r["system"], "item": r["id"], "field": "baluster_spacing", "spec": bs["v"], "model": fbs,
                        "reason": f"rozostup tyčiek je daný favoritom '{sysdef['base_favorite']}' ({fbs} m); ModifyRailingDetails.balusterSpacing "
                                  "Archicad odmietne (RailingBalusterSet, chyba -2130313112) - R1: vlastný favorit pripraví architekt"})
        item = {"item": r["id"], "kind": "railing", "payload": payload, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{r['id']}"}, "fav_hash": fav["hash"], "spec_item": r,
                "plane": r.get("plane", REFERENCE_PLANE), "exceptions": exc}
        item["hash"] = _hash([payload, eid, ref["layer"], item["props"], fav["hash"]])
        out.append(item)
    return out


def plan_linear(spec: dict, frames: dict, home, sysinfo: dict) -> list[dict]:
    """Payloads of the I3 linear elements: Beam a->b (or b->a) with the profile on the outside, Column rotated so that
    its local x is the outward normal; on the plane of the element, at its offset w; z relative to the home story."""
    out = []
    ref = spec["reference"]
    for it in spec.get("linear", []):
        plane = it.get("plane", REFERENCE_PLANE)
        fr = frames[plane]
        w0 = float(it["w"]["v"]) if it.get("w") else 0.0
        right = outside_of(spec, plane) == "right_of_ab"
        sysdef = spec["systems"][it["system"]]
        prof = spec["systems"][sysdef["profile"]]
        pinfo = sysinfo.get("profiles", {}).get(sysdef["profile"])
        if pinfo is None:
            raise TapirError(f"linear[{it['id']}]: profile {sysdef['profile']} not prepared (systems.ensure)")
        fav = sysinfo["favorites"].get(it["system"])
        if fav is None:
            raise TapirError(f"linear[{it['id']}]: favourite of {it['system']} not prepared (systems.ensure)")
        s0, s1 = it["s"]["v"]
        v_foot = it["v"]["v"][0] if isinstance(it["v"]["v"], list) else it["v"]["v"]
        floor, level = home(fr.z0 + v_foot)
        if it["axis"] == "w":                                     # pilot B: beam perpendicular to the facade (bracket)
            w0, w1 = it["w_span"]["v"]
            rn = np.array([fr.n[1], -fr.n[0]])                    # Archicad puts the profile x on the RIGHT of beg -> end
            sg = 1.0 if float(rn @ fr.d) > 0 else -1.0            # direction of profile x along s
            s_line = s0 if sg > 0 else s1
            payload = {"begCoordinate": dict(zip("xy", fr.xy(s_line, w0))), "endCoordinate": dict(zip("xy", fr.xy(s_line, w1))),
                       "floorIndex": floor, "zCoordinate": round(fr.z0 + it["v"]["v"], 6),
                       "profileId": {"guid": pinfo["guid"]}, "favoriteName": sysdef["favorite"]}
            tool = "beam"
        elif it["axis"] == "h":
            a, b = (s0, s1) if right else (s1, s0)                 # Archicad puts the profile x on the RIGHT of beg -> end
            payload = {"begCoordinate": dict(zip("xy", fr.xy(a, w0))), "endCoordinate": dict(zip("xy", fr.xy(b, w0))),
                       "floorIndex": floor, "zCoordinate": round(fr.z0 + it["v"]["v"], 6),
                       "profileId": {"guid": pinfo["guid"]}, "favoriteName": sysdef["favorite"]}
            tool = "beam"
        else:
            v0, v1 = it["v"]["v"]
            # profile x -> outward normal n, profile y -> n turned +90 deg = +d (outside right of a->b) or -d
            origin = s0 if right else s1
            xy = fr.xy(origin, w0)
            payload = {"coordinates": {"x": xy[0], "y": xy[1], "z": round(fr.z0 + v0, 6)}, "floorIndex": floor,
                       "height": round(v1 - v0, 6), "axisRotationAngle": round(math.atan2(fr.n[1], fr.n[0]), 9),
                       "profileId": {"guid": pinfo["guid"]}, "favoriteName": sysdef["favorite"]}
            tool = "column"
        confs = [it["s"]["conf"], it["v"]["conf"], prof["conf"]] + ([it["w"]["conf"]] if it.get("w") else [])
        eid = element_id(spec, it, *confs)
        parts = [("poloha s", it["s"]), ("výška v", it["v"]), ("odsadenie w", it.get("w")), ("profil vyloženie", prof.get("w")),
                 ("profil výška/šírka", prof.get("h")), ("profil/systém", {"src": prof.get("src"), "conf": prof["conf"]})]
        zdroj, istota = _zdroj(parts)
        item = {"item": it["id"], "kind": tool, "payload": payload, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{it['id']}"},
                "fav_hash": fav["hash"], "profile_guid": pinfo["guid"], "spec_item": it, "plane": plane, "w0": w0, "z_story": level}
        item["hash"] = _hash([payload, eid, ref["layer"], item["props"], item["fav_hash"], pinfo["hash"]])
        out.append(item)
    return out


def plan_objects(spec: dict, frames: dict, home, sysinfo: dict) -> list[dict]:
    """GDL objects: CreateObjects (library part = FS template) at the local origin, angle of the plane (+180 deg when the
    outside is right of a->b: local x then runs against s and local y = outward normal), then all GDL parameters incl.
    A / B / ZZYZX (otherwise taken from the Object tool defaults, I4 finding 2). Material parameters take a surface name."""
    out = []
    ref = spec["reference"]
    gdl = sysinfo.get("gdl", {})
    attrs = sysinfo.get("attrs")
    for ob in spec.get("objects", []):
        sysdef = spec["systems"][ob["system"]]
        tpl = sysdef["template"]
        ginfo = gdl.get(tpl)
        if ginfo is None:
            raise TapirError(f"objects[{ob['id']}]: GDL template {tpl} not loaded (systems.ensure)")
        plane = ob.get("plane", REFERENCE_PLANE)
        fr = frames[plane]
        outside = outside_of(spec, plane)
        s, v, w = ob["origin"]["v"]
        floor, level = home(fr.z0 + v)
        xy = fr.xy(s, w)
        vals = object_values(ob)
        writes = []
        for k, val in {**vals, **gdl_expect.info_dims(tpl, vals)}.items():
            if isinstance(val, str):                       # Material parameter: surface name -> attribute index
                if attrs is None:
                    raise TapirError(f"objects[{ob['id']}].{k}: surface lookup needs systems.ensure")
                val = attrs.surface_index(val)
            writes.append({"name": k, "value": val})
        payload = {"libraryPartName": tpl, "coordinates": {"x": xy[0], "y": xy[1], "z": round(fr.z0 + v, 6)},
                   "floorIndex": floor, "angle": round(gdl_expect.plan_angle(fr.angle(), outside), 9)}
        confs = [ob["origin"]["conf"]] + [p["conf"] for p in ob["params"].values()] + [sysdef["conf"]]
        eid = element_id(spec, ob, *confs)
        parts = [("poloha", ob["origin"])] + [(f"param {k}", p) for k, p in ob["params"].items()] + [("šablóna", sysdef)]
        zdroj, istota = _zdroj(parts)
        cls = sysinfo.get("classes", {}).get(ob["system"])
        item = {"item": ob["id"], "kind": "object", "payload": payload, "gdl": writes, "element_id": eid, "layer": ref["layer"],
                "props": {"FS_zdroj": zdroj, "FS_istota": istota, "FS_spec": f"{spec['id']}.{ob['id']}"},
                "template": tpl, "template_hash": ginfo["hash"], "spec_item": ob, "plane": plane, "classification": cls, "z_story": level}
        item["hash"] = _hash([payload, writes, eid, ref["layer"], item["props"], ginfo["hash"], cls])
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
                  ("level", det["level"], p["zCoordinate"] - item.get("z_story", 0.0))]
    else:
        checks = [("origin.x", det["origin"]["x"], p["coordinates"]["x"]), ("origin.y", det["origin"]["y"], p["coordinates"]["y"]),
                  ("bottomOffset", det["bottomOffset"], p["coordinates"]["z"] - item.get("z_story", 0.0)), ("height", det["height"], p["height"]),
                  ("axisRotationAngle", math.remainder(det["axisRotationAngle"] - p["axisRotationAngle"], 2 * math.pi), 0.0)]
    for name, got, sent in checks:
        if abs(got - sent) > tol:
            bad.append(f"{item['item']}: {name} {got:.4f} != sent {sent:.4f}")
    return bad


def object_readback(item: dict, det: dict, params: dict, tol: float) -> list[str]:
    """Object read back: library part, origin, angle, GDL parameters = sent."""
    bad = []
    p = item["payload"]
    if det.get("libPart", {}).get("name") != item["template"]:
        bad.append(f"{item['item']}: library part {det.get('libPart', {}).get('name')} != {item['template']}")
    org = det.get("origin", {})
    for name, got, sent in (("origin.x", org.get("x"), p["coordinates"]["x"]), ("origin.y", org.get("y"), p["coordinates"]["y"])):
        if got is None or abs(got - sent) > tol:
            bad.append(f"{item['item']}: {name} {got} != sent {sent:.4f}")
    ang = det.get("angle")
    if ang is not None and abs(math.remainder(ang - p["angle"], 2 * math.pi)) > 1e-4:
        bad.append(f"{item['item']}: angle {ang:.5f} != sent {p['angle']:.5f}")
    for wr in item["gdl"]:
        got = params.get(wr["name"])
        if isinstance(got, float) and isinstance(wr["value"], (int, float)) and not isinstance(wr["value"], bool)                 and fs_systems.same(got, math.radians(wr["value"]), 1e-6) and wr["name"] in ANGLE_PARAMS:
            continue                                    # Angle parameter: written in degrees, read back in radians (I4 finding 1)
        if not fs_systems.same(got, wr["value"], 1e-6):
            bad.append(f"{item['item']}: GDL {wr['name']} = {got!r} != sent {wr['value']!r}")
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
    """Layer, Element ID, GDL (nominal + instance values, object parameters), wall inset and FS_* properties of fresh elements."""
    if not items:
        return
    # a curtain wall is never moved: SetDetailsOfElements re-lays its row pattern (systems.ensure_cw_favorite) - it comes
    # on the facade layer from its favourite; a CW on another layer is reported by the read-back
    fs_systems.move_to_layer(ac, [g for g, it in items if it["kind"] != "curtainwall"], layer_index, [])
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
          save: bool = False, force: bool = False, verbose: bool = True, rebuild: set | None = None) -> dict:
    """rebuild: state items (e.g. 'O1.cw', 'Z1') rebuilt even when their hash is unchanged - the I7 loop uses it when the
    model was changed outside the spec (class 'build')."""
    t0 = time.monotonic()
    rebuild = set(rebuild or ())
    errors, warnings = validate(spec)
    if errors:
        raise TapirError("spec is invalid:\n  " + "\n  ".join(errors))
    log: list[str] = [f"WARNING: {w}" for w in warnings]
    ac = TapirClient(port=port, timeout=90)
    path = ac.guard_project(project)
    version = ac.addon_version()
    stories = ac.tapir("GetStories")["stories"]
    n_stories = 0
    want_st = story_payload(spec, stories)
    if want_st is not None:                              # pilot B: project stories from the spec (modeller rule 1)
        ac.tapir("SetStories", {"stories": want_st}, timeout=120)
        stories = ac.tapir("GetStories")["stories"]
        if story_payload(spec, stories) is not None:
            raise TapirError(f"SetStories did not give the spec stories: {[(s['index'], s['level'], s['name']) for s in stories]}")
        n_stories = 1
        log.append(f"stories set from the spec: {[(s['index'], round(s['level'], 3), s['name']) for s in stories]}")
    story_levels = {s["index"]: s["level"] for s in stories}

    sysinfo = fs_systems.ensure(ac, spec, path, out_root, log)
    want = plan(spec, story_levels, sysinfo)

    spath = state_path(spec, out_root)
    state = load_state(spath, spec, path)
    changes = {"layer_created": 0, "created": 0, "deleted": 0, "kept": 0, "stories": n_stories}
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

    all_items = want["walls"] + want["openings"] + want["linear"] + want["objects"] + want["cw"] + want["slabs"] + want["railings"]
    wanted_ids = {it["item"] for it in all_items}
    for item_id in [k for k in known if k not in wanted_ids]:
        drop(item_id, "not in the spec any more (or merged into another opening)")

    def up_to_date(item: dict) -> bool:
        entry = known.get(item["item"])
        return bool(entry) and not force and item["item"] not in rebuild and entry["hash"] == item["hash"] and entry["guid"] in alive

    def hosted_by(wall_guid: str) -> list[str]:
        """state items whose element sits in the wall (openings die with their wall)."""
        return [i for i, e in known.items() if e.get("kind") in ("window", "door", "opening") and e["guid"] in alive
                and alive[e["guid"]]["details"].get("ownerElementId", {}).get("guid") == wall_guid]

    # ---- walls (one per zone)
    created_now: list[tuple[str, dict]] = []
    wall_guids = {}
    for wall in want["walls"]:
        if up_to_date(wall):
            changes["kept"] += 1
            wall_guids[wall["item"]] = known[wall["item"]]["guid"]
            continue
        if wall["item"] in known:
            old = known[wall["item"]]["guid"]
            for oid in hosted_by(old):
                drop(oid, f"owner wall {wall['item']} is rebuilt")
            drop(wall["item"], "rebuild (I7)" if wall["item"] in rebuild else "changed" if not force else "--force")
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})     # modeller trap: create from the 3D window
        g = _created_guids(ac.tapir("CreateWalls", {"wallsData": [wall["payload"]]}), "CreateWalls")[0]
        if wall.get("top_link"):                          # rule 3: CreateWalls with the height, then the top linked to the story
            tl = wall["top_link"]
            res = ac.tapir("ModifyWalls", {"wallsWithDetails": [{"elementId": {"guid": g}, "relativeTopStory": tl["relativeTopStory"],
                                                                 "topOffset": tl["topOffset"]}]})
            if errs := results_ok(res["executionResults"]):
                raise TapirError(f"ModifyWalls (top link) {wall['item']}: {errs}")
        known[wall["item"]] = {"guid": g, "kind": "wall", "hash": wall["hash"], "element_id": wall["element_id"]}
        wall_guids[wall["item"]] = g
        created_now.append((g, wall))
        changes["created"] += 1
        save_state(spath, state)
        log.append(f"created wall {wall['element_id']} {g}")

    # ---- openings (into the wall of their zone)
    todo = {"window": [], "door": [], "opening": []}
    for o in want["openings"]:
        entry = known.get(o["item"])
        owner_guid = wall_guids[o["owner"]]
        owner_ok = entry and entry["guid"] in alive and \
            alive[entry["guid"]]["details"].get("ownerElementId", {}).get("guid") == owner_guid
        if up_to_date(o) and owner_ok:
            changes["kept"] += 1
            continue
        if entry:
            drop(o["item"], "changed" if owner_ok or entry["guid"] not in alive else "owner wall changed")
        todo[o["kind"]].append(o)
    for kind, command, key in (("window", "CreateWindows", "windowsData"), ("door", "CreateDoors", "doorsData"),
                               ("opening", "CreateWindows", "windowsData")):
        if not todo[kind]:
            continue
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})
        data = [dict(o["payload"], ownerWallId={"guid": wall_guids[o["owner"]]}) for o in todo[kind]]
        guids = _created_guids(ac.tapir(command, {key: data}, timeout=120), command)
        for g, o in zip(guids, todo[kind]):
            known[o["item"]] = {"guid": g, "kind": kind, "hash": o["hash"], "element_id": o["element_id"], "owner": o["owner"]}
            created_now.append((g, o))
            changes["created"] += 1
            log.append(f"created {kind} {o['element_id']} {g}")
        save_state(spath, state)

    # ---- linear elements (I3) and GDL objects: independent of the walls, rebuilt only when their own hash changes
    todo_free = {"beam": [], "column": [], "object": [], "curtainwall": [], "slab": [], "railing": []}
    for item in want["linear"] + want["objects"] + want["cw"] + want["slabs"] + want["railings"]:
        if up_to_date(item):
            changes["kept"] += 1
            continue
        if item["item"] in known:
            drop(item["item"], "rebuild (I7)" if item["item"] in rebuild else "changed" if not force else "--force")
        todo_free[item["kind"]].append(item)
    for kind, command, key in (("beam", "CreateBeams", "beamsData"), ("column", "CreateColumns", "columnsData"),
                               ("object", "CreateObjects", "objectsData"), ("curtainwall", "CreateCurtainWalls", "curtainWallsData"),
                               ("slab", "CreateSlabs", "slabsData"), ("railing", "CreateRailings", "railingsData")):
        if not todo_free[kind]:
            continue
        ac.tapir("ChangeWindow", {"windowType": "3DModel"})
        guids = _created_guids(ac.tapir(command, {key: [it["payload"] for it in todo_free[kind]]}, timeout=300), command)
        cls = [{"elementId": {"guid": g}, "classificationId": it["classification"]} for g, it in zip(guids, todo_free[kind]) if it.get("classification")]
        if cls:                                           # objects come unclassified - FS_* properties need a classification
            res = ac.tapir("SetClassificationsOfElements", {"elementClassifications": cls})
            if errs := results_ok(res["executionResults"]):
                raise TapirError(f"SetClassificationsOfElements: {errs}")
        for g, it in zip(guids, todo_free[kind]):
            known[it["item"]] = {"guid": g, "kind": kind, "hash": it["hash"], "element_id": it["element_id"]}
            created_now.append((g, it))
            changes["created"] += 1
            log.append(f"created {kind} {it['element_id']} {g}")
        save_state(spath, state)

    # FS_* availability for the classification items of the facade elements, then finish every element whose
    # state entry is not marked finished (fresh ones, and any left over by an interrupted earlier run)
    fs_systems.classify_unclassified(ac, [(e["guid"], e["kind"]) for e in known.values() if not e.get("finished")], log)
    changes["systems_availability"] = fs_systems.ensure_availability(
        ac, sysinfo["props"], [e["guid"] for e in known.values()], sysinfo["manifest"], log)
    fs_systems.save_manifest(out_root, sysinfo["manifest"])
    by_item = {it["item"]: it for it in all_items}
    unfinished = [(known[i]["guid"], by_item[i]) for i in by_item if not known[i].get("finished")]
    finish_elements(ac, unfinished, layer_index, elementid_prop, sysinfo["props"])
    for g, it in unfinished:
        known[it["item"]]["finished"] = True
        if (g, it) not in created_now:
            changes["finished_late"] = changes.get("finished_late", 0) + 1
    save_state(spath, state)

    # ---- quick read-back of what was sent (full comparison is verify.py)
    readback = existing(ac, [e["guid"] for e in known.values()])
    params = gdl_params(ac, [known[o["item"]]["guid"] for o in want["openings"] + want["objects"] if o.get("inst") or o["kind"] == "object"])
    mismatches = []
    tol = spec["tolerances"]["build"]
    for item in all_items:
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
        if item["kind"] == "object":
            mismatches += object_readback(item, det, params.get(g, {}), tol)
            continue
        if item["kind"] == "curtainwall":
            if abs(det.get("height", -1) - item["payload"]["height"]) > tol:
                mismatches.append(f"{item['item']}: height {det.get('height')} != sent {item['payload']['height']}")
            continue
        if item["kind"] == "slab":
            for k_read, k_sent in (("level", "level"), ("thickness", "thickness")):
                sent = item.get("level_rel", item["payload"]["level"]) if k_read == "level" else item["payload"][k_sent]
                if abs(det.get(k_read, -99) - sent) > tol:
                    mismatches.append(f"{item['item']}: {k_read} {det.get(k_read)} != sent {item['payload'][k_sent]}")
            continue
        if item["kind"] == "railing":                     # GetDetailsOfElements does not support railings - verify reads the 3D
            continue
        keys = (("height", "height"), ("bottomOffset", "zCoordinate")) if item["kind"] == "wall" else \
            tuple((k, k) for k in ("centerOffset", "width", "height", "sillHeight"))
        for k_read, k_sent in keys:
            sent = item["payload"][k_sent]
            if abs(det[k_read] - sent) > tol:
                mismatches.append(f"{item['item']}: {k_read} {det[k_read]:.4f} != sent {sent:.4f}")
        if item["kind"] == "wall":
            tl = item.get("top_link") or {"relativeTopStory": 0, "topOffset": None}
            if det.get("relativeTopStory") != tl["relativeTopStory"] or (tl["topOffset"] is not None and abs(det.get("topOffset", 99) - tl["topOffset"]) > tol):
                mismatches.append(f"{item['item']}: top link {det.get('relativeTopStory')} / {det.get('topOffset')} != sent {tl['relativeTopStory']} / {tl['topOffset']}")
        if item["kind"] == "wall" and "compositeId" in item["payload"]:
            if det.get("structureType") != "Composite" or (det.get("compositeId") or {}).get("guid") != item["payload"]["compositeId"]["guid"]:
                mismatches.append(f"{item['item']}: structure {det.get('structureType')} / composite {det.get('compositeId')} != sent")
        if item["kind"] in ("window", "door", "opening") and det.get("ownerElementId", {}).get("guid") != wall_guids[item["owner"]]:
            mismatches.append(f"{item['item']}: owner wall is not {item['owner']}")
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
              "n_changes": changes["layer_created"] + changes["stories"] + changes["created"] + changes["deleted"] + changes.get("finished_late", 0) + n_sys,
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

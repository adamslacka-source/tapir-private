"""cw_verify: read-back of the I5/I6 elements of pilot A (ADR-001 D6) - curtain walls, balcony slabs, railings.

(a) curtain walls - frames and panels read back (GetSubelementsOfHierarchicalElements + GetDetailsOfElements):
                    mullion / transom axes = u_axes / v_axes of the spec, boundary = rect of the field, frame class
                    names and profiles (width a1 + a2, depth b1 + b2, offset b2) = system, panel class per cell (door
                    panel: class + more than one 3D body), panel count = cells; 3D of the whole CW: extent = rect, outer
                    face of the frames = reveal behind the zone face, back face inside the wall
(b) photo         - every mullion / transom axis with conf 'meranie' / 'kota' on the reference plane: centre of the bar
                    on the rectified photo (cw_photo.find_bar) vs the model axis, +-3 cm; read in the photo plane like
                    the opening rects (pilot convention, ADR finding 6)
(c) balconies     - slab level / thickness, 3D: extent, top / bottom, back corners on the wall faces, every vertex of
                    the curved front on the circle of the spec (+-1 cm), crown (own tolerance = tessellation of the arc)
(d) railings      - 3D: base on the slab top, height (+-1 cm), every vertex above the slab outline; posts + inner posts
                    and balusters = expected from the reference line, the post spacing and the baluster spacing of the
                    favourite; baluster spacing measured on the 3D (median) - the spec value the favourite cannot carry
                    is an EXCEPTION (R1)

Pure Python + numpy around a TapirClient; called from verify.py.
"""
from __future__ import annotations

import math

import numpy as np

from cw_photo import find_bar
from geom import REFERENCE_PLANE, balcony_outline, cw_cell_panels, zone_w
from linear_verify import to_photo
from spec_validate import LOW_CONF, balcony_ring, point_in_poly
from tapir_client import TapirClient

AXIS_TOL = 1e-3          # m - a frame is vertical / horizontal
PROFILE_TOL = 1e-3       # m - frame profile read back
PHOTO_TOL = 0.03         # m - bar centre on the photo
BAR_SEARCH = 0.05


def _bodies(geometry: dict, fr) -> list[tuple[np.ndarray, list]]:
    out = []
    for body in geometry.get("bodies", []):
        xyz = np.array([[v["x"], v["y"], v["z"]] for v in body["vertices"]], float)
        if len(xyz):
            out.append((fr.to_svw(xyz), body["polygons"]))
    return out


def _bbox(bodies) -> list[float] | None:
    if not bodies:
        return None
    p = np.vstack([b for b, _ in bodies])
    return [float(p[:, 0].min()), float(p[:, 0].max()), float(p[:, 1].min()), float(p[:, 1].max()), float(p[:, 2].min()), float(p[:, 2].max())]


def _details(ac: TapirClient, ids: list[dict]) -> list[dict]:
    if not ids:
        return []
    return ac.tapir("GetDetailsOfElements", {"elements": ids}, timeout=120)["detailsOfElements"]


def verify_cw(ac: TapirClient, spec: dict, want_cw: list[dict], guids: dict, details: dict, frames: dict, sysinfo: dict,
              lum, rect: dict | None, cam, row, trow, problems: list) -> dict:
    proj, photo = {}, []
    for it in want_cw:
        key, cw = it["item"], it["spec_item"]
        if key not in details:
            problems.append(f"{key}: curtain wall missing in the project")
            continue
        L = it["layout"]
        fr = L["frame"]
        sysdef = spec["systems"][cw["system"]]
        names = it["names"]
        if details[key]["floorIndex"] != spec["reference"]["home_story"]["floorIndex"]:
            problems.append(f"{key}: home story {details[key]['floorIndex']} != spec")
        sub = ac.tapir("GetSubelementsOfHierarchicalElements", {"elements": [{"elementId": {"guid": guids[key]}}]})["subelements"][0]
        frs = _details(ac, sub.get("cWallFrames", []))
        pns = _details(ac, sub.get("cWallPanels", []))
        cid = cw["id"]
        # (a) frames: axes and profiles
        u_mod, v_mod, bnd = set(), set(), []
        prof_bad = {"frame": [], "boundary": []}
        for f in frs:
            d = f.get("details", {})
            a = fr.to_svw(np.array([[d["begCoordinate"]["x"], d["begCoordinate"]["y"], d["begCoordinate"]["z"]],
                                    [d["endCoordinate"]["x"], d["endCoordinate"]["y"], d["endCoordinate"]["z"]]]))
            kind = "boundary" if d.get("type") == "Boundary" else "frame"
            if kind == "boundary":
                bnd.append(a)
            elif abs(a[0, 0] - a[1, 0]) < AXIS_TOL:
                u_mod.add(round(float(a[0, 0]), 4))
            elif abs(a[0, 1] - a[1, 1]) < AXIS_TOL:
                v_mod.add(round(float(a[0, 1]), 4))
            c = d.get("frameContour", {})
            spec_p = sysdef[kind]
            got = (c.get("a1", 0) + c.get("a2", 0), c.get("b1", 0) + c.get("b2", 0), c.get("b2", 0))
            want = (spec_p["width"]["v"], spec_p["depth"]["v"], spec_p["offset"]["v"])
            if any(abs(g_ - w_) > PROFILE_TOL for g_, w_ in zip(got, want)) or (kind == "frame" and d.get("className") != names["frame"]):
                prof_bad[kind].append(f"{d.get('className')} {tuple(round(x, 4) for x in got)}")
        for kind in ("frame", "boundary"):
            sp = sysdef[kind]
            trow(cid, f"CW {kind} profile width/depth/offset (+class '{names['frame']}' for mullions/transoms)",
                 f"{sp['width']['v']}/{sp['depth']['v']}/{sp['offset']['v']}", "as spec" if not prof_bad[kind] else "; ".join(prof_bad[kind][:3]),
                 "OK" if not prof_bad[kind] else "FAIL")
        for key_ax, mod, label in (("u_axes", u_mod, "mullion axis s"), ("v_axes", v_mod, "transom axis v")):
            spec_ax = list(cw.get(key_ax, {}).get("v", []))
            mod_l = sorted(mod)
            if len(mod_l) != len(spec_ax):
                problems.append(f"{cid}: {len(mod_l)} {label}s in the model {mod_l} != spec {spec_ax}")
                continue
            for k, (sv, mv) in enumerate(zip(spec_ax, mod_l)):
                row(cid, f"CW {label} {k + 1}", sv, mv, "params")
        if bnd:
            allb = np.vstack(bnd)
            s0, s1, v0, v1 = cw["rect"]["v"]
            for name, sv, mv in (("s0", s0, allb[:, 0].min()), ("s1", s1, allb[:, 0].max()), ("v0", v0, allb[:, 1].min()), ("v1", v1, allb[:, 1].max())):
                row(cid, f"CW boundary {name}", sv, float(mv), "params")
        else:
            problems.append(f"{cid}: curtain wall without boundary frames")
        # panels per cell
        cells = cw_cell_panels(cw)
        cols, rows = L["cols"], L["rows"]
        if len(pns) != len(cells):
            problems.append(f"{cid}: {len(pns)} panels != {len(cells)} cells")
        door_ids = []
        wrong = []
        for pid, pnd in zip(sub.get("cWallPanels", []), pns):
            d = pnd.get("details", {})
            pts = d.get("polygonCoordinates", [])
            if not pts:
                continue
            svw = fr.to_svw(np.array([[q["x"], q["y"], q["z"]] for q in pts]))
            cs, cv = float(svw[:, 0].mean()), float(svw[:, 1].mean())
            col = next((i for i, (a, b) in enumerate(cols) if a - 1e-3 <= cs <= b + 1e-3), None)
            rw = next((i for i, (a, b) in enumerate(rows) if a - 1e-3 <= cv <= b + 1e-3), None)
            want_kind = cells.get((col, rw))
            want_name = names["panels"].get(want_kind)
            if d.get("className") != want_name:
                wrong.append(f"cell {col}/{rw}: {d.get('className')} != {want_name}")
            if want_kind and sysdef["panels"][want_kind].get("libpart"):
                door_ids.append((pid, col, rw, want_kind))
        trow(cid, "CW panel class per cell (glass / door)", f"{len(cells)} cells", "as spec" if not wrong else "; ".join(wrong[:3]),
             "OK" if not wrong else "FAIL")
        if door_ids:
            geo = ac.tapir("Get3DGeometryOfElements", {"elements": [p for p, *_ in door_ids]}, timeout=120)["geometries3D"]
            for (pid, col, rw, kind), g in zip(door_ids, geo):
                nb = len(g.get("bodies", []))
                trow(cid, f"CW door panel {col}/{rw} ({sysdef['panels'][kind]['libpart']}): 3D bodies > 1 (leaves, not a plain pane)", ">1", nb,
                     "OK" if nb > 1 else "FAIL")
        # 3D of the whole curtain wall
        geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": guids[key]}}]}, timeout=120)["geometries3D"][0]
        bodies = _bodies(geo, fr)
        proj[key] = bodies
        bb = _bbox(bodies)
        if bb is None:
            problems.append(f"{cid}: curtain wall without 3D")
            continue
        s0, s1, v0, v1 = cw["rect"]["v"]
        for k, name, sv in ((0, "s0", s0), (1, "s1", s1), (2, "v0", v0), (3, "v1", v1)):
            row(cid, f"CW 3D {name}", sv, bb[k], "3D")
        # depth from the FRAMES (a door panel has handles standing out of the frame plane)
        fgeo = ac.tapir("Get3DGeometryOfElements", {"elements": sub.get("cWallFrames", [])}, timeout=120)["geometries3D"]
        fb = _bbox([b_ for g_ in fgeo for b_ in _bodies(g_, fr)])
        row(cid, "CW frames 3D w front (outer face = zone face - reveal)", L["w_face"], fb[5], "3D")
        row(cid, "CW frames 3D w back (frame depth)", L["w_face"] - sysdef["frame"]["depth"]["v"], fb[4], "3D")
        z = L["zone"]
        if bb[4] < zone_w(z) - z["thickness"]["v"] - 1e-3:
            problems.append(f"{cid}: curtain wall sticks out of the back of wall {z['id']} (w {bb[4]:.3f})")
        # (b) photo: bars of the axes read on the photo
        if lum is None or rect is None or L["plane"] != REFERENCE_PLANE:
            continue
        zw = zone_w(z)
        for key_ax, axis in (("u_axes", "v"), ("v_axes", "h")):
            ax = cw.get(key_ax)
            if not ax or ax["conf"] not in ("meranie", "kota"):
                continue
            width = sysdef["frame"]["width"]["v"]
            for k, a in enumerate(ax["v"]):
                if axis == "v":
                    hi = max(rows, key=lambda r_: r_[1] - r_[0])
                    rng = (hi[0] + 0.15 * (hi[1] - hi[0]), hi[1] - 0.15 * (hi[1] - hi[0]))
                    at = float(to_photo(np.array([[a, np.mean(rng), zw]]), cam)[0, 0])
                else:
                    rng = (s0 + 0.15 * (s1 - s0), s1 - 0.15 * (s1 - s0))
                    at = float(to_photo(np.array([[np.mean(rng), a, zw]]), cam)[0, 1])
                try:
                    pos, contrast = find_bar(lum, rect, axis, at, width, rng, BAR_SEARCH)
                except ValueError:
                    continue
                label = f"photo: {'stĺpik' if axis == 'v' else 'priečka'} {k + 1} (stred lišty)"
                row(cid, label, pos, at, "photo", tolerance=PHOTO_TOL)
                photo.append({"item": cid, "edge": label, "side": "left" if axis == "v" else "top", "range": list(rng), "photo": round(pos, 4),
                              "photo_hand": a, "model_projected": round(at, 4), "delta_cm": round((at - pos) * 100, 2), "tol_cm": PHOTO_TOL * 100,
                              "contrast": round(contrast, 1)})
    return {"proj": proj, "photo": photo}


def verify_balconies(ac: TapirClient, spec: dict, want_slabs: list[dict], guids: dict, details: dict, frames: dict, stories: dict,
                     row, trow, problems: list) -> dict:
    proj = {}
    for it in want_slabs:
        key, b = it["item"], it["spec_item"]
        if key not in details:
            problems.append(f"{key}: balcony slab missing in the project")
            continue
        fr = frames[it["plane"]]
        d = details[key]
        det = d["details"]
        th = spec["systems"][b["system"]]["thickness"]["v"]
        row(key, "slab top v (level)", b["v_top"]["v"], stories[d["floorIndex"]] + det.get("level", math.nan) - fr.z0, "params")
        row(key, "slab thickness", th, det.get("thickness", math.nan), "params")
        trow(key, "slab structure", spec["systems"][b["system"]].get("structure", "Basic"), det.get("structureType"))
        geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": guids[key]}}]}, timeout=120)["geometries3D"][0]
        bodies = _bodies(geo, fr)
        proj[key] = bodies
        bb = _bbox(bodies)
        if bb is None:
            problems.append(f"{key}: slab without 3D")
            continue
        o = balcony_outline(spec, b)
        s0, s1 = b["s"]["v"]
        row(key, "3D s0", s0, bb[0], "3D")
        row(key, "3D s1", s1, bb[1], "3D")
        row(key, "3D v top", b["v_top"]["v"], bb[3], "3D")
        row(key, "3D v bottom", b["v_top"]["v"] - th, bb[2], "3D")
        back = o["pts"][:-2]
        row(key, "3D w back (on the wall face)", min(w for _, w in back), bb[4], "3D")
        allp = np.vstack([p for p, _ in bodies])
        for (s_, w_) in back + o["pts"][-2:]:                     # outline corners are vertices of the slab
            dist = float(np.min(np.hypot(allp[:, 0] - s_, allp[:, 2] - w_)))
            row(key, f"3D corner {s_:.2f}/{w_:.2f} is a vertex", 0.0, dist, "3D")
        c, r = o["arc"][2], o["arc"][3]
        front = allp[allp[:, 2] > b["w_side"]["v"] + 0.01]
        if len(front):
            dev = np.abs(np.hypot(front[:, 0] - c[0], front[:, 2] - c[1]) - r)
            row(key, f"3D front: every vertex on the arc r {r:.3f} (max deviation)", 0.0, float(dev.max()), "3D")
        tess = r * (1 - math.cos(o["arc"][4] / 2 / max(1, len(np.unique(np.round(front[:, 0], 3))) - 1))) if len(front) else 0.02
        row(key, "3D crown w (own tolerance = tessellation of the arc)", b["w_crown"]["v"], bb[5], "3D", tolerance=max(0.01, tess + 0.002))
    return {"proj": proj}


def expected_railing_counts(path_sw: list, post_spacing: float, baluster_spacing: float, distribution: str = "BestDivision") -> dict:
    """Posts (one per node), inner posts and balusters of a railing from its reference line: per segment BestDivision
    gives ceil(L / spacing) equal fields, each field ceil(f / b) - 1 balusters (checked 2026-10-19: 3 m / 1.0 -> 27,
    3 m / 1.5 -> 28, 0.621 m -> 6 with b = 0.10)."""
    posts, inner, bal = len(path_sw), 0, 0
    for p, q in zip(path_sw[:-1], path_sw[1:]):
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        n = max(1, math.ceil(L / post_spacing - 1e-6)) if distribution == "BestDivision" else max(1, math.ceil(L / post_spacing - 1e-6))
        inner += n - 1
        f = L / n
        bal += n * max(0, math.ceil(f / baluster_spacing - 1e-6) - 1)
    return {"posts": posts, "inner": inner, "balusters": bal}


def verify_railings(ac: TapirClient, spec: dict, want_rails: list[dict], guids: dict, frames: dict, row, trow, problems: list) -> dict:
    proj = {}
    bals = {b["id"]: b for b in spec.get("balconies", [])}
    for it in want_rails:
        key, r = it["item"], it["spec_item"]
        if key not in guids:
            problems.append(f"{key}: railing missing in the state")
            continue
        sysdef = spec["systems"][r["system"]]
        fr = frames[it["plane"]]
        g = guids[key]
        geo = ac.tapir("Get3DGeometryOfElements", {"elements": [{"elementId": {"guid": g}}]}, timeout=120)["geometries3D"][0]
        bodies = _bodies(geo, fr)
        proj[key] = bodies
        bb = _bbox(bodies)
        if bb is None:
            problems.append(f"{key}: railing without 3D")
            continue
        base = r["v"]["v"]
        row(key, "3D base v (on the slab top)", base, bb[2], "3D")
        row(key, "3D height (top - base)", sysdef["height"]["v"], bb[3] - bb[2], "3D")
        sub = ac.tapir("GetSubelementsOfHierarchicalElements", {"elements": [{"elementId": {"guid": g}}]})["subelements"][0]
        b = bals.get(r.get("on", ""))
        if b is not None:                                  # every part stands above the slab; the end finishes of the top rail
            ring = balcony_ring(spec, b)                   # are anchored in the wall (favourite) - reported separately
            parts = [e for k_, ids in sub.items() if k_ != "railingToprailEnds" for e in ids]
            pg = ac.tapir("Get3DGeometryOfElements", {"elements": parts}, timeout=180)["geometries3D"] if parts else []
            pts = [p_ for g_ in pg for p_, _ in _bodies(g_, fr)]
            allp = np.vstack(pts) if pts else np.zeros((0, 3))
            out = sum(1 for s_, w_ in zip(allp[:, 0], allp[:, 2]) if not point_in_poly((s_, w_), ring, 0.01))
            row(key, f"3D vertices of posts / rails / balusters outside the slab of {b['id']} (count, tol 1 cm)", 0.0, float(out), "3D", tolerance=0.0)
            ends = sub.get("railingToprailEnds", [])
            if ends:
                eg = ac.tapir("Get3DGeometryOfElements", {"elements": ends}, timeout=60)["geometries3D"]
                ep = [p_ for g_ in eg for p_, _ in _bodies(g_, fr)]
                if ep:
                    wmin = float(np.vstack(ep)[:, 2].min())
                    back = min(w for _, w in balcony_outline(spec, b)["pts"][:-2])
                    trow(key, "top rail end finishes anchored in the wall (depth behind the wall face, favourite)", "<= 0.10 m",
                         round(back - wmin, 3), "OK" if back - wmin <= 0.10 + 1e-6 else "FAIL")
        fav_bs = sysdef.get("favorite_baluster_spacing") or sysdef.get("baluster_spacing", {}).get("v")
        exp = expected_railing_counts(r["path_sw"]["v"], sysdef["post_spacing"]["v"], fav_bs, sysdef.get("post_distribution", "BestDivision"))
        n_posts = len(sub.get("railingPosts", [])) + len(sub.get("railingInnerPosts", []))
        trow(key, "posts + inner posts (one per node + BestDivision)", exp["posts"] + exp["inner"], n_posts)
        trow(key, f"balusters (bars) with the spacing of the favourite {fav_bs} m", exp["balusters"], len(sub.get("railingBalusters", [])))
        # measured baluster spacing on the 3D (median nearest neighbour along each segment)
        bl = sub.get("railingBalusters", [])
        if bl:
            bg = ac.tapir("Get3DGeometryOfElements", {"elements": bl}, timeout=180)["geometries3D"]
            cs = []
            for gg in bg:
                vs = [v for body in gg.get("bodies", []) for v in body["vertices"]]
                if vs:
                    p = fr.to_svw(np.array([[np.mean([v["x"] for v in vs]), np.mean([v["y"] for v in vs]), 0.0]]))[0]
                    cs.append((p[0], p[2]))
            cs = np.array(cs)
            if len(cs) > 2:
                dmat = np.hypot(cs[:, None, 0] - cs[None, :, 0], cs[:, None, 1] - cs[None, :, 1])
                np.fill_diagonal(dmat, np.inf)
                med = float(np.median(dmat.min(axis=1)))
                row(key, f"baluster spacing measured (median nearest neighbour) = favourite {fav_bs}", fav_bs, med, "3D")
                bs = sysdef.get("baluster_spacing")
                if bs:
                    trow(key, "baluster spacing spec (photo) vs model", bs["v"], round(med, 3),
                         "OK" if abs(med - bs["v"]) <= 0.01 else "EXC", "favorit určuje rozostup tyčiek (R1); ModifyRailingDetails ho nemení")
        trow(key, "infill (from the favourite)", sysdef.get("infill", {}).get("v"), "balusters" if bl else "none")
    return {"proj": proj}

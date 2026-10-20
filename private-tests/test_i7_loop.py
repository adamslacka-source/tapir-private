"""
Acceptance test of I7 (ADR-001 section 9): the automatic loop build -> verify -> classify -> fix -> build / verify
(facade/iterate.py) on both pilots, live in Archicad 29.

    python test_i7_loop.py --offline            # pure parts: systemic check, corrections file, rigid shift on the photo
    python test_i7_loop.py                      # + live: pilot A real loop (run 1, run 2 = 0 fixes), artificial spec
                                                #   error, artificial build error, pilot B real loop (run 1, run 2), switch back
    python test_i7_loop.py --regression         # ... and test_pilot_a.py / test_pilot_b.py after the loop
    python test_i7_loop.py --skip-b             # pilot A only

Writes only into C:/FacadePilot/Fasada_pilot_A.pln and Fasada_pilot_B.pln (every build / verify guards the open
project); switches A -> B -> A with SaveProject + OpenProject (GetProjectInfo before every write). Artificial errors use
a temporary corrections file (C:/FacadePilot/out/_i7_test) - the corrections of the repo are not touched by them, and the
model is rebuilt from the repo spec afterwards.

Acceptance (task I7):
  K1 artificial spec error: M015 window O6 moved 0.20 m to the right in the types (its sill and surround follow) ->
     verify finds it, class 'spec' (model = spec, the photo edge elsewhere), the loop re-measures the opening on the photo
     (rigid shift of the pair of jambs) and corrects the spec; the next iteration is within tolerance; the corrected
     spec is within +-2 cm of the original value; afterwards the model is rebuilt from the repo spec
  K2 artificial build error: one curtain wall of the shop window of M062 moved 0.10 m along the facade through the API
     (MoveElements, outside the spec) -> class 'build', the loop rebuilds it from the spec, verify passes, the spec is
     not changed
  K3 real deviations: pilot A (M015 parter O1-O4, M062 shop window O1-O3 and every other edge over tolerance) and
     pilot B (CW_1NP_15 bar 5.1 cm, double bar): every deviation gets one class with evidence (crop PNG); corrected
     ones are within tolerance in the next verify; ambiguous ones have a crop
  K4 second run of the loop = 0 fixes (both pilots); with --regression test_pilot_a.py and test_pilot_b.py pass
  K5 report_G2.md per pilot written (fixes, what remains with crops, numbers before / after)
  K6 G2 acceptance on the final spec (kontrola_G2.md / kontrola_G2_kolo2.md)
  K7 G2 kolo 2 (kontrola_G2_kolo2.md): the pair logic of the masonry edge, one decision for verify and the loop, hidden
     edges, metrics without the ambiguous edges, the G2 report (per-edge corrections, spec changes outside corrections)

Offline G2 kolo 2 (O7-O10): a pair with a visible reveal < 3 cm, a masonry step < 12 (contrast threshold) and an edge
that runs on beyond the opening are never evidence of a masonry edge (ambiguous); the automatic check and the loop give
ONE decision (M062 O6 left); pilot B: an edge from n = 2 samples never passes by its median, the column edges are read
with either polarity, the railing posts in front of the parapet band are masked.
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import copy
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

TESTS = Path(__file__).resolve().parent
FACADE = TESTS.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

import iterate as I  # noqa: E402
import spec_corrections as sc  # noqa: E402

OUT = Path(r"C:\FacadePilot\out")
TMP = OUT / "_i7_test"
PROJECT_A = I.PROJECT_A
PROJECT_B = I.PROJECT_B
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return bool(ok)


# -- offline ------------------------------------------------------------------------------------------------------------
def offline() -> None:
    rng = np.random.default_rng(7)
    same = [{"host": f"H{i}", "axis": "s", "delta": 0.04 + rng.normal(0, 0.004)} for i in range(8)]
    r = I.systemic_check(same, 0.03)["s"]
    check("O1 systemic: 8 hosts all +4 cm -> triggered, offset accepted (error drops, nothing leaves the tolerance)",
          r["triggered"] and r["accepted"] and abs(r["offset_m"] - 0.04) < 0.005, json.dumps(r))
    mixed = [{"host": f"H{i}", "axis": "s", "delta": (-1) ** i * 0.02} for i in range(8)]
    r = I.systemic_check(mixed, 0.03)["s"]
    check("O1 systemic: mixed signs -> not triggered", not r["triggered"], json.dumps(r))
    # corrections file: apply, stale, id selector
    root = {"openings": {"O6": {"rect": {"v": [1.0, 2.0, 3.0, 4.0]}}}, "i6": {"cw_fields": [{"id": "A", "rect": {"v": [0, 1, 0, 1]}}]}}
    data = {"schema": sc.SCHEMA, "corrections": [
        {"id": "c1", "target": "types", "path": ["openings", "O6", "rect"], "before": {"v": [1.0, 2.0, 3.0, 4.0]}, "after": {"v": [1.1, 2.1, 3.0, 4.0]}},
        {"id": "c2", "target": "types", "path": ["i6", "cw_fields", {"id": "A"}, "rect", "v", 3], "before": 1, "after": 1.05},
        {"id": "c3", "target": "types", "path": ["openings", "O6", "rect"], "before": {"v": [9, 9, 9, 9]}, "after": {"v": [0, 0, 0, 0]}},
        {"id": "c4", "target": "types", "path": ["openings", "O7"], "before": None, "after": {"x": 1}, "status": "reverted"}]}
    a, s = sc.apply(root, data, "types")
    check("O2 corrections: applied in order, id selector, stale entry skipped (never silently), reverted entry ignored",
          a == ["c1", "c2"] and s == ["c3"] and root["openings"]["O6"]["rect"]["v"] == [1.1, 2.1, 3.0, 4.0]
          and root["i6"]["cw_fields"][0]["rect"]["v"][3] == 1.05 and "O7" not in root["openings"], f"applied {a}, stale {s}")
    # rigid shift on the real photo of M015: O6 jambs predicted 0.20 m to the right -> d = -0.20
    from from_senec import build_spec
    from photo_measure import Photo
    spec = build_spec("M015_L1", i6=True)
    rr = next(x for x in spec["sources"] if x.get("rect"))["rect"]
    ph = Photo(Path(spec["generated"]["root"]) / rr["image"], rr["S"], rr["u0"], rr["v1"])
    o6 = next(o for o in spec["openings"] if o["id"] == "O6")
    s0, s1 = o6["rect"]["v"][:2]
    pe = {e["edge"]: e for e in o6["photo_edges"]}
    res = I.rigid_shift(ph, [{"edge": "left", "at": s0 + 0.2, "rng": pe["left"]["range"], "brighter": "left"},
                             {"edge": "right", "at": s1 + 0.2, "rng": pe["right"]["range"], "brighter": "right"}], I.RIGID_HALF)
    check("O3 rigid shift on the photo: O6 jambs predicted +0.20 m -> found d = -0.20 m (+-1 cm), unique", res["status"] == "certain" and abs(res["d"] + 0.2) <= 0.01,
          json.dumps(res))
    offline_g2(spec, ph)


def offline_g2(spec_m015: dict, ph_m015) -> None:
    """G2 fixes (kontrola_G2.md, causes 1-3): parallax of the reveals, no OK on the wrong edge, no 'model confirmed' by the
    candidate nearest to the model."""
    from photo_measure import Photo, masonry_edge, project, visible_reveal
    from verify import auto_edge_check
    # O4 synthetic: wall | reveal strip (rw 0.10 m) | frame | glass; the reveal on the left faces the camera
    S, v1 = 100.0, 4.0
    lum = np.full((400, 400), 180.0)
    xm, xf = 150, 160                                         # masonry corner / frame line (px)
    lum[:, xm:xf] = 135.0                                     # shaded reveal
    lum[:, xf:xf + 8] = 240.0                                 # white frame
    lum[:, xf + 8:] = 40.0                                    # glass
    rect = {"S": S, "u0": 0.0, "v1": v1}
    ph = Photo.from_lum(lum, S, 0.0, v1)
    s_m, s_f, rng = xm / S, xf / S, (0.8, 3.2)
    on_frame = auto_edge_check(lum, rect, ph, "left", s_f, rng, 0.10)
    m = masonry_edge(ph, "left", s_f, 0.10, rng)
    # expectation changed in G2 kolo 2 (cause 4, one decision): the automatic check no longer says only 'nejednoznačné'
    # (second step) but gives the loop's own verdict on the pair - MIMO with the photo edge AT the masonry corner. Still
    # never OK on the frame; the position it reports is the masonry edge (+-1 cm), so the check is stricter, not weaker.
    check("O4 two steps (frame vs reveal) and the model on the frame: the automatic check is never OK - it reports MIMO at "
          "the masonry edge (outer step of the pair) +-1 cm, the same decision as the loop's parallax measurement",
          on_frame["status"] == "MIMO" and abs(on_frame["photo"] - s_m) <= 0.01
          and m["status"] == "certain" and m["role"] == "outer" and abs(m["pos"] - s_m) <= 0.01,
          f"auto {on_frame['status']} (photo {on_frame.get('photo')}), masonry {m['status']} {m.get('pos')} ({m.get('role')}), truth {s_m}")
    no_rev = auto_edge_check(lum, rect, ph, "left", s_f, rng, None)
    check("O4 the same edge without the parallax rule (old check) said OK on the frame - the case of G2 finding 1",
          no_rev["status"] == "OK", json.dumps(no_rev))
    on_mas = auto_edge_check(lum, rect, ph, "left", s_m, rng, 0.10)
    m2 = masonry_edge(ph, "left", s_m, 0.10, rng)
    check("O4 model on the masonry corner, frame ~rw inward: OK and the pair confirms it (role 'pair')",
          on_mas["status"] == "OK" and m2["status"] == "certain" and m2["role"] == "pair" and abs(m2["pos"] - s_m) <= 0.01,
          f"auto {on_mas['status']}, masonry {m2['status']} {m2.get('pos')} ({m2.get('role')})")
    # O5 real photo M015: O2 left read on the frame (s0 3.42, I1/I5 value) -> ambiguous; masonry ~3.32 (kontrola_G2 nález 1)
    cam = next(x for x in spec_m015["sources"] if x.get("rect"))["rect"]["camera"]
    lumr = ph_m015.lum
    rr = next(x for x in spec_m015["sources"] if x.get("rect"))["rect"]
    zw = next(z for z in spec_m015["zones"] if z["id"] == "Z2").get("w", {}).get("v", 0.0)
    v0, v1_ = 0.13, 3.1887
    rw = visible_reveal(cam, "left", 3.42, 0.5 * (v0 + v1_), zw, 0.30)
    at = project(cam, 3.42, 0.5 * (v0 + v1_), zw)[0]
    rng2 = (v0 + 0.15 * (v1_ - v0), v1_ - 0.15 * (v1_ - v0))
    a2 = auto_edge_check(lumr, rr, ph_m015, "left", at, rng2, rw)
    m3 = masonry_edge(ph_m015, "left", at, rw, rng2)
    # expectation changed in G2 kolo 2 (N4, cause 1): the masonry step of O2 left is 7.7 < 12 (contrast threshold) - the pair
    # is no longer accepted as evidence ('certain' before); the step itself is still found within 3 cm of the independent
    # reading 3.32 of the controller, and the automatic check stays 'nejednoznačné' (never OK on the frame)
    check("O5 M015 O2 left at the frame (3.42): automatic check 'nejednoznačné'; the masonry step lies within 3 cm of the "
          "independent reading 3.32 (kontrola_G2) but is weak (< 12) -> ambiguous, not a proof (kontrola_G2_kolo2 N4)",
          a2["status"] == "nejednoznačné" and m3["status"] == "ambiguous" and "slabá" in m3["reason"] and abs(m3["pos"] - 3.32) <= 0.03,
          f"rw {rw * 100:.1f} cm, auto {a2['status']}, masonry {m3['status']} {m3.get('pos')} ({m3['reason'][:90]})")
    o6 = next(o for o in spec_m015["openings"] if o["id"] == "O6")
    s0, _, ov0, ov1 = o6["rect"]["v"]
    z1w = next(z for z in spec_m015["zones"] if z["id"] == "Z1").get("w", {}).get("v", 0.0)
    rw6 = visible_reveal(cam, "left", s0, 0.5 * (ov0 + ov1), z1w, o6["reveal"]["v"])
    at6 = project(cam, s0, 0.5 * (ov0 + ov1), z1w)[0]
    rng6 = (ov0 + 0.15 * (ov1 - ov0), ov1 - 0.15 * (ov1 - ov0))
    a6 = auto_edge_check(lumr, rr, ph_m015, "left", at6, rng6, rw6)
    check("O5 M015 O6 left already on the masonry (I3): automatic check OK, no false 'nejednoznačné'", a6["status"] == "OK", json.dumps(a6, ensure_ascii=False)[:200])
    offline_kolo2(spec_m015, ph_m015)
    # O6 pilot B: 'model confirmed' only through a clear band without blinds (ratio >= 1.5)
    row = lambda d, ratio, louv=False, double=False: {"row": "v", "pos_px": 100, "readable": True, "double": double, "delta_m": d, "ratio": ratio,
                                                      "louvred": louv, "second_px": 104}
    weak = I.judge_bar_rows([row(0.001, 1.08, double=True), row(0.001, 1.2, True)], 0.05)
    clear = I.judge_bar_rows([row(0.004, 1.6), row(0.03, 1.1, True)], 0.05)
    louv = I.judge_bar_rows([row(0.001, 3.0, True), row(0.002, 2.5, True)], 0.05)
    cw15 = I.judge_bar_rows([row(0.051, 1.08, double=True), row(0.008, 1.6, True), row(0.003, 2.5, True)], 0.05)
    check("O6 pilot B: candidates of similar strength (ratio < 1.5) near the model -> 'neoverené', never 'model potvrdený'",
          not weak.get("confirmed") and weak.get("unverified") and weak["cls"] == "ambiguous", weak["reason"][:120])
    check("O6 pilot B: a clear bar (ratio >= 1.5) in a band without blinds near the model -> model confirmed", clear.get("confirmed") is True, clear["reason"][:120])
    check("O6 pilot B: bands with blinds never confirm the model (joints of the blind modules), even with a clear valley",
          not louv.get("confirmed") and louv.get("unverified"), louv["reason"][:120])
    check("O6 pilot B: CW_1NP_15 pattern (parapet band +5.1 cm, double bar; near only in bands with blinds) -> nejednoznačné, not confirmed",
          cw15["cls"] == "ambiguous" and not cw15.get("confirmed") and not cw15.get("unverified"), cw15["reason"][:140])


def offline_kolo2(spec_m015: dict, ph_m015) -> None:
    """G2 kolo 2 (kontrola_G2_kolo2.md, cause 1 / 4 / 3, N1 N4 N6 N8 N9)."""
    from photo_measure import MIN_REVEAL_M, MIN_STEP, Photo, judge_reveal_edge, masonry_edge, project, visible_reveal
    from verify import auto_edge_check
    from geom import hidden_edge, jamb_context
    from build import opening_units, unit_reveal
    from geom import zone_w
    S, v1 = 100.0, 4.0
    # O7 (N1) a pair whose visible reveal is < 3 cm: masonry corner and a second step 2 cm inward (sign bottom / frame)
    lum = np.full((400, 400), 180.0)
    lum[:, 150:152] = 120.0
    lum[:, 152:160] = 240.0
    lum[:, 160:] = 40.0
    ph = Photo.from_lum(lum, S, 0.0, v1)
    m = masonry_edge(ph, "left", 1.50, 0.05, (0.8, 3.2))
    check("O7 pair with a visible reveal < 3 cm (2 cm) is no evidence of a masonry edge -> ambiguous (N1, 'ostenie' 2.2 cm)",
          m["status"] == "ambiguous" and "< 3 cm" in m["reason"], m["reason"][:120])
    check("O7 pair logic needs an expected visible reveal >= 3 cm (MIN_REVEAL_M = the threshold of the automatic check)",
          abs(MIN_REVEAL_M - 0.03) < 1e-9, f"MIN_REVEAL_M {MIN_REVEAL_M}")
    # O7 real: M015 O1 top - expected reveal 2.4 cm, the step 2.82 is the bottom of the florist's sign; the spec declares the
    # edge hidden (ODHAD) -> the automatic check says 'zakrytá', the loop does not measure it (no fix, no confirmation)
    units = {u["o"]["id"]: u for u in opening_units(spec_m015)}
    rr = next(x for x in spec_m015["sources"] if x.get("rect"))["rect"]
    cam = rr["camera"]
    zones = {z["id"]: z for z in spec_m015["zones"]}
    u1 = units["O1"]
    s0, s1, v0, v1_ = u1["hole"]
    zw = zone_w(zones[u1["zone"]])
    rw1 = visible_reveal(cam, "top", 0.5 * (s0 + s1), v1_, zw, unit_reveal(spec_m015, u1))
    at1 = project(cam, 0.5 * (s0 + s1), v1_, zw)[1]
    rng1 = (s0 + 0.15 * (s1 - s0), s1 - 0.15 * (s1 - s0))
    hid = hidden_edge(u1["o"]["rect"], "top")
    a1 = auto_edge_check(ph_m015.lum, rr, ph_m015, "top", at1, rng1, rw1, hidden=hid)
    m1 = masonry_edge(ph_m015, "top", at1, max(rw1 or 0.0, 0.024), rng1)
    check("O7 M015 O1 top (sign of the florist): expected reveal < 3 cm (no pair logic), the pair found there (2.2 cm) is "
          "ambiguous, the spec marks the top ODHAD and the automatic check reports it 'zakrytá', never OK",
          (rw1 is None or rw1 < MIN_REVEAL_M) and m1["status"] == "ambiguous" and hid and a1["status"] == "zakrytá",
          f"rw {None if rw1 is None else round(rw1 * 100, 1)} cm; masonry {m1['status']}: {m1['reason'][:80]}; auto {a1['status']}")
    # O8 (N4) contrast: the masonry step 9 < 12 with a clear frame inward -> ambiguous (the old rule accepted it at >= 6)
    lum = np.full((400, 400), 180.0)
    lum[:, 150:160] = 162.0                                   # wall -> reveal: raw step 18 = 9 after the [1 2 1] smoothing
    lum[:, 160:168] = 240.0
    lum[:, 168:] = 40.0
    ph = Photo.from_lum(lum, S, 0.0, v1)
    m = masonry_edge(ph, "left", 1.50, 0.10, (0.8, 3.2))
    a = auto_edge_check(lum, {"S": S, "u0": 0.0, "v1": v1}, ph, "left", 1.50, (0.8, 3.2), 0.10)
    check(f"O8 masonry step < {MIN_STEP:.0f} (9) with the frame 10 cm inward: no proof - masonry_edge ambiguous, automatic check "
          "'nejednoznačné' (never OK)", m["status"] == "ambiguous" and "slabá" in m["reason"] and a["status"] == "nejednoznačné",
          f"{m['reason'][:100]}; auto {a['status']}")
    # O8 real: M015 O2 left on the model (3.3229, correction c5): weak masonry step 7.7 -> ambiguous; the model is within 3 cm
    # of the independent reading of the controller (3.32) - c5 stays, the evidence is the controller, not the pair
    u2 = units["O2"]
    s0, s1, v0, v1_ = u2["hole"]
    zw = zone_w(zones[u2["zone"]])
    rw2 = visible_reveal(cam, "left", s0, 0.5 * (v0 + v1_), zw, unit_reveal(spec_m015, u2))
    at2 = project(cam, s0, 0.5 * (v0 + v1_), zw)[0]
    j2 = judge_reveal_edge(ph_m015, "left", at2, rw2, (v0 + 0.15 * (v1_ - v0), v1_ - 0.15 * (v1_ - v0)))
    check("O8 M015 O2 left (c5, 3.3229): masonry step 7.7 < 12 -> 'nejednoznačné'; model within 3 cm of the controller's 3.32",
          j2["status"] == "nejednoznačné" and abs(s0 - 3.32) <= 0.03, f"s0 {s0}, {j2['status']}: {j2['reason'][:90]}")
    # O9 (N1) continuation: the lower edge of a sign runs on beyond a free end of the lintel -> ambiguous (pair edge too)
    lum = np.full((400, 400), 180.0)
    lum[100:150, 50:350] = 60.0                               # sign s 0.5-3.5 above the lintel v 2.5
    lum[150:156, 100:300] = 135.0                             # soffit (visible reveal, camera below)
    lum[156:350, 100:300] = 40.0                              # glass of the opening s 1.0-3.0, v 0.5-2.5
    ph = Photo.from_lum(lum, S, 0.0, v1)
    m = masonry_edge(ph, "top", 2.50, 0.06, (1.3, 2.7), (1.0, 3.0, 0.5, 2.5), (True, True))
    check("O9 a top edge (pair) that runs on beyond the free ends of the opening (sign / awning / sill plate) -> ambiguous",
          m["status"] == "ambiguous" and "pokračuje" in m["reason"], m["reason"][:120])
    m_one = masonry_edge(ph, "top", 2.50, 0.06, (1.3, 2.7), (1.0, 3.0, 0.5, 2.5), (False, True))
    check("O9 ... also when only one end is free (a touching neighbour on the other side)", m_one["status"] == "ambiguous" and "pokračuje" in m_one["reason"],
          m_one["reason"][:120])
    # O10 (N8, cause 4) one decision: M062 O6 left - the automatic check and the loop's judgement are the same function and agree
    from from_senec import build_spec
    sp62 = build_spec("M062_L2", i6=True)
    r62 = next(x for x in sp62["sources"] if x.get("rect"))["rect"]
    ph62 = Photo(Path(sp62["generated"]["root"]) / r62["image"], r62["S"], r62["u0"], r62["v1"])
    un62 = {u["o"]["id"]: u for u in opening_units(sp62)}
    z62 = {z["id"]: z for z in sp62["zones"]}
    u6 = un62["O6"]
    s0, s1, v0, v1_ = u6["hole"]
    zw = zone_w(z62[u6["zone"]])
    rw6 = visible_reveal(r62["camera"], "left", s0, 0.5 * (v0 + v1_), zw, unit_reveal(sp62, u6))
    pa = project(r62["camera"], s0, v0, zw)
    pb = project(r62["camera"], s1, v1_, zw)
    rect_ph = [pa[0], pb[0], pa[1], pb[1]]
    jc = jamb_context(un62, "O6", "left")
    rng6 = (v0 + 0.15 * (v1_ - v0), v1_ - 0.15 * (v1_ - v0))
    a6 = auto_edge_check(ph62.lum, r62, ph62, "left", rect_ph[0], rng6, rw6, rect_ph=rect_ph, free=jc["free"], shared=jc["shared"])
    j6 = judge_reveal_edge(ph62, "left", rect_ph[0], rw6, rng6, rect_ph, jc["free"])
    pl = I.PilotA("M062_L2")
    f6 = {"source": "auto", "item": "O6", "host": "O6", "edge": "left", "model_photo": rect_ph[0], "photo": a6.get("photo"), "delta_cm": 0.0, "tol_cm": 3.0}
    g6 = pl._classify_reveal_edge(sp62, {}, u6, f6, "left", rect_ph[0], rw6, rng6, rect_ph, jc["free"], zw, r62["camera"], ph62, {"O6"}, 0.03, 1, TMP)
    check("O10 M062 O6 left (N8): ONE decision - automatic check status == loop judgement, and not 'MIMO' in one place and "
          "'model potvrdený' in the other", a6["status"] == j6["status"] and not (a6["status"] == "MIMO" and g6.get("confirmed"))
          and (g6["cls"] == "ambiguous") == (a6["status"] in ("nejednoznačné", "nečitateľné", "OK")),
          f"auto {a6['status']} ({a6.get('note', '')[:70]}), loop {g6['cls']}{' confirmed' if g6.get('confirmed') else ''}: {g6['reason'][:80]}")
    # O11 (N6, task 4) pilot B: an edge from n = 2 samples never passes by its median
    import photo_b
    v2 = photo_b.edge_verdict([0.0005, -0.0469], 0.03)
    check("O11 pilot B: 2 samples (+0.05 / -4.69 cm) -> FAIL (every sample judged, never the median of n = 2)",
          v2["ok"] is False and v2["rule"].startswith("každá"), json.dumps(v2))
    v7 = photo_b.edge_verdict([0.001, 0.002, -0.004, 0.0, 0.035, 0.001, 0.002], 0.03)
    check("O11 pilot B: horizontal edge with 7 samples keeps the median rule (one bad sample = a post / downpipe in front)",
          v7["ok"] is True and v7["rule"] == "medián", json.dumps(v7))
    # O12 (N6, N9) pilot B photo, read in an isolated interpreter (downloaded photo): the column with either polarity;
    # the railing post in front of the parapet band of CW_2NP_05 bar 1 is masked
    r = subprocess.run([sys.executable, "-I", str(TESTS / "b_photo_probe.py")], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        check("O12 pilot B photo probe (python -I)", False, (r.stderr or r.stdout)[-300:])
        return
    pr = json.loads(r.stdout.strip().splitlines()[-1])
    col = pr["column"]
    bad = [c for c in col if c["new_step"] >= 12.0 and abs(c["new_cm"]) > 3.0]
    readable = [c for c in col if c["new_step"] >= 12.0]
    lower_r = next(c for c in col if c["band"] == [3400, 3700] and c["edge"] == "right")
    check("O12 pilot B column STLP_L: every readable sample of both edges within 3 cm when read with either polarity "
          "(4 bands); the old fixed polarity took an edge in the tree behind the column in the lower band (N6)",
          len(readable) >= 6 and not bad and abs(lower_r["old_px"] - lower_r["new_px"]) > 8,
          "; ".join(f"{c['edge'][0]} {c['band'][0]}: {c['new_cm']:+.1f} cm (step {c['new_step']:.0f}; old {c['old_cm']:+.1f})" for c in col))
    hits = pr["masks"]["cw_2np_05_bar1"]["hits"]
    check("O12 pilot B CW_2NP_05 bar 1 (N9): a railing post stands in front of the parapet band next to the model bar -> "
          "masked (the 4993 px valley is the post, not a mullion)",
          any(h["x0"] - 3 <= 4993 <= h["x1"] + 3 and "zábradlia" in h["kind"] for h in hits), json.dumps(hits, ensure_ascii=False)[:200])


# -- live helpers -------------------------------------------------------------------------------------------------------
def client(port: int):
    from tapir_client import TapirClient
    return TapirClient(port=port, timeout=120)


def switch(port: int, target: str) -> bool:
    """SaveProject of the open project, OpenProject target, wait until GetProjectInfo says so (max 5 min)."""
    ac = client(port)
    cur = ac.project_path()
    if cur.lower() == target.lower():
        return True
    if cur.lower() not in (PROJECT_A.lower(), PROJECT_B.lower()):
        check(f"open project is a pilot before switching ({cur})", False)
        return False
    ac.tapir("SaveProject", {}, timeout=300)
    ac.tapir("OpenProject", {"projectFilePath": target}, timeout=600)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300:
        try:
            if client(port).project_path().lower() == target.lower():
                return check(f"switched to {Path(target).name} (SaveProject + OpenProject)", True, f"{time.monotonic() - t0:.0f} s")
        except Exception:                                      # noqa: BLE001 - Archicad busy while opening
            pass
        time.sleep(3)
    return check(f"switched to {Path(target).name}", False, "timeout 300 s")


def hist(i7_dir: Path) -> list[dict]:
    return json.loads((i7_dir / "iterations.json").read_text(encoding="utf-8"))["runs"]


# -- K3 / K4 pilot A real -----------------------------------------------------------------------------------------------
def real_a(port: int) -> None:
    logs1 = I.run_a(["M015_L1", "M062_L2", "M016_L2"], port, I.MAX_ITER)
    logs2 = I.run_a(["M015_L1", "M062_L2", "M016_L2"], port, I.MAX_ITER)
    for lg1, lg2 in zip(logs1, logs2):
        sid = lg1["spec"]
        f1 = lg1["first_findings"]
        classes = {c: sum(f["cls"] == c for f in f1) for c in ("build", "spec", "ambiguous", "systemic")}
        check(f"K3 {sid}: every deviation over tolerance has one class with evidence", all(f["cls"] in ("build", "spec", "ambiguous") and f.get("reason") for f in f1),
              f"{len(f1)} findings {classes}")
        photo = [f for f in f1 + lg2["final_findings"] if f["source"] in ("edge", "auto", "bar")]
        check(f"K3 {sid}: every photo deviation has a crop PNG", all(f.get("crop") and Path(f["crop"]).exists() for f in photo), f"{len(photo)} crops")
        check(f"K3 {sid}: verify PASS after the loop", lg1["verify_pass"] and lg2["verify_pass"])
        check(f"K4 {sid}: second run of the loop = 0 fixes, nothing left in class spec / build", lg2["fixes_total"] == 0
              and not [f for f in lg2["final_findings"] if f["cls"] in ("spec", "build")], f"run 1: {lg1['fixes_total']} fixes; run 2: {lg2['fixes_total']}")
        corr = sc.load(sc.path_of(sid) if sc.path_of(sid).exists() else None, sid)
        act = [c for c in corr["corrections"] if c.get("status", "active") == "active"]
        bad = [c["id"] for c in act if not (c.get("crop") and Path(c["crop"]).exists() and c.get("evidence") and c.get("kept") is True
                                            and c.get("error_after_cm") is not None and c["error_after_cm"] <= 3.0)]
        check(f"K3 {sid}: every correction of the spec has evidence + crop, was evaluated and lowered the error into tolerance",
              not bad, f"{len(act)} active: " + ", ".join(f"{c['id']} {'.'.join(str(x['id']) if isinstance(x, dict) else str(x) for x in c['path'])} "
                                                             f"{c.get('error_before_cm')}->{c.get('error_after_cm')} cm" for c in act) + (f"; bad {bad}" if bad else ""))
    # the named parter openings: M015 O1..O4 (O1/O3/O4 are glazed fields), M062 shop window O1..O3
    named = {"M015_L1": {"O1", "O2", "O3", "O4"}, "M062_L2": {"O1", "O2", "O3"}}
    for lg in logs1:
        if lg["spec"] not in named:
            continue
        runs = hist(OUT / lg["spec"] / "i7")
        first_all = runs[0]["first_findings"]
        sel = [f for f in first_all if f["item"] in named[lg["spec"]]]
        corr = sc.load(sc.path_of(lg["spec"]) if sc.path_of(lg["spec"]).exists() else None, lg["spec"])
        fixed = {c["finding"]["item"] for c in corr["corrections"] if c.get("status", "active") == "active" and c.get("finding")}
        ok = all(f["cls"] == "ambiguous" and f.get("crop") or f["cls"] == "spec" and f["item"] in fixed for f in sel)
        check(f"K3 {lg['spec']} parter {sorted(named[lg['spec']])}: each edge read 'on the frame' is corrected from the photo (certain) "
              "or ambiguous with a crop", bool(sel) and ok,
              "; ".join(f"{f['item']} {f.get('edge')} {f.get('delta_cm'):+.1f} cm -> {f['cls']}" for f in sel))
    g2_a_checks()
    kolo2_live_a(logs2)
    p = OUT / "report_G2.md"
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    check("K5 report_G2.md of pilot A written (fixes, what remains, numbers before / after per house)",
          all(h in txt for h in ("M015_L1", "M062_L2", "M016_L2")) and "Čo ostáva" in txt and "pred (1. beh" in txt, str(p))


def kolo2_live_a(logs2: list[dict]) -> None:
    """K7 (G2 kolo 2) on the live result of pilot A."""
    v15 = json.loads((OUT / "M015_L1" / "report.json").read_text(encoding="utf-8"))
    a = {(e["item"], e["edge"]): e for e in v15["auto_edges"]}
    check("K7 M015 O1 top (N1): automatic check 'zakrytá' (spec ODHAD), O2 left 'nejednoznačné' (weak step) - never OK",
          a.get(("O1", "top"), {}).get("status") == "zakrytá" and a.get(("O2", "left"), {}).get("status") == "nejednoznačné",
          f"O1 top {a.get(('O1', 'top'), {}).get('status')}, O2 left {a.get(('O2', 'left'), {}).get('status')}")
    lg15 = next(lg for lg in logs2 if lg["spec"] == "M015_L1")
    f_top = [f for f in lg15["final_findings"] if f["item"] in ("O1", "O1b") and f.get("edge") == "top"]
    check("K7 M015 O1 top: the loop neither corrects nor confirms the hidden edge (ambiguous, hidden, crop)",
          f_top and all(f["cls"] == "ambiguous" and f.get("hidden") and not f.get("confirmed") and f.get("crop") and Path(f["crop"]).exists() for f in f_top),
          "; ".join(f"{f['item']} {f['cls']} {f['reason'][:60]}" for f in f_top))
    v62 = json.loads((OUT / "M062_L2" / "report.json").read_text(encoding="utf-8"))
    o6 = next((e for e in v62["auto_edges"] if e["item"] == "O6" and e["edge"] == "left"), {})
    lg62 = next(lg for lg in logs2 if lg["spec"] == "M062_L2")
    g6 = [f for f in lg62["final_findings"] if f["item"] == "O6" and f.get("edge") == "left"]
    check("K7 M062 O6 left (N8): one decision - not 'MIMO' in verify and 'model potvrdený' in the loop",
          not (o6.get("status") == "MIMO" and any(f.get("confirmed") for f in g6)) and o6.get("status") != "MIMO",
          f"verify {o6.get('status')} ({o6.get('note', '')[:60]}); loop {[(f['cls'], bool(f.get('confirmed'))) for f in g6]}")
    for lg in logs2:
        ps = lg["iterations"][-1].get("photo_summary_final") or lg["iterations"][-1].get("photo_summary") or {}
        amb = [f for f in lg["final_findings"] if f["cls"] == "ambiguous" and not f.get("confirmed") and f.get("delta_cm") is not None
               and f["source"] in ("edge", "auto")]
        worst_amb = max([abs(f["delta_cm"]) for f in amb] or [0.0])
        check(f"K7 {lg['spec']} metrics (cause 3): max of the measured edges excludes the ambiguous ones (shown apart)",
              "amb_n" in ps and (ps.get("max_cm") is None or worst_amb <= (ps.get("max_cm") or 0) + 1e-6 or ps.get("max_at") not in
                                 {f"{f['item']} {f['edge']}" for f in amb}),
              f"max {ps.get('max_cm')} cm ({ps.get('max_at')}), ambiguous {ps.get('amb_n')} max {ps.get('amb_max_cm')} cm ({str(ps.get('amb_max_at'))[:60]})")
    txt = (OUT / "report_G2.md").read_text(encoding="utf-8") if (OUT / "report_G2.md").exists() else ""
    check("K7 report_G2.md: corrections per edge (c6 left only, c7 reverted), spec changes outside corrections (M016 O6 / A1 / Z2 / "
          "BK2 / R2), ambiguous apart in the metrics",
          "– po hranách" in txt and "Zmeny spec mimo corrections" in txt and "openings[O6].rect" in txt and "objects[A1]" in txt
          and "balconies[BK2].v_top" in txt and "railings[R2].v" in txt and "zones[Z2].rect" in txt and "mimo metriky" in txt
          and "M015_L1.c7" in txt and "vrátená" in txt, str(OUT / "report_G2.md"))
    ov = OUT / "M015_L1" / "overlay_M015_L1.png"
    vd = OUT / "M015_L1" / "i7" / "verdicts.json"
    check("K7 overlay (N10): verdicts of the loop written and the overlay redrawn after them (ambiguous edges coloured apart)",
          vd.exists() and ov.exists() and ov.stat().st_mtime >= vd.stat().st_mtime - 1, f"{vd} / {ov}")


def g2_a_checks() -> None:
    """G2 acceptance on the final spec (kontrola_G2.md): M015 left edges O1-O3 on the masonry edge (<= 3 cm, measured by
    the parallax pair on the photo, not the frame); M016 threshold of O6 = photo +-3 cm and on the balcony BK2 (<= 1 cm)."""
    from from_senec import build_spec
    from photo_measure import Photo, masonry_edge, project, visible_reveal
    from build import opening_units, unit_reveal
    from geom import zone_w
    spec = build_spec("M015_L1", i6=True)
    rr = next(x for x in spec["sources"] if x.get("rect"))["rect"]
    ph = Photo(Path(spec["generated"]["root"]) / rr["image"], rr["S"], rr["u0"], rr["v1"])
    cam = rr["camera"]
    zones = {z["id"]: z for z in spec["zones"]}
    units = {u["o"]["id"]: u for u in opening_units(spec)}
    res = []
    for oid in ("O1", "O2", "O3"):
        u = units[oid]
        s0, s1, v0, v1 = u["hole"]
        zw = zone_w(zones[u["zone"]])
        rw = visible_reveal(cam, "left", s0, 0.5 * (v0 + v1), zw, unit_reveal(spec, u))
        at = project(cam, s0, 0.5 * (v0 + v1), zw)[0]
        m = masonry_edge(ph, "left", at, rw, (v0 + 0.15 * (v1 - v0), v1 - 0.15 * (v1 - v0)))
        res.append((oid, s0, m["status"], m.get("role"), None if m.get("pos") is None else round((m["pos"] - at) * 100, 1)))
    # expectation changed in G2 kolo 2 (N4): O2 left has a masonry step 7.7 < 12 - the pair is no proof any more; its step
    # is still at the model (<= 3 cm) and the model is within 3 cm of the controller's independent reading 3.32
    ok_ = all(st == "certain" and role in ("pair", "outer") and d is not None and abs(d) <= 3.0 for o, _, st, role, d in res if o != "O2")
    o2 = next(x for x in res if x[0] == "O2")
    ok_ = ok_ and o2[2] == "ambiguous" and o2[4] is not None and abs(o2[4]) <= 3.0 and abs(o2[1] - 3.32) <= 0.03
    check("K6 M015 left edges O1-O3 on the masonry edge (G2 nález 1): O1 / O3 the parallax pair puts the masonry at the model "
          "(<= 3 cm); O2 weak masonry step (< 12, kolo 2 N4) -> ambiguous, at the model and within 3 cm of the controller's 3.32",
          ok_, "; ".join(f"{o} s0 {s0} {st} {role} {d:+.1f} cm" if d is not None else f"{o} {st}" for o, s0, st, role, d in res))
    m16 = build_spec("M016_L2", i6=True)
    o6 = next(o for o in m16["openings"] if o["id"] == "O6")
    bk2 = next(b for b in m16["balconies"] if b["id"] == "BK2")
    r16 = next(x for x in m16["sources"] if x.get("rect"))["rect"]
    ph16 = Photo(Path(m16["generated"]["root"]) / r16["image"], r16["S"], r16["u0"], r16["v1"])
    # threshold on the photo: bright frame -> dark band below, per column of the door (right of the sign), median
    ys = []
    for s_ in np.arange(7.15, 8.51, 0.05):
        x = int(round(ph16.x(s_)))
        col = ph16.lum[:, x - 1:x + 2].mean(axis=1)
        y0, y1 = int(ph16.y(7.05)), int(ph16.y(6.60))
        st = col[y0:y1 - 1] - col[y0 + 1:y1]
        ys.append(ph16.v(y0 + int(np.argmax(st)) + 1.0))
    thr = float(np.median(ys))
    check("K6 M016 O6 threshold = photo +-3 cm and the door sits on the balcony BK2 (<= 1 cm) (G2 nálezy 3-4)",
          abs(o6["rect"]["v"][2] - thr) <= 0.03 and abs(o6["rect"]["v"][2] - bk2["v_top"]["v"]) <= 0.01 and not o6.get("head_in"),
          f"O6 bottom {o6['rect']['v'][2]}, photo {thr:.3f} (median of {len(ys)} columns), BK2 top {bk2['v_top']['v']}; head {o6['rect']['v'][3]}")


# -- K1 artificial spec error -------------------------------------------------------------------------------------------
def artificial_spec(port: int) -> None:
    house = "M015_L1"
    TMP.mkdir(parents=True, exist_ok=True)
    tmp_corr = TMP / f"{house}.spec_error.json"
    real = sc.path_of(house)
    if real.exists():
        shutil.copy(real, tmp_corr)
    elif tmp_corr.exists():
        tmp_corr.unlink()
    log_dir = OUT / house / "i7_test_spec"
    shutil.rmtree(log_dir, ignore_errors=True)
    from from_senec import TYPES_DIR
    orig = json.loads((TYPES_DIR / f"{house}.json").read_text(encoding="utf-8"))["openings"]["O6"]["rect"]["v"]

    def inject(types):
        types["openings"]["O6"]["rect"]["v"][0] = round(types["openings"]["O6"]["rect"]["v"][0] + 0.20, 4)
        types["openings"]["O6"]["rect"]["v"][1] = round(types["openings"]["O6"]["rect"]["v"][1] + 0.20, 4)

    n_before = len(sc.load(tmp_corr if tmp_corr.exists() else None, house)["corrections"])
    lg = I.Loop(I.PilotA(house, port, corr_path=tmp_corr, types_hook=inject), I.MAX_ITER, log_dir=log_dir).run()
    it1 = lg["iterations"][0]
    o6 = [f for f in it1["findings"] if f["host"] == "O6" or f["item"] in ("O6", "O6_sur", "O6_sill")]
    spec_cls = [f for f in o6 if f["cls"] == "spec"]
    check("K1 verify finds the moved window O6 (deviations of O6 / its surround over tolerance)", bool(o6),
          "; ".join(f"{f['item']} {f.get('edge')} {f.get('delta_cm')} cm {f['source']}" for f in o6))
    check("K1 class 'spec' (model = spec, photo edge elsewhere) for the O6 group", bool(spec_cls) and not [f for f in o6 if f["cls"] == "build"],
          "; ".join(f"{f['item']} {f.get('edge')} -> {f['cls']}: {f['reason'][:90]}" for f in o6))
    corr = sc.load(tmp_corr, house)
    new = [c for c in corr["corrections"][n_before:] if c["path"] == ["openings", "O6", "rect"]]
    after = new[0]["after"]["v"] if new else None
    check("K1 the loop corrected the spec from the photo (rigid shift of the jambs), src I7.1, conf meranie",
          bool(new) and new[0]["src"] == "I7.1" and new[0]["conf"] == "meranie" and new[0].get("evidence", {}).get("method") == "rigid_shift",
          f"{new[0]['before']['v']} -> {after}" if new else "no correction of openings.O6.rect")
    check("K1 corrected spec within +-2 cm of the original value (types file)", after is not None and abs(after[0] - orig[0]) <= 0.02 and abs(after[1] - orig[1]) <= 0.02,
          f"original {orig[:2]}, corrected {after[:2] if after else None}")
    ev = [e for it in lg["iterations"][1:] for e in it["evaluated"] if e["item"] == "O6"]
    check("K1 next iteration within tolerance (error lowered, edges of O6 in tolerance)", bool(ev) and ev[0]["kept"] and ev[0]["after_cm"] <= 3.0,
          json.dumps(ev[:1], ensure_ascii=False))
    left = [f for f in lg["final_findings"] if (f["host"] == "O6" or f["item"].startswith("O6")) and f["cls"] in ("spec", "build")]
    check("K1 after the loop: no O6 deviation left, verify PASS", not left and lg["verify_pass"], f"left {left}")
    # restore: the model from the repo spec (no injection, repo corrections)
    pa = I.PilotA(house, port)
    spec = pa.load_spec(sc.load(pa.corr_path if pa.corr_path.exists() else None, house))
    b = pa.build(spec, set())
    v = pa.verify(spec)
    check("K1 model rebuilt from the repo spec afterwards (O6 back, verify PASS)", v["pass"] and not b["mismatches"], f"{b['n_changes']} changes")


# -- K2 artificial build error ------------------------------------------------------------------------------------------
def artificial_build(port: int) -> None:
    house = "M062_L2"
    from geom import frames_of
    TMP.mkdir(parents=True, exist_ok=True)
    tmp_corr = TMP / f"{house}.build_error.json"
    real = sc.path_of(house)
    if real.exists():
        shutil.copy(real, tmp_corr)
    elif tmp_corr.exists():
        tmp_corr.unlink()
    log_dir = OUT / house / "i7_test_build"
    shutil.rmtree(log_dir, ignore_errors=True)
    pa = I.PilotA(house, port, corr_path=tmp_corr)
    spec = pa.load_spec(sc.load(tmp_corr if tmp_corr.exists() else None, house))
    pa.build(spec, set())
    st = json.loads((OUT / house / "state.json").read_text(encoding="utf-8"))["elements"]
    g0 = st["O1.cw"]["guid"]
    fr = frames_of(spec)["F"]
    ac = client(port)
    ac.guard_project(PROJECT_A)
    mv = {"elementsWithMoveVectors": [{"elementId": {"guid": g0}, "moveVector": {"x": float(fr.d[0]) * 0.10, "y": float(fr.d[1]) * 0.10, "z": 0.0}}]}
    res = ac.tapir("MoveElements", mv)
    check("K2 curtain wall O1 of M062 moved 0.10 m along the facade through the API (outside the spec)",
          all(r.get("success") for r in res.get("executionResults", [])), json.dumps(res)[:200])
    snap = json.dumps(sc.load(tmp_corr if tmp_corr.exists() else None, house)["corrections"], sort_keys=True)
    lg = I.Loop(pa, I.MAX_ITER, log_dir=log_dir).run()
    it1 = lg["iterations"][0]
    bld = [f for f in it1["findings"] if f["cls"] == "build" and f["item"] == "O1"]
    check("K2 verify finds it as class 'build' (rows of the curtain wall O1 out of tolerance)", bool(bld),
          f"{len(bld)} rows, e.g. " + "; ".join(f"{f.get('quantity')} {f.get('delta_cm')} cm" for f in bld[:3]))
    fx = [f for f in it1["fixes"] if f["cause"] == "build" and "O1.cw" in f["rebuild"]]
    check("K2 the loop rebuilds the curtain wall from the spec (O1.cw)", bool(fx), json.dumps(fx[:1], ensure_ascii=False)[:200])
    st2 = json.loads((OUT / house / "state.json").read_text(encoding="utf-8"))["elements"]
    ev = [e for it in lg["iterations"][1:] for e in it["evaluated"] if e["item"] == "O1"]
    check("K2 restored: new element, rows of O1 in tolerance, verify PASS", st2["O1.cw"]["guid"] != g0 and bool(ev) and ev[0]["kept"] and lg["verify_pass"],
          f"guid {g0[:8]} -> {st2['O1.cw']['guid'][:8]}; {json.dumps(ev[:1], ensure_ascii=False)[:160]}")
    snap2 = json.dumps(sc.load(tmp_corr if tmp_corr.exists() else None, house)["corrections"], sort_keys=True)
    check("K2 the spec was not changed (class build fixes the model, not the spec)", snap == snap2)


# -- pilot B ------------------------------------------------------------------------------------------------------------
def real_b(port: int) -> None:
    lg1 = I.run_b(port, I.MAX_ITER)
    lg2 = I.run_b(port, I.MAX_ITER)
    f1 = lg1["first_findings"]
    check("K3 HOF_B: every deviation (and every unreadable bar) has one class with evidence and a crop",
          all(f["cls"] in ("build", "spec", "ambiguous") and f.get("reason") and (f["source"] in ("rows", "hedge") or f.get("crop") and Path(f["crop"]).exists()) for f in f1),
          f"{len(f1)} findings: " + ", ".join(f"{c}: {sum(f['cls'] == c for f in f1)}" for c in ("build", "spec", "ambiguous")))
    cw15 = [f for f in f1 if f["item"] == "CW_1NP_15" and f["source"] in ("bar", "unreadable")]
    ok = bool(cw15) and all((f["cls"] == "spec" and f.get("fix")) or (f["cls"] == "ambiguous" and f.get("crop")) for f in cw15)
    check("K3 HOF_B CW_1NP_15 bar 1 (5.1 cm, double bar): corrected with evidence or ambiguous with a crop", ok,
          "; ".join(f"{f['edge']} {f['delta_cm']} cm -> {f['cls']}: {f['reason'][:150]}" for f in cw15))
    conf = [f for f in lg2["final_findings"] if f.get("confirmed")]
    bad = [f["item"] + " " + f["edge"] for f in conf
           if not any(r.get("readable") and not r.get("louvred") and r.get("ratio", 0) >= I.CONFIRM_RATIO and abs(r["delta_m"]) * 100 <= f["tol_cm"]
                      for r in (f.get("evidence") or {}).get("rows", []))]
    unv = [f for f in lg2["final_findings"] if f.get("unverified")]
    check("K6 HOF_B (G2 nález 5): 'model potvrdený' only with a clear bar (ratio >= 1.5) in a band without blinds; the rest is "
          "'neoverené' or nejednoznačné", not bad, f"confirmed {len(conf)}, unverified {len(unv)}; bad {bad}")
    check("K4 HOF_B: second run of the loop = 0 fixes, verify (rows) PASS", lg2["fixes_total"] == 0 and lg2["verify_pass"],
          f"run 1: {lg1['fixes_total']}, run 2: {lg2['fixes_total']}")
    p = I.OUT_B / "report_G2.md"
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    check("K5 report_G2.md of pilot B written (CC BY attribution, what remains with crops)", "HOF_B" in txt and "CC BY 4.0" in txt and "Čo ostáva" in txt, str(p))
    rb = json.loads((I.OUT_B / "HOF_B" / "report_b.json").read_text(encoding="utf-8"))["result"]
    col = [e for e in rb["photo_edges"] if e["item"] in ("STLP_L", "STENA_Z")]
    check("K7 HOF_B (N6): column / wall edges judged sample by sample (never the median of 2), >= 2 readable samples each, "
          "every sample within 3 cm", col and all(e["rule"].startswith("každá") and e["readable_n"] >= 2 and e["ok"] for e in col),
          "; ".join(f"{e['item']} {e['edge']}: n {e['readable_n']}/{e['n']}, max {e['max_cm']} cm, {e['rule']}" for e in col))
    f05 = [f for f in lg2["final_findings"] if f["item"] == "CW_2NP_05" and f.get("edge", "").startswith("stĺpik 1")]
    rows05 = [r for f in f05 for r in (f.get("evidence") or {}).get("rows", [])]
    check("K7 HOF_B CW_2NP_05 bar 1 (N9): the railing post in front of the parapet band is masked; the reason does not call it a "
          "mullion of the curtain wall", (not f05) or (any(r.get("masked") for r in rows05) and all("pás bez žalúzie dáva lištu mimo modelu" not in f["reason"] for f in f05)),
          "; ".join(f"{f['cls']}: {f['reason'][:120]}" for f in f05) or "no finding (bar within tolerance after masking)")


def regression(script: str, args: list[str]) -> None:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(TESTS / script)] + args, capture_output=True, text=True, timeout=7200, encoding="utf-8",
                       errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    summ = [ln.strip() for ln in p.stdout.splitlines() if "checks passed" in ln]
    check(f"K4 {script} {' '.join(args)} passes after the loop", p.returncode == 0, (summ[-1] if summ else p.stdout[-300:]) + f" ({time.monotonic() - t0:.0f} s)")
    if p.returncode != 0:
        print("\n".join(ln for ln in p.stdout.splitlines() if "FAIL" in ln)[:3000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--regression", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    args = ap.parse_args()
    offline()
    if not args.offline:
        ac = client(args.port)
        start = ac.project_path()
        check("open project is pilot A at the start", start.lower() == PROJECT_A.lower(), start)
        if start.lower() == PROJECT_A.lower():
            real_a(args.port)
            artificial_spec(args.port)
            artificial_build(args.port)
            I.write_g2_a()
            if args.regression:
                regression("test_pilot_a.py", ["--port", str(args.port), "--save"])
            else:
                client(args.port).tapir("SaveProject", {}, timeout=300)
            if not args.skip_b and switch(args.port, PROJECT_B):
                real_b(args.port)
                if args.regression:
                    regression("test_pilot_b.py", ["--port", str(args.port), "--save"])
                switch(args.port, PROJECT_A)
            end = client(args.port).project_path()
            check("pilot A is open again at the end", end.lower() == PROJECT_A.lower(), end)
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (" - FAIL" if failed else " - PASS"))
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail[:300]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

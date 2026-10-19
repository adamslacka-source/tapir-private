"""Adapter: ONE photo of a facade -> facade-spec/1 (pilot B, ADR-001 sections 3-5: ingest, rectify, extract).

First real run "image -> spec -> BIM" without prepared data: the facade is read from the photo itself, nothing comes from
a pilot script. Photo of pilot B: "Frontsicht Westteil Suedfassade Gebaeude B HAW Hof" - PantheraLeo1359531, CC BY 4.0,
Wikimedia Commons (https://creativecommons.org/licenses/by/4.0); every image this module writes is a derivative
(rectified, cropped, overlaid) and carries that attribution.

The photo is DATA: this module only reads its pixels and EXIF (PIL / OpenCV) and is run isolated
(`python -I from_photo.py`), from outside the input folder. Nothing in the input folder is executed.

  ingest   SHA-1 of the file = the one recorded in zdroj.md, size, EXIF (camera, lens, focal length, date)
  rectify  the photo is a frontal view (the author's description; verticals are parallel). Long horizontal edges of
           the facade (fascia, blind boxes = red bands) are measured column by column: they are NOT straight in the
           photo (they drop ~12 px towards the right edge - not barrel distortion, the drop is the same for all lines,
           so it is a y-warp of the image, e.g. from an upright correction). A common smooth y-shift dy(x) is fitted
           to all lines and removed (remap). Verticals (pier edges) are checked and reported.
  camera   pinhole of a frontal view: x = cx + f (s - s_c) / Z, y = cy - f (v - v_c) / Z, Z = D0 - w (w out of the
           reference plane towards the camera). From the photo alone:
             cx      the console that projects vertical (the brackets under the walkways are perpendicular to the facade)
             k_r     (D0 - w_r) / D0: x of the console roots (on the slab edge, w = 0) against x of the railing posts at
                     the walkway edge (w = w_r) - linear fit over ~40 consoles
             cy      vanishing point of the horizontal lines perpendicular to the facade (bars of the end railing)
             k_f     (D0 - w_f) / D0 of the roof edge: soffit joints run from the fascia back to the facade
             f       EXIF focal length / sensor width (Sony ILCE-7RM4: 35.7 mm = 9504 px)  ODHAD (upright correction)
           scale: height of the walkway railing 1.05 m (ODHAD +-10 %, task) on the railing plane -> scale of the
           reference plane m0 = k_r * h_px / 1.05; D0 = f / m0; w_r = D0 (1 - k_r); w_f = D0 (1 - k_f).
           Checks: door leaf of the pier bay (2.10 m expected) and the module of the console spacing.
  extract  floors (walkway tops), red bands, head boxes, roof edge, curtain wall axes per floor (dark mullions through
           all bands of a floor; door jambs only below the transom), transoms, door fields (glass instead of the
           perforated parapet panel), louvre bottoms per field, consoles, railing bars, piers, the recessed door bay,
           the round column under the overhang. Every value: src S1 + conf (meranie = measured on the photo with the
           camera of the photo; pravidlo = rule / raster continued under the tree; odhad = depth or product).

Outputs (default C:/FacadePilot/pilot_B):
  out/HOF_B.facade-spec.json          the spec (validated by spec_validate.py - separate step, not isolated)
  work/HOF_B_rect.jpg (+ _1800)       rectified photo (CC BY 4.0 derivative)
  work/HOF_B_measure.json             every reading: pixel, refined pixel, metric value, plane, conf
  work/HOF_B_readings.png             readings drawn over the rectified photo (check by eye)

    python -I from_photo.py [--photo <jpg>] [--root C:/FacadePilot/pilot_B]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(r"C:\FacadePilot\pilot_B")
PHOTO_NAME = "Frontsicht_Westteil_Suedfassade_Gebaeude_B_HAW_Hof_20210815_HOF03743.jpg"
PHOTO_SHA1 = "b6b65321746e87a4e55fb843fe07f9bb53b45283"
ATTRIBUTION = ("Foto: \"Frontsicht Westteil Suedfassade Gebaeude B HAW Hof\" - PantheraLeo1359531, CC BY 4.0 "
               "(creativecommons.org/licenses/by/4.0), Wikimedia Commons; upravené (rektifikácia, výrez, prekrytie modelom)")
SPEC_ID = "HOF_B"
SENSOR_W_MM = 35.7                 # Sony ILCE-7RM4 sensor width; the photo is 9504 px wide = the full sensor width
RAIL_H = 1.05                      # m - scale reference (task: ODHAD +-10 %)
DOOR_CHECK = 2.10                  # m - control (task)
ORIGIN_XY = (0.0, -40.0)           # model position of s = 0 on the facade plane: south of the template content (x/y -6..28)
CW_DEPTH_W = -0.20                 # m - outer face of the CW frames behind the blind boxes (ODHAD, raffstore zone)

# ---- pixel readings by eye (full-resolution crops, agent reading), refined by the code below --------------------------------
# x of the walkway left end (corner post), the CW ends, the piers, the round column; approximate y of the lines.
READ = {
    "fascia_top": 1452, "red1_top": 1757, "red1_bot": 1795, "red2_top": 2518, "red2_bot": 2560,
    "gf_box_top": 3295, "gf_box_bot": 3346, "gf_base": 3944,
    "walk_upper_bar": 2337, "walk_lower_bar": 3149, "rail_upper_top": 2113, "rail_lower_top": 2923,
    "cw_left": 647, "pier_l": (7003, 7125), "pier_r_side": 7298, "pier_r": (7395, 7508), "cw_right_start": 7510,
    "column_l": (52, 165), "column_l_top": 1875, "downpipes": (272, 6712),
    "bay_door_leaf_top": 3470, "bay_door_leaf_bot": 3955, "bay_lintel": (3205, 3305),
    "tree_x": 8350,                # right of it the upper floors are hidden by leaves
    "walk_corner": 25,             # corner post of the walkways at the left end
    "louvre_bottom": {"2NP": 2232, "1NP": 2998, "GF": None},   # GF: all blinds down to the base
}
# curtain wall axes read by eye on full-resolution crops of every floor (x px; m = mullion through the whole floor,
# j = door jamb that ends at the transom), door fields; refined on the photo (dark bar centre) - see rec["axes"].
READ_AXES = {
    "2NP": {"floor": "2NP", "m": [649, 1081, 1338, 1986, 2634, 3152, 3670, 3926, 4322, 4715, 4977, 5502, 6015, 6278, 6677, 6990],
            "j": [4450], "doors": [(1081, 1338), (4450, 4715)]},
    "1NP": {"floor": "1NP", "m": [647, 1079, 1338, 1985, 2633, 3151, 3662, 4317, 4712, 4969, 5501, 6014, 6676, 6990],
            "j": [3413, 5197], "doors": [(1079, 1338), (3413, 3662), (4969, 5197)]},
    # ground floor: gaps between the blind units (blind plane, ~0.27 m in front of the upper mullions - see gf_plane);
    # 6688 = behind the downpipe (taken from 1.NP 6667 through the plane ratio)
    "GF": {"floor": "GF", "m": [645, 1302, 1973, 2610, 3134, 3660, 4316, 4711, 5501, 6029, 6688, 6990], "j": [], "doors": []},
    # right of the piers: the facade goes on beyond the photo edge; upper floors hidden by the tree right of tree_x
    # (their axes are taken from the ground floor); door 7597-7850 on both upper floors
    "2NPr": {"floor": "2NP", "m": [7510, 7597, 7850], "j": [], "doors": [(7597, 7850)]},
    "1NPr": {"floor": "1NP", "m": [7510, 7597, 7850], "j": [], "doors": [(7597, 7850)]},
    "GFr": {"floor": "GF", "m": [7510, 7871, 8405, 9061], "j": [], "doors": []},
}
# floors: name -> (head band y-ranges for mullions, parapet band, approx transom ys, approx head / parapet-top ys)
FLOORS = {
    "2NP": {"bands": [(1830, 1910), (1940, 2020), (2045, 2225), (2245, 2335)], "par_band": (2250, 2330),
            "head": 1797, "transoms": [1921, 2030], "par_top": 2238, "walk_bar": "walk_upper_bar"},
    "1NP": {"bands": [(2590, 2670), (2700, 2780), (2800, 3000), (3050, 3135)], "par_band": (3050, 3135),
            "head": 2560, "transoms": [2685, 2792], "par_top": 3043, "walk_bar": "walk_lower_bar"},
    "GF": {"bands": [(3400, 3600), (3600, 3880)], "par_band": None,
           "head": 3346, "transoms": [3428, 3537, 3745], "par_top": None, "walk_bar": None},
}


# ============================================================================== ingest
def ingest(path: Path) -> dict:
    data = path.read_bytes()
    sha1 = hashlib.sha1(data).hexdigest()
    if sha1 != PHOTO_SHA1:
        raise SystemExit(f"{path}: SHA-1 {sha1} != {PHOTO_SHA1} (zdroj.md) - not the verified photo")
    im = Image.open(path)
    exif = {ExifTags.TAGS.get(k, k): v for k, v in im.getexif().items()}
    sub = im.getexif().get_ifd(0x8769)
    exif.update({ExifTags.TAGS.get(k, k): v for k, v in sub.items()})
    keep = {k: str(exif.get(k)) for k in ("Make", "Model", "LensModel", "FocalLength", "FNumber", "ExposureTime", "ISOSpeedRatings",
                                          "DateTimeOriginal", "Orientation") if k in exif}
    return {"file": str(path), "sha1": sha1, "bytes": len(data), "size": list(im.size), "exif": keep,
            "focal_mm": float(exif.get("FocalLength", 20.0))}


# ============================================================================== 1-D measurement helpers
def _sub(d: np.ndarray, i: int) -> float:
    """Sub-pixel offset of the extremum at i (parabola through i-1, i, i+1)."""
    if 0 < i < len(d) - 1:
        a, b, c = d[i - 1], d[i], d[i + 1]
        den = a - 2 * b + c
        if abs(den) > 1e-9:
            return float(0.5 * (a - c) / den)
    return 0.0


def edge_in(prof: np.ndarray, sign: int) -> tuple[float, float]:
    """Strongest step of the given sign (+1 = brighter after) in a profile: (position between samples, strength)."""
    d = np.diff(prof) * sign
    i = int(np.argmax(d))
    return i + 0.5 + _sub(d, i), float(d[i])


def peak_in(prof: np.ndarray, dark: bool = False) -> tuple[float, float]:
    p = -prof if dark else prof
    base = np.median(p)
    i = int(np.argmax(p))
    return i + _sub(p, i), float(p[i] - base)


def hprof(img: np.ndarray, x0: int, x1: int, y0: int, y1: int) -> np.ndarray:
    """Row profile (median over the columns x0..x1: robust to thin occluders such as posts and bolts)."""
    return np.median(img[y0:y1, max(0, x0):x1], axis=1)


def vprof(img: np.ndarray, x0: int, x1: int, y0: int, y1: int) -> np.ndarray:
    return np.median(img[max(0, y0):y1, x0:x1], axis=0)


def h_edge(img, xc: int, y_guess: float, sign: int, half_w: int = 25, half_h: int = 14) -> tuple[float, float]:
    y0 = int(round(y_guess)) - half_h
    pos, st = edge_in(hprof(img, xc - half_w, xc + half_w, y0, y0 + 2 * half_h), sign)
    return y0 + pos, st


def peaks1d(a: np.ndarray, height: float, distance: int) -> list[int]:
    idx = [i for i in range(1, len(a) - 1) if a[i] >= height and a[i] >= a[i - 1] and a[i] >= a[i + 1]]
    idx.sort(key=lambda i: -a[i])
    keep: list[int] = []
    for i in idx:
        if all(abs(i - j) >= distance for j in keep):
            keep.append(i)
    return sorted(keep)


def robust_lstsq(A: np.ndarray, b: np.ndarray, k: float = 3.0, it: int = 6) -> tuple[np.ndarray, np.ndarray]:
    keep = np.ones(len(b), bool)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    for _ in range(it):
        r = A @ sol - b
        mad = np.median(np.abs(r[keep])) + 1e-9
        new = np.abs(r) <= max(k * 1.4826 * mad, 0.75)
        if new.sum() < A.shape[1] + 2 or (new == keep).all():
            keep = new if new.sum() >= A.shape[1] + 2 else keep
            break
        keep = new
        sol = np.linalg.lstsq(A[keep], b[keep], rcond=None)[0]
    return sol, keep


# ============================================================================== rectify
RECT_LINES = {
    # name: (approx y, channel, sign, x ranges)
    "fascia_top": (READ["fascia_top"], "g", -1, [(150, 7750)]),
    "red1_top": (READ["red1_top"], "r", +1, [(700, 6650), (7560, 8250)]),
    "red1_bot": (READ["red1_bot"], "r", -1, [(700, 6650), (7560, 8250)]),
    "red2_top": (READ["red2_top"], "r", +1, [(700, 6650), (7560, 8250)]),
    "red2_bot": (READ["red2_bot"], "r", -1, [(700, 6650), (7560, 8250)]),
}
SKIP_X = [(240, 310), (6680, 6750)]           # downpipes


def channels(bgr: np.ndarray) -> dict:
    f = bgr.astype(np.float32)
    return {"g": f.mean(axis=2), "r": f[..., 2] - 0.5 * (f[..., 0] + f[..., 1])}


def measure_lines(ch: dict, step: int = 120) -> dict:
    out = {}
    for name, (y0, c, sign, ranges) in RECT_LINES.items():
        pts = []
        for xa, xb in ranges:
            for x in range(xa, xb, step):
                if any(a <= x <= b for a, b in SKIP_X):
                    continue
                y, st = h_edge(ch[c], x, y0, sign, half_w=20, half_h=16)
                if st > (6 if c == "r" else 8):
                    pts.append((x, y, st))
        out[name] = np.array(pts)
    return out


def fit_warp(lines: dict, cx: float) -> dict:
    """Common y-shift dy(x) = a1 t + a2 t^2 + a3 t^3, t = (x - cx) / 1000, plus one constant per line."""
    names = [n for n in lines if len(lines[n]) > 5]
    rows, rhs = [], []
    for i, n in enumerate(names):
        for x, y, _ in lines[n]:
            t = (x - cx) / 1000.0
            e = np.zeros(len(names) + 3)
            e[i] = 1.0
            e[len(names):] = [t, t * t, t ** 3]
            rows.append(e)
            rhs.append(y)
    A, b = np.array(rows), np.array(rhs)
    sol, keep = robust_lstsq(A, b)
    res = A @ sol - b
    before = {}
    for i, n in enumerate(names):
        ys = lines[n][:, 1]
        xs = lines[n][:, 0]
        p = np.polyfit(xs, ys, 1)                      # best straight line per line, before the warp
        before[n] = float(np.sqrt(np.mean((np.polyval(p, xs) - ys) ** 2)))
    return {"coef": [float(c) for c in sol[len(names):]], "cx": cx, "lines": names, "n": int(keep.sum()), "outliers": int((~keep).sum()),
            "rms_after_px": float(np.sqrt(np.mean(res[keep] ** 2))), "rms_line_before_px": before}


def dy_of(x: np.ndarray | float, warp: dict) -> np.ndarray | float:
    t = (np.asarray(x, float) - warp["cx"]) / 1000.0
    a1, a2, a3 = warp["coef"]
    return a1 * t + a2 * t * t + a3 * t ** 3


def apply_warp(bgr: np.ndarray, warp: dict, strip: int = 512) -> np.ndarray:
    h, w = bgr.shape[:2]
    xs = np.arange(w, dtype=np.float32)
    dy = dy_of(xs, warp).astype(np.float32)
    out = np.empty_like(bgr)
    for r0 in range(0, h, strip):
        r1 = min(h, r0 + strip)
        ys = np.arange(r0, r1, dtype=np.float32)[:, None]
        map_y = ys + dy[None, :]
        map_x = np.broadcast_to(xs[None, :], map_y.shape).astype(np.float32)
        out[r0:r1] = cv2.remap(bgr, map_x, np.ascontiguousarray(map_y), cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out


# ============================================================================== camera
def detect_posts(g: np.ndarray, band: tuple[int, int]) -> list[int]:
    prof = vprof(g, 0, g.shape[1], band[0], band[1])
    hp = prof - cv2.blur(prof.reshape(1, -1), (61, 1)).ravel()
    return peaks1d(hp, 25, 150)


def console_roots(g: np.ndarray, posts: list[int], band: tuple[int, int], cx0: float) -> list[tuple[int, float, float]]:
    prof = vprof(g, 0, g.shape[1], band[0], band[1])
    hb = prof - cv2.blur(prof.reshape(1, -1), (41, 1)).ravel()
    out = []
    for p in posts:
        lo, hi = (p - 10, p + 260) if p < cx0 else (p - 260, p + 10)
        lo, hi = max(lo, 0), min(hi, len(hb) - 1)
        seg = hb[lo:hi]
        j = int(np.argmax(seg))
        a = max(0, j - 8)
        wgt = np.clip(seg[a:j + 9], 0, None)
        xr = lo + float(wgt @ np.arange(a, a + len(wgt)) / wgt.sum()) if wgt.sum() > 0 else float(lo + j)
        out.append((p, xr, float(seg[j])))
    return out


def fit_consoles(pairs: list[tuple[int, float, float]], x_lo: float, x_hi: float) -> dict:
    P = np.array([(p, x) for p, x, st in pairs if x_lo < p < x_hi and st > 5], float)
    A = np.column_stack([np.ones(len(P)), P[:, 0]])
    sol, keep = robust_lstsq(A, P[:, 1])
    a, k = sol
    res = A @ sol - P[:, 1]
    return {"k": float(k), "cx": float(a / (1 - k)), "n": int(keep.sum()), "rms_px": float(np.sqrt(np.mean(res[keep] ** 2))),
            "pairs": [[float(p), float(x), bool(kk)] for (p, x), kk in zip(P, keep)]}


def vertical_console_cx(pairs, lo=3600, hi=5900) -> float:
    """x where the console projects vertical: zero of the slant (root x - post x) over the posts near the centre."""
    P = np.array([(p, x - p) for p, x, st in pairs if lo < p < hi and st > 5], float)
    sol, keep = robust_lstsq(np.column_stack([np.ones(len(P)), P[:, 0]]), P[:, 1])
    return float(-sol[0] / sol[1])


def lsd_segments(g: np.ndarray, x0, y0, x1, y1, min_len=60) -> list[tuple[float, float, float, float]]:
    lsd = cv2.createLineSegmentDetector(0)
    sub = np.ascontiguousarray(g[y0:y1, x0:x1].astype(np.uint8))
    L = lsd.detect(sub)[0]
    out = []
    if L is None:
        return out
    for a, b, c, d in L.reshape(-1, 4):
        if math.hypot(c - a, d - b) >= min_len:
            out.append((float(a + x0), float(b + y0), float(c + x0), float(d + y0)))
    return out


def estimate_cy(g, cx) -> dict:
    """Vanishing height of the end-railing bars at the left end of both walkways (horizontal, perpendicular to the
    facade): long LSD segments only; the median of each walkway, then their mean (the two walkways differ by ~200 px -
    reported as the uncertainty)."""
    per = {}
    for name, (y0, y1) in (("upper", (2095, 2360)), ("lower", (2900, 3170))):
        vals = []
        for a, b, c, d in lsd_segments(g, 20, y0, 640, y1, 100):
            if c == a:
                continue
            s = (d - b) / (c - a)
            if 0.15 < s < 0.6:
                xm, ym = 0.5 * (a + c), 0.5 * (b + d)
                vals.append(ym + s * (cx - xm))
        per[name] = {"cy": float(np.median(vals)), "n": len(vals), "values": [round(v) for v in vals]}
    cy = float(np.mean([p["cy"] for p in per.values()]))
    spread = float(abs(per["upper"]["cy"] - per["lower"]["cy"]) / 2)
    return {"cy": cy, "spread_px": spread, "per_walkway": per}


def soffit_ratio(g, cx, y_front: float, y_back: float) -> dict:
    """Soffit joints (fascia -> facade, perpendicular to it): x at the fascia bottom / at the facade -> k_f."""
    ks, used = [], []
    for xa, xb in ((150, 700), (6990, 7800)):
        for a, b, c, d in lsd_segments(g, xa, int(y_front) + 6, xb, int(y_back) - 6, min_len=80):
            if abs(d - b) < 60:
                continue
            if b > d:
                a, b, c, d = c, d, a, b
            dxdy = (c - a) / (d - b)
            if abs(dxdy) < 0.4 or abs(dxdy) > 3.0:
                continue
            xf = a + (y_front - b) * dxdy
            xbk = a + (y_back - b) * dxdy
            k = (xbk - cx) / (xf - cx)
            if 0.8 < k < 0.99:
                ks.append(k)
                used.append([round(a), round(b), round(c), round(d)])
    k = np.array(ks)
    return {"k_f": float(np.median(k)), "spread": float(np.median(np.abs(k - np.median(k))) * 1.4826) if len(k) > 1 else None,
            "n": len(k), "segments": used}


def railing_profile(g, posts: list[int], y_bar_guess: float, y_top_guess: float) -> dict:
    """Per field between two posts: top edge of the top rail and top of the walkway edge (rise after the gap under the
    bottom bar) -> height in px; also the bar centres."""
    hs, tops, edges, bars_all = [], [], [], []
    for p, q in zip(posts, posts[1:]):
        if not (700 < p < 6600 and 250 < q - p < 320):
            continue
        xc = int(0.5 * (p + q))
        if any(a <= xc <= b for a, b in SKIP_X):
            continue
        prof = hprof(g, xc - 20, xc + 20, int(y_top_guess) - 15, int(y_bar_guess) + 20)
        y0 = int(y_top_guess) - 15
        # top rail = first bright line, bottom bar = last bright line
        pk = peaks1d(prof - np.median(prof), 40, 8)
        if len(pk) < 6:
            continue
        bot_c = pk[-1]
        # top rail: the brightest line 205..245 px above the bottom bar (louvre slats behind the railing are weaker)
        cands = [p_ for p_ in pk if 205 <= bot_c - p_ <= 245]
        if not cands:
            continue
        top_c = max(cands, key=lambda p_: prof[p_])
        pk = [p_ for p_ in pk if p_ >= top_c]
        ytop = y0 + edge_in(prof[max(0, top_c - 8):top_c + 1], +1)[0] + max(0, top_c - 8)
        seg = prof[bot_c + 2:bot_c + 14]
        yedge = y0 + bot_c + 2 + edge_in(seg, +1)[0]
        hs.append(yedge - ytop)
        tops.append(ytop)
        edges.append(yedge)
        bars_all.append([y0 + i + _sub(prof, i) for i in pk])
    h = np.array(hs)
    return {"h_px": float(np.median(h)), "h_spread": float(np.median(np.abs(h - np.median(h))) * 1.4826), "n": len(h),
            "top_px": float(np.median(tops)), "edge_px": float(np.median(edges)),
            "bars_px": [float(np.median([b[i] for b in bars_all if len(b) == len(bars_all[0])])) for i in range(len(bars_all[0]))] if bars_all else []}


class Camera:
    """Frontal pinhole on the rectified photo. w = out of the reference plane (towards the camera)."""

    def __init__(self, cx, cy, f, D0, s_c=0.0, v_c=0.0):
        self.cx, self.cy, self.f, self.D0, self.s_c, self.v_c = cx, cy, f, D0, s_c, v_c

    def s(self, x, w=0.0):
        return self.s_c + (np.asarray(x, float) - self.cx) * (self.D0 - w) / self.f

    def v(self, y, w=0.0):
        return self.v_c + (self.cy - np.asarray(y, float)) * (self.D0 - w) / self.f

    def x(self, s, w=0.0):
        return self.cx + (np.asarray(s, float) - self.s_c) * self.f / (self.D0 - w)

    def y(self, v, w=0.0):
        return self.cy - (np.asarray(v, float) - self.v_c) * self.f / (self.D0 - w)

    def as_dict(self):
        return {"cx": self.cx, "cy": self.cy, "f": self.f, "D0": self.D0, "s_c": self.s_c, "v_c": self.v_c}


# ============================================================================== extract helpers
def floor_axes(g, fl: dict, posts: list[int], cand_lo=600, cand_hi=7000) -> dict:
    """Mullion candidates of one floor: dark in EVERY band (through-going) or only in the lower bands (door jamb that
    ends at the transom). Posts of the railing (bright, in front) and the downpipes are masked."""
    profs = []
    for y0, y1 in fl["bands"]:
        prof = vprof(g, 0, g.shape[1], y0, y1)
        profs.append(cv2.blur(prof.reshape(1, -1), (31, 1)).ravel() - prof)
    full = np.minimum.reduce(profs)
    low = np.minimum.reduce(profs[-2:]) if len(profs) > 2 else full
    out = {"full": [], "low": []}
    for kind, d, th in (("full", full, 6.0), ("low", low, 18.0)):
        for c in peaks1d(d, th, 40):
            if not (cand_lo < c < cand_hi) or any(a - 15 <= c <= b + 15 for a, b in SKIP_X):
                continue
            if kind == "full" and any(abs(c - p) <= 12 for p in posts):
                continue
            out[kind].append((int(c) + _sub(d, int(c)), float(d[c])))
    return out


def head_dark(g, x: float, bands) -> list[float]:
    vals = []
    for y0, y1 in bands:
        prof = vprof(g, int(x) - 40, int(x) + 41, y0, y1)
        base = np.median(prof)
        j = int(np.argmin(prof[28:53])) + 28
        vals.append(float(base - prof[j]))
    return vals


def dark_center(g, x_guess: float, y0: int, y1: int, half: int = 14) -> tuple[float, float]:
    prof = vprof(g, int(x_guess) - half, int(x_guess) + half + 1, y0, y1)
    pos, st = peak_in(prof, dark=True)
    return int(x_guess) - half + pos, st


def transom_y(g, xs: list[tuple[float, float]], y_guess: float, half: int = 12) -> tuple[float, float, int]:
    """Centre of a horizontal dark bar (transom) as the median over fields."""
    ys, sts = [], []
    for a, b in xs:
        if b - a < 120:
            continue
        xc = int(0.5 * (a + b))
        prof = hprof(g, xc - int(0.3 * (b - a)), xc + int(0.3 * (b - a)), int(y_guess) - half, int(y_guess) + half + 1)
        pos, st = peak_in(prof, dark=True)
        ys.append(int(y_guess) - half + pos)
        sts.append(st)
    return float(np.median(ys)), float(np.median(sts)), len(ys)


def louvre_bottom(g, a: float, b: float, y_exp: float, half: int = 25) -> tuple[float, float]:
    """Bottom rail of a raffstore in a field: the strongest brightness step within +-half px of the bottom read by eye
    for the floor (all blinds of a floor were let down to about the same height); median over the inner field width."""
    x0, x1 = int(a + 0.15 * (b - a)), int(b - 0.15 * (b - a))
    y0 = int(y_exp) - half
    prof = hprof(g, x0, x1, y0, int(y_exp) + half + 1)
    d = np.abs(np.diff(prof))
    i = int(np.argmax(d))
    return y0 + i + 0.5 + _sub(d, i), float(d[i])


def field_colour(bgr, a: float, b: float, y0: int, y1: int, posts: list[int]) -> dict:
    xs = [x for x in range(int(a) + 12, int(b) - 12) if all(abs(x - p) > 14 for p in posts) and not any(c <= x <= d for c, d in SKIP_X)]
    sub = bgr[y0:y1][:, xs].astype(np.float32)
    return {"b_minus_r": float(np.median(sub[..., 0] - sub[..., 2])), "lum": float(np.median(sub.mean(axis=2)))}


def val(v, conf="meranie", src="S1", note=None, nd=3):
    num = isinstance(v, (int, float, np.floating)) and not isinstance(v, bool)
    d = {"v": (v if isinstance(v, int) else round(float(v), nd)) if num else v, "src": src, "conf": conf}
    if conf in ("odhad", "default") and src is None:
        d["src"] = None
    if note:
        d["note"] = note
    return d


def rng(a, b, conf="meranie", src="S1", note=None):
    d = {"v": [round(float(a), 3), round(float(b), 3)], "src": src, "conf": conf}
    if note:
        d["note"] = note
    return d


def vlist(values, conf="meranie", src="S1", note=None):
    d = {"v": [round(float(x), 3) for x in values], "src": src, "conf": conf}
    if note:
        d["note"] = note
    return d


# ============================================================================== main extraction
def extract(root: Path, photo: Path) -> dict:
    t0 = dt.datetime.now()
    meta = ingest(photo)
    bgr = cv2.imread(str(photo), cv2.IMREAD_COLOR)
    H, W = bgr.shape[:2]
    rec: dict = {"ingest": meta, "readings_by_eye": READ}

    # ---- rectify -------------------------------------------------------------------------------------------------
    ch0 = channels(bgr)
    lines0 = measure_lines(ch0)
    cx_center = W / 2.0
    warp = fit_warp(lines0, cx_center)
    rect = apply_warp(bgr, warp)
    ch = channels(rect)
    g = ch["g"]
    lines1 = measure_lines(ch)
    after = {}
    for n, pts in lines1.items():
        if len(pts) > 5:
            after[n] = {"n": len(pts), "rms_px": float(np.std(pts[:, 1])), "y": float(np.median(pts[:, 1])),
                        "range_px": float(np.percentile(pts[:, 1], 95) - np.percentile(pts[:, 1], 5))}
    warp["after"] = after
    warp["max_shift_px"] = float(np.max(np.abs(dy_of(np.arange(W), warp))))
    # verticals: pier edges and the left end of the CW over the height
    vert = {}
    for name, xg in (("pier_l_left", READ["pier_l"][0]), ("pier_r_right", READ["pier_r"][1]), ("cw_left_end", READ["cw_left"])):
        pts = []
        for yc in range(1850, 3900, 150):
            if name == "cw_left_end" and 2330 < yc < 2580 or 3150 < yc < 3350:
                continue
            prof = vprof(g, int(xg) - 25, int(xg) + 26, yc - 30, yc + 30)
            pos, st = edge_in(prof, -1 if name == "pier_r_right" else +1) if name != "cw_left_end" else edge_in(prof, -1)
            pts.append((yc, int(xg) - 25 + pos))
        P = np.array(pts)
        slope = np.polyfit(P[:, 0], P[:, 1], 1)[0]
        vert[name] = {"slope_px_per_1000px": round(float(slope * 1000), 2), "x_median": round(float(np.median(P[:, 1])), 1)}
    warp["verticals"] = vert
    rec["rectify"] = warp

    # ---- camera ----------------------------------------------------------------------------------------------------
    posts = {"upper": detect_posts(g, (2370, 2395)), "lower": detect_posts(g, (3175, 3200))}
    pairs = {"upper": console_roots(g, posts["upper"], (2480, 2505), cx_center),
             "lower": console_roots(g, posts["lower"], (3270, 3295), cx_center)}
    cx_v = float(np.mean([vertical_console_cx(pairs["upper"]), vertical_console_cx(pairs["lower"])]))
    fits = {k: fit_consoles(pairs[k], 750, 6650) for k in pairs}
    # k with cx fixed at the vertical console: x_root - cx = k (x_post - cx)
    kk = []
    for k_ in pairs:
        P = np.array([(p, x) for p, x, st in pairs[k_] if 750 < p < 6650 and st > 5 and abs(p - cx_v) > 600], float)
        r = (P[:, 1] - cx_v) / (P[:, 0] - cx_v)
        kk += list(r)
    kk = np.array(kk)
    k_r = float(np.median(kk))
    k_r_spread = float(np.median(np.abs(kk - k_r)) * 1.4826)
    cyd = estimate_cy(g, cx_v)
    cy = cyd["cy"]
    f = meta["focal_mm"] / SENSOR_W_MM * W
    rails = {"upper": railing_profile(g, posts["upper"], READ["walk_upper_bar"], READ["rail_upper_top"]),
             "lower": railing_profile(g, posts["lower"], READ["walk_lower_bar"], READ["rail_lower_top"])}
    h_px = rails["upper"]["h_px"]                     # the upper walkway: clean background (lower one: louvres behind = check)
    m_r = h_px / RAIL_H                               # px per m on the railing plane
    m0 = k_r * m_r                                    # px per m on the reference plane (slab edges, w = 0)
    D0 = f / m0
    w_r = D0 * (1 - k_r)
    red1_top = after["red1_top"]["y"]
    fascia_bot = None
    xs_fb = [x for x in range(300, 7700, 150) if not any(a <= x <= b for a, b in SKIP_X)]
    fb = [h_edge(g, x, 1534, +1, 20, 12)[0] for x in xs_fb]
    fascia_bot = float(np.median(fb))
    soff = soffit_ratio(g, cx_v, fascia_bot, red1_top)
    k_f = soff["k_f"]
    w_f = D0 * (1 - k_f)
    cam = Camera(cx_v, cy, f, D0)
    rec["camera"] = {"cx_vertical_console": cx_v, "console_fits": {k: {kk_: v for kk_, v in fits[k].items() if kk_ != "pairs"} for k in fits},
                     "k_r": k_r, "k_r_spread": k_r_spread, "cy": cyd, "f_px": f, "rail": rails, "h_rail_px": h_px,
                     "m_rail_px_per_m": m_r, "m0_px_per_m": m0, "D0": D0, "w_r": w_r, "soffit": soff, "w_f": w_f,
                     "fascia_bottom_px": fascia_bot}
    rec["posts"] = posts
    rec["console_pairs"] = {k: [[p, round(x, 1), round(st, 1)] for p, x, st in v] for k, v in pairs.items()}

    # ---- v origin: foot of the ground floor curtain wall (base line over the grass) ---------------------------------
    base = [h_edge(g, x, READ["gf_base"], -1, 25, 14) for x in range(800, 6600, 200) if not any(a <= x <= b for a, b in SKIP_X)]
    y_base = float(np.median([y for y, st in base if st > 8]))
    cam.v_c = -(cam.cy - y_base) * (D0 - CW_DEPTH_W) / f       # v = 0 at the base of the CW (on its plane)
    # s origin: left edge of the photo on the roof edge plane (the leftmost modelled point; the roof goes on beyond it)
    cam.s_c = -float((0 - cam.cx) * (D0 - w_f) / f)
    rec["origin"] = {"y_base_px": y_base, "v_c": cam.v_c, "s_c": cam.s_c}

    def S(x, w=CW_DEPTH_W):
        return float(cam.s(x, w))

    def V(y, w=CW_DEPTH_W):
        return float(cam.v(y, w))

    L = round(S(W - 1, CW_DEPTH_W), 3)                     # right edge of the photo on the CW plane
    rec["L"] = L

    # ---- horizontal levels ---------------------------------------------------------------------------------------
    lv = {}
    lv["red1_top"], lv["red1_bot"] = V(after["red1_top"]["y"], 0.0), V(after["red1_bot"]["y"], 0.0)
    lv["red2_top"], lv["red2_bot"] = V(after["red2_top"]["y"], 0.0), V(after["red2_bot"]["y"], 0.0)
    lv["fascia_top"], lv["fascia_bot"] = V(after["fascia_top"]["y"], w_f), V(fascia_bot, w_f)
    gfb = [h_edge(g, x, READ["gf_box_top"], -1, 25, 14)[0] for x in range(900, 6500, 300) if not any(a <= x <= b for a, b in SKIP_X)]
    gfb2 = [h_edge(g, x, READ["gf_box_bot"], +1, 25, 12)[0] for x in range(900, 6500, 300) if not any(a <= x <= b for a, b in SKIP_X)]
    lv["gf_box_top"], lv["gf_box_bot"] = V(np.median(gfb), 0.0), V(np.median(gfb2), 0.0)
    lv["walk_upper"] = V(rails["upper"]["edge_px"], w_r)
    lv["walk_lower"] = V(rails["lower"]["edge_px"], w_r)
    lv["rail_upper_top"] = V(rails["upper"]["top_px"], w_r)
    lv["rail_lower_top"] = V(rails["lower"]["top_px"], w_r)
    lv["base"] = 0.0
    rec["levels_px"] = {"gf_box_top": float(np.median(gfb)), "gf_box_bot": float(np.median(gfb2))}
    rec["levels"] = {k: round(v, 4) for k, v in lv.items()}

    # ---- curtain wall axes per floor: automatic candidates (log) + reading by eye refined on the photo -------------
    allposts = sorted(posts["upper"] + posts["lower"])
    cand = {fl: floor_axes(g, FLOORS[fl], allposts) for fl in ("2NP", "1NP")}
    cand["GF"] = floor_axes(g, FLOORS["GF"], [], 600, 9500)
    rec["axes_candidates"] = {fl: {k: [[round(x, 1), round(st, 1)] for x, st in v] for k, v in c.items()} for fl, c in cand.items()}
    axes, agree = {}, {}
    for sec, rd in READ_AXES.items():
        bands = FLOORS[rd["floor"]]["bands"]
        out = []
        for kind, xs in (("m", rd["m"]), ("j", rd.get("j", []))):
            for x in xs:
                band = bands[0] if (kind == "m" or rd["floor"] == "GF") else bands[-1]
                if x >= READ["tree_x"] and rd["floor"] != "GF":
                    out.append({"x": float(x), "kind": kind, "contrast": 0.0, "conf": "pravidlo", "by_eye": x,
                                "note": "skryté za stromom - os prevzatá z prízemia (raster)"})
                    continue
                if any(a - 15 <= x <= b + 15 for a, b in SKIP_X):
                    out.append({"x": float(x), "kind": kind, "contrast": 0.0, "conf": "pravidlo", "by_eye": x,
                                "note": "za odkvapovým zvodom - os prevzatá z poschodia (raster)"})
                    continue
                xr, st = dark_center(g, x, *band, half=12)
                ok = st >= 8 and abs(xr - x) <= 12
                out.append({"x": float(xr if ok else x), "kind": kind, "contrast": round(st, 1), "conf": "meranie", "by_eye": x,
                            **({} if ok else {"note": "odčítané okom na výreze (+-5 px), slabý kontrast lišty"})})
        out.sort(key=lambda a: a["x"])
        axes[sec] = out
        auto = [x for x, st in cand[rd["floor"]]["full"] + cand[rd["floor"]]["low"]]
        agree[sec] = {"matched": sum(1 for a in out if any(abs(a["x"] - c) <= 14 for c in auto)), "of": len(out)}
    rec["axes"] = axes
    rec["axes_auto_agreement"] = agree

    # ---- transoms (rows) per floor ------------------------------------------------------------------------------------
    rows = {}
    for fl in ("2NP", "1NP"):
        xs = [a["x"] for a in axes[fl] if a["kind"] == "m"]
        doors = [(axes_x(axes[fl], d0), axes_x(axes[fl], d1)) for d0, d1 in READ_AXES[fl]["doors"]]
        fields = [(a, b) for a, b in zip(xs, xs[1:]) if not any(abs(a - d0) < 20 or abs(b - d1) < 20 for d0, d1 in doors)]
        ys = []
        for yg in FLOORS[fl]["transoms"] + [FLOORS[fl]["par_top"]]:
            y, st, n = transom_y(g, fields, yg)
            ys.append({"y": y, "contrast": st, "n": n})
        rows[fl] = ys
    gf_field = (axes_x(axes["GF"], READ_AXES["GF"]["m"][2]), axes_x(axes["GF"], READ_AXES["GF"]["m"][3]))   # tilted-open slats
    rows["GF"] = [dict(zip(("y", "contrast", "n"), transom_y(g, [gf_field], yg, 10))) for yg in FLOORS["GF"]["transoms"]]
    rec["rows_px"] = rows

    # ---- door fields: glass instead of the perforated parapet panel -----------------------------------------------------
    door_check = {}
    for fl in ("2NP", "1NP"):
        y0, y1 = FLOORS[fl]["par_band"]
        res = []
        for sec in (fl, fl + "r"):
            xs = sorted(a["x"] for a in axes[sec])
            eye = [(axes_x(axes[sec], d0), axes_x(axes[sec], d1)) for d0, d1 in READ_AXES[sec]["doors"]]
            for a, b in zip(xs, xs[1:]):
                if b - a < 100:
                    continue
                c = field_colour(rect, a, b, y0, y1, allposts)
                rule = c["b_minus_r"] >= 40 or c["lum"] >= 140
                res.append({"sec": sec, "field": [round(a, 1), round(b, 1)], **{k: round(v, 1) for k, v in c.items()}, "rule_door": rule,
                            "by_eye_door": (a, b) in eye, "hidden": a >= READ["tree_x"]})
        door_check[fl] = res
    rec["doors"] = door_check

    # ---- louvre bottoms per field --------------------------------------------------------------------------------------
    louv = {}
    for fl in ("2NP", "1NP", "GF"):
        out = []
        for sec in (fl, fl + "r"):
            xs = sorted(a["x"] for a in axes[sec])
            for a, b in zip(xs, xs[1:]):
                if b - a < 60:
                    continue
                if READ["louvre_bottom"][fl] is None:
                    out.append({"sec": sec, "field": [a, b], "y": None, "conf": "meranie", "note": "spustené až po pätu"})
                elif a >= READ["tree_x"]:
                    out.append({"sec": sec, "field": [a, b], "y": None, "conf": "pravidlo", "note": "za stromom"})
                else:
                    yb, st = louvre_bottom(g, a, b, READ["louvre_bottom"][fl])
                    out.append({"sec": sec, "field": [a, b], "y": yb, "step": round(st, 1), "conf": "meranie"})
        louv[fl] = out
    rec["louvres_px"] = louv
    # slat pitch: autocorrelation of the slat texture in the fully lowered ground floor blinds
    seg = np.median(g[3450:3900, 2700:3080], axis=1)
    seg = seg - seg.mean()
    acor = np.correlate(seg, seg, "full")[len(seg) - 1:]
    rec["slat_pitch_px"] = int(np.argmax(acor[8:40]) + 8)

    # ---- piers, recessed bay, column, walkway edge -------------------------------------------------------------------
    def vedge(xg, y0, y1, sign, half=16):
        prof = vprof(g, int(xg) - half, int(xg) + half + 1, y0, y1)
        pos, st = edge_in(prof, sign)
        return int(xg) - half + pos, st

    pl0 = vedge(READ["pier_l"][0], 2700, 2950, +1)[0]
    pl1 = vedge(READ["pier_l"][1], 2700, 2950, -1)[0]
    pr_side = vedge(READ["pier_r_side"], 2700, 2950, +1)[0]
    pr0 = vedge(READ["pier_r"][0], 2700, 2950, +1)[0]
    pr1 = vedge(READ["pier_r"][1], 2700, 2950, -1)[0]
    k_bay = (pr_side - cam.cx) / (pr0 - cam.cx)
    w_bay = D0 - D0 / k_bay
    bay = {"pier_l_px": [pl0, pl1], "pier_r_px": [pr0, pr1], "pier_r_side_px": pr_side, "k": k_bay, "w_bay": w_bay}
    lt = [h_edge(g, x, READ["bay_door_leaf_top"], -1, 30, 14)[0] for x in (7170, 7200, 7230)]
    bay["leaf_top_px"] = float(np.median(lt))
    bay["leaf_h"] = V(bay["leaf_top_px"], w_bay)          # leaf from the floor (v = 0; the foot is hidden by the grass)
    bay["lintel_bot_px"] = {"GF": h_edge(g, 7210, READ["bay_lintel"][1], -1, 40, 16)[0], "1NP": h_edge(g, 7210, 2557, -1, 40, 14)[0],
                            "2NP": h_edge(g, 7210, 1830, -1, 40, 16)[0]}
    rec["bay"] = bay
    c0 = vedge(READ["column_l"][0], 2650, 2900, +1)[0]
    c1 = vedge(READ["column_l"][1], 2650, 2900, -1)[0]
    ctop = h_edge(g, int(0.5 * (c0 + c1)), READ["column_l_top"], +1, 20, 16)[0]
    z_col = (lv["red1_top"] - cam.v_c) * f / (cam.cy - ctop)          # depth: the column top meets the soffit at its facade height
    col = {"px": [c0, c1], "top_px": ctop, "Z": z_col, "w_axis": D0 - z_col, "d": (c1 - c0) * z_col / f,
           "s_axis": float(cam.s(0.5 * (c0 + c1), D0 - z_col))}
    rec["column"] = col
    plate = []
    for x in range(1500, 6400, 280):
        prof = hprof(g, x - 20, x + 20, int(rails["upper"]["edge_px"]) + 15, int(rails["upper"]["edge_px"]) + 50)
        plate.append(int(rails["upper"]["edge_px"]) + 15 + edge_in(prof, +1)[0])
    rec["walk_plate_bottom_px"] = float(np.median(plate))
    rec["walk_corner_px"] = vedge(READ["walk_corner"], 2150, 2330, +1, 18)[0]
    sp = [b - a for k_ in posts for a, b in zip(posts[k_], posts[k_][1:]) if 260 < b - a < 300 and 700 < a < 6700]
    rec["post_spacing_px"] = float(np.median(sp))

    return {"rec": rec, "cam": cam, "rect": rect, "g": g, "posts": posts, "pairs": pairs, "W": W, "H": H, "L": L, "lv": lv,
            "w_r": w_r, "w_f": w_f, "w_bay": w_bay, "S": S, "V": V, "axes": axes, "rows": rows, "louv": louv, "bay": bay, "col": col,
            "m_r": m_r, "t0": t0, "doors": door_check}


def axes_x(axl: list[dict], by_eye: float) -> float:
    """Refined x of the axis read by eye at by_eye."""
    return next(a["x"] for a in axl if a["by_eye"] == by_eye)


# ============================================================================== spec assembly
LOUVRE_W = (-0.14, -0.06)          # upper floors: louvre package between the CW face (-0.20) and the box face (0) - ODHAD
FRAME_W = 0.06                     # mullion / transom face width (dark bar ~12-13 px at 210 px/m)


def round_list(xs, nd=3):
    return [round(float(x), nd) for x in xs]


def assemble(X: dict) -> dict:
    rec, cam, lv = X["rec"], X["cam"], X["lv"]
    W = X["W"]
    w_r, w_f, w_bay = X["w_r"], X["w_f"], X["w_bay"]
    f, D0 = cam.f, cam.D0
    m_r = X["m_r"]

    # ---- ground floor plane: blind gaps vs the upper mullions (same s, other depth) -------------------------------------
    up = [a["x"] for a in X["axes"]["1NP"] + X["axes"]["1NPr"] if a["kind"] == "m" and a["conf"] == "meranie"]
    ratios = []
    inner = X["axes"]["GF"][1:-1] + X["axes"]["GFr"][1:]          # the CW ends abut piers / the building end: not a plane cue
    for a in inner:
        if a["conf"] != "meranie" or abs(a["x"] - cam.cx) < 600:
            continue
        near = [u for u in up if abs(u - a["x"]) < 60]
        if near:
            u = min(near, key=lambda u_: abs(u_ - a["x"]))
            ratios.append((a["x"] - cam.cx) / (u - cam.cx))
    ratio = float(np.median(ratios))
    w_gf_bl = (D0 - CW_DEPTH_W) * (1 - 1 / ratio) + CW_DEPTH_W          # blinds of the ground floor (w of the plane)
    rec["gf_plane"] = {"ratio": ratio, "n": len(ratios), "spread": float(np.std(ratios)), "w_blinds": w_gf_bl}
    W_GF_CW = round(w_gf_bl - 0.14, 3)                                   # GF glazing 0.14 behind its blinds (ODHAD)
    W_GF_LOUV = (round(w_gf_bl - 0.04, 3), round(w_gf_bl + 0.04, 3))

    # ---- s origin: the leftmost modelled point (column under the overhang) -------------------------------------------
    col = X["col"]
    s_col_left = float(cam.s(col["px"][0], col["w_axis"]))
    s_roof_left = float(cam.s(0, w_f))
    cam.s_c += -min(s_col_left, s_roof_left) + 0.05
    S = lambda x, w: round(float(cam.s(x, w)), 3)
    V = lambda y, w: round(float(cam.v(y, w)), 3)
    L = round(max(S(W - 1, CW_DEPTH_W), S(W - 1, W_GF_CW)), 3)
    rec["origin"].update({"s_c": cam.s_c, "L": L})

    floor_v = {"GF": 0.0, "1NP": round(lv["walk_lower"], 3), "2NP": round(lv["walk_upper"], 3)}
    head_v = {"GF": round(lv["gf_box_bot"], 3), "1NP": round(lv["red2_bot"], 3), "2NP": round(lv["red1_bot"], 3)}
    soffit_v = round(lv["red1_top"], 3)
    cw_w = {"GF": W_GF_CW, "1NP": CW_DEPTH_W, "2NP": CW_DEPTH_W}
    sec_plane = {"2NP": CW_DEPTH_W, "1NP": CW_DEPTH_W, "GF": w_gf_bl, "2NPr": CW_DEPTH_W, "1NPr": CW_DEPTH_W, "GFr": w_gf_bl}

    # piers (w = 0: flush with the slab edges, ODHAD) and the recessed bay
    bay = X["bay"]
    pl = [S(x, 0.0) for x in bay["pier_l_px"]]
    pr = [S(x, 0.0) for x in bay["pier_r_px"]]

    # ---- axes in s per section --------------------------------------------------------------------------------------
    def sec_axes(sec):
        return [dict(a, s=S(a["x"], sec_plane[sec])) for a in X["axes"][sec]]

    ax = {sec: sec_axes(sec) for sec in X["axes"]}
    # the upper right axes hidden by the tree: from the ground floor (same s)
    gf_right = [a for a in ax["GFr"] if a["x"] > 8000]
    for sec in ("2NPr", "1NPr"):
        for a in gf_right:
            ax[sec].append({"x": None, "s": a["s"], "kind": "m", "conf": "pravidlo", "contrast": 0.0, "by_eye": None,
                            "note": "za stromom - os z prízemia (rovnaké s)"})
        ax[sec].sort(key=lambda a: a["s"])

    spec_cw, objects, lin, zones, slabs, railings = [], [], [], [], [], []
    simpl: list[dict] = []
    assumptions: list[dict] = []

    # ---- rows per floor ------------------------------------------------------------------------------------------------
    rows_v = {}
    for fl in ("2NP", "1NP"):
        r = X["rows"][fl]
        rows_v[fl] = {"t1": V(r[0]["y"], CW_DEPTH_W), "t2": V(r[1]["y"], CW_DEPTH_W), "par": V(r[2]["y"], CW_DEPTH_W)}
    r = X["rows"]["GF"]
    rows_v["GF"] = [V(x["y"], W_GF_CW) for x in r]

    def cw_item(cid, s0, s1, fl, inner_s, kind, conf_rect, note, door=False, sec=""):
        v0, v1 = floor_v[fl], head_v[fl]
        if fl == "GF":
            vax = sorted(v for v in rows_v["GF"] if v0 + 0.2 < v < v1 - 0.2)
            vconf, vnote = "meranie", "priečky prízemia odčítané v poli so sklopenými lamelami (s 1973-2610 px), v ostatných poliach rovnaké (pravidlo)"
            cells = []
        elif door:
            vax = [rows_v[fl]["t1"]]
            vconf, vnote = "meranie", "priečka nad dverami = horná priečka podlažia (stred lišty)"
            cells = [{"col": 0, "row": 0, "panel": "door"}]
        else:
            vax = [rows_v[fl]["par"], rows_v[fl]["t2"], rows_v[fl]["t1"]]
            vconf, vnote = "meranie", "parapetný panel / sklo / nadsvetlík (stredy líšt, medián cez polia podlažia)"
            cols = len(inner_s) + 1
            cells = [{"col": c, "row": 0, "panel": "opaque"} for c in range(cols)]
        it = {"id": cid, "rect": {"v": round_list([s0, s1, v0, v1]), "src": "S1", "conf": conf_rect, "note": note},
              "system": "cw.hof_b", "reveal": val(-cw_w[fl], "odhad", None,
                                                    "vonkajšie líce rámov za čelom stropu / skrinkou žalúzie (raffstore 0,20 m, ODHAD)"
                                                    if fl != "GF" else
                                                    "prízemie: lamely 0,27 m pred stĺpikmi poschodí (pomer polôh v obraze, meranie), sklo 0,14 m za lamelami (ODHAD)"),
              "story": fl}
        if inner_s:
            it["u_axes"] = vlist(inner_s, "meranie", "S1", "stĺpiky (stred tmavej lišty, kamera fotky)")
        if fl == "GF":
            it["photo_w"] = val(round(w_gf_bl, 3), "meranie", "S1", "osi prízemia odčítané na medzerách medzi žalúziami (rovina lamiel)")
        if vax:
            it["v_axes"] = vlist(vax, vconf, "S1", vnote)
        if cells:
            it["cells"] = cells
        it["note"] = f"{sec} {'dvere' if door else 'pevné pole'}"
        return it

    n_cw = 0
    louv_fields = []
    for fl in ("2NP", "1NP", "GF"):
        for sec in (fl, fl + "r"):
            al = ax[sec]
            xs = [a for a in al if a["kind"] == "m" or a["kind"] == "j"]
            ss = [a["s"] for a in xs]
            # clip the ends at the piers (the CW abuts the pier behind its face)
            if sec in ("2NP", "1NP", "GF"):
                ss[-1] = min(ss[-1], pl[0])
            else:
                ss[0] = max(ss[0], pr[1])
                ss.append(L)
            door_s = []
            for d0, d1 in READ_AXES[sec]["doors"]:
                a0 = next(a["s"] for a in al if a.get("by_eye") == d0)
                a1 = next(a["s"] for a in al if a.get("by_eye") == d1)
                door_s.append((a0, a1))
            # split into runs: a door field is its own CW; consecutive fixed fields form one CW
            fields = list(zip(ss[:-1], ss[1:]))
            run = []

            def flush(run_):
                nonlocal n_cw
                if not run_:
                    return
                n_cw += 1
                inner = [b for a_, b in run_[:-1]]
                conf = "meranie" if run_[-1][1] < L - 1e-6 else "pravidlo"
                note = "pole fasády (stĺpik-priečka)" + ("; pravý koniec = okraj fotky, fasáda pokračuje" if conf == "pravidlo" else "")
                spec_cw.append(cw_item(f"CW_{fl}_{n_cw:02d}", run_[0][0], run_[-1][1], fl, inner, "fixed", "meranie", note, sec=sec))

            for a_, b_ in fields:
                is_door = any(abs(a_ - d0) < 0.02 and abs(b_ - d1) < 0.02 for d0, d1 in door_s)
                if is_door:
                    flush(run)
                    run = []
                    n_cw += 1
                    spec_cw.append(cw_item(f"CW_{fl}_{n_cw:02d}", a_, b_, fl, [], "door", "meranie",
                                           "francúzske dvere na ochodzu (sklo namiesto perforovaného parapetného panelu)", door=True, sec=sec))
                else:
                    run.append((a_, b_))
                louv_fields.append((fl, sec, a_, b_))
            flush(run)

    # ---- louvres (raffstore) per field --------------------------------------------------------------------------------
    pitch_m = rec["slat_pitch_px"] / (cam.f / (D0 - w_gf_bl))
    by_field = {}
    for fl in ("2NP", "1NP", "GF"):
        for e in X["louv"][fl]:
            by_field[(fl, round(e["field"][0], 1))] = e
    n_l = 0
    for fl, sec, a_, b_ in louv_fields:
        n_l += 1
        head = head_v[fl]
        if fl == "GF":
            bottom, conf, note = floor_v["GF"] + 0.02, "meranie", "spustené až po pätu (všetky polia prízemia)"
            wl = W_GF_LOUV
        else:
            # nearest field record of the floor (fields are the same as the axes of the floor)
            cands = [e for (f_, x0), e in by_field.items() if f_ == fl]
            xa = float(cam.x(a_, sec_plane[sec]))
            e = min(cands, key=lambda e_: abs(e_["field"][0] - xa))
            if e.get("y") is None:
                ys = [c["y"] for c in cands if c.get("y")]
                bottom, conf, note = V(float(np.median(ys)), -0.10), "pravidlo", "za stromom - výška ako ostatné polia podlažia"
            else:
                bottom, conf, note = V(e["y"], -0.10), "meranie", "spodná lišta žalúzie (krok jasu, medián cez šírku poľa)"
            wl = LOUVRE_W
        hgt = round(head - bottom, 3)
        if hgt < 0.15:
            continue
        objects.append({"id": f"LV_{fl}_{n_l:02d}", "kind": "louvers", "system": "gdl.louvers", "host": None,
                        "origin": {"v": [round(a_ + 0.0, 3), round(bottom, 3), wl[0]], "src": "S1", "conf": conf, "note": note},
                        "params": {
                            "A": val(round(b_ - a_, 3), "meranie", "S1", "šírka poľa medzi osami stĺpikov"),
                            "ZZYZX": val(hgt, conf, "S1", "od spodku skrinky po spodnú lištu (výška spustenia)"),
                            "iOrient": val(0, "meranie", "S1", "vodorovné lamely"),
                            "iSpacing": val(1, "pravidlo", "S1", "rozostup lamiel"),
                            "pitch": val(round(pitch_m, 3), "meranie", "S1", f"rozostup lamiel {rec['slat_pitch_px']} px (autokorelácia v prízemí)"),
                            "lamD": val(0.08, "odhad", None, "šírka lamely C80 (zdroj.md, ODHAD)"),
                            "lamT": val(0.01, "odhad", None, "hrúbka profilu lamely (ODHAD)"),
                            "lamAng": val(45.0, "odhad", None, "sklon lamiel (na čelnej fotke nečitateľný presne, ODHAD)", nd=1),
                            "offs": val(0.0, "pravidlo", "S1", "počiatok objektu = zadná rovina balíka"),
                            "bFrame": val(True, "meranie", "S1", "vodiace lišty + spodná lišta"),
                            "frameW": val(0.025, "odhad", None, "šírka vodiacej lišty (ODHAD)"),
                            "frameD": val(0.03, "odhad", None, "hĺbka vodiacej lišty (ODHAD)"),
                            "matLam": val("Kov - hliník", "odhad", None, "strieborné lamely (fotka), povrch šablóny"),
                            "matFrame": val("Kov - hliník", "odhad", None, "")},
                        "note": f"{sec}: raffstore pred poľom {a_:.2f}-{b_:.2f}"})
        objects[-1]["params"]["matFrame"].pop("note", None)
        objects[-1].pop("host")

    # ---- red bands / head boxes, roof edge, consoles, column, lintels -----------------------------------------------
    s_left_cw = min(spec_cw, key=lambda c: c["rect"]["v"][0])["rect"]["v"][0]
    for name, v0, v1, kind, s_ranges, prof in (
            ("PAS_2NP", lv["red1_bot"], lv["red1_top"], "band", [(s_left_cw, pl[0]), (pr[1], L)], "prof.pas_cerveny"),
            ("PAS_1NP", lv["red2_bot"], lv["red2_top"], "band", [(s_left_cw, pl[0]), (pr[1], L)], "prof.pas_cerveny"),
            ("BOX_GF", lv["gf_box_bot"], lv["gf_box_top"], "band", [(s_left_cw, pl[0]), (pr[1], L)], "prof.box_gf")):
        for k, (a_, b_) in enumerate(s_ranges):
            lin.append({"id": f"{name}_{'L' if k == 0 else 'P'}", "kind": kind, "system": "lin." + prof.split(".")[1], "axis": "h",
                        "s": rng(a_, b_, "meranie", "S1", "od konca LOP po pilier / od piliera po okraj fotky"),
                        "v": val(v0, "meranie", "S1", "spodná hrana pásu (skrinka žalúzie)"),
                        "w": val(-0.20 if prof != "prof.box_gf" else round(w_gf_bl - 0.11, 3), "odhad", None,
                                 "pás = kryt skrinky žalúzie od líca LOP po čelo (ODHAD)"),
                        "note": "červený pás pod stropom" if prof != "prof.box_gf" else "sivá skrinka žalúzií prízemia"})
    # roof edge (tapered overhang) - profiled beam along the facade
    lin.append({"id": "STRECHA", "kind": "eaves", "system": "lin.strecha", "axis": "h", "s": rng(0.0, L, "pravidlo", "S1",
                "od okraja fotky po okraj fotky (strecha pokračuje na oboch stranách)"),
                "v": val(soffit_v, "meranie", "S1", "podhľad pri fasáde = vrch červeného pásu 2.NP"),
                "w": val(-0.25, "odhad", None, "zadná hrana profilu za lícom LOP (strešná doska pokračuje dovnútra, nevidno)"),
                "note": "presah strechy: klin - podhľad stúpa k čelu, čelo s oplechovaním"})
    # consoles under the walkways (one per railing post)
    for wk, posts_key, top_v in (("H", "upper", lv["walk_upper"]), ("D", "lower", lv["walk_lower"])):
        thick = (rec["walk_plate_bottom_px"] - X["rec"]["camera"]["rail"]["upper"]["edge_px"]) / m_r
        for k, p in enumerate(X["posts"][posts_key]):
            if any(a - 25 <= p <= b + 25 for a, b in SKIP_X) or p < 20:
                continue
            sc = S(p, w_r)
            if not (0.1 < sc < L - 0.1):
                continue
            hidden = p >= READ["tree_x"]
            lin.append({"id": f"KONZ_{wk}_{k:02d}", "kind": "bracket", "system": "lin.konzola", "axis": "w",
                        "s": rng(sc - 0.04, sc + 0.04, "meranie" if not hidden else "pravidlo", "S1",
                                 "os stĺpika zábradlia na hrane ochodze (kamera fotky)" if not hidden else "za stromom (detekcia stĺpika cez listy)"),
                        "w_span": rng(0.0, round(w_r, 3), "meranie", "S1", "od čela stropu po hranu ochodze (pomer polôh päty a hrotu konzoly)"),
                        "v": val(round(top_v - thick - 0.22, 3), "odhad", None, "spodok konzoly: vrch = spodok roštu, výška 0,22 m = priemer zúženia 0,11-0,33 (ODHAD)"),
                        "note": "oceľová konzola ochodze"})
    # round column under the overhang (left)
    d = round(col["d"], 3)
    s_c0 = S(col["px"][0], col["w_axis"])
    lin.append({"id": "STLP_L", "kind": "column", "system": "lin.stlp", "axis": "v",
                "s": rng(s_c0, s_c0 + d, "meranie", "S1", "okraje stĺpa (kamera fotky v hĺbke stĺpa)"),
                "v": rng(0.0, soffit_v, "pravidlo", "S1", "od terénu po podhľad"),
                "w": val(round(col["w_axis"] - d / 2 - d / 2, 3), "odhad", None,
                         "hĺbka z výšky hlavy stĺpa: podhľad za fasádou vo výške pri fasáde (ODHAD, ~1,9 m za čelom)"),
                "note": "betónový okrúhly stĺp pod presahom vľavo"})

    # ---- walls: piers and the lintels of the recessed bay -----------------------------------------------------------
    for zid, (a_, b_) in (("PILIER_L", pl), ("PILIER_P", pr)):
        zones.append({"id": zid, "kind": "wall", "rect": {"v": round_list([a_, b_, 0.0, soffit_v]), "src": "S1", "conf": "meranie",
                      "note": "čelo piliera (okraje na fotke); po podhľad"},
                      "thickness": val(round(-w_bay + 0.25, 3), "odhad", None, "hĺbka ústupu dverí (meranie) + nadpražie / stena 0,25 m (ODHAD)"),
                      "system": "wall.betón", "role": "main"})
    bay_s0, bay_s1 = pl[1], pr[0]
    lintel = {k: V(v, w_bay) for k, v in bay["lintel_bot_px"].items()}
    tops = {"GF": floor_v["1NP"], "1NP": floor_v["2NP"], "2NP": soffit_v}
    for fl in ("GF", "1NP", "2NP"):
        if tops[fl] - lintel[fl] < 0.10:                  # 2.NP: the bay reaches the soffit, no lintel
            lintel[fl] = tops[fl]
            continue
        zones.append({"id": f"PREKLAD_{fl}", "kind": "wall", "rect": {"v": round_list([bay_s0, bay_s1, lintel[fl], tops[fl]]), "src": "S1",
                      "conf": "meranie", "note": "nadpražie / čelo podesty v rovine dverí ústupu"},
                      "thickness": val(0.25, "odhad", None, "hrúbka (ODHAD)"), "system": "wall.betón",
                      "w": val(round(w_bay, 3), "meranie", "S1", "rovina dverí ústupu: bočná stena piliera P na fotke (pomer polôh)"),
                      "role": "main"})
    leaf_v = round(V(bay["leaf_top_px"], w_bay), 3)        # door leaf height above the floor of the bay
    for fl in ("GF", "1NP", "2NP"):
        v0, v1 = floor_v[fl], lintel[fl]
        spec_cw.append({"id": f"CW_VSTUP_{fl}", "rect": {"v": round_list([bay_s0, bay_s1, v0, v1]), "src": "S1", "conf": "meranie",
                        "note": "dvere v ústupe medzi piliermi"},
                        "system": "cw.hof_b", "reveal": val(round(-w_bay, 3), "meranie", "S1", "rovina dverí ústupu (pomer polôh bočnej steny piliera)"),
                        "v_axes": vlist([round(v0 + min(leaf_v, v1 - v0 - 0.2), 3)], "meranie" if fl == "GF" else "pravidlo", "S1",
                                        "priečka nad krídlom (prízemie: meranie; poschodia: rovnaká výška krídla)"),
                        "cells": [{"col": 0, "row": 0, "panel": "door"}], "story": fl, "note": "vstup do schodiska / ústup"})

    # ---- walkways, slab edges, railings --------------------------------------------------------------------------------
    s_walk0 = S(rec["walk_corner_px"], w_r)
    thick = round((rec["walk_plate_bottom_px"] - X["rec"]["camera"]["rail"]["upper"]["edge_px"]) / m_r, 3)
    for wk, fl, top in (("OCH_1NP", "1NP", floor_v["1NP"]), ("OCH_2NP", "2NP", floor_v["2NP"])):
        slabs.append({"id": wk, "kind": "walkway", "system": "slab.ochodza", "story": fl,
                      "outline_sw": {"v": [[round(s_walk0, 3), 0.0], [L, 0.0], [L, round(w_r, 3)], [round(s_walk0, 3), round(w_r, 3)]],
                                     "src": "S1", "conf": "meranie", "note": "ľavý koniec = rohový stĺpik; hĺbka = poloha stĺpikov voči päte konzol; vpravo okraj fotky"},
                      "v_top": val(top, "meranie", "S1", "vrch čelného plechu ochodze (= úroveň podlahy podlažia, pravidlo)"),
                      "note": "pochôdzny rošt na konzolách"})
        railings.append({"id": f"ZAB_{fl}", "system": "rail.ochodza", "on": wk, "story": fl,
                         "path_sw": {"v": [[round(s_walk0 + 0.04, 3), 0.05], [round(s_walk0 + 0.04, 3), round(w_r - 0.04, 3)],
                                           [L, round(w_r - 0.04, 3)]], "src": "S1", "conf": "meranie",
                                     "note": "po hrane ochodze 0,04 m od okraja; ľavý koniec s bočným zábradlím k fasáde"},
                         "v": val(top, "meranie", "S1", "na rošte"), "inset": 0.04, "note": "zábradlie ochodze"})
    for sid, fl, top, bottom in (("STROP_1NP", "1NP", floor_v["1NP"], lv["gf_box_top"]), ("STROP_2NP", "2NP", floor_v["2NP"], lv["red2_top"])):
        slabs.append({"id": sid, "kind": "floor_edge", "system": "slab.strop", "story": fl,
                      "outline_sw": {"v": [[round(s_left_cw, 3), -1.5], [L, -1.5], [L, 0.0], [round(s_left_cw, 3), 0.0]], "src": "S1",
                                     "conf": "odhad", "note": "čelo stropu: pás 1,5 m od čela (doska pokračuje do budovy, nevidno)"},
                      "v_top": val(top, "meranie", "S1", "úroveň podlahy = vrch ochodze"),
                      "note": f"stropná doska nad {'prízemím' if fl == '1NP' else '1.NP'} (hrúbka po vrch skrinky žalúzie: {top - bottom:.3f} m)"})

    # ---- systems ---------------------------------------------------------------------------------------------------------
    systems = {
        "cw.hof_b": {"type": "curtain_wall", "favorite": "FS cw.hof_b", "conf": "odhad", "src": "S1",
                     "note": "stĺpiková-priečková LOP, tmavosivé rámy (fotka); profily ODHAD",
                     "frame": {"width": val(FRAME_W, "meranie", "S1", "tmavá lišta stĺpika 12-13 px pri 210 px/m"),
                               "depth": val(0.15, "odhad", None, "hĺbka profilu (na čelnej fotke nevidno)"),
                               "offset": val(0.05, "odhad", None, "časť pred rovinou skla"), "material": "Hliník", "surface": "Farba 25 - antracit"},
                     "boundary": {"width": val(FRAME_W / 2, "pravidlo", "S1", "polovica stĺpika: dve susedné LOP spolu = jeden stĺpik"),
                                  "depth": val(0.15, "odhad", None, ""), "offset": val(0.05, "odhad", None, ""), "material": "Hliník",
                                  "surface": "Farba 25 - antracit"},
                     "panels": {"glass": {"thickness": val(0.03, "odhad", None, "izolačné trojsklo (ODHAD)"), "material": "Sklo", "surface": "Sklo - modré"},
                                "opaque": {"thickness": val(0.03, "odhad", None, "parapetný panel s perforovaným plechom (fotka), hrúbka ODHAD"),
                                           "material": "Pozinkovaný plech", "surface": "Kov - perforovaný malé kruhy"},
                                "door": {"libpart": "Dvoukřídlé dveře LOP 26", "note": "francúzske (dvojkrídlové) dvere na ochodzu; knižnica SK šablóny (CZ 26) nemá CW Door 27"}}},
        "slab.ochodza": {"type": "slab", "favorite": "FS slab.ochodza", "structure": "Basic", "material": "Pozinkovaný plech",
                         "thickness": val(thick, "meranie", "S1", "výška čelného plechu ochodze na fotke"), "conf": "meranie", "src": "S1",
                         "note": "pochôdzny rošt (materiál: pozinkovaná oceľ, plná doska namiesto roštu - zjednodušenie)"},
        "slab.strop": {"type": "slab", "favorite": "FS slab.strop", "structure": "Basic", "material": "Betón vystužený",
                       "thickness": val(round(float(np.mean([floor_v['2NP'] - lv['red2_top'], floor_v['1NP'] - lv['gf_box_top']])), 3), "meranie", "S1",
                                        "od vrchu skrinky žalúzie po úroveň ochodze (priemer 2 podlaží)"),
                       "conf": "odhad", "src": "S1", "note": "železobetónová stropná doska (materiál ODHAD)"},
        "rail.ochodza": {"type": "railing", "base_favorite": "Oceľové vertikálne zábradlie", "favorite": "FS rail.ochodza",
                         "height": val(RAIL_H, "odhad", None, "MIERKA fotky: výška zábradlia ochodze 1,05 m (ODHAD +-10 %, zadanie)"),
                         "post_spacing": val(round(rec["post_spacing_px"] / m_r, 3), "meranie", "S1",
                                             f"rozostup stĺpikov {rec['post_spacing_px']:.0f} px v rovine zábradlia (medián)"),
                         "post_distribution": "BestDivision",
                         "infill": val("rails", "meranie", "S1", "vodorovné tyče: 3 hore po ~0,14 m, 7 dole po ~0,07 m (fotka)"),
                         "conf": "odhad", "src": "S1", "note": "oceľové zábradlie s vodorovnými tyčami"},
        "wall.betón": {"type": "wall", "favorite": "Betónová tvárnica - nosná, 200", "structure": "Basic", "reference_line": "Outside",
                       "conf": "default", "src": None, "note": "pilier / nadpražie: existujúci favorit (betón); hrúbka zo zóny"},
        "prof.pas_cerveny": {"type": "profile", "name": "FS prof.pas_cerveny", "polygon_wv": [[0, 0], [0.2, 0], [0.2, round(lv['red1_top'] - lv['red1_bot'], 3)], [0, round(lv['red1_top'] - lv['red1_bot'], 3)]],
                             "material": "Hliník", "edge_surface": "Farba 14 - karmínová červeň",
                             "w": val(0.2, "odhad", None, "hĺbka skrinky (ODHAD)"),
                             "h": val(round(lv['red1_top'] - lv['red1_bot'], 3), "meranie", "S1", "výška červeného pásu"),
                             "conf": "meranie", "src": "S1", "note": "kryt skrinky raffstore (červený)"},
        "prof.box_gf": {"type": "profile", "name": "FS prof.box_gf", "polygon_wv": [[0, 0], [0.11 + 0.11, 0], [0.22, round(lv['gf_box_top'] - lv['gf_box_bot'], 3)], [0, round(lv['gf_box_top'] - lv['gf_box_bot'], 3)]],
                        "material": "Hliník", "edge_surface": "Farba 24 - tmavosivá",
                        "w": val(0.22, "odhad", None, ""), "h": val(round(lv['gf_box_top'] - lv['gf_box_bot'], 3), "meranie", "S1", "výška sivej skrinky"),
                        "conf": "meranie", "src": "S1", "note": "skrinka žalúzií prízemia (sivá, perforovaná)"},
        "prof.konzola": {"type": "profile", "name": "FS prof.konzola", "polygon_wv": [[0, 0], [0.08, 0], [0.08, 0.22], [0, 0.22]],
                         "material": "Oceľ - konštrukčná", "edge_surface": "Kov - zinok",
                         "w": val(0.08, "odhad", None, "dva plechy konzoly (ODHAD)"), "h": val(0.22, "odhad", None, "priemer zúženej výšky"),
                         "conf": "odhad", "src": None, "note": "konzola ochodze (prierez kolmo na os konzoly: x pozdĺž fasády, y hore)"},
        "prof.stlp": {"type": "profile", "name": "FS prof.stlp", "polygon_wv": [[round(d / 2 + d / 2 * math.cos(2 * math.pi * k / 24), 4),
                                                                                  round(d / 2 + d / 2 * math.sin(2 * math.pi * k / 24), 4)] for k in range(24)],
                      "material": "Betón vystužený", "edge_surface": "Betón - 04",
                      "w": val(d, "meranie", "S1", "priemer (24-uholník)"), "h": val(d, "meranie", "S1", ""),
                      "conf": "odhad", "src": "S1", "note": "okrúhly stĺp (24-uholník)"},
        "prof.strecha": {"type": "profile", "name": "FS prof.strecha",
                         "polygon_wv": [[0, 0], [0.25, 0], [round(0.25 + w_f, 3), round(lv['fascia_bot'] - soffit_v, 3)],
                                        [round(0.25 + w_f, 3), round(lv['fascia_top'] - soffit_v, 3)], [0, round(lv['fascia_top'] - soffit_v, 3)]],
                         "material": "Betón vystužený", "edge_surface": "Farba 09 - piesková béžová",
                         "w": val(round(0.25 + w_f, 3), "meranie", "S1", "vyloženie presahu z pomeru polôh škár podhľadu (+0,25 za lícom)"),
                         "h": val(round(lv['fascia_top'] - soffit_v, 3), "meranie", "S1", "vrch čela nad podhľadom pri fasáde"),
                         "conf": "meranie", "src": "S1", "note": "presah strechy: klinový prierez (podhľad stúpa k čelu)"},
        "lin.pas_cerveny": {"type": "linear", "tool": "beam", "profile": "prof.pas_cerveny", "favorite": "FS lin.pas_cerveny", "conf": "meranie", "src": "S1"},
        "lin.box_gf": {"type": "linear", "tool": "beam", "profile": "prof.box_gf", "favorite": "FS lin.box_gf", "conf": "meranie", "src": "S1"},
        "lin.konzola": {"type": "linear", "tool": "beam", "profile": "prof.konzola", "favorite": "FS lin.konzola", "conf": "odhad", "src": "S1"},
        "lin.stlp": {"type": "linear", "tool": "column", "profile": "prof.stlp", "favorite": "FS lin.stlp", "conf": "odhad", "src": "S1"},
        "lin.strecha": {"type": "linear", "tool": "beam", "profile": "prof.strecha", "favorite": "FS lin.strecha", "conf": "meranie", "src": "S1"},
        "gdl.louvers": {"type": "gdl", "template": "FS_Louvers", "classification": "Tieniaci prvok", "conf": "odhad", "src": "S1",
                        "note": "vonkajšie lamelové žalúzie (raffstore) pred každým poľom, výška spustenia z fotky (šablóna I4)"},
    }

    # ---- simplifications and G1 -----------------------------------------------------------------------------------------
    simpl += [
        {"ref": "cw_fields", "text": "LOP nie je jeden prvok na podlažie: dvere sú vlastné LOP (API má jeden vzor riadkov na LOP; dvere idú po hornú priečku, pevné polia majú parapetný panel); susedné LOP sa dotýkajú v osi stĺpika (obvodové rámy po 0,03 = stĺpik 0,06)",
         "proposal": "prijať (alternatíva: LOP s rušenými rámami - API nevie)"},
        {"ref": "cw_fields.j", "text": "stĺpik pri dverách, ktorý na fotke končí v priečke nad dverami (1.NP 3413, 5197 px; 2.NP 4450 px), je v modeli po celú výšku (dverná LOP je samostatná)",
         "proposal": "prijať"},
        {"ref": "cw_fields[GF]", "text": "prízemie: rovina skla nie je viditeľná (žalúzie spustené); lamely 0,27 m pred stĺpikmi poschodí (meranie pomerom), sklo 0,14 m za nimi (ODHAD); dvere v prízemí nie sú viditeľné - všetky polia pevné",
         "proposal": "overiť na mieste / iná fotka"},
        {"ref": "linear[KONZ_*]", "text": "konzola je zúžený plech (0,11 m pri hrote, 0,33 m pri päte); model = vodorovný nosník 0,08 x 0,22 m",
         "proposal": "prijať (alebo GDL konzola)"},
        {"ref": "linear[STRECHA]", "text": "presah strechy = nosník s klinovým profilom pozdĺž fasády (doska s konštantnou hrúbkou by na čele nesedela o 0,22 m); strešná doska za fasádou nemodelovaná (nevidno)",
         "proposal": "prijať; strešnú dosku doplniť z pôdorysu"},
        {"ref": "slabs[OCH_*]", "text": "pochôdzny rošt ako plná doska z pozinkovaného plechu hrúbky čelného plechu", "proposal": "prijať"},
        {"ref": "slabs[STROP_*]", "text": "stropné dosky len ako pás 1,5 m za čelom (fasádny model)", "proposal": "prijať"},
        {"ref": "railings", "text": "VÝNIMKA R1: favorit s vodorovnými tyčami 'Oceľové horizontálne zábradlie - bočné kotvenie' cez API vytvorí len 2 stĺpiky bez madla a tyčí (výška 1,0 sa nedá zmeniť; overené 2026-10-19) - použitý najbližší funkčný 'Oceľové vertikálne zábradlie' (zvislé tyčky); fotka: madlo + 3 vodorovné tyče po 0,14 m + 7 po 0,07 m",
         "proposal": "R1: architekt pripraví favorit 'FS base zábradlie vodorovné' s 10 tyčami; potom prestavať"},
        {"ref": "railings.posts", "text": "stĺpiky z rozostupu (BestDivision); na fotke pri pilieroch nepravidelne (339 / 249 / 392 px)", "proposal": "prijať"},
        {"ref": "fotka.vpravo", "text": "pravých ~13 % (x > 8350 px) v 1.-2.NP zakrýva strom: stĺpiky z prízemia (pravidlo), výška žalúzií ako v podlaží; fasáda pokračuje za okraj fotky (model končí na okraji)",
         "proposal": "doplniť z ďalšej fotky"},
        {"ref": "odkvapové zvody", "text": "2 zvislé rúry (x 272 a 6712 px, s lievikom pri podhľade) - zvody, nie stĺpy; nemodelované (TZB)", "proposal": "prijať"},
        {"ref": "vľavo", "text": "biela plocha vľavo od LOP (pod presahom) nejednoznačná (stena v hĺbke / obloha) - nemodelovaná; ochodze sa na ľavom konci vracajú bočným zábradlím",
         "proposal": "overiť"},
        {"ref": "pásy", "text": "červené pásy a sivá skrinka sú nosníky s profilom; povrch triedy rámu LOP sa nezachová (nález I5) - rámy majú farbu materiálu", "proposal": "prijať"},
    ]
    props = {
        "systems[rail.ochodza].height": f"MIERKA: 1,05 m (ODHAD +-10 %); kontrola: krídlo dverí ústupu {leaf_v:.2f} m (zadanie 2,10 -> +{(leaf_v / 2.10 - 1) * 100:.0f} %; rad DIN 18101 2,26 / 2,385 m), rozostup konzol {rec['post_spacing_px'] / m_r:.3f} m (modul 1,25), konštr. výška podlažia {floor_v['2NP'] - floor_v['1NP']:.2f} m",
        "cw_fields.reveal": "prijať 0,20 m (raffstore) / overiť rezom",
    }
    spec = {
        "schema": "facade-spec/1", "id": SPEC_ID,
        "title": "HAW Hof, budova B - južná fasáda (západná časť), pilot B z jednej fotky",
        "generated": {"adapter": "facade/adapters/from_photo.py", "at": dt.datetime.now().isoformat(timespec="seconds"),
                      "photo": rec["ingest"]["file"], "photo_sha1": rec["ingest"]["sha1"], "attribution": ATTRIBUTION},
        "reference": {"a": list(ORIGIN_XY), "b": [ORIGIN_XY[0] + L, ORIGIN_XY[1]], "src": "S1", "conf": "meranie",
                      "z0": val(0.0, "meranie", "S1", "v = 0 = päta LOP prízemia (spodná hrana nad trávou); z modelu = v"),
                      "outside": "left_of_ab", "layer": f"FASADA – {SPEC_ID}",
                      "home_story": {"floorIndex": 0, "src": "S1", "conf": "pravidlo",
                                     "note": "podlažia projektu nastavené podľa fotky (stories); každý prvok dostane podlažie podľa svojej päty"}},
        "sources": [
            {"id": "S1", "type": "photo", "file": rec["ingest"]["file"], "sha1": rec["ingest"]["sha1"], "attribution": ATTRIBUTION,
             "camera": {**cam.as_dict(), "src": "S1", "conf": "odhad",
                        "note": "pinhole čelného záberu na rektifikovanej fotke: x = cx + f (s - s_c)/(D0 - w), y = cy - f (v - v_c)/(D0 - w)"},
             "warp": {k: rec["rectify"][k] for k in ("coef", "cx")}, "rect_image": str(ROOT / "work" / "HOF_B_rect.jpg"),
             "scale": {"reference": "výška zábradlia ochodze 1,05 m (ODHAD +-10 %)", "m0_px_per_m": rec["camera"]["m0_px_per_m"],
                       "k_r": rec["camera"]["k_r"], "w_r": w_r, "w_f": w_f, "D0": D0, "checks": rec.get("checks", {})}},
            {"id": "S2", "type": "rule", "file": "facade/adapters/from_photo.py", "note": "pravidlá: raster pokračuje pod stromom, zvislice LOP nad sebou"},
            {"id": "S3", "type": "drawing", "file": str(ROOT / "zdroj.md"), "note": "popis fotky, mierka, zakryté časti"}],
        "stories": [{"id": "GF", "level": val(0.0, "meranie", "S1", "päta LOP"), "floorIndex": 0, "name": "Prízemie"},
                    {"id": "1NP", "level": val(floor_v["1NP"], "meranie", "S1", "vrch ochodze 1.NP"), "floorIndex": 1, "name": "1.NP"},
                    {"id": "2NP", "level": val(floor_v["2NP"], "meranie", "S1", "vrch ochodze 2.NP"), "floorIndex": 2, "name": "2.NP"},
                    {"id": "STRECHA", "level": val(soffit_v, "meranie", "S1", "podhľad presahu pri fasáde"), "floorIndex": 3, "name": "Strecha"}],
        "zones": zones, "openings": [], "cw_fields": spec_cw, "linear": lin, "objects": objects, "slabs": slabs, "railings": railings,
        "systems": systems,
        "tolerances": {"build": 0.01, "kota": 0.01, "meranie": 0.03, "pravidlo": 0.10, "odhad": None},
        "g1_proposals": props, "simplifications": simpl,
    }
    return spec


# ============================================================================== readings image
def _font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_readings(X: dict, spec: dict, path: Path, scale: float = 0.25) -> None:
    rgb = cv2.cvtColor(X["rect"], cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb).resize((int(X["W"] * scale), int(X["H"] * scale)), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    cam = X["cam"]
    P = lambda s, v, w: (float(cam.x(s, w)) * scale, float(cam.y(v, w)) * scale)
    for cw in spec["cw_fields"]:
        s0, s1, v0, v1 = cw["rect"]["v"]
        w = -cw["reveal"]["v"]
        col = (0, 255, 255) if not any(c["panel"] == "door" for c in cw.get("cells", [])) else (255, 140, 0)
        d.rectangle([P(s0, v1, w), P(s1, v0, w)], outline=col, width=2)
        for s in cw.get("u_axes", {}).get("v", []):
            d.line([P(s, v0, w), P(s, v1, w)], fill=col, width=1)
        for v in cw.get("v_axes", {}).get("v", []):
            d.line([P(s0, v, w), P(s1, v, w)], fill=col, width=1)
    for it in spec["linear"]:
        if it["axis"] == "h":
            s0, s1 = it["s"]["v"]
            prof = spec["systems"][spec["systems"][it["system"]]["profile"]]
            h = max(p[1] for p in prof["polygon_wv"])
            wf = it["w"]["v"] + max(p[0] for p in prof["polygon_wv"])
            d.rectangle([P(s0, it["v"]["v"] + h, wf), P(s1, it["v"]["v"], wf)], outline=(255, 0, 0), width=2)
        elif it["axis"] == "w":
            s = 0.5 * sum(it["s"]["v"])
            d.line([P(s, it["v"]["v"], it["w_span"]["v"][0]), P(s, it["v"]["v"], it["w_span"]["v"][1])], fill=(255, 0, 255), width=2)
    for sl in spec["slabs"]:
        if sl["kind"] != "walkway":
            continue
        ws = sl["outline_sw"]["v"]
        s0, s1, w1 = ws[0][0], ws[1][0], ws[2][1]
        d.line([P(s0, sl["v_top"]["v"], w1), P(s1, sl["v_top"]["v"], w1)], fill=(0, 255, 0), width=2)
        d.line([P(s0, sl["v_top"]["v"] + 1.05, w1), P(s1, sl["v_top"]["v"] + 1.05, w1)], fill=(0, 255, 0), width=1)
    for ob in spec["objects"]:
        s, v, w = ob["origin"]["v"]
        a = ob["params"]["A"]["v"]
        d.line([P(s, v, w), P(s + a, v, w)], fill=(255, 255, 0), width=1)
    font = _font(14)
    d.rectangle([0, im.height - 22, im.width, im.height], fill=(0, 0, 0))
    d.text((6, im.height - 19), ATTRIBUTION + " | odčítanie: azúrová LOP, oranžová dvere, červená pásy, zelená ochodza+zábradlie, fialová konzoly, žltá spodok žalúzií",
           fill=(255, 255, 255), font=font)
    im.save(path)


def apply_corrections(spec: dict) -> None:
    """I7: corrections of the iteration loop (facade/corrections/HOF_B.json, target 'spec'); the module is loaded by its
    path (this adapter runs isolated, the facade folder is not on sys.path)."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "spec_corrections.py"
    mod_spec = importlib.util.spec_from_file_location("spec_corrections", path)
    sc = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(sc)
    corr = sc.load(sc.path_of(SPEC_ID), SPEC_ID)
    applied, stale = sc.apply(spec, corr, "spec")
    sc.finish_spec(spec, corr, applied, stale)
    if applied or stale:
        print(f"I7 corrections: {len(applied)} applied, stale {stale}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--photo", type=Path, default=None)
    ap.add_argument("--probe", action="store_true", help="print the measurements only")
    args = ap.parse_args(argv)
    photo = args.photo or args.root / "input" / PHOTO_NAME
    X = extract(args.root, photo)
    if args.probe:
        print(json.dumps({k: v for k, v in X["rec"].items() if k not in ("console_pairs", "readings_by_eye")}, indent=1, default=float))
        return 0
    spec = assemble(X)
    apply_corrections(spec)
    out, work = args.root / "out", args.root / "work"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    rect_path = work / "HOF_B_rect.jpg"
    cv2.imwrite(str(rect_path), X["rect"], [cv2.IMWRITE_JPEG_QUALITY, 92])
    small = cv2.resize(X["rect"], (1800, int(X["H"] * 1800 / X["W"])), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(work / "HOF_B_rect_1800.jpg"), small, [cv2.IMWRITE_JPEG_QUALITY, 90])
    (work / "HOF_B_measure.json").write_text(json.dumps(X["rec"], indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    draw_readings(X, spec, work / "HOF_B_readings.png")
    sp = out / f"{SPEC_ID}.facade-spec.json"
    sp.write_text(json.dumps(spec, indent=1, ensure_ascii=False), encoding="utf-8")
    n = {k: len(spec[k]) for k in ("cw_fields", "linear", "objects", "slabs", "railings", "zones")}
    print(f"spec {sp}: {n}, L = {spec['reference']['b'][0]} m, {(dt.datetime.now() - X['t0']).total_seconds():.1f} s")
    print(f"rectified: {rect_path}; readings: {work / 'HOF_B_readings.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

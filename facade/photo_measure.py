"""photo_measure: re-measure an edge or a frame bar on the rectified photo when the model and the photo disagree
(ADR-001 I7, the 'spec' branch of the iteration loop).

verify.py looks for an edge only within +-5 cm of the model edge (a hit at the border of that window is a lower bound,
not a measurement). Here the edge is searched again at the full resolution of the rectified photo in a WIDE window and
judged before anything in the spec is changed:

  candidates     every local maximum of the brightness step across the edge (profile averaged along the middle 70 %
                 of the edge; sign = the brighter side when the spec names it, else either sign), sub-pixel position
  wall test      the edge of a masonry opening is the step from the wall face to something that is not wall: the strip
                 0.03-0.10 m outside the candidate has the colour of the wall beside the opening (Lab median of strips at
                 the same distance left / right of it, or above / below), the strip inside does not. A foreign object
                 outside the edge (shop sign, awning) fails the test -> the edge is hidden
  certainty      exactly one candidate passes, its step >= MIN_STEP, the three thirds of the edge give the same
                 position within CONSIST_M, the candidate is not at the border of the window; otherwise ambiguous with
                 the reason (none / two edges / not consistent / at the border)
  rigid shift    an opening whose two opposite edges are off in the same direction is searched as a pair (both
                 steps at the spec width apart): score(d) = sum of the signed steps at edge + d; unique best d
  bars           a frame bar (mullion) is the darkest narrow valley: measured in every band of the floor separately;
                 two valleys of similar depth within a few cm in a band = double bar

Pure numpy / PIL; positions in the rectified photo plane (m), the caller converts to spec values through the camera.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MIN_STEP = 12.0          # minimum brightness step (0..255) of a readable edge (verify: AUTO_CONTRAST)
CONSIST_M = 0.015        # m - the thirds of an edge agree within this
STRIP = (0.03, 0.10)     # m - colour strip inside a candidate edge
STRIP_OUT = (0.03, 0.20) # m - colour strip outside a candidate edge (the wall face must continue that far)
WALL_DE = 15.0           # Lab distance: a strip is wall when it is within this of a reference strip of the wall
SECOND_RATIO = 0.6       # a second passing candidate stronger than this x the best = two edges (ambiguous)
BORDER_M = 0.01          # m - candidate closer than this to the window border = not found
CONTINUE_RATIO = 0.5     # the step beyond the jambs at >= this x the step on the edge = the edge runs on (wider object)
UNCLEAR_RATIO = 0.3      # between this and CONTINUE_RATIO: unclear (a stone joint in line with a lintel, or a wider object)


# ---- images ---------------------------------------------------------------------------------------------------------
class Photo:
    """Rectified photo of the reference plane: pixel <-> (s', v') of the photo plane (m)."""

    def __init__(self, path: Path, S: float, u0: float, v1: float):
        img = Image.open(path).convert("RGB")
        self.path = Path(path)
        self.rgb = np.asarray(img, dtype=np.float32)
        self.lum = self.rgb.mean(axis=2)
        self.lab = _lab(self.rgb)
        self.S, self.u0, self.v1 = float(S), float(u0), float(v1)
        self.h, self.w = self.lum.shape

    def x(self, s: float) -> float:
        return (s - self.u0) * self.S

    def y(self, v: float) -> float:
        return (self.v1 - v) * self.S

    def s(self, x: float) -> float:
        return x / self.S + self.u0

    def v(self, y: float) -> float:
        return self.v1 - y / self.S


def _lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]], np.float32)
    xyz = c @ m.T / np.array([0.9505, 1.0, 1.089], np.float32)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


# ---- profiles across an edge ----------------------------------------------------------------------------------------
def _box(ph: Photo, edge: str, a: float, b: float, r0: float, r1: float) -> tuple[int, int, int, int]:
    """Pixel box (x0, x1, y0, y1): across = a..b (s for vertical edges, v for horizontal), along = r0..r1."""
    if edge in ("left", "right"):
        x0, x1 = int(math.floor(ph.x(a))), int(math.ceil(ph.x(b)))
        y0, y1 = int(round(ph.y(r1))), int(round(ph.y(r0)))
    else:
        y0, y1 = int(math.floor(ph.y(b))), int(math.ceil(ph.y(a)))
        x0, x1 = int(round(ph.x(r0))), int(round(ph.x(r1)))
    return max(0, x0), min(ph.w, x1), max(0, y0), min(ph.h, y1)


def step_profile(ph: Photo, edge: str, at: float, rng, half: float) -> tuple[np.ndarray, np.ndarray]:
    """Positions (m, photo plane, at the pixel boundaries) and signed brightness steps across the edge within
    at +- half: step > 0 when the left / upper side is brighter. Mean along the middle of rng."""
    r0, r1 = min(rng), max(rng)
    x0, x1, y0, y1 = _box(ph, edge, at - half, at + half, r0, r1)
    if edge in ("left", "right"):
        prof = ph.lum[y0:y1, x0:x1].mean(axis=0)
        st = prof[:-1] - prof[1:]
        pos = np.array([ph.s(x0 + i + 1.0) for i in range(len(st))])
    else:
        prof = ph.lum[y0:y1, x0:x1].mean(axis=1)              # top -> bottom
        st = prof[:-1] - prof[1:]                               # above minus below
        pos = np.array([ph.v(y0 + i + 1.0) for i in range(len(st))])
    if len(st) >= 3:                                            # light smoothing [1 2 1] - a step blurred over 2 px
        st = np.convolve(np.pad(st, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    return pos, st


def _peaks(pos: np.ndarray, st: np.ndarray, sign: int | None) -> list[tuple[float, float]]:
    """Local maxima of the (signed) step: (position with sub-pixel parabola, strength)."""
    a = st * sign if sign else np.abs(st)
    out = []
    for i in range(1, len(a) - 1):
        if a[i] >= a[i - 1] and a[i] > a[i + 1] and a[i] > 0:
            den = a[i - 1] - 2 * a[i] + a[i + 1]
            off = 0.5 * (a[i - 1] - a[i + 1]) / den if abs(den) > 1e-9 else 0.0
            p = pos[i] + off * (pos[i + 1] - pos[i])
            out.append((float(p), float(a[i])))
    return out


def edge_sign(edge: str, brighter: str | None) -> int | None:
    """+1: the left / upper side is brighter, -1: right / lower, None: unknown."""
    if brighter is None:
        return None
    return +1 if brighter in ("left", "above") else -1


def outward(edge: str) -> int:
    """Direction (in s or v) from the opening interior to the wall outside the edge."""
    return {"left": -1, "right": +1, "top": +1, "bottom": -1}[edge]


# ---- does the edge belong to the opening? -----------------------------------------------------------------------------
def continuation(ph: Photo, edge: str, pos: float, rect, rng, free: tuple[bool, bool], sign: int | None,
                 inside_step: float) -> dict:
    """Strength of the same step just BEYOND the ends of the opening edge (top / bottom edge: left and right of the
    opening; left / right edge: above and below it), on the free sides only (a touching neighbour continues the edge
    by construction). An edge of a masonry opening ends at the jambs; an edge that runs on beyond the opening belongs
    to something wider (awning, shop sign, sill, cornice, pole) - the opening edge behind it is hidden."""
    s0, s1, v0, v1 = rect
    ends = [(s0 - 0.25, s0 - 0.05), (s1 + 0.05, s1 + 0.25)] if edge in ("top", "bottom") else [(v0 - 0.25, v0 - 0.05), (v1 + 0.05, v1 + 0.25)]
    out = []
    for (a, b), ok in zip(ends, free):
        if not ok:
            out.append(None)
            continue
        p_, st = step_profile(ph, edge, pos, (a, b), 0.015)
        if not len(st):
            out.append(None)
            continue
        val = float((st * sign).max()) if sign else float(np.abs(st).max())
        out.append(round(val, 1))
    vals = [x for x in out if x is not None]
    ratio = round(max(vals) / inside_step, 2) if vals and inside_step > 0 else 0.0
    return {"beyond": out, "ratio": ratio, "continues": ratio >= CONTINUE_RATIO, "unclear": UNCLEAR_RATIO <= ratio < CONTINUE_RATIO}


# ---- one edge ---------------------------------------------------------------------------------------------------------
def remeasure_edge(ph: Photo, edge: str, at: float, rng, brighter: str | None, half: float = 0.10,
                   rect=None, free: tuple[bool, bool] = (True, True)) -> dict:
    """Edge near `at` (photo plane, m) in a window +-half. rect = the opening in the photo plane (for the continuation
    test). Returns {status: certain | ambiguous, pos, step, reason, candidates, thirds, continuation}."""
    sign = edge_sign(edge, brighter)
    pos, st = step_profile(ph, edge, at, rng, half)
    if len(st) < 5:
        return {"status": "ambiguous", "reason": "okno mimo fotky", "pos": None, "step": 0.0, "candidates": []}
    lo, hi = at - half, at + half
    judged = [{"pos": round(p, 4), "step": round(s, 1), "border": p - lo < BORDER_M or hi - p < BORDER_M}
              for p, s in sorted(_peaks(pos, st, sign), key=lambda c: -c[1]) if s >= MIN_STEP]
    res = {"candidates": judged[:6], "window": [round(lo, 4), round(hi, 4)], "sign": sign}
    ok = [c for c in judged if not c["border"]]
    if not judged:
        return dict(res, status="ambiguous", reason=f"žiadny skok jasu >= {MIN_STEP:.0f} v okne ±{half * 100:.0f} cm (nečitateľná)", pos=None, step=0.0)
    if not ok:
        return dict(res, status="ambiguous", reason="najsilnejšia hrana pri okraji okna", pos=judged[0]["pos"], step=judged[0]["step"])
    best = ok[0]
    res.update(pos=best["pos"], step=best["step"])
    rivals = [c for c in ok[1:] if abs(c["pos"] - best["pos"]) > 0.02 and c["step"] >= SECOND_RATIO * best["step"]]
    if rivals:
        return dict(res, status="ambiguous", reason=f"dve hrany: {best['pos']:.3f} (skok {best['step']:.0f}) a {rivals[0]['pos']:.3f} (skok {rivals[0]['step']:.0f})")
    thirds = edge_thirds(ph, edge, best["pos"], rng, sign)
    res["thirds"] = thirds
    if len(thirds) < 3 or max(thirds) - min(thirds) > CONSIST_M:
        return dict(res, status="ambiguous", reason=f"tretiny hrany nesúhlasia ({', '.join(f'{t:.3f}' for t in thirds)})")
    if rect is not None:
        cont = continuation(ph, edge, best["pos"], rect, rng, free, sign, best["step"])
        res["continuation"] = cont
        if cont["continues"]:
            return dict(res, status="ambiguous", reason=f"hrana pokračuje mimo otvoru (skok {cont['beyond']} vs {best['step']:.0f}) - patrí širšiemu predmetu, hrana otvoru je zakrytá")
        if cont["unclear"]:
            return dict(res, status="certain", unclear=True,
                        reason=f"jedna silná hrana, tretiny súhlasia; za ostením slabší skok ({cont['ratio']:.2f} x) - škára v línii nadpražia alebo širší predmet")
    return dict(res, status="certain", reason="jedna silná hrana, tretiny súhlasia, končí na ostení")


def _mid(rng, frac: float = 0.7):
    r0, r1 = min(rng), max(rng)
    c, h = 0.5 * (r0 + r1), 0.5 * (r1 - r0) * frac
    return (c - h, c + h)


def edge_thirds(ph: Photo, edge: str, at: float, rng, sign: int | None, half: float = 0.03) -> list[float]:
    r0, r1 = _mid(rng, 0.8)
    out = []
    for k in range(3):
        a, b = r0 + k * (r1 - r0) / 3, r0 + (k + 1) * (r1 - r0) / 3
        pos, st = step_profile(ph, edge, at, (a, b), half)
        pk = [(p, s) for p, s in _peaks(pos, st, sign)]
        if pk:
            out.append(round(max(pk, key=lambda c: c[1])[0], 4))
    return out


# ---- rigid shift of an opening (pair of opposite edges) ---------------------------------------------------------------
def rigid_shift(ph: Photo, edges: list[dict], half: float = 0.35) -> dict:
    """edges: [{edge, at, rng, brighter}] of ONE axis (left + right or top + bottom). Shift d of the pair that makes
    both signed steps strongest at once. Returns {status, d, score, second, per_edge}."""
    grid = np.arange(-half, half + 1e-9, 1.0 / ph.S)
    total = np.zeros_like(grid)
    per = []
    for e in edges:
        sign = edge_sign(e["edge"], e.get("brighter")) or 1
        pos, st = step_profile(ph, e["edge"], e["at"], e["rng"], half + 0.02)
        s = np.interp(e["at"] + grid, pos[::-1] if pos[0] > pos[-1] else pos, (st * sign)[::-1] if pos[0] > pos[-1] else st * sign)
        per.append(s)
        total += np.clip(s, 0, None) * (1 if e.get("brighter") else 0) + (np.abs(s) if not e.get("brighter") else 0)
    i = int(np.argmax(total))
    best = float(total[i])
    # second-best local maximum separated by more than 3 cm
    far = [float(total[j]) for j in range(1, len(total) - 1)
           if total[j] >= total[j - 1] and total[j] >= total[j + 1] and abs(grid[j] - grid[i]) > 0.03]
    second = max(far) if far else 0.0
    each = [float(p[i]) for p in per]
    status = "certain" if best > 0 and second < 0.75 * best and min(each) >= MIN_STEP and abs(grid[i]) < half - BORDER_M else "ambiguous"
    reason = "jednoznačný posun dvojice hrán" if status == "certain" else \
        (f"druhý posun takmer rovnako silný ({second:.0f} / {best:.0f})" if second >= 0.75 * best else
         f"slabá hrana pri posune ({min(each):.0f})" if min(each) < MIN_STEP else "posun na okraji okna")
    return {"status": status, "d": round(float(grid[i]), 4), "score": round(best, 1), "second": round(second, 1),
            "per_edge": [round(x, 1) for x in each], "reason": reason}


# ---- crops (evidence) -------------------------------------------------------------------------------------------------
def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def crop_png(ph: Photo, box_sv, marks: list[dict], path: Path, title: str, zoom: int = 3, attribution: str | None = None,
             rects: list[dict] | None = None) -> Path:
    """Crop of the photo (s0, s1, v0, v1 in the photo plane) with lines: marks = [{kind: 'v'|'h', at, label, colour}],
    rects = [{rect: (s0, s1, v0, v1), colour}] drawn as thin outlines; title (lines split by newline) on a dark bar below."""
    s0, s1, v0, v1 = box_sv
    x0, x1 = max(0, int(ph.x(s0))), min(ph.w, int(ph.x(s1)))
    y0, y1 = max(0, int(ph.y(v1))), min(ph.h, int(ph.y(v0)))
    crop = Image.fromarray(ph.rgb[y0:y1, x0:x1].astype(np.uint8)).resize(((x1 - x0) * zoom, (y1 - y0) * zoom), Image.NEAREST)
    tl = title.split("\n")
    lines = tl + ([attribution[:150]] if attribution else [])
    bar = 16 * len(lines) + 6
    im = Image.new("RGB", (max(crop.size[0], 420), crop.size[1] + bar), (0, 0, 0))
    im.paste(crop, (0, 0))
    dr = ImageDraw.Draw(im)
    f = _font(13)
    for r in rects or []:
        rs0, rs1, rv0, rv1 = r["rect"]
        dr.rectangle([(ph.x(rs0) - x0) * zoom, (ph.y(rv1) - y0) * zoom, (ph.x(rs1) - x0) * zoom, (ph.y(rv0) - y0) * zoom],
                     outline=tuple(r.get("colour", (255, 230, 0))), width=1)
    for k, m in enumerate(marks):
        col = tuple(m.get("colour", (255, 230, 0)))
        if m["kind"] == "v":
            x = (ph.x(m["at"]) - x0) * zoom
            dr.line([(x, 0), (x, crop.size[1])], fill=col, width=2 if k < 2 else 1)
            dr.text((x + 3, 4 + 16 * k), m["label"], fill=col, font=f, stroke_width=2, stroke_fill=(0, 0, 0))
        else:
            y = (ph.y(m["at"]) - y0) * zoom
            dr.line([(0, y), (crop.size[0], y)], fill=col, width=2 if k < 2 else 1)
            xt = 4 + (k % 3) * 150
            dr.text((xt, y + 2 if k % 2 == 0 else y - 16), m["label"], fill=col, font=f, stroke_width=2, stroke_fill=(0, 0, 0))
    for i, ln in enumerate(lines):
        is_title = i < len(tl)
        dr.text((4, crop.size[1] + 3 + 16 * i), ln[:170], fill=(255, 255, 255) if is_title else (190, 190, 190), font=f if is_title else _font(11))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path

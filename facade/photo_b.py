"""photo_b: photo measurements of pilot B shared by verify_b and the I7 loop (and by private-tests/b_photo_probe.py,
which reads the downloaded photo in an isolated interpreter - so: numpy + the adapter helpers only, no jsonschema).

  Cam            pinhole of the frontal view (verify_b convention: xy of an (N, 3) array s, v, w)
  v_edge         vertical edge of either / a given polarity (G2 kolo 2, N6: the column changes polarity with height)
  edge_verdict   every sample judged when the samples are few or the edge is vertical - never a median of n = 2 (N6)
  front_masks    railing posts and brackets in front of the curtain walls, projected to px boxes (N9)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "adapters"))
from from_photo import dark_center, peak_in, vprof  # noqa: E402


class Cam:
    def __init__(self, c: dict):
        self.cx, self.cy, self.f, self.D0, self.s_c, self.v_c = (c[k] for k in ("cx", "cy", "f", "D0", "s_c", "v_c"))

    def xy(self, svw: np.ndarray) -> np.ndarray:
        z = self.D0 - svw[:, 2]
        return np.column_stack([self.cx + (svw[:, 0] - self.s_c) * self.f / z, self.cy - (svw[:, 1] - self.v_c) * self.f / z])

    def px_per_m(self, w: float) -> float:
        return self.f / (self.D0 - w)


def v_edge(g: np.ndarray, x_guess: float, y0: int, y1: int, sign: int | None, half: int = 14) -> tuple[float, float]:
    """Vertical edge near x_guess: strongest step of the given sign (+1 = brighter to the right; None = either sign,
    strength = |step|) of the column profile (median over the rows y0..y1); returns (x px, strength).
    G2 kolo 2 (N6): the round column stands against the bright sky in the upper bands and against darker foliage lower
    down - its edges change polarity, so a fixed sign measured a weak step beside the model (upper bands) or an edge in
    the tree behind it (lower band, the -4.69 cm sample)."""
    x0 = int(round(x_guess)) - half
    x0 = max(0, x0)
    prof = np.median(g[y0:y1, x0:x0 + 2 * half + 1], axis=0)
    d = np.abs(np.diff(prof)) if sign is None else np.diff(prof) * sign
    i = int(np.argmax(d))
    return x0 + i + 0.5, float(d[i])


EDGE_READABLE = 12.0      # a vertical edge sample weaker than this is not readable (excluded, reported)
SAMPLE_RULE_MAX_N = 3     # G2 kolo 2 (N6): with so few samples every sample must be in tolerance - never a median of n = 2


def edge_verdict(samples: list[float], tol: float, readable: list[bool] | None = None, per_sample: bool = False) -> dict:
    """Verdict of one edge from its samples (model - photo, m). per_sample (vertical edges) or n <= SAMPLE_RULE_MAX_N:
    EVERY readable sample within tol and at least 2 readable; else the median (horizontal edges with up to 7 samples).
    judged_cm = the value the verdict rests on (worst sample, or the median)."""
    readable = readable if readable is not None else [True] * len(samples)
    rs = [x for x, r in zip(samples, readable) if r]
    if not rs:
        return {"n": len(samples), "readable_n": 0, "median_cm": None, "max_cm": None, "judged_cm": None, "ok": False, "rule": "nečitateľné"}
    med = float(np.median(rs))
    worst = max(rs, key=abs)
    if per_sample or len(rs) <= SAMPLE_RULE_MAX_N:
        ok = len(rs) >= 2 and all(abs(x) <= tol + 1e-9 for x in rs)
        return {"n": len(samples), "readable_n": len(rs), "median_cm": round(med * 100, 2), "max_cm": round(abs(worst) * 100, 2),
                "judged_cm": round(worst * 100, 2), "ok": ok, "rule": "každá vzorka" + ("" if len(rs) >= 2 else " (< 2 čitateľné)")}
    return {"n": len(samples), "readable_n": len(rs), "median_cm": round(med * 100, 2), "max_cm": round(abs(worst) * 100, 2),
            "judged_cm": round(med * 100, 2), "ok": abs(med) <= tol, "rule": "medián"}


POST_MARGIN = 0.065        # m - margin of a railing post mask: reading of the post +-0.02 + its shadow / gap on the panel ~0.04 (set on
                          # CW_2NP_05 bar 1: dark valley 0.089 m left of the post axis; one case - NEOVERENÉ elsewhere)


def front_masks(spec: dict, cam) -> list[dict]:
    """Elements standing IN FRONT of the curtain walls, projected into the photo (px boxes): the brackets under the
    walkways and the railing posts on them (one post per bracket, adapter from_photo) - G2 kolo 2 (N9): in the parapet
    band they were measured as mullions. Only the posts and brackets mask: the bars of the railing (FS_Railing, R1
    closed 2026-10-20) are horizontal and the mullion reading takes the median over the rows. The post box stays at the
    depth the posts were read at (walkway edge, camera of the photo). Returns [{item, kind, x0, x1, y0, y1}]."""
    walks = [sl for sl in spec.get("slabs", []) if sl["kind"] == "walkway"]
    rails = {r["on"]: r for r in spec.get("railings", [])}
    out = []
    for it in spec.get("linear", []):
        if it.get("kind") != "bracket":
            continue
        sa, sb = it["s"]["v"]
        sc_ = 0.5 * (sa + sb)
        w0, w1 = it["w_span"]["v"]
        vb = it["v"]["v"]
        boxes = [("konzola", (sc_ - 0.05, sc_ + 0.05), (w0, w1), (vb, vb + 0.22))]
        wk = min(walks, key=lambda sl: abs(sl["v_top"]["v"] - (vb + 0.22))) if walks else None
        if wk is not None and wk["id"] in rails:
            h = spec["systems"][rails[wk["id"]]["system"]]["height"]["v"]
            # the post +- POST_MARGIN: its position comes from the reading of the post on the walkway edge (+-1-2 cm) and it
            # casts a dark shadow / gap on the panel behind it (CW_2NP_05 bar 1: valley ~20 px left of the post axis)
            boxes.append(("stĺpik zábradlia", (sc_ - 0.03 - POST_MARGIN, sc_ + 0.03 + POST_MARGIN), (w1 - 0.08, w1),
                          (wk["v_top"]["v"], wk["v_top"]["v"] + h + 0.05)))
        for kind, (s0, s1), (wa, wb), (v0, v1) in boxes:
            pts = np.array([[s, v, w] for s in (s0, s1) for v in (v0, v1) for w in (wa, wb)])
            xy = cam.xy(pts)
            out.append({"item": it["id"], "kind": kind, "x0": float(xy[:, 0].min()), "x1": float(xy[:, 0].max()),
                        "y0": float(xy[:, 1].min()), "y1": float(xy[:, 1].max())})
    return out


def masks_in(masks: list[dict], x0: float, x1: float, y0: float, y1: float, frac: float = 0.3) -> list[dict]:
    """Masks overlapping the window x0..x1 and at least `frac` of the rows y0..y1."""
    return [m for m in masks if m["x1"] >= x0 and m["x0"] <= x1 and min(y1, m["y1"]) - max(y0, m["y0"]) >= frac * (y1 - y0)]


def masked_dark_center(g: np.ndarray, x_guess: float, y0: int, y1: int, half: int, masks: list[dict]) -> tuple[float | None, float, list[str]]:
    """Darkest narrow valley (centre of a dark bar) near x_guess with the columns of the front elements neutralised
    (set to the brightest value of the profile). (None, 0, hits) when a front element covers the model bar itself."""
    hits = masks_in(masks, x_guess - half, x_guess + half, y0, y1)
    if not hits:
        x, st = dark_center(g, x_guess, y0, y1, half=half)
        return x, st, []
    names = sorted({f"{m['item']} ({m['kind']})" for m in hits})
    if any(m["x0"] - 3 <= x_guess <= m["x1"] + 3 for m in hits):
        return None, 0.0, names
    xs = int(x_guess) - half
    prof = vprof(g, xs, int(x_guess) + half + 1, y0, y1).astype(float)
    top = float(prof.max())
    for m in hits:
        a, b = max(0, int(np.floor(m["x0"])) - xs - 1), min(len(prof), int(np.ceil(m["x1"])) - xs + 2)
        prof[a:b] = top
    pos, st = peak_in(prof, dark=True)
    return xs + pos, st, names

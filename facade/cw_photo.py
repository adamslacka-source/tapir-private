"""cw_photo: frame bars (mullions / transoms) of a glazed field on the rectified photo (ADR-001 I5, pilot A).

A mullion is a bar of known face width that differs in brightness from the glass on both sides (white or coloured
frame on dark glass, or the other way round). find_bar looks for the bar centre near a predicted axis: for every
candidate centre the mean brightness inside a window of the bar width is compared with the mean of the two flanks of
the same width; the centre with the largest absolute difference wins. Pure numpy, used by verify (photo check of the
raster) and to read the raster from the photo when the spec is written (facade/adapters/senec_types/*.json, 'i6').

    python cw_photo.py <rectified.jpg> <S> <u0> <v1> v|h <at> <width> <r0> <r1> [search]
"""
from __future__ import annotations

import sys

import numpy as np


def _profile(lum: np.ndarray, rect: dict, axis: str, lo: float, hi: float, r0: float, r1: float) -> tuple[np.ndarray, float]:
    """Brightness profile across the bar. axis 'v' = vertical bar (profile along s, mean over v in r0..r1),
    'h' = horizontal bar (profile along v from top to bottom, mean over s in r0..r1). Returns (profile, first position)."""
    S, u0, v1 = rect["S"], rect["u0"], rect["v1"]
    if axis == "v":
        x0, x1 = int(round((lo - u0) * S)), int(round((hi - u0) * S))
        y0, y1 = int(round((v1 - r1) * S)), int(round((v1 - r0) * S))
        x0, x1 = max(0, x0), min(lum.shape[1], x1)
        y0, y1 = max(0, y0), min(lum.shape[0], y1)
        return lum[y0:y1, x0:x1].mean(axis=0), x0
    y0, y1 = int(round((v1 - hi) * S)), int(round((v1 - lo) * S))
    x0, x1 = int(round((r0 - u0) * S)), int(round((r1 - u0) * S))
    x0, x1 = max(0, x0), min(lum.shape[1], x1)
    y0, y1 = max(0, y0), min(lum.shape[0], y1)
    return lum[y0:y1, x0:x1].mean(axis=1), y0


def find_bar(lum: np.ndarray, rect: dict, axis: str, at: float, width: float, rng, search: float = 0.05) -> tuple[float, float]:
    """Centre of the frame bar nearest to `at` (m, s for a vertical bar, v for a horizontal one) and its contrast.
    width = face width of the bar (m), rng = (r0, r1) the extent along the bar that is averaged."""
    S = rect["S"]
    half = max(1, int(round(width * S / 2)))
    lo, hi = at - search - 2.5 * width, at + search + 2.5 * width
    prof, first = _profile(lum, rect, axis, lo, hi, min(rng), max(rng))
    if len(prof) < 6 * half:
        raise ValueError("search window outside the photo")
    best, best_c = -1.0, None
    for c in range(3 * half, len(prof) - 3 * half):
        inner = prof[c - half:c + half].mean()
        flanks = 0.5 * (prof[c - 3 * half:c - half].mean() + prof[c + half:c + 3 * half].mean())
        pos = _pos(axis, rect, first + c)
        if abs(pos - at) > search + 1e-9:
            continue
        score = abs(inner - flanks)
        if score > best:
            best, best_c = score, c
    if best_c is None:
        raise ValueError("no candidate inside the search window")
    return _pos(axis, rect, first + best_c), float(best)


def _pos(axis: str, rect: dict, px: float) -> float:
    S, u0, v1 = rect["S"], rect["u0"], rect["v1"]
    return px / S + u0 if axis == "v" else v1 - px / S


def main(argv) -> int:
    from PIL import Image
    if len(argv) < 10:
        print(__doc__)
        return 2
    lum = np.asarray(Image.open(argv[1]).convert("L"), dtype=float)
    rect = {"S": float(argv[2]), "u0": float(argv[3]), "v1": float(argv[4])}
    search = float(argv[10]) if len(argv) > 10 else 0.05
    pos, c = find_bar(lum, rect, argv[5], float(argv[6]), float(argv[7]), (float(argv[8]), float(argv[9])), search)
    print(f"{pos:.3f} contrast {c:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

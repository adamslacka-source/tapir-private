"""Local facade frame (s, v, w) <-> model coordinates. Pure Python + numpy."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Frame:
    a: np.ndarray        # left end seen from outside (x, y)
    b: np.ndarray
    d: np.ndarray        # unit vector a -> b
    n: np.ndarray        # unit outward normal (towards the street)
    z0: float
    length: float

    def to_svw(self, pts: np.ndarray) -> np.ndarray:
        """(N, 3) model xyz -> (N, 3) facade s, v, w."""
        rel = pts[:, :2] - self.a
        return np.column_stack([rel @ self.d, pts[:, 2] - self.z0, rel @ self.n])

    def xy(self, s: float, w: float = 0.0) -> tuple[float, float]:
        p = self.a + s * self.d + w * self.n
        return float(p[0]), float(p[1])


def frame_of(spec: dict) -> Frame:
    ref = spec["reference"]
    a = np.asarray(ref["a"], float)
    b = np.asarray(ref["b"], float)
    length = float(np.linalg.norm(b - a))
    d = (b - a) / length
    right = np.array([d[1], -d[0]])                    # right-hand normal of a -> b in plan
    n = right if ref["outside"] == "right_of_ab" else -right
    return Frame(a, b, d, n, float(ref["z0"]["v"]), length)


def rect_overlap_area(r1, r2) -> float:
    ds = min(r1[1], r2[1]) - max(r1[0], r2[0])
    dv = min(r1[3], r2[3]) - max(r1[2], r2[2])
    return ds * dv if ds > 0 and dv > 0 else 0.0


def rects_touch(r1, r2, tol: float = 1e-6) -> bool:
    ds = min(r1[1], r2[1]) - max(r1[0], r2[0])
    dv = min(r1[3], r2[3]) - max(r1[2], r2[2])
    return (abs(ds) <= tol and dv > 0) or (abs(dv) <= tol and ds > 0)


def angle_deg(v) -> float:
    return math.degrees(math.atan2(v[1], v[0]))

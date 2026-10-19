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

    def angle(self) -> float:
        """Plan angle of a -> b (rad)."""
        return math.atan2(float(self.d[1]), float(self.d[0]))


REFERENCE_PLANE = "F"          # id of the plane given by spec.reference (the facade of the photo)


def _frame(a, b, outside: str, z0: float) -> Frame:
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    length = float(np.linalg.norm(b - a))
    d = (b - a) / length
    right = np.array([d[1], -d[0]])                    # right-hand normal of a -> b in plan
    n = right if outside == "right_of_ab" else -right
    return Frame(a, b, d, n, float(z0), length)


def frame_of(spec: dict) -> Frame:
    ref = spec["reference"]
    return _frame(ref["a"], ref["b"], ref["outside"], ref["z0"]["v"])


def frames_of(spec: dict) -> dict[str, Frame]:
    """Plane id -> Frame: the reference plane 'F' and every entry of spec.planes (same z0 as the reference)."""
    out = {REFERENCE_PLANE: frame_of(spec)}
    z0 = spec["reference"]["z0"]["v"]
    for p in spec.get("planes", []):
        out[p["id"]] = _frame(p["a"], p["b"], p["outside"], z0)
    return out


def outside_of(spec: dict, plane: str) -> str:
    if plane == REFERENCE_PLANE:
        return spec["reference"]["outside"]
    return next(p["outside"] for p in spec.get("planes", []) if p["id"] == plane)


def zone_w(zone: dict) -> float:
    """Offset of the outside face of a wall zone from its plane (m, + = out); 0 when not given."""
    return float(zone["w"]["v"]) if zone.get("w") else 0.0


def item_plane(item: dict, zones: dict | None = None) -> str:
    """Plane of a zone / linear element / object (explicit 'plane'), or of an opening (through its zone)."""
    if item.get("plane"):
        return item["plane"]
    if zones and item.get("zone") in zones:
        return zones[item["zone"]].get("plane", REFERENCE_PLANE)
    return REFERENCE_PLANE


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

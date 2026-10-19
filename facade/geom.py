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


# ---- I5/I6 (pilot A): curtain wall fields, balconies, railings ------------------------------------------------------
def cw_grid(cw: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Columns (s0, s1) left -> right and rows (v0, v1) bottom -> top of a curtain wall field (rect + inner axes)."""
    s0, s1, v0, v1 = cw["rect"]["v"]
    us = [s0] + list(cw.get("u_axes", {}).get("v", [])) + [s1]
    vs = [v0] + list(cw.get("v_axes", {}).get("v", [])) + [v1]
    return list(zip(us[:-1], us[1:])), list(zip(vs[:-1], vs[1:]))


def cw_cell_panels(cw: dict) -> dict[tuple[int, int], str]:
    """(col, row) -> panel kind of every cell (default 'glass')."""
    cols, rows = cw_grid(cw)
    out = {(c, r): "glass" for c in range(len(cols)) for r in range(len(rows))}
    for cell in cw.get("cells", []):
        out[(cell["col"], cell["row"])] = cell["panel"]
    return out


def arc_through(p, q, sagitta: float) -> tuple[np.ndarray, float, float]:
    """Circle of the arc from p to q (2D) bulging by `sagitta` to the LEFT of p -> q: (centre, radius, central angle)."""
    p, q = np.asarray(p, float), np.asarray(q, float)
    c = float(np.linalg.norm(q - p))
    r = (c * c / 4.0 + sagitta * sagitta) / (2.0 * sagitta)
    d = (q - p) / c
    left = np.array([-d[1], d[0]])
    mid = 0.5 * (p + q)
    centre = mid + left * (sagitta - r)
    return centre, r, 2.0 * math.asin(min(1.0, c / (2.0 * r)))


def zone_face_at(spec: dict, plane: str, s: float, v: float) -> float | None:
    """w of the outside face of the wall zone of `plane` that contains (s, v) (None outside every zone)."""
    for z in spec["zones"]:
        if z.get("plane", REFERENCE_PLANE) != plane:
            continue
        zs0, zs1, zv0, zv1 = z["rect"]["v"]
        if zs0 - 1e-9 <= s <= zs1 + 1e-9 and zv0 - 1e-9 <= v <= zv1 + 1e-9:
            return zone_w(z)
    return None


def balcony_back(spec: dict, b: dict) -> list[tuple[float, float]]:
    """Back edge of a balcony slab along the faces of the wall zones it stands on, s0 -> s1: [(s, w), ...] with a step
    where the zone face changes (e.g. a rizalit 0.12 m in front of the main wall)."""
    plane = b.get("plane", REFERENCE_PLANE)
    s0, s1 = b["s"]["v"]
    v = b["v_top"]["v"] - 1e-3
    cuts = sorted({s0, s1} | {x for z in spec["zones"] if z.get("plane", REFERENCE_PLANE) == plane
                              for x in z["rect"]["v"][:2] if s0 + 1e-6 < x < s1 - 1e-6})
    pts: list[tuple[float, float]] = []
    for a, c in zip(cuts[:-1], cuts[1:]):
        w = zone_face_at(spec, plane, 0.5 * (a + c), v)
        if w is None:
            raise ValueError(f"balcony {b['id']}: no wall zone behind s {a:.3f}..{c:.3f} at v {v:.3f}")
        for pt in ((a, w), (c, w)):
            if not pts or abs(pts[-1][0] - pt[0]) > 1e-9 or abs(pts[-1][1] - pt[1]) > 1e-9:
                pts.append(pt)
    return pts


def balcony_outline(spec: dict, b: dict) -> dict:
    """Outline of a balcony slab in (s, w) of its plane: the back edge on the wall faces (s0 -> s1), the sides up to
    w_side, the front = circular arc from (s1, w_side) to (s0, w_side) through the crown (centre, w_crown).
    Returns {'pts': [(s, w)...] (open ring), 'arc': (index of the arc start, index of the arc end, centre, radius, angle)}."""
    s0, s1 = b["s"]["v"]
    ws, wc = b["w_side"]["v"], b["w_crown"]["v"]
    back = balcony_back(spec, b)
    pts = back + [(s1, ws), (s0, ws)]
    centre, r, ang = arc_through((s0, ws), (s1, ws), wc - ws)          # bulges to +w (left of s0 -> s1)
    n = len(pts)
    return {"pts": pts, "arc": (n - 2, n - 1, centre, r, ang), "crown": (0.5 * (s0 + s1), wc)}


def railing_path(spec: dict, b: dict, inset: float, chords: int) -> list[tuple[float, float]]:
    """Reference line of the railing of balcony b in (s, w): along the free edges (side, arc, side) `inset` inside the slab
    edge, from `inset` in front of the wall face at s0 to the same at s1; the arc approximated by `chords` straight segments
    (CreateRailings has straight segments only)."""
    o = balcony_outline(spec, b)
    s0, s1 = b["s"]["v"]
    centre, r, _ = o["arc"][2], o["arc"][3], o["arc"][4]
    ri = r - inset
    sa, sb = s0 + inset, s1 - inset
    wa = float(centre[1] + math.sqrt(max(ri * ri - (sa - centre[0]) ** 2, 0.0)))
    wb = float(centre[1] + math.sqrt(max(ri * ri - (sb - centre[0]) ** 2, 0.0)))
    a0, a1 = math.atan2(wa - centre[1], sa - centre[0]), math.atan2(wb - centre[1], sb - centre[0])
    arc = [(float(centre[0] + ri * math.cos(a0 + (a1 - a0) * k / chords)), float(centre[1] + ri * math.sin(a0 + (a1 - a0) * k / chords)))
           for k in range(chords + 1)]
    back = balcony_back(spec, b)
    w_wall0 = back[0][1] + inset                              # the end posts stand on the slab, not in the wall
    w_wall1 = back[-1][1] + inset
    return [(sa, w_wall0)] + arc + [(sb, w_wall1)]


def chord_error(r: float, ang: float, chords: int) -> float:
    """Largest distance between an arc (radius r, central angle ang) and its polyline of `chords` equal chords."""
    return r * (1.0 - math.cos(ang / chords / 2.0))

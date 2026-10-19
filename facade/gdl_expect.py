"""Expected geometry of the FS GDL templates from their parameters (pure Python, used by verify and the tests).

Local system of every template (gdl/templates/README.md): x = along the facade, y = out of the wall plane, z = up;
origin = insertion point. The build places an object with angle atan2(d) (+180 deg when the outside is right of a->b),
so local x runs along +s (outside left) or -s (outside right), local y = outward normal n, z = up.
"""
from __future__ import annotations

import math

# template -> parameters the expected geometry needs (validated in spec_validate._check_objects)
TEMPLATES = {
    "FS_Surround": ("opW", "opH", "bandW", "bandD", "bTop", "bBottom", "bLeft", "bRight", "headOvh"),
    "FS_ArchGable": ("rIn", "rOut", "depth", "nSeg"),
    "FS_Louvers": ("A", "ZZYZX", "lamD", "offs", "bFrame", "frameD"),
}


def local_bbox(template: str, p: dict) -> list[float]:
    """[x0, x1, y0, y1, z0, z1] of the 3D body in the local system of the object."""
    if template == "FS_Surround":
        wl = p["bandW"] if p["bLeft"] else 0.0
        wr = p["bandW"] if p["bRight"] else 0.0
        x0, x1 = -wl, p["opW"] + wr
        if p["bTop"]:
            x0, x1 = min(x0, -wl - p["headOvh"]), max(x1, p["opW"] + wr + p["headOvh"])
        z0 = -p["bandW"] if p["bBottom"] else 0.0
        z1 = p["opH"] + (p["bandW"] if p["bTop"] else 0.0)
        return [x0, x1, 0.0, p["bandD"], z0, z1]
    if template == "FS_ArchGable":
        return [-p["rOut"], p["rOut"], -p["depth"], 0.0, 0.0, p["rOut"]]
    if template == "FS_Louvers":                       # hotspot box of the template: y offs .. offs + max(lamD, frameD)
        d = max(p["lamD"], p["frameD"]) if p["bFrame"] else p["lamD"]
        return [0.0, p["A"], p["offs"], p["offs"] + d, 0.0, p["ZZYZX"]]
    raise KeyError(f"no expectation for template {template}")


def info_dims(template: str, p: dict) -> dict:
    """A, B, ZZYZX (information parameters; CreateObjects would otherwise take them from the tool defaults)."""
    x0, x1, y0, y1, z0, z1 = local_bbox(template, p)
    return {"A": round(x1 - x0, 6), "B": round(y1 - y0, 6), "ZZYZX": round(z1 - z0, 6)}


def sign_of(outside: str) -> float:
    """Direction of local x along s."""
    return -1.0 if outside == "right_of_ab" else 1.0


def facade_bbox(template: str, p: dict, origin_svw: list[float], outside: str) -> list[float]:
    """[s0, s1, v0, v1, w0, w1] of the object on the facade (origin = local 0,0,0 in s, v, w)."""
    x0, x1, y0, y1, z0, z1 = local_bbox(template, p)
    sg = sign_of(outside)
    s_a, s_b = origin_svw[0] + sg * x0, origin_svw[0] + sg * x1
    return [min(s_a, s_b), max(s_a, s_b), origin_svw[1] + z0, origin_svw[1] + z1, origin_svw[2] + y0, origin_svw[2] + y1]


def plan_angle(frame_angle: float, outside: str) -> float:
    """Object angle (rad) for a facade whose a -> b has plan angle frame_angle."""
    return frame_angle + (math.pi if outside == "right_of_ab" else 0.0)


def arch_radii(template: str, p: dict) -> tuple[float, float] | None:
    return (p["rIn"], p["rOut"]) if template == "FS_ArchGable" else None

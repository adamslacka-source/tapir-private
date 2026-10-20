"""Photo probe of pilot B for test_i7_loop.py (G2 kolo 2, N6 / N9) - run ISOLATED: python -I b_photo_probe.py

The photo of pilot B is a downloaded file, so it is only read in an isolated interpreter; this prints one JSON line:
  column   every band of the round column STLP_L: model px (from the spec through the camera), the edge with the OLD fixed
           polarity and with either polarity (verify_b.v_edge), model - photo in cm
  masks    elements in front of the facade (verify_b.front_masks) and which of them cover the parapet band of
           CW_2NP_05 near its first mullion (the 4993 px valley of kontrola_G2_kolo2 N9)
"""
import json
import sys
from pathlib import Path

FACADE = Path(__file__).resolve().parent.parent / "facade"
sys.path.insert(0, str(FACADE))
sys.path.insert(0, str(FACADE / "adapters"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import photo_b as verify_b  # noqa: E402  (photo part of verify_b, importable without jsonschema)

SPEC = Path(r"C:\FacadePilot\pilot_B\out\HOF_B.facade-spec.json")


def main() -> int:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    src = next(s for s in spec["sources"] if s["id"] == "S1")
    cam = verify_b.Cam(src["camera"])
    g = cv2.imread(str(src["rect_image"]), cv2.IMREAD_COLOR).astype(np.float32).mean(axis=2)
    stl = next(it for it in spec["linear"] if it["id"] == "STLP_L")
    s0, s1 = stl["s"]["v"]
    d = s1 - s0
    w_ax = stl["w"]["v"] + d / 2
    out = {"column": []}
    # silhouette of the cylinder through the camera at the band height (tangent points ~ axis +- r at the axis depth)
    phi = np.linspace(0, 2 * np.pi, 721)
    for y0, y1 in ((2420, 2600), (2650, 2900), (3250, 3400), (3400, 3700)):
        vmid = cam.v_c - ((0.5 * (y0 + y1)) - cam.cy) * (cam.D0 - w_ax) / cam.f
        # silhouette of the cylinder through the camera: extreme x of its circle (as verify_b from the 3D vertices)
        circ = np.column_stack([0.5 * (s0 + s1) + d / 2 * np.cos(phi), np.full_like(phi, vmid), w_ax + d / 2 * np.sin(phi)])
        xs = cam.xy(circ)[:, 0]
        for name, i_e, old_sign in (("left", int(np.argmin(xs)), +1), ("right", int(np.argmax(xs)), -1)):
            xm = float(xs[i_e])
            ppm = cam.px_per_m(float(circ[i_e, 2]))
            x_old, st_old = verify_b.v_edge(g, xm, y0, y1, old_sign, 14)
            x_new, st_new = verify_b.v_edge(g, xm, y0, y1, None, 14)
            out["column"].append({"band": [y0, y1], "edge": name, "model_px": round(xm, 1), "old_px": round(x_old, 1), "old_step": round(st_old, 1),
                                  "new_px": round(x_new, 1), "new_step": round(st_new, 1), "old_cm": round((xm - x_old) / ppm * 100, 2),
                                  "new_cm": round((xm - x_new) / ppm * 100, 2)})
    masks = verify_b.front_masks(spec, cam)
    cw = next(c for c in spec["cw_fields"] if c["id"] == "CW_2NP_05")
    w = cw["photo_w"]["v"] if cw.get("photo_w") else -cw["reveal"]["v"]
    vv = [cw["rect"]["v"][2]] + cw.get("v_axes", {}).get("v", []) + [cw["rect"]["v"][3]]
    a, b = vv[0], vv[1]
    xm = float(cam.xy(np.array([[cw["u_axes"]["v"][0], 0.5 * (a + b), w]]))[0][0])
    ya, yb = sorted(cam.xy(np.array([[cw["u_axes"]["v"][0], b - 0.04, w], [cw["u_axes"]["v"][0], a + 0.04, w]]))[:, 1])
    hits = verify_b.masks_in(masks, xm - 30, xm + 30, ya, yb)
    out["masks"] = {"n": len(masks), "kinds": sorted({m["kind"] for m in masks}), "cw_2np_05_bar1": {"model_px": round(xm, 1), "row_v": [a, b],
                    "hits": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in m.items()} for m in hits]}}
    print(json.dumps(out))                      # ASCII: the console of an isolated interpreter may be cp1252
    return 0


if __name__ == "__main__":
    sys.exit(main())

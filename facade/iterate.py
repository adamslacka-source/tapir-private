"""iterate: the I7 loop of ADR-001 - build -> verify (model vs spec, model edges vs the photo) -> classify every deviation
over tolerance -> fix -> build / verify again, until everything is within tolerance or N iterations, without a human.

Classes (every deviation gets exactly one, with its evidence):
  build      the model differs from the spec (rows of verify: parameters / 3D read back, type rows, missing element)
             -> the element is rebuilt from the spec (build(rebuild=...)); the spec is not touched
  spec       the model equals the spec but the edge on the photo is elsewhere -> the edge is measured again on the
             rectified photo at full resolution in a wide window (photo_measure.py) and, when it is certain, the spec
             value is corrected (corrections file, src 'I7.<k>', conf 'meranie'); build then rebuilds only the touched
             elements (hash of their payload). Two ways:
               rigid shift  two opposite edges of one opening off in the same direction -> the opening is searched as
                            a pair at its width (both steps strongest at once) and moved
               one edge     a single edge: a unique strong step, the thirds of the edge agree, it ends at the jambs
                            (does not run on beyond the opening), it is not hidden by / does not coincide with another
                            element of the model, it lies OUTSIDE the spec opening (the spec reads masonry openings; a
                            step inside is a frame / sash) and within 8 cm
  ambiguous  the edge cannot be read (no step, two edges, thirds disagree, hidden by an awning / sign / sill, shared
             jamb of touching openings, double bar) -> not corrected; reported for G2 with a crop. When the wide
             measurement puts the edge within tolerance of the model the narrow check of verify was the problem
             ('confirmed': the model stays)
  systemic   many edges shifted the same way (scale, camera, frame of the rectified photo) -> a global offset of the
             photo frame is corrected only when it lowers the total error (else reported)

Every correction is logged in <out>/<id>/i7/iterations.json (before / after, cause, evidence, crop PNG) and has to lower
its error in the next verify - otherwise it is reverted (status 'reverted' in the corrections file) and the model is
rebuilt. Reports for gate G2: report_G2.md per pilot.

    python iterate.py A [--houses M015_L1 M062_L2 M016_L2] [--max-iter 3]
    python iterate.py B
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "adapters"))
import build as fb  # noqa: E402
import spec_corrections as sc  # noqa: E402
import spec_diff  # noqa: E402
from build import opening_units, unit_reveal  # noqa: E402
from geom import REFERENCE_PLANE, hidden_edge, jamb_context, rects_touch, zone_w  # noqa: E402
from photo_measure import MIN_REVEAL_M, REVEAL_WIN, Photo, crop_png, judge_reveal_edge, remeasure_edge, rigid_shift, visible_reveal  # noqa: E402
from spec_validate import validate  # noqa: E402
from tapir_client import TapirClient, TapirError  # noqa: E402

MAX_ITER = 3
MAX_EDGE_SHIFT = 0.08      # m - a single edge further than this from the model is another feature
RIGID_HALF = 0.35          # m - search window of a rigid shift
EDGE_HALF = 0.10           # m - search window of one edge
OCCLUDE_TOL = 0.03         # m - an element edge this close to the photo edge "coincides"
CONFIRM_RATIO = 1.5        # pilot B: a valley confirms a mullion only when >= this x the next valley (G2 cause 3)
SYSTEMIC_MIN_HOSTS = 6
SYSTEMIC_SAME_SIGN = 0.8
EDGES_V = ("left", "right")
EDGES_H = ("top", "bottom")
PROJECT_A = r"C:\FacadePilot\Fasada_pilot_A.pln"
PROJECT_B = r"C:\FacadePilot\Fasada_pilot_B.pln"
OUT_A = Path(r"C:\FacadePilot\out")
OUT_B = Path(r"C:\FacadePilot\pilot_B\out")


# ---- camera of the photo (pilot A: verify/linear_verify convention) ----------------------------------------------------
def photo_of_spec(spec: dict, cam: dict | None, s: float, v: float, w: float) -> tuple[float, float]:
    if not cam:
        return s, v
    t = cam["D"] / (cam["D"] - w)
    sp = s if cam.get("s") is None else cam["s"] + (s - cam["s"]) * t
    return sp, cam["v"] + (v - cam["v"]) * t


def spec_of_photo(cam: dict | None, axis: str, x: float, w: float) -> float:
    """Inverse of photo_of_spec for one coordinate (axis 's' or 'v') of a point at depth w."""
    if not cam:
        return x
    t = cam["D"] / (cam["D"] - w)
    if axis == "s":
        return x if cam.get("s") is None else cam["s"] + (x - cam["s"]) / t
    return cam["v"] + (x - cam["v"]) / t


def edge_axis(edge: str) -> str:
    return "s" if edge in EDGES_V else "v"


def edge_index(edge: str) -> int:
    return {"left": 0, "right": 1, "bottom": 2, "top": 3}[edge]


def outward_sign(edge: str) -> int:
    return {"left": -1, "right": 1, "bottom": -1, "top": 1}[edge]


def csv_rows(path) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---- systemic ---------------------------------------------------------------------------------------------------------
def systemic_check(samples: list[dict], tol: float) -> dict:
    """samples: [{host, axis ('s'|'v'), delta (model - photo, m)}] of every readable photo edge. Per axis: when at least
    SYSTEMIC_MIN_HOSTS hosts are measured, >= 80 % of the host medians have one sign and |median| >= tol / 2, an offset
    of the photo frame by the median is proposed; it is accepted only if the total absolute error drops by >= 20 % and
    no edge leaves the tolerance."""
    out = {}
    for axis in ("s", "v"):
        by_host = {}
        for x in samples:
            if x["axis"] == axis:
                by_host.setdefault(x["host"], []).append(x["delta"])
        meds = [float(np.median(v)) for v in by_host.values()]
        all_d = np.array([x["delta"] for x in samples if x["axis"] == axis], float)
        res = {"hosts": len(meds), "median_cm": round(float(np.median(meds)) * 100, 2) if meds else None, "triggered": False, "accepted": False}
        if len(meds) >= SYSTEMIC_MIN_HOSTS:
            pos = sum(m > 0 for m in meds) / len(meds)
            same = max(pos, 1 - pos)
            med = float(np.median(meds))
            res["same_sign"] = round(same, 2)
            if same >= SYSTEMIC_SAME_SIGN and abs(med) >= tol / 2:
                res["triggered"] = True
                before, after = float(np.abs(all_d).sum()), float(np.abs(all_d - med).sum())
                in_b, in_a = int((np.abs(all_d) <= tol).sum()), int((np.abs(all_d - med) <= tol).sum())
                res.update(offset_m=round(med, 4), err_before_cm=round(before * 100, 1), err_after_cm=round(after * 100, 1),
                           in_tol_before=in_b, in_tol_after=in_a, accepted=after <= 0.8 * before and in_a >= in_b)
        out[axis] = res
    return out


# ---- the loop ---------------------------------------------------------------------------------------------------------
class Loop:
    """Pilot-independent part: iterations, evaluation of the fixes, revert, log."""

    def __init__(self, pilot, max_iter: int = MAX_ITER, log=print, log_dir: Path | None = None):
        self.p = pilot
        self.max_iter = max_iter
        self.log = log
        self.log_dir = log_dir

    def run(self) -> dict:
        p = self.p
        t0 = time.monotonic()
        corr = sc.load(p.corr_path, p.spec_id)
        i7_dir = self.log_dir or p.out_dir / "i7"
        i7_dir.mkdir(parents=True, exist_ok=True)
        p.i7_dir = i7_dir
        iterations, pending_rebuild, open_fixes = [], set(), []
        first_findings, final = None, None
        n_fix_total = 0
        snapshot = i7_dir / "spec_snapshot.json"
        changes = None
        for k in range(1, self.max_iter + 2):            # max_iter fix rounds + the confirming round
            spec = p.load_spec(corr)
            errors, _ = validate(spec)
            if errors:
                raise TapirError(f"{p.spec_id}: spec invalid in iteration {k}: {errors[:3]}")
            if changes is None:                           # G2 kolo 2 (N3): what changed since the last run, also outside corrections
                changes = spec_diff.spec_changes(snapshot, spec, corr.get("corrections", []))
            b = p.build(spec, pending_rebuild)
            ver = p.verify(spec)
            pending_rebuild = set()
            it = {"k": k, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "build_changes": b.get("n_changes"), "build_mismatches": b.get("mismatches", [])[:10],
                  "verify_pass": p.verify_pass(ver), "photo_summary": None, "evaluated": [], "findings": [], "fixes": [], "systemic": None}
            # evaluate the fixes of the previous round: each must lower its error, else revert
            reverted = 0
            reverted_items = set()
            for fx in open_fixes:
                ev = p.evaluate(spec, ver, fx)
                fx.update(after=ev)
                ok = ev["error_cm"] is not None and ev["error_cm"] < fx["error_before_cm"] - 1e-6 and ev.get("in_tolerance", True)
                it["evaluated"].append({"id": fx["id"], "cause": fx["cause"], "item": fx["item"], "before_cm": fx["error_before_cm"],
                                        "after_cm": ev["error_cm"], "in_tolerance": ev.get("in_tolerance"), "kept": ok, "note": ev.get("note", "")})
                for cid in fx.get("corrections", []):         # the evaluation travels with the correction (G2 report)
                    c = next((c for c in corr["corrections"] if c["id"] == cid), None)
                    if c is not None:
                        c.update(error_before_cm=fx["error_before_cm"], error_after_cm=ev["error_cm"], kept=ok, evaluated_in=k)
                        for x in c.get("edges", []):               # per edge (G2 kolo 2, N2)
                            x["error_after_cm"] = (ev.get("per_edge") or {}).get(x["edge"])
                if not ok and fx["cause"] in ("spec", "systemic"):
                    for cid in fx["corrections"]:
                        c = next(c for c in corr["corrections"] if c["id"] == cid)
                        c["status"], c["reverted_reason"] = "reverted", f"iterácia {k}: chyba {ev['error_cm']} cm nie je menšia ako {fx['error_before_cm']} cm"
                    reverted += 1
                    reverted_items.add(fx["item"])
                    pending_rebuild |= set(fx.get("rebuild", []))
            open_fixes = []
            findings = p.findings(spec, ver)
            classified = p.classify(spec, ver, findings, k, i7_dir)
            if first_findings is None:
                first_findings = classified
            it["findings"] = [{kk: vv for kk, vv in f.items() if kk not in ("raw",)} for f in classified]
            it["photo_summary"] = p.photo_summary(ver, classified)     # ambiguous edges apart (G2 kolo 2, cause 3)
            # systemic: offsets of the photo frame
            sysres = p.systemic(spec, ver)
            it["systemic"] = sysres
            fixes = []
            if k <= self.max_iter:
                # an item whose correction was reverted in this round was classified on the spec before the revert: its
                # findings wait for the rebuild (next round), a fix now would be stale
                fixes = [f for f in classified if f["cls"] in ("build", "spec") and f.get("fix")
                         and not (f["cls"] == "spec" and f["fix"].get("item") in reverted_items)]
                # one fix per target (several findings of one opening share it)
                seen, uniq = set(), []
                for f in fixes:
                    key = json.dumps(f["fix"]["key"])
                    if key not in seen:
                        seen.add(key)
                        uniq.append(f)
                fixes = uniq
                applied = p.apply_fixes(spec, fixes, corr, k)
                for fx in applied:
                    if fx["cause"] == "build":
                        pending_rebuild |= set(fx["rebuild"])
                    open_fixes.append(fx)
                    it["fixes"].append({kk: vv for kk, vv in fx.items() if kk not in ("after",)})
                for axis, res in (sysres or {}).items():
                    if res.get("accepted"):
                        fx = p.apply_systemic(spec, axis, res, corr, k)
                        if fx:
                            open_fixes.append(fx)
                            it["fixes"].append(fx)
                if any(fx["cause"] != "build" for fx in open_fixes) or reverted:
                    sc.save(p.corr_path, corr)
            n_fix_total += len(it["fixes"])
            iterations.append(it)
            self.log(f"  {p.spec_id} iterácia {k}: build {it['build_changes']} zmien, findings {len(classified)} "
                     f"({', '.join(f'{c}: {sum(f['cls'] == c for f in classified)}' for c in ('build', 'spec', 'ambiguous', 'systemic'))}), "
                     f"opravy {len(it['fixes'])}, vrátené {reverted}")
            final = {"spec": spec, "verify": ver, "findings": classified}
            if not open_fixes and not pending_rebuild:
                break
        if pending_rebuild:                               # reverts of the last round: rebuild once more
            spec = p.load_spec(corr)
            p.build(spec, pending_rebuild)
            ver = p.verify(spec)
            final = {"spec": spec, "verify": ver, "findings": p.classify(spec, ver, p.findings(spec, ver), len(iterations) + 1, i7_dir)}
            iterations[-1]["photo_summary_final"] = p.photo_summary(ver, final["findings"])
        # ambiguous findings of the final state -> simplifications of the spec (G1 / G2: reported, never silent)
        if p.report_ambiguous(final["findings"], corr):
            sc.save(p.corr_path, corr)
            spec = p.load_spec(corr)
            final["spec"] = spec
        log = {"pilot": p.name, "spec": p.spec_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.monotonic() - t0, 1),
               "max_iter": self.max_iter, "iterations": iterations, "fixes_total": n_fix_total,
               "first_findings": [{kk: vv for kk, vv in f.items() if kk != "raw"} for f in first_findings or []],
               "final_findings": [{kk: vv for kk, vv in f.items() if kk != "raw"} for f in final["findings"]],
               "corrections_file": str(p.corr_path), "corrections_active": [c["id"] for c in corr["corrections"] if c.get("status", "active") == "active"],
               "verify_pass": p.verify_pass(final["verify"]), "spec_changes": changes}
        if any(e for it in iterations for e in it["evaluated"]):
            sc.save(p.corr_path, corr)
        spec_diff.save_snapshot(snapshot, final["spec"], f"koniec behu {time.strftime('%Y-%m-%d %H:%M')}")
        if i7_dir == p.out_dir / "i7":                    # the real run: verdicts for the overlay colours (N10) + redraw
            write_verdicts(i7_dir, final["findings"])
            p.redraw(final["spec"])
        hist_path = i7_dir / "iterations.json"
        hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {"runs": []}
        if "runs" not in hist:
            hist = {"runs": [hist]}
        log["run"] = len(hist["runs"]) + 1
        hist["runs"].append(log)
        hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
        return log


# ---- pilot A ----------------------------------------------------------------------------------------------------------
class PilotA:
    name = "A"

    def __init__(self, house: str, port: int = 19723, corr_path: Path | None = None, types_hook=None, out_root: Path = OUT_A,
                 project: str = PROJECT_A):
        self.spec_id = house
        self.port = port
        self.corr_path = Path(corr_path) if corr_path else sc.path_of(house)
        self.types_hook = types_hook
        self.out_root = out_root
        self.out_dir = out_root / house
        self.project = project
        self._photo = None

    # -- adapters
    def load_spec(self, corr: dict | None = None) -> dict:
        from from_senec import build_spec
        if corr is not None:
            sc.save(self.corr_path, corr) if self.corr_path.exists() or corr["corrections"] else None
        spec = build_spec(self.spec_id, i6=True, corrections=self.corr_path if self.corr_path.exists() else False, types_hook=self.types_hook)
        p = self.out_dir / f"{self.spec_id}.facade-spec.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        return spec

    def build(self, spec: dict, rebuild: set) -> dict:
        return fb.build(spec, self.project, self.port, self.out_root, verbose=False, rebuild=rebuild)

    def verify(self, spec: dict) -> dict:
        import verify as fv
        return fv.verify(spec, self.project, self.port, self.out_root, verbose=False)

    @staticmethod
    def verify_pass(ver: dict) -> bool:
        return bool(ver.get("pass"))

    def redraw(self, spec: dict) -> None:
        """verify once more after the verdicts are written: the overlay colours ambiguous edges apart (N10)."""
        self.verify(spec)

    def photo(self, spec: dict) -> Photo:
        if self._photo is None:
            r = next(s for s in spec["sources"] if s.get("rect"))["rect"]
            self._photo = Photo(Path(spec["generated"]["root"]) / r["image"], r["S"], r["u0"], r["v1"])
        return self._photo

    # -- findings
    def findings(self, spec: dict, ver: dict) -> list[dict]:
        units = {u["o"]["id"]: u for u in opening_units(spec)}
        cw_ids = {c["id"] for c in spec.get("cw_fields", [])}
        hosts = {it["id"]: it.get("host") for it in spec.get("linear", []) + spec.get("objects", [])}
        out = []
        bad_items = set()
        for r in csv_rows(ver.get("deviations_csv")):
            if r["ok"] == "True" or r["method"] not in ("params", "3D"):
                continue
            for item in r["item"].split("+"):
                bad_items.add(item)
                out.append({"source": "rows", "item": item, "host": hosts.get(item) or item, "quantity": r["quantity"], "method": r["method"],
                            "spec_m": float(r["spec_m"]), "model_m": None if r["model_m"] in ("", "None") else float(r["model_m"]),
                            "delta_cm": None if r["delta_cm"] in ("", "None") else float(r["delta_cm"]), "tol_cm": float(r["tol_cm"])})
        for r in csv_rows(ver.get("types_csv")):
            if r["status"] == "FAIL":
                bad_items.add(r["item"])
                out.append({"source": "types", "item": r["item"], "host": hosts.get(r["item"]) or r["item"], "quantity": r["what"],
                            "spec": r["spec"], "model": r["model"]})
        for pr in ver.get("problems", []):
            head = pr.split(":")[0].split(" ")[0]
            if head and head not in bad_items and (head in units or head in cw_ids or head in hosts or head.startswith("Z")) and "photo:" not in pr \
                    and "missing" in pr:
                bad_items.add(head)
                out.append({"source": "problem", "item": head, "host": hosts.get(head) or head, "quantity": pr[:160]})
        for e in ver.get("photo_edges", []):
            if abs(e["delta_cm"]) <= e["tol_cm"] + 1e-9:
                continue
            # opening / object / CW bar entries: side, model_projected, photo, range; linear: corner, v_model_projected, v_photo, s
            side = e.get("side") or ("bottom" if str(e.get("corner", "")).startswith("bottom") else "top")
            bar = e["edge"].startswith("photo: ")
            out.append({"source": "bar" if bar else "edge", "item": e["item"], "host": hosts.get(e["item"]) or e["item"], "edge": side, "name": e["edge"],
                        "model_photo": e.get("model_projected", e.get("v_model_projected")), "photo": e.get("photo", e.get("v_photo")),
                        "delta_cm": e["delta_cm"], "tol_cm": e["tol_cm"], "contrast": e.get("contrast"), "range": e.get("range") or e.get("s")})
        for e in ver.get("auto_edges", []):
            if e["status"] == "MIMO":
                out.append({"source": "auto", "item": e["item"], "host": e["item"], "edge": e["edge"], "name": f"{e['edge']} (automatická kontrola)",
                            "model_photo": e["model"], "photo": e["photo"], "delta_cm": -e["delta_cm"], "tol_cm": 3.0, "contrast": e["contrast"]})
            elif e["status"] in ("nejednoznačné", "spoločné ostenie", "zakrytá"):
                # the automatic check could not judge the edge (verify.auto_edge_check - the SAME decision as the loop:
                # reveal pair / shared jamb / hidden by the spec): never a measurement, reported with a crop (G2 kolo 2)
                ph_ = e.get("photo")
                out.append({"source": "auto", "item": e["item"], "host": e["item"], "edge": e["edge"], "parallax": True, "auto_status": e["status"],
                            "name": f"{e['edge']} (automatická kontrola: {e['status']})", "model_photo": e["model"], "photo": ph_,
                            "delta_cm": round((e["model"] - ph_) * 100, 1) if ph_ is not None else 0.0, "tol_cm": 3.0,
                            "contrast": e.get("contrast"), "note": e.get("note", "")})
        return out

    # -- classification
    def _unit_photo_rect(self, spec, u, cam):
        z = {zz["id"]: zz for zz in spec["zones"]}[u["zone"]]
        w = zone_w(z)
        s0, s1, v0, v1 = u["hole"]
        a = photo_of_spec(spec, cam, s0, v0, w)
        b = photo_of_spec(spec, cam, s1, v1, w)
        return [a[0], b[0], a[1], b[1]], w

    def classify(self, spec: dict, ver: dict, findings: list[dict], k: int, i7_dir: Path) -> list[dict]:
        from linear_verify import camera_of
        cam = camera_of(spec)
        units = {u["o"]["id"]: u for u in opening_units(spec)}
        tol = spec["tolerances"]["meranie"] if spec["tolerances"].get("meranie") else 0.05
        photo_tol = 0.03
        boxes = ver.get("proj_boxes", {})
        bad = {f["item"] for f in findings if f["source"] in ("rows", "types", "problem")}
        out = []
        # build: one fix per item (rows / types / missing)
        for f in findings:
            if f["source"] not in ("rows", "types", "problem"):
                continue
            item = f["item"]
            if item in units and units[item]["o"]["kind"] == "opening":         # glazed field: CW + its hole
                reb = [f"{item}.cw", f"{item}.otvor"] if (f.get("quantity", "").startswith("CW") or f["source"] == "types") else [f"{item}.otvor"]
            elif any(c["id"] == item for c in spec.get("cw_fields", [])):
                reb = [f"{item}.cw", f"{item}.otvor"]
            else:
                reb = [item]
            ev = {"quantity": f.get("quantity"), "spec_m": f.get("spec_m"), "model_m": f.get("model_m"), "delta_cm": f.get("delta_cm"),
                  "spec": f.get("spec"), "model": f.get("model")}
            out.append(dict(f, cls="build", reason="model ≠ spec (spätné čítanie)", evidence=ev,
                            fix={"key": ["rebuild", item], "rebuild": reb, "item": item,
                                 "error_cm": abs(f["delta_cm"]) if f.get("delta_cm") is not None else None}))
        # photo: group by host
        groups = {}
        for f in findings:
            if f["source"] in ("edge", "auto", "bar"):
                groups.setdefault(f["host"], []).append(f)
        ph = self.photo(spec)
        for host, fs in groups.items():
            if host in bad or any(f["item"] in bad for f in fs):
                for f in fs:
                    out.append(dict(f, cls="build", reason="odchýlka voči fotke čaká na opravu modelu (build) - posúdi sa v ďalšej iterácii",
                                    evidence={}, fix=None))
                continue
            u = units.get(host)
            if u is None or u["plane"] != REFERENCE_PLANE:
                for f in fs:
                    out.append(self._ambiguous(f, "hrana prvku, ktorý nie je otvor roviny fotky - automatická oprava nie je v I7", ph, i7_dir, k))
                continue
            out += self._classify_unit(spec, ver, u, units, fs, cam, ph, boxes, photo_tol, k, i7_dir)
        # an edge whose step runs on weakly beyond the jambs is accepted only when a CERTAIN edge of another opening of the
        # same zone lies on the same line (lintels / sills of one row; a stone joint in line with them is then expected)
        for g in out:
            if not g.get("unclear"):
                continue
            mates = [h for h in out if h is not g and h["cls"] == "spec" and h.get("edge") == g["edge"] and h.get("zone") == g.get("zone")
                     and h.get("photo_wide") is not None and abs(h["photo_wide"] - g["photo_wide"]) <= 0.015]
            if mates and g["edge"] in EDGES_H:
                g.update(cls="spec", fix=g.pop("pending_fix"), unclear=False,
                         reason=f"model = spec, hrana {g['edge']} je na fotke o {(g['photo_wide'] - g['model_photo']) * 100:+.1f} cm ďalej; leží na línii istej hrany "
                                f"{', '.join(h['item'] for h in mates)} ({mates[0]['photo_wide']:.3f}) - {g['reason']}")
            else:
                g.update(reason=f"{g['reason']}; bez potvrdenia na susednom otvore v tej istej línii - neopravené")
                g.pop("pending_fix", None)
        self._merge_rect_fixes(spec, out, units, k)
        for g in out:
            if g["source"] in ("edge", "auto", "bar") and g.get("model_photo") is not None:
                g["crop"] = str(self._crop(g, ph, i7_dir, k, spec, units, cam))
                if g.get("fix"):
                    g["fix"]["crop"] = g["crop"]
                    for x in g["fix"].get("edges", []):         # crop of every merged edge (N2)
                        if x["edge"] == g.get("edge"):
                            x["crop"] = g["crop"]
        return out

    def _merge_rect_fixes(self, spec: dict, out: list[dict], units: dict, k: int) -> None:
        """Several certain edges of ONE opening (e.g. left = masonry beyond the frame, top = stone edge above the soffit)
        are one correction of its rect: the loop applies one fix per target, so the edges are merged here (each edge from
        its own measurement; a rigid shift is never merged with single edges)."""
        by = {}
        for g in out:
            if g["cls"] == "spec" and g.get("fix") and g["fix"]["key"][0] == "rect" and g.get("evidence", {}).get("method") != "rigid_shift":
                by.setdefault(g["fix"]["item"], []).append(g)
        for oid, gs in by.items():
            fixes = {id(g["fix"]): g["fix"] for g in gs}
            if len(fixes) < 2:
                continue
            u = units[oid]
            old = list(u["o"]["rect"]["v"])
            new = list(old)
            for fx in fixes.values():
                for i, (a, b) in enumerate(zip(fx["rect_before"], fx["rect_after"])):
                    if abs(a - b) > 1e-6:
                        new[i] = b
            note = f"I7 iterácia {k}: hrany {', '.join(sorted({c['edge'] for fx in fixes.values() for c in fx['check']}))} z fotky (zlúčené)"
            merged = self._rect_fix(spec, u, new, k, note)
            merged["error_cm"] = max(fx["error_cm"] for fx in fixes.values())
            merged["check"] = [c for fx in fixes.values() for c in fx["check"]]
            # G2 kolo 2 (N2): a merged fix keeps every edge with its own before / after / measurement / reason / crop
            merged["edges"] = []
            for g in gs:
                for i, (a, b) in enumerate(zip(g["fix"]["rect_before"], g["fix"]["rect_after"])):
                    if abs(a - b) > 1e-6 and not any(x["edge"] == EDGE_NAMES[i] for x in merged["edges"]):
                        merged["edges"].append({"edge": EDGE_NAMES[i], "before": a, "after": b, "error_cm": g["fix"]["error_cm"],
                                                "photo": next((c["photo"] for c in g["fix"]["check"] if c["edge"] == EDGE_NAMES[i]), None),
                                                "reason": g["reason"], "method": (g.get("evidence") or {}).get("method")})
            for g in gs:
                g["fix"] = merged

    def _ambiguous(self, f, reason, ph, i7_dir, k, evidence=None, confirmed=False):
        return dict(f, cls="ambiguous", reason=reason, evidence=evidence or {}, fix=None, confirmed=confirmed)

    def _crop(self, f, ph: Photo, i7_dir: Path, k: int, spec: dict, units: dict, cam) -> Path:
        """Evidence crop: the opening (host) with a margin, its spec rect (yellow), the model edge (yellow), the edge
        measured in the wide window (green), the other candidates (red)."""
        e = f.get("edge") or "left"
        at, pos = f.get("model_photo"), f.get("photo_wide", f.get("photo"))
        u = units.get(f["host"]) or units.get(f["item"])
        rects = []
        if u is not None:
            r, _ = self._unit_photo_rect(spec, u, cam)
            box = (r[0] - 0.45, r[1] + 0.45, r[2] - 0.35, r[3] + 0.35)
            rects.append({"rect": r, "colour": (255, 230, 0)})
        else:
            rng = f.get("range") or [at - 0.5, at + 0.5]
            box = (at - 0.6, at + 0.6, min(rng) - 0.3, max(rng) + 0.3) if e in EDGES_V else (min(rng) - 0.3, max(rng) + 0.3, at - 0.4, at + 0.4)
        kind = "v" if e in EDGES_V else "h"
        marks = [{"kind": kind, "at": at, "label": f"model {at:.3f}", "colour": (255, 230, 0)}]
        if pos is not None:
            marks.append({"kind": kind, "at": pos, "label": f"foto {pos:.3f}", "colour": (0, 255, 0)})
        for c in (f.get("evidence") or {}).get("candidates", [])[:3]:
            if pos is None or abs(c["pos"] - pos) > 1e-3:
                marks.append({"kind": kind, "at": c["pos"], "label": f"kand. {c['pos']:.3f} (skok {c['step']:.0f})", "colour": (255, 80, 80)})
        name = f"{self.spec_id}_{f['item']}_{e}_{f['source']}_it{k}.png".replace(" ", "_")
        title = (f"{self.spec_id} {f['item']} {f.get('name', e)}: model - fotka {f.get('delta_cm', 0):+.1f} cm -> {CLS_SK.get(f['cls'], f['cls'])}"
                 f"{' (model potvrdený)' if f.get('confirmed') else ' (neoverené voči fotke)' if f.get('unverified') else ''}\n{f['reason'][:160]}")
        return crop_png(ph, box, marks, i7_dir / name, title, zoom=3, rects=rects)

    def _occluders(self, ver, u, edge, pos, rng_photo, exclude: set) -> list[str]:
        """Other elements of the model whose photo box covers or ends at the edge line (within OCCLUDE_TOL)."""
        out = []
        r0, r1 = min(rng_photo), max(rng_photo)
        for kind, bb in (ver.get("proj_boxes") or {}).items():
            for item, b in bb.items():
                if item in exclude:
                    continue
                if edge in EDGES_H:
                    along = (b[0], b[1])
                    across = (b[2], b[3])
                else:
                    along = (b[2], b[3])
                    across = (b[0], b[1])
                ov = min(r1, along[1]) - max(r0, along[0])
                if ov < 0.3 * (r1 - r0):
                    continue
                if across[0] - OCCLUDE_TOL <= pos <= across[1] + OCCLUDE_TOL:
                    out.append(item)
        return out

    def _classify_unit(self, spec, ver, u, units, fs, cam, ph, boxes, photo_tol, k, i7_dir) -> list[dict]:
        oid = u["o"]["id"]
        rect_ph, w = self._unit_photo_rect(spec, u, cam)
        own = {f["edge"]: f for f in fs if f["item"] == oid and f.get("edge") in EDGES_V + EDGES_H and f["source"] in ("edge", "auto")}
        for f in fs:                                        # explicit edges win over the automatic ones
            if f["item"] == oid and f["source"] == "edge" and f.get("edge") in own:
                own[f["edge"]] = f
        exclude = {oid, f"{oid}.cw"} | {it["id"] for it in spec.get("objects", []) if it.get("host") == oid}
        out, done = [], set()
        explicit = {e["edge"]: e for e in u["o"].get("photo_edges", [])}
        # rigid shift: two opposite edges (of the unit or of its hosted elements) off in the same direction
        for pair in (EDGES_V, EDGES_H):
            # two or more edges of this axis off (the opening's own and those of its sill / surround): try the pair. The
            # narrow search of verify may find another step near a moved edge, so the signs of the deltas do not have to
            # agree - the pair search itself decides (unique, both steps strong)
            sel = [f for f in fs if f.get("edge") in pair and f["source"] in ("edge", "auto") and not f.get("parallax")]
            if len(sel) < 2:
                continue
            edges = []
            for e in pair:
                rng = explicit[e]["range"] if e in explicit else ((u["hole"][2] + 0.15 * (u["hole"][3] - u["hole"][2]), u["hole"][3] - 0.15 * (u["hole"][3] - u["hole"][2]))
                                                                  if e in EDGES_V else (u["hole"][0] + 0.15 * (u["hole"][1] - u["hole"][0]), u["hole"][1] - 0.15 * (u["hole"][1] - u["hole"][0])))
                at = rect_ph[edge_index(e)]
                edges.append({"edge": e, "at": at, "rng": rng, "brighter": explicit.get(e, {}).get("brighter")})
            rs = rigid_shift(ph, edges, RIGID_HALF)
            ev = {"method": "rigid_shift", **rs, "edges": edges}
            if rs["status"] == "certain" and abs(rs["d"]) * 100 > photo_tol * 100:
                # refine each edge within +-2 cm of the shifted position
                fine = []
                for e in edges:
                    m = remeasure_edge(ph, e["edge"], e["at"] + rs["d"], e["rng"], e["brighter"], 0.02)
                    fine.append(m["pos"] if m.get("pos") is not None and abs(m["pos"] - e["at"] - rs["d"]) <= 0.02 else e["at"] + rs["d"])
                d_ph = float(np.mean([f_ - e["at"] for f_, e in zip(fine, edges)]))
                axis = edge_axis(pair[0])
                ref_at = edges[0]["at"]
                d_spec = spec_of_photo(cam, axis, ref_at + d_ph, w) - spec_of_photo(cam, axis, ref_at, w)
                new = list(u["o"]["rect"]["v"])
                i0, i1 = edge_index(pair[0]), edge_index(pair[1])
                new[i0] = round(new[i0] + d_spec, 4)
                new[i1] = round(new[i1] + d_spec, 4)
                ev.update(refined=[round(x, 4) for x in fine], shift_photo_m=round(d_ph, 4), shift_spec_m=round(d_spec, 4))
                fix = self._rect_fix(spec, u, new, k, f"I7 iterácia {k}: posun otvoru o {d_spec * 100:+.1f} cm ({pair[0]}+{pair[1]} ako dvojica hrán)")
                err = max(abs(f["delta_cm"]) for f in sel)
                fix["error_cm"] = round(abs(d_ph) * 100, 2)
                fix["check"] = [{"edge": e["edge"], "photo": round(f_, 4), "w": w} for e, f_ in zip(edges, fine)]
                for f in sel:
                    g = dict(f, cls="spec", reason=f"model = spec, otvor {oid} je na fotke posunutý o {d_spec * 100:+.1f} cm (dvojica hrán)",
                             evidence=ev, fix=fix, zone=u["zone"])
                    if f["item"] == oid and f.get("edge") in pair:
                        g["photo_wide"] = round(fine[pair.index(f["edge"])], 4)
                    out.append(g)
                    done.add(id(f))
                _ = err
            else:                                         # not a shift: every edge on its own (below), the pair test is kept as evidence
                for f in sel:
                    f["rigid"] = {kk: rs[kk] for kk in ("status", "d", "score", "second", "reason")}
        # single edges of the unit
        for f in fs:
            if id(f) in done:
                continue
            e = f.get("edge")
            if f["source"] == "bar":
                out.append(self._ambiguous(f, "lišta rastra CW mimo tolerancie - automatická oprava osí rastra pilota A nie je v I7 (vykázané)", ph, i7_dir, k))
                continue
            if f["item"] != oid:
                out.append(self._ambiguous(f, f"hrana odvodeného prvku {f['item']} (host {oid}) bez odchýlky otvoru - parametre odvodeného prvku sa automaticky neopravujú", ph, i7_dir, k))
                continue
            s0, s1, v0, v1 = u["rect"]
            hid = hidden_edge(u["o"]["rect"], e)
            if hid:                                         # the spec says the edge is not readable (G2 kolo 2, N1)
                out.append(self._ambiguous(dict(f, hidden=True), f"hrana zakrytá - ODHAD podľa spec, slučka ju nemeria ani neopravuje: {hid}", ph, i7_dir, k))
                continue
            jc = jamb_context(units, oid, e)              # one definition with verify.auto_edge_check
            if jc["shared"]:
                out.append(self._ambiguous(f, f"spoločné ostenie s {jc['shared']} (dotýkajúce sa otvory) - hrana nie je hrana murovaného otvoru, raster sa kontroluje ako lišta", ph, i7_dir, k))
                continue
            rng_spec = (v0 + 0.15 * (v1 - v0), v1 - 0.15 * (v1 - v0)) if e in EDGES_V else (s0 + 0.15 * (s1 - s0), s1 - 0.15 * (s1 - s0))
            if f["source"] == "edge" and e in explicit:
                rng_spec = tuple(explicit[e]["range"])
            at = rect_ph[edge_index(e)]
            free = jc["free"]                             # continuation test: a touching neighbour continues the edge by construction
            # parallax (G2 cause 1): the reveal of this edge faces the camera -> the masonry edge is the outer step of the pair
            # (masonry corner, frame ~rw inward), never just the step nearest to the model
            s_e = s0 if e == "left" else s1 if e == "right" else 0.5 * (s0 + s1)
            v_e = v1 if e == "top" else v0 if e == "bottom" else 0.5 * (v0 + v1)
            rw = visible_reveal(cam, e, s_e, v_e, w, unit_reveal(spec, u))
            if rw is not None and rw >= MIN_REVEAL_M:
                out.append(self._classify_reveal_edge(spec, ver, u, f, e, at, rw, rng_spec, rect_ph, free, w, cam, ph, exclude, photo_tol, k, i7_dir))
                continue
            m = remeasure_edge(ph, e, at, rng_spec, explicit.get(e, {}).get("brighter") if f["source"] == "edge" else None, EDGE_HALF, rect_ph, free)
            ev = {"method": "remeasure_edge", "window_cm": EDGE_HALF * 100, **{kk: m.get(kk) for kk in ("status", "pos", "step", "reason", "candidates", "thirds", "continuation")}}
            g = dict(f, photo_wide=m.get("pos"), range_photo=list(rng_spec))
            if m["status"] != "certain":
                out.append(self._ambiguous(g, m["reason"], ph, i7_dir, k, ev))
                continue
            d = m["pos"] - at                                  # photo - model (photo plane)
            if abs(d) <= photo_tol:
                out.append(self._ambiguous(g, f"premeraním v okne ±{EDGE_HALF * 100:.0f} cm je hrana v tolerancii ({d * 100:+.1f} cm) - úzke okno verify bolo nespoľahlivé; model ostáva",
                                           ph, i7_dir, k, ev, confirmed=True))
                continue
            occ = self._occluders(ver, u, e, m["pos"], rng_spec, exclude)
            if occ:
                out.append(self._ambiguous(g, f"hrana splýva s prvkom modelu / je ním zakrytá: {', '.join(occ[:3])}", ph, i7_dir, k, ev))
                continue
            if abs(d) > MAX_EDGE_SHIFT:
                out.append(self._ambiguous(g, f"hrana {d * 100:+.1f} cm od modelu (> {MAX_EDGE_SHIFT * 100:.0f} cm) - iný prvok fasády", ph, i7_dir, k, ev))
                continue
            if np.sign(d) != outward_sign(e):
                out.append(self._ambiguous(g, f"istá hrana leží vnútri otvoru ({d * 100:+.1f} cm) - rám / krídlo, nie hrana murovaného otvoru (spec číta murovaný otvor)",
                                           ph, i7_dir, k, ev))
                continue
            axis = edge_axis(e)
            new = list(u["o"]["rect"]["v"])
            new_v = round(spec_of_photo(cam, axis, m["pos"], w), 4)
            new[edge_index(e)] = new_v
            fix = self._rect_fix(spec, u, new, k, f"I7 iterácia {k}: hrana {e} z fotky {m['pos']:.3f} (skok {m['step']:.0f}, tretiny {m.get('thirds')})")
            fix["error_cm"] = round(abs(d) * 100, 2)
            fix["check"] = [{"edge": e, "photo": m["pos"], "w": w}]
            g = dict(g, cls="spec", reason=f"model = spec, hrana {e} je na fotke o {d * 100:+.1f} cm ďalej (istá: {m['reason']})", evidence=ev, fix=fix,
                     zone=u["zone"])
            if m.get("unclear"):                               # needs a sibling on the same line (resolved in classify)
                g.update(cls="ambiguous", unclear=True, pending_fix=fix, fix=None, reason=m["reason"])
            out.append(g)
        return out

    def _classify_reveal_edge(self, spec, ver, u, f, e, at, rw, rng_spec, rect_ph, free, w, cam, ph, exclude, photo_tol, k, i7_dir) -> dict:
        """One edge on the visible-reveal side: photo_measure.judge_reveal_edge - the SAME decision as the automatic check of
        verify (G2 kolo 2, cause 4). A fix only for a certain masonry step (MIMO) outside the spec opening within the
        expected reveal width; the model is 'confirmed' only by the pair (frame ~rw inward) or a certain outer step at the
        model - a lone step is 'neoverené' (unverified)."""
        j = judge_reveal_edge(ph, e, at, rw, rng_spec, rect_ph, free, photo_tol)
        m = j["m"]
        ev = {"method": "masonry_edge (paralaxa ostenia)", "rw_cm": round(rw * 100, 1), "judge": j["status"],
              **{kk: m.get(kk) for kk in ("status", "pos", "step", "frame", "role", "rw_measured", "reason", "outer", "inner", "thirds",
                                          "spacing_thirds", "continuation")}}
        g = dict(f, photo_wide=m.get("pos"), range_photo=list(rng_spec), reveal_cm=round(rw * 100, 1))
        if j["status"] in ("nejednoznačné", "nečitateľné"):
            return self._ambiguous(g, f"ostenie ~{rw * 100:.1f} cm: {m['reason']}", ph, i7_dir, k, ev)
        d = m["pos"] - at                                   # photo - model (photo plane)
        if j["status"] == "OK":
            if m["role"] == "outermost":
                return dict(self._ambiguous(g, f"jediný skok pri modeli ({d * 100:+.1f} cm), rám za ostením nečitateľný - murovaná hrana NEOVERENÁ "
                                               f"(nepotvrdzuje sa modelom)", ph, i7_dir, k, ev), unverified=True)
            return self._ambiguous(g, f"model na murovanej hrane ({d * 100:+.1f} cm): {m['reason']}", ph, i7_dir, k, ev, confirmed=True)
        occ = self._occluders(ver, u, e, m["pos"], rng_spec, exclude)
        if occ:
            return self._ambiguous(g, f"hrana ostenia splýva s prvkom modelu / je ním zakrytá: {', '.join(occ[:3])}", ph, i7_dir, k, ev)
        if abs(d) > MAX_EDGE_SHIFT + REVEAL_WIN[1] * rw:      # a frame read off by <= 8 cm + the reveal width
            return self._ambiguous(g, f"murovaná hrana {d * 100:+.1f} cm od modelu - ďalej ako {MAX_EDGE_SHIFT * 100:.0f} cm + ostenie "
                                      f"{REVEAL_WIN[1] * rw * 100:.1f} cm (iný prvok fasády)", ph, i7_dir, k, ev)
        if np.sign(d) != outward_sign(e):
            return self._ambiguous(g, f"istá hrana leží vnútri otvoru ({d * 100:+.1f} cm) - rám / krídlo, nie hrana murovaného otvoru", ph, i7_dir, k, ev)
        axis = edge_axis(e)
        new = list(u["o"]["rect"]["v"])
        new[edge_index(e)] = round(spec_of_photo(cam, axis, m["pos"], w), 4)
        fix = self._rect_fix(spec, u, new, k, f"I7 iterácia {k}: murovaná hrana {e} z fotky {m['pos']:.3f} (paralaxa: ostenie {rw * 100:.1f} cm "
                                              f"očakávané, {(m.get('rw_measured') or 0) * 100:.1f} cm namerané; skok {m['step']:.0f}, odstup od rámu "
                                              f"po tretinách {m.get('spacing_thirds')})")
        fix["error_cm"] = round(abs(d) * 100, 2)
        fix["check"] = [{"edge": e, "photo": m["pos"], "w": w}]
        g = dict(g, cls="spec", reason=f"model = spec na ráme, murovaná hrana {e} je na fotke o {d * 100:+.1f} cm ďalej od kamery ({m['reason']})",
                 evidence=ev, fix=fix, zone=u["zone"])
        if m.get("unclear"):                               # weak continuation beyond a free end: needs a sibling on the same line
            ratio = (m.get("continuation") or {}).get("ratio")
            g.update(cls="ambiguous", unclear=True, pending_fix=fix, fix=None,
                     reason=f"{g['reason']}; za koncom otvoru slabší skok ({ratio} x) - škára alebo širší predmet")
        return g

    def _rect_fix(self, spec, u, new_rect, k, note) -> dict:
        """Correction(s) of the rect of a unit: opening -> types openings.<id>.rect (+ transom_h kept absolute);
        glazed field -> types i6.cw_fields[id].rect (+ touching fields that share the corrected edge and were not measured)."""
        o = u["o"]
        src = f"I7.{k}"
        old = list(o["rect"]["v"])
        corrs = []
        types = self._types()
        if o["kind"] == "opening":
            path = ["i6", "cw_fields", {"id": o["id"]}, "rect"]
            before = sc.get(types, path)
            after = dict(copy.deepcopy(before), v=new_rect, src=src, conf="meranie", note=f"{note}; pôvodne {old} ({before.get('src')}, {before.get('conf')})")
            corrs.append({"target": "types", "path": path, "before": before, "after": after})
            # linked: touching glazed fields with the same top / bottom that is not measured on their own (conf below meranie)
            for i, (a, b) in enumerate(zip(old, new_rect)):
                if abs(a - b) < 1e-6 or i < 2:
                    continue
                for cw in types.get("i6", {}).get("cw_fields", []):
                    if cw["id"] == o["id"] or not rects_touch(cw["rect"]["v"], old) or abs(cw["rect"]["v"][i] - a) > 1e-3:
                        continue
                    if cw["rect"].get("conf") in ("meranie", "kota"):
                        continue
                    p2 = ["i6", "cw_fields", {"id": cw["id"]}, "rect"]
                    nv = list(cw["rect"]["v"])
                    nv[i] = b
                    corrs.append({"target": "types", "path": p2, "before": copy.deepcopy(cw["rect"]),
                                  "after": dict(copy.deepcopy(cw["rect"]), v=nv, src=src,
                                                note=f"{cw['rect'].get('note', '')}; I7 iterácia {k}: spoločná hrana s {o['id']} ({a} -> {b})"),
                                  "linked": o["id"]})
        else:
            path = ["openings", o["id"], "rect"]
            before = sc.get(types, path)
            base = copy.deepcopy(before) if before else {}
            after = dict(base, v=new_rect, src=src, conf="meranie", note=f"{note}; pôvodne {old} ({o['rect'].get('src')}, {o['rect'].get('conf')})")
            corrs.append({"target": "types", "path": path, "before": before, "after": after})
            th = sc.get(types, ["openings", o["id"], "transom_h"])
            dtop = new_rect[3] - old[3]
            if th and abs(dtop) > 1e-6:                    # transom bar stays where the photo shows it
                corrs.append({"target": "types", "path": ["openings", o["id"], "transom_h"], "before": copy.deepcopy(th),
                              "after": dict(copy.deepcopy(th), v=round(th["v"] + dtop, 4),
                                            note=f"{th.get('note', '')}; I7 iterácia {k}: vrch otvoru {old[3]} -> {new_rect[3]}, priečka ostáva na mieste")})
        return {"key": ["rect", o["id"]], "item": o["id"], "corrections": corrs, "rect_before": old, "rect_after": new_rect,
                "rebuild": [], "src": src}

    def _types(self) -> dict:
        from from_senec import TYPES_DIR
        t = json.loads((TYPES_DIR / f"{self.spec_id}.json").read_text(encoding="utf-8"))
        if self.types_hook:
            self.types_hook(t)
        corr = sc.load(self.corr_path if self.corr_path.exists() else None, self.spec_id)
        sc.apply(t, corr, "types")
        return t

    # -- fixes
    def apply_fixes(self, spec: dict, fixes: list[dict], corr: dict, k: int) -> list[dict]:
        applied = []
        corr["sources"] = [s for s in corr.get("sources", []) if s["id"] != f"I7.{k}" or s.get("file")]
        if not any(s["id"] == f"I7.{k}" for s in corr["sources"]):
            corr["sources"].append({"id": f"I7.{k}", "type": "iteration", "file": f"facade/corrections/{self.spec_id}.json",
                                    "note": f"I7 slučka, iterácia {k}: hrana premeraná na rektifikovanej fotke S1 (facade/photo_measure.py)"})
        own_paths = {json.dumps(c["path"]) for f in fixes if f["cls"] == "spec" for c in f["fix"]["corrections"] if not c.get("linked")}
        for f in fixes:
            if f["cls"] == "spec":                        # a linked edge that has its own fix in this batch: the own one wins
                f["fix"]["corrections"] = [c for c in f["fix"]["corrections"] if not c.get("linked") or json.dumps(c["path"]) not in own_paths]
        for f in fixes:
            fx = f["fix"]
            n = len(corr["corrections"])
            rec = {"id": f"{self.spec_id}.c{n + 1}", "cause": f["cls"], "item": fx["item"], "iteration": k,
                   "error_before_cm": fx.get("error_cm") if fx.get("error_cm") is not None else abs(f.get("delta_cm") or 0.0),
                   "finding": {kk: f.get(kk) for kk in ("source", "item", "edge", "name", "quantity", "delta_cm", "tol_cm")},
                   "reason": f["reason"], "crop": f.get("crop"), "corrections": [], "rebuild": fx.get("rebuild", []), "check": fx.get("check")}
            if f["cls"] == "build":
                rec["id"] = f"{self.spec_id}.b{k}.{fx['item']}"
                applied.append(rec)
                continue
            trial = copy.deepcopy(corr)
            ids = []
            for j, c in enumerate(fx["corrections"]):
                cid = f"{self.spec_id}.c{len(trial['corrections']) + 1}"
                trial["corrections"].append(dict(c, id=cid, src=fx["src"], conf="meranie", cause=f["cls"], iteration=k, status="active", reason=f["reason"],
                                                 evidence={kk: vv for kk, vv in (f.get("evidence") or {}).items() if kk != "edges"},
                                                 crop=f.get("crop"), finding=rec["finding"],
                                                 **({"edges": fx["edges"]} if fx.get("edges") and not c.get("linked") else {})))
                ids.append(cid)
            # the corrected spec must stay valid (rule: never build an invalid spec)
            try:
                sp = self._spec_with(trial)
                errs, _ = validate(sp)
            except Exception as exc:                      # noqa: BLE001 - adapter error = invalid correction
                errs = [str(exc)]
            if errs:
                f["cls"], f["reason"] = "ambiguous", f"oprava by spec zneplatnila: {errs[0][:120]}"
                continue
            corr["corrections"] = trial["corrections"]
            rec["corrections"] = ids
            rec["rect_before"], rec["rect_after"] = fx.get("rect_before"), fx.get("rect_after")
            applied.append(rec)
        return applied

    def _spec_with(self, corr: dict) -> dict:
        import tempfile
        from from_senec import build_spec
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "trial_corrections.json"
            sc.save(tmp, corr)
            return build_spec(self.spec_id, i6=True, corrections=tmp, types_hook=self.types_hook)

    def evaluate(self, spec: dict, ver: dict, fx: dict) -> dict:
        """Error of a fix after the rebuild: build -> rows of the item; spec -> |model edge (projected now) - photo edge
        measured before| for every corrected edge, and the photo checks of verify on that edge."""
        if fx["cause"] == "build":
            rows = [r for r in csv_rows(ver.get("deviations_csv")) if r["item"].split("+")[0] == fx["item"] and r["method"] in ("params", "3D")]
            bad = [r for r in rows if r["ok"] != "True"]
            errs = [abs(float(r["delta_cm"])) for r in bad if r["delta_cm"] not in ("", "None")]
            return {"error_cm": max(errs) if errs else 0.0, "in_tolerance": not bad, "note": f"{len(rows)} riadkov, {len(bad)} mimo tolerancie"}
        from linear_verify import camera_of
        cam = camera_of(spec)
        units = {u["o"]["id"]: u for u in opening_units(spec)}
        u = units.get(fx["item"])
        if u is None:
            return {"error_cm": None, "in_tolerance": False, "note": "otvor v spec chýba"}
        rect_ph, w = self._unit_photo_rect(spec, u, cam)
        edges = {c["edge"] for c in fx.get("check") or []}
        # the error after = what verify measures now on the corrected edge (its own narrow search around the new model
        # edge: explicit edge row, else the automatic check); the wide position of the loop is only the fallback
        meas = {}
        for e in ver.get("photo_edges", []):
            if e["item"] == fx["item"] and e.get("side") in edges:
                meas.setdefault(e["side"], abs(e["delta_cm"]))
        for e in ver.get("auto_edges", []):
            if e["item"] == fx["item"] and e["edge"] in edges and e["status"] != "nečitateľné":
                meas.setdefault(e["edge"], abs(e["delta_cm"]))
        errs = [meas.get(c["edge"], abs(rect_ph[edge_index(c["edge"])] - c["photo"]) * 100) for c in fx.get("check") or []]
        still = [f for f in self.findings(spec, ver) if f["item"] == fx["item"] and f["source"] in ("edge", "auto") and f.get("edge") in edges]
        return {"error_cm": round(max(errs), 2) if errs else None, "in_tolerance": not still,
                "per_edge": {c["edge"]: round(x, 2) for c, x in zip(fx.get("check") or [], errs)},
                "note": f"verify na opravenej hrane: {meas}" if not still else f"verify stále mimo: {[(f['edge'], f['delta_cm']) for f in still]}"}

    def photo_summary(self, ver: dict, findings: list[dict] | None = None) -> dict:
        """All photo checks of verify: explicit edges + CW bars (contract) and the automatic opening edges (readable).
        G2 kolo 2 (cause 3): edges that cannot be judged - automatic 'nejednoznačné' / shared jamb / hidden, or over
        tolerance and classified ambiguous by the loop - are NOT part of the metrics (n, median, max); they are counted
        apart (amb_*). A 'model potvrdený' edge counts with its wide re-measurement."""
        verdict = {}
        for f in findings or []:
            if f.get("source") in ("edge", "auto", "bar") and f.get("edge"):
                verdict[(f["item"], f["edge"])] = f
        meas, amb = [], []
        for e in ver.get("photo_edges", []):
            side = e.get("side") or ("bottom" if str(e.get("corner", "")).startswith("bottom") else "top")
            _sum_one(meas, amb, e["item"], side, abs(e["delta_cm"]), e["tol_cm"], verdict, e.get("edge"))
        for e in ver.get("auto_edges", []):
            if e["status"] == "nečitateľné":
                continue
            if e["status"] not in ("OK", "MIMO"):
                amb.append((e["item"], e["edge"], abs(e["delta_cm"]), e["status"]))
                continue
            _sum_one(meas, amb, e["item"], e["edge"], abs(e["delta_cm"]), 3.0, verdict, e["edge"])
        n_unread = sum(1 for e in ver.get("auto_edges", []) if e["status"] == "nečitateľné")
        return _summary(meas, amb, n_unread)

    def systemic(self, spec: dict, ver: dict) -> dict:
        samples = []
        for e in ver.get("photo_edges", []):
            side = e.get("side")
            samples.append({"host": e["item"].split("_")[0], "axis": "s" if side in EDGES_V else "v", "delta": e["delta_cm"] / 100})
        for e in ver.get("auto_edges", []):
            if e["status"] != "nečitateľné":
                samples.append({"host": e["item"], "axis": edge_axis(e["edge"]), "delta": -e["delta_cm"] / 100})
        return systemic_check(samples, 0.03)

    def apply_systemic(self, spec, axis, res, corr, k) -> dict | None:
        src = next(s for s in spec["sources"] if s.get("rect"))
        key = "u0" if axis == "s" else "v1"
        cur = src["rect"][key]
        # model - photo = median -> move the photo frame by the median: pos' = pos + median
        new = round(cur + res["offset_m"] if axis == "s" else cur + res["offset_m"], 6)
        cid = f"{self.spec_id}.c{len(corr['corrections']) + 1}"
        corr["corrections"].append({"id": cid, "target": "spec", "path": ["sources", {"id": src["id"]}, "rect", key], "before": cur, "after": new,
                                    "src": f"I7.{k}", "conf": "meranie", "cause": "systemic", "iteration": k, "status": "active", "evidence": res})
        self._photo = None
        return {"id": cid, "cause": "systemic", "item": f"sources[{src['id']}].rect.{key}", "iteration": k, "corrections": [cid],
                "error_before_cm": res["err_before_cm"], "rebuild": []}

    def report_ambiguous(self, findings: list[dict], corr: dict) -> bool:
        """Ambiguous findings of the final state -> simplifications in the corrections file (spec reports them)."""
        by_unit = {}
        for f in findings:
            if f["cls"] == "ambiguous" and not f.get("confirmed"):
                by_unit.setdefault(f["item"], []).append(f)
        new = [{"ref": f"openings[{item}] (I7: nejednoznačné hrany)",
                "text": "I7 slučka hranu neopravila: " + "; ".join(f"{f.get('edge')} {f.get('delta_cm'):+.1f} cm - {f['reason']}" for f in fs),
                "proposal": "prijať model; hranu odčítať z ďalšieho záberu alebo zamerania"} for item, fs in sorted(by_unit.items())]
        old = corr.get("simplifications", [])
        if json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True):
            return False
        corr["simplifications"] = new
        return True


EDGE_NAMES = ("left", "right", "bottom", "top")


def _opp(edge: str) -> str:
    return {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[edge]


# ---- pilot B ----------------------------------------------------------------------------------------------------------
class PilotB:
    """Pilot B: freestanding curtain walls read from ONE photo (verify_b). Photo checks: mullion bars (dark bar centre
    in the parapet row) and horizontal edges. A bar off the model is measured again in EVERY row of its field."""
    name = "B"
    spec_id = "HOF_B"

    def __init__(self, port: int = 19723, corr_path: Path | None = None, out_root: Path = OUT_B, project: str = PROJECT_B):
        self.port = port
        self.corr_path = Path(corr_path) if corr_path else sc.path_of(self.spec_id)
        self.out_root = out_root
        self.out_dir = out_root / self.spec_id
        self.project = project
        self._img = None

    def load_spec(self, corr: dict | None = None) -> dict:
        spec = json.loads((self.out_root / f"{self.spec_id}.facade-spec.json").read_text(encoding="utf-8"))
        corr = corr if corr is not None else sc.load(self.corr_path if self.corr_path.exists() else None, self.spec_id)
        done = set(spec.get("generated", {}).get("corrections_applied", []))
        a, s = sc.apply(spec, corr, "spec", skip_ids=done)
        sc.finish_spec(spec, corr, a, s)
        return spec

    def build(self, spec: dict, rebuild: set) -> dict:
        return fb.build(spec, self.project, self.port, self.out_root, verbose=False, rebuild=rebuild)

    def verify(self, spec: dict) -> dict:
        import verify_b
        return verify_b.verify(spec, self.project, self.port, self.out_root)

    @staticmethod
    def verify_pass(ver: dict) -> bool:
        return not ver["problems"] and ver["rows_ok"] == ver["rows"]

    def redraw(self, spec: dict) -> None:
        pass                                          # the pilot B overlay has no per-edge labels

    def _gray(self, spec):
        if self._img is None:
            import cv2
            path = Path(next(s for s in spec["sources"] if s["id"] == "S1")["rect_image"])
            rgb = cv2.imread(str(path), cv2.IMREAD_COLOR)
            self._img = (rgb, rgb.astype(np.float32).mean(axis=2))
        return self._img

    def findings(self, spec: dict, ver: dict) -> list[dict]:
        out = []
        for r in csv_rows(self.out_dir / "deviations_b.csv"):
            if r["ok"] != "True":
                out.append({"source": "rows", "item": r["item"], "host": r["item"], "quantity": r["quantity"], "method": r["method"],
                            "delta_cm": None if r["delta_cm"] in ("", "None") else float(r["delta_cm"]), "tol_cm": float(r["tol_cm"])})
        num = lambda x: float(x) if x not in ("", None, "None") else None
        for p in csv_rows(self.out_dir / "photo_edges_b.csv"):
            if p.get("sample") == "True":
                continue
            readable = p.get("readable") == "True"
            if readable and p.get("ok") == "True":
                continue
            out.append({"source": "bar" if readable else "unreadable", "item": p["item"], "host": p["item"], "edge": p["edge"], "name": p["edge"],
                        "model_px": float(p["model_px"]), "photo_px": num(p.get("photo_px")), "delta_cm": num(p.get("delta_cm")) or 0.0,
                        "tol_cm": float(p["tol_cm"]), "contrast": num(p.get("contrast")) or 0.0, **({"masked": p["masked"]} if p.get("masked") else {})})
        for e in ver.get("photo_edges", []):
            if not e["ok"]:
                out.append({"source": "hedge", "item": e["item"], "host": e["item"], "edge": e["edge"], "name": e["edge"],
                            "delta_cm": e.get("judged_cm") if e.get("judged_cm") is not None else 0.0, "tol_cm": 3.0,
                            "rule": e.get("rule"), "readable_n": e.get("readable_n")})
        return out

    def classify(self, spec: dict, ver: dict, findings: list[dict], k: int, i7_dir: Path) -> list[dict]:
        out = []
        cam = Cam(next(s for s in spec["sources"] if s["id"] == "S1")["camera"])
        cws = {c["id"]: c for c in spec["cw_fields"]}
        for f in findings:
            if f["source"] == "rows":
                reb = [f"{f['item']}.cw"] if f["item"] in cws else [f["item"]]
                out.append(dict(f, cls="build", reason="model ≠ spec (spätné čítanie)", evidence={"quantity": f["quantity"], "delta_cm": f["delta_cm"]},
                                fix={"key": ["rebuild", f["item"]], "rebuild": reb, "item": f["item"], "error_cm": abs(f["delta_cm"] or 0)}))
                continue
            if f["source"] == "hedge":
                out.append(dict(f, cls="ambiguous", reason=f"hrana pilota B (vodorovná / stĺp / stena) mimo tolerancie ({f.get('rule')}, čitateľné vzorky "
                                                           f"{f.get('readable_n')}) - automatická oprava nie je v I7 (vykázané)", evidence={}, fix=None))
                continue
            cw = cws.get(f["item"])
            kk = int(f["edge"].split()[1]) - 1
            res = self.bar_rows(spec, cw, kk, cam)
            g = dict(f, evidence=res)
            tol = f["tol_cm"] / 100
            v = judge_bar_rows(res["rows"], tol)
            g.update(cls=v["cls"], fix=None, reason=v["reason"], **{key_: v[key_] for key_ in ("confirmed", "unverified") if v.get(key_)})
            if v.get("photo_wide_px") is not None:
                g["photo_wide_px"] = v["photo_wide_px"]
            if v["cls"] == "spec":
                s_new = round(cam.s_of(g["photo_wide_px"], res["w"]), 4)
                path = ["cw_fields", {"id": cw["id"]}, "u_axes", "v", kk]
                g["fix"] = {"key": path, "item": cw["id"], "corrections": [{"target": "spec", "path": path, "before": cw["u_axes"]["v"][kk], "after": s_new}],
                            "rebuild": [], "src": f"I7.{k}", "error_cm": round(abs(v["median_m"]) * 100, 2),
                            "check": [{"kind": "bar", "px": g["photo_wide_px"], "w": res["w"]}]}
            g["crop"] = str(self._crop(spec, g, cam, i7_dir, k))
            out.append(g)
        return out

    def bar_rows(self, spec: dict, cw: dict, k: int, cam: "Cam") -> dict:
        """Dark bar of mullion k in every row of the field (between the transoms): best valley, second valley. Columns of
        the elements in front of the facade (railing posts, brackets - photo_b.front_masks, G2 kolo 2 N9) are
        neutralised; a row where such an element covers the model bar itself is not readable ('zakrytá')."""
        import photo_b
        _, g = self._gray(spec)
        masks = photo_b.front_masks(spec, photo_b.Cam(next(s_ for s_ in spec["sources"] if s_["id"] == "S1")["camera"]))
        w = cw["photo_w"]["v"] if cw.get("photo_w") else -cw["reveal"]["v"]
        s = cw["u_axes"]["v"][k]
        v0, v1 = cw["rect"]["v"][2], cw["rect"]["v"][3]
        vv = [v0] + cw.get("v_axes", {}).get("v", []) + [v1]
        louv = [(o["origin"]["v"][0], o["origin"]["v"][0] + o["params"]["A"]["v"], o["origin"]["v"][1], o["origin"]["v"][1] + o["params"]["ZZYZX"]["v"])
                for o in spec.get("objects", []) if o.get("kind") == "louvers"]
        rows = []
        for a, b in zip(vv[:-1], vv[1:]):
            xm, ya = cam.xy(s, b - 0.04, w)
            _, yb = cam.xy(s, a + 0.04, w)
            x0 = max(0, int(round(xm)) - 30)
            y0_, y1_ = max(0, int(ya)), min(g.shape[0], int(yb))
            if y1_ - y0_ < 3 or x0 + 61 > g.shape[1]:
                rows.append({"row": f"v {a:.2f}–{b:.2f}", "model_px": round(xm, 1), "pos_px": None, "depth": 0.0, "second_px": None,
                             "second_depth": 0.0, "readable": False, "double": False, "delta_m": None, "ratio": 0.0, "louvred": False})
                continue
            hits = photo_b.masks_in(masks, x0, x0 + 61, y0_, y1_)
            names = sorted({f"{m['item']} ({m['kind']})" for m in hits})
            if any(m["x0"] - 3 <= xm <= m["x1"] + 3 for m in hits):
                rows.append({"row": f"v {a:.2f}–{b:.2f}", "model_px": round(xm, 1), "pos_px": None, "depth": 0.0, "second_px": None,
                             "second_depth": 0.0, "readable": False, "double": False, "delta_m": None, "ratio": 0.0, "louvred": False,
                             "masked": "zakrytá: " + ", ".join(names)})
                continue
            prof = g[y0_:y1_, x0:x0 + 61].mean(axis=0)
            for m in hits:                                 # a post / bracket in front: its columns cannot be a mullion
                ia, ib = max(0, int(np.floor(m["x0"])) - x0 - 1), min(61, int(np.ceil(m["x1"])) - x0 + 2)
                prof[ia:ib] = prof.max()
            prof = np.convolve(prof, np.ones(3) / 3, mode="same")
            vals = []
            for i in range(4, len(prof) - 5):
                if prof[i] <= prof[i - 1] and prof[i] < prof[i + 1]:
                    l_, r_ = prof[max(0, i - 14):i - 3].max(), prof[i + 4:i + 15].max()
                    vals.append((x0 + i, float(min(l_, r_) - prof[i])))
            vals.sort(key=lambda t: -t[1])
            best = vals[0] if vals else (None, 0.0)
            second = next((v for v in vals[1:] if abs(v[0] - best[0]) >= 4), (None, 0.0)) if vals else (None, 0.0)
            ppm = cam.px_per_m(w)
            covered = any(lv[0] - 0.10 <= s <= lv[1] + 0.10 and min(b, lv[3]) - max(a, lv[2]) >= 0.5 * (b - a) for lv in louv)
            rows.append({"row": f"v {a:.2f}–{b:.2f}", "model_px": round(xm, 1), "pos_px": best[0], "depth": round(best[1], 1),
                         "ratio": round(best[1] / max(second[1], 0.1), 2) if best[0] is not None else 0.0, "louvred": covered,
                         "second_px": second[0], "second_depth": round(second[1], 1), "readable": best[1] >= 8.0,
                         "double": best[1] >= 8.0 and second[1] >= 0.6 * best[1] and second[0] is not None and abs(second[0] - best[0]) <= 15,
                         "delta_m": round((xm - best[0]) / ppm, 4) if best[0] is not None else None,
                         **({"masked": "maskované: " + ", ".join(names)} if names else {})})
        return {"w": w, "s": s, "rows": rows}

    def _crop(self, spec, f, cam, i7_dir: Path, k: int) -> Path:
        """Evidence crop of a mullion: the whole height of its field, model axis (yellow), per row the darkest bar
        (green = within tolerance of the model, red = off; a second valley of a double bar dashed), Δ per row."""
        from PIL import Image, ImageDraw
        from from_photo import ATTRIBUTION
        from photo_measure import _font
        rgb, _ = self._gray(spec)
        x = f["model_px"]
        cw = next(c for c in spec["cw_fields"] if c["id"] == f["item"])
        w = cw["photo_w"]["v"] if cw.get("photo_w") else -cw["reveal"]["v"]
        _, y0 = cam.xy(0, cw["rect"]["v"][3], w)
        _, y1 = cam.xy(0, cw["rect"]["v"][2], w)
        Z = 2
        X0, X1, Y0, Y1 = max(0, int(x) - 110), min(rgb.shape[1], int(x) + 110), max(0, int(y0) - 10), min(rgb.shape[0], int(y1) + 10)
        crop = Image.fromarray(rgb[Y0:Y1, X0:X1, ::-1]).resize(((X1 - X0) * Z, (Y1 - Y0) * Z), Image.NEAREST)
        title = [f"{f['item']} {f['edge']}: model - fotka (verify) {f['delta_cm']:+.1f} cm -> {CLS_SK.get(f['cls'], f['cls'])}"
                 + (" (model potvrdený)" if f.get("confirmed") else "")]
        words, line = f["reason"].split(), ""
        for wd in words:                                   # wrap the reason to the crop width
            if len(line) + len(wd) > 62:
                title.append(line)
                line = ""
            line += wd + " "
        title.append(line)
        title = title[:7]
        im = Image.new("RGB", (crop.size[0], crop.size[1] + 15 * (len(title) + 1) + 6), (0, 0, 0))
        im.paste(crop, (0, 0))
        dr = ImageDraw.Draw(im)
        fnt, small = _font(12), _font(10)
        dr.line([((x - X0) * Z, 0), ((x - X0) * Z, crop.size[1])], fill=(255, 230, 0), width=1)
        dr.text((4, 4), f"model {x:.0f} px", fill=(255, 230, 0), font=fnt, stroke_width=2, stroke_fill=(0, 0, 0))
        tol_m = f["tol_cm"] / 100
        for r in (f.get("evidence") or {}).get("rows", []):
            if r["pos_px"] is None:
                continue
            va, vb = (float(x_) for x_ in r["row"].split()[1].split("–"))
            ya, yb = cam.xy(0, vb, w)[1], cam.xy(0, va, w)[1]
            col = (0, 255, 0) if abs(r["delta_m"]) <= tol_m else (255, 70, 70)
            dr.line([((r["pos_px"] - X0) * Z, (ya - Y0) * Z), ((r["pos_px"] - X0) * Z, (yb - Y0) * Z)], fill=col, width=2)
            if r["double"]:
                xs = (r["second_px"] - X0) * Z
                for yy in range(int((ya - Y0) * Z), int((yb - Y0) * Z), 8):
                    dr.line([(xs, yy), (xs, yy + 4)], fill=(255, 150, 60), width=1)
            dr.text(((x - X0) * Z + 8, (0.5 * (ya + yb) - Y0) * Z), f"{r['delta_m'] * 100:+.1f} cm" + (" dvoj." if r["double"] else ""),
                    fill=col, font=fnt, stroke_width=2, stroke_fill=(0, 0, 0))
        for i, ln in enumerate(title):
            dr.text((4, crop.size[1] + 3 + 15 * i), ln, fill=(255, 255, 255), font=fnt)
        dr.text((4, crop.size[1] + 3 + 15 * len(title)), ATTRIBUTION[:95], fill=(180, 180, 180), font=small)
        path = i7_dir / f"HOF_B_{f['item']}_bar{f['edge'].split()[1]}_it{k}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)
        return path

    def apply_fixes(self, spec: dict, fixes: list[dict], corr: dict, k: int) -> list[dict]:
        applied = []
        if fixes and not any(s["id"] == f"I7.{k}" for s in corr.setdefault("sources", [])):
            corr["sources"].append({"id": f"I7.{k}", "type": "iteration", "file": "facade/corrections/HOF_B.json",
                                    "note": f"I7 slučka, iterácia {k}: lišta premeraná vo všetkých pásoch poľa na rektifikovanej fotke S1"})
        for f in fixes:
            fx = f["fix"]
            rec = {"id": f"HOF_B.b{k}.{fx['item']}", "cause": f["cls"], "item": fx["item"], "iteration": k, "error_before_cm": fx["error_cm"],
                   "finding": {kk: f.get(kk) for kk in ("source", "item", "edge", "delta_cm")}, "reason": f["reason"], "crop": f.get("crop"),
                   "corrections": [], "rebuild": fx.get("rebuild", []), "check": fx.get("check")}
            if f["cls"] == "spec":
                for c in fx["corrections"]:
                    cid = f"HOF_B.c{len(corr['corrections']) + 1}"
                    corr["corrections"].append(dict(c, id=cid, src=fx["src"], conf="meranie", cause="spec", iteration=k, status="active",
                                                    evidence=f.get("evidence"), crop=f.get("crop")))
                    rec["corrections"].append(cid)
                rec["id"] = rec["corrections"][0]
            applied.append(rec)
        return applied

    def evaluate(self, spec: dict, ver: dict, fx: dict) -> dict:
        if fx["cause"] == "build":
            rows = [r for r in csv_rows(self.out_dir / "deviations_b.csv") if r["item"] == fx["item"]]
            bad = [r for r in rows if r["ok"] != "True"]
            return {"error_cm": max([abs(float(r["delta_cm"])) for r in bad if r["delta_cm"] not in ("", "None")] or [0.0]), "in_tolerance": not bad}
        still = [f for f in self.findings(spec, ver) if f["item"] == fx["item"] and f["source"] == "bar"]
        return {"error_cm": max([abs(f["delta_cm"]) for f in still] or [0.0]), "in_tolerance": not still}

    def photo_summary(self, ver: dict, findings: list[dict] | None = None) -> dict:
        """Readable mullions + vertical / horizontal edges (judged value); bars the loop could not judge apart (cause 3)."""
        verdict = {(f["item"], f["edge"]): f for f in findings or [] if f.get("source") in ("bar", "unreadable") and f.get("edge")}
        rows = [p for p in csv_rows(self.out_dir / "photo_edges_b.csv") if p.get("sample") != "True"]
        meas, amb = [], []
        for p_ in rows:
            if p_.get("readable") == "True":
                _sum_one(meas, amb, p_["item"], p_["edge"], abs(float(p_["delta_cm"])), float(p_["tol_cm"]), verdict, p_["edge"])
        for e in ver.get("photo_edges", []):
            if e.get("readable_n", 1) == 0:
                amb.append((e["item"], e["edge"], 0.0, "nečitateľné"))
                continue
            meas.append((e["item"], e["edge"], abs(e.get("judged_cm", e["median_cm"])), 3.0, ""))
        return _summary(meas, amb, sum(1 for p_ in rows if p_.get("readable") != "True"))

    def systemic(self, spec: dict, ver: dict) -> dict:
        samples = [{"host": p["item"], "axis": "s", "delta": float(p["delta_cm"]) / 100} for p in csv_rows(self.out_dir / "photo_edges_b.csv")
                   if p.get("sample") != "True" and p.get("readable") == "True"]
        samples += [{"host": e["item"], "axis": "v", "delta": e["median_cm"] / 100} for e in ver.get("photo_edges", [])]
        return systemic_check(samples, 0.03)

    def apply_systemic(self, spec, axis, res, corr, k):
        return None                                   # pilot B: camera offsets are reported, not changed (scale ODHAD)

    def report_ambiguous(self, findings: list[dict], corr: dict) -> bool:
        return False                                  # pilot B lists them in report_G2.md and G1 (simplifications of the adapter)


def _sum_one(meas: list, amb: list, item: str, edge: str, d: float, tol: float, verdict: dict, name) -> None:
    """One photo check into the measured or the ambiguous bucket (the loop's verdict decides for a check over tolerance)."""
    f = verdict.get((item, edge))
    if d > tol + 1e-9 and f is not None and f.get("cls") == "ambiguous":
        if f.get("confirmed") and f.get("photo_wide") is not None and f.get("model_photo") is not None:
            meas.append((item, edge, abs(f["photo_wide"] - f["model_photo"]) * 100, tol, "model potvrdený (široké premeranie)"))
        else:
            amb.append((item, edge, d, (f.get("reason") or "")[:90]))
        return
    meas.append((item, edge, d, tol, ""))


def _summary(meas: list, amb: list, n_unread: int) -> dict:
    a = np.array([x[2] for x in meas]) if meas else np.zeros(0)
    worst = max(meas, key=lambda x: x[2]) if meas else None
    aw = max(amb, key=lambda x: x[2]) if amb else None
    return {"n": len(meas), "in_tol": sum(1 for x in meas if x[2] <= x[3] + 1e-9), "median_cm": round(float(np.median(a)), 2) if len(a) else None,
            "max_cm": round(float(a.max()), 2) if len(a) else None, "sum_cm": round(float(a.sum()), 1), "unreadable": n_unread,
            "max_at": f"{worst[0]} {worst[1]}" if worst else None,
            "amb_n": len(amb), "amb_max_cm": round(aw[2], 2) if aw else None, "amb_max_at": f"{aw[0]} {aw[1]} ({aw[3]})" if aw else None}


def write_verdicts(i7_dir: Path, findings: list[dict]) -> None:
    """Final verdicts of the loop on the photo edges (verify.load_verdicts colours the overlay: N10)."""
    edges = [{"item": f["item"], "edge": f.get("edge"), "cls": f["cls"], "confirmed": bool(f.get("confirmed")),
              "model_photo": f.get("model_photo"), "reason": (f.get("reason") or "")[:160]}
             for f in findings if f.get("source") in ("edge", "auto", "bar") and f.get("edge")]
    (i7_dir / "verdicts.json").write_text(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "edges": edges}, ensure_ascii=False, indent=1),
                                          encoding="utf-8")


def judge_bar_rows(rows: list[dict], tol: float) -> dict:
    """Pilot B: verdict on one mullion from its bands (bar_rows). G2 cause 3: the model is never confirmed by picking the
    candidate nearest to it. Only a band WITHOUT blinds (a valley in a band with blinds is the joint of two blind modules /
    a guide rail - circular evidence) whose darkest valley is clearly the strongest (ratio >= CONFIRM_RATIO x the next
    valley) counts: such a band near the model confirms it, off the model it is a measurement against it. A fix needs
    every readable band to agree (single bars, spread <= 1 cm) AND at least one clear band without blinds.
    Returns {cls: ambiguous | spec, confirmed, unverified, reason, photo_wide_px, median_m}."""
    readable = [r for r in rows if r["readable"]]
    single = [r for r in readable if not r["double"]]
    doubles = [r for r in readable if r["double"]]
    fmt = lambda rs: ", ".join(f"{r['row']}: {r['pos_px']} px ({r['delta_m'] * 100:+.1f} cm{', dvojitá ' + str(r['second_px']) + ' px' if r['double'] else ''}"
                               f"{', žalúzia' if r.get('louvred') else ''}{', ' + r['masked'] if r.get('masked') else ''}, pomer {r.get('ratio')})" for r in rs)
    hidden = [r for r in rows if not r["readable"] and str(r.get("masked", "")).startswith("zakrytá")]
    hid_txt = (f"; pás {', '.join(r['row'] for r in hidden)} zakrytý prvkom pred fasádou ({hidden[0]['masked'][9:]}) - nemeria sa") if hidden else ""
    clean = [r for r in readable if not r.get("louvred")]
    strong = [r for r in clean if r.get("ratio", 0.0) >= CONFIRM_RATIO]
    s_near = [r for r in strong if abs(r["delta_m"]) <= tol]
    s_far = [r for r in strong if abs(r["delta_m"]) > tol]
    weak_far = [r for r in clean if abs(r["delta_m"]) > tol]
    spec_ok = (len(single) >= 2 and not doubles and max(r["delta_m"] for r in single) - min(r["delta_m"] for r in single) <= 0.01
               and any(not r.get("louvred") and r.get("ratio", 0.0) >= CONFIRM_RATIO for r in single))
    if s_near and not s_far:
        return {"cls": "ambiguous", "confirmed": True, "photo_wide_px": round(float(np.median([r["pos_px"] for r in s_near])), 1),
                "reason": f"model potvrdený: v páse bez žalúzie jasne najtmavšia lišta (pomer ≥ {CONFIRM_RATIO}) pri modeli ({fmt(s_near)}); "
                          f"odchýlka verify je z iného pásu - spec sa nemení{hid_txt}"}
    if not readable:
        return {"cls": "ambiguous", "unverified": True, "reason": f"lišta nečitateľná vo všetkých pásoch poľa (kontrast < 8 alebo zakrytá) - neoverené{hid_txt}"}
    if not clean:
        return {"cls": "ambiguous", "unverified": True,
                "reason": f"nečitateľné, neoverené: lišta čitateľná len v pásoch so žalúziou ({fmt(readable)}) - škára / vodiaca lišta "
                          f"žalúzie, nie dôkaz stĺpika; model sa nepotvrdzuje"}
    if spec_ok:
        med = float(np.median([r["delta_m"] for r in single]))
        return {"cls": "spec", "median_m": med, "photo_wide_px": round(float(np.median([r["pos_px"] for r in single])), 1),
                "reason": f"model = spec, stĺpik je vo všetkých {len(single)} pásoch o {med * 100:+.1f} cm inde ({fmt(single)})"}
    if weak_far and not s_near:
        return {"cls": "ambiguous",
                "reason": f"nejednoznačné: v páse bez žalúzie je najtmavšia lišta mimo modelu ({fmt(weak_far)}) a pásy nesúhlasia; prvky pred "
                          f"fasádou (stĺpiky zábradlia, konzoly) sú maskované z modelu{hid_txt}; oprava potrebuje súhlas všetkých čitateľných "
                          f"pásov s jasnou lištou (pomer ≥ {CONFIRM_RATIO}) - neopravené, model neoverený"}
    if not strong:
        return {"cls": "ambiguous", "unverified": True,
                "reason": f"nečitateľné, neoverené: v páse bez žalúzie nie je jasne najsilnejšia lišta (pomer < {CONFIRM_RATIO}: {fmt(clean)}); "
                          f"výber kandidáta najbližšieho k modelu by bol potvrdenie modelu modelom"}
    return {"cls": "ambiguous", "reason": f"pásy poľa nesúhlasia / dvojitá lišta ({fmt(readable)}) - neopravené"}


class Cam:
    def __init__(self, c: dict):
        self.cx, self.cy, self.f, self.D0, self.s_c, self.v_c = (c[k] for k in ("cx", "cy", "f", "D0", "s_c", "v_c"))

    def xy(self, s, v, w):
        z = self.D0 - w
        return self.cx + (s - self.s_c) * self.f / z, self.cy - (v - self.v_c) * self.f / z

    def px_per_m(self, w):
        return self.f / (self.D0 - w)

    def s_of(self, x, w):
        return self.s_c + (x - self.cx) * (self.D0 - w) / self.f


# ---- G2 report --------------------------------------------------------------------------------------------------------
CLS_SK = {"build": "build (model ≠ spec)", "spec": "spec (odčítanie z fotky)", "ambiguous": "nejednoznačné", "systemic": "systematické"}


def _md_val(v):
    if isinstance(v, dict) and "v" in v:
        return str(v["v"])
    return str(v)


TEXT_PATHS = ("assumptions", "simplifications", "g1_proposals", "exceptions")
RECT_EDGES = ("left", "right", "bottom", "top")


def _is_text_change(c: dict) -> bool:
    return c["path"].split(".")[0].split("[")[0] in TEXT_PATHS or c["path"].endswith(".note")


def _edges_of(c: dict) -> list[dict]:
    """Per-edge rows of a correction (G2 kolo 2, N2): the stored 'edges' of a merged fix, else the changed indices of a
    rect value / the index of a single coordinate."""
    if c.get("edges"):
        return c["edges"]
    b, a = c.get("before"), c.get("after")
    bv = b.get("v") if isinstance(b, dict) else b
    av = a.get("v") if isinstance(a, dict) else a
    path = c.get("path", [])
    if isinstance(bv, list) and isinstance(av, list) and len(bv) == len(av) == 4 and "rect" in [x for x in path if isinstance(x, str)]:
        return [{"edge": RECT_EDGES[i], "before": x, "after": y} for i, (x, y) in enumerate(zip(bv, av)) if abs(x - y) > 1e-6]
    if len(path) >= 2 and path[-2] == "v" and isinstance(path[-1], int) and "rect" in [x for x in path if isinstance(x, str)]:
        return [{"edge": RECT_EDGES[path[-1]], "before": bv, "after": av}]
    return []


def _max_line(fs: list[dict]) -> tuple[str, str]:
    """(max over measured findings, max over ambiguous findings) with the edge that gives it (cause 3: apart)."""
    meas, amb = [], []
    for f in fs:
        if f.get("delta_cm") is None or f["source"] not in ("edge", "auto", "bar", "unreadable"):
            continue
        if f["cls"] == "ambiguous" and not f.get("confirmed"):
            amb.append(f)
        else:
            d = abs(f["delta_cm"])
            if f.get("confirmed") and f.get("photo_wide") is not None and f.get("model_photo") is not None:
                d = abs(f["photo_wide"] - f["model_photo"]) * 100
            meas.append((d, f))
    m = max(meas, key=lambda x: x[0]) if meas else None
    a = max(amb, key=lambda f: abs(f["delta_cm"])) if amb else None
    ms = f"{m[0]:.1f} cm ({m[1]['item']} {m[1].get('edge')}, {CLS_SK.get(m[1]['cls'], m[1]['cls'])}{', model potvrdený' if m[1].get('confirmed') else ''})" if m else "–"
    as_ = f"{abs(a['delta_cm']):.1f} cm ({a['item']} {a.get('edge')}: {(a.get('reason') or '')[:70]})" if a else "–"
    return ms, as_


def g2_report(pilot: str, specs: list[tuple[str, Path, Path]], path: Path, overlays: dict, extra: str = "", explain: dict | None = None) -> Path:
    """specs: [(spec id, i7 dir with iterations.json, corrections file)]. 'pred' = first findings of the first run in the
    history, 'po' = final findings of the last run; corrections = every entry of the corrections file (active and
    reverted) with its evidence, broken down per edge (N2); spec changes outside the corrections file from the diff of
    the spec against the previous run (N3); metrics without the ambiguous edges, those apart (cause 3)."""
    explain = explain or {}
    L = [f"# G2 – prijatie výsledku: pilot {pilot} (I7 slučka build → verify → oprava)", "",
         f"Vygenerované {time.strftime('%Y-%m-%d %H:%M')} skriptom `facade/iterate.py`. Brána G2 (ADR-001 kap. 3.2): architekt prijme "
         "výsledok alebo napíše pripomienku vetou. Opravy spec sú v `facade/corrections/<spec>.json` (zdroj `I7.<iterácia>`, istota meranie), "
         "každá s dôkazom (premeraná hrana, výrez), rozpísaná po hranách. Ručné zmeny vstupov adaptéra (typové súbory, šablóny) mimo "
         "corrections sú v sekcii „Zmeny spec mimo corrections“ (diff spec voči verzii predošlého behu). Metriky (medián, max) sú len z "
         "**meraných** hrán; nejednoznačné (spoločné ostenie, zakrytá hrana, dve hrany, slabá hrana) sú uvedené zvlášť. "
         "Výrez: žltý obdĺžnik = otvor spec v rovine fotky, žltá čiara = hrana modelu, zelená = hrana nameraná v širokom okne, "
         "červené = ďalší kandidáti. Overlay: popis fotokontroly biely = v tolerancii, červený = mimo tolerancie bez vysvetlenia, "
         "fialový = len nejednoznačné.", ""]
    if extra:
        L += [extra, ""]
    cnt = lambda fs, c: sum(1 for f in fs if f["cls"] == c)
    for sid, i7_dir, corr_path in specs:
        hist = json.loads((i7_dir / "iterations.json").read_text(encoding="utf-8"))["runs"]
        corr = sc.load(corr_path if corr_path.exists() else None, sid)
        first, final = hist[0]["first_findings"], hist[-1]["final_findings"]
        L += [f"## {sid}", "", "| Beh | Iterácie | Opravy | verify na konci | Čas |", "|---|---|---|---|---|"]
        for r in hist:
            L.append(f"| {r['run']} ({r['at']}) | {len(r['iterations'])} (max {r['max_iter']}) | {r['fixes_total']} | {'PASS' if r['verify_pass'] else 'FAIL'} | {r['seconds']} s |")
        L += ["", "| Stav | build | spec | nejednoznačné (z toho model potvrdený / neoverené) | max odchýlka model − fotka (merané hrany) | "
                  "max nejednoznačné (mimo metriky) |", "|---|---|---|---|---|---|"]
        for label, fs in (("pred (1. beh, iterácia 1)", first), ("po (posledný beh)", final)):
            amb = [f for f in fs if f["cls"] == "ambiguous"]
            mx, ax = _max_line(fs)
            L.append(f"| {label} | {cnt(fs, 'build')} | {cnt(fs, 'spec')} | {len(amb)} ({sum(1 for f in amb if f.get('confirmed'))} / "
                     f"{sum(1 for f in amb if f.get('unverified'))}) | {mx} | {ax} |")
        s0_ = hist[0]["iterations"][0].get("photo_summary")
        s1_ = hist[-1]["iterations"][-1].get("photo_summary_final") or hist[-1]["iterations"][-1].get("photo_summary")
        if s0_ and s1_:
            L += ["", "| Kontroly fotky (verify) | merané hrany / lišty | v tolerancii | medián abs(model − fotka) | max (hrana) | súčet | "
                      "nejednoznačné (mimo metriky): počet, max | nečitateľné |", "|---|---|---|---|---|---|---|---|"]
            for label, s_ in (("pred", s0_), ("po", s1_)):
                amb_txt = f"{s_.get('amb_n', '–')}, {s_.get('amb_max_cm')} cm ({s_.get('amb_max_at')})" if s_.get("amb_n") else (str(s_.get("amb_n", "–")))
                L.append(f"| {label} | {s_['n']} | {s_['in_tol']} | {s_['median_cm']} cm | {s_['max_cm']} cm ({s_.get('max_at', '–')}) | {s_['sum_cm']} cm | "
                         f"{amb_txt} | {s_['unreadable']} |")
            if "amb_n" not in s0_:
                L.append("\n_Riadok „pred“ je z behu pred dočistením (kolo 2): jeho metriky ešte obsahujú aj nejednoznačné hrany._")
        for line in explain.get(sid, []):
            L.append(f"\n**Vysvetlenie:** {line}")
        fixed_now = {(f["item"], f.get("edge")) for f in first if f["cls"] == "spec"} - {(f["item"], f.get("edge")) for f in final}
        if fixed_now:
            L.append(f"\nOpravené hrany (pred mimo tolerancie, po v tolerancii): {', '.join(f'{i} {e}' for i, e in sorted(fixed_now))}.")
        L += ["", "### Opravy spec z fotky (corrections) – po hranách", ""]
        cs = corr.get("corrections", [])
        if cs:
            L += ["| Id | Iterácia | Príčina | Cesta | Hrana | Pred | Po | Chyba pred → po (verify) | Stav | Dôkaz |", "|---|---|---|---|---|---|---|---|---|---|"]
            for c in cs:
                path_s = ".".join(str(x["id"]) if isinstance(x, dict) else str(x) for x in c["path"])
                ev = c.get("evidence") or {}
                why = c.get("reason") or ev.get("reason") or (c.get("finding") or {}).get("name") or ""
                stat = "aktívna" if c.get("status", "active") == "active" else f"vrátená ({c.get('reverted_reason', '')})"
                edges = _edges_of(c)
                if len(edges) <= 1:
                    e0 = edges[0] if edges else {}
                    L.append(f"| {c['id']} | {c.get('iteration')} | {c.get('cause')} | `{path_s}` | {e0.get('edge', '–')} | "
                             f"{e0.get('before', _md_val(c['before']))} | {e0.get('after', _md_val(c['after']))} | "
                             f"{c.get('error_before_cm', '–')} → {c.get('error_after_cm', '–')} cm | {stat} | {why[:160]}"
                             + (f" ([výrez]({Path(c['crop']).as_posix()}))" if c.get("crop") else "") + " |")
                else:
                    L.append(f"| {c['id']} | {c.get('iteration')} | {c.get('cause')} | `{path_s}` | **{len(edges)} hrany (zlúčená oprava)** | "
                             f"{_md_val(c['before'])} | {_md_val(c['after'])} | max {c.get('error_before_cm', '–')} → {c.get('error_after_cm', '–')} cm | {stat} | – |")
                    for x in edges:
                        crop = x.get("crop") or (c.get("crop") if x.get("edge") == (c.get("finding") or {}).get("edge") else None)
                        L.append(f"| ↳ | | | | {x['edge']} | {x.get('before')} | {x.get('after')} | "
                                 f"{x.get('error_cm', '–')} → {x.get('error_after_cm', '–')} cm | | {(x.get('reason') or '–')[:160]}"
                                 + (f" ([výrez]({Path(crop).as_posix()}))" if crop else " (výrez tejto hrany nebol uložený)") + " |")
        else:
            L.append("Žiadne.")
        # N3: spec changes outside the corrections file (types file, templates, adapter), from the diff against the previous run
        L += ["", "### Zmeny spec mimo corrections (diff voči verzii predošlého behu)", ""]
        rows_, n_text = [], 0
        for r in hist:
            sch = r.get("spec_changes") or {}
            for c in sch.get("changes", []):
                if c.get("via") != "ručne":
                    continue
                if _is_text_change(c):
                    n_text += 1
                    continue
                rows_.append((r["run"], sch.get("against"), c))
        if rows_:
            L += ["| Beh | Voči | Cesta | Pred | Po | Zdroj / istota | Poznámka |", "|---|---|---|---|---|---|---|"]
            for run, against, c in rows_:
                part = (f" (hrany {', '.join(RECT_EDGES[i] for i in c['manual_idx'] if i < 4)} ručne; ostatné z corrections)"
                        if c.get("manual_idx") and c["path"].endswith(".rect") else "")
                L.append(f"| {run} | {against} | `{c['path']}`{part} | {str(c['before'])[:70]} | {str(c['after'])[:70]} | "
                         f"{c.get('src') or '–'} / {c.get('conf') or '–'}{' (pred ' + str(c.get('conf_before')) + ')' if c.get('conf_before') and c.get('conf_before') != c.get('conf') else ''} | "
                         f"{(c.get('note') or '')[:140]} |")
        else:
            L.append("Žiadne (alebo bez uloženej verzie predošlého behu)." if not any((r.get("spec_changes") or {}).get("against") for r in hist)
                     else "Žiadne.")
        if n_text:
            L.append(f"\nZmenené texty (predpoklady, zjednodušenia, návrhy G1, poznámky): {n_text} – pozri G1.")
        builds = [fx for r in hist for it in r["iterations"] for fx in it["fixes"] if fx["cause"] == "build"]
        if builds:
            L += ["", "### Opravy modelu (build)", "", "| Beh/iterácia | Prvok | Prestavané | Riadky pred | Po |", "|---|---|---|---|---|"]
            evs = {e["id"]: e for r in hist for it in r["iterations"] for e in it["evaluated"]}
            for fx in builds:
                e = evs.get(fx["id"], {})
                L.append(f"| {fx['iteration']} | {fx['item']} | {', '.join(fx['rebuild'])} | {fx['error_before_cm']} cm | {e.get('after_cm', '–')} cm ({e.get('note', '')}) |")
        L += ["", "### Čo ostáva (nejednoznačné / výnimky) – posledný beh", ""]
        amb = [f for f in final if f["cls"] not in ("spec", "build")]
        if amb:
            L += ["| Prvok | Hrana | Model − fotka | Príčina | Výrez |", "|---|---|---|---|---|"]
            for f in amb:
                L.append(f"| {f['item']} | {f.get('name') or f.get('edge')} | {f.get('delta_cm', 0):+.1f} cm | "
                         f"{'**model potvrdený** – ' if f.get('confirmed') else '**neoverené** (nečitateľné, model sa nepotvrdzuje sám sebou) – ' if f.get('unverified') else '**zakrytá (ODHAD)** – ' if f.get('hidden') else ''}{f['reason'][:220]} | "
                         + (f"[png]({Path(f['crop']).as_posix()})" if f.get("crop") else "–") + " |")
        else:
            L.append("Nič.")
        left = [f for f in final if f["cls"] in ("build", "spec")]
        if left:
            L += ["", "**Neopravené po poslednom behu:** " + ", ".join(f"{f['item']} {f.get('edge') or f.get('quantity')}" for f in left)]
        sysd = (hist[-1]["iterations"][-1].get("systemic") or {})
        L += ["", "### Systematické posuny (mierka / kamera / rám fotky)", "",
              "| Os | Hostiteľov | Medián model − fotka | Rovnaké znamienko | Spustené | Prijaté |", "|---|---|---|---|---|---|"]
        for axis, r in sysd.items():
            L.append(f"| {axis} | {r['hosts']} | {r.get('median_cm')} cm | {r.get('same_sign', '–')} | {'áno' if r['triggered'] else 'nie'} | "
                     f"{'áno' if r.get('accepted') else 'nie'} |")
        if sid in overlays:
            L += ["", "Overlay po slučke: " + ", ".join(f"`{p}`" for p in overlays[sid])]
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ---- CLI --------------------------------------------------------------------------------------------------------------
def run_a(houses: list[str], port: int, max_iter: int, log=print) -> list[dict]:
    ac = TapirClient(port=port, timeout=30)
    ac.guard_project(PROJECT_A)
    logs = []
    for h in houses:
        logs.append(Loop(PilotA(h, port), max_iter, log).run())
    write_g2_a()
    return logs


G2_A_NOTE = (
    "**Konvencia spec po kontrole G2 (2026-10-19):** všetky štyri hrany otvoru sú hrany **murovaného otvoru**. Na šikmom zábere je "
    "ostenie na strane od kamery viditeľné (M015, M062: kamera vpravo → ľavé ostenia, horné podhľady): murovaná hrana je vonkajší skok "
    "páru (murovaná hrana, rám o viditeľnú šírku ostenia ďalej), nie skok najbližší k modelu (`photo_measure.masonry_edge`). Automatická "
    "kontrola dá pri druhom skoku v očakávanej vzdialenosti ostenia stav `nejednoznačné`, nikdy OK. Výrezy: žltá = hrana modelu, zelená = "
    "nameraná murovaná hrana.\n\n"
    "**M062 – výklad O1–O3:** ľavá hrana O1 je čítaná na **rám** (ostenie je viditeľné len v hornej tretine, dolu ho zakrýva kmeň stromu – "
    "slučka ho neprečíta isto, nejednoznačné s výrezom); O2/O3 majú spoločné ostenia, vrch zakrýva markíza (dve hrany). Výklad teda **nie je** "
    "v konvencii murovaného otvoru – vykázané.\n\n"
    "**Podlažia (pilot A):** jedna sada pre projekt: 1.NP ±0,000 (úroveň morfového modelu, nemenená), 2.NP +1,950, 3.NP (podkrovie) +5,550 "
    "(`facade/adapters/senec_types/_project_stories.json`); každý prvok má podlažie podľa svojej päty, steny sú vrchom viazané na podlažie; "
    "morfový model pred / po zmene podlaží Δ 0,0 cm (`private-tests/morph_guard.py`).\n\n"
    "**Dočistenie po 2. kontrole G2 (2026-10-20, `kontrola_G2_kolo2.md`):** párová logika murovanej hrany prijme pár len pri "
    "viditeľnom ostení ≥ 3 cm (očakávanom aj nameranom), murovanom skoku ≥ 12 (prah kontrastu) a hrane, ktorá nepokračuje mimo otvoru "
    "(tabuľa, markíza, parapetný plech) – inak `nejednoznačné`. Automatická kontrola verify a slučka rozhodujú o hrane s viditeľným "
    "ostením **jednou funkciou** (`photo_measure.judge_reveal_edge`) a spoločné ostenie / zakrytú hranu označujú rovnako "
    "(`geom.jamb_context`, `rect.edge_conf`). Hrana, ktorú spec vyhlási za zakrytú (ODHAD), sa nemeria ani neopravuje.")

G2_EXPLAIN_A = {
    "M015_L1": [
        "vrch O1 / O1b 2,8211 je **ODHAD** (`rect.edge_conf.top`): nadpražie zakrýva tabuľa kvetinárstva, hrana 2,80–2,82 na fotke je "
        "spodok tabule, nie kameň (kontrola G2 kolo 2, N1 / č. 15). Pôvodná oprava c6 menila ľavú hranu aj vrch naraz – od kola 2 "
        "mení len ľavú hranu (0,9158), vrch je ručná zmena typového súboru s istotou ODHAD (sekcia „Zmeny spec mimo corrections“), "
        "c7 (vrch O1b viazaný na O1) je vrátená. Ostáva ako dolná hranica: rám je na 2,76, nad ním po spodok tabule tmavý pás.",
        "ľavá hrana O2 (oprava c5, 3,3229): murovaný skok má kontrast 7,7 < 12 – od kola 2 je automaticky **nejednoznačná** (slabá "
        "hrana), poloha sa opiera o nezávislé odčítanie kontrolóra 3,32 (kontrola G2, č. 1, odchýlka 0,8 cm); spec sa nemení.",
    ],
    "M062_L2": [
        "max odchýlka 6,2 → 19,0 cm v behu pred dočistením bola **ľavá hrana O2 = spoločné ostenie** výkladu O1/O2 (nie chyba modelu); "
        "od kola 2 je spoločné ostenie mimo metriky. Ľavá hrana O6: automatická kontrola aj slučka teraz rozhodujú tou istou funkciou – "
        "murovaný skok 11,6 < 12 → nejednoznačné (predtým „MIMO 5,4“ v kontrole a zároveň „model potvrdený“ v slučke, N8).",
        "O4: nadsvetlík bez zvislej priečky (`transom_cols` = 1 → GDL `vgn_02`, overené 3D geometriou: zvislá priečka v strede zmizla); "
        "vodorovná priečka je v strede nadsvetlíka (v 2,51; fotka 2,52) – parameter `gs_horiz_div_height_02` na jej polohu nemá vplyv "
        "(overené 0,405 / 0,5 → priečka ostáva v 2,51), preto ho spec už neposiela.",
    ],
    "M016_L2": [
        "max odchýlka 5,0 → 6,1 cm (beh po ručnej zmene O6): **spodok O6 −6,1 cm** – automatická kontrola vybrala kandidáta 6,751 = tmavý "
        "pás (truhlík / päta zábradlia) pod bielymi osteniami; ostenia končia na ≈ 6,81 = projekcia prahu 6,78 (ďalší kandidáti 6,793 "
        "a 6,827). Slučka hranu klasifikuje ako dve hrany (nejednoznačné), od kola 2 je mimo metriky. Model (prah na BK2) je podľa "
        "kontrolóra správny.",
        "ručné zmeny O6, A1, Z2, BK2 a R2 (G2 nálezy 3–4, opravné kolo 2d612d1) sú v sekcii „Zmeny spec mimo corrections“ – diff voči "
        "spec z commitu 175bfe2.",
    ],
}


def write_g2_a(houses=("M015_L1", "M062_L2", "M016_L2")) -> Path:
    have = [h for h in houses if (OUT_A / h / "i7" / "iterations.json").exists()]
    return g2_report("A (Senec, Lichnerova)", [(h, OUT_A / h / "i7", sc.path_of(h)) for h in have], OUT_A / "report_G2.md",
                     {h: [str(OUT_A / h / f"overlay_{h}.png"), str(OUT_A / h / f"overlay_edges_{h}.png")] for h in have}, extra=G2_A_NOTE,
                     explain=G2_EXPLAIN_A)


def run_b(port: int, max_iter: int, log=print) -> dict:
    ac = TapirClient(port=port, timeout=30)
    ac.guard_project(PROJECT_B)
    lg = Loop(PilotB(port), max_iter, log).run()
    write_g2_b()
    return lg


def write_g2_b() -> Path:
    from from_photo import ATTRIBUTION
    return g2_report("B (HAW Hof, budova B)", [("HOF_B", OUT_B / "HOF_B" / "i7", sc.path_of("HOF_B"))], OUT_B / "report_G2.md",
                     {"HOF_B": [str(OUT_B / "HOF_B" / n) for n in ("overlay_HOF_B.png", "overlay_detail_HOF_B.png", "overlay_detail_bez_zaluzii_HOF_B.png")]},
                     extra=f"Výrezy fotky: {ATTRIBUTION}. Mierka fotky je ODHAD (zábradlie 1,05 m) – odchýlky voči fotke sú relatívne.\n\n"
                           "**Po kontrole G2 (2026-10-19):** „model potvrdený“ len vtedy, keď je v páse **bez žalúzie** lišta jasne najtmavšia "
                           f"(≥ {CONFIRM_RATIO}× ďalší kandidát) pri modeli; pásy so žalúziou merajú škáry / vodiace lišty žalúzií (kruhový dôkaz) a "
                           "nepotvrdzujú nič; inak **neoverené** (nečitateľné). Západná svetlá plocha = plná stena koncového poľa `STENA_Z` "
                           "(hĺbka, hrúbka, materiál ODHAD); stĺp `STLP_L` má os v hĺbke, v ktorej sa odčítali jeho okraje (hĺbka NEOVERENÁ). "
                           "Zábradlie: výnimka R1 (favorit s vodorovnými tyčami pripraví architekt).\n\n"
                           "**Dočistenie po 2. kontrole G2 (2026-10-20):** zvislé hrany (stĺp, stena) sa merajú v 4 pásoch po výške s ľubovoľnou "
                           "polaritou a každá vzorka sa hodnotí samostatne (nikdy medián z 2 vzoriek); stĺpiky zábradlí a konzoly pred LOP sú "
                           "maskované z modelu (`photo_b.front_masks`), v parapetnom páse sa nemerajú ako lišty LOP.",
                     explain=G2_EXPLAIN_B)


G2_EXPLAIN_B = {
    "HOF_B": [
        "stĺp `STLP_L` (N6 / č. 7): dolná vzorka −4,69 cm nebola okraj stĺpa, ale hrana v korune stromu za ním – meranie hľadalo "
        "skok pevnej polarity (stĺp svetlejší ako pozadie), no v hornom páse je stĺp tmavší ako obloha a dole svetlejší / tmavší ako "
        "lístie. Rovnaká chyba bola v odčítaní adaptéra (horné vzorky +1,5 / −2,4 cm boli slabé skoky vedľa modelu). Oprava: obe "
        "polarity, 4 pásy, každá vzorka zvlášť; adaptér číta okraje rovnako → stĺp o 0,9 cm vľavo, Ø 0,416 → 0,415 m (datum osi s "
        "fasády ostáva pevný). Perspektíva valca ani sokel príčinou neboli (šírka stĺpa na fotke je po celej výške ~108 px).",
        "CW_2NP_05 st. 1 (N9): lišta 4993 px v parapetnom páse je stĺpik zábradlia ochodze pred LOP (`KONZ_H_18`, 4987–5010 px) – "
        "je maskovaný; dôvod v tabuľke už nehovorí o lište LOP.",
    ],
}



def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pilot", choices=["A", "B"])
    ap.add_argument("--houses", nargs="*", default=["M015_L1", "M062_L2", "M016_L2"])
    ap.add_argument("--port", type=int, default=19723)
    ap.add_argument("--max-iter", type=int, default=MAX_ITER)
    args = ap.parse_args(argv)
    if args.pilot == "A":
        logs = run_a(args.houses, args.port, args.max_iter)
    else:
        logs = [run_b(args.port, args.max_iter)]
    for lg in logs:
        print(f"{lg['spec']}: {lg['fixes_total']} opráv v {len(lg['iterations'])} iteráciách, verify {'PASS' if lg['verify_pass'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""spec_diff: what changed in a facade spec between two runs of the I7 loop (G2 kolo 2, N3 / cause 2).

The G2 report used to list only the entries of corrections/<id>.json. Changes made by hand in the inputs of the adapter
(types file, templates, GDL catalogue) were invisible there (M016: O6, A1, Z2, BK2, R2). The loop now keeps the final
spec of every run (<out>/<id>/i7/spec_snapshot.json) and diffs the spec of the next run against it; every changed value
is listed with its new source:
  via 'corrections'  the new value comes from the corrections file (src I7.<k>) - also in the corrections table
  via 'ručne'        anything else (types file, template, adapter): a manual change outside corrections/*.json

Values {v, src, conf, note} compare by v; lists of items with an 'id' match by id; 'generated' is ignored.
"""
from __future__ import annotations

import json
from pathlib import Path

IGNORE = {"generated"}


def _fmt_path(path: list) -> str:
    out = ""
    for p in path:
        if isinstance(p, int):
            out += f"[{p}]"
        elif isinstance(p, dict):
            out += f"[{p['id']}]"
        else:
            out += ("." if out else "") + str(p)
    return out


def _is_value(x) -> bool:
    return isinstance(x, dict) and "v" in x and ("src" in x or "conf" in x)


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def diff(a, b, path: list | None = None) -> list[dict]:
    """Changed leaves between spec a (before) and b (after): [{path, before, after, src, conf, note}]."""
    path = path or []
    out: list[dict] = []
    if _is_value(a) or _is_value(b):
        av = a.get("v") if isinstance(a, dict) else a
        bv = b.get("v") if isinstance(b, dict) else b
        extra_a = {k: v for k, v in (a or {}).items() if k not in ("v", "src", "conf", "note")} if isinstance(a, dict) else {}
        extra_b = {k: v for k, v in (b or {}).items() if k not in ("v", "src", "conf", "note")} if isinstance(b, dict) else {}
        conf_changed = isinstance(a, dict) and isinstance(b, dict) and a.get("conf") != b.get("conf")
        if not _same(av, bv) or not _same(extra_a, extra_b) or conf_changed:
            out.append({"path": _fmt_path(path), "before": av, "after": bv,
                        "src": (b or {}).get("src") if isinstance(b, dict) else None, "conf": (b or {}).get("conf") if isinstance(b, dict) else None,
                        "conf_before": (a or {}).get("conf") if isinstance(a, dict) else None,
                        "note": ((b or {}).get("note") or "")[:300] if isinstance(b, dict) else "",
                        **({"extra_before": extra_a, "extra_after": extra_b} if not _same(extra_a, extra_b) else {})})
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if not path and k in IGNORE:
                continue
            if k not in a:
                out.append({"path": _fmt_path(path + [k]), "before": None, "after": _short(b[k]), "src": _src(b[k]), "conf": None, "note": "pridané"})
            elif k not in b:
                out.append({"path": _fmt_path(path + [k]), "before": _short(a[k]), "after": None, "src": None, "conf": None, "note": "odobraté"})
            else:
                out += diff(a[k], b[k], path + [k])
        return out
    if isinstance(a, list) and isinstance(b, list) and all(isinstance(x, dict) and "id" in x for x in a + b) and (a or b):
        ia, ib = {x["id"]: x for x in a}, {x["id"]: x for x in b}
        for k in list(ia) + [k for k in ib if k not in ia]:
            if k not in ib:
                out.append({"path": _fmt_path(path + [{"id": k}]), "before": "(prvok)", "after": None, "src": None, "conf": None, "note": "odobraté"})
            elif k not in ia:
                out.append({"path": _fmt_path(path + [{"id": k}]), "before": None, "after": "(prvok)", "src": _src(ib[k]), "conf": None, "note": "pridané"})
            else:
                out += diff(ia[k], ib[k], path + [{"id": k}])
        return out
    if not _same(a, b):
        out.append({"path": _fmt_path(path), "before": _short(a), "after": _short(b), "src": None, "conf": None, "note": ""})
    return out


def _short(x):
    s = json.dumps(x, ensure_ascii=False)
    return x if len(s) <= 160 else s[:157] + "..."


def _src(x):
    return x.get("src") if isinstance(x, dict) else None


def _explained(corrections: list[dict]) -> dict:
    """(item id, field) -> indices of a list value set by ACTIVE corrections ('all' for the whole value)."""
    out: dict = {}
    for c in corrections or []:
        if c.get("status", "active") != "active":
            continue
        path = c.get("path", [])
        item = next((x["id"] for x in path if isinstance(x, dict)), None)
        if item is None and len(path) >= 2 and path[0] == "openings":
            item = path[1]
        names = [x for x in path if isinstance(x, str)]
        field = next((n for n in names if n in ("rect", "transom_h", "u_axes", "v_axes", "s", "v", "origin")), names[-1] if names else None)
        idx = path[-1] if isinstance(path[-1], int) else "all"
        cur = out.setdefault((item, field), set())
        cur.add(idx)
    return out


def classify(changes: list[dict], corrections: list[dict] | None = None) -> list[dict]:
    """via 'corrections' when the new value carries a src of the I7 loop or every changed index is set by an active
    correction, 'corrections + ručne' when only some are, else 'ručne'."""
    exp = _explained(corrections or [])
    for c in changes:
        if str(c.get("src") or "").startswith("I7"):
            c["via"] = "corrections"
            continue
        head = c["path"].split(".")
        item = head[0][head[0].find("[") + 1:-1] if "[" in head[0] else None
        field = head[1] if len(head) > 1 else None
        got = exp.get((item, field))
        if got and isinstance(c.get("before"), list) and isinstance(c.get("after"), list) and len(c["before"]) == len(c["after"]):
            changed = {i for i, (x, y) in enumerate(zip(c["before"], c["after"])) if x != y}
            if "all" in got or changed <= got:
                c["via"] = "corrections"
            elif changed & got:
                c["via"] = "ručne"
                c["by_corrections"] = sorted(changed & got)
                c["manual_idx"] = sorted(changed - got)
            else:
                c["via"] = "ručne"
        else:
            c["via"] = "corrections" if got and "all" in got else "ručne"
    return changes


def spec_changes(snapshot: Path, spec: dict, corrections: list[dict] | None = None) -> dict:
    """Diff of `spec` against the snapshot of the previous run (None when there is none)."""
    if not snapshot.exists():
        return {"against": None, "changes": []}
    prev = json.loads(snapshot.read_text(encoding="utf-8"))
    return {"against": (prev.get("generated") or {}).get("snapshot_of") or (prev.get("generated") or {}).get("baseline") or str(snapshot),
            "changes": classify(diff(prev, spec), corrections)}


def save_snapshot(snapshot: Path, spec: dict, label: str) -> None:
    sp = json.loads(json.dumps(spec))
    sp.setdefault("generated", {})["snapshot_of"] = label
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(sp, ensure_ascii=False, indent=1), encoding="utf-8")

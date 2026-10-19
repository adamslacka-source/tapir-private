"""spec_corrections: corrections of a facade spec written by the I7 iteration loop (ADR-001 I7, class 'spec').

The loop never edits a source file by hand: every correction is one entry of a corrections file
(`facade/corrections/<spec id>.json`, schema `facade-corrections/1`) with the path of the value, the value before and
after, the source (`I7.<iteration>`), the certainty (`meranie`), the cause and the evidence (re-measured edge, crop PNG).
The adapters apply the active entries when they write the spec:

  target 'types'  pilot A: applied to the types file of the house (facade/adapters/senec_types/<house>.json) BEFORE the
                  spec is derived from it, so generated elements follow (sill and surround of a corrected opening)
  target 'spec'   applied to the finished spec (pilot B: from_photo.py; any pilot: global values such as the photo frame)

An entry is applied only when the value at its path is still `before` (otherwise it is stale - the source changed since
the loop measured - and it is listed in spec.generated.corrections_stale, never applied silently). Reverted entries stay
in the file with status 'reverted' and the reason (the loop keeps a correction only when it lowers the error).

Path elements: a key (str), a list index (int) or {"id": X} = the list item whose id is X. Pure standard library: the
pilot-B adapter loads this file by its path from an isolated interpreter (python -I).
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

SCHEMA = "facade-corrections/1"
DIR = Path(__file__).resolve().parent / "corrections"


def path_of(spec_id: str) -> Path:
    return DIR / f"{spec_id}.json"


def load(path: Path | None, spec_id: str | None = None) -> dict:
    if path is not None and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise ValueError(f"{path}: schema {data.get('schema')!r} != {SCHEMA}")
        return data
    return {"schema": SCHEMA, "spec": spec_id, "corrections": [], "simplifications": [], "sources": []}


def save(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def same(a, b, tol: float = 1e-6) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), abs_tol=tol)
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same(a[k], b[k], tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y, tol) for x, y in zip(a, b))
    return a == b


def _step(node, key, create: bool):
    if isinstance(key, dict):
        hit = [x for x in node if isinstance(x, dict) and x.get("id") == key["id"]]
        if len(hit) != 1:
            raise KeyError(f"{len(hit)} items with id {key['id']!r}")
        return hit[0]
    if isinstance(key, int):
        return node[key]
    if key not in node:
        if not create:
            raise KeyError(key)
        node[key] = {}
    return node[key]


def get(root, path: list, default=None):
    node = root
    try:
        for k in path:
            node = _step(node, k, False)
    except (KeyError, IndexError, TypeError):
        return default
    return node


def put(root, path: list, value) -> None:
    node = root
    for k in path[:-1]:
        node = _step(node, k, True)
    last = path[-1]
    if isinstance(last, dict):
        raise KeyError("the last path element must be a key or an index")
    node[last] = copy.deepcopy(value)


def apply(root, data: dict, target: str, skip_ids: set | None = None) -> tuple[list[str], list[str]]:
    """Apply the active corrections of `target` to `root` in place. Returns (applied ids, stale ids)."""
    applied, stale = [], []
    for c in data.get("corrections", []):
        if c.get("status", "active") != "active" or c["target"] != target or c["id"] in (skip_ids or set()):
            continue
        cur = get(root, c["path"], None)
        if not same(cur, c["before"]):
            stale.append(c["id"])
            continue
        put(root, c["path"], c["after"])
        applied.append(c["id"])
    return applied, stale


def finish_spec(spec: dict, data: dict, applied: list[str], stale: list[str]) -> None:
    """Sources of the iterations, simplifications reported by the loop (ambiguous findings) and the bookkeeping."""
    have = {s["id"] for s in spec.get("sources", [])}
    used = {c["src"] for c in data.get("corrections", []) if c["id"] in applied}
    for s in data.get("sources", []):
        if s["id"] in used and s["id"] not in have:
            spec.setdefault("sources", []).append(dict(s))
    if data.get("simplifications"):
        spec.setdefault("simplifications", [])
        refs = {x["ref"] for x in spec["simplifications"]}
        spec["simplifications"] += [dict(x) for x in data["simplifications"] if x["ref"] not in refs]
    gen = spec.setdefault("generated", {})
    gen["corrections_applied"] = sorted(set(gen.get("corrections_applied", [])) | set(applied))
    if stale:
        gen["corrections_stale"] = sorted(set(gen.get("corrections_stale", [])) | set(stale))

"""g1: gate G1 table (ADR-001 3.2) - everything in the model that is NOT from a dimension or a measurement.

Rows: (1) every spec value with certainty pravidlo / odhad / default, with the proposed value from
spec.g1_proposals; (2) every exception (the spec wants it, the API / library cannot build it); (3) every
deliberate simplification (spec.simplifications). An empty column 'Schválené' = proposal accepted (ADR 3.2).

    python g1.py <spec.json> [--report <out>/<id>/report.json] [--out C:/FacadePilot/out]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from spec_validate import low_values  # noqa: E402

UNITS = ("thickness", "reveal", "level", "transom_h", "sidelight_w", "sidelight_sill", "transom_bar_h", "bar_h", "].w", "].h", "].v", "].s", "band", "ledge")


def _unit(ref: str, value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and any(k in ref for k in UNITS):
        return "m"
    return ""


def _val(v) -> str:
    if isinstance(v, bool):
        return "áno" if v else "nie"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(v)


def g1_markdown(spec: dict, exceptions: list[dict]) -> str:
    props = spec.get("g1_proposals", {})
    hs = spec["reference"]["home_story"]
    rows = [("reference.home_story", hs["floorIndex"], hs["conf"], hs.get("src"), hs.get("note", ""))]
    rows += list(low_values(spec))
    lines = [f"# G1 – predpoklady na potvrdenie: {spec['id']}", "",
             f"Spec: `{spec['id']}.facade-spec.json` · vygenerované z modelu, ktorý je postavený (ADR-001, brána G1).  ",
             "Sem patrí len to, čo nie je z kóty ani z merania na fotke: odhad, pravidlo, predvolená hodnota systému, "
             "výnimky (model nevie, čo spec chce) a vedomé zjednodušenia. Prázdny stĺpec **Schválené** = návrh prijatý.", "",
             "Istota: kota > meranie > pravidlo > odhad > default. Zdroje: S1 fotka, S2 skript domu (hĺbky), S5 projekt ArchiCADu, "
             "S6 typy odčítané z rektifikovanej fotky.", "",
             "## 1. Hodnoty pod úrovňou merania", "",
             "| # | Hodnota (ref) | V modeli | Jednotka | Istota | Zdroj | Prečo | Návrh | Schválené |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, (ref, value, conf, src, note) in enumerate(rows, 1):
        proposal = props.get(ref, "prijať hodnotu v modeli")
        lines.append(f"| {i} | `{ref}` | {_val(value)} | {_unit(ref, value)} | {conf} | {src or '–'} | {note} | {proposal} | |")
    lines += ["", "## 2. Výnimky – spec chce, model nevie (vykázané, nie zamlčané)", ""]
    if exceptions:
        lines += ["| # | Prvok / systém | Pole | Spec | Model | Dôvod | Návrh | Schválené |", "|---|---|---|---|---|---|---|---|"]
        for i, e in enumerate(exceptions, 1):
            lines.append(f"| {i} | {e.get('item', e['system'])} | {e['field']} | {_val(e['spec'])} | {_val(e['model']) if e['model'] is not None else '–'} | "
                         f"{e['reason']} | {'Tapir T7 (oprava `libraryPartName` – parametre prvku), potom prestavať' if 'T7' in e['reason'] or 'transom' in e['field'] else 'riešiť v ďalšom inkremente'} | |")
    else:
        lines.append("Žiadne.")
    lines += ["", "## 3. Zjednodušenia", ""]
    simp = spec.get("simplifications", [])
    if simp:
        lines += ["| # | Kde | Čo | Návrh | Schválené |", "|---|---|---|---|---|"]
        for i, s in enumerate(simp, 1):
            lines.append(f"| {i} | `{s['ref']}` | {s['text']} | {s['proposal']} | |")
    else:
        lines.append("Žiadne.")
    odh = spec.get("generated", {}).get("pilot_odhady", [])
    if odh:
        lines += ["", "## 4. ODHAD zapísané v pilote (info[\"odhady\"] skriptu domu, len na vedomie)", ""]
        lines += [f"- {o}" for o in odh]
    return "\n".join(lines) + "\n"


def write_g1(spec: dict, exceptions: list[dict], out_root: Path) -> Path:
    path = Path(out_root) / spec["id"] / "G1_predpoklady.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(g1_markdown(spec, exceptions), encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--report", type=Path, help="report.json of verify (exceptions); default <out>/<id>/report.json")
    ap.add_argument("--out", type=Path, default=Path(r"C:\FacadePilot\out"))
    args = ap.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    rep = args.report or args.out / spec["id"] / "report.json"
    exceptions = json.loads(rep.read_text(encoding="utf-8")).get("exceptions", []) if rep.exists() else []
    print(write_g1(spec, exceptions, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

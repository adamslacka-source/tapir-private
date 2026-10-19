"""GDL objects of the facade pipeline from XML templates, without C++ (ADR-001 D5, increment I4).

    template XML (facade/gdl/templates/<name>.xml: parameters + master / 2D / 3D / parameter scripts)
      -> LP_XMLConverter convertlibrary (compile + interpret check of the GDL scripts)
      -> C:/FacadePilot/out/_gdl/<name>.gsm
      -> AddFilesToEmbeddedLibrary (overwriteExisting) -> ReloadLibraries -> GetAvailableLibraryParts

    python gdl_build.py                      # compile all templates (offline, no Archicad)
    python gdl_build.py --deploy             # ... and load the changed ones into the embedded library of the open project
    python gdl_build.py FS_Louvers --deploy  # one template

Idempotent: the manifest (out/_gdl/manifest.json) keeps the hash of template + default overrides + converter build;
an unchanged template that is already loaded in the project is neither compiled nor uploaded again.
Every converter call has a timeout; a GDL error (anything but 'Missing ancestor', which only says that the built-in
subtypes 'General GDL Object' / 'Model Element' are not files the converter could see) fails the build.
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tapir_client import TapirClient, TapirError, results_ok  # noqa: E402

TEMPLATES = HERE / "gdl" / "templates"
OUT = Path(r"C:\FacadePilot\out\_gdl")
MANIFEST = OUT / "manifest.json"
CONVERTER = Path(r"C:\Program Files\Graphisoft\Archicad 29\LP_XMLConverter.exe")
PROJECT = r"C:\FacadePilot\Fasada_pilot_A.pln"
EMBEDDED_DIR = "FS_GDL"                         # folder inside the embedded library
CONVERT_TIMEOUT = 120                            # s per converter call
TEMPLATE_NAMES = ("FS_Louvers", "FS_SpandrelPanel", "FS_CladdingPanels", "FS_Surround")
IGNORED_ERRORS = ("Missing ancestor",)


class GdlBuildError(RuntimeError):
    pass


# -- converter ----------------------------------------------------------------------------------------------------
def run_converter(args: list[str], timeout: float = CONVERT_TIMEOUT) -> subprocess.CompletedProcess:
    if not CONVERTER.exists():
        raise GdlBuildError(f"LP_XMLConverter not found: {CONVERTER}")
    try:
        return subprocess.run([str(CONVERTER), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GdlBuildError(f"LP_XMLConverter {' '.join(args[:1])}: no answer within {timeout:.0f} s") from exc


def converter_version() -> str:
    """'LP_XMLConverter 29.0.0, build 3000, WIN64' (first line printed without arguments)."""
    out = run_converter([], timeout=30).stdout.strip().splitlines()
    return out[0].strip() if out else ""


# -- templates ----------------------------------------------------------------------------------------------------
def template_path(name: str) -> Path:
    p = TEMPLATES / f"{name}.xml"
    if not p.exists():
        raise GdlBuildError(f"unknown template {name} ({p})")
    return p


def template_info(xml: str) -> dict:
    """MainGUID, version comment and parameters {name: (type, default)} of a template XML."""
    guid = re.search(r'<Symbol[^>]*MainGUID="([0-9A-F-]+)"', xml).group(1)
    params = {}
    for m in re.finditer(r'<(\w+) Name="(\w+)">\s*<Description>.*?</Description>\s*<Value>([^<]*)</Value>', xml, re.S):
        params[m.group(2)] = (m.group(1), m.group(3))
    ver = re.search(r'! (FS_\w+) ([0-9.]+)', xml)
    return {"guid": guid, "params": params, "version": ver.group(2) if ver else ""}


def apply_defaults(xml: str, defaults: dict | None) -> str:
    """Overrides the default <Value> of named parameters (instance values are set later per element)."""
    for name, value in (defaults or {}).items():
        if isinstance(value, bool):
            value = int(value)
        pat = re.compile(rf'(<\w+ Name="{re.escape(name)}">\s*<Description>.*?</Description>\s*<Value>)[^<]*(</Value>)', re.S)
        xml, n = pat.subn(lambda m: f"{m.group(1)}{value}{m.group(2)}", xml)
        if n != 1:
            raise GdlBuildError(f"parameter {name} not found in the template")
    return xml


def source_hash(xml: str, conv: str) -> str:
    return hashlib.sha256((xml + "\n" + conv).encode("utf-8")).hexdigest()[:16]


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"schema": "fs-gdl-manifest/1", "parts": {}}


def save_manifest(man: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")


def compile_template(name: str, defaults: dict | None = None, conv: str | None = None) -> dict:
    """Template -> checked .gsm in OUT. Returns {name, gsm, hash, guid, params, warnings, seconds}."""
    conv = conv or converter_version()
    xml = apply_defaults(template_path(name).read_text(encoding="utf-8"), defaults)
    h = source_hash(xml, conv)
    src_dir = OUT / "src" / name                      # convertlibrary works on folders: one folder per template
    bin_dir = OUT / "bin" / name
    for d in (src_dir, bin_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    (src_dir / f"{name}.xml").write_text(xml, encoding="utf-8")
    t0 = time.monotonic()
    res = run_converter(["convertlibrary", "-l", "UTF8", "-reportlevel", "2", "-interpret", "-checkinit",
                         str(src_dir), str(bin_dir)])
    lines = (res.stdout + "\n" + res.stderr).splitlines()
    errors = [ln.strip() for ln in lines if ": error:" in ln and not any(k in ln for k in IGNORED_ERRORS)]
    warnings = [ln.strip() for ln in lines if ": warning:" in ln]
    gsm_built = bin_dir / f"{name}.gsm"
    if errors or not gsm_built.exists():
        raise GdlBuildError(f"{name}: LP_XMLConverter exit {res.returncode}; errors: {errors or lines[-5:]}")
    gsm = OUT / f"{name}.gsm"
    shutil.copyfile(gsm_built, gsm)
    # round trip: the .gsm read back to XML must carry the same GUID and parameters as the source
    back = OUT / "src" / f"{name}.roundtrip.xml"
    rt = run_converter(["libpart2xml", "-l", "UTF8", str(gsm), str(back)])
    if rt.returncode != 0 or not back.exists():
        raise GdlBuildError(f"{name}: libpart2xml round trip failed: {rt.stdout[-400:]}")
    info, info_back = template_info(xml), template_info(back.read_text(encoding="utf-8-sig"))
    if info["guid"] != info_back["guid"] or set(info["params"]) != set(info_back["params"]):
        raise GdlBuildError(f"{name}: round trip differs (GUID or parameters)")
    return {"name": name, "gsm": str(gsm), "hash": h, "guid": info["guid"], "version": info["version"],
            "params": {k: v[0] for k, v in info["params"].items()}, "warnings": warnings,
            "seconds": round(time.monotonic() - t0, 2), "converter": conv}


# -- Archicad -----------------------------------------------------------------------------------------------------
def embedded_path(name: str) -> str:
    return f"{EMBEDDED_DIR}\\{name}.gsm"


def loaded_parts(ac: TapirClient, names) -> dict[str, dict]:
    res = ac.tapir("GetAvailableLibraryParts", {"filterByTypeId": "Object"}, timeout=120)
    want = set(names)
    return {p["documentName"]: p for p in res["libraryParts"] if p.get("documentName") in want}


def deploy(ac: TapirClient, names=TEMPLATE_NAMES, defaults: dict | None = None, force: bool = False,
           project: str = PROJECT) -> dict:
    """Builds the changed templates and loads them into the embedded library of the open project.
    Returns {'changed': [...], 'unchanged': [...], 'parts': {name: libpart}, 'reload': ExecutionResult}."""
    ac.guard_project(project)
    conv = converter_version()
    man = load_manifest()
    present = loaded_parts(ac, names)
    changed, unchanged, builds = [], [], {}
    for name in names:
        xml = apply_defaults(template_path(name).read_text(encoding="utf-8"), (defaults or {}).get(name))
        h = source_hash(xml, conv)
        entry = man["parts"].get(name)
        if not force and entry and entry.get("hash") == h and name in present and Path(entry["gsm"]).exists():
            unchanged.append(name)
            continue
        builds[name] = compile_template(name, (defaults or {}).get(name), conv)
        changed.append(name)
    reload_result = None
    if changed:
        files = [{"inputPath": builds[n]["gsm"], "outputPath": embedded_path(n), "type": "Object"} for n in changed]
        res = ac.tapir("AddFilesToEmbeddedLibrary", {"files": files, "overwriteExisting": True}, timeout=120)
        if errs := results_ok(res["executionResults"]):
            raise TapirError(f"AddFilesToEmbeddedLibrary: {errs}")
        reload_result = ac.tapir("ReloadLibraries", {}, timeout=180)
        if not reload_result.get("success", False):
            raise TapirError(f"ReloadLibraries: {reload_result}")
        present = loaded_parts(ac, names)
    missing = [n for n in names if n not in present]
    if missing:
        raise TapirError(f"library parts not visible after reload: {missing}")
    for n in changed:
        man["parts"][n] = {k: builds[n][k] for k in ("hash", "gsm", "guid", "version", "params", "converter")} | {
            "embedded": embedded_path(n), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "libpart_guid": present[n].get("guid"), "warnings": builds[n]["warnings"]}
    if changed:
        save_manifest(man)
    return {"changed": changed, "unchanged": unchanged, "parts": present, "reload": reload_result, "builds": builds}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", default=list(TEMPLATE_NAMES))
    ap.add_argument("--deploy", action="store_true", help="load into the embedded library of the open project")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    print(converter_version())
    if args.deploy:
        r = deploy(TapirClient(), args.names, force=args.force)
        print(f"changed: {r['changed']}  unchanged: {r['unchanged']}")
        for n, p in r["parts"].items():
            print(f"  {n}: index {p.get('index')}, guid {p.get('guid')}")
        return 0
    for n in args.names:
        b = compile_template(n)
        print(f"{n}: {b['gsm']} ({b['seconds']} s), hash {b['hash']}, warnings {len(b['warnings'])}")
        for w in b["warnings"]:
            print("   ", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())

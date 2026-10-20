"""
Spike of ADR-001 R1 (SetRailingSystemDefaults): what do the Railing tool defaults carry?

    python spike_railing_defaults.py                 # Archicad on port 19723
    python spike_railing_defaults.py --port 19724 --out C:/temp/spike.json

READ ONLY - writes nothing into Archicad. Needs the Tapir add-on with GetRailingSystemDefaults.
Standard library only.

Prints and saves (JSON, default %TEMP%/spike_railing_defaults.json):
  1. ACAPI_Element_GetDefaultsExt / GetDefaults for API_RailingID: whether they work, the node and
     segment templates (with the library part of each subelement) and the number of items of every
     railing memo array in the defaults (does the default carry a rail list?);
  2. every kind of Railing subelement default (APISubElement_Railing*, with and without
     APISubElemFlag_DefaultDefault): the error codes and the GDL parameters Archicad gives;
  3. every Railing Favorite of the project: memo and subElements as ACAPI_Favorite_Get gives them -
     how a favorite with N rails, a glass panel or balusters is represented (kind, subIndex,
     subSubIndex, rail heights, library parts).
The answers decide which SetRailingSystemDefaults fields can work (see ADR-001, Stav R1).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.stdout.reconfigure (encoding='utf-8')

TIMEOUT_S = 180
URL = ''


def run (command: str, parameters: dict | None = None) -> dict:
    body = json.dumps ({'command': command, 'parameters': parameters or {}}).encode ('utf-8')
    request = urllib.request.Request (URL, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen (request, timeout=TIMEOUT_S) as response:
        data = json.loads (response.read ().decode ('utf-8'))
    if not data.get ('succeeded'):
        return {'error': data.get ('error', data)}
    return data.get ('result', {})


def tapir (command: str, parameters: dict | None = None) -> dict:
    result = run ('API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command},
        'addOnCommandParameters': parameters or {}})
    if 'error' in result:
        return result
    return result.get ('addOnCommandResponse', result)


def symbol (os_: dict | None) -> str:
    if not os_:
        return '-'
    return os_.get ('libraryPartName') or os_.get ('symbolId', '-')


def print_railing (label: str, railing: dict | None) -> None:
    if not railing:
        print (f'  {label}: -')
        return
    seg = railing.get ('segment', {})
    pat = seg.get ('pattern', {})
    print (f"  {label}: height {seg.get ('height')}, post {symbol (railing.get ('node', {}).get ('post', {}).get ('symbol'))}")
    print (f"    pattern {pat.get ('distribution')}, nPanels {pat.get ('nPanels')}, nBalusterSets {pat.get ('nBalusterSets')}, nInnerPosts {pat.get ('nInnerPosts')}")
    print (f"    panel visible {pat.get ('panel', {}).get ('visible')} {symbol (pat.get ('panel', {}).get ('symbol'))}; "
           f"baluster {symbol (pat.get ('balusterSet', {}).get ('balusterSymbol'))} spacing {pat.get ('balusterSet', {}).get ('distribution', {}).get ('length')}")
    for key in ('toprail', 'handrail', 'additionalHandrails', 'rail', 'additionalRails'):
        item = seg.get (key, {})
        print (f"    {key}: visible {item.get ('visible')}, height {item.get ('height')}, {symbol (item.get ('symbol'))}")


def print_memo (label: str, memo: dict | None) -> None:
    if not memo:
        print (f'  {label}: -')
        return
    counts = {k: v for k, v in memo.get ('counts', {}).items () if v}
    print (f'  {label} counts (non-zero): {counts or "all empty"}')
    if memo.get ('rails'):
        print (f"    rail heights: {[r.get ('height') for r in memo['rails']]}")


def main () -> int:
    global URL
    parser = argparse.ArgumentParser ()
    parser.add_argument ('--host', default='http://127.0.0.1')
    parser.add_argument ('--port', type=int, default=19723)
    parser.add_argument ('--out', default=os.path.join (tempfile.gettempdir (), 'spike_railing_defaults.json'))
    args = parser.parse_args ()
    URL = f'{args.host}:{args.port}'

    try:
        version = tapir ('GetAddOnVersion')
    except (urllib.error.URLError, TimeoutError) as error:
        print (f'Archicad is not reachable on {URL}: {error}')
        return 2
    print ('Tapir add-on version:', version)

    report: dict = {}
    defaults = tapir ('GetRailingSystemDefaults', {'includeParameters': True})
    report['toolDefaults'] = defaults
    if 'error' in defaults:
        print ('GetRailingSystemDefaults failed (add-on without the command?):', defaults['error'])
        return 1

    print ('\n1. Railing tool defaults')
    print (f"  GetDefaultsExt: {defaults.get ('getDefaultsExt')}, GetDefaults: {defaults.get ('getDefaults')}")
    print_railing ('railing', defaults.get ('railing'))
    print_memo ('memo (Ext)', defaults.get ('memo'))
    print_memo ('memo (Basic)', defaults.get ('memoBasic'))

    print ('\n2. Subelement defaults (APISubElement_Railing*)')
    for item in defaults.get ('subElementProbe', []):
        sub = item.get ('subElement')
        params = sub.get ('gdlParameters', {}) if sub else {}
        details = sub.get ('details', {}) if sub else {}
        print (f"  {item['kind']:<20} err {item['errorWithType']:>12} / {item.get ('errorWithoutType', '-'):>12}  "
               f"{'type ' + sub.get ('elementType', '?') if sub else '':<26} params {params.get ('count', '-'):>4}  "
               f"{symbol (details.get ('symbol')) if details else ''}")
        names = [p['name'] for p in params.get ('parameters', []) if p.get ('type') in ('Material', 'BuildingMaterial', 'Profile', 'Length')]
        if names:
            print (f"      material/profile/length parameters: {', '.join (names[:40])}")

    print ('\n3. Railing Favorites')
    favorites = tapir ('GetFavoritesByType', {'elementType': 'Railing'}).get ('favorites', [])
    report['favorites'] = {}
    for name in favorites:
        favorite = tapir ('GetRailingSystemDefaults', {'favoriteName': name, 'probeSubElements': False, 'includeParameters': True})
        report['favorites'][name] = favorite.get ('favorite', favorite)
        fav = favorite.get ('favorite', {})
        print (f"\n  '{name}': get {fav.get ('get')}")
        print_railing ('railing', fav.get ('railing'))
        print_memo ('memo', fav.get ('memo'))
        subs = fav.get ('subElements', [])
        print (f'    subElements: {len (subs)}')
        for sub in subs:
            details = sub.get ('details', {})
            extra = ''
            if 'height' in details:
                extra = f" height {details.get ('height')} visible {details.get ('visible')}"
            print (f"      {sub.get ('kind'):<20} subType {sub.get ('subType')} subIndex {sub.get ('subIndex')} subSubIndex {sub.get ('subSubIndex')} "
                   f"flags {sub.get ('subElemFlags')} type {sub.get ('elementType')} params {sub.get ('gdlParameters', {}).get ('count')}"
                   f"{extra} {symbol (details.get ('symbol')) if details else ''}")

    with open (args.out, 'w', encoding='utf-8') as file:
        json.dump (report, file, ensure_ascii=False, indent=1)
    print (f'\nsaved: {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit (main ())

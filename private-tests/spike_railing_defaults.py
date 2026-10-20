"""
Spike of ADR-001 R1 (SetRailingSystemDefaults): what do the Railing tool defaults carry?
Step by step, because the first version of GetRailingSystemDefaults crashed Archicad 29.

    python spike_railing_defaults.py                       # Archicad on port 19723, all steps
    python spike_railing_defaults.py --list                # list the steps
    python spike_railing_defaults.py --from basic-names    # resume after a crash / restart
    python spike_railing_defaults.py --only ext            # one step
    python spike_railing_defaults.py --port 19724 --log C:/temp/spike.jsonl

READ ONLY - writes nothing into Archicad. Needs the Tapir add-on with GetRailingSystemDefaults
(commit after b565598). Standard library only.

Every step is one Tapir call. BEFORE the call the log gets a line {"step": ..., "status": "started"}
and AFTER it {"status": "ok" | "error" | "crashed", "result": ...}; each line is flushed and fsynced,
so after a crash the last "started" line without an answer names the step that crashed Archicad.
The script stops at the first crash (connection reset / refused). Default log:
%TEMP%/spike_railing_defaults.jsonl (appended).

Steps (from the safest; the risky ones are opt-in switches of GetRailingSystemDefaults):
  basic                 ACAPI_Element_GetDefaults only (as CreateRailings and the API example)
  basic-memo            + counts of the railing memo arrays (BMGetPtrSize)
  basic-names           + library part names of the subelement symbols (ACAPI_LibraryPart_Search)
  ext                   + ACAPI_Element_GetDefaultsExt (Railing) - not documented for the Railing
  ext-memo              + counts of the Ext memo arrays
  sub-<Kind>            one subelement default (GetDefaultsExt with one API_SubElement), per kind
  fav-<n>               Railing Favorite n (ACAPI_Favorite_Get, memo only)
  fav-<n>-sub           the same with its subElements
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure (encoding='utf-8')

TIMEOUT_S = 180
URL = ''
KINDS = ['Node', 'Segment', 'Post', 'InnerPost', 'Rail', 'AdditionalRail', 'Handrail', 'AdditionalHandrail',
         'Toprail', 'Panel', 'Pattern', 'BalusterSet', 'Baluster', 'RailEnd', 'HandrailEnd', 'ToprailEnd',
         'RailConnection', 'HandrailConnection', 'ToprailConnection', 'Finish']
CONNECTION_ERRORS = (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError)


class Crashed (Exception):
    pass


def run (command: str, parameters: dict | None = None) -> dict:
    body = json.dumps ({'command': command, 'parameters': parameters or {}}).encode ('utf-8')
    request = urllib.request.Request (URL, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen (request, timeout=TIMEOUT_S) as response:
            data = json.loads (response.read ().decode ('utf-8'))
    except CONNECTION_ERRORS as error:
        raise Crashed (repr (error)) from error
    except urllib.error.URLError as error:
        if isinstance (error.reason, CONNECTION_ERRORS):
            raise Crashed (repr (error)) from error
        raise
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


def log_line (path: str, record: dict) -> None:
    record = dict (record, time=time.strftime ('%Y-%m-%d %H:%M:%S'))
    with open (path, 'a', encoding='utf-8') as file:
        file.write (json.dumps (record, ensure_ascii=False) + '\n')
        file.flush ()
        os.fsync (file.fileno ())


def base_steps () -> list[tuple[str, dict]]:
    steps = [
        ('basic', {}),
        ('basic-memo', {'readMemoArrays': True}),
        ('basic-names', {'readMemoArrays': True, 'resolveLibraryPartNames': True}),
        ('ext', {'useExt': True}),
        ('ext-memo', {'useExt': True, 'readMemoArrays': True}),
    ]
    for kind in KINDS:
        steps.append ((f'sub-{kind}', {'probeSubElements': True, 'subElementKinds': [kind], 'includeParameters': True}))
    return steps


def summary (name: str, result: dict) -> str:
    if 'error' in result:
        return f"error {json.dumps (result['error'], ensure_ascii=False)[:200]}"
    parts = []
    if 'getDefaults' in result:
        parts.append (f"GetDefaults {result['getDefaults']}")
    if 'getDefaultsExt' in result:
        parts.append (f"GetDefaultsExt {result['getDefaultsExt']}")
    for key in ('memo', 'memoExt'):
        memo = result.get (key)
        if memo:
            arrays = memo.get ('counts') or memo.get ('pointers') or {}
            parts.append (f"{key} " + json.dumps ({k: v for k, v in arrays.items () if v not in (0, 'null')}))
    seg = (result.get ('railing') or {}).get ('segment')
    if seg and name.startswith ('basic'):
        parts.append (f"height {seg.get ('height')}, rail {seg.get ('rail', {}).get ('height')}, "
                      f"panel visible {seg.get ('pattern', {}).get ('panel', {}).get ('visible')}")
    for item in result.get ('subElementProbe', []):
        sub = item.get ('subElement')
        parts.append (f"{item['kind']}: error {item['errorWithType']}" +
                      (f", type {sub.get ('elementType')}, {sub.get ('gdlParameters', {}).get ('count')} GDL params" if sub else ''))
    fav = result.get ('favorite')
    if fav:
        subs = fav.get ('subElements')
        parts.append (f"favorite get {fav.get ('get')}" + (f", {len (subs)} subElements: " +
                      ', '.join (f"{s.get ('kind')}[{s.get ('subIndex')},{s.get ('subSubIndex')}]" for s in subs) if subs is not None else ''))
    return '; '.join (parts)


def main () -> int:
    global URL
    parser = argparse.ArgumentParser ()
    parser.add_argument ('--host', default='http://127.0.0.1')
    parser.add_argument ('--port', type=int, default=19723)
    parser.add_argument ('--log', default=os.path.join (tempfile.gettempdir (), 'spike_railing_defaults.jsonl'))
    parser.add_argument ('--from', dest='start', help='start at this step (resume after a crash)')
    parser.add_argument ('--only', help='run only this step')
    parser.add_argument ('--list', action='store_true', help='list the steps and exit')
    args = parser.parse_args ()
    URL = f'{args.host}:{args.port}'

    steps = base_steps ()
    if args.list:
        for name, params in steps:
            print (f'{name:<24} {json.dumps (params)}')
        print ('fav-<n> / fav-<n>-sub  Railing Favorites of the project (after the steps above)')
        return 0

    log_line (args.log, {'step': 'session', 'status': 'started', 'url': URL})
    try:
        version = tapir ('GetAddOnVersion')
    except (Crashed, urllib.error.URLError, TimeoutError) as error:
        print (f'Archicad is not reachable on {URL}: {error}')
        return 2
    print ('Tapir add-on version:', version)
    print ('log:', args.log)

    def do_step (name: str, params: dict) -> dict | None:
        log_line (args.log, {'step': name, 'status': 'started', 'parameters': params})
        try:
            result = tapir ('GetRailingSystemDefaults', params)
        except Crashed as error:
            log_line (args.log, {'step': name, 'status': 'crashed', 'error': str (error)})
            print (f'[CRASH] {name}: {error}')
            raise
        except (urllib.error.URLError, TimeoutError) as error:
            log_line (args.log, {'step': name, 'status': 'error', 'error': repr (error)})
            print (f'[ERROR] {name}: {error}')
            return None
        status = 'error' if 'error' in result else 'ok'
        log_line (args.log, {'step': name, 'status': status, 'result': result})
        print (f'[{status.upper ()}] {name}: {summary (name, result)}', flush=True)
        return result

    started = args.start is None
    try:
        for name, params in steps:
            if args.only and name != args.only:
                continue
            if not started and name != args.start:
                continue
            started = True
            do_step (name, params)

        if args.only is None or args.only.startswith ('fav-'):
            favorites = tapir ('GetFavoritesByType', {'elementType': 'Railing'}).get ('favorites', [])
            log_line (args.log, {'step': 'favorites', 'status': 'ok', 'result': favorites})
            for n, favorite in enumerate (favorites):
                for suffix, params in (('', {'favoriteName': favorite, 'readMemoArrays': True}),
                                       ('-sub', {'favoriteName': favorite, 'readMemoArrays': True, 'favoriteSubElements': True, 'includeParameters': True})):
                    name = f'fav-{n}{suffix}'
                    if args.only and name != args.only:
                        continue
                    if not started and name != args.start:
                        continue
                    started = True
                    print (f'   ({name} = {favorite!r})')
                    do_step (name, params)
    except Crashed:
        print ('Archicad crashed - restart it, then resume with --from <next step> (see --list).')
        return 3
    return 0


if __name__ == '__main__':
    sys.exit (main ())

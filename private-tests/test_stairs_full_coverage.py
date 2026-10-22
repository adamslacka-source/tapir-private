"""
Full coverage test of the Stair tool API mapping (ADR-002 kap. 12): GetStairDetails, ModifyStairs,
CreateStairs "stair".

Run it on a TEST project only (refuses a project whose name contains "Eden"), with the build
that has GetStairDetails / ModifyStairs:

    python test_stairs_full_coverage.py [--port 19723] [--cleanup] [--only rules,fpdLayout]

Steps:
  1. creates a plain straight stair far from the origin (x = 1300 m),
  2. GetStairDetails -> the writable settings tree (createRequest.stair),
  3. for EVERY writable leaf: ModifyStairs with that one field changed (bool negated, number
     shifted, enum switched to another allowed value, pen + 1, overridden attribute toggled off),
     then reads back -> PASS (kept), NOT KEPT (Archicad changed it), ERROR; method change/recreate,
  4. round trip: CreateStairs with the createRequest of the stair -> GetStairDetails, compare,
  5. geometry change through ModifyStairs (recreate, position check), experimental subelement change.
Writes the per-field table to private-tests/out/stair_coverage.json and prints a summary.
Only the Python standard library is used.
"""

import argparse
import copy
import json
import os
import re
import sys
import urllib.request

parser = argparse.ArgumentParser ()
parser.add_argument ('--port', type=int, default=None)
parser.add_argument ('--cleanup', action='store_true')
parser.add_argument ('--only', default='', help='comma separated top-level members to test')
args = parser.parse_args ()


def Post (port, command, parameters=None, timeout=300):
    request = urllib.request.Request ('http://127.0.0.1:{}'.format (port))
    request.add_header ('Content-Type', 'application/json')
    body = json.dumps ({'command': command, 'parameters': parameters or {}}).encode ('utf8')
    with urllib.request.urlopen (request, body, timeout=timeout) as response:
        return json.loads (response.read ())


def FindPort ():
    for port in range (19723, 19744):
        try:
            if Post (port, 'API.IsAlive', timeout=2).get ('result', {}).get ('isAlive'):
                return port
        except Exception:
            continue
    return None


port = args.port or FindPort ()
if port is None:
    sys.exit ('Archicad is not running.')


def Tapir (command, parameters=None):
    response = Post (port, 'API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command},
        'addOnCommandParameters': parameters or {}})
    if not response.get ('succeeded'):
        raise RuntimeError ('{} failed: {}'.format (command, json.dumps (response.get ('error'))))
    return response['result']['addOnCommandResponse']


info = Tapir ('GetProjectInfo')
name = 'Untitled' if info.get ('isUntitled') else info.get ('projectName', '')
print ('Port {} project {}'.format (port, name))
if 'eden' in name.lower ():
    sys.exit ('Refusing to run against Eden - open an untitled / test project.')
for command in ('GetStairDetails', 'ModifyStairs'):
    r = Post (port, 'API.IsAddOnCommandAvailable', {'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command}})
    if not r.get ('result', {}).get ('available'):
        sys.exit ('{} is not available - load the full_stairs build.'.format (command))

X0, Y0 = 1300.0, 1000.0
created = []


def CreatePlain (x):
    r = Tapir ('CreateStairs', {'stairsData': [{
        'baseLinePoints': [{'x': x, 'y': Y0}, {'x': x, 'y': Y0 + 17 * 0.29}],
        'zCoordinate': 0.0, 'totalHeight': 3.10, 'stepNum': 18, 'flightWidth': 1.2,
        'baseLinePosition': 'Center', 'applyRules': False}]})['elements'][0]
    if 'elementId' not in r:
        sys.exit ('CreateStairs failed: {}'.format (json.dumps (r)))
    created.append (r['elementId'])
    return r['elementId']


def Details (elementId):
    d = Tapir ('GetStairDetails', {'elements': [{'elementId': elementId}]})['stairs'][0]
    if 'error' in d:
        raise RuntimeError (json.dumps (d))
    return d


def Leaves (tree, path=()):
    """(path, value) of every leaf; attribute objects {index, attributeId} and overridden objects are leaves."""
    if isinstance (tree, dict):
        if 'index' in tree and ('attributeId' in tree or len (tree) == 1):
            yield path, tree
            return
        if 'overridden' in tree:
            yield path, tree
            return
        for k, v in tree.items ():
            yield from Leaves (v, path + (k,))
    else:
        yield path, tree


def Nest (path, value):
    out = value
    for k in reversed (path):
        out = {k: out}
    return out


def AllowedValues (elementId, path):
    r = Tapir ('ModifyStairs', {'stairs': [{'elementId': elementId, 'stair': Nest (path, '__invalid__')}]})['stairs'][0]
    m = re.search (r'must be one of ([^;"]+)', json.dumps (r))
    return [v.strip () for v in m.group (1).split (',')] if m else []


def NewValue (elementId, path, value):
    key = path[-1]
    if isinstance (value, bool):
        return not value
    if isinstance (value, dict):
        if 'overridden' in value:
            return {'overridden': False} if value['overridden'] else None
        return None   # attribute: kept as is (needs a second attribute of the type)
    if isinstance (value, int):
        if 'Pen' in key or key == 'pen':
            return value % 254 + 1
        return value + 1
    if isinstance (value, float):
        if 'Applied' in key:
            return None
        if 'angle' in key.lower () or 'Pitch' in key or 'Slant' in key:
            return round (value + 0.05, 6)
        return round (value + 0.01, 6)
    if isinstance (value, str):
        allowed = [v for v in AllowedValues (elementId, path) if v != value]
        return allowed[0] if allowed else None
    return None


results = []
current = CreatePlain (X0)
base = Details (current)['createRequest']['stair']
only = set (filter (None, args.only.split (',')))
leaves = [(p, v) for p, v in Leaves (base) if not only or p[0] in only]
print ('{} writable leaves'.format (len (leaves)))

for path, value in leaves:
    label = '.'.join (path)
    newValue = NewValue (current, path, value)
    if newValue is None:
        results.append ({'field': label, 'status': 'SKIPPED', 'detail': 'no second value to try ({})'.format (json.dumps (value))})
        continue
    try:
        r = Tapir ('ModifyStairs', {'stairs': [{'elementId': current, 'stair': Nest (path, newValue)}]})['stairs'][0]
    except Exception as e:
        results.append ({'field': label, 'status': 'ERROR', 'detail': str (e)})
        continue
    if 'error' in r:
        results.append ({'field': label, 'status': 'ERROR', 'detail': r['error'].get ('message', '')})
        continue
    current = r['elementId']
    status = 'PASS' if not r.get ('notKept') else 'NOT KEPT'
    results.append ({'field': label, 'status': status, 'method': r.get ('method'), 'asked': newValue,
                     'detail': '; '.join (r.get ('notKept', [])) + ((' | changeError: ' + r['changeError']) if r.get ('changeError') else '')})
    print ('[{}] {} -> {} ({})'.format (status, label, json.dumps (newValue), r.get ('method')))
if current not in created:
    created.append (current)

# --- round trip: createRequest -> CreateStairs -> same settings -------------------------------
roundTrip = {}
try:
    original = Details (current)
    request = copy.deepcopy (original['createRequest'])
    request['baseLinePoints'] = [{'x': p['x'] + 5.0, 'y': p['y']} for p in request['baseLinePoints']]
    r = Tapir ('CreateStairs', {'stairsData': [request]})['elements'][0]
    if 'elementId' in r:
        created.append (r['elementId'])
        copyStair = Details (r['elementId'])['createRequest']['stair']
        diffs = [('.'.join (p), v, dict (Leaves (copyStair)).get (p)) for p, v in Leaves (original['createRequest']['stair']) if dict (Leaves (copyStair)).get (p) != v]
        roundTrip = {'status': 'PASS' if not diffs else 'DIFF', 'diffs': diffs[:50]}
    else:
        roundTrip = {'status': 'ERROR', 'detail': r}
except Exception as e:
    roundTrip = {'status': 'ERROR', 'detail': str (e)}
print ('[{}] round trip createRequest -> CreateStairs ({} differences)'.format (roundTrip.get ('status'), len (roundTrip.get ('diffs', []))))

# --- geometry through ModifyStairs (recreate) ---------------------------------------------------
geometry = {}
try:
    g = CreatePlain (X0 + 10.0)
    newPoints = [{'x': X0 + 10.0, 'y': Y0 + 1.0}, {'x': X0 + 10.0, 'y': Y0 + 2.6}, {'x': X0 + 12.0, 'y': Y0 + 2.6}]
    r = Tapir ('ModifyStairs', {'stairs': [{'elementId': g, 'baseLinePoints': newPoints, 'zCoordinate': 0.5,
                                            'turns': [{'pointIndex': 1, 'type': 'Winder'}]}]})['stairs'][0]
    if 'error' in r:
        geometry = {'status': 'ERROR', 'detail': r['error']}
    else:
        created.append (r['elementId'])
        d = Details (r['elementId'])
        gc = d['createRequest']['baseLinePoints']
        ok = len (gc) == 3 and all (abs (a['x'] - b['x']) < 0.002 and abs (a['y'] - b['y']) < 0.002 for a, b in zip (gc, newPoints)) and abs (d['zCoordinate'] - 0.5) < 0.001
        geometry = {'status': 'PASS' if ok else 'FAIL', 'method': r.get ('method'), 'points': gc, 'z': d['zCoordinate'],
                    'turns': [t['type'] for t in d['createRequest']['turns']]}
except Exception as e:
    geometry = {'status': 'ERROR', 'detail': str (e)}
print ('[{}] ModifyStairs geometry (recreate) {}'.format (geometry.get ('status'), json.dumps (geometry)[:300]))

# --- experimental: one placed tread through the memo --------------------------------------------
subelement = {}
try:
    d = Details (current)
    t0 = d['subelements']['treads'][0]
    r = Tapir ('ModifyStairs', {'stairs': [{'elementId': current, 'subelements': {'treads': [{'index': 0, 'thickness': round (t0['thickness'] + 0.01, 6)}]}}]})['stairs'][0]
    if 'error' not in r:
        current = r['elementId']
        created.append (current)
        after = Details (current)['subelements']['treads'][0]['thickness']
        subelement = {'status': 'PASS' if abs (after - (t0['thickness'] + 0.01)) < 1e-6 else 'NOT KEPT', 'before': t0['thickness'], 'after': after, 'response': r}
    else:
        subelement = {'status': 'ERROR', 'detail': r['error']}
except Exception as e:
    subelement = {'status': 'ERROR', 'detail': str (e)}
print ('[INFO] subelement tread[0].thickness: {}'.format (json.dumps (subelement)[:300]))

counts = {}
for r in results:
    counts[r['status']] = counts.get (r['status'], 0) + 1
outDir = os.path.join (os.path.dirname (os.path.abspath (__file__)), 'out')
os.makedirs (outDir, exist_ok=True)
outFile = os.path.join (outDir, 'stair_coverage.json')
with open (outFile, 'w', encoding='utf8') as f:
    json.dump ({'fields': results, 'counts': counts, 'roundTrip': roundTrip, 'geometry': geometry, 'subelement': subelement}, f, indent=1, ensure_ascii=False)
print ()
print ('Fields: {}'.format (json.dumps (counts)))
print ('Table: {}'.format (outFile))

if args.cleanup and created:
    unique = []
    for e in created:
        if e not in unique:
            unique.append (e)
    Tapir ('DeleteElements', {'elements': [{'elementId': e} for e in unique]})
    print ('Deleted the test stairs.')
sys.exit (0 if not counts.get ('ERROR') and geometry.get ('status') == 'PASS' else 1)

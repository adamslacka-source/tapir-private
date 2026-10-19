"""
Smoke test of the private Tapir build (CreateRailings + CreateCurtainWalls).

Run it on a COPY of a project, with the private TapirAddOn_AC29_Win.apx loaded:

    python test_tapir_private.py            # finds the running Archicad by itself
    python test_tapir_private.py --port 19723
    python test_tapir_private.py --cleanup  # deletes what it created at the end

It creates, far away from the project origin (x >= 1000 m) so it touches nothing:
  * an L-shaped railing 6 m + 2.5 m long, 3.0 m above project zero,
  * a curtain wall 8 x 6 m with a 1.25 m column grid and 0.9 / 2.1 m rows,
then reads back their subelements and prints the counts plus a PASS/FAIL summary.
Only the Python standard library is used.
"""

import argparse
import json
import sys
import urllib.request

ORIGIN_X = 1000.0
ORIGIN_Y = 1000.0

parser = argparse.ArgumentParser ()
parser.add_argument ('--port', type=int, default=None)
parser.add_argument ('--cleanup', action='store_true')
args = parser.parse_args ()


def Post (port, command, parameters=None, timeout=60):
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
    sys.exit ('Archicad is not running (no JSON API on ports 19723-19743).')
print ('Archicad JSON API on port {}'.format (port))


def Tapir (command, parameters=None):
    response = Post (port, 'API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command},
        'addOnCommandParameters': parameters or {}
    }, timeout=300)
    if not response.get ('succeeded'):
        raise RuntimeError ('{} failed: {}'.format (command, json.dumps (response.get ('error'), indent=2)))
    result = response['result']['addOnCommandResponse']
    if isinstance (result, dict) and 'error' in result:
        raise RuntimeError ('{} returned error: {}'.format (command, json.dumps (result['error'], indent=2)))
    return result


def IsAvailable (command):
    response = Post (port, 'API.IsAddOnCommandAvailable', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command}
    })
    return bool (response.get ('result', {}).get ('available'))


def ElementOf (creationResult):
    element = creationResult['elements'][0]
    if 'elementId' not in element:
        raise RuntimeError ('creation failed: {}'.format (json.dumps (element, indent=2)))
    return element


def ZRange (element):
    zs = []
    def Walk (o):
        if isinstance (o, dict):
            if isinstance (o.get ('z'), (int, float)):
                zs.append (o['z'])
            for v in o.values ():
                Walk (v)
        elif isinstance (o, list):
            for v in o:
                Walk (v)
    Walk (Tapir ('Get3DGeometryOfElements', {'elements': [element]}))
    return (min (zs), max (zs)) if zs else (None, None)


results = []

def Check (name, ok, detail=''):
    results.append ((name, ok))
    print ('[{}] {}{}'.format ('PASS' if ok else 'FAIL', name, (' - ' + detail) if detail else ''))


print ('Tapir version: {}'.format (Tapir ('GetAddOnVersion').get ('version')))
for command in ('CreateRailings', 'CreateCurtainWalls'):
    Check ('{} available'.format (command), IsAvailable (command))
if not all (ok for _, ok in results):
    sys.exit ('The private Tapir build is not loaded - check the Add-On Manager.')

created = []

# --- railing -----------------------------------------------------------------
railing = None
try:
    railing = ElementOf (Tapir ('CreateRailings', {'railingsData': [{
        'coordinates': [
            {'x': ORIGIN_X, 'y': ORIGIN_Y},
            {'x': ORIGIN_X + 6.0, 'y': ORIGIN_Y},
            {'x': ORIGIN_X + 6.0, 'y': ORIGIN_Y + 2.5}
        ],
        'zCoordinate': 3.0
    }]}))
    created.append (railing)
    Check ('CreateRailings', True, railing['elementId']['guid'])
except Exception as e:
    Check ('CreateRailings', False, str (e))

# --- curtain wall --------------------------------------------------------------
curtainWall = None
try:
    curtainWall = ElementOf (Tapir ('CreateCurtainWalls', {'curtainWallsData': [{
        'coordinates': [
            {'x': ORIGIN_X, 'y': ORIGIN_Y + 10.0},
            {'x': ORIGIN_X + 8.0, 'y': ORIGIN_Y + 10.0}
        ],
        'zCoordinate': 0.0,
        'height': 6.0,
        'primaryPattern': {'logic': 'FixedSizes', 'sizes': [1.25]},
        'secondaryPattern': {'logic': 'FixedSizes', 'sizes': [0.9, 2.1]}
    }]}))
    created.append (curtainWall)
    Check ('CreateCurtainWalls', True, curtainWall['elementId']['guid'])
except Exception as e:
    Check ('CreateCurtainWalls', False, str (e))

# --- read back -----------------------------------------------------------------
if created:
    subelements = Tapir ('GetSubelementsOfHierarchicalElements', {'elements': created})['subelements']
    for element, subs in zip (created, subelements):
        counts = {key: len (value) for key, value in subs.items () if isinstance (value, list)}
        print ('{}: {}'.format (element['elementId']['guid'], json.dumps (counts)))
        if element is railing:
            Check ('railing has posts', counts.get ('railingPosts', 0) > 0, str (counts.get ('railingPosts')))
            # zCoordinate 3.0 must lift the railing: its 3D model has to start at 3.0 m
            # (ACAPI_Element_Create alone leaves a railing on the active story at 0).
            zMin, zMax = ZRange (railing)
            Check ('railing base at zCoordinate 3.0', zMin is not None and abs (zMin - 3.0) < 0.01,
                   '3D z from {} to {} m'.format (zMin, zMax))
        if element is curtainWall:
            panels = counts.get ('cWallPanels', 0)
            # 8 m / 1.25 m = 6.4 columns -> 7 columns; 6 m in 0.9 + 2.1 rows -> 4 rows
            Check ('curtain wall has panels', panels > 0, '{} panels (expected about 7 x 4 = 28)'.format (panels))
            Check ('curtain wall has frames', counts.get ('cWallFrames', 0) > 0, str (counts.get ('cWallFrames')))

    if args.cleanup:
        Tapir ('DeleteElements', {'elements': created})
        print ('Deleted the {} test elements.'.format (len (created)))
    else:
        print ('Test elements left in the project around x = {} m, y = {} m (use --cleanup to delete).'.format (ORIGIN_X, ORIGIN_Y))

failed = [name for name, ok in results if not ok]
print ()
print ('RESULT: {}'.format ('ALL PASSED' if not failed else 'FAILED: ' + ', '.join (failed)))
sys.exit (1 if failed else 0)

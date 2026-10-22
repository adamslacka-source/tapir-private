"""
Test of CreateStairs with landings and winders (ADR-002) and of GetStairBaseLineData.

Run it AFTER Archicad was restarted with the new private TapirAddOn_AC29_Win_private.apx, on a
TEST project (never on Eden.pln - the script refuses a project whose name contains "Eden"
unless --force-project is given):

    python test_stairs_landings_winders.py              # finds the running Archicad by itself
    python test_stairs_landings_winders.py --port 19723
    python test_stairs_landings_winders.py --cleanup    # deletes what it created at the end
    python test_stairs_landings_winders.py --inspect <stair guid> [<guid> ...]
        # only dumps GetStairBaseLineData of existing stairs (e.g. one drawn by hand with
        # landings / winders, to see how Archicad stores them) and exits

It creates, far from the project origin (x >= 1100 m):
  (a) two slabs 3.10 m apart (top levels 0.00 and 3.10) and a U-shaped stair 6+6+6 risers with
      2 landings between them (turns: Landing, Landing),
  (b) an L-shaped stair with winders in the corner (turn: Winder),
  (c) a SCH2-like U-shaped stair: winders in the first corner, a landing in the second,
  (d) INFO only: a straight stair with a landing segment between two collinear flights,
  (e) going 0.34 m at riser 0.172 m (2h+g = 0.684) with applyRules false + z read-back,
  (f) INFO: extraTopTread / extraBottomTread variants, (g) INFO: structure params / hidden structure,
then reads every stair back with GetStairBaseLineData and prints PASS/FAIL.
Only the Python standard library is used.
"""

import argparse
import json
import sys
import urllib.request

ORIGIN_X = 1100.0
ORIGIN_Y = 1000.0
HEIGHT = 3.10
RISERS = 18
GOING = 0.29
WIDTH = 1.20

parser = argparse.ArgumentParser ()
parser.add_argument ('--port', type=int, default=None)
parser.add_argument ('--cleanup', action='store_true')
parser.add_argument ('--force-project', action='store_true')
parser.add_argument ('--inspect', nargs='+', default=None)
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


def StairData (element):
    return Tapir ('GetStairBaseLineData', {'elements': [element]})['stairs'][0]


def Summary (data):
    if 'error' in data:
        return json.dumps (data['error'])
    base = data['baseLine']
    turns = [v['turnType'] for v in base['vertexData'] if v['type'] == 'BaseLine']
    segments = [e['segmentType'] for e in base['edgeData'] if e['type'] == 'BaseLine']
    return ('steps={} riser={:.4f} going={:.4f} height={:.3f} treads run/landing={}/{} risers={} '
            'vertexData[{}] turns={} edgeData[{}] segments={}').format (
        data['stepNum'], data['riserHeight'], data['treadDepth'], data['totalHeight'],
        data['runTreadCount'], data['landingTreadCount'], data['riserCount'],
        base['vertexDataCount'], turns, base['edgeDataCount'], segments)


def InnerTurns (data):
    """turnType of the inner baseline vertices, matched by coordinate order."""
    base = data['baseLine']
    entries = [v for v in base['vertexData'] if v['type'] == 'BaseLine']
    if len (entries) != base['nCoords']:
        return None
    return [v['turnType'] for v in entries[1:-1]]


results = []

def Check (name, ok, detail=''):
    results.append ((name, ok))
    print ('[{}] {}{}'.format ('PASS' if ok else 'FAIL', name, (' - ' + detail) if detail else ''))


def Info (name, detail):
    print ('[INFO] {} - {}'.format (name, detail))


for command in ('CreateStairs', 'GetStairBaseLineData'):
    Check ('{} available'.format (command), IsAvailable (command))
if not all (ok for _, ok in results):
    sys.exit ('The new private Tapir build is not loaded (GetStairBaseLineData missing) - restart Archicad with the new .apx.')

if args.inspect:
    for guid in args.inspect:
        data = StairData ({'elementId': {'guid': guid}})
        print (json.dumps (data, indent=2))
    sys.exit (0)

projectName = ''
try:
    projectName = Tapir ('GetProjectInfo').get ('projectName', '')
except Exception as e:
    print ('GetProjectInfo failed: {}'.format (e))
print ('Project: {}'.format (projectName or '(unknown)'))
if 'eden' in projectName.lower () and not args.force_project:
    sys.exit ('Refusing to run against "{}" - open a test project (or pass --force-project).'.format (projectName))

created = []
COMMON = {
    'zCoordinate': 0.0,
    'totalHeight': HEIGHT,
    'stepNum': RISERS,
    'treadDepth': GOING,
    'lockTreadDepth': True,
    'flightWidth': WIDTH,
    'baseLinePosition': 'Center',
    'walkingLinePosition': 'Center',
    'applyRules': False,
}


def CreateStair (name, data):
    params = dict (COMMON)
    params.update (data)
    try:
        element = ElementOf (Tapir ('CreateStairs', {'stairsData': [params]}))
    except Exception as e:
        Check (name + ': created', False, str (e))
        return None, None
    created.append (element)
    stair = StairData (element)
    Check (name + ': created', 'error' not in stair, element['elementId']['guid'])
    print ('       ' + Summary (stair))
    return element, stair


# --- (a) slabs 3.10 m apart + U stair 6+6+6 with 2 landings ---------------------
# Flight axis (baseLinePosition Center): 5 goings per flight + half a landing (WIDTH / 2).
flight = 5 * GOING
x0, y0 = ORIGIN_X, ORIGIN_Y
uPoints = [
    {'x': x0, 'y': y0},
    {'x': x0, 'y': y0 + flight + WIDTH / 2},
    {'x': x0 - (WIDTH / 2 + flight + WIDTH / 2), 'y': y0 + flight + WIDTH / 2},
    {'x': x0 - (WIDTH / 2 + flight + WIDTH / 2), 'y': y0},
]
try:
    slabPolygon = [
        {'x': x0 - 6.0, 'y': y0 - 2.0}, {'x': x0 + 2.0, 'y': y0 - 2.0},
        {'x': x0 + 2.0, 'y': y0 + 5.0}, {'x': x0 - 6.0, 'y': y0 + 5.0}
    ]
    slabs = Tapir ('CreateSlabs', {'slabsData': [
        {'level': 0.0, 'thickness': 0.25, 'polygonCoordinates': slabPolygon},
        {'level': HEIGHT, 'thickness': 0.25, 'polygonCoordinates': slabPolygon},
    ]})['elements']
    created.extend (s for s in slabs if 'elementId' in s)
    Check ('(a) two slabs 3.10 m apart', sum (1 for s in slabs if 'elementId' in s) == 2, json.dumps (slabs))
except Exception as e:
    Check ('(a) two slabs 3.10 m apart', False, str (e))

_, stairA = CreateStair ('(a) U stair 6+6+6, 2 landings', {
    'baseLinePoints': uPoints,
    'turns': [{'pointIndex': 1, 'type': 'Landing'}, {'pointIndex': 2, 'type': 'Landing'}],
})
if stairA and 'error' not in stairA:
    Check ('(a) 18 risers', stairA['stepNum'] == RISERS, str (stairA['stepNum']))
    Check ('(a) riser height 3.10/18', abs (stairA['riserHeight'] - HEIGHT / RISERS) < 0.001, '{:.4f}'.format (stairA['riserHeight']))
    Check ('(a) total height 3.10', abs (stairA['totalHeight'] - HEIGHT) < 0.001, '{:.3f}'.format (stairA['totalHeight']))
    Check ('(a) turns Landing, Landing', InnerTurns (stairA) == ['Landing', 'Landing'], str (InnerTurns (stairA)))
    Check ('(a) landing treads present', stairA['landingTreadCount'] >= 2, str (stairA['landingTreadCount']))
    Info ('(a) run treads (6+6+6 means 15 goings in flights)', str (stairA['runTreadCount']))

# --- (b) L stair with winders ---------------------------------------------------
xb = ORIGIN_X + 10.0
_, stairB = CreateStair ('(b) L stair with winders', {
    'baseLinePoints': [
        {'x': xb, 'y': y0},
        {'x': xb, 'y': y0 + 9 * GOING + WIDTH / 2},
        {'x': xb + WIDTH / 2 + 8 * GOING, 'y': y0 + 9 * GOING + WIDTH / 2},
    ],
    'turns': [{'pointIndex': 1, 'type': 'Winder', 'numOfWindersType': 'Min'}],
})
if stairB and 'error' not in stairB:
    Check ('(b) turn Winder', InnerTurns (stairB) == ['Winder'], str (InnerTurns (stairB)))
    Check ('(b) 18 risers', stairB['stepNum'] == RISERS, str (stairB['stepNum']))

# --- (c) SCH2-like: winders in corner 1, landing in corner 2 --------------------
xc = ORIGIN_X + 20.0
_, stairC = CreateStair ('(c) U stair winder + landing (SCH2 type)', {
    'baseLinePoints': [
        {'x': xc, 'y': y0},
        {'x': xc, 'y': y0 + flight + WIDTH / 2},
        {'x': xc - (WIDTH / 2 + flight + WIDTH / 2), 'y': y0 + flight + WIDTH / 2},
        {'x': xc - (WIDTH / 2 + flight + WIDTH / 2), 'y': y0},
    ],
    'turns': [{'pointIndex': 1, 'type': 'Winder'}, {'pointIndex': 2, 'type': 'Landing'}],
})
if stairC and 'error' not in stairC:
    Check ('(c) turns Winder, Landing', InnerTurns (stairC) == ['Winder', 'Landing'], str (InnerTurns (stairC)))

# --- (d) INFO: straight stair with a landing segment --------------------------
xd = ORIGIN_X + 30.0
try:
    params = dict (COMMON)
    params.update ({
        'baseLinePoints': [
            {'x': xd, 'y': y0}, {'x': xd, 'y': y0 + 8 * GOING},
            {'x': xd, 'y': y0 + 8 * GOING + 1.2}, {'x': xd, 'y': y0 + 16 * GOING + 1.2},
        ],
        'segments': [{'edgeIndex': 1, 'type': 'Landing'}],
        'keepOnTurnFailure': True,
    })
    element = Tapir ('CreateStairs', {'stairsData': [params]})['elements'][0]
    if 'elementId' in element:
        created.append (element)
        Info ('(d) straight stair with landing segment', Summary (StairData (element)))
    else:
        Info ('(d) straight stair with landing segment', json.dumps (element))
except Exception as e:
    Info ('(d) straight stair with landing segment', str (e))

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
    try:
        Walk (Tapir ('Get3DGeometryOfElements', {'elements': [element]}))
    except Exception as e:
        print ('       Get3DGeometryOfElements failed: {}'.format (e))
    return (min (zs), max (zs)) if zs else (None, None)


# --- (e) going 340 mm at riser 172 mm (2h+g = 684 > 650): rules off --------------
xe = ORIGIN_X + 40.0
elementE, stairE = CreateStair ('(e) going 0.34 with applyRules false', {
    'baseLinePoints': [{'x': xe, 'y': y0}, {'x': xe, 'y': y0 + 17 * 0.34}],
    'treadDepth': 0.34,
})
if stairE and 'error' not in stairE:
    going = stairE.get ('goingFromRisers')
    Check ('(e) going 0.34 (walking line / (risers - 1))', going is not None and abs (going - 0.34) < 0.005,
           'measured {} / treadDepth field {:.4f}'.format (going, stairE['treadDepth']))
    Check ('(e) riser 3.10/18', abs (stairE['riserHeight'] - HEIGHT / RISERS) < 0.001, '{:.4f}'.format (stairE['riserHeight']))
    Check ('(e) read back zCoordinate 0 / zTop 3.10', abs (stairE['zCoordinate']) < 0.001 and abs (stairE['zTop'] - HEIGHT) < 0.001,
           'z {} .. {}'.format (stairE.get ('zCoordinate'), stairE.get ('zTop')))
    rules = stairE['rules']
    Info ('(e) 2R+G rule after applyRules false', 'max {} applied={}'.format (rules['twoRiserPlusGoingMax'], rules['twoRiserPlusGoingMaxApplied']))
    zMin, zMax = ZRange (elementE)
    Info ('(e) 3D z range (structure below 0 = the 100 mm problem)', '{} .. {}'.format (zMin, zMax))

# --- (f) INFO: extraTopTread / extraBottomTread effect --------------------------
for i, (top, bottom) in enumerate (((False, False), (True, False), (False, True), (True, True))):
    element, stair = CreateStair ('(f) extraTopTread={} extraBottomTread={}'.format (top, bottom), {
        'baseLinePoints': [{'x': xe + 4.0 + 2.5 * i, 'y': y0}, {'x': xe + 4.0 + 2.5 * i, 'y': y0 + 17 * GOING}],
        'extraTopTread': top, 'extraBottomTread': bottom,
    })
    if stair and 'error' not in stair:
        Info ('(f) top={} bottom={}'.format (top, bottom), 'treads={} risers={} read back top={} bottom={} 3D z={}'.format (
            stair['treadCount'], stair['riserCount'], stair['extraTopTread'], stair['extraBottomTread'], ZRange (element)))

# --- (g) INFO: monolith structure with explicit thickness / flat bottom ---------
element, stair = CreateStair ('(g) monolith structure params', {
    'baseLinePoints': [{'x': xe + 16.0, 'y': y0}, {'x': xe + 16.0, 'y': y0 + 17 * GOING}],
    'structure': {'structType': 'Monolith', 'bottomType': 'Flat', 'monolithThickness': 0.15, 'bottomOversize': 0.0, 'thickness': 0.15},
})
if stair and 'error' not in stair:
    under = [s for s in stair['structureDefaults'] if s['role'] == 'Run' and s['side'] == 'Under']
    Info ('(g) structure read back', json.dumps (under))
    Info ('(g) 3D z range', str (ZRange (element)))
element, stair = CreateStair ('(g2) structure hidden', {
    'baseLinePoints': [{'x': xe + 18.5, 'y': y0}, {'x': xe + 18.5, 'y': y0 + 17 * GOING}],
    'structureVisible': False,
})
if stair and 'error' not in stair:
    Check ('(g2) structureVisible false kept', stair['structureVisible'] is False, str (stair['structureVisible']))
    Info ('(g2) 3D z range', str (ZRange (element)))

# --- (h) SCH1 exactly: 3 flights 6+6+6 risers, 172 mm, going 0.30, 2 landings ---------
# Center baseline; each flight 5 goings, landings WIDTH x WIDTH, extra top tread off (the
# last tread is the landing / slab), monolith flat structure (no 100 mm below the base).
SCH1_GOING = 0.30
xh = ORIGIN_X + 70.0
f1 = 5 * SCH1_GOING
elementH, stairH = CreateStair ('(h) SCH1 6+6+6, going 0.30, 2 landings', {
    'baseLinePoints': [
        {'x': xh, 'y': y0},
        {'x': xh, 'y': y0 + f1 + WIDTH / 2},
        {'x': xh - (WIDTH / 2 + f1 + WIDTH / 2), 'y': y0 + f1 + WIDTH / 2},
        {'x': xh - (WIDTH / 2 + f1 + WIDTH / 2), 'y': y0},
    ],
    'treadDepth': SCH1_GOING,
    'extraTopTread': False,
    'structure': {'structType': 'Monolith', 'bottomType': 'Flat', 'monolithThickness': 0.15, 'bottomOversize': 0.0},
    'turns': [{'pointIndex': 1, 'type': 'Landing'}, {'pointIndex': 2, 'type': 'Landing'}],
})
if stairH and 'error' not in stairH:
    Check ('(h) turns Landing, Landing', InnerTurns (stairH) == ['Landing', 'Landing'], str (InnerTurns (stairH)))
    Check ('(h) 18 risers of 172 mm', stairH['stepNum'] == RISERS and abs (stairH['riserHeight'] - HEIGHT / RISERS) < 0.001,
           '{} x {:.4f}'.format (stairH['stepNum'], stairH['riserHeight']))
    Check ('(h) 15 run treads (5 per flight)', stairH['runTreadCount'] == 15, 'run {} landing {}'.format (stairH['runTreadCount'], stairH['landingTreadCount']))
    zMin, zMax = ZRange (elementH)
    Check ('(h) 3D from 0.00 to 3.10', zMin is not None and zMin > -0.005 and abs (zMax - HEIGHT) < 0.01, '{} .. {}'.format (zMin, zMax))
    Info ('(h) structures', json.dumps (stairH.get ('structures')))

# --- validation: bad input must give an error, not a crash ----------------------
try:
    bad = Tapir ('CreateStairs', {'stairsData': [{
        'baseLinePoints': [{'x': xb, 'y': y0 - 5.0}, {'x': xb, 'y': y0 - 5.0}],
        'zCoordinate': 0.0
    }]})['elements'][0]
    Check ('zero-length baseline edge is rejected', 'error' in bad, json.dumps (bad))
except Exception as e:
    Check ('zero-length baseline edge is rejected', True, str (e))
try:
    bad = Tapir ('CreateStairs', {'stairsData': [{
        'baseLinePoints': [{'x': xb, 'y': y0 - 5.0}, {'x': xb, 'y': y0 - 2.0}],
        'zCoordinate': 0.0,
        'turns': [{'pointIndex': 1, 'type': 'Landing'}]
    }]})['elements'][0]
    Check ('turn at an end point is rejected', 'error' in bad, json.dumps (bad))
except Exception as e:
    Check ('turn at an end point is rejected', True, str (e))

if created:
    if args.cleanup:
        Tapir ('DeleteElements', {'elements': created})
        print ('Deleted the {} test elements.'.format (len (created)))
    else:
        print ('Test elements left in the project around x = {} m, y = {} m (use --cleanup to delete).'.format (ORIGIN_X, ORIGIN_Y))

failed = [name for name, ok in results if not ok]
print ()
print ('RESULT: {}'.format ('ALL PASSED' if not failed else 'FAILED: ' + ', '.join (failed)))
sys.exit (1 if failed else 0)

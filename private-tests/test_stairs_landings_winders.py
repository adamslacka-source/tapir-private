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
    # Position: the stored baseline (local to basePlane) mapped back to global must start at
    # the first input point (the recreate used to move it to the project origin).
    if 'error' not in stair and stair.get ('baseLineGlobalCoords') and params.get ('turns'):
        g = stair['baseLineGlobalCoords']
        p = params['baseLinePoints']
        ok = all (abs (a['x'] - b['x']) < 0.002 and abs (a['y'] - b['y']) < 0.002 for a, b in zip (g, p)) and len (g) == len (p)
        Check (name + ': stays at its input position', ok, 'global {} vs input {}'.format (
            [(round (c['x'], 3), round (c['y'], 3)) for c in g], [(c['x'], c['y']) for c in p]))
    # MoveElements on the (possibly recreated) stair GUID must work.
    if 'error' not in stair:
        r = Tapir ('MoveElements', {'elementsWithMoveVectors': [{'elementId': element['elementId'], 'moveVector': {'x': 0.0, 'y': 0.0, 'z': 0.0}}]})
        rs = r.get ('executionResults', [])
        Check (name + ': MoveElements on the returned GUID', bool (rs) and rs[0].get ('success'), json.dumps (rs))
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
    going = stairE.get ('goingFromBaseLine')
    Check ('(e) going 0.34 (baseline / (risers - 1), straight stair)', going is not None and abs (going - 0.34) < 0.005,
           'baseline {} / walking line {} / treadDepth field {:.4f}'.format (going, stairE.get ('goingFromRisers'), stairE['treadDepth']))
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

# --- (i) U stair with landings off the origin (Eden report: recreate moved it to 0,0) -----
xi, yi = 1105.0, 1003.0
CreateStair ('(i) U stair with landings at (1105, 1003)', {
    'baseLinePoints': [
        {'x': xi, 'y': yi},
        {'x': xi, 'y': yi + f1 + WIDTH / 2},
        {'x': xi - (WIDTH / 2 + f1 + WIDTH / 2), 'y': yi + f1 + WIDTH / 2},
        {'x': xi - (WIDTH / 2 + f1 + WIDTH / 2), 'y': yi},
    ],
    'treadDepth': SCH1_GOING,
    'turns': [{'pointIndex': 1, 'type': 'Landing'}, {'pointIndex': 2, 'type': 'Landing'}],
})
# Same without lockTreadDepth (Eden: the turned baseline collapsed to a straight 1.7 m flight).
params_i2 = {
    'baseLinePoints': [
        {'x': xi + 6.0, 'y': yi},
        {'x': xi + 6.0, 'y': yi + f1 + WIDTH / 2},
        {'x': xi + 6.0 - (WIDTH / 2 + f1 + WIDTH / 2), 'y': yi + f1 + WIDTH / 2},
        {'x': xi + 6.0 - (WIDTH / 2 + f1 + WIDTH / 2), 'y': yi},
    ],
    'treadDepth': SCH1_GOING,
    'lockTreadDepth': False,
    'turns': [{'pointIndex': 1, 'type': 'Landing'}, {'pointIndex': 2, 'type': 'Landing'}],
}
_, stairI2 = CreateStair ('(i2) same, lockTreadDepth false', params_i2)
if stairI2 and 'error' not in stairI2:
    Check ('(i2) baseline not collapsed (length about 2 x 2.10 + 2.70 m)', abs (stairI2.get ('baseLineLength', 0) - (2 * (f1 + WIDTH / 2) + (WIDTH + f1))) < 0.05,
           'baseLineLength {}'.format (stairI2.get ('baseLineLength')))

# --- (k) SCH2 type: U, 16 x 0.19375 m, going 0.217, width 0.90, well 0.25, winders in both corners,
#         off the origin and at an absolute z of 4.25 m (a story whose level is not 0) -----------
K_RISERS, K_HEIGHT, K_GOING, K_WIDTH, K_WELL = 16, 16 * 0.19375, 0.217, 0.90, 0.25
K_Z = 4.25
axisGap = K_WIDTH + K_WELL                        # distance of the two flight axes
legK = (15 * K_GOING - axisGap) / 2.0             # Center baseline = walking line, 15 goings
xk, yk = 1112.0, 1007.0
paramsK = {
    'baseLinePoints': [
        {'x': xk, 'y': yk},
        {'x': xk, 'y': yk + legK},
        {'x': xk - axisGap, 'y': yk + legK},
        {'x': xk - axisGap, 'y': yk},
    ],
    'zCoordinate': K_Z,
    'totalHeight': K_HEIGHT,
    'stepNum': K_RISERS,
    'treadDepth': K_GOING,
    'lockTreadDepth': False,
    'flightWidth': K_WIDTH,
    'turns': [{'pointIndex': 1, 'type': 'Winder'}, {'pointIndex': 2, 'type': 'Winder'}],
}
_, stairK = CreateStair ('(k) SCH2 U 16 x 0.19375, going 0.217, width 0.90, 2 winders, z 4.25', paramsK)
if stairK and 'error' not in stairK:
    Check ('(k) turns Winder, Winder', InnerTurns (stairK) == ['Winder', 'Winder'], str (InnerTurns (stairK)))
    Check ('(k) 16 risers of 0.19375', stairK['stepNum'] == K_RISERS and abs (stairK['riserHeight'] - 0.19375) < 0.0005,
           '{} x {:.5f}'.format (stairK['stepNum'], stairK['riserHeight']))
    Check ('(k) absolute z 4.25 (story level {})'.format (stairK.get ('homeStoryLevel')), abs (stairK.get ('zCoordinate', -99) - K_Z) < 0.001,
           'zCoordinate {} zTop {}'.format (stairK.get ('zCoordinate'), stairK.get ('zTop')))
    Check ('(k) flight width 0.90', abs (stairK['flightWidth'] - K_WIDTH) < 0.001, str (stairK['flightWidth']))
    going = stairK.get ('goingFromBaseLine')
    Check ('(k) going 0.217 (baseline / 15)', going is not None and abs (going - K_GOING) < 0.005, 'baseline {} walking line {}'.format (going, stairK.get ('goingFromRisers')))
    Check ('(k) walking line Center kept', stairK.get ('walkingLinePosition') == 'Center', str (stairK.get ('walkingLinePosition')))
    Check ('(k) rules stay off after the recreate', not stairK['rules']['treadDepthMinApplied'] and not stairK['rules']['twoRiserPlusGoingMaxApplied'],
           json.dumps ({k: v for k, v in stairK['rules'].items () if k.endswith ('Applied') and v}))

# --- DeleteElements with a wrong item / a missing element must fail (private build) -------
try:
    r = Post (port, 'API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': 'DeleteElements'},
        'addOnCommandParameters': {'elements': [{'elementId': {'guid': '00000000-1111-2222-3333-444444444444'}}]}})
    rs = r.get ('result', {}).get ('addOnCommandResponse', {}).get ('executionResults', [])
    Check ('DeleteElements of a missing element reports a failure', bool (rs) and not rs[0].get ('success'), json.dumps (r)[:300])
    r = Post (port, 'API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': 'DeleteElements'},
        'addOnCommandParameters': {'elements': [{'guid': '00000000-1111-2222-3333-444444444444'}]}})
    rs = r.get ('result', {}).get ('addOnCommandResponse', {}).get ('executionResults', [])
    Check ('DeleteElements with {"guid": ...} item is not a success', not r.get ('succeeded') or (bool (rs) and not rs[0].get ('success')), json.dumps (r)[:300])
except Exception as e:
    Check ('DeleteElements error reporting', False, str (e))

# --- (l) floor plan display: home story + relevant stories (break mark) ---------------------
DISPLAY = {
    'storyVisibility': 'AllRelevantStories',
    'gridTypeBottom': 'WithBreakMarkVisibleHidden',
    'gridTypeTop': 'AboveBreakMarkVisible',
    'gridTypeMiddle': 'Multistory2DVisibleInbetweenBreakMarks',
    'gridTypeAbove': 'NotRelevant',
    'gridTypeBelow': 'NotRelevant',
    'showWalkingLine': True,
}
_, stairL = CreateStair ('(l) floorPlanDisplay at create', {
    'baseLinePoints': [{'x': xe + 36.0, 'y': y0}, {'x': xe + 36.0, 'y': y0 + 17 * GOING}],
    'floorPlanDisplay': DISPLAY,
})
if stairL and 'error' not in stairL:
    fpd = stairL.get ('floorPlanDisplay', {})
    Check ('(l) floor plan display read back', all (fpd.get (k) == v for k, v in DISPLAY.items ()),
           json.dumps ({k: fpd.get (k) for k in list (DISPLAY) + ['visibility', 'isAutoOnStoryVisibility']}))
elementL2, stairL2 = CreateStair ('(l2) stair for SetStairFloorPlanDisplay', {
    'baseLinePoints': [{'x': xe + 38.5, 'y': y0}, {'x': xe + 38.5, 'y': y0 + 17 * GOING}],
})
if stairL2 and 'error' not in stairL2:
    try:
        r = Tapir ('SetStairFloorPlanDisplay', {'elements': [elementL2], 'floorPlanDisplay': dict (DISPLAY, storyVisibility='HomeAndOneStoryUp', gridTypeAbove='AboveBreakMarkVisible')})
        one = r['stairs'][0]
        ok = 'error' not in one and one['floorPlanDisplay']['storyVisibility'] == 'HomeAndOneStoryUp' and one['floorPlanDisplay']['gridTypeAbove'] == 'AboveBreakMarkVisible'
        Check ('(l2) SetStairFloorPlanDisplay changed and read back', ok, json.dumps (one)[:400])
        if 'elementId' in one and one['elementId']['guid'] != elementL2['elementId']['guid']:
            Info ('(l2) new GUID after the change', one['elementId']['guid'])
            created.append ({'elementId': one['elementId']})
    except Exception as e:
        Check ('(l2) SetStairFloorPlanDisplay', False, str (e))

# --- (j) INFO: Smooth monolith heel (Eden: always 100 mm below the start) ---------------
for i, structure in enumerate ((
        {'structType': 'Monolith', 'bottomType': 'Smooth'},
        {'structType': 'Monolith', 'bottomType': 'Smooth', 'horizontalThickness': 0.0},
        {'structType': 'Monolith', 'bottomType': 'Smooth', 'thickness': 0.0},
        {'structType': 'Monolith', 'bottomType': 'Smooth', 'thinning': True},
        {'structType': 'Monolith', 'bottomType': 'Smooth', 'monolithThickness': 0.05})):
    element, stair = CreateStair ('(j) smooth monolith variant {}'.format (i), {
        'baseLinePoints': [{'x': xe + 22.0 + 2.5 * i, 'y': y0}, {'x': xe + 22.0 + 2.5 * i, 'y': y0 + 17 * GOING}],
        'structure': structure,
    })
    if stair and 'error' not in stair:
        Info ('(j) {}'.format (json.dumps (structure)), '3D z {} placed {}'.format (ZRange (element), json.dumps ((stair.get ('structures') or [{}])[0])))

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

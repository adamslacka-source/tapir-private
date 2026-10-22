"""
Regression test: editing EXISTING elements (report from Eden 2026-10-22: SetDetailsOfElements,
MoveElements and ModifyWalls answered "Failed to find the element" / "Element does not exist" /
"Failed to load wall" for every existing element, while GetDetailsOfElements found them).

Run it on a TEST project only (refuses a project whose name contains "Eden"):

    python test_edit_existing_elements.py [--port 19723]

Steps on a wall created far away (x = 2100 m): Create -> GetDetails -> SetDetails (layer)
-> Move -> ModifyWalls (height) -> read back -> Delete; if ProbeElementAccess is available
(build after 52ed566) it also reports the lookup inside / outside an undoable command.
The same sequence is repeated with the 3D window active (the Eden scripts switch to it).
Only the Python standard library is used.
"""

import argparse
import json
import sys
import urllib.request

parser = argparse.ArgumentParser ()
parser.add_argument ('--port', type=int, default=None)
args = parser.parse_args ()


def Post (port, command, parameters=None, timeout=120):
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
        'addOnCommandParameters': parameters or {}
    })
    if not response.get ('succeeded'):
        raise RuntimeError ('{} failed: {}'.format (command, json.dumps (response.get ('error'))))
    return response['result']['addOnCommandResponse']


def IsAvailable (command):
    response = Post (port, 'API.IsAddOnCommandAvailable', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command}})
    return bool (response.get ('result', {}).get ('available'))


info = Tapir ('GetProjectInfo')
name = info.get ('projectName', '') if not info.get ('isUntitled') else 'Untitled'
print ('Port {} project {}'.format (port, name))
if 'eden' in name.lower ():
    sys.exit ('Refusing to run against Eden - open an untitled / test project.')

results = []


def Check (label, ok, detail=''):
    results.append ((label, ok))
    print ('[{}] {}{}'.format ('PASS' if ok else 'FAIL', label, (' - ' + detail) if detail else ''))


def AllSucceeded (response):
    rs = response.get ('executionResults', [])
    return bool (rs) and all (r.get ('success') for r in rs), json.dumps (rs)


def Run (tag, y):
    wall = Tapir ('CreateWalls', {'wallsData': [{
        'begCoordinate': {'x': 2100.0, 'y': y}, 'endCoordinate': {'x': 2105.0, 'y': y},
        'zCoordinate': 0.0, 'height': 3.0, 'thickness': 0.3}]})['elements'][0]
    Check (tag + ' CreateWalls', 'elementId' in wall, json.dumps (wall))
    if 'elementId' not in wall:
        return
    eid = {'elementId': wall['elementId']}
    try:
        if IsAvailable ('ProbeElementAccess'):
            probe = Tapir ('ProbeElementAccess', {'elements': [eid]})
            inside = probe['inside']['elements'][0]
            Check (tag + ' lookup inside an undoable command', inside['getErr'] == 0 and inside['getHeaderErr'] == 0, json.dumps (probe))
        details = Tapir ('GetDetailsOfElements', {'elements': [eid]})['detailsOfElements'][0]
        Check (tag + ' GetDetailsOfElements', 'error' not in details)
        layer = details.get ('layerIndex')
        ok, detail = AllSucceeded (Tapir ('SetDetailsOfElements', {'elementsWithDetails': [{'elementId': eid['elementId'], 'details': {'layerIndex': layer}}]}))
        Check (tag + ' SetDetailsOfElements (layer)', ok, detail)
        ok, detail = AllSucceeded (Tapir ('MoveElements', {'elementsWithMoveVectors': [{'elementId': eid['elementId'], 'moveVector': {'x': 1.0, 'y': 0.0, 'z': 0.0}}]}))
        Check (tag + ' MoveElements', ok, detail)
        ok, detail = AllSucceeded (Tapir ('ModifyWalls', {'wallsWithDetails': [{'elementId': eid['elementId'], 'height': 2.5}]}))
        Check (tag + ' ModifyWalls (height)', ok, detail)
        after = Tapir ('GetDetailsOfElements', {'elements': [eid]})['detailsOfElements'][0].get ('details', {})
        Check (tag + ' read back moved + 2.5 m', abs (after.get ('begCoordinate', {}).get ('x', 0) - 2101.0) < 1e-6 and abs (after.get ('height', 0) - 2.5) < 1e-6,
               'beg x {} height {}'.format (after.get ('begCoordinate', {}).get ('x'), after.get ('height')))
    finally:
        ok, detail = AllSucceeded (Tapir ('DeleteElements', {'elements': [eid]}))
        Check (tag + ' DeleteElements', ok, detail)


Run ('[floor plan]', 2000.0)
try:
    Tapir ('ChangeWindow', {'windowType': '3DModel'})
    Run ('[3D window]', 2002.0)
finally:
    Tapir ('ChangeWindow', {'windowType': 'FloorPlan'})

failed = [label for label, ok in results if not ok]
print ()
print ('RESULT: {}'.format ('ALL PASSED' if not failed else 'FAILED: ' + ', '.join (failed)))
sys.exit (1 if failed else 0)

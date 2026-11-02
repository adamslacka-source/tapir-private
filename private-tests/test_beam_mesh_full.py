"""
Full coverage test of the Beam and Mesh tools (docs/private/beam-mesh-field-map.md): CreateBeams,
ModifyBeams, CreateMeshes, ModifyMeshes, GetDetailsOfElements, the floorIndex filter of
GetElementsByType / GetAllElements / FilterElements and the story switch of ChangeWindow.

Run it after the Archicad restart, ONLY on a scratch new file or on a copy of a project:

    python test_beam_mesh_full.py --self-check          # the project guard only, no connection
    python test_beam_mesh_full.py [--port 19723] [--cleanup] [--only beam,mesh,filter,story]

It refuses to run if the project is Kamenne.pln or cesty.pln, if the project cannot be identified,
or if it cannot decide which of several running Archicad instances to use. The project is checked
again before every write command. Nothing is saved.

Steps for each tool:
  1. compatibility: the requests of the production scripts (v6_sektor) still create the element,
  2. Create with the new settings given at creation, then read back,
  3. every writable field: Modify with that one field changed, then read back:
     PASS (kept), NOT KEPT (Archicad changed it), ERROR (refused),
  4. memo data: beam holes, segments, cuts, schemes, gables; mesh polygon and sublines.
Writes the per-field table to private-tests/out/beam_mesh_coverage.json and prints a summary.
Only the Python standard library is used.
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.request

FORBIDDEN_PROJECTS = ('kamenne', 'cesty')
PORTS = range (19723, 19744)
X0, Y0 = 1300.0, 1000.0


def ProjectStem (pathOrName):
    """The file name without folders and extension, lower case: 'C:\\x\\Kamenne.pln' -> 'kamenne'."""
    name = re.split (r'[\\/]', str (pathOrName).strip ())[-1]
    return re.sub (r'\.(pln|pla|tpl|bpn)$', '', name, flags=re.IGNORECASE).strip ().lower ()


def RefusalReason (info):
    """Why this project must not be tested, or None. Fails closed: an unknown project is refused."""
    if not isinstance (info, dict):
        return 'the project could not be identified'
    if info.get ('isUntitled'):
        return None
    names = [info[key] for key in ('projectPath', 'projectName', 'projectLocation') if info.get (key)]
    if not names:
        return 'the project could not be identified'
    for name in names:
        if ProjectStem (name) in FORBIDDEN_PROJECTS:
            return 'the project is {} (forbidden)'.format (name)
    return None


def SelfCheck ():
    cases = [
        ({'isUntitled': True}, True),
        ({'isUntitled': False, 'projectPath': 'C:\\AT\\scratch.pln', 'projectName': 'scratch'}, True),
        ({'isUntitled': False, 'projectPath': 'C:\\AT\\Kamenne_copy.pln', 'projectName': 'Kamenne_copy'}, True),
        ({'isUntitled': False, 'projectPath': 'C:\\AT Diplom\\Kamenne.pln', 'projectName': 'Kamenne'}, False),
        ({'isUntitled': False, 'projectPath': 'C:/x/KAMENNE.PLN'}, False),
        ({'isUntitled': False, 'projectName': 'cesty'}, False),
        ({'isUntitled': False, 'projectPath': 'D:\\x\\cesty.pln', 'projectName': 'cesty'}, False),
        ({'isUntitled': False, 'projectPath': 'D:\\scratch.pln', 'projectName': 'cesty'}, False),
        ({'isUntitled': False}, False),
        (None, False),
    ]
    failed = 0
    for info, allowed in cases:
        ok = (RefusalReason (info) is None) == allowed
        failed += 0 if ok else 1
        print ('{}  allowed={}  {}'.format ('ok  ' if ok else 'FAIL', allowed, info))
    print ('Self-check: {}'.format ('FAILED' if failed else 'passed'))
    return failed == 0


parser = argparse.ArgumentParser (description='Full coverage test of the Beam and Mesh tools.')
parser.add_argument ('--port', type=int, default=None)
parser.add_argument ('--cleanup', action='store_true', help='delete the test elements at the end')
parser.add_argument ('--only', default='beam,mesh,filter,story', help='comma separated: beam, mesh, filter, story')
parser.add_argument ('--self-check', action='store_true', help='test the project guard without any connection, then exit')
args = parser.parse_args ()

if args.self_check:
    sys.exit (0 if SelfCheck () else 1)


def Post (port, command, parameters=None, timeout=300):
    request = urllib.request.Request ('http://127.0.0.1:{}'.format (port))
    request.add_header ('Content-Type', 'application/json')
    body = json.dumps ({'command': command, 'parameters': parameters or {}}).encode ('utf8')
    with urllib.request.urlopen (request, body, timeout=timeout) as response:
        return json.loads (response.read ())


def TapirOn (port, command, parameters=None, timeout=300):
    response = Post (port, 'API.ExecuteAddOnCommand', {
        'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': command},
        'addOnCommandParameters': parameters or {}}, timeout)
    if not response.get ('succeeded'):
        raise RuntimeError ('{} failed: {}'.format (command, json.dumps (response.get ('error'))))
    return response['result']['addOnCommandResponse']


def ProjectInfo (port, timeout=10):
    try:
        return TapirOn (port, 'GetProjectInfo', timeout=timeout)
    except Exception:
        return None


def ChoosePort ():
    if args.port is not None:
        reason = RefusalReason (ProjectInfo (args.port))
        if reason is not None:
            sys.exit ('Refusing to run on port {}: {}.'.format (args.port, reason))
        return args.port
    allowed = []
    for port in PORTS:
        try:
            if not Post (port, 'API.IsAlive', timeout=2).get ('result', {}).get ('isAlive'):
                continue
        except Exception:
            continue
        info = ProjectInfo (port)
        reason = RefusalReason (info)
        print ('Port {}: {} -> {}'.format (port, (info or {}).get ('projectName', 'Untitled' if (info or {}).get ('isUntitled') else '?'), reason or 'usable'))
        if reason is None:
            allowed.append (port)
    if len (allowed) != 1:
        sys.exit ('Refusing to run: {} usable Archicad instances. Open one scratch project or give --port.'.format (len (allowed)))
    return allowed[0]


port = ChoosePort ()
info = ProjectInfo (port)
print ('Port {} project {}'.format (port, 'Untitled' if info.get ('isUntitled') else info.get ('projectName', '')))

WRITE_COMMANDS = ('CreateBeams', 'ModifyBeams', 'CreateMeshes', 'ModifyMeshes', 'DeleteElements', 'ChangeWindow')


def Tapir (command, parameters=None):
    if command in WRITE_COMMANDS:
        reason = RefusalReason (ProjectInfo (port))
        if reason is not None:
            sys.exit ('Stopped before {}: {}.'.format (command, reason))
    return TapirOn (port, command, parameters)


# The build is checked through the schema: the previous build refuses an unknown field.
probe = Post (port, 'API.IsAddOnCommandAvailable', {'addOnCommandId': {'commandNamespace': 'TapirCommand', 'commandName': 'CreateBeams'}})
if not probe.get ('result', {}).get ('available'):
    sys.exit ('CreateBeams is not available - load the private Tapir build.')

report = {'project': info, 'fields': []}
created = []


def Record (tool, field, status, detail=''):
    report['fields'].append ({'tool': tool, 'field': field, 'status': status, 'detail': detail})
    print ('{:10} {:6} {:32} {}'.format (status, tool, field, str (detail)[:110]))


def Attributes (attributeType):
    return Tapir ('GetAttributesByType', {'attributeType': attributeType})['attributes']


LAYERS = Attributes ('Layer')
FILLS = Attributes ('Fill')
LINETYPES = Attributes ('Line')
SURFACES = Attributes ('Surface')
MATERIALS = Attributes ('BuildingMaterial')
STORIES = Tapir ('GetStories')


def AnotherOf (attributes, usedIndex=None):
    for attribute in attributes:
        if attribute['index'] != usedIndex:
            return attribute
    return attributes[0]


def Details (elementId):
    return Tapir ('GetDetailsOfElements', {'elements': [{'elementId': elementId}]})['detailsOfElements'][0]


def Same (expected, actual):
    """expected may name only some members of an object: only those are compared."""
    if isinstance (expected, dict):
        return isinstance (actual, dict) and all (Same (v, actual.get (k)) for k, v in expected.items ())
    if isinstance (expected, list):
        return isinstance (actual, list) and len (expected) == len (actual) and all (Same (a, b) for a, b in zip (expected, actual))
    if isinstance (expected, (int, float)) and not isinstance (expected, bool) and isinstance (actual, (int, float)):
        return abs (expected - actual) < 1e-6
    return expected == actual


def Created (result, what):
    item = result['elements'][0]
    if 'elementId' not in item:
        raise RuntimeError ('{} failed: {}'.format (what, json.dumps (item)))
    created.append (item['elementId'])
    return item['elementId']


def Failure (executionResult):
    return None if executionResult.get ('success') else json.dumps (executionResult.get ('error', executionResult))


def CheckField (tool, elementId, field, value, modify, readKey=None, readValue=None):
    """One field: Modify, read back, compare. readKey: where Get reports a write-only shorthand."""
    try:
        failure = modify (elementId, {field: value})
    except Exception as e:
        Record (tool, field, 'ERROR', e)
        return
    if failure:
        Record (tool, field, 'ERROR', failure)
        return
    details = Details (elementId)
    if field in ('layerIndex', 'layerId', 'floorIndex'):
        actual = details.get ('layerIndex') if field != 'floorIndex' else details.get ('floorIndex')
        expected = readValue if readValue is not None else value
    else:
        actual = details['details'].get (readKey or field) if 'details' in details else None
        expected = readValue if readValue is not None else value
    if actual is None and 'details' not in details:
        actual = details.get ('typeSpecificDetails', {}).get (readKey or field)
    Record (tool, field, 'PASS' if Same (expected, actual) else 'NOT KEPT', '' if Same (expected, actual) else 'sent {} got {}'.format (expected, actual))


def Tsd (elementId):
    details = Details (elementId)
    return details.get ('details', details.get ('typeSpecificDetails', {}))


# ------------------------------------------------------------------------------------------------
# Beam
# ------------------------------------------------------------------------------------------------

def BeamItem (x, **extra):
    item = {'begCoordinate': {'x': x, 'y': Y0}, 'endCoordinate': {'x': x + 6.0, 'y': Y0}, 'zCoordinate': 3.0}
    item.update (extra)
    return item


def ModifyBeam (elementId, fields):
    item = {'elementId': elementId}
    item.update (fields)
    return Failure (Tapir ('ModifyBeams', {'beamsWithDetails': [item]})['executionResults'][0])


def DisplayFields (lineTypeIndex, fillId, surfaceId):
    return {
        'renovationStatus': 'Demolished',
        'showReferenceAxis': 'HideAlways',
        'showContourLines': 'ByMVO',
        'referencePen': 7,
        'referenceLineTypeIndex': lineTypeIndex,
        'cutContourPen': 8,
        'cutContourLineTypeIndex': lineTypeIndex,
        'uncutLinePen': 9,
        'uncutLineTypeIndex': lineTypeIndex,
        'overheadLinePen': 10,
        'overheadLineTypeIndex': lineTypeIndex,
        'hiddenLinePen': 11,
        'hiddenLineTypeIndex': lineTypeIndex,
        'displayOption': 'OutLinesOnly',
        'viewDepthLimitation': 'EntireElement',
        'uncutProjectionMode': 'Projected',
        'overheadProjectionMode': 'Symbolic',
        'visibility': {'showOnHome': True, 'showAllAbove': False, 'showAllBelow': False, 'showRelAbove': 1, 'showRelBelow': 0},
        'isAutoOnStoryVisibility': False,
        'sequence': 42,
        'isFlipped': True,
        'holeDefaults': {'type': 'Circular', 'showContour': True, 'width': 0.11, 'height': 0.12, 'level': 0.13},
        'cutFillPen': {'overridden': True, 'penIndex': 12},
        'cutFillBackgroundPen': {'overridden': True, 'penIndex': 13},
        'coverFill': {'use': True, 'useFromSurface': False, 'orientationComesFrom3D': False, 'fillId': fillId,
                      'foregroundPen': 14, 'backgroundPen': 15, 'transformationType': 'Rotated'},
    }


def TestBeam ():
    print ('\n--- Beam ---')
    layer = AnotherOf (LAYERS, Details (Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0)]}), 'CreateBeams minimal'))['layerIndex'])
    Record ('beam', 'compatibility: minimal request', 'PASS')
    profile = Attributes ('Profile')
    production = BeamItem (X0 + 10, floorIndex=STORIES['firstStory'] if STORIES['firstStory'] >= 0 else 0, anchorPoint='Center', offset=0.0, isWidthAndHeightLinked=False, width=0.3, height=0.5)
    if profile:
        production['profileId'] = profile[0]['attributeId']
    Created (Tapir ('CreateBeams', {'beamsData': [production]}), 'CreateBeams production-like')
    Record ('beam', 'compatibility: v6_sektor fields', 'PASS')

    lineType = AnotherOf (LINETYPES)['index']
    fillId = AnotherOf (FILLS)['attributeId']
    surfaceId = AnotherOf (SURFACES)['attributeId']
    fields = DisplayFields (lineType, fillId, surfaceId)

    # Everything at creation, in one item.
    item = BeamItem (X0 + 20, layerIndex=layer['index'], **fields)
    item['holes'] = [{'type': 'Rectangular', 'centerX': 1.0, 'centerZ': -0.1, 'width': 0.2, 'height': 0.1}]
    beam = Created (Tapir ('CreateBeams', {'beamsData': [item]}), 'CreateBeams all settings')
    details = Details (beam)
    Record ('beam', 'Create layerIndex', 'PASS' if details.get ('layerIndex') == layer['index'] else 'NOT KEPT', details.get ('layerIndex'))
    tsd = Tsd (beam)
    for field, value in fields.items ():
        Record ('beam', 'Create ' + field, 'PASS' if Same (value, tsd.get (field)) else 'NOT KEPT', '' if Same (value, tsd.get (field)) else 'sent {} got {}'.format (value, tsd.get (field)))
    Record ('beam', 'Create holes', 'PASS' if len (tsd.get ('holes', [])) == 1 else 'NOT KEPT', tsd.get ('holes'))

    # layerId at creation.
    byId = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 30, layerId=layer['attributeId'])]}), 'CreateBeams layerId')
    Record ('beam', 'Create layerId', 'PASS' if Details (byId).get ('layerIndex') == layer['index'] else 'NOT KEPT')

    # Two layer fields at once are refused without creating anything.
    both = Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 35, layerId=layer['attributeId'], layerIndex=layer['index'])]})['elements'][0]
    Record ('beam', 'layerId + layerIndex refused', 'PASS' if 'error' in both else 'NOT KEPT', both)

    # Every field through Modify, on a plain beam.
    plain = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 40)]}), 'CreateBeams plain')
    CheckField ('beam', plain, 'layerIndex', layer['index'], ModifyBeam)
    CheckField ('beam', plain, 'layerId', layer['attributeId'], ModifyBeam, readValue=layer['index'])
    for field, value in fields.items ():
        CheckField ('beam', plain, field, value, ModifyBeam)
    for option in ('ShowAlways', 'HideAlways', 'ByMVO'):
        CheckField ('beam', plain, 'showReferenceAxis', option, ModifyBeam)
        CheckField ('beam', plain, 'showContourLines', option, ModifyBeam)
    for option in ('Standard', 'StandardWithAbstract', 'CutOnly', 'OutLinesOnly', 'AbstractAll', 'CutAll'):
        CheckField ('beam', plain, 'displayOption', option, ModifyBeam)
    for option in ('ToFloorPlanRange', 'ToAbsoluteLimit', 'EntireElement'):
        CheckField ('beam', plain, 'viewDepthLimitation', option, ModifyBeam)
    for option in ('Symbolic', 'Projected'):
        CheckField ('beam', plain, 'uncutProjectionMode', option, ModifyBeam)
        CheckField ('beam', plain, 'overheadProjectionMode', option, ModifyBeam)
    CheckField ('beam', plain, 'isAutoOnStoryVisibility', True, ModifyBeam)
    CheckField ('beam', plain, 'isAutoOnStoryVisibility', False, ModifyBeam)
    CheckField ('beam', plain, 'cutFillPen', {'overridden': False}, ModifyBeam)
    CheckField ('beam', plain, 'cutFillBackgroundPen', {'overridden': False}, ModifyBeam)
    CheckField ('beam', plain, 'coverFill', {'use': False}, ModifyBeam)
    CheckField ('beam', plain, 'level', 0.25, ModifyBeam)
    CheckField ('beam', plain, 'offset', 0.05, ModifyBeam)
    CheckField ('beam', plain, 'profileAngle', 0.1, ModifyBeam)
    CheckField ('beam', plain, 'anchorPoint', 'TopCenter', ModifyBeam)
    CheckField ('beam', plain, 'width', 0.33, ModifyBeam)
    CheckField ('beam', plain, 'height', 0.44, ModifyBeam)
    if STORIES['lastStory'] > STORIES['firstStory']:
        CheckField ('beam', plain, 'floorIndex', STORIES['lastStory'], ModifyBeam)
        CheckField ('beam', plain, 'floorIndex', STORIES['firstStory'], ModifyBeam)

    # Curved shapes.
    curved = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 50, beamShape='HorizontallyCurved', arcAngle=0.5)]}), 'CreateBeams curved')
    Record ('beam', 'Create beamShape', 'PASS' if Tsd (curved).get ('beamShape') == 'HorizontallyCurved' else 'NOT KEPT', Tsd (curved).get ('beamShape'))
    CheckField ('beam', curved, 'beamShape', 'Straight', ModifyBeam)

    # Memo: holes, gables.
    memoBeam = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 60)]}), 'CreateBeams memo')
    failure = ModifyBeam (memoBeam, {'holes': [{'type': 'Circular', 'showContour': True, 'centerX': 2.0, 'centerZ': -0.15, 'width': 0.1},
                                               {'type': 'Rectangular', 'centerX': 3.0, 'centerZ': -0.15, 'width': 0.2, 'height': 0.1}]})
    Record ('beam', 'Modify holes', 'ERROR' if failure else ('PASS' if len (Tsd (memoBeam).get ('holes', [])) == 2 else 'NOT KEPT'), failure or Tsd (memoBeam).get ('holes'))
    gable = {'a': 0.0, 'b': 0.0, 'c': 1.0, 'd': -0.1, 'xb': 0.0, 'xe': 6.0, 'nx': 0.0, 'ny': 1.0}
    failure = ModifyBeam (memoBeam, {'gables': [gable]})
    Record ('beam', 'Modify gables', 'ERROR' if failure else ('PASS' if len (Tsd (memoBeam).get ('gables', [])) == 1 else 'NOT KEPT'), failure or Tsd (memoBeam).get ('gables'))

    # Memo: one segment, all fields.
    material = {'overridden': True, 'attributeId': surfaceId}
    segmentFields = {
        'width': 0.31, 'height': 0.41, 'isWidthAndHeightLinked': False, 'isHomogeneous': False, 'endWidth': 0.21, 'endHeight': 0.22,
        'isEndWidthAndHeightLinked': False, 'circleBased': False, 'buildingMaterialId': AnotherOf (MATERIALS)['attributeId'],
        'leftMaterial': material, 'topMaterial': material, 'rightMaterial': material, 'bottomMaterial': material, 'endsMaterial': material,
        'extrusionMaterial': material, 'materialsChained': False,
    }
    for field, value in segmentFields.items ():
        failure = ModifyBeam (memoBeam, {'allSegments': {field: value}})
        actual = (Tsd (memoBeam).get ('segments') or [{}])[0].get (field)
        Record ('beam', 'allSegments.' + field, 'ERROR' if failure else ('PASS' if Same (value, actual) else 'NOT KEPT'), failure or ('' if Same (value, actual) else 'got {}'.format (actual)))

    # Memo: segment count, cuts and schemes.
    failure = ModifyBeam (memoBeam, {'segments': [{'width': 0.3}, {'width': 0.2}, {'width': 0.1}]})
    tsd = Tsd (memoBeam)
    Record ('beam', 'segments resize 1 -> 3', 'ERROR' if failure else ('PASS' if len (tsd.get ('segments', [])) == 3 and tsd.get ('nSegments') == 3 else 'NOT KEPT'), failure or tsd.get ('nSegments'))
    count = len (tsd.get ('segments', []))
    if count:
        cuts = [{'type': 'Custom', 'customAngle': 0.2}] * (count + 1)
        failure = ModifyBeam (memoBeam, {'cuts': cuts})
        Record ('beam', 'cuts', 'ERROR' if failure else ('PASS' if Same (cuts, Tsd (memoBeam).get ('cuts')) else 'NOT KEPT'), failure or Tsd (memoBeam).get ('cuts'))
        schemes = [{'lengthType': 'Proportional', 'lengthProportion': 1.0 / count}] * count
        failure = ModifyBeam (memoBeam, {'schemes': schemes})
        Record ('beam', 'schemes', 'ERROR' if failure else ('PASS' if Same (schemes, Tsd (memoBeam).get ('schemes')) else 'NOT KEPT'), failure or Tsd (memoBeam).get ('schemes'))
        wrong = ModifyBeam (memoBeam, {'cuts': cuts[:-1]})
        Record ('beam', 'cuts wrong count refused', 'PASS' if wrong else 'NOT KEPT', wrong)

    # Segments at creation.
    multi = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 70, segments=[{'width': 0.4, 'height': 0.5}, {'width': 0.2, 'height': 0.3}],
                                                                    gables=[gable])]}), 'CreateBeams segments')
    tsd = Tsd (multi)
    Record ('beam', 'Create segments', 'PASS' if len (tsd.get ('segments', [])) == 2 else 'NOT KEPT', tsd.get ('nSegments'))
    Record ('beam', 'Create gables', 'PASS' if len (tsd.get ('gables', [])) == 1 else 'NOT KEPT', tsd.get ('gables'))

    # Round trip: what Get reports for a segment is accepted back.
    tsd = Tsd (plain)
    failure = ModifyBeam (plain, {'segments': tsd.get ('segments'), 'cuts': tsd.get ('cuts'), 'schemes': tsd.get ('schemes'), 'coverFill': tsd.get ('coverFill')})
    Record ('beam', 'round trip of Get', 'ERROR' if failure else 'PASS', failure or '')

    # favoriteName: the error for an unknown favorite proves that it is read at creation.
    missing = Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 80, favoriteName='no such favorite xyz')]})['elements'][0]
    Record ('beam', 'favoriteName is read', 'PASS' if 'error' in missing else 'NOT KEPT', missing)


# ------------------------------------------------------------------------------------------------
# Mesh
# ------------------------------------------------------------------------------------------------

def MeshItem (x, **extra):
    item = {'polygonCoordinates': [{'x': x, 'y': Y0 + 20, 'z': 0.0}, {'x': x + 8, 'y': Y0 + 20, 'z': 0.5},
                                   {'x': x + 8, 'y': Y0 + 28, 'z': 1.0}, {'x': x, 'y': Y0 + 28, 'z': 0.2}]}
    item.update (extra)
    return item


def ModifyMesh (elementId, fields):
    return Failure (Tapir ('ModifyMeshes', {'meshesData': [{'elementId': elementId, 'meshData': fields}]})['executionResults'][0])


def TestMesh ():
    print ('\n--- Mesh ---')
    first = Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (X0)]}), 'CreateMeshes minimal')
    layer = AnotherOf (LAYERS, Details (first)['layerIndex'])
    Record ('mesh', 'compatibility: minimal request', 'PASS')
    material = AnotherOf (MATERIALS)['attributeId']
    Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (X0 + 10, floorIndex=0, level=0.1, skirtType='SolidBodyWithSkirt', skirtLevel=0.5, ridges='AllSmooth', showLines=False, buildingMaterialId=material)]}), 'CreateMeshes production-like')
    Record ('mesh', 'compatibility: v6_sektor fields', 'PASS')

    fillId = AnotherOf (FILLS)['attributeId']
    surface = {'overridden': True, 'attributeId': AnotherOf (SURFACES)['attributeId']}
    visibility = {'showOnHome': True, 'showAllAbove': False, 'showAllBelow': False, 'showRelAbove': 1, 'showRelBelow': 0}
    coverFill = {'use': True, 'foregroundPen': 14, 'backgroundPen': 15, 'fillId': fillId, 'useLocalOrigin': True, 'localOrigin': {'x': X0, 'y': Y0}, 'angle': 0.3}
    fields = {
        'renovationStatus': 'Demolished',
        'contourVisibility': visibility,
        'fillVisibility': visibility,
        'sectionContourPen': 16,
        'cutFillPen': {'overridden': True, 'penIndex': 12},
        'cutFillBackgroundPen': {'overridden': True, 'penIndex': 13},
        'topMaterial': surface,
        'sideMaterial': surface,
        'bottomMaterial': surface,
        'materialsChained': False,
        'coverFill': coverFill,
    }

    # Everything at creation.
    mesh = Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (X0 + 20, layerIndex=layer['index'], **fields)]}), 'CreateMeshes all settings')
    details = Details (mesh)
    Record ('mesh', 'Create layerIndex', 'PASS' if details.get ('layerIndex') == layer['index'] else 'NOT KEPT', details.get ('layerIndex'))
    tsd = Tsd (mesh)
    for field, value in fields.items ():
        Record ('mesh', 'Create ' + field, 'PASS' if Same (value, tsd.get (field)) else 'NOT KEPT', '' if Same (value, tsd.get (field)) else 'sent {} got {}'.format (value, tsd.get (field)))

    byId = Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (X0 + 30, layerId=layer['attributeId'])]}), 'CreateMeshes layerId')
    Record ('mesh', 'Create layerId', 'PASS' if Details (byId).get ('layerIndex') == layer['index'] else 'NOT KEPT')

    shorthand = Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (X0 + 35, visibility=visibility)]}), 'CreateMeshes visibility')
    tsd = Tsd (shorthand)
    kept = Same (visibility, tsd.get ('contourVisibility')) and Same (visibility, tsd.get ('fillVisibility'))
    Record ('mesh', 'Create visibility (both)', 'PASS' if kept else 'NOT KEPT', tsd.get ('contourVisibility'))

    # Every field through Modify, on a plain mesh.
    plain = Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (X0 + 40)]}), 'CreateMeshes plain')
    CheckField ('mesh', plain, 'layerIndex', layer['index'], ModifyMesh)
    CheckField ('mesh', plain, 'layerId', layer['attributeId'], ModifyMesh, readValue=layer['index'])
    for field, value in fields.items ():
        CheckField ('mesh', plain, field, value, ModifyMesh)
    CheckField ('mesh', plain, 'visibility', {'showRelAbove': 2}, ModifyMesh, readKey='fillVisibility')
    CheckField ('mesh', plain, 'buildingMaterialId', material, ModifyMesh)
    for option in ('AllSharp', 'AllSmooth', 'UserDefined'):
        CheckField ('mesh', plain, 'ridges', option, ModifyMesh)
    for option in ('SurfaceOnlyWithoutSkirt', 'WithSkirt', 'SolidBodyWithSkirt'):
        CheckField ('mesh', plain, 'skirtType', option, ModifyMesh)
    CheckField ('mesh', plain, 'skirtLevel', 0.75, ModifyMesh)
    CheckField ('mesh', plain, 'level', 0.25, ModifyMesh)
    CheckField ('mesh', plain, 'showLines', True, ModifyMesh)
    CheckField ('mesh', plain, 'contourPen', 5, ModifyMesh)
    CheckField ('mesh', plain, 'levelPen', 6, ModifyMesh)
    CheckField ('mesh', plain, 'lineTypeIndex', AnotherOf (LINETYPES)['index'], ModifyMesh)
    CheckField ('mesh', plain, 'cutFillPen', {'overridden': False}, ModifyMesh)
    CheckField ('mesh', plain, 'topMaterial', {'overridden': False}, ModifyMesh)
    CheckField ('mesh', plain, 'coverFill', {'use': False}, ModifyMesh)

    # Memo: sublines, polygon, vertex ids.
    x = X0 + 40
    sublines = [{'coordinates': [{'x': x + 2, 'y': Y0 + 22, 'z': 1.5}, {'x': x + 6, 'y': Y0 + 26, 'z': 1.5}]}]
    failure = ModifyMesh (plain, {'sublines': sublines})
    tsd = Tsd (plain)
    Record ('mesh', 'Modify sublines', 'ERROR' if failure else ('PASS' if len (tsd.get ('sublines', [])) == 1 else 'NOT KEPT'), failure or tsd.get ('sublines'))
    Record ('mesh', 'polygonVertexIds (read)', 'PASS' if tsd.get ('polygonVertexIds') else 'NOT KEPT', tsd.get ('polygonVertexIds'))
    subline = (tsd.get ('sublines') or [{}])[0]
    Record ('mesh', 'sublines.vertexIds (read)', 'PASS' if subline.get ('vertexIds') else 'NOT KEPT', subline.get ('vertexIds'))
    failure = ModifyMesh (plain, {'polygonCoordinates': MeshItem (x + 1)['polygonCoordinates']})
    Record ('mesh', 'Modify polygonCoordinates', 'ERROR' if failure else 'PASS', failure or '')

    # Round trip of the fields a Get reports.
    tsd = Tsd (plain)
    failure = ModifyMesh (plain, {key: tsd[key] for key in ('contourVisibility', 'fillVisibility', 'coverFill', 'topMaterial', 'sideMaterial', 'bottomMaterial', 'cutFillPen', 'cutFillBackgroundPen', 'sublines') if key in tsd})
    Record ('mesh', 'round trip of Get', 'ERROR' if failure else 'PASS', failure or '')


# ------------------------------------------------------------------------------------------------
# Story filter and story switch
# ------------------------------------------------------------------------------------------------

def TestFilter ():
    print ('\n--- Filter by story ---')
    lastStory, firstStory = STORIES['lastStory'], STORIES['firstStory']
    if lastStory == firstStory:
        Record ('filter', 'floorIndex', 'SKIPPED', 'the project has one story')
        return
    low = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 90, floorIndex=firstStory)]}), 'beam on first story')
    high = Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (X0 + 91, floorIndex=lastStory)]}), 'beam on last story')
    before = Tapir ('GetStories')['actStory']
    guids = lambda r: {e['elementId']['guid'] for e in r['elements']}
    onLast = guids (Tapir ('GetElementsByType', {'elementType': 'Beam', 'floorIndex': lastStory}))
    Record ('filter', 'GetElementsByType floorIndex', 'PASS' if high['guid'] in onLast and low['guid'] not in onLast else 'NOT KEPT')
    onLast = guids (Tapir ('GetAllElements', {'floorIndex': lastStory}))
    Record ('filter', 'GetAllElements floorIndex', 'PASS' if high['guid'] in onLast and low['guid'] not in onLast else 'NOT KEPT')
    both = [{'elementId': low}, {'elementId': high}]
    filtered = guids (Tapir ('FilterElements', {'elements': both, 'floorIndex': firstStory}))
    Record ('filter', 'FilterElements floorIndex', 'PASS' if filtered == {low['guid']} else 'NOT KEPT', filtered)
    Record ('filter', 'active story unchanged', 'PASS' if Tapir ('GetStories')['actStory'] == before else 'NOT KEPT')


def TestStory ():
    print ('\n--- Story switch ---')
    stories = Tapir ('GetStories')
    if stories['lastStory'] == stories['firstStory']:
        Record ('story', 'ChangeWindow storyIndex', 'SKIPPED', 'the project has one story')
        return
    before = stories['actStory']
    target = stories['firstStory'] if before != stories['firstStory'] else stories['lastStory']
    result = Tapir ('ChangeWindow', {'windowType': 'FloorPlan', 'storyIndex': target})
    actual = Tapir ('GetStories')['actStory']
    Record ('story', 'ChangeWindow storyIndex', 'PASS' if actual == target else 'NOT KEPT', result)
    Tapir ('ChangeWindow', {'windowType': 'FloorPlan', 'storyIndex': before})


only = {part.strip () for part in args.only.split (',')}
try:
    if 'beam' in only:
        TestBeam ()
    if 'mesh' in only:
        TestMesh ()
    if 'filter' in only:
        TestFilter ()
    if 'story' in only:
        TestStory ()
finally:
    if args.cleanup and created:
        reason = RefusalReason (ProjectInfo (port))
        if reason is None:
            Tapir ('DeleteElements', {'elements': [{'elementId': e} for e in created]})
    outDir = os.path.join (os.path.dirname (os.path.abspath (__file__)), 'out')
    os.makedirs (outDir, exist_ok=True)
    outPath = os.path.join (outDir, 'beam_mesh_coverage.json')
    with open (outPath, 'w', encoding='utf-8') as f:
        json.dump (report, f, indent=1, ensure_ascii=False)
    statuses = {}
    for row in report['fields']:
        statuses[row['status']] = statuses.get (row['status'], 0) + 1
    print ('\nSummary: {}\nTable: {}'.format (statuses, outPath))
    bad = [row for row in report['fields'] if row['status'] in ('ERROR', 'NOT KEPT')]
    for row in bad:
        print ('  {} {} {}: {}'.format (row['status'], row['tool'], row['field'], str (row['detail'])[:160]))

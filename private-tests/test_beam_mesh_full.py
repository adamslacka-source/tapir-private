"""
Full coverage test of the Beam and Mesh tools (docs/private/beam-mesh-field-map.md): CreateBeams,
ModifyBeams, CreateMeshes, ModifyMeshes, GetDetailsOfElements, the floorIndex filter of
GetElementsByType / GetAllElements / FilterElements and the story switch of ChangeWindow.

Run it after the Archicad restart, ONLY on a scratch new file or on a copy of a project:

    python test_beam_mesh_full.py --self-check          # the project guard only, no connection
    python test_beam_mesh_full.py [--port 19723] [--cleanup] [--only beam,mesh,layers,filter,story]

It refuses to run if the project is Kamenne.pln or cesty.pln, if the project cannot be identified,
or if it cannot decide which of several running Archicad instances to use. The project is checked
again before every write command. Nothing is saved.

Steps for each tool:
  1. compatibility: the requests of the production scripts (v6_sektor) still create the element,
  2. every field on its own fresh element, through Create and through Modify, then read back:
     PASS (kept), LIMIT (a measured Archicad limit), NOT KEPT (a new finding), ERROR (refused),
  3. memo data: beam holes, segments, cuts, schemes, gables; mesh polygon and sublines,
  4. hidden and locked layers, the story filter, the story switch and on which stories the
     elements really are (OnActualFloor).
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
parser.add_argument ('--only', default='beam,mesh,layers,filter,story', help='comma separated: beam, mesh, layers, filter, story')
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

WRITE_COMMANDS = ('CreateBeams', 'ModifyBeams', 'CreateMeshes', 'ModifyMeshes', 'DeleteElements', 'ChangeWindow', 'CreateLayers', 'SetDetailsOfElements')


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
counter = [0]


def Record (tool, field, status, detail=''):
    report['fields'].append ({'tool': tool, 'field': field, 'status': status, 'detail': str (detail)})
    print ('{:10} {:6} {:36} {}'.format (status, tool, field, str (detail)[:110]))


class Guarded:
    """A failing step is one ERROR row; the next steps still run."""

    def __init__ (self, tool, step):
        self.tool, self.step = tool, step

    def __enter__ (self):
        return self

    def __exit__ (self, excType, exc, tb):
        if exc is not None and not issubclass (excType, SystemExit):
            Record (self.tool, self.step, 'ERROR', exc)
            return True
        return False


def Attributes (attributeType):
    return Tapir ('GetAttributesByType', {'attributeType': attributeType})['attributes']


LAYERS = Attributes ('Layer')
FILLS = Attributes ('Fill')
LINETYPES = Attributes ('Line')
SURFACES = Attributes ('Surface')
MATERIALS = Attributes ('BuildingMaterial')
PROFILES = Attributes ('Profile')
STORIES = Tapir ('GetStories')

# Limits measured on AC29: Archicad changes the value, the command says so in 'warnings'.
LIMITS = {
    ('beam', 'showReferenceAxis', 'ShowAlways'): 'Archicad: becomes ByMVO',
    ('beam', 'showContourLines', 'HideAlways'): 'Archicad: becomes ByMVO',
    ('beam', 'sequence'): 'Archicad replaces the value',
    ('beam', 'holeDefaults'): 'Archicad does not store it on a placed beam',
    ('beam', 'beamShape', 'HorizontallyCurved'): 'Archicad derives the shape: it needs arcAngle',
}


def IsLimit (tool, field, value):
    return (tool, field) in LIMITS or (isinstance (value, str) and (tool, field, value) in LIMITS)


def AnotherOf (attributes, usedIndex=None):
    for attribute in attributes:
        if attribute['index'] != usedIndex:
            return attribute
    return attributes[0]


def Details (elementId):
    return Tapir ('GetDetailsOfElements', {'elements': [{'elementId': elementId}]})['detailsOfElements'][0]


def Tsd (elementId):
    return Details (elementId).get ('details', {})


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
    report.setdefault ('createWarnings', []).extend (item.get ('warnings', []))
    return item['elementId']


def NextX ():
    counter[0] += 1
    return X0 + 10.0 * counter[0]


def BeamItem (x, **extra):
    item = {'begCoordinate': {'x': x, 'y': Y0}, 'endCoordinate': {'x': x + 6.0, 'y': Y0}, 'zCoordinate': 3.0}
    item.update (extra)
    return item


def MeshItem (x, **extra):
    item = {'polygonCoordinates': [{'x': x, 'y': Y0 + 20, 'z': 0.0}, {'x': x + 8, 'y': Y0 + 20, 'z': 0.5},
                                   {'x': x + 8, 'y': Y0 + 28, 'z': 1.0}, {'x': x, 'y': Y0 + 28, 'z': 0.2}]}
    item.update (extra)
    return item


def NewBeam (**extra):
    return Created (Tapir ('CreateBeams', {'beamsData': [BeamItem (NextX (), **extra)]}), 'CreateBeams')


def NewMesh (**extra):
    return Created (Tapir ('CreateMeshes', {'meshesData': [MeshItem (NextX (), **extra)]}), 'CreateMeshes')


def ModifyBeamResult (elementId, fields):
    return Tapir ('ModifyBeams', {'beamsWithDetails': [dict (elementId=elementId, **fields)]})['executionResults'][0]


def ModifyMeshResult (elementId, fields):
    return Tapir ('ModifyMeshes', {'meshesData': [{'elementId': elementId, 'meshData': fields}]})['executionResults'][0]


FRESH = {'beam': NewBeam, 'mesh': NewMesh}
MODIFY = {'beam': ModifyBeamResult, 'mesh': ModifyMeshResult}


def Rated (tool, field, sent, expected, actual, warnings=()):
    if Same (expected, actual):
        Record (tool, field, 'PASS', 'unexpected warnings {}'.format (list (warnings)) if warnings else '')
    elif IsLimit (tool, re.sub (r'^(Create|Modify) ', '', field).split ('=')[0], sent):
        Record (tool, field, 'LIMIT', 'sent {} got {}; warned: {}'.format (sent, actual, bool (warnings)))
    else:
        Record (tool, field, 'NOT KEPT', 'sent {} got {}; warned: {}'.format (sent, actual, bool (warnings)))


def CheckModify (tool, field, value, setup=None, readKey=None, readValue=None):
    """One field on its own fresh element: a failure cannot cascade to the next field."""
    label = 'Modify {}={}'.format (field, json.dumps (value)[:28])
    with Guarded (tool, label):
        element = FRESH[tool] (**(setup or {}))
        result = MODIFY[tool] (element, {field: value})
        if not result.get ('success'):
            Record (tool, label, 'ERROR', json.dumps (result.get ('error')))
            return
        expected = value if readValue is None else readValue
        details = Details (element)
        if field in ('layerIndex', 'layerId'):
            actual = details.get ('layerIndex')
        elif field == 'floorIndex':
            actual = details.get ('floorIndex')
        else:
            actual = details.get ('details', {}).get (readKey or field)
        Rated (tool, label, value, expected, actual, result.get ('warnings', ()))


def CheckCreate (tool, field, value, setup=None, readKey=None, readValue=None):
    label = 'Create {}={}'.format (field, json.dumps (value)[:28])
    with Guarded (tool, label):
        fields = dict (setup or {})
        fields[field] = value
        element = FRESH[tool] (**fields)
        expected = value if readValue is None else readValue
        details = Details (element)
        actual = details.get ('layerIndex') if field in ('layerIndex', 'layerId') else details.get ('details', {}).get (readKey or field)
        Rated (tool, label, value, expected, actual)


def CheckBoth (tool, field, value, setup=None, readKey=None, readValue=None):
    CheckCreate (tool, field, value, setup, readKey, readValue)
    CheckModify (tool, field, value, setup, readKey, readValue)


# ------------------------------------------------------------------------------------------------
# Beam
# ------------------------------------------------------------------------------------------------

def TestBeam ():
    print ('\n--- Beam ---')
    with Guarded ('beam', 'compatibility: minimal request'):
        NewBeam ()
        Record ('beam', 'compatibility: minimal request', 'PASS')
    with Guarded ('beam', 'compatibility: v6_sektor fields'):
        item = dict (floorIndex=0, anchorPoint='Center', offset=0.0, isWidthAndHeightLinked=False, profileAngle=0.05, slantAngle=0.05)
        if PROFILES:
            item['profileId'] = PROFILES[0]['attributeId']
        NewBeam (**item)
        Record ('beam', 'compatibility: v6_sektor fields', 'PASS')

    # Priority: the axis on a profiled beam.
    profile = {'profileId': PROFILES[0]['attributeId']} if PROFILES else {}
    for option in ('HideAlways', 'ByMVO', 'ShowAlways'):
        CheckBoth ('beam', 'showReferenceAxis', option, setup=profile)
    for option in ('ShowAlways', 'ByMVO', 'HideAlways'):
        CheckBoth ('beam', 'showContourLines', option, setup=profile)

    # Priority: visibility. An explicit one needs the automatic one off.
    manual = {'isAutoOnStoryVisibility': False}
    CheckCreate ('beam', 'isAutoOnStoryVisibility', False)
    CheckModify ('beam', 'isAutoOnStoryVisibility', False)
    CheckModify ('beam', 'isAutoOnStoryVisibility', True, setup=manual)
    for visibility in ({'showOnHome': True, 'showAllAbove': True, 'showAllBelow': True}, {'showOnHome': True, 'showAllAbove': False, 'showAllBelow': False, 'showRelAbove': 0, 'showRelBelow': 0},
                       {'showOnHome': True, 'showRelAbove': 1, 'showRelBelow': 1}, {'showOnHome': True, 'showAllAbove': True, 'showAllBelow': False}):
        CheckCreate ('beam', 'visibility', visibility, setup=manual)
        CheckModify ('beam', 'visibility', visibility, setup=manual)
        CheckModify ('beam', 'visibility', visibility)

    layer = AnotherOf (LAYERS, Details (NewBeam ()).get ('layerIndex'))
    CheckBoth ('beam', 'layerIndex', layer['index'])
    CheckBoth ('beam', 'layerId', layer['attributeId'], readValue=layer['index'])
    with Guarded ('beam', 'layerId + layerIndex refused'):
        both = Tapir ('CreateBeams', {'beamsData': [BeamItem (NextX (), layerId=layer['attributeId'], layerIndex=layer['index'])]})['elements'][0]
        Record ('beam', 'layerId + layerIndex refused', 'PASS' if 'error' in both else 'NOT KEPT', both)

    for option in ('Standard', 'StandardWithAbstract', 'CutOnly', 'OutLinesOnly', 'AbstractAll', 'CutAll'):
        CheckBoth ('beam', 'displayOption', option)
    for option in ('ToFloorPlanRange', 'ToAbsoluteLimit', 'EntireElement'):
        CheckBoth ('beam', 'viewDepthLimitation', option, setup={'displayOption': 'StandardWithAbstract'})
    for option in ('Symbolic', 'Projected'):
        CheckBoth ('beam', 'uncutProjectionMode', option, setup={'displayOption': 'Standard'})
        CheckBoth ('beam', 'overheadProjectionMode', option, setup={'displayOption': 'Standard'})

    fillId = AnotherOf (FILLS)['attributeId']
    coverFill = {'use': True, 'useFromSurface': False, 'orientationComesFrom3D': False, 'fillId': fillId, 'foregroundPen': 14, 'backgroundPen': 15,
                 'transformationType': 'Distorted', 'transformation': {'origin': {'x': 0.5, 'y': 0.25}, 'xAxis': {'x': 0.8, 'y': 0.6}, 'yAxis': {'x': -0.6, 'y': 0.8}}}
    CheckBoth ('beam', 'coverFill', coverFill)
    CheckModify ('beam', 'coverFill', {'use': False})

    lineType = AnotherOf (LINETYPES)['index']
    for field, value in (('referencePen', 7), ('referenceLineTypeIndex', lineType), ('cutContourPen', 8), ('cutContourLineTypeIndex', lineType), ('uncutLinePen', 9), ('uncutLineTypeIndex', lineType),
                         ('overheadLinePen', 10), ('overheadLineTypeIndex', lineType), ('hiddenLinePen', 11), ('hiddenLineTypeIndex', lineType), ('renovationStatus', 'Demolished'),
                         ('cutFillPen', {'overridden': True, 'penIndex': 12}), ('cutFillBackgroundPen', {'overridden': True, 'penIndex': 13}), ('isFlipped', True),
                         ('sequence', 42), ('holeDefaults', {'type': 'Circular', 'showContour': True, 'width': 0.11, 'height': 0.12, 'level': 0.13})):
        CheckBoth ('beam', field, value)
    CheckModify ('beam', 'cutFillPen', {'overridden': False}, setup={'cutFillPen': {'overridden': True, 'penIndex': 12}})
    for field, value in (('level', 0.25), ('offset', 0.05), ('profileAngle', 0.1), ('anchorPoint', 'TopCenter'), ('width', 0.33), ('height', 0.44)):
        CheckModify ('beam', field, value)
    if STORIES['lastStory'] > STORIES['firstStory']:
        CheckModify ('beam', 'floorIndex', STORIES['lastStory'])
    CheckCreate ('beam', 'beamShape', 'HorizontallyCurved', setup={'arcAngle': 0.5})
    CheckModify ('beam', 'beamShape', 'Straight', setup={'beamShape': 'HorizontallyCurved', 'arcAngle': 0.5})
    CheckModify ('beam', 'beamShape', 'HorizontallyCurved', setup={})

    # Memo.
    holes = [{'type': 'Circular', 'showContour': True, 'centerX': 2.0, 'centerZ': -0.15, 'width': 0.1}, {'type': 'Rectangular', 'centerX': 3.0, 'centerZ': -0.15, 'width': 0.2, 'height': 0.1}]
    CheckBoth ('beam', 'holes', holes)
    gable = {'a': 0.0, 'b': 0.0, 'c': 1.0, 'd': 0.5, 'xb': -100.0, 'xe': 100.0, 'nx': 1.0, 'ny': 0.0}
    CheckCreate ('beam', 'gables', [gable])
    with Guarded ('beam', 'Modify gables is refused and keeps the beam'):
        beam = NewBeam ()
        result = ModifyBeamResult (beam, {'gables': [gable]})
        kept = 'details' in Details (beam)
        Record ('beam', 'Modify gables is refused and keeps the beam', 'PASS' if (not result.get ('success')) and kept else 'NOT KEPT', 'beam kept: {}, {}'.format (kept, result))

    surfaceId = AnotherOf (SURFACES)['attributeId']
    material = {'overridden': True, 'attributeId': surfaceId}
    for field, value in (('width', 0.31), ('height', 0.41), ('isWidthAndHeightLinked', False), ('isHomogeneous', False), ('endWidth', 0.21), ('endHeight', 0.22), ('isEndWidthAndHeightLinked', False),
                         ('circleBased', False), ('buildingMaterialId', AnotherOf (MATERIALS)['attributeId']), ('leftMaterial', material), ('topMaterial', material), ('rightMaterial', material),
                         ('bottomMaterial', material), ('endsMaterial', material), ('extrusionMaterial', material), ('materialsChained', False)):
        label = 'Modify allSegments.' + field
        with Guarded ('beam', label):
            beam = NewBeam (**({'allSegments': {'isHomogeneous': False}} if field in ('endWidth', 'endHeight') else {}))
            result = ModifyBeamResult (beam, {'allSegments': {field: value}})
            actual = (Tsd (beam).get ('segments') or [{}])[0].get (field)
            Rated ('beam', label, value, value, actual) if result.get ('success') else Record ('beam', label, 'ERROR', result)

    with Guarded ('beam', 'segments, cuts, schemes'):
        beam = NewBeam ()
        result = ModifyBeamResult (beam, {'segments': [{'width': 0.3}, {'width': 0.2}, {'width': 0.1}]})
        Record ('beam', 'Modify segments resize 1 to 3', 'PASS' if result.get ('success') and Tsd (beam).get ('nSegments') == 3 else 'NOT KEPT', result)
        cuts = [{'type': 'Custom', 'customAngle': 0.2}] * 4
        ModifyBeamResult (beam, {'cuts': cuts})
        Rated ('beam', 'Modify cuts', cuts, cuts, Tsd (beam).get ('cuts'))
        schemes = [{'lengthType': 'Fixed', 'fixedLength': 2.0}] * 3
        ModifyBeamResult (beam, {'schemes': schemes})
        Rated ('beam', 'Modify schemes Fixed', schemes, schemes, Tsd (beam).get ('schemes'))
        wrong = ModifyBeamResult (beam, {'cuts': cuts[:-1]})
        Record ('beam', 'cuts wrong count refused', 'PASS' if not wrong.get ('success') else 'NOT KEPT', wrong)
        tsd = Tsd (beam)
        result = ModifyBeamResult (beam, {'segments': tsd.get ('segments'), 'cuts': tsd.get ('cuts'), 'schemes': tsd.get ('schemes'), 'coverFill': tsd.get ('coverFill')})
        Record ('beam', 'round trip of Get', 'PASS' if result.get ('success') else 'ERROR', result)
    with Guarded ('beam', 'Create segments'):
        beam = NewBeam (segments=[{'width': 0.4, 'height': 0.5}, {'width': 0.2, 'height': 0.3}])
        Record ('beam', 'Create segments', 'PASS' if len (Tsd (beam).get ('segments', [])) == 2 else 'NOT KEPT', Tsd (beam).get ('nSegments'))
    with Guarded ('beam', 'favoriteName is read'):
        missing = Tapir ('CreateBeams', {'beamsData': [BeamItem (NextX (), favoriteName='no such favorite xyz')]})['elements'][0]
        Record ('beam', 'favoriteName is read', 'PASS' if 'error' in missing else 'NOT KEPT', missing)


# ------------------------------------------------------------------------------------------------
# Mesh
# ------------------------------------------------------------------------------------------------

def TestMesh ():
    print ('\n--- Mesh ---')
    with Guarded ('mesh', 'compatibility: minimal request'):
        NewMesh ()
        Record ('mesh', 'compatibility: minimal request', 'PASS')
    material = AnotherOf (MATERIALS)['attributeId']
    with Guarded ('mesh', 'compatibility: v6_sektor fields'):
        NewMesh (floorIndex=0, level=0.1, skirtType='SolidBodyWithSkirt', skirtLevel=0.5, ridges='AllSmooth', showLines=False, buildingMaterialId=material)
        Record ('mesh', 'compatibility: v6_sektor fields', 'PASS')

    for visibility in ({'showOnHome': True, 'showAllAbove': True, 'showAllBelow': True}, {'showOnHome': True, 'showRelAbove': 1, 'showRelBelow': 1}, {'showOnHome': True, 'showAllAbove': False, 'showAllBelow': False, 'showRelAbove': 0, 'showRelBelow': 0}):
        CheckBoth ('mesh', 'contourVisibility', visibility)
        CheckBoth ('mesh', 'fillVisibility', visibility)
        CheckBoth ('mesh', 'visibility', visibility, readKey='contourVisibility')
        CheckBoth ('mesh', 'visibility', visibility, readKey='fillVisibility')
    CheckBoth ('mesh', 'fillVisibility', {'showOnHome': False, 'showAllBelow': True})

    layer = AnotherOf (LAYERS, Details (NewMesh ()).get ('layerIndex'))
    CheckBoth ('mesh', 'layerIndex', layer['index'])
    CheckBoth ('mesh', 'layerId', layer['attributeId'], readValue=layer['index'])

    fillId = AnotherOf (FILLS)['attributeId']
    CheckBoth ('mesh', 'coverFill', {'use': True, 'foregroundPen': 14, 'backgroundPen': 15, 'fillId': fillId})
    CheckBoth ('mesh', 'coverFill', {'useLocalOrigin': True, 'localOrigin': {'x': X0, 'y': Y0}, 'angle': 0.3})
    CheckModify ('mesh', 'coverFill', {'use': False})

    surface = {'overridden': True, 'attributeId': AnotherOf (SURFACES)['attributeId']}
    for field, value in (('renovationStatus', 'Demolished'), ('sectionContourPen', 16), ('cutFillPen', {'overridden': True, 'penIndex': 12}), ('cutFillBackgroundPen', {'overridden': True, 'penIndex': 13}),
                         ('topMaterial', surface), ('sideMaterial', surface), ('bottomMaterial', surface), ('materialsChained', False)):
        CheckBoth ('mesh', field, value)
    CheckModify ('mesh', 'topMaterial', {'overridden': False}, setup={'topMaterial': surface})
    CheckModify ('mesh', 'buildingMaterialId', material)
    for option in ('AllSharp', 'AllSmooth', 'UserDefined'):
        CheckBoth ('mesh', 'ridges', option)
    for option in ('SurfaceOnlyWithoutSkirt', 'WithSkirt', 'SolidBodyWithSkirt'):
        CheckBoth ('mesh', 'skirtType', option)
    for field, value in (('skirtLevel', 0.75), ('level', 0.25), ('showLines', True), ('contourPen', 5), ('levelPen', 6), ('lineTypeIndex', AnotherOf (LINETYPES)['index'])):
        CheckBoth ('mesh', field, value)

    with Guarded ('mesh', 'sublines, polygon, vertex ids'):
        mesh = NewMesh ()
        x = X0 + 10.0 * counter[0]
        result = ModifyMeshResult (mesh, {'sublines': [{'coordinates': [{'x': x + 2, 'y': Y0 + 22, 'z': 1.5}, {'x': x + 6, 'y': Y0 + 26, 'z': 1.5}]}]})
        tsd = Tsd (mesh)
        Record ('mesh', 'Modify sublines', 'PASS' if result.get ('success') and len (tsd.get ('sublines', [])) == 1 else 'NOT KEPT', result)
        Record ('mesh', 'polygonVertexIds (read)', 'PASS' if tsd.get ('polygonVertexIds') else 'NOT KEPT', tsd.get ('polygonVertexIds'))
        Record ('mesh', 'sublines.vertexIds (read)', 'PASS' if (tsd.get ('sublines') or [{}])[0].get ('vertexIds') else 'NOT KEPT')
        result = ModifyMeshResult (mesh, {'polygonCoordinates': MeshItem (x + 1)['polygonCoordinates']})
        Record ('mesh', 'Modify polygonCoordinates', 'PASS' if result.get ('success') else 'ERROR', result)
        tsd = Tsd (mesh)
        result = ModifyMeshResult (mesh, {key: tsd[key] for key in ('contourVisibility', 'fillVisibility', 'coverFill', 'topMaterial', 'sideMaterial', 'bottomMaterial', 'cutFillPen', 'cutFillBackgroundPen', 'sublines') if key in tsd})
        Record ('mesh', 'round trip of Get', 'PASS' if result.get ('success') else 'ERROR', result)


# ------------------------------------------------------------------------------------------------
# Hidden and locked layers
# ------------------------------------------------------------------------------------------------

def TestLayers ():
    print ('\n--- Hidden and locked layers ---')
    Tapir ('CreateLayers', {'layerDataArray': [{'name': 'T_hidden', 'isHidden': True, 'isLocked': False, 'isWireframe': False},
                                               {'name': 'T_locked', 'isHidden': False, 'isLocked': True, 'isWireframe': False},
                                               {'name': 'T_visible', 'isHidden': False, 'isLocked': False, 'isWireframe': False}], 'overwriteExisting': True})
    layers = {layer['name']: layer for layer in Attributes ('Layer')}
    hidden, locked, visible = layers['T_hidden'], layers['T_locked'], layers['T_visible']
    for tool in ('beam', 'mesh'):
        with Guarded (tool, 'hidden layer'):
            element = FRESH[tool] (layerIndex=hidden['index'])
            Rated (tool, 'Create on hidden layer', hidden['index'], hidden['index'], Details (element).get ('layerIndex'))
            result = MODIFY[tool] (element, {'layerIndex': visible['index']})
            moved = Details (element).get ('layerIndex') == visible['index']
            Record (tool, 'Move off a hidden layer', 'PASS' if moved else 'LIMIT', 'Archicad refuses it: {}'.format (result) if not moved else '')
            viaSet = Tapir ('SetDetailsOfElements', {'elementsWithDetails': [{'elementId': element, 'details': {'layerIndex': visible['index']}}]})['executionResults'][0]
            moved = Details (element).get ('layerIndex') == visible['index']
            Record (tool, 'Move off a hidden layer (SetDetails)', 'PASS' if moved else 'LIMIT', 'Archicad refuses it: {}'.format (viaSet) if not moved else '')
        with Guarded (tool, 'locked layer refused'):
            refused = Tapir ('CreateBeams' if tool == 'beam' else 'CreateMeshes', {('beamsData' if tool == 'beam' else 'meshesData'): [(BeamItem if tool == 'beam' else MeshItem) (NextX (), layerIndex=locked['index'])]})['elements'][0]
            Record (tool, 'locked layer refused', 'PASS' if 'error' in refused else 'NOT KEPT', refused)


# ------------------------------------------------------------------------------------------------
# Story filter, story switch and the real effect of the visibility
# ------------------------------------------------------------------------------------------------

def TestFilter ():
    print ('\n--- Filter by story ---')
    lastStory, firstStory = STORIES['lastStory'], STORIES['firstStory']
    if lastStory == firstStory:
        Record ('filter', 'floorIndex', 'SKIPPED', 'the project has one story')
        return
    with Guarded ('filter', 'floorIndex'):
        low, high = NewBeam (floorIndex=firstStory), NewBeam (floorIndex=lastStory)
        before = Tapir ('GetStories')['actStory']
        guids = lambda r: {e['elementId']['guid'] for e in r['elements']}
        onLast = guids (Tapir ('GetElementsByType', {'elementType': 'Beam', 'floorIndex': lastStory}))
        Record ('filter', 'GetElementsByType floorIndex', 'PASS' if high['guid'] in onLast and low['guid'] not in onLast else 'NOT KEPT')
        onLast = guids (Tapir ('GetAllElements', {'floorIndex': lastStory}))
        Record ('filter', 'GetAllElements floorIndex', 'PASS' if high['guid'] in onLast and low['guid'] not in onLast else 'NOT KEPT')
        filtered = guids (Tapir ('FilterElements', {'elements': [{'elementId': low}, {'elementId': high}], 'floorIndex': firstStory}))
        Record ('filter', 'FilterElements floorIndex', 'PASS' if filtered == {low['guid']} else 'NOT KEPT', filtered)
        Record ('filter', 'active story unchanged', 'PASS' if Tapir ('GetStories')['actStory'] == before else 'NOT KEPT')


def TestStory ():
    print ('\n--- Story switch and visibility per story ---')
    stories = Tapir ('GetStories')
    first, last = stories['firstStory'], stories['lastStory']
    if last - first < 2:
        Record ('story', 'ChangeWindow storyIndex', 'SKIPPED', 'needs three stories')
        return
    before = stories['actStory']
    middle = first + 1
    all3 = {first, middle, first + 2}
    cases = []
    with Guarded ('story', 'create visibility cases'):
        everywhere = {'showOnHome': True, 'showAllAbove': True, 'showAllBelow': True}
        homeOnly = {'showOnHome': True, 'showAllAbove': False, 'showAllBelow': False, 'showRelAbove': 0, 'showRelBelow': 0}
        cases = [('beam Create all', NewBeam (floorIndex=middle, visibility=everywhere), all3), ('beam Create home', NewBeam (floorIndex=middle, visibility=homeOnly), {middle}),
                 ('mesh Create all', NewMesh (floorIndex=middle, visibility=everywhere), all3), ('mesh Create home', NewMesh (floorIndex=middle, visibility=homeOnly), {middle}),
                 ('mesh Create rel below 1', NewMesh (floorIndex=middle, visibility={'showOnHome': True, 'showRelBelow': 1}), {first, middle})]
        beam, mesh = NewBeam (floorIndex=middle, isAutoOnStoryVisibility=False), NewMesh (floorIndex=middle)
        cases += [('beam Modify all', beam, all3), ('mesh Modify all', mesh, all3)]
        ModifyBeamResult (beam, {'visibility': everywhere})
        ModifyMeshResult (mesh, {'visibility': everywhere})
    seen = {name: set () for name, _, _ in cases}
    try:
        for story in sorted (all3):
            result = Tapir ('ChangeWindow', {'windowType': 'FloorPlan', 'storyIndex': story})
            actual = Tapir ('GetStories')['actStory']
            Record ('story', 'ChangeWindow storyIndex {}'.format (story), 'PASS' if actual == story else 'NOT KEPT', result)
            for name, element, _ in cases:
                if Tapir ('FilterElements', {'elements': [{'elementId': element}], 'filters': ['OnActualFloor']})['elements']:
                    seen[name].add (story)
    finally:
        Tapir ('ChangeWindow', {'windowType': 'FloorPlan', 'storyIndex': before})
    for name, _, expected in cases:
        Record ('story', 'shown on ' + name, 'PASS' if seen[name] == expected else 'NOT KEPT', 'expected {} seen {}'.format (sorted (expected), sorted (seen[name])))


only = {part.strip () for part in args.only.split (',')}
sections = [('beam', TestBeam), ('mesh', TestMesh), ('layers', TestLayers), ('filter', TestFilter), ('story', TestStory)]
try:
    for name, section in sections:
        if name in only:
            with Guarded (name, 'section'):
                section ()
finally:
    if args.cleanup and created and RefusalReason (ProjectInfo (port)) is None:
        with Guarded ('cleanup', 'DeleteElements'):
            Tapir ('DeleteElements', {'elements': [{'elementId': e} for e in created]})
    outDir = os.path.join (os.path.dirname (os.path.abspath (__file__)), 'out')
    os.makedirs (outDir, exist_ok=True)
    outPath = os.path.join (outDir, 'beam_mesh_coverage.json')
    with open (outPath, 'w', encoding='utf-8') as f:
        json.dump (report, f, indent=1, ensure_ascii=False, default=str)
    statuses = {}
    for row in report['fields']:
        statuses[row['status']] = statuses.get (row['status'], 0) + 1
    print ('\nSummary: {}\nTable: {}'.format (statuses, outPath))
    bad = [row for row in report['fields'] if row['status'] in ('ERROR', 'NOT KEPT')]
    for row in bad:
        print ('  {} {} {}: {}'.format (row['status'], row['tool'], row['field'], row['detail'][:160]))
sys.exit (1 if bad else 0)

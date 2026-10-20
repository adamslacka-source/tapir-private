"""
Acceptance test of SetRailingSystemDefaults / GetRailingSystemDefaults (ADR-001 R1) and of the
Curtain Wall class surfaces of SetCurtainWallSystemDefaults (ADR-001 nález I5).

    python test_railing_cw_defaults.py                 # Archicad on port 19723
    python test_railing_cw_defaults.py --port 19724
    python test_railing_cw_defaults.py --experimental      # + the opt-in Ext / subelement paths

Run spike_railing_defaults.py first (step by step, read only): the first GetRailingSystemDefaults
crashed Archicad 29 on ACAPI_Element_GetDefaultsExt for the Railing (or right after it). Without
--experimental only the verified plain calls are used (ACAPI_Element_GetDefaults / ChangeDefaults,
Favorites without subElements) - then the GDL parameters and the number of rails are expected in
notKept. Use --experimental only for the steps the spike passed.

Needs Archicad with the Tapir add-on built from this commit (run spike_railing_defaults.py first:
it is read only and says what the Railing defaults carry). Standard library only. Every test
element is placed at x >= 5000 m and deleted at the end, with the test Favorites (also after a
failed check); every defaults command of the test uses restoreDefaults, and the end probes that
the Railing and Curtain Wall defaults are what they were before the test.

  R1 rails:   SetRailingSystemDefaults height 1.0, posts 1.5 m, handrail 1.0, 5 rails evenly
              between 0.1 and 0.9, no infill, Favorite 'FS test rail 5' -> a railing from the
              Favorite has 5 rails per segment whose heights (3D z-range of each RailingRail
              subelement above the railing base, read through GetSubelementsOfHierarchicalElements)
              match +-2 cm, and no visible panel.
  R2 glass:   infill panel, the first Material parameter of the panel object set to a glass
              surface, Favorite 'FS test rail glass' -> the railing has panels and the panel
              parameter has the glass surface.
  R3 panel:   the same with a metal surface (sheet panel), Favorite 'FS test rail panel'.
  R4 favorite: a second railing from 'FS test rail glass' has the same signature (rails, panels,
              balusters, rail heights, panel surface) as the first.
  C1 CW surface: a frame class and a panel class with a surface, Favorite 'FS test cw surf' ->
              no surface in notKept, and the frames / panels of a curtain wall from the Favorite
              have the surface (GetDetailsOfElements: surface.surfaceIndex / surfaces.outer).
Every response's kept / notKept / unverified is printed - the R checks that fail are the
NEOVERENE parts of ADR-001 R1 (what the Railing defaults cannot carry through the API).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure (encoding='utf-8')

TIMEOUT_S = 180
X0 = 5000.0
RAIL_LENGTH = 6.0
TOL_RAIL = 0.02          # rail axis vs. 3D z-range centre of its profile
RAILS = 5
RAIL_BOTTOM = 0.1
RAIL_TOP = 0.9
RAIL_HEIGHTS = [RAIL_BOTTOM + (i + 1) * (RAIL_TOP - RAIL_BOTTOM) / (RAILS + 1) for i in range (RAILS)]

FAV_RAILS = 'FS test rail 5'
FAV_GLASS = 'FS test rail glass'
FAV_PANEL = 'FS test rail panel'
FAV_CW = 'FS test cw surf'
CW_FRAME = 'FS test cw surf frame'
CW_PANEL = 'FS test cw surf panel'

results: list[tuple[str, bool, str]] = []
created_elements: list[dict] = []
created_favorites: list[str] = []
URL = ''
EXPERIMENTAL = False


def check (name: str, ok: bool, detail: str = '') -> bool:
    results.append ((name, bool (ok), detail))
    print (f"[{'PASS' if ok else 'FAIL'}] {name}" + (f' - {detail}' if detail else ''), flush=True)
    return bool (ok)


def info (text: str) -> None:
    print (f'       {text}', flush=True)


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


def short (obj, limit: int = 300) -> str:
    text = json.dumps (obj, ensure_ascii=False)
    return text if len (text) <= limit else text[:limit] + '...'


def element_ids (response: dict) -> list[dict]:
    ids = []
    for item in response.get ('elements', []):
        if 'elementId' in item:
            ids.append ({'elementId': item['elementId']})
            created_elements.append ({'elementId': item['elementId']})
    return ids


def remember_favorite (response: dict, name: str) -> bool:
    favorite = response.get ('favorite', {})
    if favorite.get ('result') in ('created', 'overwritten'):
        if name not in created_favorites:
            created_favorites.append (name)
        return True
    return False


def bbox_of (element_id: dict) -> dict | None:
    geometry = tapir ('Get3DGeometryOfElements', {'elements': [element_id]}).get ('geometries3D', [{}])[0]
    vertices = [v for body in geometry.get ('bodies', []) for v in body.get ('vertices', [])]
    if not vertices:
        return None
    return {'zMin': min (v['z'] for v in vertices), 'zMax': max (v['z'] for v in vertices),
            'xMin': min (v['x'] for v in vertices), 'xMax': max (v['x'] for v in vertices)}


def subelements (element_id: dict) -> dict:
    return tapir ('GetSubelementsOfHierarchicalElements', {'elements': [element_id]}).get ('subelements', [{}])[0]


def gdl_parameters (ids: list[dict]) -> list[dict]:
    if not ids:
        return []
    return tapir ('GetGDLParametersOfElements', {'elements': ids}).get ('gdlParametersOfElements', [])


def report_fields (label: str, response: dict) -> None:
    info (f"{label} apiPath: {short (response.get ('apiPath'), 400)}")
    info (f"{label} kept: {response.get ('kept')}")
    for item in response.get ('notKept', []):
        info (f"{label} notKept {item['field']}: {item['reason']}")
    for item in response.get ('unverified', []):
        info (f"{label} unverified {item['field']}: {item['reason']}")


def not_kept_fields (response: dict) -> list[str]:
    return [n['field'] for n in response.get ('notKept', [])]


def surface (pattern: str) -> dict | None:
    surfaces = tapir ('GetAttributesByType', {'attributeType': 'Surface'}).get ('attributes', [])
    for item in surfaces:
        if re.search (pattern, item.get ('name', ''), re.IGNORECASE):
            return item
    return None


def railing_signature (railing_id: dict) -> dict:
    """Rails (heights above the railing base from the 3D z-range of each rail), panels, balusters."""
    subs = subelements (railing_id)
    base = bbox_of (railing_id)
    z0 = base['zMin'] if base else 0.0
    rail_heights = []
    for rail in subs.get ('railingRails', []):
        box = bbox_of (rail)
        if box:
            rail_heights.append (round ((box['zMin'] + box['zMax']) / 2.0 - z0, 3))
    segments = max (1, len (subs.get ('railingSegments', [])))
    return {
        'segments': segments,
        'rails': len (subs.get ('railingRails', [])),
        'railHeights': sorted (rail_heights),
        'panels': len (subs.get ('railingPanels', [])),
        'panelIds': subs.get ('railingPanels', []),
        'balusters': len (subs.get ('railingBalusters', [])),
        'toprails': len (subs.get ('railingToprails', [])),
        'handrails': len (subs.get ('railingHandrails', [])),
        'height': round (base['zMax'] - base['zMin'], 3) if base else None,
    }


def create_railing (favorite: str, offset: float) -> dict | None:
    item = {'favoriteName': favorite,
            'coordinates': [{'x': X0 + offset, 'y': 0.0}, {'x': X0 + offset + RAIL_LENGTH, 'y': 0.0}],
            'zCoordinate': 0.0}
    if EXPERIMENTAL:
        item['useExtDefaults'] = True
    created = tapir ('CreateRailings', {'railingsData': [item]})
    ids = element_ids (created)
    if not check (f"railing from '{favorite}' created", len (ids) == 1, short (created)):
        return None
    return ids[0]


def base_railing_input () -> dict:
    parameters = {
        'segment': {'height': 1.0},
        'posts': {'spacing': 1.5, 'distribution': 'BestDivision'},
        'handrail': {'visible': True, 'height': 1.0},
        'restoreDefaults': True,
    }
    if EXPERIMENTAL:
        parameters['experimental'] = {'useExt': True, 'subElements': True, 'writeRailList': True}
    return parameters


def favorite_input (name: str) -> dict:
    favorite = {'name': name, 'overwriteExisting': True}
    if EXPERIMENTAL:
        favorite['useExtDefaults'] = True
    return favorite


def panel_material_parameter (probe: dict) -> str | None:
    for item in probe.get ('subElementProbe', []):
        if item.get ('kind') == 'Panel' and item.get ('subElement'):
            for param in item['subElement'].get ('gdlParameters', {}).get ('parameters', []):
                if param.get ('type') == 'Material':
                    return param['name']
    return None


# ----------------------------------------------------------------------------------------------

def test_r1_rails () -> None:
    parameters = base_railing_input ()
    parameters.update ({
        'rails': {'count': RAILS, 'bottom': RAIL_BOTTOM, 'top': RAIL_TOP, 'visible': True},
        'infill': {'kind': 'none'},
        'favorite': favorite_input (FAV_RAILS),
    })
    response = tapir ('SetRailingSystemDefaults', parameters)
    if not check ('R1 SetRailingSystemDefaults (5 rails) answers', 'kept' in response, short (response, 600)):
        return
    report_fields ('R1', response)
    check ('R1 defaults restored', response.get ('defaultsRestored') is True, short (response.get ('defaultsRestored')))
    check ('R1 favourite saved', remember_favorite (response, FAV_RAILS), short (response.get ('favorite')))
    check ('R1 segment height and post spacing kept', 'segment.height' in response['kept'] and 'posts.spacing' in response['kept'],
           short (response.get ('notKept'), 400))
    check ('R1 rails.count not refused', 'rails.count' not in not_kept_fields (response), short (response.get ('notKept'), 400))

    railing = create_railing (FAV_RAILS, 0.0)
    if not railing:
        return
    sig = railing_signature (railing)
    info ('R1 railing: ' + short ({k: v for k, v in sig.items () if k != 'panelIds'}, 500))
    check (f"R1 {RAILS} rails per segment", sig['rails'] == RAILS * sig['segments'], f"{sig['rails']} rails, {sig['segments']} segments")
    expected = sorted (round (h, 3) for h in RAIL_HEIGHTS)
    per_segment = sorted (set (sig['railHeights']))
    check (f'R1 rail heights {expected} +-{TOL_RAIL * 100:.0f} cm',
           len (per_segment) == RAILS and all (abs (a - b) <= TOL_RAIL for a, b in zip (per_segment, expected)),
           f'{per_segment}')
    visible_panels = sig['panels']
    check ('R1 no panel infill', visible_panels == 0, f'{visible_panels} panel subelements')


def test_r2_r3_panels () -> tuple[dict | None, str | None]:
    probe = tapir ('GetRailingSystemDefaults', {'includeParameters': True, 'useExt': True, 'probeSubElements': True,
                                                'subElementKinds': ['Panel']}) if EXPERIMENTAL else {}
    material_param = panel_material_parameter (probe)
    glass = surface (r'glass|sklo|skl')
    metal = surface (r'steel|ocel|oce|metal|kov|alumin|zinc|plech')
    info (f"R2/R3 panel material parameter: {material_param}, glass: {glass and glass['name']}, metal: {metal and metal['name']}")
    check ('R2 the Panel subelement default has a Material parameter', material_param is not None,
           short ([i for i in probe.get ('subElementProbe', []) if i.get ('kind') == 'Panel'], 400))
    first_glass = None
    for label, fav, surf, offset in (('R2 glass', FAV_GLASS, glass, 20.0), ('R3 panel', FAV_PANEL, metal, 40.0)):
        parameters = base_railing_input ()
        panel: dict = {}
        if material_param and surf:
            panel['parameters'] = [{'name': material_param, 'value': surf['name']}]
        parameters.update ({'infill': {'kind': 'panel', 'panel': panel}, 'favorite': favorite_input (fav)})
        response = tapir ('SetRailingSystemDefaults', parameters)
        if not check (f'{label} SetRailingSystemDefaults answers', 'kept' in response, short (response, 600)):
            continue
        report_fields (label, response)
        check (f'{label} favourite saved', remember_favorite (response, fav), short (response.get ('favorite')))
        check (f'{label} infill.kind kept', 'infill.kind' in response['kept'], short (response.get ('notKept'), 400))
        if panel:
            check (f'{label} panel surface parameter kept', f'infill.panel.parameters.{material_param}' in response['kept'],
                   short (response.get ('notKept'), 400))
        railing = create_railing (fav, offset)
        if not railing:
            continue
        sig = railing_signature (railing)
        info (f'{label} railing: ' + short ({k: v for k, v in sig.items () if k != 'panelIds'}, 500))
        check (f'{label} railing has panels', sig['panels'] > 0, f"{sig['panels']} panels")
        if panel and sig['panelIds']:
            params = gdl_parameters (sig['panelIds'][:1])
            values = {p['name']: p.get ('value') for g in params for p in g.get ('parameters', [])}
            check (f'{label} panel {material_param} = {surf["name"]} (index {surf["index"]})', values.get (material_param) == surf['index'],
                   f'{values.get (material_param)}')
            sig['panelSurface'] = values.get (material_param)
        if fav == FAV_GLASS:
            first_glass = sig
    return first_glass, material_param


def test_r4_favorite (first: dict | None, material_param: str | None) -> None:
    if not first:
        check ('R4 second railing from the glass favourite (needs R2)', False, 'R2 gave no railing')
        return
    railing = create_railing (FAV_GLASS, 60.0)
    if not railing:
        return
    sig = railing_signature (railing)
    if material_param and sig['panelIds']:
        values = {p['name']: p.get ('value') for g in gdl_parameters (sig['panelIds'][:1]) for p in g.get ('parameters', [])}
        sig['panelSurface'] = values.get (material_param)
    keys = ('rails', 'panels', 'balusters', 'toprails', 'handrails', 'railHeights', 'height', 'panelSurface')
    a = {k: first.get (k) for k in keys}
    b = {k: sig.get (k) for k in keys}
    check ('R4 same infill as the first railing from the favourite', a == b, f'{short (a)} vs {short (b)}')


def test_c1_cw_surface () -> None:
    frame_surface = surface (r'blue|modr|paint|lak|powder|RAL')
    panel_surface = surface (r'glass|sklo|skl')
    surfaces = tapir ('GetAttributesByType', {'attributeType': 'Surface'}).get ('attributes', [])
    frame_surface = frame_surface or (surfaces[1] if len (surfaces) > 1 else None)
    panel_surface = panel_surface or (surfaces[2] if len (surfaces) > 2 else None)
    if not check ('C1 two surfaces in the project', frame_surface is not None and panel_surface is not None, ''):
        return
    info (f"C1 frame surface {frame_surface['name']} ({frame_surface['index']}), panel surface {panel_surface['name']} ({panel_surface['index']})")
    cw_flags = {'readBackExt': True, 'surfaceRetryBySubElement': True} if EXPERIMENTAL else {}
    first = tapir ('SetCurtainWallSystemDefaults', dict ({
        'frameClasses': [{'name': CW_FRAME, 'width': 0.06, 'depth': 0.15, 'offset': 0.05, 'surfaceName': frame_surface['name']}],
        'panelClasses': [{'name': CW_PANEL, 'thickness': 0.024, 'surfaceName': panel_surface['name']}],
        'restoreDefaults': True}, **cw_flags))
    if not check ('C1 SetCurtainWallSystemDefaults answers', 'frameClasses' in first, short (first, 600)):
        return
    for diag in first.get ('surfaceDiagnostics', []):
        info ('C1 surfaceDiagnostics: ' + short (diag, 800))
    items = first['frameClasses'] + first['panelClasses']
    not_kept = {c.get ('name'): c.get ('notKept') for c in items if c.get ('notKept')}
    check ('C1 surfaces of frame and panel class kept (no notKept)', not not_kept, short (not_kept or items, 600))
    index = {c['name']: c['classIndex'] for c in items if 'classIndex' in c}
    if not check ('C1 class indices', CW_FRAME in index and CW_PANEL in index, short (index)):
        return

    second = tapir ('SetCurtainWallSystemDefaults', dict ({
        'frameClasses': [{'name': CW_FRAME, 'width': 0.06, 'depth': 0.15, 'offset': 0.05, 'surfaceName': frame_surface['name']}],
        'panelClasses': [{'name': CW_PANEL, 'thickness': 0.024, 'surfaceName': panel_surface['name']}],
        'primaryPattern': {'sizes': [1.5], 'logic': 'FixedSizes'},
        'secondaryPattern': {'sizes': [3.0], 'logic': 'FixedSizes'},
        'cells': [{'columnIndex': 0, 'rowIndex': 0, 'panelClassIndex': index[CW_PANEL],
                   'leftFrameClassIndex': index[CW_FRAME], 'bottomFrameClassIndex': index[CW_FRAME]}],
        'favorite': favorite_input (FAV_CW),
        'restoreDefaults': True}, **cw_flags))
    check ('C1 favourite saved', remember_favorite (second, FAV_CW), short (second.get ('favorite')))
    cw_item = {'favoriteName': FAV_CW, 'coordinates': [{'x': X0 + 100.0, 'y': 0.0}, {'x': X0 + 106.0, 'y': 0.0}],
               'zCoordinate': 0.0, 'height': 3.0}
    if EXPERIMENTAL:
        cw_item['useExtDefaults'] = True
    cw = tapir ('CreateCurtainWalls', {'curtainWallsData': [cw_item]})
    ids = element_ids (cw)
    if not check ('C1 curtain wall from the favourite created', len (ids) == 1, short (cw)):
        return
    subs = subelements (ids[0])
    frames = tapir ('GetDetailsOfElements', {'elements': subs.get ('cWallFrames', [])}).get ('detailsOfElements', [])
    panels = tapir ('GetDetailsOfElements', {'elements': subs.get ('cWallPanels', [])}).get ('detailsOfElements', [])
    class_frames = [f['details'] for f in frames if f.get ('details', {}).get ('className') == CW_FRAME]
    class_panels = [p['details'] for p in panels if p.get ('details', {}).get ('className') == CW_PANEL]
    info ('C1 frame surfaces: ' + short ([f.get ('surface') for f in class_frames][:4], 400))
    info ('C1 panel surfaces: ' + short ([p.get ('surfaces') for p in class_panels][:4], 400))
    check ('C1 frames of the class have the surface',
           bool (class_frames) and all (f.get ('surface', {}).get ('useOwnMaterial') and f['surface'].get ('surfaceIndex') == frame_surface['index'] for f in class_frames),
           f'{len (class_frames)} frames')
    check ('C1 panels of the class have the surface',
           bool (class_panels) and all (p.get ('surfaces', {}).get ('outer', {}).get ('surfaceIndex') == panel_surface['index'] for p in class_panels),
           f'{len (class_panels)} panels')


def railing_defaults_state () -> dict:
    parameters = {'useExt': True, 'probeSubElements': True, 'includeParameters': True} if EXPERIMENTAL else {}
    state = tapir ('GetRailingSystemDefaults', parameters)
    return {k: state.get (k) for k in ('railing', 'memo', 'railingExt', 'memoExt', 'subElementProbe')}


def cleanup (before: dict) -> None:
    if created_elements:
        response = tapir ('DeleteElements', {'elements': list (reversed (created_elements))})
        print ('cleanup elements:', short (response, 200))
    for favorite in created_favorites:
        print ('cleanup favorite:', favorite, short (tapir ('DeleteFavorites', {'favorites': [favorite]}), 200))
    left = [f for f in tapir ('GetFavoritesByType', {'elementType': 'Railing'}).get ('favorites', []) +
            tapir ('GetFavoritesByType', {'elementType': 'CurtainWall'}).get ('favorites', []) if f.startswith ('FS test ')]
    check ('cleanup: no test favourite left', not left, short (left))
    after = railing_defaults_state ()
    check ('cleanup: Railing defaults (with subelement defaults and their parameters) as before the test', after == before,
           'differs' if after != before else '')
    probe = tapir ('SetCurtainWallSystemDefaults', {'frameClasses': [{'name': CW_FRAME}], 'panelClasses': [{'name': CW_PANEL}], 'restoreDefaults': True})
    left_classes = [c.get ('name') for c in probe.get ('frameClasses', []) + probe.get ('panelClasses', []) if c.get ('result') != 'created']
    check ('cleanup: no test class left in the Curtain Wall defaults', 'frameClasses' in probe and not left_classes, short (left_classes or probe, 300))


def main () -> int:
    global URL, EXPERIMENTAL
    parser = argparse.ArgumentParser ()
    parser.add_argument ('--host', default='http://127.0.0.1')
    parser.add_argument ('--port', type=int, default=19723)
    parser.add_argument ('--experimental', action='store_true', help='also use the opt-in Ext / subelement paths (run the spike first)')
    args = parser.parse_args ()
    URL = f'{args.host}:{args.port}'
    EXPERIMENTAL = args.experimental

    try:
        version = tapir ('GetAddOnVersion')
    except (urllib.error.URLError, TimeoutError) as error:
        print (f'Archicad is not reachable on {URL}: {error}')
        return 2
    print ('Tapir add-on version:', short (version))
    before = railing_defaults_state ()
    if not check ('GetRailingSystemDefaults answers', before.get ('railing') is not None, short (before, 300)):
        return 1

    try:
        for test in (test_r1_rails, test_r2_r3_panels, test_c1_cw_surface):
            try:
                value = test ()
                if test is test_r2_r3_panels:
                    test_r4_favorite (*value)
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as error:
                check (f'{test.__name__} ran without an exception', False, repr (error))
    finally:
        try:
            cleanup (before)
        except (urllib.error.URLError, TimeoutError) as error:
            print ('cleanup failed:', error)

    failed = [r for r in results if not r[1]]
    print (f'\n{len (results) - len (failed)}/{len (results)} checks passed')
    for name, _, detail in failed:
        print (f'  FAIL {name}: {detail}')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit (main ())

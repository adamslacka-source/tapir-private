"""
Acceptance test of the I5/I6 C++ changes of ADR-001 (sections 7-9: T2, T3, T9 and the bool fix).

    python test_i5_i6_cpp.py                 # runs against Archicad on port 19723
    python test_i5_i6_cpp.py --port 19724    # another Archicad instance

Needs Archicad with the Tapir add-on built from this commit. Standard library only.
Every test element is placed at x >= 4000 m and deleted at the end, together with the test
Favorites (also after a failed check). The Curtain Wall test classes never stay in the tool
defaults: every SetCurtainWallSystemDefaults call of the test uses restoreDefaults, and the
cleanup probes that the class names are gone.

  T2 SetCurtainWallSystemDefaults: two frame classes (mullion, transom), two panel classes
     (glass, spandrel), boundary frame, 1.5 m x (0.9 + 2.1) m pattern with the spandrel in the
     lower row, saved as Favorite 'FS cw.test' -> every field kept (no 'notKept'); a curtain wall
     from the Favorite has frames whose className and frameContour (a1 + a2 = width, b1 + b2 =
     depth, b2 = offset) and panels whose className and thickness match the input; the GDL
     parameters of a frame and a panel can be read; a second curtain wall from the Favorite has
     the same frame/panel signature.
  T3 ModifyRailingDetails: a railing 6 m long gets height 1.10 m and posts every 1.5 m
     (BestDivision) -> 'changed' lists both, the 3D top is 1.10 m +-1 cm, posts + inner posts
     = 6 / 1.5 + 1; a Favorite 'FS rail.test' saved from it gives the same.
     CreateRailings with height/postSpacing (1.00 m, 2.0 m) -> top 1.00 m +-1 cm, 6 / 2 + 1 posts.
  T9 Get3DGeometryOfElements: a beam and a column have non-empty 3D geometry of the right size.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure (encoding='utf-8')

TIMEOUT_S = 120
X0 = 4000.0
TOL_GEOM = 0.01          # 1 cm, ADR-001 I6
TOL_EXACT = 1e-6

CW_FAVORITE = 'FS cw.test'
RAIL_FAVORITE = 'FS rail.test'
MULLION = 'FS test mullion 60x150'
TRANSOM = 'FS test transom 50x120'
GLASS = 'FS test glass 24'
SPANDREL = 'FS test spandrel 100'
FRAME_SPEC = {
    MULLION: {'width': 0.06, 'depth': 0.15, 'offset': 0.05},
    TRANSOM: {'width': 0.05, 'depth': 0.12, 'offset': 0.04},
}
PANEL_SPEC = {GLASS: 0.024, SPANDREL: 0.10}
BOUNDARY_SPEC = {'width': 0.08, 'depth': 0.15, 'offset': 0.05}
CW_LENGTH = 6.0
CW_ROWS = [0.9, 2.1]
RAIL_LENGTH = 6.0

results: list[tuple[str, bool, str]] = []
created_elements: list[dict] = []
created_favorites: list[str] = []
URL = ''


def check (name: str, ok: bool, detail: str = '') -> bool:
    results.append ((name, bool (ok), detail))
    print (f"[{'PASS' if ok else 'FAIL'}] {name}" + (f' - {detail}' if detail else ''), flush=True)
    return bool (ok)


def info (text: str) -> None:
    print (f'       {text}', flush=True)


def run (command: str, parameters: dict | None = None) -> dict:
    """Runs an official JSON command; raises on transport errors, returns the 'result' (or {'error': ...})."""
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


def bbox_of (element_id: dict) -> dict | None:
    """Axis-aligned box of the 3D bodies of one element (Get3DGeometryOfElements)."""
    geometry = tapir ('Get3DGeometryOfElements', {'elements': [element_id]}).get ('geometries3D', [{}])[0]
    vertices = [v for body in geometry.get ('bodies', []) for v in body.get ('vertices', [])]
    if not vertices:
        return None
    return {
        'nBodies': len (geometry.get ('bodies', [])),
        'xMin': min (v['x'] for v in vertices), 'xMax': max (v['x'] for v in vertices),
        'yMin': min (v['y'] for v in vertices), 'yMax': max (v['y'] for v in vertices),
        'zMin': min (v['z'] for v in vertices), 'zMax': max (v['z'] for v in vertices),
    }


def subelements (element_id: dict) -> dict:
    return tapir ('GetSubelementsOfHierarchicalElements', {'elements': [element_id]}).get ('subelements', [{}])[0]


def details_of (ids: list[dict]) -> list[dict]:
    if not ids:
        return []
    return tapir ('GetDetailsOfElements', {'elements': ids}).get ('detailsOfElements', [])


def set_cw_defaults (parameters: dict) -> dict:
    """SetCurtainWallSystemDefaults that never leaves the test classes in the tool defaults."""
    return tapir ('SetCurtainWallSystemDefaults', dict (parameters, restoreDefaults=True))


# ----------------------------------------------------------------------------------------------

def cw_class_input (material_name: str | None) -> dict:
    frames = []
    for name, spec in FRAME_SPEC.items ():
        item = dict (name=name, **spec)
        if material_name:
            item['buildingMaterialName'] = material_name
        frames.append (item)
    frames[1]['basedOn'] = MULLION
    panels = [{'name': name, 'thickness': thickness} for name, thickness in PANEL_SPEC.items ()]
    return {'frameClasses': frames, 'panelClasses': panels, 'boundaryFrame': dict (BOUNDARY_SPEC)}


def cw_signature (cw_id: dict) -> dict | None:
    """Frame classes with their contours and panel classes with thickness and height of the centroid."""
    subs = subelements (cw_id)
    frames = details_of (subs.get ('cWallFrames', []))
    panels = details_of (subs.get ('cWallPanels', []))
    if not frames or not panels:
        return None
    frame_sig: dict[str, set] = {}
    for item in frames:
        d = item.get ('details', {})
        contour = d.get ('frameContour', {})
        key = d.get ('className', '?') if d.get ('type') == 'Custom' else d.get ('type', '?')
        frame_sig.setdefault (key, set ()).add ((
            round (contour.get ('a1', 0) + contour.get ('a2', 0), 4),
            round (contour.get ('b1', 0) + contour.get ('b2', 0), 4),
            round (contour.get ('b2', 0), 4)))
    panel_sig: dict[str, list] = {}
    for item in panels:
        d = item.get ('details', {})
        zs = [c['z'] for c in d.get ('polygonCoordinates', [])]
        panel_sig.setdefault (d.get ('className', '?'), []).append ((
            round (d.get ('thickness', -1), 4), round (sum (zs) / len (zs), 2) if zs else None))
    return {'frames': {k: sorted (v) for k, v in frame_sig.items ()},
            'panels': {k: sorted (v, key=lambda t: (t[0], t[1] or 0)) for k, v in panel_sig.items ()},
            'frameIds': subs.get ('cWallFrames', []), 'panelIds': subs.get ('cWallPanels', []),
            'frameDetails': frames, 'panelDetails': panels}


def test_t2 () -> None:
    materials = tapir ('GetAttributesByType', {'attributeType': 'BuildingMaterial'}).get ('attributes', [])
    material_name = materials[0].get ('name') if materials else None

    # 1. classes only, to learn the class indices (the defaults are restored, so a second call
    #    with the same input gives the same indices)
    first = set_cw_defaults (cw_class_input (material_name))
    if not check ('T2 SetCurtainWallSystemDefaults (classes) succeeds', 'frameClasses' in first and 'panelClasses' in first, short (first, 600)):
        return
    check ('T2 defaults restored after the call', first.get ('defaultsRestored') is True, short (first.get ('defaultsRestored')))
    class_items = first['frameClasses'] + first['panelClasses']
    check ('T2 every class created without an error', all ('classIndex' in c for c in class_items), short (class_items, 600))
    not_kept = {c.get ('name'): c['notKept'] for c in class_items if c.get ('notKept')}
    check ('T2 every class field kept (no notKept)', not not_kept, short (not_kept, 600))
    check ('T2 boundary frame kept', first.get ('boundaryFrame', {}).get ('result') == 'modified' and not first['boundaryFrame'].get ('notKept'),
           short (first.get ('boundaryFrame')))
    index = {c['name']: c['classIndex'] for c in class_items if 'classIndex' in c}
    if not check ('T2 class indices returned', all (n in index for n in (MULLION, TRANSOM, GLASS, SPANDREL)), short (index)):
        return

    # 2. the same classes + the pattern + cells, saved as the Favorite
    parameters = cw_class_input (material_name)
    parameters.update ({
        'primaryPattern': {'sizes': [1.5], 'logic': 'FixedSizes'},
        'secondaryPattern': {'sizes': CW_ROWS, 'logic': 'FixedSizes'},
        'cells': [
            {'columnIndex': 0, 'rowIndex': 0, 'panelClassIndex': index[SPANDREL],
             'leftFrameClassIndex': index[MULLION], 'bottomFrameClassIndex': index[TRANSOM]},
            {'columnIndex': 0, 'rowIndex': 1, 'panelClassIndex': index[GLASS],
             'leftFrameClassIndex': index[MULLION], 'bottomFrameClassIndex': index[TRANSOM]},
        ],
        'favorite': {'name': CW_FAVORITE, 'overwriteExisting': True},
    })
    second = set_cw_defaults (parameters)
    if not check ('T2 SetCurtainWallSystemDefaults (pattern + favorite) succeeds', 'favorite' in second, short (second, 600)):
        return
    if second['favorite'].get ('result') in ('created', 'overwritten'):
        created_favorites.append (CW_FAVORITE)
    check ('T2 favourite FS cw.test saved', second['favorite'].get ('result') in ('created', 'overwritten'), short (second['favorite']))
    same_index = {c['name']: c.get ('classIndex') for c in second['frameClasses'] + second['panelClasses']}
    check ('T2 class indices stable between the two calls', all (same_index.get (k) == v for k, v in index.items ()), short (same_index))

    # 3. curtain walls from the Favorite
    cw_ids = []
    for k in range (2):
        cw = tapir ('CreateCurtainWalls', {'curtainWallsData': [{
            'favoriteName': CW_FAVORITE,
            'coordinates': [{'x': X0 + 20.0 * k, 'y': 0.0}, {'x': X0 + 20.0 * k + CW_LENGTH, 'y': 0.0}],
            'zCoordinate': 0.0, 'height': sum (CW_ROWS)}]})
        ids = element_ids (cw)
        if not check (f'T2 curtain wall {k + 1} from the favourite created', len (ids) == 1, short (cw)):
            return
        cw_ids.append (ids[0])

    sig = cw_signature (cw_ids[0])
    if not check ('T2 curtain wall has frames and panels', sig is not None, short (subelements (cw_ids[0]), 300)):
        return
    info ('frames: ' + short (sig['frames'], 600))
    info ('panels: ' + short ({k: v[:3] for k, v in sig['panels'].items ()}, 600))
    for name, spec in FRAME_SPEC.items ():
        expected = (round (spec['width'], 4), round (spec['depth'], 4), round (spec['offset'], 4))
        check (f'T2 frames of class {name}: width/depth/offset = spec', sig['frames'].get (name) == [expected],
               short (sig['frames'].get (name)))
    expected_boundary = (BOUNDARY_SPEC['width'], BOUNDARY_SPEC['depth'], BOUNDARY_SPEC['offset'])
    check ('T2 boundary frames: width/depth/offset = spec', sig['frames'].get ('Boundary') == [expected_boundary],
           short (sig['frames'].get ('Boundary')))
    for name, thickness in PANEL_SPEC.items ():
        got = sig['panels'].get (name, [])
        check (f'T2 panels of class {name}: thickness {thickness}', bool (got) and all (abs (t - thickness) <= TOL_EXACT for t, _ in got),
               short (got[:4]))
    spandrel_z = [z for _, z in sig['panels'].get (SPANDREL, []) if z is not None]
    glass_z = [z for _, z in sig['panels'].get (GLASS, []) if z is not None]
    check ('T2 spandrels in the lower row, glass in the upper row',
           bool (spandrel_z) and bool (glass_z) and max (spandrel_z) < CW_ROWS[0] < min (glass_z),
           f'spandrel z {sorted (set (spandrel_z))}, glass z {sorted (set (glass_z))}')
    columns = round (CW_LENGTH / 1.5)
    check ('T2 panel count = columns x rows', sum (len (v) for v in sig['panels'].values ()) == columns * len (CW_ROWS),
           f"{sum (len (v) for v in sig['panels'].values ())} vs {columns * len (CW_ROWS)}")

    gdl = tapir ('GetGDLParametersOfElements', {'elements': [sig['frameIds'][0], sig['panelIds'][0]]}).get ('gdlParametersOfElements', [])
    readable = [g for g in gdl if 'parameters' in g]
    check ('T2 GDL parameters of a frame and a panel readable', len (readable) == 2, short (gdl, 300))
    for label, g in zip (('frame', 'panel'), readable):
        sizes = {p['name']: p.get ('value') for p in g['parameters'] if p.get ('type') == 'Length'}
        info (f'{label} GDL lengths: ' + short (sizes, 400))

    sig2 = cw_signature (cw_ids[1])
    check ('T2 second curtain wall from the favourite has the same signature',
           sig2 is not None and sig2['frames'] == sig['frames'] and
           {k: sorted (t for t, _ in v) for k, v in sig2['panels'].items ()} == {k: sorted (t for t, _ in v) for k, v in sig['panels'].items ()},
           short (sig2 and sig2['frames']))

    # 4. errors are reported per item, nothing else is refused
    bad = set_cw_defaults ({'frameClasses': [{'name': 'FS test bad', 'basedOn': 'No such class 123'}, {'name': 'FS test bad 2', 'libraryPartName': 'No such part 123'}],
                            'panelClasses': [{'name': 'FS test bad panel', 'thickness': -1}]})
    check ('T2 unknown basedOn / library part give per item errors',
           len (bad.get ('frameClasses', [])) == 2 and all ('error' in c for c in bad.get ('frameClasses', [])), short (bad, 400))


def railing_posts (railing_id: dict) -> tuple[int, int]:
    subs = subelements (railing_id)
    return len (subs.get ('railingPosts', [])), len (subs.get ('railingInnerPosts', []))


def check_railing (label: str, railing_id: dict, height: float, spacing: float) -> None:
    box = bbox_of (railing_id)
    if check (f'{label}: 3D geometry', box is not None, short (box)):
        check (f'{label}: height {height:.2f} m +-1 cm', abs ((box['zMax'] - box['zMin']) - height) <= TOL_GEOM,
               f"zMin {box['zMin']:.4f}, zMax {box['zMax']:.4f}")
    posts, inner = railing_posts (railing_id)
    expected = round (RAIL_LENGTH / spacing) + 1
    check (f'{label}: posts + inner posts = length / spacing + 1 = {expected}', posts + inner == expected,
           f'{posts} posts + {inner} inner posts')


def test_t3 () -> None:
    created = tapir ('CreateRailings', {'railingsData': [{
        'coordinates': [{'x': X0 + 60.0, 'y': 0.0}, {'x': X0 + 60.0 + RAIL_LENGTH, 'y': 0.0}], 'zCoordinate': 0.0}]})
    ids = element_ids (created)
    if not check ('T3 test railing created', len (ids) == 1, short (created)):
        return
    railing_id = ids[0]
    before = bbox_of (railing_id)
    info ('before: ' + short (before) + f', posts {railing_posts (railing_id)}')

    response = tapir ('ModifyRailingDetails', {'railings': [{
        'elementId': railing_id['elementId'], 'height': 1.10, 'postSpacing': 1.5, 'postDistribution': 'BestDivision',
        'handrail': {'height': 0.90}}]})
    item = response.get ('results', [{}])[0]
    if not check ('T3 ModifyRailingDetails answers', 'changed' in item, short (response, 600)):
        return
    info ('state: ' + short (item.get ('state'), 600))
    for entry in item.get ('notChanged', []):
        info (f"not changed: {entry['field']}: {entry['reason']}")
    check ('T3 height changed', 'height' in item['changed'], short (item.get ('notChanged')))
    # Known limitation (confirmed live 2026-10-19): a railing from the template defaults has no
    # RailingPattern subelement, so the post spacing of an EXISTING railing cannot be changed.
    # The facade pipeline always rebuilds railings with CreateRailings (postSpacing at creation,
    # checked below), so this is reported, and the command must say why instead of failing silently.
    for field in ('postSpacing', 'postDistribution'):
        reported = field in item['changed'] or any (n.get ('field') == field and n.get ('reason') for n in item.get ('notChanged', []))
        check (f'T3 {field} changed or reported with a reason (known limitation)', reported, short (item.get ('notChanged')))
    info ('handrail.height ' + ('changed' if 'handrail.height' in item['changed'] else 'NOT changed (informative)'))
    check_railing ('T3 modified railing', railing_id, 1.10, 1.5)

    fav = tapir ('CreateFavoritesFromElements', {'favoritesFromElements': [{'elementId': railing_id['elementId'], 'favorite': RAIL_FAVORITE}]})
    if fav.get ('executionResults', [{}])[0].get ('success'):
        created_favorites.append (RAIL_FAVORITE)
        from_fav = tapir ('CreateRailings', {'railingsData': [{
            'favoriteName': RAIL_FAVORITE,
            'coordinates': [{'x': X0 + 70.0, 'y': 0.0}, {'x': X0 + 70.0 + RAIL_LENGTH, 'y': 0.0}], 'zCoordinate': 0.0}]})
        fav_ids = element_ids (from_fav)
        if check ('T3 railing from favourite FS rail.test created', len (fav_ids) == 1, short (from_fav)):
            check_railing ('T3 railing from the favourite', fav_ids[0], 1.10, 1.5)
    else:
        check ('T3 favourite FS rail.test saved', False, short (fav))

    direct = tapir ('CreateRailings', {'railingsData': [{
        'coordinates': [{'x': X0 + 80.0, 'y': 0.0}, {'x': X0 + 80.0 + RAIL_LENGTH, 'y': 0.0}], 'zCoordinate': 0.0,
        'height': 1.00, 'postSpacing': 2.0, 'postDistribution': 'BestDivision'}]})
    direct_ids = element_ids (direct)
    if check ('T3 CreateRailings with height/postSpacing created', len (direct_ids) == 1, short (direct)):
        check_railing ('T3 CreateRailings height/postSpacing', direct_ids[0], 1.00, 2.0)

    bad = tapir ('ModifyRailingDetails', {'railings': [{'elementId': railing_id['elementId'], 'height': -1.0}]})
    check ('T3 invalid height gives an error item', 'error' in bad.get ('results', [{}])[0], short (bad))


def test_t9 () -> None:
    beam = tapir ('CreateBeams', {'beamsData': [{
        'begCoordinate': {'x': X0 + 100.0, 'y': 0.0}, 'endCoordinate': {'x': X0 + 104.0, 'y': 0.0}, 'zCoordinate': 3.0}]})
    beam_ids = element_ids (beam)
    if check ('T9 test beam created', len (beam_ids) == 1, short (beam)):
        box = bbox_of (beam_ids[0])
        check ('T9 beam has 3D geometry', box is not None, short (box))
        if box:
            check ('T9 beam geometry is 4 m long', abs ((box['xMax'] - box['xMin']) - 4.0) <= TOL_GEOM, short (box))
    column = tapir ('CreateColumns', {'columnsData': [{'coordinates': {'x': X0 + 110.0, 'y': 0.0, 'z': 0.0}}]})
    column_ids = element_ids (column)
    if check ('T9 test column created', len (column_ids) == 1, short (column)):
        box = bbox_of (column_ids[0])
        check ('T9 column has 3D geometry', box is not None and box['zMax'] - box['zMin'] > 0.1, short (box))


def cleanup () -> None:
    if created_elements:
        response = tapir ('DeleteElements', {'elements': list (reversed (created_elements))})
        print ('cleanup elements:', short (response, 200))
    for favorite in created_favorites:
        print ('cleanup favorite:', favorite, short (tapir ('DeleteFavorites', {'favorites': [favorite]}), 200))
    # The test classes must not be in the Curtain Wall defaults: a probe that adds them (and
    # restores the defaults) has to report them as 'created'.
    probe = set_cw_defaults ({'frameClasses': [{'name': n} for n in FRAME_SPEC], 'panelClasses': [{'name': n} for n in PANEL_SPEC]})
    left = [c.get ('name') for c in probe.get ('frameClasses', []) + probe.get ('panelClasses', []) if c.get ('result') != 'created']
    check ('cleanup: no test class left in the Curtain Wall defaults', 'frameClasses' in probe and not left, short (left or probe, 300))


def main () -> int:
    global URL
    parser = argparse.ArgumentParser ()
    parser.add_argument ('--host', default='http://127.0.0.1')
    parser.add_argument ('--port', type=int, default=19723)
    args = parser.parse_args ()
    URL = f'{args.host}:{args.port}'

    try:
        version = tapir ('GetAddOnVersion')
    except (urllib.error.URLError, TimeoutError) as error:
        print (f'Archicad is not reachable on {URL}: {error}')
        return 2
    print ('Tapir add-on version:', short (version))

    try:
        for test in (test_t2, test_t3, test_t9):
            try:
                test ()
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as error:
                check (f'{test.__name__} ran without an exception', False, repr (error))
    finally:
        try:
            cleanup ()
        except (urllib.error.URLError, TimeoutError) as error:
            print ('cleanup failed:', error)

    failed = [r for r in results if not r[1]]
    print (f'\n{len (results) - len (failed)}/{len (results)} checks passed')
    for name, _, detail in failed:
        print (f'  FAIL {name}: {detail}')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit (main ())

"""
Acceptance test of the I3 C++ changes of ADR-001 (section 8: T1, T4, T7, T8).

    python test_i3_cpp.py                 # runs against Archicad on port 19723
    python test_i3_cpp.py --port 19724    # another Archicad instance

Needs Archicad with the Tapir add-on built from this commit. Standard library only.
Every test element is placed at x >= 2000 m and deleted at the end, together with the test
Profile and the test Favorites (also after a failed check).

  T1 CreateProfilesFromPolygons: profile from a rectangle with a rectangular cut-out ->
     area read back from GetProfiles (skin outlines) within 1 %, width/height, beam flag,
     a second create without overwrite is refused, with overwrite it keeps the guid;
     a beam with this profile is created and reports the profile.
  T4 CreateFavoritesFromDefaults: favourite from the Beam tool defaults -> GetFavoritesByType,
     duplicate refused, overwrite reported as 'overwritten'.
  T7 CreateDoors with libraryPartName -> GetGDLParametersOfElements works, SetGDLParameters
     changes the transom height (gs_transom_height or similar), a Favorite made from the door
     gives a door whose parameters can be read too.
  T8 revealDepthLocation: ModifyDoors Core/Side and CreateWindows Core read back from
     GetDetailsOfElements.
"""
from __future__ import annotations

import sys
if hasattr (sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure (encoding='utf-8', errors='replace')

import argparse
import json
import urllib.error
import urllib.request

TIMEOUT_S = 60
X0 = 2000.0
PROFILE_NAME = 'I3 test profil rimsa'
BEAM_FAVORITE = 'I3 test FS beam'
DOOR_FAVORITE = 'I3 test FS door transom'
DOOR_PARTS = ['Double Door with Transom 27', 'Door with Sidelight on Side 1 and Transom 27', 'Door with Transom 27']
TRANSOM_PARAMS = ['gs_transom_height', 'gs_transom_h', 'gs_ftransom_height']

results: list[tuple[str, bool, str]] = []
created_elements: list[dict] = []
created_profiles: list[dict] = []
created_favorites: list[str] = []
URL = ''


def check (name: str, ok: bool, detail: str = '') -> bool:
    results.append ((name, bool (ok), detail))
    print (f"[{'PASS' if ok else 'FAIL'}] {name}" + (f' - {detail}' if detail else ''), flush=True)
    return bool (ok)


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


def polygon_area (coords: list[dict]) -> float:
    total = 0.0
    for i in range (len (coords) - 1):
        total += coords[i]['x'] * coords[i + 1]['y'] - coords[i + 1]['x'] * coords[i]['y']
    return abs (total) / 2.0


def skin_area (skin: dict) -> float:
    """Outer contour minus holes, from GetProfiles' outlineCoords/outlineSubPolyEnds (slot 0 = anchor)."""
    coords = skin.get ('outlineCoords', [])
    ends = skin.get ('outlineSubPolyEnds', [])
    areas = []
    for k in range (1, len (ends)):
        part = coords[ends[k - 1] + 1: ends[k] + 1]
        if len (part) >= 4:
            areas.append (polygon_area (part))
    if not areas:
        return 0.0
    return areas[0] - sum (areas[1:])


def gdl_param (params: list[dict], names: list[str]) -> dict | None:
    lowered = {p.get ('name', '').lower (): p for p in params}
    for name in names:
        if name.lower () in lowered:
            return lowered[name.lower ()]
    for p in params:
        if 'transom' in p.get ('name', '').lower () and p.get ('type') == 'Length':
            return p
    return None


# ----------------------------------------------------------------------------------------------
def test_t1 () -> None:
    materials = tapir ('GetAttributesByType', {'attributeType': 'BuildingMaterial'}).get ('attributes', [])
    if not check ('T1 project has a building material', bool (materials)):
        return
    material = materials[0]
    rect = [{'x': 0.0, 'y': 0.0}, {'x': 0.30, 'y': 0.0}, {'x': 0.30, 'y': 0.20}, {'x': 0.0, 'y': 0.20}]
    cut = [{'x': 0.10, 'y': 0.05}, {'x': 0.20, 'y': 0.05}, {'x': 0.20, 'y': 0.10}, {'x': 0.10, 'y': 0.10}]
    expected_area = 0.30 * 0.20 - 0.10 * 0.05
    profile = {
        'name': PROFILE_NAME,
        'polygons': [{'outer': {'polygonCoordinates': rect}, 'holes': [{'polygonCoordinates': cut}]}],
        'buildingMaterialName': material['name'] if 'name' in material else None,
        'useWith': {'wall': True, 'beam': True, 'column': False},
    }
    if profile['buildingMaterialName'] is None:
        del profile['buildingMaterialName']
        profile['buildingMaterialId'] = {'attributeId': material['attributeId']}

    response = tapir ('CreateProfilesFromPolygons', {'profiles': [dict (profile, overwriteExisting=True)]})
    items = response.get ('attributes', [])
    if not check ('T1 CreateProfilesFromPolygons returns an attribute', len (items) == 1 and 'attributeId' in items[0], short (response)):
        return
    first = items[0]
    created_profiles.append (first['attributeId'])
    check ('T1 result has index and name', isinstance (first.get ('index'), int) and first.get ('name') == PROFILE_NAME, short (first))

    again = tapir ('CreateProfilesFromPolygons', {'profiles': [profile]}).get ('attributes', [{}])[0]
    check ('T1 second create without overwrite is refused', 'error' in again, short (again))
    over = tapir ('CreateProfilesFromPolygons', {'profiles': [dict (profile, overwriteExisting=True)]}).get ('attributes', [{}])[0]
    check ('T1 overwrite keeps the guid and reports created=false',
           over.get ('attributeId') == first['attributeId'] and over.get ('created') is False, short (over))

    details = tapir ('GetProfiles', {'attributeIds': [{'attributeId': first['attributeId']}]}).get ('profiles', [{}])[0]
    skins = details.get ('skins', [])
    area = sum (skin_area (s) for s in skins)
    check ('T1 one skin', len (skins) == 1, f'{len (skins)} skins')
    check ('T1 area within 1 %', abs (area - expected_area) <= 0.01 * expected_area, f'{area:.6f} m2 vs {expected_area:.6f} m2')
    check ('T1 width 0.30 / height 0.20',
           abs (details.get ('width', 0) - 0.30) < 1e-4 and abs (details.get ('height', 0) - 0.20) < 1e-4,
           f"{details.get ('width')} x {details.get ('height')}")
    check ('T1 use flags wall+beam, not column',
           details.get ('wallType') is True and details.get ('beamType') is True and details.get ('coluType') is False,
           short ({k: details.get (k) for k in ('wallType', 'beamType', 'coluType')}))
    if skins:
        check ('T1 skin has the building material', skins[0].get ('buildingMaterialId') == {'attributeId': material['attributeId']}
               or skins[0].get ('buildingMaterialId', {}).get ('guid') == material['attributeId'].get ('guid'),
               short (skins[0].get ('buildingMaterialId')))

    beam = tapir ('CreateBeams', {'beamsData': [{
        'begCoordinate': {'x': X0, 'y': 0.0}, 'endCoordinate': {'x': X0 + 3.0, 'y': 0.0},
        'zCoordinate': 0.0, 'profileId': first['attributeId']}]})
    beam_ids = element_ids (beam)
    if check ('T1 beam with the profile is created', len (beam_ids) == 1, short (beam)):
        beam_details = tapir ('GetDetailsOfElements', {'elements': beam_ids}).get ('detailsOfElements', [{}])[0]
        check ('T1 beam reports the profile', first['attributeId']['guid'].lower () in json.dumps (beam_details).lower (),
               short (beam_details.get ('typeSpecificDetails', {}), 200))


def test_t4 () -> None:
    params = {'favorites': [{'name': BEAM_FAVORITE, 'elementType': 'Beam'}]}
    first = tapir ('CreateFavoritesFromDefaults', params).get ('favorites', [{}])[0]
    if first.get ('result') == 'created':
        created_favorites.append (BEAM_FAVORITE)
    elif 'error' in first:
        # left over from an earlier run: overwrite it and own it
        first = tapir ('CreateFavoritesFromDefaults', {'favorites': [dict (params['favorites'][0], overwriteExisting=True)]}).get ('favorites', [{}])[0]
        created_favorites.append (BEAM_FAVORITE)
    check ('T4 favourite created from the Beam defaults', first.get ('result') in ('created', 'overwritten'), short (first))
    names = tapir ('GetFavoritesByType', {'elementType': 'Beam'}).get ('favorites', [])
    check ('T4 GetFavoritesByType lists it', BEAM_FAVORITE in names, f'{len (names)} beam favourites')
    dup = tapir ('CreateFavoritesFromDefaults', params).get ('favorites', [{}])[0]
    check ('T4 duplicate without overwrite is refused', 'error' in dup, short (dup))
    over = tapir ('CreateFavoritesFromDefaults', {'favorites': [dict (params['favorites'][0], overwriteExisting=True)]}).get ('favorites', [{}])[0]
    check ('T4 overwrite reported', over.get ('result') == 'overwritten', short (over))
    bad = tapir ('CreateFavoritesFromDefaults', {'favorites': [{'name': 'I3 bad', 'elementType': 'NoSuchType'}]})
    check ('T4 invalid element type gives an error', 'error' in json.dumps (bad), short (bad))


def read_and_set_transom (door_ids: list[dict], label: str) -> None:
    got = tapir ('GetGDLParametersOfElements', {'elements': door_ids})
    item = got.get ('gdlParametersOfElements', [{}])[0]
    if not check (f'{label}: GetGDLParametersOfElements works', 'parameters' in item, short (item)):
        return
    param = gdl_param (item['parameters'], TRANSOM_PARAMS)
    if not check (f'{label}: transom height parameter found', param is not None,
                  ', '.join (p['name'] for p in item['parameters'] if 'transom' in p['name'].lower ())):
        return
    new_value = round (float (param['value']) + 0.10, 4)
    set_result = tapir ('SetGDLParametersOfElements', {'elementsWithGDLParameters': [{
        'elementId': door_ids[0]['elementId'], 'gdlParameters': [{'name': param['name'], 'value': new_value}]}]})
    check (f'{label}: SetGDLParametersOfElements succeeds', set_result.get ('executionResults', [{}])[0].get ('success') is True, short (set_result))
    after = tapir ('GetGDLParametersOfElements', {'elements': door_ids}).get ('gdlParametersOfElements', [{}])[0]
    value = next ((p['value'] for p in after.get ('parameters', []) if p['name'] == param['name']), None)
    check (f"{label}: {param['name']} changed", value is not None and abs (float (value) - new_value) < 1e-6, f'{value} vs {new_value}')


def test_t7_t8 () -> None:
    wall = tapir ('CreateWalls', {'wallsData': [{
        'begCoordinate': {'x': X0 + 10.0, 'y': 0.0}, 'endCoordinate': {'x': X0 + 22.0, 'y': 0.0},
        'zCoordinate': 0.0, 'height': 3.5, 'thickness': 0.45}]})
    wall_ids = element_ids (wall)
    if not check ('T7 test wall created', len (wall_ids) == 1, short (wall)):
        return
    wall_id = wall_ids[0]['elementId']

    door_ids: list[dict] = []
    used_part = None
    for part in DOOR_PARTS:
        door = tapir ('CreateDoors', {'doorsData': [{'ownerWallId': wall_id, 'centerOffset': 2.0, 'libraryPartName': part}]})
        door_ids = element_ids (door)
        if door_ids:
            used_part = part
            break
    if not check ('T7 door created through libraryPartName', bool (door_ids), str (used_part)):
        return
    details = tapir ('GetDetailsOfElements', {'elements': door_ids}).get ('detailsOfElements', [{}])[0]
    lib_name = details.get ('typeSpecificDetails', {}).get ('libPart', {}).get ('name', '')
    check ('T7 door has the requested library part', used_part in json.dumps (details, ensure_ascii=False), lib_name)
    read_and_set_transom (door_ids, 'T7 libraryPartName door')

    fav = tapir ('CreateFavoritesFromElements', {'favoritesFromElements': [{'elementId': door_ids[0]['elementId'], 'favorite': DOOR_FAVORITE}]})
    if fav.get ('executionResults', [{}])[0].get ('success'):
        created_favorites.append (DOOR_FAVORITE)
        door2 = tapir ('CreateDoors', {'doorsData': [{'ownerWallId': wall_id, 'centerOffset': 6.0, 'favoriteName': DOOR_FAVORITE}]})
        door2_ids = element_ids (door2)
        if check ('T7 door from the favourite of that door created', bool (door2_ids), short (door2)):
            got = tapir ('GetGDLParametersOfElements', {'elements': door2_ids}).get ('gdlParametersOfElements', [{}])[0]
            check ('T7 favourite door: GDL parameters readable', 'parameters' in got, short (got))
    else:
        check ('T7 favourite from the door created', False, short (fav))

    # T8 - reveal depth location on the door, then on a new window
    for location in ('Core', 'Side'):
        modified = tapir ('ModifyDoors', {'doorsWithDetails': [{'elementId': door_ids[0]['elementId'], 'revealDepthLocation': location}]})
        ok = modified.get ('executionResults', [{}])[0].get ('success') is True
        read = tapir ('GetDetailsOfElements', {'elements': door_ids}).get ('detailsOfElements', [{}])[0].get ('typeSpecificDetails', {})
        check (f'T8 ModifyDoors revealDepthLocation={location} read back', ok and read.get ('revealDepthLocation') == location,
               short ({'modify': modified, 'read': read.get ('revealDepthLocation')}))
    window = tapir ('CreateWindows', {'windowsData': [{'ownerWallId': wall_id, 'centerOffset': 9.5, 'sillHeight': 0.9,
                                                       'revealDepthLocation': 'Core', 'revealDepthOffset': 0.05}]})
    window_ids = element_ids (window)
    if check ('T8 window with revealDepthLocation created', bool (window_ids), short (window)):
        read = tapir ('GetDetailsOfElements', {'elements': window_ids}).get ('detailsOfElements', [{}])[0].get ('typeSpecificDetails', {})
        check ('T8 CreateWindows revealDepthLocation=Core read back', read.get ('revealDepthLocation') == 'Core', short (read.get ('revealDepthLocation')))
        check ('T8 CreateWindows revealDepthOffset=0.05 read back', abs (read.get ('revealDepthOffset', -1) - 0.05) < 1e-6, short (read.get ('revealDepthOffset')))
        modified = tapir ('ModifyWindows', {'windowsWithDetails': [{'elementId': window_ids[0]['elementId'], 'revealDepthLocation': 'Side'}]})
        read = tapir ('GetDetailsOfElements', {'elements': window_ids}).get ('detailsOfElements', [{}])[0].get ('typeSpecificDetails', {})
        check ('T8 ModifyWindows revealDepthLocation=Side read back', read.get ('revealDepthLocation') == 'Side', short ({'modify': modified, 'read': read.get ('revealDepthLocation')}))


def cleanup () -> None:
    if created_elements:
        # openings first (they go with the wall anyway), then the rest
        response = tapir ('DeleteElements', {'elements': list (reversed (created_elements))})
        print ('cleanup elements:', short (response, 200))
    for favorite in created_favorites:
        tapir ('DeleteFavorites', {'favorites': [favorite]})
    for attribute_id in created_profiles:
        response = tapir ('DeleteAttributes', {'attributesToDelete': [{'attributeType': 'Profile', 'attributeId': {'attributeId': attribute_id}}]})
        print ('cleanup profile:', short (response, 200))


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
        for test in (test_t1, test_t4, test_t7_t8):
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

"""3D views of the BIM facades: hide non-facade layers, place a 2-point perspective camera in front
of each facade, save the 3D window as PNG, then restore layers and the original camera (always)."""
import sys, json, math, time
sys.path.insert(0, sys.argv[1]); from ac import C, T
houses = sys.argv[3].split(',')
outdir = sys.argv[2]
proj = T('GetProjectInfo')['projectName']; print('project', proj)
orig_proj = T('Get3DProjectionSettings')['perspective']
ids = C('API.GetAttributesByType', {'attributeType': 'Layer'})['result']['attributeIds']
layers = T('GetLayers', {'attributeIds': ids})['layers']
changed = [l for l in layers if not l.get('isHidden') and not l['name'].startswith('FASADA')]
print('layers to hide temporarily:', len(changed))
try:
    T('CreateLayers', {'layerDataArray': [{'attributeId': l['attributeId'], 'name': l['name'], 'isHidden': True} for l in changed], 'overwriteExisting': True})
    T('ChangeWindow', {'windowType': '3DModel'})
    for h in houses:
        spec_path, tag = h.split('=')
        s = json.load(open(spec_path, encoding='utf-8'))
        ref = s['reference']; a, b = ref['a'], ref['b']; z0 = ref.get('z0', {}).get('v', 0.0)
        dx, dy = b[0]-a[0], b[1]-a[1]; L = math.hypot(dx, dy); d = (dx/L, dy/L)
        n = (d[1], -d[0]) if ref.get('outside', 'right_of_ab') == 'right_of_ab' else (-d[1], d[0])
        H = max([st.get('top', st.get('level', 0)) if isinstance(st.get('top', 0), (int, float)) else 0 for st in []] + [10.0])
        cx, cy = (a[0]+b[0])/2, (a[1]+b[1])/2
        views = ((('celkovy', 0.2, max(3.6*L, 30.0), 4.0), ('sikmy', 0.85, max(2.8*L, 24.0), 4.0)) if L < 20 else
                 (('celkovy', 0.05, 1.25*L, 5.0), ('sikmy', 0.75, 0.85*L, 5.0)))
        for view, side, dist, eye in views:
            cam = (cx + n[0]*dist + d[0]*dist*side, cy + n[1]*dist + d[1]*dist*side, z0 + eye)
            tgt = (cx, cy, z0 + 4.5)
            hd = math.hypot(cam[0]-tgt[0], cam[1]-tgt[1])
            az = math.degrees(math.atan2(cam[1]-tgt[1], cam[0]-tgt[0])) % 360
            p = dict(orig_proj); p.update({'cameraPosition': {'x': cam[0], 'y': cam[1], 'z': cam[2]},
                                         'targetPosition': {'x': tgt[0], 'y': tgt[1], 'z': tgt[2]},
                                         'distance': hd, 'azimuth': az, 'viewCone': 55, 'rollAngle': 0, 'isTwoPointPersp': True})
            T('Set3DProjectionSettings', {'perspective': p})
            T('RebuildView', {}, 240); time.sleep(3)
            import os; f = os.path.join(outdir, f"bim3d_{tag}_{view}.png")
            T('SaveCurrentWindowAsImage', {'imageFilePath': f}, 240); print('saved', f)
finally:
    T('Set3DProjectionSettings', {'perspective': orig_proj})
    T('CreateLayers', {'layerDataArray': [{'attributeId': l['attributeId'], 'name': l['name'], 'isHidden': False} for l in changed], 'overwriteExisting': True})
    back = T('GetLayers', {'attributeIds': ids})['layers']
    print('restored: hidden now', sum(1 for l in back if l.get('isHidden')), 'of', len(back), '(was', sum(1 for l in layers if l.get('isHidden')), ')')

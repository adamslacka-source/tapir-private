"""
Defines a curtain wall system in the Curtain Wall tool defaults with SetCurtainWallSystemDefaults.

A mullion and a transom frame class (profile width, depth and the part of the depth outside the
panels) and a glass and an opaque spandrel panel class are created or changed by name, the boundary
frames get a wider profile, the pattern is a 1.5 m x (0.9 + 2.1) m unit with the spandrel panel in
the lower row, and the result is saved as a Favorite. The command reads the defaults back and lists
in 'notKept' every asked field that Archicad did not keep.
"""

import aclib

response = aclib.RunTapirCommand ('SetCurtainWallSystemDefaults', {
    'frameClasses': [
        { 'name': 'Example mullion 60x150', 'width': 0.06, 'depth': 0.15, 'offset': 0.05 },
        { 'name': 'Example transom 60x120', 'basedOn': 'Example mullion 60x150', 'width': 0.06, 'depth': 0.12, 'offset': 0.05 }
    ],
    'panelClasses': [
        { 'name': 'Example glass 32', 'thickness': 0.032 },
        { 'name': 'Example spandrel 120', 'thickness': 0.12 }
    ],
    'boundaryFrame': { 'width': 0.08, 'depth': 0.15, 'offset': 0.05 },
    'primaryPattern': { 'sizes': [1.5], 'logic': 'FixedSizes' },
    'secondaryPattern': { 'sizes': [0.9, 2.1], 'logic': 'FixedSizes' },
    'favorite': { 'name': 'Example CW system', 'overwriteExisting': True },
    'restoreDefaults': True
})
print (response)

# The class indices of the response are what the cells of CreateCurtainWalls refer to.
frameClasses = { c['name']: c['classIndex'] for c in response.get ('frameClasses', []) if 'classIndex' in c }
panelClasses = { c['name']: c['classIndex'] for c in response.get ('panelClasses', []) if 'classIndex' in c }

curtainWall = aclib.RunTapirCommand ('CreateCurtainWalls', {
    'curtainWallsData': [{
        'favoriteName': 'Example CW system',
        'coordinates': [ { 'x': 0.0, 'y': 0.0 }, { 'x': 6.0, 'y': 0.0 } ],
        'height': 3.0,
        'cells': [
            { 'columnIndex': 0, 'rowIndex': 0, 'panelClassIndex': panelClasses.get ('Example spandrel 120', 1),
              'leftFrameClassIndex': frameClasses.get ('Example mullion 60x150', 1),
              'bottomFrameClassIndex': frameClasses.get ('Example transom 60x120', 1) },
            { 'columnIndex': 0, 'rowIndex': 1, 'panelClassIndex': panelClasses.get ('Example glass 32', 1),
              'leftFrameClassIndex': frameClasses.get ('Example mullion 60x150', 1),
              'bottomFrameClassIndex': frameClasses.get ('Example transom 60x120', 1) }
        ]
    }]
})
print (curtainWall)

aclib.RunTapirCommand ('DeleteFavorites', { 'favorites': ['Example CW system'] })

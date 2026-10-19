import aclib

# Builds a Profile (complex profile) from a cross-section polygon, without copying an existing
# Profile: a 30 x 20 cm cornice block with a 10 x 5 cm cut-out, coordinates in meters.
# Each polygon becomes one skin with its own building material; the edges take the surface of
# the building material unless edgeSurfaceId/edgeSurfaceName is given. Available from Archicad 27.

buildingMaterials = aclib.RunTapirCommand (
    'GetAttributesByType', {'attributeType': 'BuildingMaterial'})['attributes']

profileName = 'Cornice from Python'
result = aclib.RunTapirCommand (
    'CreateProfilesFromPolygons', {
        'profiles': [{
            'name': profileName,
            'polygons': [{
                'outer': {
                    'polygonCoordinates': [
                        {'x': 0.00, 'y': 0.00},
                        {'x': 0.30, 'y': 0.00},
                        {'x': 0.30, 'y': 0.20},
                        {'x': 0.00, 'y': 0.20}
                    ]
                },
                'holes': [{
                    'polygonCoordinates': [
                        {'x': 0.10, 'y': 0.05},
                        {'x': 0.20, 'y': 0.05},
                        {'x': 0.20, 'y': 0.10},
                        {'x': 0.10, 'y': 0.10}
                    ]
                }]
            }],
            'buildingMaterialId': {'attributeId': buildingMaterials[0]['attributeId']},
            'useWith': {'wall': True, 'beam': True, 'column': False},
            'overwriteExisting': True
        }]
    })

profile = result['attributes'][0]
if 'attributeId' in profile:
    details = aclib.RunTapirCommand (
        'GetProfiles', {
            'attributeIds': [{'attributeId': profile['attributeId']}],
            'fields': ['width', 'height', 'beamType']
        })

    aclib.RunTapirCommand (
        'DeleteAttributes', {
            'attributesToDelete': [{
                'attributeType': 'Profile',
                'attributeId': {'attributeId': profile['attributeId']}
            }]
        })

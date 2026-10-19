import aclib

# Sets where the reveal depth of a window is measured from: 'Side' (the clicked side of the
# wall) or 'Core' (the core skin of a composite wall on that side). CreateWindows/CreateDoors
# take it together with revealDepthOffset, ModifyWindows/ModifyDoors change it, and
# GetDetailsOfElements reads it back.

wall = aclib.RunTapirCommand (
    'CreateWalls', {
        'wallsData': [{
            'begCoordinate': {'x': 2000.0, 'y': 10.0},
            'endCoordinate': {'x': 2006.0, 'y': 10.0},
            'zCoordinate': 0.0,
            'height': 3.0,
            'thickness': 0.45
        }]
    })['elements']

window = aclib.RunTapirCommand (
    'CreateWindows', {
        'windowsData': [{
            'ownerWallId': wall[0]['elementId'],
            'centerOffset': 3.0,
            'sillHeight': 0.9,
            'revealDepthLocation': 'Core',
            'revealDepthOffset': 0.05
        }]
    })['elements']

aclib.RunTapirCommand (
    'ModifyWindows', {
        'windowsWithDetails': [{
            'elementId': window[0]['elementId'],
            'revealDepthLocation': 'Side'
        }]
    })

details = aclib.RunTapirCommand (
    'GetDetailsOfElements', {'elements': window}, debug=False)['detailsOfElements'][0]
print ('revealDepthLocation: {}'.format (details['typeSpecificDetails'].get ('revealDepthLocation')))

aclib.RunTapirCommand (
    'DeleteElements', {'elements': window + wall})

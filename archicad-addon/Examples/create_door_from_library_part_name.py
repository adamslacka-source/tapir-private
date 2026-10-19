import aclib

# Places a door by its library part name (a part that has no Favorite in the template) and
# changes one of its GDL parameters. A door placed this way gets the parameters of that library
# part, so GetGDLParametersOfElements and SetGDLParametersOfElements work on it, and so does a
# Favorite made from it.

wall = aclib.RunTapirCommand (
    'CreateWalls', {
        'wallsData': [{
            'begCoordinate': {'x': 2000.0, 'y': 0.0},
            'endCoordinate': {'x': 2006.0, 'y': 0.0},
            'zCoordinate': 0.0,
            'height': 3.0,
            'thickness': 0.3
        }]
    })['elements']

door = aclib.RunTapirCommand (
    'CreateDoors', {
        'doorsData': [{
            'ownerWallId': wall[0]['elementId'],
            'centerOffset': 2.0,
            'libraryPartName': 'Double Door with Transom 27'
        }]
    })['elements']

if 'elementId' in door[0]:
    parameters = aclib.RunTapirCommand (
        'GetGDLParametersOfElements', {'elements': door}, debug=False)['gdlParametersOfElements'][0]
    transom = [p for p in parameters.get ('parameters', []) if p['name'] == 'gs_transom_height']
    if transom:
        aclib.RunTapirCommand (
            'SetGDLParametersOfElements', {
                'elementsWithGDLParameters': [{
                    'elementId': door[0]['elementId'],
                    'gdlParameters': [{'name': 'gs_transom_height', 'value': transom[0]['value'] + 0.1}]
                }]
            })

aclib.RunTapirCommand (
    'DeleteElements', {'elements': [e for e in door + wall if 'elementId' in e]})

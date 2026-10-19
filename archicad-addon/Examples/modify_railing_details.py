"""
Changes the details of an existing Railing with ModifyRailingDetails.

The height of the segments, the post spacing (the length of the railing pattern), the handrail
and the visibility of the infill panels are changed. The response lists the fields Archicad kept
('changed'), the ones it did not with the reason ('notChanged') and the railing as read back
('state': segment heights, pattern lengths, the number of posts, inner posts, panels, balusters).
"""

import aclib

railing = aclib.RunTapirCommand ('CreateRailings', {
    'railingsData': [{
        'coordinates': [ { 'x': 0.0, 'y': 0.0 }, { 'x': 6.0, 'y': 0.0 } ]
    }]
})['elements'][0]

response = aclib.RunTapirCommand ('ModifyRailingDetails', {
    'railings': [{
        'elementId': railing['elementId'],
        'height': 1.1,
        'postSpacing': 1.5,
        'postDistribution': 'BestDivision',
        'handrail': { 'visible': True, 'height': 0.9 },
        'panel': { 'visible': True }
    }]
})
print (response)

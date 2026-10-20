"""
Sets the Railing tool defaults with SetRailingSystemDefaults: a 1.0 m railing with posts every
1.5 m, a handrail at 1.0 m, five horizontal rails spaced evenly below it and a glass panel infill,
saved as a Favorite; then a railing is created from the Favorite. The command reads the defaults
back: 'kept', 'notKept' (with the reason) and 'unverified' (written but not readable from the
defaults) list every asked field. The GDL parameter names of the panel and rail objects depend on
the library; GetRailingSystemDefaults with includeParameters lists them.
"""

import aclib

response = aclib.RunTapirCommand ('SetRailingSystemDefaults', {
    'segment': { 'height': 1.0 },
    'posts': { 'spacing': 1.5, 'distribution': 'BestDivision' },
    'handrail': { 'visible': True, 'height': 1.0 },
    'rails': { 'count': 5, 'bottom': 0.1, 'top': 0.9 },
    'infill': { 'kind': 'panel' },
    'favorite': { 'name': 'Example railing 5 rails + glass', 'overwriteExisting': True },
    'restoreDefaults': True
})
print ('kept:', response.get ('kept'))
print ('notKept:', response.get ('notKept'))
print ('unverified:', response.get ('unverified'))

railing = aclib.RunTapirCommand ('CreateRailings', {
    'railingsData': [{
        'favoriteName': 'Example railing 5 rails + glass',
        'coordinates': [ { 'x': 0.0, 'y': 0.0 }, { 'x': 6.0, 'y': 0.0 } ]
    }]
})
print (railing)

aclib.RunTapirCommand ('DeleteFavorites', { 'favorites': ['Example railing 5 rails + glass'] })

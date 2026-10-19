"""
Creates Railing elements with CreateRailings.

A railing is placed along a reference polyline on the floor plan; every point becomes a
node and every pair of consecutive points a segment. The posts, rails, panels, balusters
and their distribution come from the Railing tool defaults of the open project (or from
'favoriteName'), so the examples only give the reference line and the base level.
"""

import aclib

# --- 1. the simplest call: only the reference line ---------------------------
aclib.RunTapirCommand ('CreateRailings', {
    'railingsData': [
        {
            'coordinates': [
                { 'x': 0.0, 'y': 0.0 },
                { 'x': 6.0, 'y': 0.0 }
            ]
        }
    ]
})

# --- 2. an L-shaped balcony railing 3.0 m above the project zero --------------
response = aclib.RunTapirCommand ('CreateRailings', {
    'railingsData': [
        {
            'coordinates': [
                { 'x': 0.0, 'y': 5.0 },
                { 'x': 6.0, 'y': 5.0 },
                { 'x': 6.0, 'y': 7.5 }
            ],
            'zCoordinate': 3.0
        }
    ]
})

# --- 3. read back the posts, rails and panels of the new railing ----------------
railing = response['elements'][0]
print (aclib.RunTapirCommand ('GetSubelementsOfHierarchicalElements', {
    'elements': [railing]
}))

# --- 4. a 1.10 m high railing with posts every 1.5 m -----------------------------
# ModifyRailingDetails changes these (and the handrail, rails, panels) on an existing railing.
aclib.RunTapirCommand ('CreateRailings', {
    'railingsData': [
        {
            'coordinates': [
                { 'x': 0.0, 'y': 10.0 },
                { 'x': 6.0, 'y': 10.0 }
            ],
            'height': 1.1,
            'postSpacing': 1.5,
            'postDistribution': 'BestDivision'
        }
    ]
})

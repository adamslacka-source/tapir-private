"""
Reads the Railing tool defaults with GetRailingSystemDefaults: the node and segment templates
(posts, pattern, panel, balusters, top rail, handrails, rails) with their library parts, the memo
of the defaults and every Railing subelement default Archicad gives through
ACAPI_Element_GetDefaultsExt, with the names of their GDL parameters. The same command reads a
Railing Favorite, here the first one of the project.
"""

import aclib

defaults = aclib.RunTapirCommand ('GetRailingSystemDefaults', { 'includeParameters': True })
print ('segment height:', defaults.get ('railing', {}).get ('segment', {}).get ('height'))
for item in defaults.get ('subElementProbe', []):
    sub = item.get ('subElement')
    print (item['kind'], 'defaultDefault' if item['defaultDefault'] else '',
           'error', item['errorWithType'], item.get ('errorWithoutType', ''),
           sub.get ('gdlParameters', {}).get ('count') if sub else '-')

favorites = aclib.RunTapirCommand ('GetFavoritesByType', { 'elementType': 'Railing' }).get ('favorites', [])
if favorites:
    favorite = aclib.RunTapirCommand ('GetRailingSystemDefaults', { 'favoriteName': favorites[0], 'probeSubElements': False })
    print (favorites[0], [s['kind'] for s in favorite.get ('favorite', {}).get ('subElements', [])])

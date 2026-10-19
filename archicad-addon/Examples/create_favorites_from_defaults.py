import aclib

# Saves the current defaults of the Beam tool as a Favorite, without placing a sample beam.
# A second run with the same name is refused unless overwriteExisting is true.
# Library part based tools (Door, Window, Object...) keep their GDL parameters in the Favorite.

favoriteName = 'Beam defaults from Python'

aclib.RunTapirCommand (
    'CreateFavoritesFromDefaults', {
        'favorites': [{
            'name': favoriteName,
            'elementType': 'Beam',
            'overwriteExisting': True
        }]
    })

beamFavorites = aclib.RunTapirCommand (
    'GetFavoritesByType', {'elementType': 'Beam'})['favorites']
print ('Listed among Beam favorites: {}'.format (favoriteName in beamFavorites))

aclib.RunTapirCommand (
    'DeleteFavorites', {'favorites': [favoriteName]})

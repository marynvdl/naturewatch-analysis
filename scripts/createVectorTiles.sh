# Using Mapbox' tilesets-cli: https://github.com/mapbox/tilesets-cli/
# Follow this tutorial: https://docs.mapbox.com/help/tutorials/get-started-mts-and-tilesets-cli/

export MAPBOX_ACCESS_TOKEN=####

# Create a tileset source
tilesets upload-source nature-watch dams-test-source ./data/vector/dams.geojson.ld

# Create a new tileset
tilesets create nature-watch.dam-test-tiles --recipe ./mapbox_recipes/damsRecipe.json --name "Dams test"

# Publish tileset
tilesets publish nature-watch.dam-test-tiles

# Check status
tilesets status nature-watch.dam-test-tiles
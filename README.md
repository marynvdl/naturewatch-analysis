# naturewatch-analysis

This repository publishes data for [naturewatch-app](https://github.com/marynvdl/naturewatch-app). The app reads the published objects. It does not run this code.

[docs/pipelines.md](docs/pipelines.md) is how a dataset becomes those objects. This page is the front door. It does not repeat that path.

## Live

- **Cropland 2024.** The recipe is `cookbook/recipes/cropland-2024.yaml`. The seed run is [seed/README.md](seed/README.md). The sprout run is [sprout/README.md](sprout/README.md). The facts run is [facts/README.md](facts/README.md). The tile run is [tiles/README.md](tiles/README.md).
- **Fire.** Export from Earth Engine, then build tiles. Both are started by hand. See [gee/README.md](gee/README.md).

`gs://nature-watch-bucket` is private. Map tiles stay on public `gs://nature-watch-tiles`.

## Archived

Older tile tasks and the dams Mapbox recipe are in `archive/`. They are not run. Do not move a workflow file from `archive/` back into `.github/workflows/`.

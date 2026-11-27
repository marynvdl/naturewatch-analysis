# energy: power lines and power sites

Scripts for the Energy layer: one seed read from Overture's `base/infrastructure` theme, two sprouts on the stats grid, a raster PMTiles archive for power lines and a vector one for power sites. The recipes are [`cookbook/recipes/power-lines-2026-10-v1.yaml`](../cookbook/recipes/power-lines-2026-10-v1.yaml) and [`power-sites-2026-10-v1.yaml`](../cookbook/recipes/power-sites-2026-10-v1.yaml), and `rules.py` reads its rules from them. The parcel is `docs/parcels/energy.md` in naturewatch-app.

Task 01 ran these on the laptop for two windows. The user approved the classes, the attributes, leaving out Global Renewables Watch, a seed of every infrastructure subtype, the tile settings and the heatmap design on 27 November 2026.

## Run

```bash
pip install -r energy/requirements.txt
python energy/seed.py all                 # every infrastructure row in each window: data/energy/raw/<name>.parquet
python energy/seed.py seed                # the window seeds: data/energy/seed/<name>.parquet
python energy/seed.py schema              # data/energy/schema-classes.csv and schema-tags.csv
python energy/seed.py footers             # data/energy/footers.json: rows per 10° block, footers only
python energy/sprout.py sites karoo       # data/energy/sites/karoo.parquet: kept sites, group and card
python energy/sprout.py window karoo      # data/energy/sprout/power-lines/karoo.tif and power-sites/karoo.tif
python energy/tiles.py lines karoo        # data/energy/tiles/power-lines-karoo.pmtiles
python energy/tiles.py sites karoo        # data/energy/tiles/power-sites-karoo.pmtiles (Tippecanoe)
python energy/render.py check karoo --bbox 23.98 -30.70 24.12 -30.60 --zoom 14   # features over imagery
python energy/render.py sample karoo --lines data/energy/tiles/power-lines-karoo.pmtiles --zoom 8
MAPBOX_ACCESS_TOKEN=pk... python energy/preview.py   # http://127.0.0.1:8766/preview.html
python -m pytest energy
```

`boxes.yaml` names the two 2° windows: `karoo` (22.3–24.3°E, 32.6–30.6°S: Beaufort West, Karoo National Park, De Aar) and `gironde` (1.5°W–0.5°E, 43.8–45.8°N: Bordeaux, the Landes, Cestas). Overture is read with DuckDB from its public bucket, only the row groups whose bbox meets the window. Global Renewables Watch v1.0 is read from its two GeoPackages in `data/energy/grw/`. Imagery is Esri World Imagery, cached in `data/energy/imagery/`.

## Method

**Seed.** Every `base/infrastructure` row, so Water can reuse the read: Overture `id`, `subtype`, `class`, the primary name, the first OpenStreetMap record without its version (`n…`, `w…`, `r…`), `wikidata`, `source_tags` (the raw OpenStreetMap tags), `height`, `surface`, `level`, `bbox` and the geometry.

**Power lines.** `power_line`, `minor_line`, and `cable` only when tagged `location=overhead`. A line tagged underground, underwater or indoor is left out. The voltage class comes from the highest value in `voltage`:

| Class | Name | Voltage | Seen in the windows |
| --- | --- | --- | --- |
| 4 | extra-high | 330 kV and up | 400, 765 kV |
| 3 | transmission | 200–330 kV | 220, 225 kV |
| 2 | sub-transmission | 50–200 kV | 63, 66, 90, 132 kV |
| 1 | distribution | under 50 kV | 20, 33 kV, 400 V |

Without a voltage, a `power_line` is 2 and a `minor_line` 1. A cell is a line cell when a kept line touches it (all-touched, as roads), and the highest class wins.

**Power sites.** Each kept site has a source group. A site with several sources takes the first in this order, so the dirtiest wins: coal, oil and gas (`fossil`), nuclear, bioenergy and waste, hydro, wind, solar, other, storage. Substations are their own group.

- Plants: kept, as polygons and points, group from `plant:source`.
- Generators: group from `generator:source`. Polygons from 0.1 ha. Points are kept except solar, so a wind turbine marks its own cell. A generator on a roof is left out.
- Substations: polygons from 100 m². Points are left out.
- Towers, poles, switches, portals, transformers and insulators are left out.

A polygon marks the cells whose centre it holds, a point the cell it falls in. The burn value rises with the group, so the dirtiest group wins a cell: 1 substation, 2 storage, 3 other, 4 solar, 5 wind, 6 hydro, 7 bioenergy and waste, 8 nuclear, 9 oil and gas, 10 coal.

**Feature card.** Each site keeps `name`, `operator`, `source` (raw `plant:source` or `generator:source`), `method`, `output` (`plant:output:electricity` or `generator:output:electricity`), `voltage`, `start_date` and `osm_id`, with `class`, `group` and `area_m2`.

**Tiles.** Power lines use the roads encoding (`roads/tiles.py`, which reads a one-band sprout as level with no surface): 512 px palette PNG, top zoom 11, below it 8 shades in √(share / 0.1), at zoom 11 red = class × 4. Power sites are one Tippecanoe layer `sites`, zooms 2–14. To zoom 9 each site is a point inside it, for a heatmap drawn in the client. From zoom 10 it is its own shape. Crowded points merge (`--cluster-densest-as-needed`, 12 px), keeping `point_count` and summing `area_m2` and, per group g, `area_<g>` and `n_<g>`. No site's weight is lost zoomed out, and the client can draw the heat of one group.

## Task 01 sample (27 November 2026)

Overture release `2026-10-21.0`. Laptop, DuckDB 2.0.0, GDAL 3.11.4, Tippecanoe, Python 3.14.

### What the theme holds

| | karoo | gironde |
| --- | --- | --- |
| Infrastructure rows | 9,023 (86% `power`, mostly 5,655 towers) | 398,918 (16% `power`) |
| Raw read | 17.5 s, 0.79 MB, peak 0.57 GB | 26.8 s, 34.5 MB, peak 1.24 GB |
| Power lines with `voltage` | 95 of 99 | 1,508 of 1,541 |
| Plants with source / name / output / start date | 11 / 10 / 11 / 5 of 11 | 219 / 138 / 208 / 145 of 221 |
| Plant `operator` | 5 of 11 | 55 of 221 |
| Generators with a source | 1,646 of 1,646 | 6,688 of 6,705 |
| Generator polygons inside a plant | 1,307 of 1,646 | 4,985 of 6,705 |
| Wind turbines | 205 points, no wind-farm polygon | 21 |

`owner` is never tagged. Water and sewage treatment plants and pumping stations are not in `base/infrastructure` in either window. The `water` subtype holds dams, weirs and drinking water, and `utility` holds pipelines, covered reservoirs and water towers. The schema counts are in `data/energy/schema-classes.csv` and `schema-tags.csv` (not committed).

### Global Renewables Watch, compared

Global Renewables Watch v1.0 (March 2025): solar PV polygons and onshore wind turbine points to the second quarter of 2024, detected from imagery. The repository's only licence is MIT, and the dataset states none of its own.

| | karoo | gironde |
| --- | --- | --- |
| Solar, OpenStreetMap (union) | 17.14 km² | 38.52 km² |
| Solar, Global Renewables Watch | 5 features, 1.88 km² | 223 features, 30.96 km² |
| Overlap | 1.69 km² | 28.66 km² |
| Only in Global Renewables Watch | 1 feature, 0.13 km² | 33 features, 1.07 km² |
| OpenStreetMap sites of 1 ha and up not in Global Renewables Watch | 4, 13.27 km² | 38, 4.79 km² |
| Wind turbines, OpenStreetMap / Global Renewables Watch | 205 / 108, all 108 within 150 m of an OpenStreetMap turbine | 21 / 0 |

Over imagery: the four large De Aar plants (645, 296, 200 and 187 ha) are real and missing from Global Renewables Watch. So are the 97 OpenStreetMap turbines near 24.0°E 31.85°S, on pads and access roads. The largest Global Renewables Watch-only feature in Gironde (8 ha at 0.30°E 44.34°N) is a vineyard, and the largest OpenStreetMap-only one (59 ha at 0.65°W 44.11°N) is a solar park. Global Renewables Watch is not used. It is in the parcel's Deferred list as a possible gap-fill where OpenStreetMap is thin.

### Sprouts and tiles per window

| | karoo | gironde |
| --- | --- | --- |
| Line features burned | 114 | 5,159 |
| Line cells (of 64,000,000) | 104,348 | 351,347 |
| `power-lines` COG | 144,160 bytes | 399,119 bytes |
| Sites kept | 296 (9 solar plants 17.1 km², 51 solar generators, 205 turbines, 29 substations, 1 storage, 1 oil) | 1,141 (188 solar plants 37.7 km², 650 solar generators, 229 substations, 21 turbines, 1 nuclear plant, 15 bioenergy, 6 oil and gas, 5 hydro) |
| Site cells | 24,907 | 74,846 |
| `power-sites` COG | 79,093 bytes | 92,592 bytes |
| Burn, both layers | under 1 s, peak 1.0 GB | under 1 s, peak 1.0 GB |
| `power-lines` archive | 105 tiles, 139,835 bytes, largest 5,640 | 251 tiles, 645,991 bytes, largest 12,478 |
| `power-sites` archive | 387 features written, 97,708 bytes, largest tile 1,411 | 2,242 features written, 657,889 bytes, largest tile 5,970 |

A burn holds the 64 million-cell window in memory. The tile builds take under 6 s at a peak of 0.3 GB.

### Choices

- **Share curve.** `share_full` 0.25 (the roads value) left the Karoo transmission lines faint at zoom 6. At 0.05, Gironde's 20 kV network became a haze. 0.1 shows lines at zooms 4–8 in both. France maps its distribution lines and the Karoo barely does, so Gironde looks denser zoomed out partly for that reason. The voltage band lets the pressure sandbox weight only lines from 50 kV up.
- **Top zoom.** 11, as roads. The 765 and 400 kV corridors at De Aar and the 225 kV lines around Cestas are crisp there.
- **Sites zoomed out.** Five views were compared in `preview.py`: points with the densest dropped, clusters with a count, points by size, a heatmap, and the footprint as a raster share. The user chose the heatmap. Its colours, radius, intensity, opacity and weight are paint only. The preview offers five palettes, and a weight of √area with each site at least 1 ha.

Renders are in `data/energy/renders/` (not committed).

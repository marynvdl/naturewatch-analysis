# gee: Google Earth Engine exports

Python scripts that run in Google Earth Engine and write cloud-optimised GeoTIFFs (COGs) to Cloud Storage. The tile tasks in `../tasks` turn those COGs into map tiles.

```
GEE export (this folder)  ->  gs://nature-watch-bucket/COGS/<layer>/<year>/*.tif
tile task (../tasks)      ->  gs://nature-watch-tiles/<layer>/<year>/{z}/{x}/{y}.png
app catalog (naturewatch-app, layers.json)  ->  the year appears on the map
```

## Fire

`fire/export_fire.py` is the Python version of the `fire_exportFirms` notebook.

For one calendar year it counts, per pixel, in how many 14-day windows NASA FIRMS saw a fire, and exports one COG per block in `blocks/africa_blocks.geojson` to `gs://nature-watch-bucket/COGS/fire/<year>/fire<year>_<n>.tif`.

### Run it from GitHub
**Actions -> Fire export -> Run workflow**, then pick an action:

| Action | What it does |
|---|---|
| `check` | Logs in to Earth Engine, checks FIRMS covers the whole year, and runs the real calculation on a small test area. Start here. |
| `export-dry-run` | Shows what an export would do. Starts nothing. |
| `export` | Starts one Earth Engine task per block, then finishes. The export itself runs inside Earth Engine and takes a few hours. Blocks whose file already exists are skipped unless `overwrite` is ticked. |
| `status` | Shows the state of the tasks (`READY`, `RUNNING`, `COMPLETED`, `FAILED`). |

When every block is `COMPLETED`, run **Fire tiles** for the same year. Use the `blocks` input (for example `3,7-9`) to re-run only some blocks.

### Run it on a laptop
```
pip install -r gee/requirements.txt
cd gee/fire
python export_fire.py plan   --year 2025        # no login needed
python export_fire.py check  --year 2025
python export_fire.py export --year 2025 --dry-run
python export_fire.py export --year 2025
python export_fire.py status --year 2025
python -m pytest .                              # tests, no login needed
```
Log in once with `earthengine authenticate`, or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key (the workflow does this).

The Google Cloud project of the service account (the `GCP_SA_KEY` secret) must be registered for Earth Engine and be allowed to write to `nature-watch-bucket`. If `check` fails at login, that is the first thing to look at.

### Blocks
`blocks/africa_blocks.geojson` has 25 squares of 15 degrees that cover Africa, Madagascar and the western Indian Ocean. They exist to keep each export below Earth Engine's size limits. Block id `n` is written to `fire<year>_<n-1>.tif`, which is how the notebook numbered the files, so names stay the same as in 2016-2024. Some blocks are mostly sea.

### Same calculation as the notebook, on purpose
So a new year is comparable with 2016-2024, the calculation is unchanged, including two quirks:
- **27 windows, not 26.** The 14-day windows start on 1 January and every 14 days up to 31 December, which gives 27 starts. The last one is only 1-2 days inside the year, so a pixel can count up to 27 (about 940 pixels reach 27 in 2024). The map's legend text says the maximum is 26.
- **The last window runs into January.** Every window is 14 days long, so the one starting on 30/31 December also counts detections up to about 13 January of the next year.

If you fix either, fix it for every year at once, or the years stop being comparable.

### Not tested here
Everything except the Earth Engine calls is covered by the tests (date windows, block ids, file names, export settings, the command line). The Earth Engine part can only be tested with a login; the `check` action runs it against a small area for that reason.

### Next
- Global coverage, not only Africa.
- Year windows that follow the dry and rainy seasons per latitude.
- A better layout than 25 fixed blocks. The true resolution of this layer is 1 km, so the 30 m export is far larger than it needs to be (about 1.1 GB per year for data that is about 3 MB at 1 km).

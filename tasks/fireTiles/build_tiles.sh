#!/usr/bin/env bash
# Build fire map tiles for one year from the COGs that the GEE export wrote.
#
# Usage: build_tiles.sh <cog_dir> <work_dir> [max_zoom]
#
# Reads   <cog_dir>/*.tif
# Writes  <work_dir>/tiles/{z}/{x}/{y}.png   (XYZ scheme, zoom 0..max_zoom)
#
# Needs GDAL (gdalbuildvrt, gdalwarp, gdaldem, gdal2tiles.py). It does no cloud
# access, so it runs the same on a laptop and in the GitHub Action container.
#
# Why this is cheap: the FIRMS fire counts are aggregated to a 1 km grid in the
# GEE export (reproject(..., 1000)), so the 30 m COGs hold about 1000x more
# pixels than information. GDAL reads the COG overviews when it resamples to
# 1 km, and zoom 9 (about 300 m per pixel) is already finer than the data, so
# higher zoom levels only add tiles, not detail. The map enlarges zoom 9 itself.
set -euo pipefail

COG_DIR="${1:?usage: build_tiles.sh <cog_dir> <work_dir> [max_zoom]}"
WORK_DIR="${2:?usage: build_tiles.sh <cog_dir> <work_dir> [max_zoom]}"
MAX_ZOOM="${3:-9}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLOR_FILE="${COLOR_FILE:-$HERE/color.txt}"

# Absolute paths: the script changes into WORK_DIR below
[ -d "$COG_DIR" ] || { echo "ERROR: $COG_DIR is not a directory" >&2; exit 1; }
COG_DIR="$(cd "$COG_DIR" && pwd)"
mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

shopt -s nullglob
COGS=("$COG_DIR"/*.tif)
if [ "${#COGS[@]}" -eq 0 ]; then
  echo "ERROR: no .tif files in $COG_DIR" >&2
  exit 1
fi

cd "$WORK_DIR"
rm -rf tiles merged.vrt fire_1km.tif fire_colored.tif

echo "1. Virtual mosaic of ${#COGS[@]} COGs"
# -strict makes GDAL fail on a file it cannot open instead of skipping it,
# which would leave a hole in the map.
gdalbuildvrt -q -strict merged.vrt "${COGS[@]}"

echo "2. Resample to the native 1 km grid in Web Mercator"
# Do not set a nodata value here: 0 is a real value (no fire) and is made
# transparent by the colour table.
gdalwarp -q -overwrite -t_srs EPSG:3857 -r near -tr 1000 1000 -tap \
  -multi -wo NUM_THREADS=ALL_CPUS -co COMPRESS=DEFLATE -co TILED=YES \
  merged.vrt fire_1km.tif

echo "3. Apply the colour table (0 = transparent)"
gdaldem color-relief -q -alpha fire_1km.tif "$COLOR_FILE" fire_colored.tif \
  -co COMPRESS=DEFLATE

echo "4. Make tiles, zoom 0-$MAX_ZOOM"
PROCESSES="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
gdal2tiles.py -q -z "0-$MAX_ZOOM" -r near -w none -a 0 --xyz \
  --processes="$PROCESSES" fire_colored.tif tiles

COUNT="$(find tiles -name '*.png' | wc -l | tr -d ' ')"
if [ "$COUNT" -lt 100 ] || [ ! -f tiles/0/0/0.png ]; then
  echo "ERROR: only $COUNT tiles were made, or the zoom 0 tile is missing" >&2
  exit 1
fi
echo "Done: $COUNT tiles, $(du -sh tiles | cut -f1)"

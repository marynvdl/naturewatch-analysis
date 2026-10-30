#!/usr/bin/env python3
"""Write the cropland sprout: one COG and the static STAC files beside it.

The recipe is the catalog. This script reads it and does not edit it.
Seed value 0 becomes 0 and seed value 100 becomes 1. The South Africa clip
also maps 255 to nodata. The global recipe has no clip: the sprout mosaics
the checked tiles, and a tile the publisher does not offer stays nodata.

The published run is a Batch job in europe-west1. The laptop only submits
that job.

  python sprout/cropland.py submit
  python sprout/cropland.py submit --recipe cookbook/recipes/cropland-2024-v2.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from osgeo import gdal, osr

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "cropland-2024.yaml"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
MACHINE = "e2-standard-2"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
SCRATCH = "gs://nature-watch-bucket/scratch/sprout/cropland-2024/"
BATCH_ENV = "NATUREWATCH_SPROUT_BATCH"
# Stop adding overviews once the longest side is this many pixels or fewer.
OVERVIEW_MIN = 512

DTYPE_NAME = {gdal.GDT_Byte: "uint8"}


def load_recipe(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def mapping(recipe: dict) -> dict[int, int]:
    """Seed value to sprout value. JSON object keys arrive as strings."""
    return {int(src): int(dst) for src, dst in recipe["sprout"]["from_seed"].items()}


def published_values(recipe: dict) -> dict[str, int]:
    return {name: int(value) for name, value in recipe["sprout"]["values"].items()}


def version_of(recipe: dict) -> str:
    folder = recipe["sprout"]["path"].rstrip("/").split("/")[-2]
    if len(folder) < 2 or folder[0] != "v" or not folder[1:].isdigit():
        raise ValueError(f"sprout path has no version folder: {recipe['sprout']['path']}")
    return folder


def class_values_text(recipe: dict) -> str:
    values = published_values(recipe)
    parts = [f"{name}:{value}" for name, value in values.items()]
    return ",".join(parts)


def spatial_reference(wkt: str) -> str:
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    name = srs.GetAuthorityName(None) or srs.GetAuthorityName("GEOGCS")
    code = srs.GetAuthorityCode(None) or srs.GetAuthorityCode("GEOGCS")
    if not name or not code:
        raise ValueError(f"raster CRS has no EPSG code: {wkt}")
    return f"{name}:{code}"


def raster_facts(path: Path) -> dict:
    dataset = gdal.Open(str(path))
    band = dataset.GetRasterBand(1)
    transform = dataset.GetGeoTransform()
    nodata = band.GetNoDataValue()
    # A histogram skips the nodata value. Clear it in memory only; the file is unchanged.
    if nodata is not None:
        band.DeleteNoDataValue()
    histogram = band.GetHistogram(
        min=-0.5, max=255.5, buckets=256, include_out_of_range=True, approx_ok=False
    )
    counts = {value: int(count) for value, count in enumerate(histogram) if count}
    facts = {
        "width": dataset.RasterXSize,
        "height": dataset.RasterYSize,
        "crs": spatial_reference(dataset.GetProjection()),
        "pixel_size_deg": abs(transform[1]),
        "transform": transform,
        "dtype": DTYPE_NAME.get(band.DataType, gdal.GetDataTypeName(band.DataType)),
        "nodata": None if nodata is None else int(nodata),
        "counts": counts,
        "projection": dataset.GetProjection(),
        "version": dataset.GetMetadataItem("version"),
        "class_values": dataset.GetMetadataItem("class_values"),
        "overviews": band.GetOverviewCount(),
        "layout": dataset.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE"),
    }
    dataset = None
    return facts


def raster_frame(path: Path) -> dict:
    """Grid only. A histogram of the global mosaic is a separate full read."""
    dataset = gdal.Open(str(path))
    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    frame = {
        "width": dataset.RasterXSize,
        "height": dataset.RasterYSize,
        "transform": dataset.GetGeoTransform(),
        "projection": dataset.GetProjection(),
        "nodata": None if nodata is None else int(nodata),
    }
    dataset = None
    return frame


def require_mapped(counts: dict[int, int], recipe: dict) -> None:
    table = mapping(recipe)
    extra = sorted(set(counts) - set(table))
    if extra:
        raise ValueError(f"source values {extra} are not in sprout.from_seed")
    published = set(published_values(recipe).values())
    bad = sorted(dst for dst in table.values() if dst not in published)
    if bad:
        raise ValueError(f"from_seed writes {bad}, which are not sprout values")


def expected_counts(clip_counts: dict[int, int], recipe: dict) -> dict[int, int]:
    expected: dict[int, int] = {}
    for src, dst in mapping(recipe).items():
        count = clip_counts.get(src, 0)
        if count:
            expected[dst] = expected.get(dst, 0) + count
    return expected


def bounds_of(transform, width: int, height: int) -> tuple[float, float, float, float]:
    minx = transform[0]
    maxy = transform[3]
    maxx = minx + transform[1] * width
    miny = maxy + transform[5] * height
    return minx, miny, maxx, maxy


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _progress(label: str):
    last = {"value": -1}

    def progress(complete, _message, _data):
        percent = int(complete * 20) * 5
        if percent != last["value"]:
            last["value"] = percent
            print(f"{label} {percent}%", flush=True)
        return 1

    return progress


def write_remap_vrt(clip: Path, dest: Path, recipe: dict, projection: str) -> None:
    dataset = gdal.Open(str(clip))
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    transform = dataset.GetGeoTransform()
    dataset = None
    pairs = ",".join(f"{src}:{dst}" for src, dst in sorted(mapping(recipe).items()))
    nodata = published_values(recipe)["nodata"]
    geo = ", ".join(f"{value:.12g}" for value in transform)
    dest.write_text(
        f"""<VRTDataset rasterXSize="{width}" rasterYSize="{height}">
  <SRS>{xml_escape(projection)}</SRS>
  <GeoTransform>{geo}</GeoTransform>
  <Metadata>
    <MDI key="version">{xml_escape(version_of(recipe))}</MDI>
    <MDI key="class_values">{xml_escape(class_values_text(recipe))}</MDI>
  </Metadata>
  <VRTRasterBand dataType="Byte" band="1">
    <NoDataValue>{nodata}</NoDataValue>
    <ComplexSource>
      <SourceFilename relativeToVRT="0">{xml_escape(str(clip))}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>
      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>
      <LUT>{pairs}</LUT>
    </ComplexSource>
  </VRTRasterBand>
</VRTDataset>
"""
    )


def write_remapped(clip: Path, dest: Path, recipe: dict) -> dict:
    facts = raster_facts(clip)
    if facts["transform"][2] != 0 or facts["transform"][4] != 0:
        raise ValueError(f"{clip.name} is rotated or sheared")
    if facts["crs"] != recipe["grid"]["crs"]:
        raise ValueError(f"clip CRS is {facts['crs']}, recipe says {recipe['grid']['crs']}")
    if abs(facts["pixel_size_deg"] - float(recipe["grid"]["pixel_size_deg"])) > 1e-12:
        raise ValueError(
            f"clip pixel size is {facts['pixel_size_deg']}, recipe says {recipe['grid']['pixel_size_deg']}"
        )
    require_mapped(facts["counts"], recipe)
    vrt = dest.with_suffix(".vrt")
    write_remap_vrt(clip, vrt, recipe, facts["projection"])
    if dest.exists():
        dest.unlink()
    gdal.Translate(
        str(dest),
        str(vrt),
        format="GTiff",
        noData=published_values(recipe)["nodata"],
        creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"],
        callback=_progress("remap"),
    )
    return facts


def overview_sizes(width: int, height: int) -> list[tuple[int, int]]:
    sizes = []
    while max(width, height) > OVERVIEW_MIN:
        width = max(1, width // 2)
        height = max(1, height // 2)
        sizes.append((width, height))
    return sizes


def write_max_overviews(source: Path, work: Path) -> list[Path]:
    """GDAL 3.11 cannot resample COG overviews with max. Warp can.

    Each level is the max of the previous one, nodata ignored, so a cropland
    pixel survives and a block of only nodata stays nodata. Max of max over
    nested windows is the max of the full window.
    """
    dataset = gdal.Open(str(source))
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    transform = dataset.GetGeoTransform()
    nodata = int(dataset.GetRasterBand(1).GetNoDataValue())
    dataset = None
    extent = bounds_of(transform, width, height)
    paths = []
    current = source
    for index, (level_width, level_height) in enumerate(overview_sizes(width, height), start=1):
        dest = work / f"overview-{index}.tif"
        if dest.exists():
            dest.unlink()
        gdal.Warp(
            str(dest),
            str(current),
            format="GTiff",
            width=level_width,
            height=level_height,
            outputBounds=extent,
            resampleAlg="max",
            srcNodata=nodata,
            dstNodata=nodata,
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"],
            callback=_progress(f"overview {index}"),
        )
        paths.append(dest)
        current = dest
    return paths


def write_cog_vrt(source: Path, overviews: list[Path], dest: Path, recipe: dict) -> None:
    facts = raster_frame(source)
    geo = ", ".join(f"{value:.12g}" for value in facts["transform"])
    overview_xml = "\n".join(
        f"""    <Overview>
      <SourceFilename relativeToVRT="0">{xml_escape(str(path))}</SourceFilename>
      <SourceBand>1</SourceBand>
    </Overview>"""
        for path in overviews
    )
    width = facts["width"]
    height = facts["height"]
    dest.write_text(
        f"""<VRTDataset rasterXSize="{width}" rasterYSize="{height}">
  <SRS>{xml_escape(facts["projection"])}</SRS>
  <GeoTransform>{geo}</GeoTransform>
  <Metadata>
    <MDI key="version">{xml_escape(version_of(recipe))}</MDI>
    <MDI key="class_values">{xml_escape(class_values_text(recipe))}</MDI>
  </Metadata>
  <VRTRasterBand dataType="Byte" band="1">
    <NoDataValue>{published_values(recipe)["nodata"]}</NoDataValue>
    <SimpleSource>
      <SourceFilename relativeToVRT="0">{xml_escape(str(source))}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>
      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>
    </SimpleSource>
{overview_xml}
  </VRTRasterBand>
</VRTDataset>
"""
    )


def write_cog(source: Path, overviews: list[Path], dest: Path, recipe: dict) -> None:
    vrt = dest.with_suffix(".vrt")
    write_cog_vrt(source, overviews, vrt, recipe)
    if dest.exists():
        dest.unlink()
    # Metadata and noData stay in the VRT. Passing them as translate options
    # hides the overviews this driver is asked to copy.
    gdal.Translate(
        str(dest),
        str(vrt),
        format="COG",
        creationOptions=[
            "COMPRESS=DEFLATE",
            "LEVEL=9",
            "PREDICTOR=STANDARD",
            "BLOCKSIZE=512",
            "OVERVIEWS=FORCE_USE_EXISTING",
            "OVERVIEW_COMPRESS=DEFLATE",
            "OVERVIEW_PREDICTOR=STANDARD",
            "BIGTIFF=IF_SAFER",
            "NUM_THREADS=ALL_CPUS",
            "STATISTICS=YES",
        ],
        callback=_progress("cog"),
    )


def check_sprout(source: dict, sprout: dict, recipe: dict) -> list[str]:
    problems = []
    values = published_values(recipe)
    if sprout["layout"] != "COG":
        problems.append(f"layout is {sprout['layout']}, expected COG")
    if sprout["crs"] != recipe["grid"]["crs"]:
        problems.append(f"CRS is {sprout['crs']}, recipe says {recipe['grid']['crs']}")
    if sprout["dtype"] != recipe["grid"]["dtype"]:
        problems.append(f"dtype is {sprout['dtype']}, recipe says {recipe['grid']['dtype']}")
    if sprout["nodata"] != values["nodata"]:
        problems.append(f"nodata is {sprout['nodata']}, recipe says {values['nodata']}")
    if sprout["version"] != version_of(recipe):
        problems.append(f"version is {sprout['version']}, path says {version_of(recipe)}")
    if sprout["class_values"] != class_values_text(recipe):
        problems.append(f"class values are {sprout['class_values']}")
    if sprout["width"] != source["width"] or sprout["height"] != source["height"]:
        problems.append(
            f"size is {sprout['width']}×{sprout['height']}, source is {source['width']}×{source['height']}"
        )
    if sprout["transform"] != source["transform"]:
        problems.append("geotransform changed between the source and the sprout")
    if abs(sprout["pixel_size_deg"] - float(recipe["grid"]["pixel_size_deg"])) > 1e-12:
        problems.append(f"pixel size is {sprout['pixel_size_deg']}")
    expected = expected_counts(source["counts"], recipe)
    accounted = sum(expected.values())
    total = sprout["width"] * sprout["height"]
    gap = total - accounted
    # A clip's nodata is already in from_seed. A mosaic's missing tiles are not.
    if gap < 0:
        problems.append(f"source counts cover {accounted} pixels of {total}")
    elif gap:
        expected[values["nodata"]] = expected.get(values["nodata"], 0) + gap
    if sprout["counts"] != expected:
        problems.append(f"counts are {sprout['counts']}, expected {expected}")
    if overview_sizes(source["width"], source["height"]) and sprout["overviews"] < 1:
        problems.append("COG has no overviews")
    return problems


def write_sprout(clip: Path, dest: Path, recipe: dict, work: Path | None = None) -> tuple[dict, dict]:
    work = work or dest.parent
    work.mkdir(parents=True, exist_ok=True)
    gdal.SetCacheMax(512 * 1024 * 1024)
    remapped = work / "remapped.tif"
    clip_facts = write_remapped(clip, remapped, recipe)
    overviews = write_max_overviews(remapped, work)
    write_cog(remapped, overviews, dest, recipe)
    sprout_facts = raster_facts(dest)
    problems = check_sprout(clip_facts, sprout_facts, recipe)
    if problems:
        raise ValueError("sprout failed the recipe check:\n" + "\n".join(problems))
    return clip_facts, sprout_facts


def pixel_offset(delta: float, pixel: float) -> int:
    value = delta / pixel
    nearest = round(value)
    if abs(value - nearest) > 1e-4:
        raise ValueError(f"offset is {value} pixels, not a whole pixel")
    return int(nearest)


def boxes_overlap(one: tuple[int, int, int, int], other: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = one
    bx, by, bw, bh = other
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def mosaic_canvas(frames: list[dict], pixel: float) -> dict:
    """Union of the tile windows. A gap between tiles is left uncovered."""
    minx = min(frame["transform"][0] for frame in frames)
    maxy = max(frame["transform"][3] for frame in frames)
    maxx = max(frame["transform"][0] + frame["transform"][1] * frame["width"] for frame in frames)
    miny = min(frame["transform"][3] + frame["transform"][5] * frame["height"] for frame in frames)
    width = pixel_offset(maxx - minx, pixel)
    height = pixel_offset(maxy - miny, pixel)
    placed = []
    for frame in frames:
        xoff = pixel_offset(frame["transform"][0] - minx, pixel)
        yoff = pixel_offset(maxy - frame["transform"][3], pixel)
        box = (xoff, yoff, frame["width"], frame["height"])
        if xoff < 0 or yoff < 0 or xoff + frame["width"] > width or yoff + frame["height"] > height:
            raise ValueError(f"{Path(frame['path']).name} falls outside the mosaic")
        for earlier in placed:
            if boxes_overlap(box, earlier[0]):
                raise ValueError(
                    f"{Path(frame['path']).name} overlaps {Path(earlier[1]['path']).name}"
                )
        placed.append((box, frame))
    return {
        "width": width,
        "height": height,
        "transform": (minx, pixel, 0.0, maxy, 0.0, -pixel),
        "placed": placed,
        "projection": frames[0]["projection"],
    }


def check_source_tile(facts: dict, recipe: dict, path: Path) -> None:
    if facts["transform"][2] != 0 or facts["transform"][4] != 0:
        raise ValueError(f"{path.name} is rotated or sheared")
    if facts["transform"][5] >= 0:
        raise ValueError(f"{path.name} is not north-up")
    if facts["crs"] != recipe["grid"]["crs"]:
        raise ValueError(f"{path.name} CRS is {facts['crs']}, recipe says {recipe['grid']['crs']}")
    if facts["dtype"] != recipe["grid"]["dtype"]:
        raise ValueError(f"{path.name} dtype is {facts['dtype']}, recipe says {recipe['grid']['dtype']}")
    pixel = float(recipe["grid"]["pixel_size_deg"])
    if abs(facts["pixel_size_deg"] - pixel) > 1e-12:
        raise ValueError(f"{path.name} pixel size is {facts['pixel_size_deg']}, recipe says {pixel}")
    counted = sum(facts["counts"].values())
    if counted != facts["width"] * facts["height"]:
        raise ValueError(f"{path.name} counted {counted} pixels of {facts['width'] * facts['height']}")
    require_mapped(facts["counts"], recipe)


def write_mosaic_vrt(canvas: dict, dest: Path, recipe: dict) -> None:
    pairs = ",".join(f"{src}:{dst}" for src, dst in sorted(mapping(recipe).items()))
    nodata = published_values(recipe)["nodata"]
    geo = ", ".join(f"{value:.12g}" for value in canvas["transform"])
    sources = []
    for (xoff, yoff, width, height), frame in canvas["placed"]:
        sources.append(
            f"""    <ComplexSource>
      <SourceFilename relativeToVRT="0">{xml_escape(str(frame["path"]))}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>
      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{width}" ySize="{height}"/>
      <LUT>{pairs}</LUT>
    </ComplexSource>"""
        )
    width = canvas["width"]
    height = canvas["height"]
    body = "\n".join(sources)
    dest.write_text(
        f"""<VRTDataset rasterXSize="{width}" rasterYSize="{height}">
  <SRS>{xml_escape(canvas["projection"])}</SRS>
  <GeoTransform>{geo}</GeoTransform>
  <VRTRasterBand dataType="Byte" band="1">
    <NoDataValue>{nodata}</NoDataValue>
{body}
  </VRTRasterBand>
</VRTDataset>
"""
    )


def write_mosaic_sprout(
    tile_paths: list[Path | str], dest: Path, recipe: dict, work: Path
) -> tuple[dict, dict]:
    """Mosaic checked tiles, remap with from_seed, and write one COG.

    Pixels in the union window that no tile covers stay nodata. An unmapped
    source value stops the run before the COG is written.
    """
    if not tile_paths:
        raise ValueError("the recipe names no tiles")
    work.mkdir(parents=True, exist_ok=True)
    gdal.SetCacheMax(512 * 1024 * 1024)
    # Handles for every tile. The block cache, not this pool, bounds decoded pixels.
    gdal.SetConfigOption("GDAL_MAX_DATASET_POOL_SIZE", str(dataset_pool_size(len(tile_paths))))
    frames = []
    counts: dict[int, int] = {}
    for index, tile_path in enumerate(tile_paths, start=1):
        path = Path(tile_path)
        print(f"read {index}/{len(tile_paths)} {tile_path}", flush=True)
        facts = raster_facts(path)
        check_source_tile(facts, recipe, path)
        facts["path"] = tile_path
        frames.append(facts)
        for value, count in facts["counts"].items():
            counts[value] = counts.get(value, 0) + count
    pixel = float(recipe["grid"]["pixel_size_deg"])
    canvas = mosaic_canvas(frames, pixel)
    vrt = work / "mosaic.vrt"
    write_mosaic_vrt(canvas, vrt, recipe)
    remapped = work / "remapped.tif"
    if remapped.exists():
        remapped.unlink()
    gdal.Translate(
        str(remapped),
        str(vrt),
        format="GTiff",
        noData=published_values(recipe)["nodata"],
        creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"],
        callback=_progress("mosaic"),
    )
    grid = raster_frame(remapped)
    if grid["width"] != canvas["width"] or grid["height"] != canvas["height"]:
        raise ValueError(
            f"mosaic is {grid['width']}×{grid['height']}, expected {canvas['width']}×{canvas['height']}"
        )
    overviews = write_max_overviews(remapped, work)
    write_cog(remapped, overviews, dest, recipe)
    sprout_facts = raster_facts(dest)
    source_facts = {
        "width": grid["width"],
        "height": grid["height"],
        "transform": grid["transform"],
        "counts": counts,
    }
    problems = check_sprout(source_facts, sprout_facts, recipe)
    if problems:
        raise ValueError("sprout failed the recipe check:\n" + "\n".join(problems))
    return source_facts, sprout_facts


def stac_names(recipe: dict) -> tuple[str, str]:
    stem = Path(recipe["sprout"]["path"]).name.removesuffix(".tif")
    return "collection.json", f"{stem}.json"


def stac_documents(recipe: dict, facts: dict) -> tuple[dict, dict]:
    minx, miny, maxx, maxy = bounds_of(facts["transform"], facts["width"], facts["height"])
    year = int(recipe["year"])
    start = f"{year}-01-01T00:00:00Z"
    end = f"{year + 1}-01-01T00:00:00Z"
    values = published_values(recipe)
    classes = []
    for name, value in values.items():
        entry = {"value": value, "name": name}
        if name == "nodata":
            entry["nodata"] = True
        classes.append(entry)
    collection_name, item_name = stac_names(recipe)
    bbox = [minx, miny, maxx, maxy]
    collection = {
        "stac_version": "1.0.0",
        "type": "Collection",
        "id": recipe["sprout"]["id"],
        "title": recipe["title"],
        "description": (
            f"Generated from cookbook/recipes/{recipe['id']}.yaml. "
            f"{recipe['source']['citation']}"
        ),
        "license": "other",
        "extent": {
            "spatial": {"bbox": [bbox]},
            "temporal": {"interval": [[start, end]]},
        },
        "links": [
            {"rel": "root", "href": f"./{collection_name}", "type": "application/json"},
            {"rel": "self", "href": f"./{collection_name}", "type": "application/json"},
            {"rel": "item", "href": f"./{item_name}", "type": "application/json"},
        ],
    }
    item = {
        "stac_version": "1.0.0",
        "type": "Feature",
        "stac_extensions": [
            "https://stac-extensions.github.io/raster/v1.1.0/schema.json",
            "https://stac-extensions.github.io/classification/v1.1.0/schema.json",
        ],
        "id": Path(item_name).stem,
        "collection": recipe["sprout"]["id"],
        "bbox": bbox,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        },
        "properties": {
            "datetime": None,
            "start_datetime": start,
            "end_datetime": end,
            "title": recipe["title"],
        },
        "assets": {
            "data": {
                "href": recipe["sprout"]["path"],
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
                "title": recipe["title"],
                "raster:bands": [
                    {
                        "data_type": recipe["grid"]["dtype"],
                        "nodata": values["nodata"],
                        "spatial_resolution": float(recipe["grid"]["pixel_size_deg"]),
                        "classification:classes": classes,
                    }
                ],
            }
        },
        "links": [
            {"rel": "root", "href": f"./{collection_name}", "type": "application/json"},
            {"rel": "collection", "href": f"./{collection_name}", "type": "application/json"},
            {"rel": "self", "href": f"./{item_name}", "type": "application/json"},
        ],
    }
    return collection, item


def write_stac(recipe: dict, facts: dict, directory: Path) -> list[Path]:
    collection, item = stac_documents(recipe, facts)
    collection_name, item_name = stac_names(recipe)
    paths = []
    for name, document in ((collection_name, collection), (item_name, item)):
        path = directory / name
        path.write_text(json.dumps(document, indent=2) + "\n")
        paths.append(path)
    return paths


def gs_join(prefix: str, name: str) -> str:
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix + name


def gs_to_vsi(gs_path: str) -> str:
    if not gs_path.startswith("gs://"):
        raise ValueError(f"not a gs path: {gs_path}")
    return "/vsigs/" + gs_path[len("gs://") :]


def vsi_copy(source: str, dest: str) -> None:
    incoming = gdal.VSIFOpenL(source, "rb")
    if incoming is None:
        raise OSError(f"cannot read {source}")
    parent = dest.rsplit("/", 1)[0]
    if parent.startswith("/vsi"):
        gdal.MkdirRecursive(parent, 0o755)
    outgoing = gdal.VSIFOpenL(dest, "wb")
    if outgoing is None:
        gdal.VSIFCloseL(incoming)
        raise OSError(f"cannot write {dest}")
    try:
        while True:
            chunk = gdal.VSIFReadL(1, 8 * 1024 * 1024, incoming)
            if not chunk:
                break
            written = gdal.VSIFWriteL(chunk, 1, len(chunk), outgoing)
            if written != len(chunk):
                raise OSError(f"short write to {dest}")
    finally:
        gdal.VSIFCloseL(incoming)
        gdal.VSIFCloseL(outgoing)


def upload_outputs(recipe: dict, cog: Path, stac_paths: list[Path]) -> None:
    destination = recipe["sprout"]["path"]
    vsi_copy(str(cog), gs_to_vsi(destination))
    folder = destination.rsplit("/", 1)[0]
    for path in stac_paths:
        vsi_copy(str(path), gs_to_vsi(gs_join(folder, path.name)))
    print(f"uploaded {destination}", flush=True)


def delete_scratch(prefix: str) -> None:
    folder = gs_to_vsi(prefix).rstrip("/")
    names = gdal.ReadDir(folder) or []
    for name in names:
        if gdal.Unlink(f"{folder}/{name}") != 0:
            print(f"scratch object left in place: {gs_join(prefix, name)}", flush=True)


def scratch_prefix(recipe: dict) -> str:
    return f"gs://nature-watch-bucket/scratch/sprout/{recipe['id']}/"


def dataset_pool_size(tile_count: int) -> int:
    """Open dataset handles. Must cover every source tile and the worker threads.

    Decoded pixels stay in the 512 MB block cache. A pool of 2 is below the
    thread count, so GDAL stops when the mosaic opens.
    """
    return max(16, tile_count + 16)


def job_shape(recipe: dict) -> dict:
    """Machine size for this recipe.

    The clip job is the version 1 shape. The global mosaic keeps the
    compressed working files, about 30 GB, on a 100 GB disk so a worse
    compression ratio still fits. One source tile is 1.6 GB uncompressed,
    so the machine has 16 GB of RAM.
    """
    pool = str(dataset_pool_size(len(recipe["seed"].get("tiles") or [])))
    if "clip" in recipe["seed"]:
        return {
            "machine": MACHINE,
            "cpu_milli": "2000",
            "memory_mib": "6144",
            "disk_gb": 30,
            "max_run_seconds": 10800,
            "dataset_pool_size": pool,
        }
    return {
        "machine": "e2-standard-4",
        "cpu_milli": "4000",
        "memory_mib": "12288",
        "disk_gb": 100,
        "max_run_seconds": 43200,
        "dataset_pool_size": pool,
    }


def require_destination(recipe: dict) -> None:
    path = recipe["sprout"]["path"]
    if "clip" not in recipe["seed"] and path.endswith("/cropland-2024-zaf.tif"):
        raise SystemExit(f"refusing to write the version 1 sprout: {path}")


def tile_url(recipe: dict, name: str) -> str:
    base = recipe["source"]["download"]
    if not base.endswith("/"):
        base += "/"
    return base + name


def transferred_object_name(url: str) -> str:
    """Object name Storage Transfer writes. The host plus the URL path."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.path:
        raise ValueError(f"not an http url: {url}")
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{host}/{parsed.path.lstrip('/')}"


def tile_object_name(tile: str, url: str, present: set[str]) -> str:
    """Object to mosaic. The host path wins. A flat name is only the 2024 move."""
    if "/" in tile or tile in ("", ".", ".."):
        raise ValueError(f"tile name is not a flat file name: {tile}")
    nested = transferred_object_name(url)
    if nested in present:
        return nested
    if tile in present:
        return tile
    raise SystemExit(f"missing {tile}")


def vsi_exists(path: str) -> bool:
    try:
        return gdal.VSIStatL(path) is not None
    except RuntimeError:
        return False


def resolve_tile(recipe: dict, name: str) -> str:
    prefix = recipe["seed"]["path"]
    url = tile_url(recipe, name)
    nested = transferred_object_name(url)
    present: set[str] = set()
    if vsi_exists(gs_to_vsi(gs_join(prefix, nested))):
        present.add(nested)
    if name != nested and vsi_exists(gs_to_vsi(gs_join(prefix, name))):
        present.add(name)
    return gs_join(prefix, tile_object_name(name, url, present))


def materialize_clip(recipe: dict, work: Path) -> Path:
    name = recipe["seed"]["clip"]["file"]
    dest = work / name
    if dest.exists() and dest.stat().st_size > 0:
        print(f"using {dest}", flush=True)
        return dest
    source = gs_join(recipe["seed"]["path"], name)
    print(f"copy {source}", flush=True)
    vsi_copy(gs_to_vsi(source), str(dest))
    return dest


def batch_job(script_prefix: str, shape: dict | None = None) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends.

    The script and recipe are copied to the scratch prefix before submit.
    The container downloads them. A Cloud Storage mount is not used: the
    Batch host's root is read-only, so creating a mount directory fails.
    """
    if shape is None:
        shape = job_shape({"id": "cropland-2024", "seed": {"clip": {}}, "sprout": {"path": ""}})
    prefix = script_prefix.removeprefix("gs://").rstrip("/")
    fetch = f"""python3 - << 'PY'
import json
import urllib.request
from pathlib import Path
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={{"Metadata-Flavor": "Google"}},
)
token = json.load(urllib.request.urlopen(request))["access_token"]

def download(name, dest):
    url = "https://storage.googleapis.com/{prefix}/" + name
    body = urllib.request.Request(url, headers={{"Authorization": "Bearer " + token}})
    dest.write_bytes(urllib.request.urlopen(body).read())

folder = Path("/tmp/in")
folder.mkdir(exist_ok=True)
download("cropland.py", folder / "cropland.py")
download("recipe.json", folder / "recipe.json")
PY
python3 /tmp/in/cropland.py run --recipe /tmp/in/recipe.json --work /tmp/work --upload --scratch {script_prefix}
"""
    return {
        "taskGroups": [
            {
                "taskCount": "1",
                "taskSpec": {
                    "maxRunDuration": f"{shape['max_run_seconds']}s",
                    "maxRetryCount": 1,
                    "computeResource": {
                        "cpuMilli": shape["cpu_milli"],
                        "memoryMib": shape["memory_mib"],
                    },
                    "environment": {
                        "variables": {
                            "CPL_MACHINE_IS_GCE": "YES",
                            BATCH_ENV: "1",
                            "GDAL_CACHEMAX": "512",
                            "GDAL_MAX_DATASET_POOL_SIZE": shape["dataset_pool_size"],
                        }
                    },
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "sh",
                                "commands": ["-c", fetch],
                            }
                        }
                    ],
                },
            }
        ],
        "allocationPolicy": {
            "instances": [
                {
                    "policy": {
                        "machineType": shape["machine"],
                        "provisioningModel": "SPOT",
                        "bootDisk": {"sizeGb": shape["disk_gb"], "type": "pd-balanced"},
                    }
                }
            ],
            "location": {"allowedLocations": [f"regions/{REGION}"]},
            "serviceAccount": {"email": SERVICE_ACCOUNT},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def gcloud(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gcloud", *args, f"--project={PROJECT}"], check=True, text=True)


def upload_with_gcloud(local: Path, destination: str) -> None:
    gcloud(["storage", "cp", str(local), destination])


def print_job_followup(job_id: str) -> None:
    print(
        f"status: gcloud batch jobs describe {job_id} --location={REGION} --project={PROJECT}",
        flush=True,
    )
    print(
        f"cancel: gcloud batch jobs delete {job_id} --location={REGION} --project={PROJECT}",
        flush=True,
    )
    print(f"output: see sprout.path in the recipe", flush=True)


def command_run(recipe: dict, work: Path, upload: bool, scratch: str | None) -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit(
            "The sprout run is the Batch job in europe-west1. "
            "From this computer, use: python sprout/cropland.py submit"
        )
    require_destination(recipe)
    work.mkdir(parents=True, exist_ok=True)
    cog = work / Path(recipe["sprout"]["path"]).name
    if "clip" in recipe["seed"]:
        clip = materialize_clip(recipe, work)
        _source_facts, sprout_facts = write_sprout(clip, cog, recipe, work)
    else:
        paths = [gs_to_vsi(resolve_tile(recipe, name)) for name in recipe["seed"]["tiles"]]
        _source_facts, sprout_facts = write_mosaic_sprout(paths, cog, recipe, work)
    stac_paths = write_stac(recipe, sprout_facts, work)
    print(f"cog_bytes {cog.stat().st_size}", flush=True)
    print(f"overviews {sprout_facts['overviews']}", flush=True)
    print(f"counts {sprout_facts['counts']}", flush=True)
    if upload:
        upload_outputs(recipe, cog, stac_paths)
    if scratch:
        delete_scratch(scratch)


def command_submit(recipe_path: Path) -> None:
    recipe = load_recipe(recipe_path)
    require_destination(recipe)
    job_id = recipe["sprout"]["id"]
    shape = job_shape(recipe)
    prefix = scratch_prefix(recipe)
    gcloud(["services", "enable", "batch.googleapis.com"])
    existing = subprocess.run(
        ["gcloud", "storage", "ls", recipe["sprout"]["path"], f"--project={PROJECT}"],
        text=True,
        capture_output=True,
    )
    if existing.returncode == 0 and recipe["sprout"]["path"] in existing.stdout:
        raise SystemExit(f"{recipe['sprout']['path']} already exists")
    work = ROOT / "data" / "sprout" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    upload_with_gcloud(Path(__file__).resolve(), gs_join(prefix, "cropland.py"))
    upload_with_gcloud(recipe_json, gs_join(prefix, "recipe.json"))
    config = batch_job(prefix, shape)
    config_path = work / "batch_job.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(config_path.read_text(), flush=True)
    print(
        f"job {job_id}: spot {shape['machine']} disk {shape['disk_gb']} GB "
        f"memory {shape['memory_mib']} MiB limit {shape['max_run_seconds']}s",
        flush=True,
    )
    gcloud(["batch", "jobs", "submit", job_id, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {job_id} in {REGION}", flush=True)
    print(f"output {recipe['sprout']['path']}", flush=True)
    print_job_followup(job_id)


def peak_memory_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "submit"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--work", type=Path, default=ROOT / "data" / "sprout" / "cropland-2024")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--scratch", default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    if args.command == "submit":
        command_submit(args.recipe)
    else:
        command_run(load_recipe(args.recipe), args.work, args.upload, args.scratch)
    elapsed = time.perf_counter() - started
    print(f"wall_seconds {elapsed:.1f}")
    print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()

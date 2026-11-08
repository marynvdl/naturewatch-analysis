"""Partitioned raster PMTiles: plan shards, build one, merge them.

Nothing here knows about cropland. A layer is three functions:

  render(z, x, y)        the top-zoom tile as an array, or None when it is empty
  reduce(children)       a parent array from {(dx, dy): array}, or None
  encode(array) -> bytes the tile as stored in the archive
  decode(bytes) -> array the inverse, used by the merge to build the low zooms

A shard is one tile at `shard_zoom` and everything under it down to `top_zoom`.
It is built depth first, so memory holds one root-to-leaf path of arrays and the
encoded tiles, never a whole zoom level. The merge writes zooms below
`shard_zoom` from the shard root tiles and streams the rest in tile id order.

Tile ids order tiles by zoom first, then along a Hilbert curve. All tiles under
one shard tile at a given zoom have consecutive ids, and shards at the shard
zoom sort by id, so concatenating shards zoom by zoom is already in id order.
`merge_archive` checks that rather than assuming it.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from pmtiles.reader import all_tiles
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer

Bounds = tuple[float, float, float, float]  # west, south, east, north, degrees
Entry = tuple[int, bytes]


class MergeError(RuntimeError):
    pass


# ---- planning ---------------------------------------------------------------


def plan_shards(
    cells: dict[str, Bounds], shard_zoom: int, covering: Callable[[Bounds, int], list[tuple[int, int]]]
) -> list[dict]:
    """One shard for every shard-zoom tile that touches a source cell.

    cells maps a source name to its bounds. The result is in tile id order and
    each shard lists the source names it reads, sorted.
    """
    found: dict[tuple[int, int], set[str]] = {}
    for name, bounds in cells.items():
        for x, y in covering(bounds, shard_zoom):
            found.setdefault((x, y), set()).add(name)
    shards = [
        {"id": f"{shard_zoom}-{x}-{y}", "z": shard_zoom, "x": x, "y": y, "sources": sorted(names)}
        for (x, y), names in found.items()
    ]
    shards.sort(key=lambda shard: zxy_to_tileid(shard["z"], shard["x"], shard["y"]))
    return shards


def ancestor(z: int, x: int, y: int, zoom: int) -> tuple[int, int]:
    shift = z - zoom
    return x >> shift, y >> shift


# ---- one shard --------------------------------------------------------------


def build_subtree(
    root: tuple[int, int, int],
    top_zoom: int,
    render: Callable[[int, int, int], object],
    reduce: Callable[[dict], object],
    encode: Callable[[object], bytes],
    entries: list[Entry],
) -> object:
    """Tiles from `root` down to `top_zoom`, appended to entries. Returns the root array or None.

    The top zoom is rendered. Every other zoom is reduced from its four children.
    A tile that is None is left out and counts as nodata for its parent.
    """
    z, x, y = root
    if z == top_zoom:
        values = render(z, x, y)
    else:
        children = {}
        for dy in (0, 1):
            for dx in (0, 1):
                child = build_subtree((z + 1, x * 2 + dx, y * 2 + dy), top_zoom, render, reduce, encode, entries)
                if child is not None:
                    children[(dx, dy)] = child
        values = reduce(children) if children else None
    if values is not None:
        entries.append((zxy_to_tileid(z, x, y), encode(values)))
    return values


def archive_header(bounds: Bounds, min_zoom: int, max_zoom: int, center_zoom: int | None = None) -> dict:
    west, south, east, north = bounds
    return {
        "tile_type": TileType.PNG,
        "tile_compression": Compression.NONE,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "min_lon_e7": int(west * 1e7),
        "min_lat_e7": int(south * 1e7),
        "max_lon_e7": int(east * 1e7),
        "max_lat_e7": int(north * 1e7),
        "center_zoom": min(max_zoom, 10) if center_zoom is None else center_zoom,
        "center_lon_e7": int((west + east) / 2 * 1e7),
        "center_lat_e7": int((south + north) / 2 * 1e7),
    }


def write_archive(entries: Iterable[Entry], dest: Path, header: dict, metadata: dict) -> dict:
    """Write entries to a PMTiles file. Entries must arrive in strictly increasing tile id.

    The pmtiles Writer keeps one index entry per run in memory and one hash per
    distinct tile body. It spools tile bytes to a temporary file, so peak disk
    is the archive twice.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    previous = -1
    count = 0
    with open(dest, "wb") as handle:
        writer = Writer(handle)
        for tile_id, data in entries:
            if tile_id <= previous:
                raise MergeError(f"tile id {tile_id} is not after {previous}")
            previous = tile_id
            writer.write_tile(tile_id, data)
            count += 1
        if count == 0:
            raise MergeError("no tiles to write")
        writer.finalize(header, metadata)
    return {"tiles": count, "bytes": dest.stat().st_size, "distinct_tiles": len(writer.hash_to_offset)}


def read_archive(path: Path) -> Iterator[tuple[tuple[int, int, int], bytes]]:
    """Tiles of a PMTiles file in the order stored, which is tile id order."""
    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:

            def get_bytes(offset: int, length: int) -> bytes:
                return bytes(mapped[offset : offset + length])

            yield from all_tiles(get_bytes)


def config_digest(config: dict) -> str:
    """Short hash of everything that changes a shard's bytes."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def build_shard(
    shard: dict,
    top_zoom: int,
    render: Callable[[int, int, int], object],
    reduce: Callable[[dict], object],
    encode: Callable[[object], bytes],
    dest: Path,
    bounds: Bounds,
    metadata: dict,
    root_sink: list | None = None,
) -> dict:
    """Build one shard to `dest` and return its record. An empty shard writes no file.

    root_sink: when given, the shard root's array (None if empty) is appended to it, for a merge that needs it unquantised.
    """
    started = time.perf_counter()
    entries: list[Entry] = []
    root = (shard["z"], shard["x"], shard["y"])
    root_values = build_subtree(root, top_zoom, render, reduce, encode, entries)
    if root_sink is not None:
        root_sink.append(root_values)
    entries.sort(key=lambda entry: entry[0])
    by_zoom: dict[int, int] = {}
    for tile_id, _data in entries:
        zoom = tileid_to_zxy(tile_id)[0]
        by_zoom[zoom] = by_zoom.get(zoom, 0) + 1
    record = {
        "shard": shard["id"],
        "tiles": len(entries),
        "tiles_by_zoom": {str(zoom): count for zoom, count in sorted(by_zoom.items())},
        "png_bytes": sum(len(data) for _id, data in entries),
        "file": dest.name if entries else None,
        "bytes": 0,
    }
    if entries:
        header = archive_header(bounds, shard["z"], top_zoom)
        record["bytes"] = write_archive(entries, dest, header, metadata)["bytes"]
    record["wall_seconds"] = round(time.perf_counter() - started, 2)
    return record


# ---- the merge --------------------------------------------------------------


class _Peek:
    """An iterator with one tile of look-ahead."""

    def __init__(self, iterator: Iterator):
        self.iterator = iterator
        self.head = next(iterator, None)

    def pop(self):
        item = self.head
        self.head = next(self.iterator, None)
        return item


def upper_levels(
    roots: dict[tuple[int, int], object],
    shard_zoom: int,
    reduce: Callable[[dict], object],
    encode: Callable[[object], bytes],
) -> list[Entry]:
    """Tiles at zooms 0 to shard_zoom - 1 from the shard root arrays, sorted by tile id."""
    entries: list[Entry] = []
    level = roots
    for zoom in range(shard_zoom - 1, -1, -1):
        parents: dict[tuple[int, int], dict] = {}
        for (x, y), values in level.items():
            parents.setdefault((x // 2, y // 2), {})[(x % 2, y % 2)] = values
        level = {}
        for (x, y), children in parents.items():
            values = reduce(children)
            if values is not None:
                level[(x, y)] = values
                entries.append((zxy_to_tileid(zoom, x, y), encode(values)))
    entries.sort(key=lambda entry: entry[0])
    return entries


def merge_entries(
    shards: list[dict],
    shard_files: dict[str, Path | None],
    records: dict[str, dict],
    shard_zoom: int,
    top_zoom: int,
    upper: list[Entry],
) -> Iterator[Entry]:
    """Every tile of the archive in tile id order, checked as it goes.

    Raises MergeError when an id repeats or goes backwards, when a tile at
    shard_zoom or above comes from a shard that is not its ancestor, or when a
    shard gave a different count of tiles at some zoom than its record says.
    """
    previous = -1
    for tile_id, data in upper:
        if tile_id <= previous:
            raise MergeError(f"upper tile id {tile_id} is not after {previous}")
        previous = tile_id
        yield tile_id, data

    streams = []
    for shard in shards:
        path = shard_files[shard["id"]]
        streams.append(_Peek(read_archive(path)) if path is not None else _Peek(iter(())))
    seen = [dict() for _ in shards]
    for zoom in range(shard_zoom, top_zoom + 1):
        for shard, stream, counts in zip(shards, streams, seen):
            while stream.head is not None and stream.head[0][0] == zoom:
                (z, x, y), data = stream.pop()
                if ancestor(z, x, y, shard_zoom) != (shard["x"], shard["y"]):
                    raise MergeError(f"tile {z}/{x}/{y} is in shard {shard['id']}, which is not its ancestor")
                tile_id = zxy_to_tileid(z, x, y)
                if tile_id <= previous:
                    raise MergeError(f"tile {z}/{x}/{y} id {tile_id} is not after {previous}")
                previous = tile_id
                counts[zoom] = counts.get(zoom, 0) + 1
                yield tile_id, data
    for shard, stream, counts in zip(shards, streams, seen):
        if stream.head is not None:
            raise MergeError(f"shard {shard['id']} has tiles below zoom {shard_zoom} or above {top_zoom}")
        expected = {int(zoom): count for zoom, count in records[shard["id"]]["tiles_by_zoom"].items()}
        if counts != expected:
            raise MergeError(f"shard {shard['id']} gave {counts}, its record says {expected}")

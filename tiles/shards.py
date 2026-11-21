"""The global raster archive in shards, for any share layer: build a shard, merge the shards, check against the sprout.

A layer hands over what differs between roads and buildings (`ShardLayer`). Everything else is here:

  run_shard     copy the shard's source COGs, build a VRT over them, render the top zoom and reduce to the
                shard zoom (tiles/pyramid.py), then write the shard archive, its root tile unquantised and
                a record, last, under the scratch prefix. A current record means the shard is done.
  run_merge     check every record, build the zooms below the shard zoom from the roots, stream the shards
                in tile id order into one archive, rebuild sample subtrees from the sprout, publish.
  command_local the same on a folder of COGs, nothing published.

Also the Batch job pieces shared by the layers' submit scripts: the bootstrap that fetches the code and the
pmtiles wheel inside the GDAL container, and the job spec.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from osgeo import gdal

import global_cropland as store  # noqa: E402  storage, upload and Range check helpers
import pyramid  # noqa: E402

gdal.UseExceptions()

CACHE_CONTROL = "public, max-age=31536000, immutable"
PMTILES_VERSION = "3.8.1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
REGION = "europe-west1"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"


@dataclass
class ShardLayer:
    """What a layer brings to the sharded build."""

    name: str  # the archive is <name>.pmtiles
    config: dict  # everything that changes a shard's bytes; its digest marks a record current
    open_render: Callable[[str], Callable]  # a source path (VRT or COG) -> render(z, x, y)
    reduce: Callable[[dict], object]
    encode: Callable[[object], bytes]
    save_root: Callable[[object], dict]  # root tile -> arrays for np.savez
    load_root: Callable[[dict], object]
    metadata: dict
    source_bounds: Callable[[str], pyramid.Bounds]  # a source name -> its bounds, for the archive header
    shard_zoom: int = 5
    top_zoom: int = 11
    extra: dict = field(default_factory=dict)


def shard_files(scratch: str, shard: dict) -> tuple[str, str, str]:
    base = scratch.rstrip("/") + "/shards/" + shard["id"]
    return base + ".pmtiles", base + ".json", base + ".root.npz"


def _save_npz(arrays: dict, local: Path) -> None:
    with open(local, "wb") as handle:
        np.savez(handle, **arrays)


def run_shard(layer: ShardLayer, shard: dict, work: Path, scratch: str, sources: str) -> dict:
    """Build one shard. Skips it when its record is current and its files are whole."""
    digest = pyramid.config_digest(layer.config)
    file_path, record_path, root_path = shard_files(scratch, shard)
    record = store.read_json(record_path)
    if record and record.get("config") == digest and (
        (record["tiles"] == 0 or store.size_of(file_path) == record["bytes"])
        and (not record.get("root") or store.exists(root_path))
    ):
        print(f"shard {shard['id']} is done, skipping", flush=True)
        return record
    started = time.perf_counter()
    folder = work / shard["id"]
    for name in shard["sources"]:
        store.copy(sources.rstrip("/") + f"/{name}.tif", folder / "src" / f"{name}.tif")
    fetched = time.perf_counter() - started
    vrt = folder / "shard.vrt"
    gdal.BuildVRT(str(vrt), [str(folder / "src" / f"{name}.tif") for name in shard["sources"]]).FlushCache()
    render = layer.open_render(str(vrt))
    out = folder / f"{shard['id']}.pmtiles"
    bounds = store.union([layer.source_bounds(name) for name in shard["sources"]])
    roots: list = []
    result = pyramid.build_shard(
        shard, layer.top_zoom, render, layer.reduce, layer.encode, out, bounds, layer.metadata, roots
    )
    render = None
    result.update(
        config=digest, sources=shard["sources"], fetch_seconds=round(fetched, 1),
        root=roots[0] is not None, peak_memory_bytes=peak_memory_bytes(),
    )
    if result["tiles"]:
        store.copy(out, file_path)
        if store.size_of(file_path) != result["bytes"]:
            raise OSError(f"{file_path} is {store.size_of(file_path)} bytes, expected {result['bytes']}")
    if result["root"]:
        local_root = folder / "root.npz"
        _save_npz(layer.save_root(roots[0]), local_root)
        store.copy(local_root, root_path)
    store.write_json(record_path, result)  # last, so a record means a finished shard
    shutil.rmtree(folder, ignore_errors=True)
    print(json.dumps({k: v for k, v in result.items() if k != "tiles_by_zoom"}), flush=True)
    return result


def run_merge(
    layer: ShardLayer, plan: list[dict], work: Path, scratch: str, dest: str, sprout: str | None = None,
    verify_samples: int = 0, edge_samples: int = 0, keep_scratch: bool = False, publish: bool = True,
) -> dict:
    """Check the shards, write the archive, rebuild samples from the sprout, then publish and delete the shards."""
    digest = pyramid.config_digest(layer.config)
    started = time.perf_counter()
    if publish and store.exists(dest):
        raise SystemExit(f"{dest} already exists. Remove it on purpose before merging again.")
    records, missing = {}, []
    for shard in plan:
        file_path, record_path, root_path = shard_files(scratch, shard)
        record = store.read_json(record_path)
        if not record or record.get("config") != digest or (record["tiles"] and store.size_of(file_path) != record["bytes"]):
            missing.append(shard["id"])
        else:
            records[shard["id"]] = record
    if missing:
        raise SystemExit(f"{len(missing)} of {len(plan)} shards are missing or not current: {', '.join(missing[:12])}")

    local_files: dict[str, Path | None] = {}
    roots: dict[tuple[int, int], object] = {}
    for shard in plan:
        file_path, _record, root_path = shard_files(scratch, shard)
        record = records[shard["id"]]
        local_files[shard["id"]] = None
        if record["tiles"]:
            target = work / "shards" / f"{shard['id']}.pmtiles"
            store.copy(file_path, target)
            local_files[shard["id"]] = target
        if record.get("root"):
            local_root = work / "shards" / f"{shard['id']}.root.npz"
            store.copy(root_path, local_root)
            with np.load(local_root) as arrays:
                roots[(shard["x"], shard["y"])] = layer.load_root(dict(arrays))
    fetched = time.perf_counter()

    upper = pyramid.upper_levels(roots, layer.shard_zoom, layer.reduce, layer.encode)
    bounds = store.union([layer.source_bounds(name) for shard in plan for name in shard["sources"]])
    archive = work / f"{layer.name}.pmtiles"
    stream = pyramid.merge_entries(plan, local_files, records, layer.shard_zoom, layer.top_zoom, upper)
    stats = pyramid.write_archive(
        stream, archive, pyramid.archive_header(bounds, 0, layer.top_zoom, center_zoom=2), layer.metadata
    )
    expected = len(upper) + sum(record["tiles"] for record in records.values())
    if stats["tiles"] != expected:
        raise pyramid.MergeError(f"wrote {stats['tiles']} tiles, shards and upper zooms give {expected}")
    built = time.perf_counter()
    by_zoom: dict[str, int] = {}
    for record in records.values():
        for zoom, count in record.get("tiles_by_zoom", {}).items():
            by_zoom[zoom] = by_zoom.get(zoom, 0) + count
    stats.update(
        shards=len(plan),
        shards_with_tiles=sum(1 for record in records.values() if record["tiles"]),
        upper_tiles=len(upper),
        tiles_by_zoom=dict(sorted(by_zoom.items(), key=lambda item: int(item[0]))),
        shard_wall_seconds_total=round(sum(r["wall_seconds"] for r in records.values()), 1),
        shard_peak_memory_bytes_max=max(r["peak_memory_bytes"] for r in records.values()),
    )
    if (verify_samples or edge_samples) and sprout:
        weights = {shard_id: record["tiles"] for shard_id, record in records.items()}
        stats["verify"] = verify_against_sprout(layer, archive, plan, weights, sprout, verify_samples, edge_samples)
    if publish:
        store.upload_archive(archive, dest)
        store.set_cache_control(dest, CACHE_CONTROL)
        if dest.startswith("gs://nature-watch-tiles/"):
            stats["range_check"] = store.check_range(dest)
        stats["cache_control"] = CACHE_CONTROL
    if not keep_scratch:
        store.delete_prefix(scratch.rstrip("/") + "/shards")
    stats.update(
        merge_wall_seconds=round(time.perf_counter() - started, 1),
        fetch_seconds=round(fetched - started, 1),
        build_seconds=round(built - fetched, 1),
        peak_memory_bytes=peak_memory_bytes(),
    )
    print("merge " + json.dumps(stats), flush=True)
    return stats


def verify_against_sprout(
    layer: ShardLayer, archive: Path, plan: list[dict], weights: dict[str, int], sprout: str, samples: int,
    edge_samples: int = 0, root_zoom: int = 8,
) -> dict:
    """Rebuild sample subtrees straight from the sprout and compare every tile, present or absent, byte for byte.

    Samples are drawn from shards with tiles, weighted by their tile count, so most land where the layer has data.
    An edge sample is a subtree on its shard's west or north edge, where a render reads past the shard.
    """
    render = layer.open_render(sprout)
    rng = random.Random(20261118)
    span = 2 ** (root_zoom - layer.shard_zoom)
    checked, roots = 0, []
    shard_weights = [weights.get(shard["id"], 0) for shard in plan]
    picks = []
    for shard in rng.choices(plan, weights=shard_weights, k=samples) if samples else []:
        picks.append((root_zoom, shard["x"] * span + rng.randrange(span), shard["y"] * span + rng.randrange(span)))
    for shard in rng.choices(plan, weights=shard_weights, k=edge_samples) if edge_samples else []:
        if rng.random() < 0.5:
            picks.append((root_zoom, shard["x"] * span, shard["y"] * span + rng.randrange(span)))
        else:
            picks.append((root_zoom, shard["x"] * span + rng.randrange(span), shard["y"] * span))
    for root in picks:
        entries: list[pyramid.Entry] = []
        pyramid.build_subtree(root, layer.top_zoom, render, layer.reduce, layer.encode, entries)
        expected = dict(entries)
        found = store.archive_lookup(archive, root, layer.top_zoom)
        if expected != found:
            differing = sum(1 for key in set(expected) & set(found) if expected[key] != found[key])
            raise pyramid.MergeError(
                f"sample under {root} disagrees with the sprout: {len(set(expected) - set(found))} missing, "
                f"{len(set(found) - set(expected))} extra, {differing} different"
            )
        checked += len(expected)
        roots.append("/".join(map(str, root)))
    return {"samples": samples, "edge_samples": edge_samples, "root_zoom": root_zoom, "tiles_compared": checked, "roots": roots}


def command_local(layer: ShardLayer, plan: list[dict], sources: Path, work: Path) -> dict:
    """The whole sharded flow on a local folder of COGs named as the plan's sources. Nothing is published."""
    scratch = str(work / "scratch")
    for shard in plan:
        run_shard(layer, shard, work / "tmp", scratch, str(sources))
    stats = run_merge(layer, plan, work / "merge", scratch, str(work / f"{layer.name}.pmtiles"), keep_scratch=True, publish=False)
    store.copy(work / "merge" / f"{layer.name}.pmtiles", work / f"{layer.name}.pmtiles")
    return stats


def peak_memory_bytes() -> int:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


# ---- Batch --------------------------------------------------------------------------


def bootstrap(prefix: str, wheel: str, code_files, plan_name: str = "plan.json") -> str:
    """Shell lines that fetch the code, recipe, plan and wheel from <prefix>code/ and make them importable."""
    bucket_path = prefix.removeprefix("gs://").rstrip("/")
    names = [*code_files, "recipe.json", plan_name, wheel]
    downloads = "\n".join(f'download("{n}", folder / "{n}")' for n in names)
    return f"""python3 - << 'PY'
import json
import urllib.request
from pathlib import Path
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={{"Metadata-Flavor": "Google"}},
)
token = json.load(urllib.request.urlopen(request))["access_token"]

def download(name, dest):
    url = "https://storage.googleapis.com/{bucket_path}/code/" + name
    body = urllib.request.Request(url, headers={{"Authorization": "Bearer " + token}})
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(urllib.request.urlopen(body).read())

folder = Path("/tmp/in")
{downloads}
PY
python3 -c "import zipfile; zipfile.ZipFile('/tmp/in/{wheel}').extractall('/tmp/in/lib')"
python3 -c "import numpy" 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-numpy)
export PYTHONPATH=/tmp/in/lib
"""


def job_spec(shape: dict, script: str, tasks: int, parallelism: int, batch_env: str) -> dict:
    """A Batch job of `tasks` spot tasks in europe-west1 running `script` in the GDAL container."""
    spec = {
        "maxRunDuration": f"{shape['max_run_seconds']}s",
        "maxRetryCount": shape["max_retries"],
        "lifecyclePolicies": [{"action": "RETRY_TASK", "actionCondition": {"exitCodes": [50001]}}],
        "computeResource": {"cpuMilli": shape["cpu_milli"], "memoryMib": shape["memory_mib"]},
        "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", batch_env: "1", "GDAL_CACHEMAX": "256"}},
        "runnables": [{"container": {"imageUri": IMAGE, "entrypoint": "sh", "commands": ["-c", script]}}],
    }
    return {
        "taskGroups": [{"taskCount": str(tasks), "parallelism": str(min(tasks, parallelism)), "taskSpec": spec}],
        "allocationPolicy": {
            "instances": [
                {
                    "policy": {
                        "machineType": shape["machine"],
                        "provisioningModel": "SPOT",
                        "bootDisk": {"sizeGb": shape["disk_gb"], "type": shape.get("disk_type", "pd-balanced")},
                    }
                }
            ],
            "location": {"allowedLocations": [f"regions/{REGION}"]},
            "serviceAccount": {"email": SERVICE_ACCOUNT},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def pmtiles_wheel(work: Path) -> Path:
    found = list(work.glob(f"pmtiles-{PMTILES_VERSION}-*.whl"))
    if not found:
        subprocess.run(
            [sys.executable, "-m", "pip", "download", f"pmtiles=={PMTILES_VERSION}", "--no-deps", "-d", str(work)], check=True
        )
        found = list(work.glob(f"pmtiles-{PMTILES_VERSION}-*.whl"))
    return found[0]

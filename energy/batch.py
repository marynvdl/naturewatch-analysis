#!/usr/bin/env python3
"""The global infrastructure seed and the power-lines and power-sites sprouts as Batch jobs in europe-west1.

The 648 blocks of 10° are split into at most 20 runs of similar weight, one Batch task each, as roads.
Each block writes its output, then a JSON record, so a rerun skips finished blocks. One sprout task
writes both sprouts, so each seed block is read once.

  python energy/batch.py submit seed --pilot 40N_000E             print the pilot job, submit nothing
  python energy/batch.py submit seed --pilot 40N_000E --approved  submit it
  python energy/batch.py submit seed --approved                   every block
  python energy/batch.py submit sprout --approved                 every block, after every seed block has a record
  python energy/batch.py collect                                  both VRTs and STAC files, after every sprout block
  python energy/batch.py records seed|sprout [--pilot]            totals from the records written so far

A seed pilot writes to the seed path, as the full run would. A sprout pilot writes to the scratch
prefix, because its neighbours' seed blocks may not exist yet. The helpers are roads/batch.py's.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "roads"))
sys.path.append(str(HERE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rb = _load("roads_batch", ROOT / "roads" / "batch.py")  # records, uploads, VRT, STAC, wheel, job spec
gcs, blocks = rb.gcs, rb.blocks

import rules  # noqa: E402

eseed = _load("energy_seed", HERE / "seed.py")
esprout = _load("energy_sprout", HERE / "sprout.py")

TASKS = rb.TASKS
LAYERS = {"power-lines": rules.LINES, "power-sites": rules.SITES}
BANDS = {"power-lines": ("voltage",), "power-sites": ("source",)}
DATA = ROOT / "data" / "energy"
CODE_FILES = (
    "energy/batch.py", "energy/rules.py", "energy/seed.py", "energy/sprout.py",
    "roads/batch.py", "roads/blocks.py", "roads/seed.py", "roads/sprout.py", "sprout/cropland.py",
)
RECIPE_COPIES = [f"cookbook/recipes/{rules.LINES['id']}.json", f"cookbook/recipes/{rules.SITES['id']}.json"]
SHAPES = {
    "seed": {**rb.SHAPES["seed"]},
    # Two 40,000 × 40,000 byte bands are burned one after the other: 1.6 GB each, plus DuckDB.
    "sprout": {**rb.SHAPES["sprout"]},
}


def scratch() -> str:
    return f"gs://nature-watch-bucket/scratch/energy/{rules.LINES['seed']['id']}/"


def seed_object(block: str, suffix: str) -> str:
    return rb.seed_object(rules.LINES, block, suffix)


def sprout_object(layer: str, block: str, suffix: str, pilot: bool = False) -> str:
    if pilot:
        return gcs.gs_join(scratch(), f"pilot/{layer}/{block}.{suffix}")
    return rb.sprout_object(LAYERS[layer], block, suffix)


def record_object(block: str, pilot: bool) -> str:
    """One record per block for both sprouts, written after both COGs, beside the power-lines blocks."""
    return sprout_object("power-lines", block, "json", pilot)


# ---- seed task -----------------------------------------------------------------------


def run_seed(plan: dict, work: Path) -> None:
    rb.require_batch()
    work.mkdir(parents=True, exist_ok=True)
    con = rb.seedmod.connect(work, SHAPES["seed"]["duckdb_memory"])
    con.execute(f"SET s3_region='{rules.LINES['source']['s3_region']}'")
    source = f"read_parquet('{rules.LINES['source']['path']}')"
    for block in rb.task_blocks(plan):
        data, record_path = seed_object(block, "parquet"), seed_object(block, "json")
        if rb.is_complete(rb.read_record(record_path), data):
            print(f"skip {block}: already written", flush=True)
            continue
        local = work / f"{block}.parquet"
        result = eseed.write_block(con, source, blocks.bounds(block), local)
        if result["rows"]:
            rb.upload(local, data)
            local.unlink()
        record = {
            "block": block, "release": rules.LINES["source"]["release"], **result,
            "peak_memory_bytes": rb.seedmod.peak_memory_bytes(),
            "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        rb.write_record(record_path, record)
        print(json.dumps(record), flush=True)


# ---- sprout task ---------------------------------------------------------------------


def sprout_complete(record: dict | None, block: str, pilot: bool) -> bool:
    if record is None:
        return False
    return all(rb.is_complete(record["layers"][layer], sprout_object(layer, block, "tif", pilot)) for layer in LAYERS)


def run_sprout(plan: dict, work: Path, pilot: bool) -> None:
    rb.require_batch()
    cache = work / "seed"
    cache.mkdir(parents=True, exist_ok=True)
    con = rb.seedmod.connect(work, SHAPES["sprout"]["duckdb_memory"])
    gcs.gdal.SetCacheMax(256 * 1024 * 1024)
    order = rb.task_blocks(plan)
    for position, block in enumerate(order):
        record_path = record_object(block, pilot)
        if sprout_complete(rb.read_record(record_path), block, pilot):
            print(f"skip {block}: already written", flush=True)
            continue
        started = time.perf_counter()
        found, missing = rb.neighbour_seeds(rules.LINES, block, pilot)
        found = [(name, r) for name, r in found if r.get("power_classes")]  # only blocks with power rows are fetched
        keep = {name for name, _ in found}
        if position + 1 < len(order):
            keep |= {order[position + 1], *blocks.neighbours(order[position + 1])}
        for path in cache.glob("*.parquet"):
            if path.stem not in keep:
                path.unlink()
        paths = [str(rb.fetch_seed(rules.LINES, name, r, cache)) for name, r in found]
        fetched = time.perf_counter() - started
        bounds = blocks.bounds(block)
        layers = {layer: {"features": 0, "marked_cells": 0, "by_value": {}, "bytes": 0} for layer in LAYERS}
        if paths:
            layers = esprout.burn_block(con, paths, bounds, work, lambda layer, local: rb.upload(local, sprout_object(layer, block, "tif", pilot)))
        pixel = rules.LINES["grid"]["pixel_size_deg"]
        west, north, cols, rows = esprout.roads_sprout.snapped(bounds, pixel, tuple(rules.LINES["grid"]["origin"]))
        record = {
            "block": block, "layers": layers, "width": cols, "height": rows, "transform": [west, pixel, 0, north, 0, -pixel],
            "seed_blocks": [name for name, _ in found], "seed_missing": missing,
            "fetch_seconds": round(fetched, 1), "seconds": round(time.perf_counter() - started, 1),
            "peak_memory_bytes": rb.seedmod.peak_memory_bytes(),
            "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        rb.write_record(record_path, record)
        print(json.dumps(record), flush=True)


# ---- collect -------------------------------------------------------------------------


def layer_records(records: list[dict], layer: str) -> list[dict]:
    """A block record in the shape roads' VRT and STAC writers read, for one sprout."""
    return [{**{k: r[k] for k in ("block", "width", "height", "transform")}, "bytes": r["layers"][layer]["bytes"]} for r in records]


def command_collect() -> None:
    work = DATA / "collect"
    records = rb.fetch_records(gcs.gs_join(rules.LINES["sprout"]["path"], "blocks/"), work / "records")
    missing = [b for b in blocks.all_blocks() if b not in records]
    if missing:
        raise SystemExit(f"{len(missing)} sprout blocks have no record, for example {missing[0]}")
    ordered = [records[b] for b in blocks.all_blocks()]
    for layer, recipe in LAYERS.items():
        out = work / layer
        out.mkdir(parents=True, exist_ok=True)
        per_layer = layer_records(ordered, layer)
        vrt = out / f"{rb.stem(recipe)}.vrt"
        vrt.write_text(rb.vrt_text(recipe, per_layer, BANDS[layer]))
        collection, item = rb.stac_documents(recipe, per_layer, BANDS[layer])
        (out / "collection.json").write_text(json.dumps(collection, indent=2) + "\n")
        (out / f"{recipe['sprout']['id']}.json").write_text(json.dumps(item, indent=2) + "\n")
        for path in (vrt, out / "collection.json", out / f"{recipe['sprout']['id']}.json"):
            gcs.upload_with_gcloud(path, gcs.gs_join(recipe["sprout"]["path"], path.name))
    print(json.dumps(totals("sprout", ordered)))


def totals(kind: str, records) -> dict:
    records = list(records)
    out: dict = {"blocks": len(records)}
    if kind == "seed":
        out["with_data"] = sum(1 for r in records if r.get("rows"))
        for key in ("rows", "bytes", "seconds"):
            out[key] = round(sum(r.get(key, 0) for r in records), 1)
        for field in ("by_subtype", "power_classes"):
            summed: dict[str, int] = {}
            for r in records:
                for k, v in r.get(field, {}).items():
                    summed[k] = summed.get(k, 0) + v
            out[field] = dict(sorted(summed.items(), key=lambda kv: -kv[1]))
    else:
        for layer in LAYERS:
            items = [r["layers"][layer] for r in records]
            by_value: dict[str, int] = {}
            for item in items:
                for k, v in item["by_value"].items():
                    by_value[k] = by_value.get(k, 0) + v
            out[layer] = {
                "with_data": sum(1 for i in items if i["bytes"]),
                **{key: round(sum(i.get(key, 0) for i in items), 1) for key in ("features", "marked_cells", "bytes", "seconds")},
                "by_value": by_value,
            }
        out["seconds"] = round(sum(r["seconds"] for r in records), 1)
        out["fetch_seconds"] = round(sum(r["fetch_seconds"] for r in records), 1)
    out["peak_memory_bytes_max"] = max((r.get("peak_memory_bytes", 0) for r in records), default=0)
    return out


# ---- submit --------------------------------------------------------------------------


def seed_records() -> dict[str, dict]:
    return rb.fetch_records(rules.LINES["seed"]["path"], DATA / "collect" / "seed-records")


def make_plan(kind: str, pilot: str | None) -> dict:
    if pilot:
        blocks.corner(pilot)
        return {"kind": kind, "groups": [[pilot]], "pilot": True}
    if kind == "seed":
        weights = blocks.footer_weights(json.loads((DATA / "footers.json").read_text()))
        minimum = 20_000.0  # a block with no footer still lists the footers and runs a query
    else:
        seeds = seed_records()
        missing = [b for b in blocks.all_blocks() if b not in seeds]
        if missing:
            raise SystemExit(f"{len(missing)} seed blocks have no record, for example {missing[0]}: run the seed job first")
        power = {b: sum(r.get("power_classes", {}).values()) for b, r in seeds.items()}
        # A block with power near it burns two 1.6 billion-cell bands, however few its features.
        weights = {b: sum(power[n] for n in [b, *blocks.neighbours(b)]) for b in blocks.all_blocks()}
        weights = {b: (w + 2_000_000 if w else 0) for b, w in weights.items()}
        minimum = 20_000.0
    return {"kind": kind, "groups": blocks.groups(weights, TASKS, minimum), "pilot": False}


def batch_job(prefix: str, kind: str, tasks: int, wheel: str, pilot: bool) -> dict:
    import shards  # tiles/shards.py, on the path roads/batch.py adds

    command = (
        f"python3 /tmp/in/energy/batch.py run-{kind} --plan /tmp/in/plan.json --work /tmp/work"
        + (" --pilot-run" if pilot else "")
    )
    script = shards.bootstrap(prefix, wheel, (*CODE_FILES, *RECIPE_COPIES)) + command + "\n"
    return shards.job_spec(SHAPES[kind], script, tasks, TASKS, gcs.BATCH_ENV)


def job_name(kind: str, pilot: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    label = f"-pilot-{pilot.lower().replace('_', '-')}" if pilot else ""
    return f"energy-2026-10-{kind}{label}-{stamp}"


def command_submit(kind: str, pilot: str | None, approved: bool) -> None:
    plan = make_plan(kind, pilot)
    work = DATA / "batch"
    work.mkdir(parents=True, exist_ok=True)
    wheel = rb.duckdb_wheel(work)
    prefix = scratch()
    name = job_name(kind, pilot)
    config = batch_job(prefix, kind, len(plan["groups"]), wheel.name, bool(pilot))
    copies = {}
    for relative, recipe in zip(RECIPE_COPIES, (rules.LINES, rules.SITES)):
        copies[relative] = work / Path(relative).name
        copies[relative].write_text(json.dumps(recipe, default=str) + "\n")
    (work / "recipe.json").write_text(json.dumps(rules.LINES, default=str) + "\n")  # the bootstrap fetches one
    (work / "plan.json").write_text(json.dumps(plan) + "\n")
    config_path = work / f"{name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    sizes = [len(g) for g in plan["groups"]]
    shape = SHAPES[kind]
    print(f"job {name}: {len(sizes)} task(s), {sum(sizes)} blocks, {min(sizes)}–{max(sizes)} a task, spot {shape['machine']}")
    print(f"config {config_path}")
    print(f"status: gcloud batch jobs describe {name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"tasks:  gcloud batch tasks list --job={name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"cancel: gcloud batch jobs delete {name} --location={gcs.REGION} --project={gcs.PROJECT}")
    if kind == "seed":
        print(f"output: {rules.LINES['seed']['path']}")
    elif pilot:
        print(f"output: {gcs.gs_join(scratch(), 'pilot/')}")
    else:
        print(f"output: {', '.join(gcs.gs_join(r['sprout']['path'], 'blocks/') for r in LAYERS.values())}")
    if not approved:
        print("not submitted: pass --approved once the cost is approved")
        return
    for relative in CODE_FILES:
        gcs.upload_with_gcloud(ROOT / relative, prefix + "code/" + relative)
    for relative, local in copies.items():
        gcs.upload_with_gcloud(local, prefix + "code/" + relative)
    for path in (work / "recipe.json", work / "plan.json", wheel):
        gcs.upload_with_gcloud(path, prefix + "code/" + path.name)
    gcs.gcloud(["services", "enable", "batch.googleapis.com"])
    gcs.gcloud(["batch", "jobs", "submit", name, f"--location={gcs.REGION}", f"--config={config_path}"])
    print(f"submitted {name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("submit", "run-seed", "run-sprout", "collect", "records"))
    parser.add_argument("kind", nargs="?", choices=("seed", "sprout"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--pilot", nargs="?", const="", help="one block, for example 40N_000E; with records, the pilot's")
    parser.add_argument("--pilot-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    if args.command == "submit":
        if not args.kind:
            parser.error("submit needs seed or sprout")
        command_submit(args.kind, args.pilot or None, args.approved)
    elif args.command == "run-seed":
        run_seed(json.loads(args.plan.read_text()), args.work)
    elif args.command == "run-sprout":
        run_sprout(json.loads(args.plan.read_text()), args.work, args.pilot_run)
    elif args.command == "collect":
        command_collect()
    else:
        if args.kind == "seed":
            records = seed_records()
        else:
            prefix = gcs.gs_join(scratch(), "pilot/power-lines/") if args.pilot is not None else gcs.gs_join(rules.LINES["sprout"]["path"], "blocks/")
            records = rb.fetch_records(prefix, DATA / "collect" / "sprout-records")
        print(json.dumps(totals(args.kind or "seed", records.values()), indent=1))


if __name__ == "__main__":
    main()

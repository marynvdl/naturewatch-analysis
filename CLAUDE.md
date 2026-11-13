# Nature Watch analysis

This repo writes the data pipelines for `marynvdl/naturewatch-app`: source map layers, spatial statistics, and writes to Google Cloud Storage and Supabase. The app reads the published objects. It does not run this code. The app repo is usually checked out beside this one at `../naturewatch-app`.

The stack is Python, GDAL/OGR, Cloud Optimized GeoTIFF, STAC metadata, Google Cloud Storage, Google Cloud Batch, and Earth Engine. Prefer lightweight scripts that run without an orchestrator, and plain SQL/PostGIS, over pipeline frameworks unless the task asks for one.

## Where a fact lives

Write each fact once.

- `docs/pipelines.md`: how a dataset becomes published objects, including transformations, cloud costs, and scripts.
- `cookbook/recipes/`: the hand-edited layer catalog.
- The step README (`seed/`, `sprout/`, `tiles/`, `stats/`, `places/`, `gee/`): how to run that step, and what the run cost.
- Task files and their parcels live in naturewatch-app: `../naturewatch-app/docs/tasks/` and `../naturewatch-app/docs/parcels/`, archived under `docs/archive/` there.
- In naturewatch-app: `docs/blueprint.md` for tables, bucket paths, and how the app reads layers, and `docs/archive/sooty-earth-roadmap.md` for the 20 October 2026 plan.

## Tasks

A task is one branch and one pull request into `main` here. `/task <file>` starts one. Do the named task only, and do not start the next until its done-when is met. A task marked for naturewatch-app is done in that repo.

If you are not at least 90% sure which direction to take, ask with a few concrete options, then wait. Plan a task before changing code.

## Cost control

Keeping compute, storage, and egress costs low is critical.

- Never run heavy analytical processing on this laptop. Test scripts on a tightly cropped sample area first.
- Keep memory use low, minimise database writes, and partition jobs when preparing to scale to 10,000+ polygons.
- Before a transfer, a Batch job, an Earth Engine export, or any cloud provisioning, write the expected cost and get explicit approval.
- Inspect workflow triggers before pushing, so a commit does not start a billable remote job.
- Record bytes stored, wall time, and peak memory in the step's README when a run completes.

## Retired workflows

The crop, built, and bare virtual-machine tile tasks are retired. Their folders are in `archive/vm-tile-tasks/`, and their workflows do not start a machine. `tasks/fireTiles/` is the live fire task and does not start one. Do not push a commit that restores an old virtual-machine workflow or moves a workflow file from `archive/` back into `.github/workflows/`: GitHub runs the workflow in the commit you push. New pipeline code lives outside `tasks/` until a workflow is added on purpose and started by hand.

## Accuracy

Keep geospatial figures exact: square kilometres, percentages, and valid coverage values. Distinguish source pixels from display tiles, and use the spheroid area method the blueprint names.

## Secrets

Never commit or expose a service-account JSON key, a private key, a bucket access token, or a database password. Reference credentials only through environment variables. If a raw token or key is exposed, stop and tell the user so it can be rotated.

## Git

Branch from `main`. When a task passes local checks, commit only its files, push the branch, and open a pull request into `main`.

Name a branch `<type>/<short-name>`, and a task branch `<type>/task-NN-<name>`. The types are `feat`, `fix`, `docs`, `chore`, `ci`, and `data` for work on the hosted database, the bucket, or Batch. A commit subject is `<type>: <imperative summary>`, about 72 characters at most, with a body only when the reason is not recorded elsewhere. A pull request title has the same form and names the task, for example `feat: recognitions on the map (task 09b)`. Its description follows `.github/PULL_REQUEST_TEMPLATE.md` and holds only what is not recorded elsewhere. Pull requests are squash-merged, so the title becomes the one commit on the target branch. Report the pull request and its checks. Do not merge until the checks pass and the user says so.

In the app repo, do not open or merge a pull request from `develop` into `main` until a production release is explicitly requested.

Do not add AI or tool attribution to commits, pull requests, or code.

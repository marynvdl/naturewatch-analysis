"""Browser checks for the version 3 cropland tiles, through Mapbox GL JS and PMTiles.

  python tiles/v3_check.py serve [--lan]      pages for any browser, results land in data/tiles/v3/results.jsonl
  python tiles/v3_check.py chrome --page check  headless Chrome with a fresh profile, waits for the result
  python tiles/v3_check.py load                 cold load of cropland and treeloss in headless Chrome, with a net log

The pages read `MAPBOX_ACCESS_TOKEN` from the environment of the server. It is not written to disk.
Archives are `data/tiles/v3/views/<encoding>.pmtiles`, made with `transcode.py views` and `transcode`.
Nothing here writes to the cloud.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import transcode  # noqa: E402
from cropland import ByteRangeHandler  # noqa: E402
from pmtiles.tile import zxy_to_tileid  # noqa: E402

HERE = Path(__file__).parent
PAGES = HERE / "v3_check"
DATA = HERE.parent / "data" / "tiles" / "v3"
VIEWS = DATA / "views"
RESULTS = DATA / "results.jsonl"
MAPBOX_JS = HERE.parent.parent / "naturewatch-app" / "frontend" / "naturewatch" / "node_modules" / "mapbox-gl" / "dist"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

ENCODINGS = ["gray-alpha", "rgb-r1", "rgb-r255", "rgba-r1", "rgba-r255", "palette-r1", "palette-r255", "palette-r1-512", "rgb-r1-512"]
PLACES = {
    "zakouma": ((10.02, 22.63), [2, 6, 9, 12, 13]),
    "madagascar-edge": ((-18.15, 49.98), [6, 9, 12]),
    "swartland": ((-33.35, 18.8), [9, 12, 13]),
    "ocean": ((0.0, -30.0), [6]),
}
GFW = "https://tiles.globalforestwatch.org/umd_tree_cover_loss/v1.13/dynamic/{z}/{x}/{y}.png?&start_year=2001&end_year=2024&render_type=true_color"


def tests() -> list[dict]:
    """One map view per place and zoom, with the tiles to read back.

    Zoom z up to 12: the map sits at z - 1 (256 px tiles) on the corner shared by four tiles.
    Zoom 13: the map sits at 12 on the centre of one zoom 12 tile, which Mapbox draws overzoomed.
    """
    out = []
    for name, ((lat, lon), zooms) in PLACES.items():
        for zoom in zooms:
            tile_zoom = min(zoom, 12)
            x, y = transcode.tile_at(lat, lon, tile_zoom)
            if zoom <= 12:
                tiles = [(tile_zoom, x + dx, y + dy) for dy in (-1, 0) for dx in (-1, 0)]
                center = corner(tile_zoom, x, y)
            else:
                tiles = [(tile_zoom, x, y)]
                west, north = corner(tile_zoom, x, y)
                east, south = corner(tile_zoom, x + 1, y + 1)
                center = ((west + east) / 2, math.degrees(math.atan(math.sinh((math.asinh(math.tan(math.radians(north))) + math.asinh(math.tan(math.radians(south)))) / 2))))
            out.append({"id": f"{name}-z{zoom}", "map_zoom": zoom - 1, "center": center, "tiles": tiles})
    return out


def corner(zoom: int, x: int, y: int) -> tuple[float, float]:
    n = 2**zoom
    return x / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


@functools.lru_cache(maxsize=1)
def reference() -> dict[int, bytes]:
    return dict(transcode.tiles_in_order(transcode.file_source(VIEWS / "gray-alpha.pmtiles")))


def expected(z: int, x: int, y: int) -> bytes:
    data = reference().get(zxy_to_tileid(z, x, y))
    if data is None:
        return bytes([transcode.NODATA]) * (transcode.SIZE * transcode.SIZE)
    return transcode.decode_v2(data).tobytes()


def tile_body(encoding: str, z: int, x: int, y: int) -> bytes | None:
    get = transcode.file_source(VIEWS / f"{encoding}.pmtiles")
    from pmtiles.reader import Reader

    return Reader(get).get(z, x, y)


# Archives that are read from the cloud through this server, so the page needs no public bucket or CORS.
# v3 is the merge output in scratch (not public): the server adds the caller's gcloud token. Nothing is written.
REMOTE = {
    "v2.pmtiles": ("https://storage.googleapis.com/nature-watch-tiles/cropland/2024/v2/cropland.pmtiles", False),
    "v3.pmtiles": (
        "https://storage.googleapis.com/storage/v1/b/nature-watch-bucket/o/"
        + urllib.parse.quote("scratch/tiles/cropland-2024-v3/cropland.pmtiles", safe="") + "?alt=media",
        True,
    ),
}
_token = {"value": None, "at": 0.0}


def gcloud_token() -> str:
    if _token["value"] is None or time.time() - _token["at"] > 1800:
        _token["value"] = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
        _token["at"] = time.time()
    return _token["value"]


class Handler(ByteRangeHandler):
    """Archives with Range, the pages, expected values, raw tiles, and a results sink."""

    results_event: threading.Event | None = None
    cache_control = "no-store"

    def send_header(self, keyword: str, value: str) -> None:
        if keyword == "Cache-Control":
            value = self.cache_control
        super().send_header(keyword, value)

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        if path == "/config.js":
            config = {"token": os.environ.get("MAPBOX_ACCESS_TOKEN", ""), "encodings": ENCODINGS, "tests": tests(), "v2": transcode.V2_URL, "gfw": GFW}
            self._send(200, f"window.CHECK = {json.dumps(config)};".encode(), "text/javascript")
        elif parts[0] == "expected" and len(parts) == 4:
            self._send(200, expected(*map(int, parts[1:])), "application/octet-stream")
        elif parts[0] == "png" and len(parts) == 5:
            body = tile_body(parts[1], *map(int, parts[2:]))
            self._send(200 if body else 404, body or b"", "image/png")
        elif parts[0] == "lib":
            self.path = "/" + "/".join(parts[1:])
            self.directory = str(MAPBOX_JS)
            super().do_GET()
        elif parts[0] == "slow" and len(parts) == 3:
            time.sleep(int(parts[1]) / 1000)
            self.path = "/" + parts[2]
            self.directory = str(VIEWS)
            super().do_GET()
        elif parts[0] == "remote" and len(parts) == 2 and parts[1] in REMOTE:
            self.proxy(*REMOTE[parts[1]])
        elif parts[0] == "archives":
            # ?delay=ms&cc=<Cache-Control> imitate a slow host and its headers, to see how the browser queues requests.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            time.sleep(int(query.get("delay", ["0"])[0]) / 1000)
            self.cache_control = query.get("cc", ["no-store"])[0]
            self.path = "/" + "/".join(parts[1:])
            self.directory = str(VIEWS)
            super().do_GET()
        else:
            self.directory = str(PAGES)
            super().do_GET()

    def proxy(self, url: str, auth: bool) -> None:
        """Forward a Range request to the archive and relay the answer."""
        headers = {"Range": self.headers.get("Range", "bytes=0-")}
        name = "v3" if auth else "v2"
        try:
            if auth:
                headers["Authorization"] = "Bearer " + gcloud_token()
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                body, status, content_range = response.read(), response.status, response.headers.get("Content-Range")
        except urllib.error.HTTPError as error:
            print(f"remote {name} {headers['Range']} -> {error.code} {error.reason}", flush=True)
            self._send(error.code, b"", "text/plain")
            return
        except Exception as error:  # no gcloud login, no network
            print(f"remote {name} {headers['Range']} -> failed: {error}", flush=True)
            self._send(502, str(error).encode(), "text/plain")
            return
        print(f"remote {name} {headers['Range']} -> {status} {len(body)} bytes", flush=True)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        self.wfile.write(body)
        Handler.remote_bytes += len(body)

    remote_bytes = 0

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        payload["user_agent"] = self.headers.get("User-Agent")
        payload["received"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        with RESULTS.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")
        summary = {key: payload.get(key) for key in ("page", "label", "summary")}
        print("result", json.dumps(summary), flush=True)
        if payload.get("final") and Handler.results_event:
            Handler.results_event.set()
        self._send(204, b"", "text/plain")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.end_headers()


def lan_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]


def serve(port: int, lan: bool) -> http.server.ThreadingHTTPServer:
    host = "0.0.0.0" if lan else "127.0.0.1"
    server = http.server.ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(PAGES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_chrome(url: str, timeout: float, net_log: Path | None = None) -> dict:
    """Open url in headless Chrome with a new profile (cold cache) and wait for a final result."""
    Handler.results_event = threading.Event()
    profile = Path(tempfile.mkdtemp(prefix="v3-chrome-"))
    args = [CHROME, "--headless=new", f"--user-data-dir={profile}", "--no-first-run", "--window-size=1024,768", "--enable-unsafe-swiftshader", "--use-angle=swiftshader"]
    if net_log:
        args += [f"--log-net-log={net_log}", "--net-log-capture-mode=Default"]
    process = subprocess.Popen(args + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finished = Handler.results_event.wait(timeout)
    process.terminate()
    process.wait(20)
    shutil.rmtree(profile, ignore_errors=True)
    return {"finished": finished}


def net_log_summary(path: Path) -> dict:
    """Requests per host from a Chrome net log: count, status, bytes received, OPTIONS preflights."""
    text = path.read_text()
    try:
        log = json.loads(text)
    except json.JSONDecodeError:
        log = json.loads(text.rstrip().rstrip(",") + "]}")  # Chrome was stopped before it closed the log
    types = {value: key for key, value in log["constants"]["logEventTypes"].items()}
    requests: dict[int, dict] = {}
    for event in log["events"]:
        kind = types.get(event["type"])
        source = event["source"]["id"]
        params = event.get("params") or {}
        if kind == "URL_REQUEST_START_JOB" and "url" in params:
            requests[source] = {"url": params["url"], "method": params.get("method"), "bytes": 0, "status": None, "preflight": False, "start": int(event["time"])}
        elif source not in requests:
            continue
        elif kind == "REQUEST_ALIVE" and event.get("phase") == 2:
            requests[source]["end"] = int(event["time"])
        elif kind == "HTTP_TRANSACTION_READ_RESPONSE_HEADERS":
            headers = params.get("headers") or [""]
            parts = headers[0].split()
            requests[source]["status"] = int(parts[1]) if len(parts) > 1 else None
            for header in headers[1:]:
                name, _, value = header.partition(":")
                if name.strip().lower() == "content-length":
                    requests[source]["length"] = int(value)
        elif kind in ("URL_REQUEST_JOB_BYTES_READ", "URL_REQUEST_JOB_FILTERED_BYTES_READ"):
            requests[source]["bytes"] = max(requests[source]["bytes"], 0) + params.get("byte_count", 0) * (kind.endswith("FILTERED_BYTES_READ"))
        elif kind == "CHECK_CORS_PREFLIGHT_REQUIRED" and params.get("preflight_required"):
            requests[source]["preflight"] = True
    hosts: dict[str, dict] = {}
    for request in requests.values():
        host = urllib.parse.urlparse(request["url"]).netloc
        row = hosts.setdefault(host, {"requests": 0, "options": 0, "preflight_required": 0, "bytes": 0, "status": {}})
        row["requests"] += 1
        row["options"] += request["method"] == "OPTIONS"
        row["preflight_required"] += request["preflight"]
        row["bytes"] += request.get("length", request["bytes"])
        status = str(request["status"])
        row["status"][status] = row["status"].get(status, 0) + 1
        row.setdefault("timeline", []).append((request["start"], request.get("end"), request["status"], request.get("length")))
    for row in hosts.values():
        first = min(start for start, *_rest in row["timeline"])
        row["timeline"] = [(start - first, end - start if end else None, status, length) for start, end, status, length in sorted(row["timeline"], key=lambda item: item[0])]
    return hosts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve")
    serve_cmd.add_argument("--port", type=int, default=5173)
    serve_cmd.add_argument("--lan", action="store_true", help="listen on the local network, for a phone")
    chrome = commands.add_parser("chrome")
    chrome.add_argument("--port", type=int, default=5173)
    chrome.add_argument("--page", default="check")
    chrome.add_argument("--query", default="")
    chrome.add_argument("--timeout", type=float, default=900)
    load = commands.add_parser("load")
    load.add_argument("--port", type=int, default=5173)
    load.add_argument("--runs", type=int, default=3)
    load.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    if not os.environ.get("MAPBOX_ACCESS_TOKEN"):
        raise SystemExit("set MAPBOX_ACCESS_TOKEN to a Mapbox public token")
    if args.command == "serve":
        server = serve(args.port, args.lan)
        host = lan_address() if args.lan else "localhost"
        print(f"check   http://{host}:{args.port}/check.html", flush=True)
        print(f"compare http://{host}:{args.port}/compare.html", flush=True)
        print("cancel: Ctrl+C", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
        return
    server = serve(args.port, False)
    base = f"http://localhost:{args.port}"  # the tile bucket allows this origin
    try:
        if args.command == "chrome":
            print(json.dumps(run_chrome(f"{base}/{args.page}.html?{args.query}", args.timeout)))
        else:
            rows = []
            for run in range(args.runs):
                for layer in ("cropland", "treeloss"):
                    log = DATA / "netlogs" / f"{layer}-{run}.json"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    status = run_chrome(f"{base}/load.html?layer={layer}&run={run}", args.timeout, log)
                    rows.append({"layer": layer, "run": run, **status, "hosts": net_log_summary(log)})
                    print(json.dumps(rows[-1]), flush=True)
            (DATA / "load-netlog.json").write_text(json.dumps(rows, indent=2))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

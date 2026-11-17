#!/usr/bin/env python3
"""Look at the roads sample archives on a Mapbox map, painted the way the app would paint them.

  MAPBOX_ACCESS_TOKEN=... python roads/preview.py [--port 8765]
  open http://127.0.0.1:8765/preview.html

Serves roads/preview.html and every data/roads/tiles/*.pmtiles with HTTP Range, on 127.0.0.1 only.
The token is read from the environment and handed to the page. It is not written anywhere.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tiles"))

import cropland  # noqa: E402
import seed as seedmod  # noqa: E402

TILES = seedmod.DATA / "tiles"


def config() -> dict:
    archives = sorted(path.name for path in TILES.glob("*.pmtiles"))
    windows = {name: box["bounds"] for name, box in seedmod.load_boxes().items() if box["kind"] == "window"}
    return {"token": os.environ.get("MAPBOX_ACCESS_TOKEN", ""), "archives": archives, "windows": windows}


class Handler(cropland.ByteRangeHandler):
    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0]
        if path in ("/", "/preview.html"):
            return str(HERE / "preview.html")
        if path.startswith("/tiles/"):
            return str(TILES / Path(path).name)
        return str(HERE / "missing")

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/config.js":
            body = ("window.ROADS = " + json.dumps(config()) + ";").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not os.environ.get("MAPBOX_ACCESS_TOKEN"):
        raise SystemExit("set MAPBOX_ACCESS_TOKEN (a public pk. token, such as the app's) and run again")
    if not list(TILES.glob("*.pmtiles")):
        raise SystemExit(f"no archive in {TILES}. Run roads/tiles.py window <name> first.")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"preview http://127.0.0.1:{args.port}/preview.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

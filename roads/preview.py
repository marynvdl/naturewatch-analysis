#!/usr/bin/env python3
"""Look at the roads sample archives on a Mapbox map, painted the way the app would paint them.

  MAPBOX_ACCESS_TOKEN=... python roads/preview.py [--port 8765] [--remote URL]
  open http://127.0.0.1:8765/preview.html

Serves roads/preview.html and every data/roads/tiles/*.pmtiles with HTTP Range, on 127.0.0.1 only.
A --remote archive is passed through at /remote/<n>/<name>, Range included, because the tiles bucket's
CORS rule does not allow this origin. Only the URLs given with --remote are fetched.
The token is read from the environment and handed to the page. It is not written anywhere.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tiles"))

import cropland  # noqa: E402
import seed as seedmod  # noqa: E402

TILES = seedmod.DATA / "tiles"


REMOTE: list[str] = []  # public archives, such as the published one, listed first


def config() -> dict:
    archives = REMOTE + sorted(path.name for path in TILES.glob("*.pmtiles"))
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

    def remote(self, head: bool) -> bool:
        """Pass /remote/<n>/<name> through to REMOTE[n]. False when the path is not one."""
        parts = self.path.split("?", 1)[0].split("/")
        if len(parts) < 3 or parts[1] != "remote":
            return False
        try:
            url = REMOTE[int(parts[2])]
        except (ValueError, IndexError):
            self.send_error(404, "No such remote archive")
            return True
        headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        request = urllib.request.Request(url, headers=headers, method="HEAD" if head else "GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = b"" if head else response.read()
                self.send_response(response.status)
                for name in ("Content-Type", "Content-Range", "ETag"):
                    if response.headers.get(name):
                        self.send_header(name, response.headers[name])
                self.send_header("Content-Length", response.headers.get("Content-Length", str(len(body))))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as error:
            self.send_error(error.code, error.reason)
        return True

    def do_HEAD(self):
        if not self.remote(head=True):
            super().do_HEAD()

    def do_GET(self):
        if self.remote(head=False):
            return
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
    parser.add_argument("--remote", action="append", default=[], help="a public archive URL to list as well")
    args = parser.parse_args()
    REMOTE.extend(args.remote)
    if not os.environ.get("MAPBOX_ACCESS_TOKEN"):
        raise SystemExit("set MAPBOX_ACCESS_TOKEN (a public pk. token, such as the app's) and run again")
    if not REMOTE and not list(TILES.glob("*.pmtiles")):
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

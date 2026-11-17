"""Draw one cached box as a PNG: Google share, TEMPO share after the floor, and Overture share.

  python buildings/render.py <name> [...] --threshold 0.5 --floor 0.02

Reads data/buildings/<name>.npz from compare.py and writes data/buildings/<name>.png.
Shades follow the sprout tiles: eight steps evenly spaced in sqrt(share), zero is blank.
Cells Google does not cover are pale blue in the Google panel.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare import CACHE, load_box  # noqa: E402

STEPS = 7
PANEL = 360
BLANK = (250, 250, 247)
UNCOVERED = (214, 228, 240)


def shade_index(share: np.ndarray) -> np.ndarray:
    """0 for no buildings, 1..STEPS evenly spaced in sqrt(share)."""
    s = np.clip(np.nan_to_num(share), 0.0, 1.0)
    k = np.ceil(np.sqrt(s) * STEPS).astype(np.uint8)
    return np.where(s > 0, np.maximum(k, 1), 0)


def palette() -> list[tuple[int, int, int]]:
    out = [BLANK]
    for k in range(1, STEPS + 1):
        v = int(round(225 - 205 * (k / STEPS)))
        out.append((v, v, v))
    return out


def panel(share: np.ndarray, covered: np.ndarray | None = None) -> Image.Image:
    colours = np.array(palette(), dtype=np.uint8)
    rgb = colours[shade_index(share)]
    if covered is not None:
        rgb[~covered] = UNCOVERED
    img = Image.fromarray(rgb, "RGB")
    scale = max(1, PANEL // max(img.size))
    return img.resize((img.width * scale, img.height * scale), Image.NEAREST)


def render(name: str, threshold: float, floor: float) -> Path:
    data = load_box(name)
    i = int(np.argmin(np.abs(data["thresholds"] - threshold)))
    covered = data["google_valid"] > 0.5
    tempo = np.nan_to_num(data["tempo"])
    tempo = np.where(tempo >= floor, tempo, 0.0) if floor > 0 else tempo
    panels = [
        (f"Google >= {data['thresholds'][i]:.1f}", panel(data["google"][i], covered)),
        (f"TEMPO, floor {floor:g}", panel(tempo)),
        ("Overture outlines", panel(data["overture"])),
    ]
    pad, head = 8, 22
    w = sum(p.width for _, p in panels) + pad * (len(panels) + 1)
    h = max(p.height for _, p in panels) + head + pad
    out = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(out)
    x = pad
    for title, p in panels:
        draw.text((x, 4), title, fill=(30, 30, 30))
        out.paste(p, (x, head))
        x += p.width + pad
    path = CACHE / f"{name}.png"
    out.save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("names", nargs="+")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--floor", type=float, default=0.0)
    args = p.parse_args(argv)
    for name in args.names:
        print(render(name, args.threshold, args.floor))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""10° blocks for the global roads seed and sprout.

A block is named from its south-west corner, like 30S_020E (30°S to 20°S, 20°E to 30°E).
A segment belongs to the block that holds the centre of its bbox, so every seed row is
stored once. The sprout of a block also burns the roads of its eight neighbours that
reach into it.
"""
from __future__ import annotations

BLOCK_DEG = 10
COLUMNS = 360 // BLOCK_DEG
ROWS = 180 // BLOCK_DEG


def name(south: int, west: int) -> str:
    lat = f"{abs(south):02d}{'S' if south < 0 else 'N'}"
    lon = f"{abs(west):03d}{'W' if west < 0 else 'E'}"
    return f"{lat}_{lon}"


def corner(block: str) -> tuple[int, int]:
    """South, west of a block name."""
    lat, lon = block.split("_")
    south = int(lat[:-1]) * (-1 if lat[-1] == "S" else 1)
    west = int(lon[:-1]) * (-1 if lon[-1] == "W" else 1)
    if south % BLOCK_DEG or west % BLOCK_DEG or not -90 <= south < 90 or not -180 <= west < 180:
        raise ValueError(f"not a block: {block}")
    return south, west


def bounds(block: str) -> tuple[float, float, float, float]:
    """West, south, east, north."""
    south, west = corner(block)
    return float(west), float(south), float(west + BLOCK_DEG), float(south + BLOCK_DEG)


def of_point(lon: float, lat: float) -> str:
    """The block holding a point. East and north edges belong to the block beyond, except at 180° and 90°."""
    col = min(max(int((lon + 180) // BLOCK_DEG), 0), COLUMNS - 1)
    row = min(max(int((lat + 90) // BLOCK_DEG), 0), ROWS - 1)
    return name(row * BLOCK_DEG - 90, col * BLOCK_DEG - 180)


def all_blocks() -> list[str]:
    """Every block, north to south, then west to east, so neighbours in a group sit close together."""
    return [
        name(south, west)
        for south in range(90 - BLOCK_DEG, -90 - 1, -BLOCK_DEG)
        for west in range(-180, 180, BLOCK_DEG)
    ]


def neighbours(block: str) -> list[str]:
    """The eight blocks around a block. Longitude wraps at 180°; there is nothing beyond the poles."""
    south, west = corner(block)
    out = []
    for dlat in (-BLOCK_DEG, 0, BLOCK_DEG):
        for dlon in (-BLOCK_DEG, 0, BLOCK_DEG):
            if dlat == 0 and dlon == 0:
                continue
            s = south + dlat
            if not -90 <= s < 90:
                continue
            w = (west + dlon + 180) % 360 - 180
            out.append(name(s, w))
    return out


def groups(weights: dict[str, float], count: int, minimum: float = 1.0) -> list[list[str]]:
    """Split every block, in all_blocks order, into at most `count` runs of similar total weight.

    A block missing from weights counts as `minimum`. Runs are contiguous, so a sprout
    task reuses neighbour files from one block to the next.
    """
    order = all_blocks()
    weight = [max(float(weights.get(b, 0.0)), minimum) for b in order]
    total = sum(weight)
    target = total / count
    out: list[list[str]] = [[]]
    running = 0.0
    for block, w in zip(order, weight):
        if out[-1] and running + w / 2 > target * len(out) and len(out) < count:
            out.append([])
        out[-1].append(block)
        running += w
    return out


def footer_weights(footers: dict) -> dict[str, float]:
    """Segment rows per block from `seed.py footers`, whose keys are '<south>_<west>'."""
    out: dict[str, float] = {}
    for key, value in footers.get("blocks", {}).items():
        if key == "unknown":
            continue
        south, west = (int(part) for part in key.split("_"))
        out[name(south, west)] = float(value["rows"])
    return out

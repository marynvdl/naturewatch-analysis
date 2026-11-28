"""The Energy rules, read from the two recipes, in Python for the checks and tests and in DuckDB SQL for the burn.

Power lines: a voltage class from the highest voltage on the line.
Power sites: a source group for plants, generators and substations.
The recipes are the catalog: cookbook/recipes/power-lines-2026-10-v1.yaml and power-sites-2026-10-v1.yaml.
"""
from __future__ import annotations

import json
from pathlib import Path

RECIPES = Path(__file__).resolve().parents[1] / "cookbook" / "recipes"
LINES_RECIPE = RECIPES / "power-lines-2026-10-v1.yaml"
SITES_RECIPE = RECIPES / "power-sites-2026-10-v1.yaml"


def load_recipe(path: Path) -> dict:
    """The recipe YAML, or the JSON copy beside it that a Batch task gets, since its container has no PyYAML."""
    copy = path.with_suffix(".json")
    if copy.exists():
        return json.loads(copy.read_text())
    import yaml

    return yaml.safe_load(path.read_text())


LINES = load_recipe(LINES_RECIPE)
SITES = load_recipe(SITES_RECIPE)
_lines = LINES["lines"]
_sites_recipe = SITES
_sites = _sites_recipe["sites"]

VOLTAGE_CLASSES = sorted(((int(c), v["from_volts"]) for c, v in _lines["voltage_classes"].items()), key=lambda cv: -cv[1])
LINE_CLASSES = dict(_lines["without_voltage"])  # the class when no voltage is tagged
BURIED = set(_lines["buried"])

GROUPS = list(_sites["group_order"])  # dirtiest first: a site with several sources takes the first listed
SOURCES = {source: group for group, sources in _sites["sources"].items() for source in sources}
ROOF = set(_sites["roof"])
MIN_GENERATOR_M2 = _sites["min_generator_m2"]  # a generator polygon smaller than this is a roof or a single array
MIN_SUBSTATION_M2 = _sites["min_substation_m2"]  # smaller substations are distribution kiosks
# Burn values. A cell takes the highest value burned into it, so dirtier groups sit higher.
SITE_VALUES = {name: int(value) for value, name in _sites_recipe["sprout"]["bands"]["source"].items() if int(value) > 0}
CARD = dict(_sites["card"])
OUTLINE_ONLY = set(_sites.get("outline_only", []))  # plant polygons of these groups are not burned


def volts(text: str | None) -> int | None:
    """The highest voltage in an OpenStreetMap voltage tag, such as '225000;63000'."""
    best = None
    for part in (text or "").replace(",", ";").split(";"):
        part = part.strip()
        if part.isdigit():
            best = max(best or 0, int(part))
    return best


def line_class(cls: str, tags: dict) -> int | None:
    """1–4 for a kept line, None for a line that is not drawn. Cables count only when overhead."""
    if tags.get("location") in BURIED:
        return None
    if cls == "cable" and tags.get("location") != "overhead":
        return None
    if cls not in LINE_CLASSES and cls != "cable":
        return None
    v = volts(tags.get("voltage"))
    if v is None:
        return LINE_CLASSES.get(cls, 1)
    return next(c for c, low in VOLTAGE_CLASSES if v >= low)


def group(source: str | None) -> str:
    found = {SOURCES.get(s.strip(), "other") for s in (source or "").split(";") if s.strip()}
    return next((g for g in GROUPS if g in found), "other")


def site_group(cls: str, tags: dict, area_m2: float | None) -> str | None:
    """The source group of a kept site, or None. `area_m2` is 0 for a point, None when not known.

    Plants keep their source. Generators keep theirs unless on a roof or, as a polygon, under MIN_GENERATOR_M2.
    A point generator is kept unless solar, where a point is a roof or a single array. A turbine marks its cell.
    Substations are their own group, as polygons from MIN_SUBSTATION_M2. A substation point is dropped.
    """
    if cls == "plant":
        return group(tags.get("plant:source"))
    if cls == "substation":
        return "substation" if area_m2 is None or area_m2 >= MIN_SUBSTATION_M2 else None
    if cls == "generator":
        source = group(tags.get("generator:source"))
        if tags.get("generator:location") in ROOF or tags.get("location") in ROOF:
            return None
        if area_m2 is None:
            return source
        if area_m2 == 0:
            return None if source == "solar" else source
        return source if area_m2 >= MIN_GENERATOR_M2 else None
    return None


def burned(cls: str, group: str | None, area_m2: float | None) -> bool:
    """Whether a kept site marks sprout cells. A wind plant polygon does not: its turbines do."""
    if group is None:
        return False
    return not (cls == "plant" and group in OUTLINE_ONLY and (area_m2 or 0) > 0)


# ---- the same rules in DuckDB SQL, over seed rows (class, source_tags, geometry) -----------------


def _q(values) -> str:
    return ", ".join("'" + v + "'" for v in sorted(values))


def volts_sql(tag: str = "source_tags['voltage']") -> str:
    parts = (
        f"list_filter(list_transform(string_split(replace(coalesce({tag}, ''), ',', ';'), ';'), lambda x: trim(x)), "
        "lambda x: regexp_full_match(x, '[0-9]+'))"
    )
    return f"list_max(list_transform({parts}, lambda x: CAST(x AS BIGINT)))"


def line_class_sql() -> str:
    v = volts_sql()
    by_volts = " ".join(f"WHEN {v} >= {low} THEN {c}" for c, low in VOLTAGE_CLASSES)
    no_volts = " ".join(f"WHEN class = '{k}' THEN {c}" for k, c in LINE_CLASSES.items())
    return (
        f"CASE WHEN coalesce(source_tags['location'], '') IN ({_q(BURIED)}) THEN NULL "
        f"WHEN class = 'cable' AND coalesce(source_tags['location'], '') <> 'overhead' THEN NULL "
        f"WHEN class NOT IN ({_q(list(LINE_CLASSES) + ['cable'])}) THEN NULL "
        f"WHEN {v} IS NULL THEN CASE {no_volts} ELSE 1 END "
        f"{by_volts} END"
    )


def group_sql(tag: str) -> str:
    """The group of a ';' list of sources: the first of GROUPS found, 'other' when none is known."""
    items = f"list_transform(string_split(coalesce({tag}, ''), ';'), lambda x: trim(x))"
    mapped = "list_transform(" + items + ", lambda x: CASE " + " ".join(
        f"WHEN x = '{s}' THEN '{g}'" for s, g in sorted(SOURCES.items())
    ) + " WHEN x = '' THEN NULL ELSE 'other' END)"
    return "CASE " + " ".join(f"WHEN list_contains({mapped}, '{g}') THEN '{g}'" for g in GROUPS) + " ELSE 'other' END"


def site_group_sql(area: str = "area_m2") -> str:
    """`area` is the spheroid area in m², 0 for a point."""
    gen = group_sql("source_tags['generator:source']")
    plant = group_sql("source_tags['plant:source']")
    roof = f"(coalesce(source_tags['generator:location'], '') IN ({_q(ROOF)}) OR coalesce(source_tags['location'], '') IN ({_q(ROOF)}))"
    return (
        f"CASE WHEN class = 'plant' THEN {plant} "
        f"WHEN class = 'substation' AND {area} >= {MIN_SUBSTATION_M2} THEN 'substation' "
        f"WHEN class = 'generator' AND {roof} THEN NULL "
        f"WHEN class = 'generator' AND {area} = 0 THEN CASE WHEN {gen} = 'solar' THEN NULL ELSE {gen} END "
        f"WHEN class = 'generator' AND {area} >= {MIN_GENERATOR_M2} THEN {gen} "
        f"END"
    )


def site_value_sql(group: str = "site_group") -> str:
    return "CASE " + " ".join(f"WHEN {group} = '{g}' THEN {v}" for g, v in SITE_VALUES.items()) + " END"


def burned_sql(group: str = "site_group", area: str = "area_m2") -> str:
    if not OUTLINE_ONLY:
        return "TRUE"
    return f"NOT (class = 'plant' AND {group} IN ({_q(OUTLINE_ONLY)}) AND {area} > 0)"

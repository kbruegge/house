#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "osmium>=4.0",
# ]
# ///
"""Render an OpenStreetMap PBF area around Richardstraße in Bochum as SVG and GeoJSON.

The script uses the local OSM PBF extract and does not call a geocoding
service. It finds ways whose exact OSM ``name`` is
Richardstraße (with German accents normalized), chooses the occurrence closest
to Bochum's center, and then renders nearby highway and railway ways.

Usage:
    uv run draw_richardstrasse.py
    uv run draw_richardstrasse.py --radius-km 2 --output bochum-map.svg
    uv run draw_richardstrasse.py --input path/to/map.osm.pbf --street "Richardstraße"

The default run writes both an SVG and a small GeoJSON FeatureCollection.
Ways are clipped to a circle of --radius-km around the selected street. The
GeoJSON contains one LineString per road or railway way, with only a type,
classification, and ``selected`` properties; street names and other OSM
metadata are omitted.
"""


import argparse
import html
import json
import math
import sys
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import osmium


# The center is only used to distinguish the several Richardstraße ways in the
# regional extract. The actual map extent is calculated from the matching ways.
BOCHUM_CENTER = (51.4818, 7.2162)  # latitude, longitude

# Width in SVG pixels, colour, and whether the way should be dashed.
HIGHWAY_STYLE: dict[str, tuple[float, str, bool]] = {
    "motorway": (7.0, "#bd5b5b", False),
    "motorway_link": (5.0, "#bd5b5b", False),
    "trunk": (6.0, "#d27b45", False),
    "trunk_link": (4.5, "#d27b45", False),
    "primary": (5.2, "#df9b45", False),
    "primary_link": (4.0, "#df9b45", False),
    "secondary": (4.4, "#e2bd58", False),
    "secondary_link": (3.5, "#e2bd58", False),
    "tertiary": (3.5, "#d2c46c", False),
    "tertiary_link": (3.0, "#d2c46c", False),
    "unclassified": (2.7, "#918b82", False),
    "residential": (2.5, "#918b82", False),
    "living_street": (2.4, "#918b82", False),
    "service": (1.6, "#aaa49b", False),
    "pedestrian": (1.8, "#bf8a9f", False),
    "cycleway": (1.3, "#589b91", True),
    "footway": (1.0, "#8c8a83", True),
    "path": (1.0, "#8c8a83", True),
    "bridleway": (1.0, "#9a8066", True),
    "track": (1.2, "#aa906e", True),
    "steps": (1.0, "#8c8a83", True),
    "corridor": (1.0, "#8c8a83", True),
}

# Drawing order keeps major roads underneath smaller streets and paths.
HIGHWAY_ORDER = {
    "motorway": 0,
    "motorway_link": 1,
    "trunk": 2,
    "trunk_link": 3,
    "primary": 4,
    "primary_link": 5,
    "secondary": 6,
    "secondary_link": 7,
    "tertiary": 8,
    "tertiary_link": 9,
}
# Railway values that represent linear tracks. Platforms, stations, and yards
# are intentionally omitted because this export is intended for line animation.
RAILWAY_TYPES = {
    "rail",
    "light_rail",
    "tram",
    "subway",
    "narrow_gauge",
    "preserved",
    "monorail",
    "funicular",
    "construction",
    "disused",
    "abandoned",
}
RAILWAY_STYLE: dict[str, tuple[float, str, bool]] = {
    "rail": (3.0, "#65615b", False),
    "light_rail": (2.6, "#77716a", False),
    "tram": (2.2, "#77716a", False),
    "subway": (2.4, "#77716a", False),
}

LABEL_HIGHWAYS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
}


@dataclass(slots=True)
class Way:
    osm_id: int
    highway: str
    railway: str
    name: str
    coords: list[tuple[float, float]]  # longitude, latitude
    tags: dict[str, str]
    part: int = 0  # index of the piece when a way is clipped into several runs

    @property
    def midpoint(self) -> tuple[float, float]:
        return self.coords[len(self.coords) // 2]

    @property
    def length_degrees(self) -> float:
        return sum(
            math.hypot(b[0] - a[0], b[1] - a[1])
            for a, b in zip(self.coords, self.coords[1:])
        )


def normalized_name(value: str) -> str:
    """Normalize street names so Richardstraße and Richardstrasse match."""
    value = value.casefold().replace("ß", "ss")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return "".join(value.split())


def way_from_osm(way: osmium.osm.Way) -> Way | None:
    highway = way.tags.get("highway")
    railway = way.tags.get("railway")
    if not highway and railway not in RAILWAY_TYPES:
        return None
    if way.tags.get("area") == "yes":
        return None

    coords = [
        (node.location.lon, node.location.lat)
        for node in way.nodes
        if node.location.valid()
    ]
    if len(coords) < 2:
        return None

    return Way(
        osm_id=way.id,
        highway=str(highway or ""),
        railway=str(railway or ""),
        name=str(way.tags.get("name", "")),
        coords=coords,
        tags={str(key): str(value) for key, value in way.tags},
    )


class TargetWayHandler(osmium.SimpleHandler):
    def __init__(self, street: str) -> None:
        super().__init__()
        self.street = normalized_name(street)
        self.ways: list[Way] = []

    def way(self, way: osmium.osm.Way) -> None:
        parsed = way_from_osm(way)
        if (
            parsed
            and parsed.highway
            and normalized_name(parsed.name) == self.street
        ):
            self.ways.append(parsed)


class NearbyWayHandler(osmium.SimpleHandler):
    def __init__(self, bounds: tuple[float, float, float, float]) -> None:
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = bounds
        self.ways: list[Way] = []

    def way(self, way: osmium.osm.Way) -> None:
        parsed = way_from_osm(way)
        if not parsed:
            return
        lons = [point[0] for point in parsed.coords]
        lats = [point[1] for point in parsed.coords]
        if (
            max(lons) >= self.min_lon
            and min(lons) <= self.max_lon
            and max(lats) >= self.min_lat
            and min(lats) <= self.max_lat
        ):
            self.ways.append(parsed)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Approximate distance between (longitude, latitude) pairs."""
    lon_scale = math.cos(math.radians((a[1] + b[1]) / 2.0))
    dx = (a[0] - b[0]) * 111.32 * lon_scale
    dy = (a[1] - b[1]) * 110.57
    return math.hypot(dx, dy)


def average_point(points: Iterable[tuple[float, float]]) -> tuple[float, float]:
    points = list(points)
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def circle_bounds(
    center: tuple[float, float], radius_km: float
) -> tuple[float, float, float, float]:
    """Bounding box (min_lon, min_lat, max_lon, max_lat) of a circle in lon/lat."""
    lon, lat = center
    lat_padding = radius_km / 110.57
    lon_padding = radius_km / (111.32 * max(0.2, math.cos(math.radians(lat))))
    return (lon - lon_padding, lat - lat_padding, lon + lon_padding, lat + lat_padding)


def parse_center(value: str) -> tuple[float, float]:
    try:
        lat, lon = (float(part.strip()) for part in value.split(",", maxsplit=1))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
        return lat, lon
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("center must be written as LAT,LON") from None


def find_input(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.is_file():
            raise FileNotFoundError(f"OSM input does not exist: {explicit}")
        return explicit

    script_dir = Path(__file__).resolve().parent
    # The script may live beside the data directory or one level above it.
    search_dirs = [Path.cwd(), script_dir, script_dir / "osm_data"]
    candidates: list[Path] = []
    for directory in search_dirs:
        candidates.extend(directory.glob("*.osm.pbf"))
        candidates.extend(directory.glob("*.pbf"))
    candidates = list(dict.fromkeys(path.resolve() for path in candidates))
    if not candidates:
        raise FileNotFoundError(
            "No .osm.pbf or .pbf file found; pass one with --input"
        )
    # If an osm_data directory contains several extracts, the largest is usually
    # the regional extract rather than a small test file.
    return max(candidates, key=lambda path: path.stat().st_size)


def style_for_way(way: Way) -> tuple[float, str, bool]:
    if way.railway:
        return RAILWAY_STYLE.get(way.railway, (2.4, "#77716a", False))
    return HIGHWAY_STYLE.get(way.highway, (1.5, "#9a958c", False))


def circle_interval(
    a: tuple[float, float],
    b: tuple[float, float],
    center: tuple[float, float],
    radius_km: float,
) -> tuple[float, float] | None:
    """Parameter range [t0, t1] of segment a->b (0..1) that lies inside the circle.

    The distance is measured in a local kilometre plane around the centre, so
    the circle is round on the map rather than an ellipse in lon/lat.
    """
    clon, clat = center
    kx = 111.32 * math.cos(math.radians(clat))
    ky = 110.57
    ax, ay = (a[0] - clon) * kx, (a[1] - clat) * ky
    dx, dy = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    qa = dx * dx + dy * dy
    qb = 2.0 * (ax * dx + ay * dy)
    qc = ax * ax + ay * ay - radius_km * radius_km
    if qa == 0.0:
        return (0.0, 1.0) if qc <= 0.0 else None
    disc = qb * qb - 4.0 * qa * qc
    if disc < 0.0:
        return None
    root = math.sqrt(disc)
    t0 = max(0.0, (-qb - root) / (2.0 * qa))
    t1 = min(1.0, (-qb + root) / (2.0 * qa))
    return (t0, t1) if t0 <= t1 else None


def lerp(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def clip_ways_to_circle(
    ways: list[Way], center: tuple[float, float], radius_km: float
) -> list[Way]:
    """Cut every way at the circle, keeping only the parts inside it.

    Long ways (e.g. rail lines crossing the area) are split into runs so no
    geometry remains outside the radius.
    """
    clipped: list[Way] = []
    for way in ways:
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for a, b in zip(way.coords, way.coords[1:]):
            interval = circle_interval(a, b, center, radius_km)
            if interval is None:
                if len(current) >= 2:
                    runs.append(current)
                current = []
                continue
            t0, t1 = interval
            start = a if t0 == 0.0 else lerp(a, b, t0)
            end = b if t1 == 1.0 else lerp(a, b, t1)
            if not current or current[-1] != start:
                if len(current) >= 2:
                    runs.append(current)
                current = [start]
            current.append(end)
        if len(current) >= 2:
            runs.append(current)
        for part, coords in enumerate(runs):
            clipped.append(replace(way, coords=coords, part=part))
    return clipped


def write_geojson(
    output: Path,
    ways: list[Way],
    target_ways: list[Way],
    bounds: tuple[float, float, float, float],
) -> None:
    """Write a compact, browser-friendly GeoJSON export."""
    min_lon, min_lat, max_lon, max_lat = bounds
    target_ids = {way.osm_id for way in target_ways}
    features = []
    for way in ways:
        properties = {"selected": way.osm_id in target_ids}
        if way.railway:
            properties["type"] = "railway"
            properties["railway"] = way.railway
        else:
            properties["type"] = "highway"
            properties["highway"] = way.highway
        features.append(
            {
                "type": "Feature",
                "id": f"way/{way.osm_id}" + (f"-{way.part}" if way.part else ""),
                "properties": properties,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [round(lon, 6), round(lat, 6)]
                        for lon, lat in way.coords
                    ],
                },
            }
        )

    collection = {
        "type": "FeatureCollection",
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "features": features,
    }
    output.write_text(
        json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def svg_path(way: Way, project) -> str:
    points = [project(lon, lat) for lon, lat in way.coords]
    return "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def write_svg(
    output: Path,
    ways: list[Way],
    target_ways: list[Way],
    bounds: tuple[float, float, float, float],
    street: str,
    width: int,
    height: int,
) -> None:
    min_lon, min_lat, max_lon, max_lat = bounds
    margin = 28.0
    usable_width = width - 2 * margin
    usable_height = height - 2 * margin
    center_lat = (min_lat + max_lat) / 2.0
    meters_per_lon = 111_320.0 * math.cos(math.radians(center_lat))
    meters_per_lat = 110_570.0
    map_width_m = (max_lon - min_lon) * meters_per_lon
    map_height_m = (max_lat - min_lat) * meters_per_lat
    scale = min(usable_width / map_width_m, usable_height / map_height_m)
    drawn_width = map_width_m * scale
    drawn_height = map_height_m * scale
    x_offset = margin + (usable_width - drawn_width) / 2.0
    y_offset = margin + (usable_height - drawn_height) / 2.0

    def project(lon: float, lat: float) -> tuple[float, float]:
        x = x_offset + (lon - min_lon) * meters_per_lon * scale
        y = y_offset + (max_lat - lat) * meters_per_lat * scale
        return x, y

    target_ids = {way.osm_id for way in target_ways}
    sorted_ways = sorted(
        ways,
        key=lambda way: (HIGHWAY_ORDER.get(way.highway, 20), way.osm_id),
    )

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Map around {html.escape(street)} in Bochum">',
        "  <title>OpenStreetMap roads around " + html.escape(street) + ", Bochum</title>",
        "  <desc>Generated from the local OpenStreetMap PBF extract.</desc>",
        '  <rect width="100%" height="100%" fill="#f7f4ee"/>',
        '  <g id="roads" fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]

    for way in sorted_ways:
        line_width, colour, dashed = style_for_way(way)
        path = svg_path(way, project)
        name = html.escape(way.name or way.railway or way.highway, quote=True)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        opacity = ' opacity="0.6"' if way.tags.get("tunnel") == "yes" else ""
        # A pale casing prevents local roads disappearing where they cross.
        lines.append(
            f'    <path d="{path}" stroke="#fffdf8" stroke-width="{line_width + 2.2:.1f}"{opacity}/>'
        )
        lines.append(
            f'    <path d="{path}" stroke="{colour}" stroke-width="{line_width:.1f}"{dash}{opacity}>'
            f"<title>{name}</title></path>"
        )
        if way.railway:
            # A light dashed center line makes tracks recognizable without
            # drawing a full pair of rails, which stays subtle at this scale.
            lines.append(
                f'    <path d="{path}" stroke="#f7f4ee" stroke-width="0.9" '
                f'stroke-dasharray="1 5"{opacity}/>'
            )

    # Highlight all segments of the selected occurrence, rather than only one
    # OSM way, so the street remains easy to spot in the generated map.
    for way in target_ways:
        path = svg_path(way, project)
        lines.append(
            f'    <path d="{path}" stroke="#fffdf8" stroke-width="6.8"/>'
        )
        lines.append(
            f'    <path d="{path}" stroke="#c94c4c" stroke-width="4.2">'
            f'<title>{html.escape(way.name, quote=True)} (selected street)</title></path>'
        )
    lines.append("  </g>")

    # Label the selected street and named larger/local streets. Keep one label
    # per name and prefer the longest way for names split into many segments.
    target_labels: dict[str, Way] = {}
    for way in target_ways:
        target_labels.setdefault(way.name, way)
    other_labels: dict[str, Way] = {}
    for way in sorted(ways, key=lambda item: item.length_degrees, reverse=True):
        if way.name and way.osm_id not in target_ids and way.highway in LABEL_HIGHWAYS:
            other_labels.setdefault(way.name, way)
    label_ways = dict(
        list(target_labels.items())
        + list(other_labels.items())[: max(0, 60 - len(target_labels))]
    )
    labels: list[str] = ['  <g id="labels" font-family="system-ui, sans-serif">']
    for name, way in label_ways.items():
        x, y = project(*way.midpoint)
        selected = way.osm_id in target_ids
        font_size = 12 if selected else 9
        colour = "#a83232" if selected else "#5c5852"
        weight = "bold" if selected else "normal"
        labels.append(
            f'    <text x="{x:.1f}" y="{y:.1f}" font-size="{font_size}" '
            f'font-weight="{weight}" fill="{colour}" text-anchor="middle" '
            f'paint-order="stroke" stroke="#f7f4ee" stroke-width="3">'
            f"{html.escape(name)}</text>"
        )
    labels.append("  </g>")
    lines.extend(labels)

    target_point = average_point(point for way in target_ways for point in way.coords)
    target_x, target_y = project(*target_point)
    lines.extend(
        [
            '  <g id="map-info">',
            f'    <rect x="16" y="14" width="260" height="48" rx="6" fill="#fffdf8" fill-opacity="0.92" stroke="#d7d0c5"/>',
            f'    <text x="28" y="35" font-family="system-ui, sans-serif" font-size="14" font-weight="bold" fill="#302d29">{html.escape(street)}, Bochum</text>',
            f'    <text x="28" y="51" font-family="system-ui, sans-serif" font-size="10" fill="#6d665d">{len(ways)} map ways · local PBF extract</text>',
            f'    <circle cx="{target_x:.1f}" cy="{target_y:.1f}" r="7" fill="none" stroke="#c94c4c" stroke-width="1.5"/>',
            "  </g>",
            '  <text x="16" y="' + str(height - 12) + '" font-family="system-ui, sans-serif" font-size="9" fill="#817a71">© OpenStreetMap contributors · rendered locally</text>',
            "</svg>",
        ]
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        help="OSM PBF file (otherwise the largest *.osm.pbf/*.pbf in this directory)",
    )
    parser.add_argument(
        "--street",
        default="Richardstrasse",
        help="street name to find; accents are normalized (default: Richardstrasse)",
    )
    parser.add_argument(
        "--center",
        type=parse_center,
        default=BOCHUM_CENTER,
        metavar="LAT,LON",
        help="center used to choose the Bochum occurrence (default: Bochum center)",
    )
    parser.add_argument(
        "--radius-km",
        type=float,
        default=0.5,
        help="radius of the circle around the selected street; ways are clipped to it (default: 0.5)",
    )
    parser.add_argument(
        "--match-radius-km",
        type=float,
        default=3.0,
        help="join matching street segments within this distance (default: 3)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="SVG output path (default: next to the input PBF)",
    )
    parser.add_argument(
        "--geojson-output",
        type=Path,
        default=None,
        help="GeoJSON output path (default: next to the input PBF)",
    )
    parser.add_argument("--width", type=int, default=1200, help="SVG width in pixels")
    parser.add_argument("--height", type=int, default=900, help="SVG height in pixels")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.radius_km <= 0 or args.match_radius_km <= 0:
        print("radius values must be positive", file=sys.stderr)
        return 2
    if args.width < 200 or args.height < 200:
        print("SVG dimensions must be at least 200 pixels", file=sys.stderr)
        return 2

    try:
        input_path = find_input(args.input)
        print(f"Reading {input_path}")
        target_handler = TargetWayHandler(args.street)
        target_handler.apply_file(str(input_path), locations=True)
    except (FileNotFoundError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1

    if not target_handler.ways:
        print(
            f"No highway named {args.street!r} was found in {input_path.name}",
            file=sys.stderr,
        )
        return 1

    center = (args.center[1], args.center[0])  # longitude, latitude
    anchor_way = min(
        target_handler.ways,
        key=lambda way: distance_km(way.midpoint, center),
    )
    anchor = anchor_way.midpoint
    target_ways = [
        way
        for way in target_handler.ways
        if distance_km(way.midpoint, anchor) <= args.match_radius_km
    ]
    points = [point for way in target_ways for point in way.coords]
    target_centre = (
        (min(p[0] for p in points) + max(p[0] for p in points)) / 2.0,
        (min(p[1] for p in points) + max(p[1] for p in points)) / 2.0,
    )
    bounds = circle_bounds(target_centre, args.radius_km)

    print(
        f"Found {len(target_handler.ways)} matching highway ways; "
        f"using {len(target_ways)} near {anchor[1]:.5f},{anchor[0]:.5f} "
        f"({distance_km(anchor, center):.1f} km from supplied center)"
    )
    print("Reading nearby highway geometry for the second pass")
    nearby_handler = NearbyWayHandler(bounds)
    nearby_handler.apply_file(str(input_path), locations=True)

    output = (
        args.output.expanduser()
        if args.output
        else input_path.parent / "richardstrasse-bochum.svg"
    )
    geojson_output = (
        args.geojson_output.expanduser()
        if args.geojson_output
        else input_path.parent / "richardstrasse-bochum.geojson"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    geojson_output.parent.mkdir(parents=True, exist_ok=True)

    map_ways = clip_ways_to_circle(nearby_handler.ways, target_centre, args.radius_km)
    write_svg(
        output,
        map_ways,
        target_ways,
        bounds,
        args.street,
        args.width,
        args.height,
    )
    write_geojson(
        geojson_output,
        map_ways,
        target_ways,
        bounds,
    )
    print(f"Wrote {output} ({len(map_ways)} map ways, clipped to circle)")
    print(f"Wrote {geojson_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import * as d3 from "d3";
import type { Feature, FeatureCollection, LineString } from "geojson";

import "./styles.css";

import { roadsGzipBase64 } from "./generated/roads";

type RoadProps = { type: string; selected: boolean; highway?: string; railway?: string };
type Road = Feature<LineString, RoadProps>;

// The GeoJSON is stored gzipped (see scripts/generate-roads.ts); decompress it here.
async function loadRoads(): Promise<FeatureCollection<LineString, RoadProps>> {
  const bytes = Uint8Array.from(atob(roadsGzipBase64), (c) => c.charCodeAt(0));
  const json = new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip")));
  return (await json.json()) as FeatureCollection<LineString, RoadProps>;
}

const roads = await loadRoads();

const EARTH_RADIUS_M = 6_371_000;
// The map is framed on a circle of this radius around the centre (zoom level).
const FRAME_RADIUS_M = 800;

const centre = d3.geoCentroid(roads);
const frame = d3.geoCircle().center(centre).radius((FRAME_RADIUS_M / EARTH_RADIUS_M) * (180 / Math.PI))();

// Muted tones that sit quietly on the #f7f4ee paper colour.
const LINE_STYLE: Record<string, { color: string; width: number }> = {
  primary: { color: "#b98f52", width: 2.2 },
  trunk: { color: "#b98f52", width: 2.2 },
  secondary: { color: "#bf9d62", width: 2 },
  tertiary: { color: "#c7aa74", width: 1.8 },
  residential: { color: "#a6a093", width: 1.4 },
  unclassified: { color: "#a6a093", width: 1.4 },
  living_street: { color: "#a6a093", width: 1.4 },
  service: { color: "#bcb6a9", width: 1 },
  footway: { color: "#9e9383", width: 0.9 },
  path: { color: "#9e9383", width: 0.9 },
  steps: { color: "#9e9383", width: 0.9 },
  track: { color: "#a99273", width: 1 },
  railway: { color: "#8c9396", width: 1.1 },
  other: { color: "#b5b0a4", width: 1 },
};

// Richardstraße itself (the exporter marks it `selected`) gets a darker, heavier stroke.
const SELECTED_STYLE = { color: "#8a5a1e", width: 2.8 };

function styleFor(road: Road) {
  if (road.properties.selected) return SELECTED_STYLE;
  const kind = road.properties.railway ? "railway" : (road.properties.highway ?? "other");
  return LINE_STYLE[kind] ?? LINE_STYLE.other;
}

// Small seeded PRNG so the reveal looks the same on every visit.
function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const svg = d3.select<SVGSVGElement, unknown>("#map");
const layer = svg.append("g").attr("fill", "none").attr("stroke-linecap", "round").attr("stroke-linejoin", "round");
const PAD = 40;

const projection = d3.geoMercator();
const pathFor = d3.geoPath(projection);

// Lines are drawn with pathLength="1" so the dash math is independent of
// geometry size; that lets us re-project on resize without restarting.
const paths = layer
  .selectAll<SVGPathElement, Road>("path")
  .data(roads.features)
  .join("path")
  .attr("pathLength", 1)
  .attr("stroke", (d) => styleFor(d).color)
  .attr("stroke-width", (d) => styleFor(d).width)
  .attr("stroke-opacity", (d) => (d.properties.selected ? 0.9 : 0.6))
  .attr("stroke-dasharray", "1 1")
  .attr("stroke-dashoffset", 1);

function layout() {
  const width = window.innerWidth;
  const height = window.innerHeight;
  svg.attr("width", width).attr("height", height).attr("viewBox", `0 0 ${width} ${height}`);
  projection.fitExtent(
    [
      [PAD, PAD],
      [width - PAD, height - PAD],
    ],
    frame,
  );
  paths.attr("d", (d) => pathFor(d));
}

// Reveal order: radiate outward from the centre of the network, with jitter.
function scheduleReveal() {
  const ranked = roads.features
    .map((f) => ({ f, dist: d3.geoDistance(centre, d3.geoCentroid(f)) }))
    .sort((a, b) => a.dist - b.dist);
  
  const rank = new Map<Road, number>(ranked.map((r, i) => [r.f as Road, i / Math.max(1, ranked.length - 1)]));

  const rand = mulberry32(42);
  const START_MS = 800;
  const SPREAD_MS = 22000;
  const JITTER_MS = 700;

  const delay = new Map<Road, number>();
  const duration = new Map<Road, number>();
  for (const f of roads.features as Road[]) {
    delay.set(f, START_MS + (rank.get(f) ?? 0) * SPREAD_MS + rand() * JITTER_MS);
    duration.set(f, 2600 + rand() * 1800);
  }

  paths
    .transition()
    .delay((d) => delay.get(d) ?? 0)
    .duration((d) => duration.get(d) ?? 3000)
    .ease(d3.easeSinInOut)
    .attr("stroke-dashoffset", 0);
}

layout();

if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  paths.attr("stroke-dashoffset", 0);
} else {
  scheduleReveal();
}

let resizeFrame = 0;
window.addEventListener("resize", () => {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(layout);
});

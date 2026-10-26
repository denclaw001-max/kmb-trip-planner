import { useEffect, useRef, useState } from "react";
// MapLibre v6 loads its tile-parsing worker as a sibling file of the
// library module (…/assets/maplibre-gl-worker.mjs). The Vite bundle does
// not emit that file, so production maps stayed blank with "Worker
// failed to load" until the build script was made to copy it into
// dist/assets/ (see "build" in package.json). The committed copy under
// web/dist/assets/ is what the deployed site serves.
import * as maplibregl from "maplibre-gl";
import type { Feature } from "geojson";
import "maplibre-gl/dist/maplibre-gl.css";

// Explicit worker URL (with a cache-busting query): browsers that
// visited while the worker file was missing may hold a cached failure
// for the library's default module-relative URL.
maplibregl.setWorkerUrl("/assets/maplibre-gl-worker.mjs?wk=2");

export interface Endpoint { label: string; lat: number; lon: number }
export interface BusLeg {
  type: "bus"; route: string; bound: string; line_key: string;
  dest_tc: string; dest_en: string;
  from_stop: StopRef; to_stop: StopRef; stops: number; minutes: number;
  path: [number, number][];
}
export interface WalkLeg {
  type: "walk"; from_stop: StopRef | null; to_stop: StopRef | null;
  meters: number; minutes: number; path: [number, number][];
}
export interface StopRef { id: string; name_tc: string; name_en: string; lat: number; lon: number }
export type Leg = BusLeg | WalkLeg;
export interface Itinerary {
  transfers: number; total_minutes: number; walk_meters: number; legs: Leg[];
}

const STYLE_URL =
  "https://mapapi.geodata.gov.hk/gs/api/v1.0.0/vt/basemap/WGS84/resources/styles/root.json";
const VT_BASE =
  "https://mapapi.geodata.gov.hk/gs/api/v1.0.0/vt/basemap/WGS84";

const OSM_STYLE = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm" }],
} as unknown as maplibregl.StyleSpecification;

// Diagnostic basemap (Phase 3 follow-up): the full LandsD style renders
// nothing in browsers even though its tiles verify fine. MINIMAL_STYLE
// uses the same LandsD tiles with a handful of hand-picked layers, so
// ?basemap=minimal / ?basemap=osm / (default full LandsD) can be
// compared in the real app to isolate the failing layer.
const MINIMAL_STYLE = {
  version: 8,
  glyphs: `${VT_BASE}/resources/fonts/{fontstack}/{range}.pbf`,
  sources: {
    esri: {
      type: "vector",
      tiles: [`${VT_BASE}/tile/{z}/{y}/{x}.pbf`],
      scheme: "xyz",
      maxzoom: 19,
      attribution:
        "<a href='https://api.portal.hkmapservice.gov.hk/disclaimer' target='_blank'>&copy; Map information from Lands Department</a>",
    },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#eef2ea" } },
    {
      id: "sea", type: "fill", source: "esri",
      "source-layer": "13_HYDRPOLY_Seafill_200K",
      paint: { "fill-color": "#b8d4ea" },
    },
    {
      id: "water", type: "fill", source: "esri",
      "source-layer": "10_HYDRPOLY_200K",
      paint: { "fill-color": "#a8c8e8" },
    },
    {
      id: "roads", type: "line", source: "esri",
      "source-layer": "TSPTLINE_200K:1",
      paint: { "line-color": "#ffffff", "line-width": 2 },
    },
  ],
} as unknown as maplibregl.StyleSpecification;

export type BasemapMode = "landsd" | "minimal" | "osm";

export function basemapMode(): BasemapMode {
  const v = new URLSearchParams(window.location.search).get("basemap");
  return v === "minimal" || v === "osm" ? v : "landsd";
}

// The LandsD style JSON points at its tiles through a relative "../../"
// reference to an ArcGIS service description, which MapLibre cannot turn
// into tile requests on its own (the map stays blank). Fetch the style,
// rewrite the source to explicit tile URLs (note: {z}/{y}/{x} order), and
// absolutise the glyphs path before handing it to MapLibre.
async function loadStyle(mode: BasemapMode): Promise<maplibregl.StyleSpecification> {
  if (mode === "osm") return OSM_STYLE;
  if (mode === "minimal") return MINIMAL_STYLE;
  try {
    const res = await fetch(STYLE_URL);
    if (!res.ok) throw new Error(`style ${res.status}`);
    const style = await res.json();
    const src = style.sources?.esri;
    if (src) {
      delete src.url;
      src.type = "vector";
      src.scheme = "xyz";
      src.tiles = [`${VT_BASE}/tile/{z}/{y}/{x}.pbf`];
    }
    style.glyphs = `${VT_BASE}/resources/fonts/{fontstack}/{range}.pbf`;
    if (typeof style.sprite === "string" && !style.sprite.startsWith("http")) {
      style.sprite = `${VT_BASE}/resources/sprites/sprite`;
    }
    return style as maplibregl.StyleSpecification;
  } catch {
    // Last-resort basemap so the map is never blank.
    return OSM_STYLE;
  }
}

interface Props {
  from: Endpoint | null;
  to: Endpoint | null;
  itinerary: Itinerary | null;
  pickTarget: "from" | "to" | null;
  onPick: (target: "from" | "to", lat: number, lon: number) => void;
  onDrag: (target: "from" | "to", lat: number, lon: number) => void;
}

export default function MapView({ from, to, itinerary, pickTarget, onPick, onDrag }: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markers = useRef<{ from?: maplibregl.Marker; to?: maplibregl.Marker }>({});
  const propsRef = useRef({ pickTarget, onPick, onDrag });
  propsRef.current = { pickTarget, onPick, onDrag };
  const [ready, setReady] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!divRef.current || mapRef.current) return;
    let cancelled = false;
    let map: maplibregl.Map | null = null;
    const mode = basemapMode();
    loadStyle(mode).then((style) => {
      if (cancelled || !divRef.current) return;
      map = new maplibregl.Map({
        container: divRef.current,
        style,
        center: [114.17, 22.32],
        zoom: 10.5,
        attributionControl: { compact: true },
      });
      const m = map;
      mapRef.current = m;
      m.addControl(new maplibregl.NavigationControl(), "top-right");
      m.on("error", (e) => {
        const msg = (e as { error?: { message?: string } }).error?.message
          || "unknown map error";
        setNote(`Map error (${mode}): ${msg}`);
      });
      m.on("load", () => {
        setNote(`Basemap "${mode}" loaded`);
        m.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        m.addLayer({
          id: "walk-lines", type: "line", source: "route",
          filter: ["==", ["get", "kind"], "walk"],
          paint: { "line-color": "#666", "line-width": 3, "line-dasharray": [1.5, 1.5] },
        });
        m.addLayer({
          id: "bus-lines", type: "line", source: "route",
          filter: ["==", ["get", "kind"], "bus"],
          paint: { "line-color": "#c8102e", "line-width": 4, "line-opacity": 0.85 },
        });
        m.addSource("points", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        m.addLayer({
          id: "stop-points", type: "circle", source: "points",
          paint: {
            "circle-radius": 5, "circle-color": "#fff",
            "circle-stroke-color": "#c8102e", "circle-stroke-width": 2,
          },
        });
        setReady(true);
      });
      m.on("click", (e) => {
        const { pickTarget, onPick } = propsRef.current;
        if (pickTarget) onPick(pickTarget, e.lngLat.lat, e.lngLat.lng);
      });
    });
    return () => {
      cancelled = true;
      if (map) { map.remove(); mapRef.current = null; }
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.getCanvas().style.cursor = pickTarget ? "crosshair" : "";
  }, [pickTarget]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const setMarker = (key: "from" | "to", ep: Endpoint | null, color: string) => {
      markers.current[key]?.remove();
      markers.current[key] = undefined;
      if (!ep) return;
      const m = new maplibregl.Marker({ color, draggable: true })
        .setLngLat([ep.lon, ep.lat])
        .setPopup(new maplibregl.Popup({ offset: 20 }).setText(ep.label))
        .addTo(map);
      m.on("dragend", () => {
        const ll = m.getLngLat();
        propsRef.current.onDrag(key, ll.lat, ll.lng);
      });
      markers.current[key] = m;
    };
    setMarker("from", from, "#1a9e54");
    setMarker("to", to, "#c8102e");
  }, [from, to, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const draw = () => {
      const src = map.getSource("route") as maplibregl.GeoJSONSource | undefined;
      const psrc = map.getSource("points") as maplibregl.GeoJSONSource | undefined;
      if (!src || !psrc) return;
      const features: Feature[] = [];
      const points: Feature[] = [];
      if (itinerary) {
        for (const leg of itinerary.legs) {
          if (leg.path && leg.path.length >= 2) {
            features.push({
              type: "Feature",
              properties: { kind: leg.type },
              geometry: { type: "LineString", coordinates: leg.path },
            });
          }
          if (leg.type === "bus") {
            for (const s of [leg.from_stop, leg.to_stop]) {
              points.push({
                type: "Feature", properties: {},
                geometry: { type: "Point", coordinates: [s.lon, s.lat] },
              });
            }
          }
        }
      }
      src.setData({ type: "FeatureCollection", features });
      psrc.setData({ type: "FeatureCollection", features: points });
      const all: [number, number][] = [];
      features.forEach((f) => {
        if (f.geometry.type === "LineString") all.push(...(f.geometry.coordinates as [number, number][]));
      });
      if (from) all.push([from.lon, from.lat]);
      if (to) all.push([to.lon, to.lat]);
      if (all.length >= 2) {
        const b = new maplibregl.LngLatBounds(all[0], all[0]);
        all.forEach((c) => b.extend(c));
        map.fitBounds(b, { padding: 50, maxZoom: 15, duration: 600 });
      }
    };
    draw();
  }, [itinerary, from, to, ready]);

  return (
    <div ref={divRef} className="map">
      {note && (
        <div style={{
          position: "absolute", left: 8, top: 8, zIndex: 5,
          background: "rgba(255,255,255,.92)", padding: "2px 8px",
          borderRadius: 4, fontSize: 12, pointerEvents: "none",
          maxWidth: "80%",
        }}>{note}</div>
      )}
    </div>
  );
}

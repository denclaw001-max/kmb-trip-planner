import { useEffect, useRef } from "react";
import * as maplibregl from "maplibre-gl";
import type { Feature } from "geojson";
import "maplibre-gl/dist/maplibre-gl.css";

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

  useEffect(() => {
    if (!divRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: divRef.current,
      style: STYLE_URL,
      center: [114.17, 22.32],
      zoom: 10.5,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.on("load", () => {
      map.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "walk-lines", type: "line", source: "route",
        filter: ["==", ["get", "kind"], "walk"],
        paint: { "line-color": "#666", "line-width": 3, "line-dasharray": [1.5, 1.5] },
      });
      map.addLayer({
        id: "bus-lines", type: "line", source: "route",
        filter: ["==", ["get", "kind"], "bus"],
        paint: { "line-color": "#c8102e", "line-width": 4, "line-opacity": 0.85 },
      });
      map.addSource("points", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "stop-points", type: "circle", source: "points",
        paint: {
          "circle-radius": 5, "circle-color": "#fff",
          "circle-stroke-color": "#c8102e", "circle-stroke-width": 2,
        },
      });
    });
    map.on("click", (e) => {
      const { pickTarget, onPick } = propsRef.current;
      if (pickTarget) onPick(pickTarget, e.lngLat.lat, e.lngLat.lng);
    });
    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
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
  }, [from, to]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
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
    if (map.isStyleLoaded()) draw();
    else map.once("load", draw);
  }, [itinerary, from, to]);

  return <div ref={divRef} className="map" />;
}

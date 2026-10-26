import { useEffect, useRef, useState } from "react";

// Lightweight slippy map: raster tiles as <img>, route lines as SVG,
// markers as plain DOM elements.
//
// This replaced a MapLibre GL implementation (2026-10-26) after a long
// blank-map investigation: MapLibre's WebGL canvas verifiably painted
// the map into its drawing buffer (in-app probe: 96-99% of pixels
// painted) yet never presented it on screen — on the user's Edge/PC,
// phone, and in the test browser — while its controls also vanished.
// Raster tiles + SVG present through the ordinary image/DOM pipeline
// and work in every browser. Tiles: OpenStreetMap (attribution shown
// on the map and in the app badge).

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

interface Props {
  from: Endpoint | null;
  to: Endpoint | null;
  itinerary: Itinerary | null;
  pickTarget: "from" | "to" | null;
  onPick: (target: "from" | "to", lat: number, lon: number) => void;
  onDrag: (target: "from" | "to", lat: number, lon: number) => void;
}

const TILE = 256;
const MIN_Z = 10;
const MAX_Z = 18;

function project(lat: number, lon: number, z: number) {
  const s = TILE * 2 ** z;
  const x = ((lon + 180) / 360) * s;
  const sin = Math.sin((lat * Math.PI) / 180);
  const y = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s;
  return { x, y };
}

function unproject(x: number, y: number, z: number) {
  const s = TILE * 2 ** z;
  const lon = (x / s) * 360 - 180;
  const n = Math.PI - (2 * Math.PI * y) / s;
  const lat = (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  return { lat, lon };
}

interface View { cx: number; cy: number; z: number }

export default function MapView({ from, to, itinerary, pickTarget, onPick, onDrag }: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const c0 = project(22.32, 114.17, 11);
  const [view, setView] = useState<View>({ cx: c0.x, cy: c0.y, z: 11 });
  const propsRef = useRef({ pickTarget, onPick, onDrag });
  propsRef.current = { pickTarget, onPick, onDrag };
  const panRef = useRef<{ sx: number; sy: number; cx: number; cy: number; moved: boolean } | null>(null);
  const dragRef = useRef<"from" | "to" | null>(null);
  const fitSigRef = useRef<string>("");

  // ------------------------------------------------------------ sizing
  useEffect(() => {
    const el = divRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // ------------------------------------------------------- wheel zoom
  useEffect(() => {
    const el = divRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      setView((v) => zoomAt(v, px, py, rect.width, rect.height,
                             e.deltaY < 0 ? 1 : -1));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  function zoomAt(v: View, px: number, py: number, w: number, h: number, dz: number): View {
    const z = Math.min(MAX_Z, Math.max(MIN_Z, v.z + dz));
    if (z === v.z) return v;
    const wx = v.cx + (px - w / 2);
    const wy = v.cy + (py - h / 2);
    const ll = unproject(wx, wy, v.z);
    const np = project(ll.lat, ll.lon, z);
    return { cx: np.x - (px - w / 2), cy: np.y - (py - h / 2), z };
  }

  // ------------------------------------------------------------ fit
  useEffect(() => {
    if (!size.w || dragRef.current) return;
    const pts: { lat: number; lon: number }[] = [];
    if (from) pts.push(from);
    if (to) pts.push(to);
    if (itinerary) {
      for (const leg of itinerary.legs) {
        for (const [lon, lat] of leg.path || []) pts.push({ lat, lon });
      }
    }
    if (!pts.length) return;
    const sig = JSON.stringify(pts.map((p) => [p.lat.toFixed(5), p.lon.toFixed(5)]));
    if (sig === fitSigRef.current) return;
    fitSigRef.current = sig;
    if (pts.length === 1) {
      const c = project(pts[0].lat, pts[0].lon, 15);
      setView({ cx: c.x, cy: c.y, z: 15 });
      return;
    }
    const w0 = pts.map((p) => project(p.lat, p.lon, 0));
    const xs = w0.map((p) => p.x);
    const ys = w0.map((p) => p.y);
    const dx = Math.max(...xs) - Math.min(...xs);
    const dy = Math.max(...ys) - Math.min(...ys);
    const zx = Math.log2((size.w - 90) / Math.max(dx, 1e-9));
    const zy = Math.log2((size.h - 90) / Math.max(dy, 1e-9));
    const z = Math.min(MAX_Z, Math.max(MIN_Z, Math.floor(Math.min(zx, zy))));
    const mid = unproject((Math.min(...xs) + Math.max(...xs)) / 2,
                          (Math.min(...ys) + Math.max(...ys)) / 2, 0);
    const c = project(mid.lat, mid.lon, z);
    setView({ cx: c.x, cy: c.y, z });
  }, [from, to, itinerary, size]);

  // ------------------------------------------------------ interaction
  const toWorld = (px: number, py: number) => ({
    x: view.cx + (px - size.w / 2),
    y: view.cy + (py - size.h / 2),
  });

  const onPointerDown = (e: React.PointerEvent) => {
    if (dragRef.current) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    panRef.current = { sx: e.clientX, sy: e.clientY, cx: view.cx, cy: view.cy, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const el = divRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (dragRef.current) {
      const w = toWorld(e.clientX - rect.left, e.clientY - rect.top);
      const ll = unproject(w.x, w.y, view.z);
      propsRef.current.onDrag(dragRef.current, ll.lat, ll.lon);
      return;
    }
    const p = panRef.current;
    if (!p) return;
    const dx = e.clientX - p.sx;
    const dy = e.clientY - p.sy;
    if (Math.abs(dx) + Math.abs(dy) > 5) p.moved = true;
    if (p.moved) setView((v) => ({ ...v, cx: p.cx - dx, cy: p.cy - dy }));
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const wasDrag = dragRef.current;
    dragRef.current = null;
    const p = panRef.current;
    panRef.current = null;
    if (wasDrag || !p || p.moved) return;
    const { pickTarget, onPick } = propsRef.current;
    if (!pickTarget || !divRef.current) return;
    const rect = divRef.current.getBoundingClientRect();
    const w = toWorld(e.clientX - rect.left, e.clientY - rect.top);
    const ll = unproject(w.x, w.y, view.z);
    onPick(pickTarget, ll.lat, ll.lon);
  };

  // ------------------------------------------------------------ tiles
  const tiles: React.ReactNode[] = [];
  if (size.w > 0) {
    const x0 = view.cx - size.w / 2;
    const y0 = view.cy - size.h / 2;
    const tx0 = Math.floor(x0 / TILE);
    const ty0 = Math.floor(y0 / TILE);
    const tx1 = Math.floor((x0 + size.w) / TILE);
    const ty1 = Math.floor((y0 + size.h) / TILE);
    const max = 2 ** view.z;
    for (let tx = tx0; tx <= tx1; tx++) {
      for (let ty = ty0; ty <= ty1; ty++) {
        if (tx < 0 || ty < 0 || tx >= max || ty >= max) continue;
        tiles.push(
          <img key={`${view.z}/${tx}/${ty}`}
            src={`https://tile.openstreetmap.org/${view.z}/${tx}/${ty}.png`}
            alt="" draggable={false}
            style={{
              position: "absolute", left: tx * TILE - x0, top: ty * TILE - y0,
              width: TILE, height: TILE, userSelect: "none", pointerEvents: "none",
            }} />,
        );
      }
    }
  }

  // ---------------------------------------------------------- overlay
  const toScreen = (lat: number, lon: number) => {
    const w = project(lat, lon, view.z);
    return { x: w.x - (view.cx - size.w / 2), y: w.y - (view.cy - size.h / 2) };
  };
  const lines: React.ReactNode[] = [];
  const dots: React.ReactNode[] = [];
  if (itinerary && size.w > 0) {
    itinerary.legs.forEach((leg, i) => {
      if (!leg.path || leg.path.length < 2) return;
      const pts = leg.path.map(([lon, lat]) => {
        const s = toScreen(lat, lon);
        return `${s.x.toFixed(1)},${s.y.toFixed(1)}`;
      }).join(" ");
      lines.push(
        <polyline key={`l${i}`} points={pts} fill="none"
          stroke={leg.type === "bus" ? "#c8102e" : "#666"}
          strokeWidth={leg.type === "bus" ? 4 : 3}
          strokeOpacity={leg.type === "bus" ? 0.85 : 1}
          strokeDasharray={leg.type === "bus" ? undefined : "1 7"}
          strokeLinecap="round" strokeLinejoin="round" />,
      );
      if (leg.type === "bus") {
        for (const s of [leg.from_stop, leg.to_stop]) {
          const p = toScreen(s.lat, s.lon);
          dots.push(
            <circle key={`d${i}-${s.id}`} cx={p.x} cy={p.y} r={5}
              fill="#fff" stroke="#c8102e" strokeWidth={2} />,
          );
        }
      }
    });
  }

  const renderMarker = (ep: Endpoint | null, kind: "from" | "to") => {
    if (!ep || size.w === 0) return null;
    const p = toScreen(ep.lat, ep.lon);
    const color = kind === "from" ? "#1a9e54" : "#c8102e";
    return (
      <div key={kind}
        style={{
          position: "absolute", left: p.x, top: p.y, zIndex: 6,
          transform: "translate(-50%, -100%)", textAlign: "center",
          cursor: "grab", touchAction: "none",
        }}
        onPointerDown={(e) => {
          e.stopPropagation();
          dragRef.current = kind;
          (e.target as Element).setPointerCapture?.(e.pointerId);
        }}
        onPointerMove={onPointerMove}
        onPointerUp={() => { dragRef.current = null; }}
        title={ep.label}>
        <div style={{
          width: 20, height: 20, margin: "0 auto", borderRadius: "50% 50% 50% 0",
          background: color, border: "2px solid #fff",
          transform: "rotate(-45deg)", boxShadow: "0 1px 4px rgba(0,0,0,.4)",
        }} />
        <div style={{
          marginTop: 3, display: "inline-block", maxWidth: 150,
          background: "rgba(255,255,255,.92)", borderRadius: 4, padding: "1px 6px",
          fontSize: 11, whiteSpace: "nowrap", overflow: "hidden",
          textOverflow: "ellipsis", boxShadow: "0 1px 3px rgba(0,0,0,.25)",
        }}>{ep.label}</div>
      </div>
    );
  };

  return (
    <div ref={divRef} className="map"
      style={{
        overflow: "hidden", background: "#e9eef1", touchAction: "none",
        cursor: pickTarget ? "crosshair" : "grab",
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onDoubleClick={(e) => {
        if (!divRef.current) return;
        const rect = divRef.current.getBoundingClientRect();
        setView((v) => zoomAt(v, e.clientX - rect.left, e.clientY - rect.top,
                               rect.width, rect.height, 1));
      }}>
      {tiles}
      <svg style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
        width={size.w} height={size.h}>
        {lines}
        {dots}
      </svg>
      {renderMarker(from, "from")}
      {renderMarker(to, "to")}
      <div style={{
        position: "absolute", right: 8, top: 8, zIndex: 7, display: "flex",
        flexDirection: "column", boxShadow: "0 1px 4px rgba(0,0,0,.3)",
        borderRadius: 4, overflow: "hidden",
      }}>
        <button type="button" aria-label="Zoom in"
          onClick={() => setView((v) => zoomAt(v, size.w / 2, size.h / 2, size.w, size.h, 1))}
          style={{ width: 30, height: 30, fontSize: 18, border: "none", background: "#fff", cursor: "pointer" }}>+</button>
        <button type="button" aria-label="Zoom out"
          onClick={() => setView((v) => zoomAt(v, size.w / 2, size.h / 2, size.w, size.h, -1))}
          style={{ width: 30, height: 30, fontSize: 18, border: "none", background: "#fff", cursor: "pointer", borderTop: "1px solid #ddd" }}>−</button>
      </div>
      <div style={{
        position: "absolute", right: 6, bottom: 4, zIndex: 5, fontSize: 11,
        color: "#555", background: "rgba(255,255,255,.75)", padding: "0 5px",
        borderRadius: 3, pointerEvents: "none",
      }}>
        © <a href="https://www.openstreetmap.org/copyright" target="_blank"
          rel="noreferrer" style={{ color: "#555", pointerEvents: "auto" }}>OpenStreetMap</a> contributors
      </div>
    </div>
  );
}

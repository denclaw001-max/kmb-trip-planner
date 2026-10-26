import { useCallback, useEffect, useRef, useState } from "react";
import MapView, { type BusLeg, type Endpoint, type Itinerary } from "./MapView";
import { t, type Lang } from "./i18n";
import "./App.css";

interface SearchResult {
  kind: "stop" | "place"; stop_id?: string;
  label_tc: string; label_en: string; lat: number; lon: number;
}
interface Fallback {
  nearest_stop: { id: string; name_tc: string; name_en: string; lat: number; lon: number };
  remaining_meters: number;
  deep_link: { google_maps: string; apple_maps: string };
}
interface Hint { mode: "mtr" | "ferry"; name_tc: string; meters: number }
interface PlanResult {
  status: "full" | "partial" | "none"; reason: string | null;
  itineraries: Itinerary[]; fallback: Fallback | null;
  transport_hints?: { origin: Hint[]; dest: Hint[] };
  meta: { engine_ms: number; from: { lat: number; lon: number }; to: { lat: number; lon: number } };
}

function HintLine({ label, hints, lang }: { label: string; hints: Hint[]; lang: Lang }) {
  if (!hints.length) return null;
  return (
    <div className="hint-line">
      <span className="hint-label">{label}：</span>
      {hints.map((h, i) => (
        <span key={i} className="hint-chip">
          {h.mode === "mtr"
            ? `🚇 ${t(lang, "mtr")} ${t(lang, "stationAt", { name: h.name_tc })}`
            : `⛴ ${t(lang, "ferry")} ${t(lang, "pierAt", { name: h.name_tc })}`}
          （{t(lang, "aboutM", { m: h.meters })}）
        </span>
      ))}
    </div>
  );
}
interface EtaItem { eta: string | null; remark_tc: string; remark_en: string; dest_tc: string; dest_en: string }

const stopName = (lang: Lang, s: { name_tc: string; name_en: string }) =>
  lang === "zh" ? s.name_tc : s.name_en;

/* ---------------- PlaceInput ---------------- */
function PlaceInput(props: {
  lang: Lang; placeholder: string; value: Endpoint | null;
  onChange: (ep: Endpoint | null) => void; onPickRequest: () => void; picking: boolean;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState<SearchResult[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => { setText(props.value?.label ?? ""); }, [props.value]);

  const query = (q: string) => {
    setText(q);
    props.onChange(null);
    clearTimeout(timer.current);
    if (q.trim().length < 1) { setResults([]); setOpen(false); return; }
    timer.current = setTimeout(async () => {
      try {
        const r = await fetch(`/api/search?q=${encodeURIComponent(q.trim())}`);
        const d = await r.json();
        setResults(d.results ?? []);
        setOpen(true);
      } catch { /* offline */ }
    }, 300);
  };

  return (
    <div className="place-input">
      <input
        value={text} placeholder={props.placeholder}
        onChange={(e) => query(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
      />
      <button type="button" className={props.picking ? "pick active" : "pick"}
        title={t(props.lang, "pickOnMap")} onClick={props.onPickRequest}>📍</button>
      {open && results.length > 0 && (
        <ul className="dropdown">
          {results.map((r, i) => (
            <li key={i} onClick={() => {
              props.onChange({
                label: props.lang === "zh" ? r.label_tc : r.label_en,
                lat: r.lat, lon: r.lon,
              });
              setOpen(false);
            }}>
              <span className={r.kind === "stop" ? "badge stop" : "badge place"}>
                {t(props.lang, r.kind === "stop" ? "stopBadge" : "placeBadge")}
              </span>
              {props.lang === "zh" ? r.label_tc : r.label_en}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ---------------- EtaBox ---------------- */
function EtaBox({ leg, lang }: { leg: BusLeg; lang: Lang }) {
  const [etas, setEtas] = useState<EtaItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  const st = leg.line_key.split("|")[2] ?? "1";

  const load = useCallback(async () => {
    try {
      const u = `/api/eta?stop_id=${leg.from_stop.id}&route=${encodeURIComponent(leg.route)}` +
        `&service_type=${st}&dest=${encodeURIComponent(leg.dest_tc)}`;
      const r = await fetch(u);
      if (!r.ok) throw new Error();
      const d = await r.json();
      setEtas(d.etas ?? []);
      setFailed(false);
    } catch { setFailed(true); }
  }, [leg, st]);

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);

  const fmt = (iso: string) => {
    const d = new Date(iso);
    const mins = Math.round((d.getTime() - Date.now()) / 60000);
    const hhmm = d.toLocaleTimeString(lang === "zh" ? "zh-HK" : "en-GB",
      { hour: "2-digit", minute: "2-digit", hour12: false });
    const rel = mins <= 1 ? t(lang, "arriving") : t(lang, "minsLater", { n: mins });
    return `${hhmm}（${rel}）`;
  };

  return (
    <div className="eta-box">
      <div className="eta-head">
        <span>🚌 {t(lang, "etaTitle")} · {leg.route}</span>
        <button type="button" onClick={load}>{t(lang, "etaRefresh")}</button>
      </div>
      {failed || (etas && etas.length === 0) ? (
        <div className="eta-none">{t(lang, "etaNone")}</div>
      ) : !etas ? (
        <div className="eta-none">…</div>
      ) : (
        <ul className="eta-list">
          {etas.slice(0, 3).map((e, i) => (
            <li key={i}>
              {e.eta ? fmt(e.eta)
                : <span className="eta-remark">
                    {(lang === "zh" ? e.remark_tc : e.remark_en) || t(lang, "etaScheduled")}
                  </span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ---------------- ResultCard ---------------- */
function ResultCard({ itin, idx, lang, selected, onSelect }: {
  itin: Itinerary; idx: number; lang: Lang; selected: boolean;
  onSelect: () => void;
}) {
  const firstBus = itin.legs.find((l): l is BusLeg => l.type === "bus");
  return (
    <div className={selected ? "card selected" : "card"} onClick={onSelect}>
      <div className="card-head">
        <strong>{t(lang, "plan")} {idx + 1}</strong>
        <span>{itin.transfers === 0 ? t(lang, "direct") : `${itin.transfers} ${t(lang, "transfers")}`}</span>
        <span>{t(lang, "estMinutes", { n: Math.round(itin.total_minutes) })}（{t(lang, "estimated")}）</span>
        <span>{t(lang, "walk", { m: itin.walk_meters })}</span>
      </div>
      {selected && (
        <div className="card-body">
          {firstBus && <EtaBox leg={firstBus} lang={lang} />}
          <ol className="steps">
            {itin.legs.map((leg, i) => leg.type === "walk" ? (
              <li key={i} className="step walk">
                🚶 {t(lang, "walk", { m: leg.meters })}
                {leg.from_stop && <> · {stopName(lang, leg.from_stop)}</>}
                {leg.to_stop && <> → {stopName(lang, leg.to_stop)}</>}
              </li>
            ) : (
              <li key={i} className="step bus">
                <span className="route-badge">{leg.route}</span>
                {t(lang, "towards")} {lang === "zh" ? leg.dest_tc : leg.dest_en}
                <div className="step-sub">
                  {stopName(lang, leg.from_stop)} {t(lang, "board")} →{" "}
                  {stopName(lang, leg.to_stop)} {t(lang, "alight")} ·{" "}
                  {t(lang, "stopsRide", { n: leg.stops })} · {t(lang, "estMinutes", { n: Math.round(leg.minutes) })}
                </div>
              </li>
            ))}
          </ol>
          <div className="walk-note">{t(lang, "walkLine")}</div>
        </div>
      )}
    </div>
  );
}

/* ---------------- App ---------------- */
export default function App() {
  const [lang, setLang] = useState<Lang>("zh");
  const [from, setFrom] = useState<Endpoint | null>(null);
  const [to, setTo] = useState<Endpoint | null>(null);
  const [maxTransfers, setMaxTransfers] = useState(2);
  const [lwb, setLwb] = useState(true);
  const [pickTarget, setPickTarget] = useState<"from" | "to" | null>(null);
  const [result, setResult] = useState<PlanResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sel, setSel] = useState(0);
  const [copied, setCopied] = useState(false);

  const doPlan = useCallback(async (
    f: Endpoint, tt: Endpoint, mt: number, useLwb: boolean,
  ) => {
    setLoading(true); setError("");
    try {
      const u = `/api/plan?flat=${f.lat}&flon=${f.lon}&tlat=${tt.lat}&tlon=${tt.lon}` +
        `&max_transfers=${mt}&lwb=${useLwb}`;
      const r = await fetch(u);
      if (!r.ok) throw new Error();
      const d: PlanResult = await r.json();
      setResult(d); setSel(0);
      const p = new URLSearchParams({
        flat: String(f.lat), flon: String(f.lon), tlat: String(tt.lat), tlon: String(tt.lon),
        fl: f.label, tl: tt.label, mt: String(mt), lwb: useLwb ? "1" : "0",
      });
      history.replaceState(null, "", `?${p}`);
    } catch { setError("⚠"); }
    setLoading(false);
  }, []);

  useEffect(() => {
    const p = new URLSearchParams(location.search);
    const flat = parseFloat(p.get("flat") ?? ""), flon = parseFloat(p.get("flon") ?? "");
    const tlat = parseFloat(p.get("tlat") ?? ""), tlon = parseFloat(p.get("tlon") ?? "");
    if ([flat, flon, tlat, tlon].every((v) => !Number.isNaN(v))) {
      const f = { label: p.get("fl") ?? "", lat: flat, lon: flon };
      const tt = { label: p.get("tl") ?? "", lat: tlat, lon: tlon };
      const mt = parseInt(p.get("mt") ?? "2"), useLwb = p.get("lwb") !== "0";
      setFrom(f); setTo(tt); setMaxTransfers(mt); setLwb(useLwb);
      doPlan(f, tt, mt, useLwb);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onPick = (target: "from" | "to", lat: number, lon: number) => {
    const ep = { label: `📍 ${lat.toFixed(5)}, ${lon.toFixed(5)}`, lat, lon };
    if (target === "from") setFrom(ep); else setTo(ep);
    setPickTarget(null);
  };
  const onDrag = (target: "from" | "to", lat: number, lon: number) => {
    const ep = { label: `📍 ${lat.toFixed(5)}, ${lon.toFixed(5)}`, lat, lon };
    if (target === "from") setFrom(ep); else setTo(ep);
  };
  const useMyLocation = () => {
    navigator.geolocation?.getCurrentPosition((pos) => {
      setFrom({ label: `📍 ${t(lang, "myLocation")}`, lat: pos.coords.latitude, lon: pos.coords.longitude });
    });
  };
  const share = async () => {
    await navigator.clipboard.writeText(location.href).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const selectedItin = result?.itineraries[sel] ?? result?.itineraries[0] ?? null;
  const gmapsFull = from && to
    ? `https://www.google.com/maps/dir/?api=1&origin=${from.lat},${from.lon}&destination=${to.lat},${to.lon}&travelmode=transit`
    : "#";

  return (
    <div className="app">
      <header>
        <h1>🚌 {t(lang, "title")}</h1>
        <div className="lang-toggle">
          <button className={lang === "zh" ? "on" : ""} onClick={() => setLang("zh")}>繁中</button>
          <button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>EN</button>
        </div>
      </header>
      <div className="layout">
        <div className="panel">
          <div className="field">
            <label>{t(lang, "from")}</label>
            <PlaceInput lang={lang} placeholder={t(lang, "fromPh")} value={from}
              onChange={setFrom} picking={pickTarget === "from"}
              onPickRequest={() => setPickTarget(pickTarget === "from" ? null : "from")} />
            <button type="button" className="mini" onClick={useMyLocation}>📍 {t(lang, "myLocation")}</button>
          </div>
          <button type="button" className="swap" onClick={() => { setFrom(to); setTo(from); }}>
            ⇅ {t(lang, "swap")}
          </button>
          <div className="field">
            <label>{t(lang, "to")}</label>
            <PlaceInput lang={lang} placeholder={t(lang, "toPh")} value={to}
              onChange={setTo} picking={pickTarget === "to"}
              onPickRequest={() => setPickTarget(pickTarget === "to" ? null : "to")} />
          </div>
          {pickTarget && <div className="picking-note">{t(lang, "picking")}</div>}
          <div className="options">
            <label>{t(lang, "maxTransfers")}
              <select value={maxTransfers} onChange={(e) => setMaxTransfers(Number(e.target.value))}>
                {[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n} {t(lang, "times")}</option>)}
              </select>
            </label>
            <label className="check">
              <input type="checkbox" checked={lwb} onChange={(e) => setLwb(e.target.checked)} />
              {t(lang, "includeLwb")}
            </label>
          </div>
          <button type="button" className="search-btn" disabled={loading}
            onClick={() => {
              if (!from || !to) { setError(t(lang, "needBoth")); return; }
              doPlan(from, to, maxTransfers, lwb);
            }}>
            {loading ? t(lang, "searching") : `🔍 ${t(lang, "search")}`}
          </button>
          {error && <div className="error">{error}</div>}

          <div className="results">
            {result?.transport_hints && (
              <>
                <HintLine label={t(lang, "nearOrigin")} hints={result.transport_hints.origin} lang={lang} />
                <HintLine label={t(lang, "nearDest")} hints={result.transport_hints.dest} lang={lang} />
              </>
            )}
            {result?.status === "partial" && result.fallback && (
              <div className="notice partial">
                <strong>{t(lang, "partialTitle")}</strong>
                <p>{t(lang, "partialBody", {
                  stop: stopName(lang, result.fallback.nearest_stop),
                  m: result.fallback.remaining_meters,
                })}</p>
                <a className="dl" href={result.fallback.deep_link.google_maps} target="_blank" rel="noreferrer">{t(lang, "openGoogle")}</a>
                <a className="dl" href={result.fallback.deep_link.apple_maps} target="_blank" rel="noreferrer">{t(lang, "openApple")}</a>
              </div>
            )}
            {result?.status === "none" && (
              <div className="notice none">
                <p>{result.reason === "origin_no_bus_stop" ? t(lang, "noneOrigin") : t(lang, "noneGeneric")}</p>
                <a className="dl" href={gmapsFull} target="_blank" rel="noreferrer">{t(lang, "openGoogleRoute")}</a>
              </div>
            )}
            {result?.itineraries.map((itin, i) => (
              <ResultCard key={i} itin={itin} idx={i} lang={lang}
                selected={i === sel} onSelect={() => setSel(i)} />
            ))}
            {result && result.status === "full" && (
              <button type="button" className="share-btn" onClick={share}>
                🔗 {copied ? t(lang, "shared") : t(lang, "share")}
              </button>
            )}
            <p className="fineprint">{t(lang, "schematic")}</p>
            <p className="fineprint">{t(lang, "dataNote")}</p>
          </div>
        </div>
        <div className="map-wrap">
          <MapView from={from} to={to} itinerary={selectedItin}
            pickTarget={pickTarget} onPick={onPick} onDrag={onDrag} />
          <div className="map-badge">{t(lang, "mapData")}</div>
        </div>
      </div>
    </div>
  );
}

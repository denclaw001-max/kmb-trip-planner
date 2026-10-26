"""Phase 2 backend: FastAPI wrapper around the Phase 1 routing core.

Endpoints:
  GET /api/health  — keep-warm target + data status
  GET /api/search  — Tier-1 KMB stop search + Tier-2 Photon places (proxied)
  GET /api/plan    — routing (wraps router.Router.plan)
  GET /api/eta     — first-leg live ETA proxy (KMB eta endpoint)
Static frontend (web/dist) is served at / when built.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .geo import haversine_m
from .graph import load_graph
from .router import Router
from .service_time import all_flag_patterns, service_context

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(BASE, "data", "bundle.json")
DIST = os.path.join(BASE, "web", "dist")
HKT = ZoneInfo("Asia/Hong_Kong")

app = FastAPI(title="KMB Trip Planner")
_graph = load_graph(BUNDLE)
_router = Router(_graph)
_FLAG_PATTERNS = all_flag_patterns(_graph)
_photon_cache = {}

# FR-4 other-transport hints (Phase 0 tiered plan): MTR heavy-rail
# stations (curated static list, Photon-verified coords) + ferry piers
# (extracted from TD GTFS during sync). Light Rail is not yet included.
def _load_json(name):
    path = os.path.join(BASE, "data", name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []

def _clean_ferry_name(name):
    """GTFS ferry stop names are '<street><pier name>'; keep the pier
    part for display (e.g. 民光街中環五號碼頭 -> 中環五號碼頭)."""
    cut = -1
    for token in ("海傍街", "街", "道", "路", "徑", "里"):
        idx = name.rfind(token)
        if idx >= 0:
            cut = max(cut, idx + len(token))
    cleaned = name[cut:] if 0 <= cut <= len(name) - 3 else name
    return cleaned


_TRANSPORT_POINTS = (
    [("mtr", s["name_tc"], s["lat"], s["lon"]) for s in _load_json("mtr_stations.json")]
    + [("ferry", _clean_ferry_name(s["name_tc"]), s["lat"], s["lon"])
       for s in _load_json("ferry_stops.json")]
)


def _transport_hints(lat, lon, radius_m=600):
    best = {}
    for mode, name, slat, slon in _TRANSPORT_POINTS:
        d = haversine_m(lat, lon, slat, slon)
        if d <= radius_m and (mode not in best or d < best[mode]["meters"]):
            best[mode] = {"mode": mode, "name_tc": name, "meters": round(d)}
    return sorted(best.values(), key=lambda h: h["meters"])


def _photon_search(q, limit=4):
    if q in _photon_cache:
        return _photon_cache[q]
    params = urllib.parse.urlencode({
        "q": q, "limit": limit, "bbox": "113.75,22.05,114.55,22.65",
        "lat": 22.30, "lon": 114.18, "location_bias_scale": 0.2})
    req = urllib.request.Request(
        f"https://photon.komoot.io/api/?{params}",
        headers={"User-Agent": "kmb-trip-planner/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
    except Exception:
        return []
    out = []
    for f in data.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        p = f.get("properties", {})
        name = p.get("name") or ""
        if not name:
            continue
        extra = p.get("district") or p.get("city") or p.get("locality") or ""
        label = f"{name}（{extra}）" if extra and extra != name else name
        out.append({"kind": "place", "label_tc": label, "label_en": label,
                    "lat": lat, "lon": lon})
    if len(_photon_cache) > 300:
        _photon_cache.clear()
    _photon_cache[q] = out
    return out


@app.get("/api/health")
def health():
    mtime = datetime.fromtimestamp(os.path.getmtime(BUNDLE), timezone.utc)
    return {"status": "ok", "stops": len(_graph.stops),
            "lines": len(_graph.lines),
            "lines_with_windows": _graph.stats.get("lines_with_service_windows", 0),
            "data_updated": mtime.isoformat()}


@app.get("/api/search")
def search(q: str = Query(min_length=1, max_length=60)):
    ql = q.strip().lower()
    scored = []
    for sid, s in _graph.stops.items():
        tc, en = s["name_tc"], s["name_en"]
        if ql in tc.lower() or ql in en.lower():
            starts = tc.lower().startswith(ql) or en.lower().startswith(ql)
            scored.append((0 if starts else 1, len(tc), sid, s))
    scored.sort(key=lambda x: (x[0], x[1]))
    results = [{"kind": "stop", "stop_id": sid,
                "label_tc": s["name_tc"], "label_en": s["name_en"],
                "lat": s["lat"], "lon": s["lon"]}
               for _, _, sid, s in scored[:6]]
    if len(q.strip()) >= 2:
        results += _photon_search(q.strip())
    return {"results": results}


_DEPART_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _depart_context(depart: str):
    """Resolve (router depart arg, meta info). Default is 'now' (HKT).
    An optional depart=HH:MM pins the departure clock for today, which
    also powers testing of the Phase 4 service-window behaviour."""
    now = datetime.now(HKT)
    m = _DEPART_RE.match(depart or "")
    if m:
        now = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                          second=0, microsecond=0)
    clock, patterns, service_date = service_context(
        now, _FLAG_PATTERNS, _graph.service_exceptions)
    info = {"depart_hkt": now.strftime("%Y-%m-%d %H:%M"),
            "service_day": service_date.isoformat(),
            "clock_min": round(clock)}
    return (clock, patterns), info


@app.get("/api/plan")
def plan(flat: float, flon: float, tlat: float, tlon: float,
         max_transfers: int = Query(2, ge=0, le=4), lwb: bool = True,
         depart: str = ""):
    t0 = time.perf_counter()
    depart_arg, depart_info = _depart_context(depart)
    result = _router.plan((flat, flon), (tlat, tlon),
                          max_transfers=max_transfers, include_lwb=lwb,
                          depart=depart_arg)
    _enrich_paths(result, (flat, flon), (tlat, tlon))
    result["transport_hints"] = {
        "origin": _transport_hints(flat, flon),
        "dest": _transport_hints(tlat, tlon),
    }
    result["meta"] = {"engine_ms": round((time.perf_counter() - t0) * 1000),
                      "from": {"lat": flat, "lon": flon},
                      "to": {"lat": tlat, "lon": tlon},
                      "depart": depart_info}
    return result


def _enrich_paths(result, origin, dest):
    """Attach map geometry to each leg: bus legs get the stop-by-stop
    coordinate path (schematic straight lines between stops, per PRD);
    walk legs get their two endpoints."""
    def coord(stop_obj, fallback):
        if stop_obj:
            s = _graph.stops[stop_obj["id"]]
            return [s["lon"], s["lat"]]
        return [fallback[1], fallback[0]]
    for itin in result.get("itineraries", []):
        for leg in itin["legs"]:
            if leg["type"] == "bus":
                line = _graph.lines.get(leg["line_key"])
                if not line:
                    leg["path"] = []
                    continue
                stops = line["stops"]
                bi = stops.index(leg["from_stop"]["id"])
                ai = stops.index(leg["to_stop"]["id"])
                leg["path"] = [[_graph.stops[s]["lon"], _graph.stops[s]["lat"]]
                               for s in stops[bi:ai + 1]]
            else:
                leg["path"] = [coord(leg.get("from_stop"), origin),
                               coord(leg.get("to_stop"), dest)]


@app.get("/api/eta")
def eta(stop_id: str, route: str, service_type: str = "1", dest: str = ""):
    url = (f"https://data.etabus.gov.hk/v1/transport/kmb/eta/"
           f"{stop_id}/{route}/{service_type}")
    req = urllib.request.Request(url, headers={"User-Agent": "kmb-trip-planner/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
    except Exception:
        return JSONResponse({"error": "eta_unavailable"}, status_code=502)
    items = []
    for d in data.get("data", []):
        items.append({"eta_seq": d.get("eta_seq"), "eta": d.get("eta"),
                      "remark_tc": d.get("remark_tc") or "",
                      "remark_en": d.get("remark_en") or "",
                      "dest_tc": d.get("dest_tc") or "",
                      "dest_en": d.get("dest_en") or ""})
    items.sort(key=lambda x: (x["eta_seq"] is None, x["eta_seq"]))
    if dest:
        matched = [i for i in items if i["dest_tc"] == dest]
        if matched:
            items = matched
    return {"etas": items[:5],
            "generated": data.get("generated_timestamp")}


if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

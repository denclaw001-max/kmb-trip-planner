#!/usr/bin/env python3
"""Phase 1 data sync: build the routing bundle from three official sources.

Sources (all verified in Phase 0):
  1. KMB open data API (data.etabus.gov.hk) — routes / stops / route-stops.
     Refreshed daily 05:00 by KMB; run this after 05:30 HKT.
  2. Transport Department GTFS (static.data.gov.hk/td/pt-headway-tc/gtfs.zip)
     — agency_id per route, used to build the Long Win (LWB) route-number set
       (the KMB ETA `co` field cannot distinguish operators — Phase 0 §2).
     — ferry stops (agency FERRY) extracted for later FR-4 use.
     — service windows per line (Phase 4): frequencies bands + calendar
       weekday patterns (+ calendar_dates exceptions), matched to KMB
       lines by route number and first-stop proximity; trips without
       frequency bands contribute their fixed first-stop departure.
  3. TD routes-and-fares GeoJSON (JSON_BUS.json, ~78MB) — per-route official
     journeyTime + fullFare. Streamed through jq, never loaded whole.

Output: data/bundle.json (+ data/ferry_stops.json, data/sync_report.json)
Bundle line journey times are attached later by graph.py matching rules;
here we store TD journey-time rows keyed for that matching.

Sanity checks (PRD Part 2 §3.6): if stop/route counts move >5% vs the
previous bundle, the new bundle is written to bundle.candidate.json and
the old bundle is kept, with the failure recorded in the sync report.
"""
import csv
import gzip
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
KMB = "https://data.etabus.gov.hk/v1/transport/kmb"
GTFS_URL = "https://static.data.gov.hk/td/pt-headway-tc/gtfs.zip"
GEOJSON_URL = "https://static.data.gov.hk/td/routes-fares-geojson/JSON_BUS.json"
UA = {"User-Agent": "kmb-trip-planner-sync/0.1 (Phase 1)"}


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)


def fetch_bytes(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_kmb():
    routes = fetch_json(f"{KMB}/route")["data"]
    stops = fetch_json(f"{KMB}/stop")["data"]
    route_stops = fetch_json(f"{KMB}/route-stop")["data"]
    return routes, stops, route_stops


def get_gtfs_extras(z):
    """Return (lwb_route_numbers, ferry_stops, gtfs_version_note)."""
    version = min(f.date_time for f in z.infolist())
    version_s = "%04d-%02d-%02d" % (version[0], version[1], version[2])

    lwb = set()
    ferry_route_ids = set()
    with z.open("routes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            if "LWB" in row["agency_id"]:
                lwb.add(row["route_short_name"])
            if row["agency_id"] == "FERRY":
                ferry_route_ids.add(row["route_id"])

    # ferry stop ids via trips + stop_times (streamed; stop_times is ~50MB)
    ferry_trip_ids = set()
    with z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            if row["route_id"] in ferry_route_ids:
                ferry_trip_ids.add(row["trip_id"])
    ferry_stop_ids = set()
    with z.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            if row["trip_id"] in ferry_trip_ids:
                ferry_stop_ids.add(row["stop_id"])
    ferry_stops = []
    with z.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            if row["stop_id"] in ferry_stop_ids:
                ferry_stops.append({
                    "stop_id": row["stop_id"], "name_tc": row["stop_name"],
                    "lat": float(row["stop_lat"]), "lon": float(row["stop_lon"]),
                })
    return sorted(lwb), ferry_stops, version_s


def _gtfs_minutes(t):
    """'07:12:00' -> 432.0. GTFS hours may be >= 24 (after-midnight)."""
    try:
        h, m, s = (t or "").split(":")
        return int(h) * 60 + int(m) + int(s) / 60.0
    except (ValueError, AttributeError):
        return None


def _hav_m(lat1, lon1, lat2, lon2):
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_service_windows(z, lines_out, stops_out):
    """Build per-line service windows from GTFS frequencies/calendar.

    Returns (windows, exceptions, stats):
      windows: {line_key: {flags "1111100" (Mon..Sun): bands}} where each
        band is [start_min, end_min, headway_min] of ORIGIN departures in
        GTFS convention (minutes may exceed 1440). Trips without a
        frequency band contribute their fixed first-stop departure as a
        [t, t, 0] band.
      exceptions: {"YYYYMMDD": {"add": [flags...], "remove": [flags...]}}
        — weekday-pattern flips derived from calendar_dates (e.g. public
        holidays running a Sunday pattern), computed exactly at pattern
        level from the service-level exceptions.
    Direction matching: a GTFS trip joins the KMB line (same route
    number, service_type 1) whose first stop is nearest the trip's own
    first stop (must be <= 600 m), which also keeps same-numbered
    routes of other operators apart.
    """
    import datetime as dt
    from collections import defaultdict

    gtfs_stops = {}
    with z.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            try:
                gtfs_stops[row["stop_id"]] = (float(row["stop_lat"]),
                                              float(row["stop_lon"]))
            except (KeyError, ValueError):
                pass

    bus_route_short = {}
    with z.open("routes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            ag = row["agency_id"] or ""
            if "KMB" in ag or "LWB" in ag:
                bus_route_short[row["route_id"]] = row["route_short_name"]

    svc_flags, svc_range = {}, {}
    with z.open("calendar.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            svc_flags[row["service_id"]] = "".join(
                row[d] for d in ("monday", "tuesday", "wednesday",
                                 "thursday", "friday", "saturday", "sunday"))
            svc_range[row["service_id"]] = (row["start_date"], row["end_date"])

    cal_dates = []
    try:
        with z.open("calendar_dates.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                cal_dates.append((row["date"], row["service_id"],
                                  row["exception_type"]))
    except KeyError:
        pass

    trip_route, trip_svc = {}, {}
    with z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            if row["route_id"] in bus_route_short:
                trip_route[row["trip_id"]] = bus_route_short[row["route_id"]]
                trip_svc[row["trip_id"]] = row["service_id"]

    trip_bands = {}
    with z.open("frequencies.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            tid = row["trip_id"]
            if tid not in trip_route:
                continue
            a = _gtfs_minutes(row["start_time"])
            b = _gtfs_minutes(row["end_time"])
            if a is None or b is None:
                continue
            try:
                h = float(row["headway_secs"]) / 60.0
            except (TypeError, ValueError):
                h = 0.0
            trip_bands.setdefault(tid, []).append([a, b, h])

    trip_first = {}   # trip_id -> (stop_sequence, stop_id, departure_min)
    with z.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
            tid = row["trip_id"]
            if tid not in trip_route:
                continue
            try:
                seq = int(row["stop_sequence"])
            except (TypeError, ValueError):
                continue
            cur = trip_first.get(tid)
            if cur is None or seq < cur[0]:
                dep = _gtfs_minutes(row.get("departure_time")
                                    or row.get("arrival_time"))
                trip_first[tid] = (seq, row["stop_id"], dep)

    cand = defaultdict(list)
    for key, line in lines_out.items():
        if line["service_type"] != "1" or len(line["stops"]) < 2:
            continue
        fs = stops_out[line["stops"][0]]
        cand[line["route"]].append((key, fs["lat"], fs["lon"]))

    agg = {}
    matched = unmatched = 0
    for tid, short in trip_route.items():
        first = trip_first.get(tid)
        lines_c = cand.get(short)
        if first is None or not lines_c:
            unmatched += 1
            continue
        gcoord = gtfs_stops.get(first[1])
        if gcoord is None:
            unmatched += 1
            continue
        best = None
        for key, la, lo in lines_c:
            d = _hav_m(gcoord[0], gcoord[1], la, lo)
            if best is None or d < best[0]:
                best = (d, key)
        if best is None or best[0] > 600:
            unmatched += 1
            continue
        matched += 1
        flags = svc_flags.get(trip_svc.get(tid))
        if not flags:
            continue
        bands = trip_bands.get(tid)
        if bands:
            agg.setdefault((best[1], flags), []).extend(bands)
        elif first[2] is not None:
            agg.setdefault((best[1], flags), []).append([first[2], first[2], 0.0])

    windows = {}
    for (key, flags), bands in agg.items():
        bands.sort()
        merged = []
        for a, b, h in bands:
            if merged and a <= merged[-1][1] + 1:
                if b > merged[-1][1]:
                    merged[-1][1] = b
                if h > 0:
                    merged[-1][2] = h if merged[-1][2] == 0 else min(merged[-1][2], h)
            else:
                merged.append([a, b, h])
        windows.setdefault(key, {})[flags] = [
            [round(a), round(b), round(h, 1)] for a, b, h in merged]

    exc_by_date = defaultdict(dict)
    for date_s, svc, etype in cal_dates:
        exc_by_date[date_s][svc] = etype
    exceptions = {}
    for date_s, changes in exc_by_date.items():
        try:
            wd = dt.date(int(date_s[:4]), int(date_s[4:6]),
                         int(date_s[6:8])).weekday()
        except ValueError:
            continue
        before, after = set(), set()
        for svc, fl in svc_flags.items():
            rng = svc_range[svc]
            if not (rng[0] <= date_s <= rng[1]):
                continue
            base = fl[wd] == "1"
            if base:
                before.add(fl)
            is_after = base if svc not in changes else changes[svc] == "1"
            if is_after:
                after.add(fl)
        add = sorted(after - before)
        remove = sorted(before - after)
        if add or remove:
            exceptions[date_s] = {"add": add, "remove": remove}

    stats = {"gtfs_bus_trips": len(trip_route),
             "trips_matched_to_lines": matched,
             "trips_unmatched": unmatched,
             "lines_with_windows": len(windows),
             "exception_dates": len(exceptions)}
    return windows, exceptions, stats


def get_td_journey():
    """Extract route-level rows from the 78MB GeoJSON with jq (streaming).

    One row per (routeId, routeSeq) variant: company, names, journeyTime,
    fullFare. Returns list of dicts.
    """
    tmp = os.path.join(DATA_DIR, "_json_bus.tmp.json")
    with open(tmp, "wb") as f:
        f.write(fetch_bytes(GEOJSON_URL, timeout=300))
    try:
        jq_expr = (
            ".features[] | [.properties.routeId, .properties.routeSeq, "
            ".properties.companyCode, .properties.routeNameC, "
            ".properties.locStartNameC, .properties.locEndNameC, "
            ".properties.journeyTime, .properties.fullFare] | @tsv"
        )
        out = subprocess.run(["jq", "-r", jq_expr, tmp],
                             capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise RuntimeError(f"jq failed: {out.stderr[:300]}")
    finally:
        os.remove(tmp)
    seen = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        rid, rseq, co, name, start, end, jt, fare = parts
        key = (rid, rseq)
        if key in seen:
            continue
        try:
            jt_v = float(jt) if jt not in ("", "null") else None
        except ValueError:
            jt_v = None
        try:
            fare_v = float(fare) if fare not in ("", "null") else None
        except ValueError:
            fare_v = None
        seen[key] = {"route_id": rid, "route_seq": rseq, "company": co,
                     "route": name, "start_tc": start, "end_tc": end,
                     "journey_time_min": jt_v, "full_fare": fare_v}
    return list(seen.values())


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    report = {}
    routes, stops, route_stops = get_kmb()
    report["kmb"] = {"routes_entries": len(routes), "stops": len(stops),
                     "route_stops": len(route_stops),
                     "distinct_route_numbers": len({r["route"] for r in routes})}
    print("KMB:", report["kmb"], flush=True)

    gtfs_zip = zipfile.ZipFile(io.BytesIO(fetch_bytes(GTFS_URL)))
    lwb_numbers, ferry_stops, gtfs_version = get_gtfs_extras(gtfs_zip)
    report["gtfs"] = {"version_files_dated": gtfs_version,
                      "lwb_route_numbers": len(lwb_numbers),
                      "ferry_stops": len(ferry_stops)}
    print("GTFS:", report["gtfs"], flush=True)

    td_rows = get_td_journey()
    companies = Counter(r["company"] for r in td_rows)
    report["td_geojson"] = {"route_variants": len(td_rows),
                            "companies": dict(companies),
                            "with_journey_time": sum(1 for r in td_rows if r["journey_time_min"])}
    print("TD GeoJSON:", report["td_geojson"], flush=True)

    stops_out = {}
    for s in stops:
        stops_out[s["stop"]] = {"name_tc": s["name_tc"], "name_en": s["name_en"],
                                "lat": float(s["lat"]), "lon": float(s["long"])}
    lines = {}
    for rs in route_stops:
        key = f"{rs['route']}|{rs['bound']}|{rs['service_type']}"
        lines.setdefault(key, []).append((int(rs["seq"]), rs["stop"]))
    lines_out = {}
    route_meta = {(r["route"], r["bound"], r["service_type"]): r for r in routes}
    for key, seq_stops in lines.items():
        seq_stops.sort()
        route, bound, st = key.split("|")
        meta = route_meta.get((route, bound, st), {})
        lines_out[key] = {
            "route": route, "bound": bound, "service_type": st,
            "orig_tc": meta.get("orig_tc", ""), "dest_tc": meta.get("dest_tc", ""),
            "orig_en": meta.get("orig_en", ""), "dest_en": meta.get("dest_en", ""),
            "stops": [sid for _, sid in seq_stops],
        }
    windows, svc_exceptions, win_stats = get_service_windows(
        gtfs_zip, lines_out, stops_out)
    report["service_windows"] = win_stats
    print("Service windows:", win_stats, flush=True)

    bundle = {"stops": stops_out, "lines": lines_out,
              "lwb_route_numbers": lwb_numbers, "td_journey": td_rows,
              "service_windows": windows, "service_exceptions": svc_exceptions}

    # sanity check vs previous bundle (PRD: >5% drop in stops/lines = reject)
    prev_path = os.path.join(DATA_DIR, "bundle.json")
    target = prev_path
    if os.path.exists(prev_path):
        prev = json.load(open(prev_path, encoding="utf-8"))
        for label, old, new in (
            ("stops", len(prev["stops"]), len(stops_out)),
            ("lines", len(prev["lines"]), len(lines_out)),
        ):
            if old and abs(new - old) / old > 0.05:
                report.setdefault("sanity_warnings", []).append(
                    f"{label}: {old} -> {new} (>5% change); bundle NOT swapped")
                target = os.path.join(DATA_DIR, "bundle.candidate.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    report["bundle_file"] = os.path.basename(target)
    report["bundle_mb"] = round(os.path.getsize(target) / 1e6, 2)

    with open(os.path.join(DATA_DIR, "ferry_stops.json"), "w", encoding="utf-8") as f:
        json.dump(ferry_stops, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA_DIR, "sync_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())

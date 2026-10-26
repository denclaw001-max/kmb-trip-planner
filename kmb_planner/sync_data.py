#!/usr/bin/env python3
"""Phase 1 data sync: build the routing bundle from three official sources.

Sources (all verified in Phase 0):
  1. KMB open data API (data.etabus.gov.hk) — routes / stops / route-stops.
     Refreshed daily 05:00 by KMB; run this after 05:30 HKT.
  2. Transport Department GTFS (static.data.gov.hk/td/pt-headway-tc/gtfs.zip)
     — agency_id per route, used to build the Long Win (LWB) route-number set
       (the KMB ETA `co` field cannot distinguish operators — Phase 0 §2).
     — ferry stops (agency FERRY) extracted for later FR-4 use.
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


def get_gtfs_extras():
    """Return (lwb_route_numbers, ferry_stops, gtfs_version_note)."""
    blob = fetch_bytes(GTFS_URL)
    z = zipfile.ZipFile(io.BytesIO(blob))
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

    lwb_numbers, ferry_stops, gtfs_version = get_gtfs_extras()
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
    bundle = {"stops": stops_out, "lines": lines_out,
              "lwb_route_numbers": lwb_numbers, "td_journey": td_rows}

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

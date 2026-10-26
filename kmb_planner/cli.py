#!/usr/bin/env python3
"""CLI for the Phase 1 routing core.

Examples:
  python3 -m kmb_planner.cli stats
  python3 -m kmb_planner.cli plan --from-coords 22.3454,114.1926 --to-coords 22.2930,114.1693
  python3 -m kmb_planner.cli plan --from-name 竹園邨 --to-name 尖沙咀碼頭
  python3 -m kmb_planner.cli stops 時代廣場        # search stop names (Tier-1)

Place-name geocoding here uses Photon (Phase 0 decision) purely as a
test convenience; the production /search endpoint is Phase 2 work.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from kmb_planner.graph import load_graph          # noqa: E402
from kmb_planner.router import Router              # noqa: E402

BUNDLE = os.path.join(BASE, "data", "bundle.json")


def photon(place):
    q = urllib.parse.urlencode({
        "q": place, "limit": 1, "bbox": "113.75,22.05,114.55,22.65",
        "lat": 22.30, "lon": 114.18, "location_bias_scale": 0.2})
    req = urllib.request.Request(
        f"https://photon.komoot.io/api/?{q}",
        headers={"User-Agent": "kmb-trip-planner-cli/0.1"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    if not data["features"]:
        raise SystemExit(f"Photon found nothing for: {place}")
    lon, lat = data["features"][0]["geometry"]["coordinates"]
    return lat, lon


def parse_coords(text):
    lat, lon = text.split(",")
    return float(lat), float(lon)


def fmt_plan(result):
    lines = [f"status: {result['status']}" + (f" ({result['reason']})" if result.get("reason") else "")]
    for i, it in enumerate(result["itineraries"], 1):
        lines.append(f"方案 {i}: {it['transfers']} 次轉車 · 約 {it['total_minutes']} 分鐘（預估）· 步行 {it['walk_meters']}m")
        for leg in it["legs"]:
            if leg["type"] == "walk":
                frm = leg["from_stop"]["name_tc"] if leg["from_stop"] else "起點"
                to = leg["to_stop"]["name_tc"] if leg["to_stop"] else "目的地"
                lines.append(f"  步行 {leg['meters']}m：{frm} → {to}")
            else:
                lines.append(
                    f"  巴士 {leg['route']}（往{leg['dest_tc']}）："
                    f"{leg['from_stop']['name_tc']} 上車 → {leg['to_stop']['name_tc']} 落車"
                    f"（{leg['stops']} 站，約 {leg['minutes']} 分鐘）")
    fb = result.get("fallback")
    if fb:
        ns = fb["nearest_stop"]
        lines.append(f"最近可達站：{ns['name_tc']}，距目的地直線 {fb['remaining_meters']}m")
        lines.append(f"Google Maps 接駁：{fb['deep_link']['google_maps']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    p = sub.add_parser("plan")
    p.add_argument("--from-coords")
    p.add_argument("--to-coords")
    p.add_argument("--from-name")
    p.add_argument("--to-name")
    p.add_argument("--max-transfers", type=int, default=2)
    p.add_argument("--max-walk", type=int, default=800)
    p.add_argument("--transfer-walk", type=int, default=300)
    p.add_argument("--no-lwb", action="store_true")
    p.add_argument("--json", action="store_true")
    s = sub.add_parser("stops")
    s.add_argument("query")
    args = ap.parse_args()

    g = load_graph(BUNDLE)
    if args.cmd == "stats":
        print(json.dumps(g.stats, ensure_ascii=False, indent=2))
        print(f"stops: {len(g.stops)}")
        return
    if args.cmd == "stops":
        q = args.query.lower()
        hits = [(sid, s) for sid, s in g.stops.items()
                if q in s["name_tc"].lower() or q in s["name_en"].lower()]
        for sid, s in hits[:15]:
            print(f"{s['name_tc']} | {s['name_en']} | {s['lat']},{s['lon']} | {sid}")
        print(f"({len(hits)} matches)")
        return

    origin = parse_coords(args.from_coords) if args.from_coords else photon(args.from_name)
    dest = parse_coords(args.to_coords) if args.to_coords else photon(args.to_name)
    r = Router(g)
    t0 = time.perf_counter()
    result = r.plan(origin, dest, max_transfers=args.max_transfers,
                    max_walk_m=args.max_walk, transfer_walk_m=args.transfer_walk,
                    include_lwb=not args.no_lwb)
    ms = (time.perf_counter() - t0) * 1000
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(fmt_plan(result))
    print(f"[{ms:.0f} ms]", file=sys.stderr)


if __name__ == "__main__":
    main()

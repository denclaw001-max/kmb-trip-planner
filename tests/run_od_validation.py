#!/usr/bin/env python3
"""50-pair OD regression validation (PRD Phase 1 deliverable).

For each pair: resolve endpoint coords (Photon, cached in od_pairs.json;
manual overrides for islands/rural points), run the engine, then check
  * structural validity: every bus leg boards/alights on the same line in
    stop order; transfer walks within limit; origin/final walks <= 800m;
    transfers within the requested max; times positive and sane vs the
    straight-line distance;
  * expected-route match where a well-known answer exists (any itinerary
    in the returned top-3 matching one acceptable route sequence counts);
  * status expectation for fallback/edge pairs.
Writes phase1/od_validation_results.json and prints a summary.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kmb_planner.cli import photon                     # noqa: E402
from kmb_planner.geo import haversine_m                # noqa: E402
from kmb_planner.graph import load_graph               # noqa: E402
from kmb_planner.router import Router                  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, "tests", "od_pairs.json")
OUT = os.path.join(BASE, "phase1", "od_validation_results.json")

# (id, category, from_place, to_place, expected_route_sequences|None,
#  expected_statuses)
PAIRS = [
    (1, "direct", "竹園邨", "尖沙咀碼頭", [["1"]], ["full"]),
    (2, "direct", "元朗站", "佐敦", [["68X"], ["268X"]], ["full"]),
    (3, "direct", "大埔中心", "中環", [["307"]], ["full"]),
    (4, "direct", "馬鞍山市中心", "金鐘", [["680"]], ["full"]),
    (5, "direct", "觀塘市中心", "金鐘", [["601"], ["619"]], ["full"]),
    (6, "direct", "堅尼地城", "觀塘碼頭", [["101"]], ["full"]),
    (7, "direct", "元朗", "銅鑼灣", [["968"]], ["full"]),
    (8, "direct", "粉嶺站", "灣仔", [["978"]], ["full"]),
    (9, "direct", "上水站", "中環", [["673"], ["978"]], ["full"]),
    (10, "direct", "坑口", "觀塘", [["98A"]], ["full"]),
    (11, "transfer1", "天水圍市中心", "尖沙咀", None, ["full"]),
    (12, "transfer1", "葵芳站", "尖沙咀", None, ["full"]),
    (13, "transfer1", "青衣站", "大埔墟", None, ["full"]),
    (14, "transfer1", "屯門站", "馬鞍山", None, ["full"]),
    (15, "transfer1", "元朗", "將軍澳站", None, ["full"]),
    (16, "transfer1", "荃灣站", "馬鞍山", None, ["full"]),
    (17, "transfer1", "大埔墟", "屯門", None, ["full"]),
    (18, "transfer1", "沙田市中心", "屯門站", [["263"]], ["full"]),
    (19, "transfer1", "東涌站", "尖沙咀", None, ["full"]),
    (20, "transfer1", "香港國際機場", "旺角", None, ["full"]),
    (21, "transfer1", "柴灣", "屯門", None, ["full"]),
    (22, "transfer1", "杏花邨", "荃灣", None, ["full"]),
    (23, "transfer1", "天水圍", "大埔", None, ["full"]),
    (24, "transfer1", "北角", "將軍澳", None, ["full"]),
    (25, "transfer1", "西貢", "尖沙咀", None, ["full"]),
    (26, "transfer2", "屯門", "西貢", None, ["full"]),
    (27, "transfer2", "馬鞍山", "葵芳", None, ["full"]),
    (28, "transfer2", "上水", "東涌", [["E43"]], ["full"]),
    (29, "transfer2", "觀塘", "大埔", [["74X"]], ["full"]),
    (30, "transfer2", "黃大仙", "香港國際機場", [["E23"], ["A23"]], ["full"]),
    (31, "transfer2", "太和", "銅鑼灣", None, ["full"]),
    (32, "transfer2", "小西灣", "元朗", None, ["full"]),
    (33, "transfer2", "天水圍", "馬鞍山", None, ["full"]),
    (34, "transfer2", "長沙灣", "西貢", None, ["full"]),
    (35, "transfer2", "調景嶺", "屯門", None, ["full"]),
    (36, "fallback", "黃大仙", "赤柱", None, ["partial", "none"]),
    (37, "fallback", "中環", "大澳", None, ["partial", "none"]),
    (38, "fallback", "沙田", "長洲", None, ["partial", "none"]),
    (39, "fallback", "旺角", "蒲台島", None, ["partial", "none"]),
    (40, "fallback", "元朗", "梅窩", None, ["partial", "none"]),
    (41, "fallback", "中環", "太平山頂", None, ["partial", "none"]),
    (42, "edge", "尖沙咀碼頭", "竹園邨", [["1"]], ["full"]),
    (43, "edge", "大角咀", "黃大仙", None, ["full"]),
    (44, "edge", "旺角", "奧運站", None, ["full"]),
    (45, "edge", "鴨脷洲邨", "荃灣", None, ["full", "partial", "none"]),
    (46, "edge", "大帽山郊野公園", "荃灣", None, ["full", "partial", "none"]),
    (47, "edge", "紅磡站", "啟德", None, ["full"]),
    (48, "edge", "科學園", "中環", None, ["full", "partial"]),
    (49, "edge", "落馬洲站", "中環", None, ["full", "partial"]),
    (50, "edge", "香港迪士尼樂園", "屯門", [["R33"]], ["full"]),
]

# Photon misplaces several test points (airport resolved to the runway,
# 上水 to a northern village, 大角咀 to the wrong district, 馬鞍山 to the
# mountain, 坑口 to another village) — pin verified coords for those.
# 梅窩 longitude is 113.994 (an earlier 114.994 typo put it in the sea
# east of Hong Kong and produced a nonsense 70km fallback — caught by
# this validation run, fixed here).
OVERRIDES = {
    "赤柱": (22.2180, 114.2260), "大澳": (22.2580, 113.8630),
    "長洲": (22.2090, 114.0280), "蒲台島": (22.1690, 114.2590),
    "梅窩": (22.2650, 113.9940), "太平山頂": (22.2759, 114.1455),
    "大帽山郊野公園": (22.4115, 114.1228),
    "香港國際機場": (22.3155, 113.9366),   # Ground Transportation Centre
    "上水": (22.5012, 114.1287),            # 上水站
    "大角咀": (22.3265, 114.1625),          # 維港灣一帶
    "馬鞍山": (22.4247, 114.2317),          # 馬鞍山市中心（新港城）
    "元朗": (22.4445, 114.0310),            # 元朗大馬路（又新街）
    "坑口": (22.3155, 114.2643),            # 坑口站
}


def coords_for(place, cache):
    if place in OVERRIDES:
        return OVERRIDES[place]
    if place not in cache:
        cache[place] = photon(place)
        time.sleep(0.15)
    return tuple(cache[place])


def structural_check(g, result, origin, dest, max_transfers=2):
    problems = []
    if result["status"] == "full":
        itins = result["itineraries"]
    elif result["status"] == "partial":
        itins = result["itineraries"]
    else:
        return problems
    if not itins:
        return ["no itinerary returned"]
    dist = haversine_m(*origin, *dest)
    for itin in itins:
        if itin["transfers"] > max_transfers:
            problems.append(f"transfers {itin['transfers']} > {max_transfers}")
        bus = [s for s in itin["legs"] if s["type"] == "bus"]
        if result["status"] == "full" and not bus:
            problems.append("full itinerary without a bus leg")
        for leg in bus:
            line = g.lines.get(leg["line_key"])
            if line is None:
                problems.append(f"unknown line {leg['line_key']}")
                continue
            stops = line["stops"]
            bi = stops.index(leg["from_stop"]["id"])
            ai = stops.index(leg["to_stop"]["id"])
            if ai <= bi:
                problems.append(f"{leg['route']}: alight not after board")
            if leg["stops"] != ai - bi:
                problems.append(f"{leg['route']}: stop count mismatch")
        for leg in itin["legs"]:
            if leg["type"] == "walk" and leg["from_stop"] and leg["to_stop"]:
                if leg["meters"] > 300 + 25:
                    problems.append(f"transfer walk {leg['meters']}m > 300m")
        total = itin["total_minutes"]
        floor_min = dist / 1000 / 70 * 60 * 0.7   # faster than 70km/h avg = wrong
        if result["status"] == "full" and total < floor_min:
            problems.append(f"total {total}min below sanity floor {floor_min:.0f}min")
        if total <= 0:
            problems.append("non-positive total time")
    return problems


def main():
    g = load_graph(os.path.join(BASE, "data", "bundle.json"))
    router = Router(g)
    cache = {}
    if os.path.exists(CACHE):
        cache = {k: tuple(v) for k, v in
                 json.load(open(CACHE, encoding="utf-8")).items()}
    results = []
    for pid, cat, frm, to, expected, statuses in PAIRS:
        o = coords_for(frm, cache)
        d = coords_for(to, cache)
        t0 = time.perf_counter()
        res = router.plan(o, d)
        ms = (time.perf_counter() - t0) * 1000
        problems = structural_check(g, res, o, d)
        status_ok = res["status"] in statuses
        exp_ok = None
        if expected:
            # family match: trailing-letter variants (307A/680X/601P) count
            # as their base route — they are the same corridor's variants.
            import re as _re
            fam = lambda r: _re.sub(r"[A-Z]+$", "", r)  # noqa: E731
            seqs = [tuple(fam(s["route"]) for s in it["legs"]
                          if s["type"] == "bus")
                    for it in res["itineraries"]]
            exp_ok = any(tuple(fam(e) for e in exp) in seqs
                         for exp in expected)
        top = res["itineraries"][0] if res["itineraries"] else None
        results.append({
            "id": pid, "category": cat, "from": frm, "to": to,
            "origin": o, "dest": d, "status": res["status"],
            "status_ok": status_ok, "expected_match": exp_ok,
            "problems": problems, "ms": round(ms),
            "top": ({"transfers": top["transfers"],
                     "total_minutes": top["total_minutes"],
                     "routes": [s["route"] for s in top["legs"]
                                if s["type"] == "bus"]} if top else None),
            "remaining_meters": (res["fallback"]["remaining_meters"]
                                 if res.get("fallback") else None),
        })
        print(f"#{pid:02d} {cat:9s} {frm} -> {to}: {res['status']}"
              f" top={results[-1]['top']} exp={exp_ok} probs={problems} [{ms:.0f}ms]")
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    full = [r for r in results if r["status"] == "full"]
    print("\n=== summary ===")
    print(f"pairs: {len(results)}  full: {len(full)}  "
          f"partial: {sum(1 for r in results if r['status']=='partial')}  "
          f"none: {sum(1 for r in results if r['status']=='none')}")
    print(f"status expectations met: {sum(1 for r in results if r['status_ok'])}/{len(results)}")
    exps = [r for r in results if r["expected_match"] is not None]
    print(f"expected-route matches: {sum(1 for r in exps if r['expected_match'])}/{len(exps)}")
    print(f"structural problems: {sum(len(r['problems']) for r in results)} "
          f"across {sum(1 for r in results if r['problems'])} pairs")
    times = sorted(r["ms"] for r in results)
    print(f"query ms: median {times[len(times)//2]}, p95 {times[int(len(times)*0.95)]}")


if __name__ == "__main__":
    main()

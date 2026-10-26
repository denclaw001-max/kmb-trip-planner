#!/usr/bin/env python3
"""Phase 0: test free place search (Photon + Nominatim) on 20 HK places.
Hit = a result in top 5 whose name/address contains an expected keyword
(case-insensitive) AND coords inside HK bbox. Nominatim: 1 req / 1.1s.
"""
import json, time, urllib.parse, urllib.request

HK_BBOX = (22.05, 22.65, 113.75, 114.55)  # lat_min, lat_max, lon_min, lon_max

PLACES = [
    # (query, [expected keywords in result name/address])
    ("時代廣場 銅鑼灣", ["時代廣場", "Times Square"]),
    ("香港國際機場", ["香港國際機場", "Hong Kong International Airport"]),
    ("太平山頂", ["山頂", "The Peak", "Peak Tower", "Peak Galleria"]),
    ("香港迪士尼樂園", ["迪士尼", "Disneyland"]),
    ("海洋公園 香港", ["海洋公園", "Ocean Park"]),
    ("朗豪坊 旺角", ["朗豪坊", "Langham Place"]),
    ("海港城 尖沙咀", ["海港城", "Harbour City"]),
    ("新城市廣場 沙田", ["新城市廣場", "New Town Plaza"]),
    ("太古廣場 金鐘", ["太古廣場", "Pacific Place"]),
    ("apm 觀塘", ["apm", "Millennium City"]),
    ("屯門市廣場", ["屯門市廣場", "Tuen Mun Town Plaza", "tmtplaza"]),
    ("形點 元朗", ["形點", "YOHO"]),
    ("愉景新城 荃灣", ["愉景新城", "Discovery Park"]),
    ("香港中文大學", ["中文大學", "Chinese University"]),
    ("香港科技大學", ["科技大學", "University of Science and Technology"]),
    ("伊利沙伯醫院", ["伊利沙伯醫院", "Queen Elizabeth Hospital"]),
    ("西九文化區", ["西九", "West Kowloon Cultural", "M+"]),
    ("大館 中環", ["大館", "Tai Kwun"]),
    ("軒尼詩道500號", ["軒尼詩道", "Hennessy Road", "希慎廣場", "Hysan"]),
    ("彌敦道700號 旺角", ["彌敦道", "Nathan Road", "TOP"]),
]

def fetch(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

def in_hk(lat, lon):
    return HK_BBOX[0] <= lat <= HK_BBOX[1] and HK_BBOX[2] <= lon <= HK_BBOX[3]

def judge(results, keywords):
    """results: list of (name_text, lat, lon). Return rank (1-based) of first hit or None."""
    for i, (name, lat, lon) in enumerate(results[:5], 1):
        if in_hk(lat, lon) and any(k.lower() in name.lower() for k in keywords):
            return i
    return None

def photon(query):
    # NOTE: lang=zh is NOT supported by Photon (only default/de/en/fr) -> HTTP 400.
    # Must pass HK bbox + lat/lon bias, else same-name places in Taiwan win.
    q = urllib.parse.urlencode({
        "q": query, "limit": 5,
        "bbox": "113.75,22.05,114.55,22.65",
        "lat": 22.30, "lon": 114.18, "location_bias_scale": 0.2,
    })
    data = fetch(f"https://photon.komoot.io/api/?{q}", "kmb-trip-planner-phase0/0.1 (research test)")
    out = []
    for f in data.get("features", []):
        p = f["properties"]
        text = " ".join(str(p.get(k, "")) for k in ("name", "street", "district", "city", "county", "state"))
        lon, lat = f["geometry"]["coordinates"]
        out.append((text, lat, lon))
    return out

def nominatim(query):
    # NOTE: countrycodes=hk returns [] - OSM/Nominatim tags HK features as
    # country China. Use viewbox bias instead, filter by bbox in judge().
    q = urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 5,
        "viewbox": "113.75,22.65,114.55,22.05", "addressdetails": 1,
    })
    data = fetch(f"https://nominatim.openstreetmap.org/search?{q}",
                 "kmb-trip-planner-phase0/0.1 (one-off research test; contact via project owner)")
    out = []
    for r in data:
        out.append((r.get("display_name", ""), float(r["lat"]), float(r["lon"])))
    return out

def main():
    results = {"photon": [], "nominatim": []}
    for idx, (query, kws) in enumerate(PLACES, 1):
        row = {"query": query}
        try:
            res = photon(query)
            rank = judge(res, kws)
            row["photon_rank"] = rank
            row["photon_top"] = res[0][0][:60] if res else "(no results)"
        except Exception as e:
            row["photon_rank"] = None
            row["photon_top"] = f"ERROR {e}"
        results["photon"].append(row)
        print(f"[{idx}/20] {query} photon_rank={row['photon_rank']} top={row['photon_top']}")
    print("--- nominatim (slow, 1/s) ---")
    for idx, (query, kws) in enumerate(PLACES, 1):
        row = results["photon"][idx - 1]
        try:
            res = nominatim(query)
            rank = judge(res, kws)
            row["nominatim_rank"] = rank
            row["nominatim_top"] = res[0][0][:60] if res else "(no results)"
        except Exception as e:
            row["nominatim_rank"] = None
            row["nominatim_top"] = f"ERROR {e}"
        results["nominatim"].append(row)
        print(f"[{idx}/20] {query} nominatim_rank={row['nominatim_rank']} top={row['nominatim_top']}")
        time.sleep(1.1)
    for svc in ("photon", "nominatim"):
        rows = results["photon"] if svc == "photon" else results["nominatim"]
        key = f"{svc}_rank"
        hit1 = sum(1 for r in results["photon"] if r.get(key) == 1)
        hit5 = sum(1 for r in results["photon"] if r.get(key))
        print(f"SUMMARY {svc}: top1={hit1}/20 top5={hit5}/20")
    with open("place_search_results.json", "w", encoding="utf-8") as f:
        json.dump(results["photon"], f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()

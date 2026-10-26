#!/usr/bin/env python3
"""One-off builder for data/mtr_stations.json (FR-4 hints, Phase 3).

Geocodes the canonical MTR heavy-rail station list with Photon
(Phase 0 method), HK-bbox-checked. Ambiguous stations are pinned by
OVERRIDES. Light Rail is deferred per the Phase 0 tiered plan.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kmb_planner.cli import photon  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATIONS = [
    # Island line
    "堅尼地城", "香港大學", "西營盤", "上環", "中環", "金鐘", "灣仔", "銅鑼灣",
    "天后", "炮台山", "北角", "鰂魚涌", "太古", "西灣河", "筲箕灣", "杏花邨", "柴灣",
    # Tsuen Wan line
    "尖沙咀", "佐敦", "油麻地", "旺角", "太子", "深水埗", "長沙灣", "荔枝角",
    "美孚", "荔景", "葵芳", "葵興", "大窩口", "荃灣",
    # Kwun Tong line
    "黃埔", "何文田", "石硤尾", "九龍塘", "樂富", "黃大仙", "鑽石山", "彩虹",
    "九龍灣", "牛頭角", "觀塘", "藍田", "油塘", "調景嶺",
    # Tseung Kwan O line
    "將軍澳", "坑口", "寶琳", "康城",
    # Tung Chung line
    "香港", "九龍", "奧運", "南昌", "青衣", "欣澳", "東涌",
    # Airport Express
    "機場", "博覽館",
    # East Rail line
    "會展", "紅磡", "旺角東", "大圍", "沙田", "火炭", "馬場", "大學",
    "大埔墟", "太和", "粉嶺", "上水", "羅湖", "落馬洲",
    # Tuen Ma line
    "烏溪沙", "馬鞍山", "恆安", "大水坑", "石門", "第一城", "沙田圍", "車公廟",
    "顯徑", "啟德", "宋皇臺", "土瓜灣", "尖東", "柯士甸", "荃灣西", "錦上路",
    "元朗", "朗屏", "天水圍", "兆康", "屯門",
    # South Island line
    "海洋公園", "黃竹坑", "利東", "海怡半島",
    # Disneyland line
    "迪士尼",
]

OVERRIDES = {
    "機場": (22.3155, 113.9366),   # Airport Station at Terminal 1
    "馬場": (22.4008, 114.2053),   # Racecourse Station
}


def main():
    out, problems = [], []
    for name in STATIONS:
        if name in OVERRIDES:
            lat, lon = OVERRIDES[name]
        else:
            try:
                lat, lon = photon(f"{name}站")
            except SystemExit:
                problems.append((name, "not found"))
                continue
            time.sleep(0.12)
            if not (22.05 <= lat <= 22.65 and 113.75 <= lon <= 114.55):
                problems.append((name, f"outside bbox {lat},{lon}"))
                continue
        out.append({"name_tc": name, "lat": round(lat, 6), "lon": round(lon, 6)})
    path = os.path.join(BASE, "data", "mtr_stations.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(out)} stations -> {path}")
    if problems:
        print("PROBLEMS:", problems)


if __name__ == "__main__":
    main()

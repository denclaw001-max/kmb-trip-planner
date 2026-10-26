"""Phase 4 validation on the real bundle: known service-window cases.

Run: python3 tests/validate_hours.py   (exit 0 = all pass)
Cases pin the 5P peak-only pattern (Mon-Fri: outbound 07:10-08:00,
inbound 17:55-19:30), the Sunday closure, late-evening route 1, and
the legacy time-independent behaviour when no depart time is given.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kmb_planner.graph import load_graph          # noqa: E402
from kmb_planner.router import Router             # noqa: E402
from kmb_planner.service_time import (              # noqa: E402
    all_flag_patterns, service_context)

HKT = ZoneInfo("Asia/Hong_Kong")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
g = load_graph(os.path.join(BASE, "data", "bundle.json"))
router = Router(g)
PATTERNS = all_flag_patterns(g)

FAIL = []


def ctx(y, mo, d, h, mi):
    dt = datetime(y, mo, d, h, mi, tzinfo=HKT)
    clock, patterns, _ = service_context(dt, PATTERNS, g.service_exceptions)
    return (clock, patterns)


def routes_of(result):
    out = []
    for itin in result["itineraries"]:
        out.append(tuple(l["route"] for l in itin["legs"] if l["type"] == "bus"))
    return out


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAIL.append(name)


def stop_coord(stop_id):
    s = g.stops[stop_id]
    return (s["lat"], s["lon"])


line_5p_o = g.lines["5P|O|1"]
line_5p_i = g.lines["5P|I|1"]
tsz_wan_shan = stop_coord(line_5p_o["stops"][0])     # 慈雲山(中) terminus
tst_pier = stop_coord(line_5p_o["stops"][-1])        # 尖沙咀碼頭 terminus
chuk_yuen = (22.345415, 114.19264)                   # 竹園邨總站 (live-verified)
tst = (22.293816, 114.168797)                        # 尖沙咀碼頭，海港城

assert datetime(2026, 10, 26).weekday() == 0  # Monday
assert datetime(2026, 11, 1).weekday() == 6   # Sunday

# 1. Monday 13:52 (Dennis's live case): 5P must NOT be offered.
r = router.plan(chuk_yuen, tst, depart=ctx(2026, 10, 26, 13, 52))
check("Mon 13:52 5P absent", all("5P" not in seq for seq in routes_of(r)),
      str(routes_of(r)))
check("Mon 13:52 route 1 offered", any("1" in seq for seq in routes_of(r)))

# 2. Legacy behaviour (no depart): 5P IS offered for the same OD.
r = router.plan(chuk_yuen, tst)
check("legacy no-depart 5P offered", any("5P" in seq for seq in routes_of(r)),
      str(routes_of(r)))

# 3. Monday 07:20 from Tsz Wan Shan: 5P outbound is in its band.
r = router.plan(tsz_wan_shan, tst_pier, depart=ctx(2026, 10, 26, 7, 20))
check("Mon 07:20 5P outbound offered",
      any(seq and seq[0] == "5P" for seq in routes_of(r)), str(routes_of(r)))

# 4. Monday 18:05 from TST pier: 5P inbound is in its band.
r = router.plan(tst_pier, tsz_wan_shan, depart=ctx(2026, 10, 26, 18, 5))
check("Mon 18:05 5P inbound offered",
      any("5P" in seq for seq in routes_of(r)), str(routes_of(r)))

# 5. Sunday 07:20: 5P does not run at all.
r = router.plan(tsz_wan_shan, tst_pier, depart=ctx(2026, 11, 1, 7, 20))
check("Sun 07:20 5P absent", all("5P" not in seq for seq in routes_of(r)),
      str(routes_of(r)))

# 6. Monday 23:30: route 1 (runs to ~00:45) still offered.
r = router.plan(chuk_yuen, tst, depart=ctx(2026, 10, 26, 23, 30))
check("Mon 23:30 route 1 offered", any("1" in seq for seq in routes_of(r)),
      str(routes_of(r)))

# 7. Monday 09:30 from Tsz Wan Shan: outbound band ended 08:00, so no 5P.
r = router.plan(tsz_wan_shan, tst_pier, depart=ctx(2026, 10, 26, 9, 30))
check("Mon 09:30 5P absent", all("5P" not in seq for seq in routes_of(r)),
      str(routes_of(r)))

print("\n%d failures" % len(FAIL))
sys.exit(1 if FAIL else 0)

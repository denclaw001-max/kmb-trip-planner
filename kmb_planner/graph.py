"""Build the in-memory network graph from the sync bundle.

- Lines: (route, bound, service_type). Only service_type == "1" (main
  service) enters the MVP graph, per PRD FR-2.
- Segment times: if the line matches a TD route variant by
  (route, orig_tc, dest_tc), the official TD journeyTime is apportioned
  across segments by distance share (Phase 0 upgrade to PRD §6).
  Otherwise a distance/speed model is used, with speed tiered by the
  line's average stop spacing.
- Walking neighbours: every stop gets the list of other stops within
  MAX_TRANSFER_WALK_M (300m), via the grid index. Same-location stops
  (termini, opposite sides) are covered by this same mechanism, which is
  how the PRD's "stop cluster" idea is implemented here.
"""
import json
import re
import statistics
from collections import defaultdict

from .geo import GridIndex, haversine_m

# Special / overnight-only route families excluded from the MVP graph
# (PRD risk: special services pollute results). Verified against the
# 2026-10-23 data: N*/NA*/NB* overnight, R* recreation/event, HK*
# sightseeing, SP*/PB* special shuttles, X* special peak variants,
# T* peak-only specials, and bare digits+R (101R racing, 92R Sunday
# recreation, 5R event shuttles — all 27 such routes in the data are
# special services). Regular all-day families that merely look special
# (A, E, S, B, W, P960/P968 premium, and digits+X trunk routes such as
# 68X/260X/968X) are kept.
_EXCLUDED_ROUTE = re.compile(r"^(\d+R$|(N|NA|NB|R|HK|SP|PB|X|T)\d)")

# Curated exceptions: regular daily Disneyland services that happen to
# carry an R number. Without them Disneyland would be unreachable in the
# graph even though these are ordinary published services.
_ROUTE_WHITELIST = {"R8", "R33", "R42"}

WALK_SPEED_M_PER_MIN = 80.0
DETOUR_FACTOR = 1.3
MAX_TRANSFER_WALK_M = 300


def _speed_kmh(avg_spacing_m):
    if avg_spacing_m >= 1200:
        return 40.0
    if avg_spacing_m >= 700:
        return 30.0
    if avg_spacing_m >= 400:
        return 22.0
    return 16.0


class Graph:
    def __init__(self, bundle):
        self.stops = bundle["stops"]            # id -> {name_tc, name_en, lat, lon}
        self.lwb_numbers = set(bundle.get("lwb_route_numbers", []))
        self.lines = {}                          # key -> line dict
        self.stop_lines = defaultdict(list)      # stop_id -> [(line_key, idx)]
        self.stats = {}
        self._build_lines(bundle)
        self._build_spatial()

    # ------------------------------------------------------------- lines
    def _build_lines(self, bundle):
        td = defaultdict(list)
        for row in bundle.get("td_journey", []):
            if row["journey_time_min"]:
                td[(row["route"], row["start_tc"], row["end_tc"])].append(
                    row["journey_time_min"])
        td_median = {k: statistics.median(v) for k, v in td.items()}

        matched = unmatched = excluded = 0
        for key, line in bundle["lines"].items():
            if line["service_type"] != "1":
                continue
            if (line["route"] not in _ROUTE_WHITELIST
                    and _EXCLUDED_ROUTE.match(line["route"])):
                excluded += 1
                continue
            stops = line["stops"]
            if len(stops) < 2:
                continue
            seg_m = []
            for a, b in zip(stops, stops[1:]):
                sa, sb = self.stops[a], self.stops[b]
                seg_m.append(haversine_m(sa["lat"], sa["lon"], sb["lat"], sb["lon"]))
            total_m = sum(seg_m)
            # TD publishes one canonical direction per route; the reverse
            # KMB bound has orig/dest swapped — try both (Phase 1 finding:
            # exact-only matching covered just 51% of lines).
            anchor = td_median.get((line["route"], line["orig_tc"], line["dest_tc"]))
            if anchor is None:
                anchor = td_median.get((line["route"], line["dest_tc"], line["orig_tc"]))
            seg_min = []
            if anchor and total_m > 0:
                for d in seg_m:
                    seg_min.append(anchor * d / total_m)
                method = "td_journey"
                matched += 1
            else:
                avg_spacing = total_m / max(1, len(seg_m))
                kmh = _speed_kmh(avg_spacing)
                for d in seg_m:
                    seg_min.append(d * DETOUR_FACTOR / (kmh * 1000 / 60))
                method = "speed_model"
                unmatched += 1
            prefix = [0.0]
            for t in seg_min:
                prefix.append(prefix[-1] + t)
            entry = {
                "key": key, "route": line["route"], "bound": line["bound"],
                "service_type": line["service_type"],
                "orig_tc": line["orig_tc"], "dest_tc": line["dest_tc"],
                "orig_en": line["orig_en"], "dest_en": line["dest_en"],
                "stops": stops, "seg_m": seg_m, "prefix_min": prefix,
                "total_min": prefix[-1], "time_method": method,
                "is_lwb": line["route"] in self.lwb_numbers,
            }
            self.lines[key] = entry
            for idx, sid in enumerate(stops):
                self.stop_lines[sid].append((key, idx))
        self.stats["lines_in_graph"] = len(self.lines)
        self.stats["lines_excluded_special"] = excluded
        self.stats["lines_td_journey_matched"] = matched
        self.stats["lines_speed_model"] = unmatched
        if matched + unmatched:
            self.stats["td_match_rate"] = round(matched / (matched + unmatched), 3)

    # ----------------------------------------------------------- spatial
    def _build_spatial(self):
        self.grid = GridIndex(cell_m=400)
        for sid, s in self.stops.items():
            self.grid.add(sid, s["lat"], s["lon"])
        self.neighbours = {}
        for sid, s in self.stops.items():
            lst = [(oid, d) for oid, d in
                   self.grid.within(s["lat"], s["lon"], MAX_TRANSFER_WALK_M)
                   if oid != sid]
            lst.sort(key=lambda x: x[1])
            self.neighbours[sid] = lst
        self.stats["stops_with_walk_neighbours"] = sum(
            1 for v in self.neighbours.values() if v)

    def stops_within(self, lat, lon, radius_m):
        return list(self.grid.within(lat, lon, radius_m))


def load_graph(bundle_path):
    with open(bundle_path, encoding="utf-8") as f:
        return Graph(json.load(f))

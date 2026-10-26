"""RAPTOR-style transfer-round routing over the KMB network (PRD Part 2 §4).

No timetable exists, so a "trip" is the line itself: riding cost from a
boarding stop to a later stop on the same line is the precomputed segment
time sum (graph.py). Each round adds one bus leg. Between rounds, walking
transfers (<= transfer_walk_m) expand the reached set.

Cost model (PRD §6): total = ride times + walking (80 m/min) + a fixed
5 min waiting estimate per boarding. Sorting is transfers first, then
total time, then walking distance (PRD §0.3 decision 2).

Fallback (PRD §5.3): if no stop within max_walk_m of the destination is
reached, return the reached stop closest to the destination (with the
report's protections: candidates >=500m apart, must make >=20% progress
when possible, origin-area stops don't count).
"""
from .geo import haversine_m
from .graph import WALK_SPEED_M_PER_MIN

WAIT_MIN_PER_BOARDING = 5.0


class State:
    __slots__ = ("stop", "arrival", "legs", "walk_m", "prev", "via")

    def __init__(self, stop, arrival, legs, walk_m, prev, via):
        self.stop = stop            # stop_id (None for the origin pseudo-state)
        self.arrival = arrival      # minutes from trip start
        self.legs = legs            # bus legs used so far
        self.walk_m = walk_m        # total walking metres so far
        self.prev = prev            # previous State
        self.via = via              # ("walk", from_stop|None, metres) |
                                    # ("bus", line_key, board_stop, board_idx)


class Router:
    def __init__(self, graph):
        self.g = graph

    # ------------------------------------------------------------ helpers
    def _walk_expand(self, states, transfer_walk_m):
        """states: dict stop->State. Returns new dict including neighbours
        reached by walking <= transfer_walk_m (best arrival per stop)."""
        out = dict(states)
        for stop, st in states.items():
            for nb, metres in self.g.neighbours.get(stop, ()):
                if metres > transfer_walk_m:
                    break  # neighbours are distance-sorted
                arr = st.arrival + metres / WALK_SPEED_M_PER_MIN
                cur = out.get(nb)
                if cur is None or arr < cur.arrival:
                    out[nb] = State(nb, arr, st.legs, st.walk_m + metres,
                                    st, ("walk", stop, metres))
        return out

    def _bus_round(self, pool, legs, include_lwb):
        """One RAPTOR round: from boarding pool (states with legs-1) ride
        every line forward. Returns dict stop->State with legs."""
        reached = {}
        g = self.g
        for key, line in g.lines.items():
            if not include_lwb and line["is_lwb"]:
                continue
            stops = line["stops"]
            prefix = line["prefix_min"]
            best_val = None      # arrival + WAIT - prefix[idx]
            best_state = None
            best_idx = -1
            for idx, sid in enumerate(stops):
                if best_state is not None:
                    arr = best_val + prefix[idx]
                    cur = reached.get(sid)
                    if cur is None or arr < cur.arrival:
                        reached[sid] = State(
                            sid, arr, legs,
                            best_state.walk_m, best_state,
                            ("bus", key, best_state.stop, best_idx))
                st = pool.get(sid)
                if st is not None:
                    val = st.arrival + WAIT_MIN_PER_BOARDING - prefix[idx]
                    if best_val is None or val < best_val:
                        best_val, best_state, best_idx = val, st, idx
        return reached

    def _reconstruct(self, state):
        """Follow the backpointer chain -> ordered segment list."""
        segs = []
        cur = state
        while cur is not None and cur.via is not None:
            if cur.via[0] == "bus":
                _, key, board_stop, board_idx = cur.via
                line = self.g.lines[key]
                alight_idx = line["stops"].index(cur.stop, board_idx + 1) \
                    if cur.stop in line["stops"][board_idx + 1:] else None
                # stops.index with start arg is exact; fallback safety above
                segs.append({
                    "type": "bus", "route": line["route"],
                    "bound": line["bound"], "line_key": key,
                    "dest_tc": line["dest_tc"], "dest_en": line["dest_en"],
                    "from_stop": board_stop, "to_stop": cur.stop,
                    "stops": alight_idx - board_idx,
                    "minutes": round(line["prefix_min"][alight_idx]
                                     - line["prefix_min"][board_idx], 1),
                })
                cur = cur.prev
            else:  # walk
                _, from_stop, metres = cur.via
                segs.append({"type": "walk",
                             "from_stop": from_stop, "to_stop": cur.stop,
                             "meters": round(metres),
                             "minutes": round(metres / WALK_SPEED_M_PER_MIN, 1)})
                cur = cur.prev
        segs.reverse()
        return segs

    @staticmethod
    def _merge_walks(segs):
        """Merge consecutive walk segments (walk-transfer + final walk can
        touch) and drop zero-length walks when other segments exist."""
        out = []
        for s in segs:
            if (s["type"] == "walk" and out and out[-1]["type"] == "walk"):
                prev = out[-1]
                prev["to_stop"] = s["to_stop"]
                prev["meters"] += s["meters"]
                prev["minutes"] = round(prev["minutes"] + s["minutes"], 1)
            else:
                out.append(dict(s))
        if len(out) > 1:
            out = [s for s in out
                   if not (s["type"] == "walk" and s["meters"] == 0)]
        return out

    def _name_stops(self, segs):
        for s in segs:
            for field in ("from_stop", "to_stop"):
                sid = s.get(field)
                if sid:
                    info = self.g.stops[sid]
                    s[field] = {"id": sid, "name_tc": info["name_tc"],
                                "name_en": info["name_en"],
                                "lat": info["lat"], "lon": info["lon"]}
                else:
                    s[field] = None
        return segs

    # --------------------------------------------------------------- plan
    def plan(self, origin, dest, max_transfers=2, max_walk_m=800,
             transfer_walk_m=300, include_lwb=True):
        g = self.g
        o_lat, o_lon = origin
        d_lat, d_lon = dest
        max_legs = max_transfers + 1

        origin_states = {}
        for sid, metres in g.stops_within(o_lat, o_lon, max_walk_m):
            origin_states[sid] = State(
                sid, metres / WALK_SPEED_M_PER_MIN, 0, metres,
                State(None, 0.0, 0, 0, None, None),
                ("walk", None, metres))
        if not origin_states:
            return {"status": "none", "reason": "origin_no_bus_stop",
                    "itineraries": [], "fallback": None}

        pools = {0: origin_states}       # legs -> {stop: State} after bus round
        expanded = {0: origin_states}    # legs -> states after walk expansion
        candidates = []                  # (transfers, total_min, walk_m, segs)

        for legs in range(1, max_legs + 1):
            reached = self._bus_round(expanded[legs - 1], legs, include_lwb)
            pools[legs] = reached
            expanded[legs] = self._walk_expand(reached, transfer_walk_m)
            for st in list(reached.values()) + list(expanded[legs].values()):
                info = g.stops[st.stop]
                d = haversine_m(info["lat"], info["lon"], d_lat, d_lon)
                if d <= max_walk_m:
                    segs = self._reconstruct(st)
                    if d > 1:
                        segs = segs + [{"type": "walk", "from_stop": st.stop,
                                        "to_stop": None, "meters": round(d),
                                        "minutes": round(d / WALK_SPEED_M_PER_MIN, 1)}]
                    total = st.arrival + d / WALK_SPEED_M_PER_MIN
                    candidates.append((legs - 1, total, st.walk_m + d, segs))
            if candidates and legs >= 1:
                # a solution with fewer legs can still appear in later
                # rounds only with more transfers, which sorts worse;
                # but equal-leg alternatives need all rounds up to the
                # point where the best transfer count is exhausted.
                pass

        if candidates:
            return {"status": "full", "reason": None,
                    "itineraries": self._select(candidates), "fallback": None}
        return self._fallback(origin, dest, pools, expanded, max_walk_m)

    def _select(self, candidates):
        # Group by route sequence first: the top-3 must be three genuinely
        # different ways to travel, not the same buses alighting one stop
        # apart (Phase 1 validation finding: hundreds of near-duplicate
        # candidates per query). Best (fastest) variant per sequence wins.
        seen = {}
        for transfers, total, walk_m, segs in candidates:
            named = self._merge_walks(self._name_stops([dict(s) for s in segs]))
            key = tuple(s["route"] for s in named if s["type"] == "bus")
            cur = seen.get(key)
            item = {"transfers": transfers, "total_minutes": round(total, 1),
                    "time_is_estimate": True, "walk_meters": round(walk_m),
                    "legs": named}
            if cur is None or total < cur["total_minutes"]:
                seen[key] = item
        items = sorted(seen.values(),
                       key=lambda x: (x["transfers"], x["total_minutes"],
                                      x["walk_meters"]))
        kept = []
        for it in items:
            dominated = any(
                k["transfers"] <= it["transfers"]
                and k["total_minutes"] <= it["total_minutes"]
                and k["walk_meters"] <= it["walk_meters"]
                and (k["transfers"], k["total_minutes"], k["walk_meters"])
                != (it["transfers"], it["total_minutes"], it["walk_meters"])
                for k in kept)
            if not dominated:
                kept.append(it)
            if len(kept) >= 3:
                break
        return kept

    def _fallback(self, origin, dest, pools, expanded, max_walk_m):
        g = self.g
        o_lat, o_lon = origin
        d_lat, d_lon = dest
        od = haversine_m(o_lat, o_lon, d_lat, d_lon)
        best_state = {}
        for legs in pools:
            if legs == 0:
                continue
            for st in list(pools[legs].values()) + list(expanded[legs].values()):
                cur = best_state.get(st.stop)
                if cur is None or (st.legs, st.arrival) < (cur.legs, cur.arrival):
                    best_state[st.stop] = st
        scored = []
        for sid, st in best_state.items():
            info = g.stops[sid]
            d_dest = haversine_m(info["lat"], info["lon"], d_lat, d_lon)
            d_org = haversine_m(info["lat"], info["lon"], o_lat, o_lon)
            if d_org < 150:      # still at the origin — not progress
                continue
            scored.append((d_dest, sid, st))
        scored.sort()
        good = [s for s in scored if od == 0 or (od - s[0]) / od >= 0.2]
        pool_scored = good if good else scored[:1]
        picked = []
        for d_dest, sid, st in pool_scored:
            info = g.stops[sid]
            if all(haversine_m(info["lat"], info["lon"],
                                g.stops[p[1]]["lat"], g.stops[p[1]]["lon"]) >= 500
                   for p in picked):
                picked.append((d_dest, sid, st))
            if len(picked) >= 3:
                break
        if not picked:
            return {"status": "none", "reason": "no_progress",
                    "itineraries": [], "fallback": None}
        d_dest, sid, st = picked[0]
        segs = self._merge_walks(
            self._name_stops([dict(s) for s in self._reconstruct(st)]))
        info = g.stops[sid]
        itin = {"transfers": st.legs - 1, "total_minutes": round(st.arrival, 1),
                "time_is_estimate": True, "walk_meters": round(st.walk_m),
                "legs": segs}
        return {
            "status": "partial", "reason": None, "itineraries": [itin],
            "fallback": {
                "nearest_stop": {"id": sid, "name_tc": info["name_tc"],
                                 "name_en": info["name_en"],
                                 "lat": info["lat"], "lon": info["lon"]},
                "remaining_meters": round(d_dest),
                "remaining_is_straight_line": True,
                "alternative_stops": [
                    {"id": p[1], "name_tc": g.stops[p[1]]["name_tc"],
                     "remaining_meters": round(p[0])} for p in picked[1:]],
                "deep_link": {
                    "google_maps": (
                        "https://www.google.com/maps/dir/?api=1"
                        f"&origin={info['lat']},{info['lon']}"
                        f"&destination={d_lat},{d_lon}&travelmode=transit"),
                    "apple_maps": (
                        "https://maps.apple.com/?saddr="
                        f"{info['lat']},{info['lon']}&daddr={d_lat},{d_lon}&dirflg=r"),
                },
            },
        }

"""Unit tests for the routing engine on a small synthetic network.

Network (lat ~22.31, lon ~114.17):
  L1: A - B - C - D          (north-south)
  L2: E - F - G              (east-west; E is ~285m east of C = walk transfer)
  L3: A - B - C - A          (circular, first stop == last stop)
  L4: Z - Y                  (isolated pair, far north-east)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kmb_planner.graph import Graph          # noqa: E402
from kmb_planner.router import Router        # noqa: E402

STOPS = {
    "A": (22.3000, 114.1700), "B": (22.3050, 114.1700),
    "C": (22.3100, 114.1700), "D": (22.3150, 114.1700),
    "E": (22.3100, 114.1728), "F": (22.3100, 114.1800),
    "G": (22.3100, 114.1900),
    "Z": (22.5000, 114.3000), "Y": (22.5100, 114.3000),
}


def make_bundle():
    stops = {sid: {"name_tc": sid, "name_en": sid, "lat": la, "lon": lo}
             for sid, (la, lo) in STOPS.items()}

    def line(route, ids, dest):
        return {"route": route, "bound": "O", "service_type": "1",
                "orig_tc": ids[0], "dest_tc": dest, "orig_en": ids[0],
                "dest_en": dest, "stops": ids}
    lines = {
        "L1|O|1": line("L1", ["A", "B", "C", "D"], "D"),
        "L2|O|1": line("L2", ["E", "F", "G"], "G"),
        "L3|O|1": line("L3", ["A", "B", "C", "A"], "A"),
        "L4|O|1": line("L4", ["Z", "Y"], "Y"),
    }
    return {"stops": stops, "lines": lines,
            "lwb_route_numbers": ["L2"], "td_journey": [],
            # Phase 4: L1 runs 10:00-11:00 daily (pattern Mon..Sun all 1),
            # departures from origin every 10 min within the band.
            "service_windows": {"L1|O|1": {"1111111": [[600, 660, 10]]}},
            "service_exceptions": {}}


def bus_legs(itin):
    return [s for s in itin["legs"] if s["type"] == "bus"]


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = Router(Graph(make_bundle()))

    def test_direct(self):
        r = self.router.plan(STOPS["A"], STOPS["D"])
        self.assertEqual(r["status"], "full")
        top = r["itineraries"][0]
        self.assertEqual(top["transfers"], 0)
        legs = bus_legs(top)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["route"], "L1")
        self.assertEqual(legs[0]["from_stop"]["id"], "A")
        self.assertEqual(legs[0]["to_stop"]["id"], "D")
        self.assertEqual(legs[0]["stops"], 3)

    def test_walking_transfer(self):
        r = self.router.plan(STOPS["A"], STOPS["G"])
        self.assertEqual(r["status"], "full")
        top = r["itineraries"][0]
        self.assertEqual(top["transfers"], 1)
        legs = bus_legs(top)
        # L1 and the circular L3 both serve A->C; either is a correct top pick
        self.assertIn(tuple(l["route"] for l in legs),
                      [("L1", "L2"), ("L3", "L2")])
        walks = [s for s in top["legs"] if s["type"] == "walk"
                 and s["from_stop"] and s["to_stop"]]
        self.assertTrue(any(w["from_stop"]["id"] == "C"
                            and w["to_stop"]["id"] == "E"
                            and w["meters"] <= 300 for w in walks))

    def test_circular_route(self):
        # B -> A is only possible on the circular L3 (B-C-A) or walking;
        # the engine must not produce a zero-length ride at A on L3.
        r = self.router.plan(STOPS["B"], STOPS["A"])
        self.assertEqual(r["status"], "full")
        for itin in r["itineraries"]:
            for leg in bus_legs(itin):
                self.assertGreaterEqual(leg["stops"], 1)
        routes = [tuple(l["route"] for l in bus_legs(i))
                  for i in r["itineraries"]]
        self.assertIn(("L3",), routes)

    def test_fallback_partial(self):
        r = self.router.plan(STOPS["A"], STOPS["Y"])
        self.assertEqual(r["status"], "partial")
        fb = r["fallback"]
        self.assertGreater(fb["remaining_meters"], 0)
        self.assertIn(fb["nearest_stop"]["id"], set("ABCDEFG"))
        self.assertTrue(fb["deep_link"]["google_maps"].startswith("https://"))

    def test_origin_uncovered(self):
        r = self.router.plan((22.2000, 114.0000), STOPS["D"])
        self.assertEqual(r["status"], "none")
        self.assertEqual(r["reason"], "origin_no_bus_stop")

    def test_service_window_in_hours(self):
        # 10:30, L1's daily pattern active -> direct L1 ride as usual.
        r = self.router.plan(STOPS["A"], STOPS["D"],
                             depart=(630.0, {"1111111"}))
        self.assertEqual(r["status"], "full")
        legs = bus_legs(r["itineraries"][0])
        self.assertEqual([l["route"] for l in legs], ["L1"])

    def test_service_window_out_of_hours(self):
        # 11:40: L1 has stopped. The only way left is the unrestricted
        # circular L3 to C plus the final walk to D.
        r = self.router.plan(STOPS["A"], STOPS["D"],
                             depart=(700.0, {"1111111"}))
        self.assertEqual(r["status"], "full")
        for itin in r["itineraries"]:
            self.assertNotIn("L1", [l["route"] for l in bus_legs(itin)])
        legs = bus_legs(r["itineraries"][0])
        self.assertEqual([l["route"] for l in legs], ["L3"])

    def test_service_window_wrong_day(self):
        # 10:30 but today's active patterns don't include L1's daily
        # pattern -> L1 is treated as not running today.
        r = self.router.plan(STOPS["A"], STOPS["D"],
                             depart=(630.0, {"0000011"}))
        for itin in r["itineraries"]:
            self.assertNotIn("L1", [l["route"] for l in bus_legs(itin)])

    def test_lwb_toggle(self):
        on = self.router.plan(STOPS["A"], STOPS["G"], include_lwb=True)
        off = self.router.plan(STOPS["A"], STOPS["G"], include_lwb=False)
        self.assertEqual(on["status"], "full")
        self.assertNotEqual(off["status"], "full")


if __name__ == "__main__":
    unittest.main(verbosity=2)

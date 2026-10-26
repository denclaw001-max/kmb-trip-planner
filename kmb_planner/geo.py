"""Geographic helpers: haversine distance + a simple grid index.

Pure stdlib — the deployment target (Render free, 512MB) should not need
scipy just for radius queries over ~7k stops.
"""
import math

EARTH_R = 6_371_000.0


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


class GridIndex:
    """Bucket points into ~cell_m grid cells; radius queries scan the
    neighbouring cells and filter by exact haversine distance."""

    def __init__(self, cell_m=400):
        self.cell_deg = cell_m / 111_320.0
        self.cells = {}

    def _key(self, lat, lon):
        return (int(lat / self.cell_deg), int(lon / self.cell_deg))

    def add(self, item_id, lat, lon):
        self.cells.setdefault(self._key(lat, lon), []).append((item_id, lat, lon))

    def within(self, lat, lon, radius_m):
        """Yield (item_id, dist_m) for points within radius_m of (lat, lon)."""
        span = max(1, int(math.ceil(radius_m / 111_320.0 / self.cell_deg)))
        cx, cy = self._key(lat, lon)
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for item_id, ilat, ilon in self.cells.get((cx + dx, cy + dy), ()):
                    d = haversine_m(lat, lon, ilat, ilon)
                    if d <= radius_m:
                        yield item_id, d

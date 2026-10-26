# 九巴行程規劃 KMB Trip Planner

A KMB (+ Long Win) bus trip planner for Hong Kong: stop/place search,
map picking, up to 2 transfers with walking links, live first-leg ETA,
fallback suggestions with map deep links, Traditional Chinese / English.

Built in phases against a verified PRD (data: KMB open data API,
Transport Department GTFS + routes-and-fares GeoJSON; basemap: Lands
Department vector map API).

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m kmb_planner.server   # http://localhost:8000
```

## Refresh data (after 05:30 HKT daily)

```bash
.venv/bin/python -m kmb_planner.sync_data
```

## Deploy (Render)

`render.yaml` blueprint: one free Python web service; the frontend is
pre-built into `web/dist` and served by the same process. Health check
at `/api/health` doubles as the keep-warm ping target.

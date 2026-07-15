# Billing Consumption — Fetch & Dashboard

A small **local web app** to pull Airia billing total-consumption data for a date range and
build an interactive cost/token dashboard — in one click, with a live fetch progress log.

This replaces the two-step manual flow (`total_consumption_paginated.sh` → `generate_dashboard.py`)
with a browser UI. It's a **local troubleshooting tool** (binds to `127.0.0.1` only).

## Layout

```
billing-tests/
├── conf/config.yaml      # single source of defaults (API url, dates, filters, branding)
├── src/                  # all code
│   ├── server.py             # the local web app (stdlib http server, streams progress)
│   ├── dashboard_core.py     # shared: load_config / fetch_all / aggregate / render_html
│   ├── generate_dashboard.py # offline CLI (build a dashboard from an existing JSON)
│   ├── index.html            # control-panel UI
│   └── dashboard_template.html
├── output/               # DATA ONLY — fetched *.json + generated dashboard *.html
├── .env                  # optional: AIRIA_API_KEY=ak-...
└── total_consumption_paginated.sh   # original script (still works standalone)
```

## Run it

```bash
cd src
python3 server.py
# → serving on http://127.0.0.1:8787
```

Open **http://127.0.0.1:8787** in your browser:

1. The form prefills Base URL + date range from `conf/config.yaml`.
2. Enter an **API key** — or leave it blank to use `AIRIA_API_KEY` from `.env` / the
   environment (the field shows whether a default key was found).
3. Pick the date range (and optional Advanced filters: user email, agent, model, page size).
4. Click **Fetch & Build Dashboard**. Watch the page-by-page fetch log, then the dashboard
   renders inline. Use **Open in new tab** / **Download** for the saved files.

Each run writes two files to `output/`:
`billing_totalconsumption_<timestamp>.json` (raw) and `billing_dashboard_<timestamp>.html`.

Requirements: Python 3.9+ and PyYAML (`pip install pyyaml`). No other dependencies, no build step.

## API key resolution

`UI field` → `AIRIA_API_KEY` (env or `.env`) → `api.api_key` literal in config (optional).
The key is never sent back to the browser or logged.

```bash
# .env  (optional)
AIRIA_API_KEY=ak-xxxxxxxx
```

## Offline CLI (no server)

Rebuild a dashboard from a JSON you already have:

```bash
cd src
python3 generate_dashboard.py --input ../output/billing_totalconsumption_20260624_105909.json
python3 generate_dashboard.py --input data.json --output ../output/report.html \
        --title "Acme Usage" --subtitle "Q2 2026"
```

## What the dashboard shows

KPIs (total cost, token vs image cost, input/output tokens, executions, success rate, avg
cost/exec), spend-over-time (stacked by model, toggle to tokens), cost by model / provider,
tokens by model, top pipelines / users, full sortable model & pipeline tables, and a
filterable/paginated execution detail table.

## Configuration (`conf/config.yaml`)

- `api` — `base_url`, `api_key_env` (env var name holding the key), `auth_header`.
- `query` — default `start_date` / `end_date`, `page_size`, optional filters, `descending`.
- `output.dir` — where fetched JSON + dashboards are written.
- `dashboard.branding` — title / subtitle / logo text.
- `dashboard.options` — `include_image_charges`, `attribute_image_cost_to_model`, and the
  top-N counts for the charts.
- `dashboard.fields` — key mappings (only change if your JSON schema differs from the Airia export).

## How cost is computed

Per-execution cost = sum of each step's `tokensCost` **+** `additionalCharges` (image generation).
The top-level `totalCost` field is only populated on a subset of rows, so it is **not** used.

## Notes

- The browser cannot call the Airia API directly (the `X-API-Key` header triggers a CORS
  preflight the API won't allow), so `server.py` proxies the call — that's the whole reason for
  the tiny backend. No Next.js / framework needed.
- The dashboard HTML loads Chart.js from a CDN, so viewing it needs internet on first open.

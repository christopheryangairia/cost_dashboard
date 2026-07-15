#!/usr/bin/env python3
"""
Offline CLI: build a dashboard from an already-fetched billing JSON.

Thin wrapper over dashboard_core. Use this when you have an output/*.json and just
want to (re)generate the HTML without running the server.

Examples:
    python3 generate_dashboard.py
    python3 generate_dashboard.py --input ../output/billing_totalconsumption_20260624_105909.json
    python3 generate_dashboard.py --input data.json --output ../output/report.html
    python3 generate_dashboard.py --title "Acme Usage" --subtitle "Q2 2026"
"""
import argparse
import json
import os
import sys

import dashboard_core as C


def _resolve(path, base):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def main():
    ap = argparse.ArgumentParser(description="Generate a consumption dashboard from a JSON export.")
    ap.add_argument("--config", default=C.DEFAULT_CONFIG, help="config YAML (default: conf/config.yaml)")
    ap.add_argument("--input", help="input JSON export (overrides dashboard.input in config)")
    ap.add_argument("--output", help="output HTML path (overrides dashboard.output in config)")
    ap.add_argument("--template", help="template HTML path")
    ap.add_argument("--title", help="override dashboard title")
    ap.add_argument("--subtitle", help="override dashboard subtitle")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        sys.exit(f"config not found: {args.config}")
    cfg = C.load_config(args.config)
    io = cfg["dashboard_io"]

    # input/output paths in the YAML are relative to the output dir by convention
    out_dir = _resolve(cfg["output"].get("dir", C.DEFAULT_OUTPUT_DIR), C.ROOT_DIR)

    input_path = args.input or (io.get("input") and _resolve(io["input"], out_dir))
    if not input_path:
        sys.exit("no input file — pass --input or set dashboard.input in the config")
    input_path = _resolve(input_path, os.getcwd())
    if not os.path.exists(input_path):
        sys.exit(f"input not found: {input_path}")

    output_path = args.output or (io.get("output") and _resolve(io["output"], out_dir)) \
        or os.path.join(out_dir, "billing_dashboard.html")
    output_path = _resolve(output_path, os.getcwd())

    template_path = args.template or (io.get("template") and _resolve(io["template"], C.SRC_DIR)) \
        or C.DEFAULT_TEMPLATE

    branding = dict(cfg["branding"])
    if args.title:
        branding["title"] = args.title
    if args.subtitle:
        branding["subtitle"] = args.subtitle

    with open(input_path) as f:
        raw = json.load(f)
    items = raw.get(cfg["fields"]["itemsPath"]) if isinstance(raw, dict) else raw
    if items is None and isinstance(raw, list):
        items = raw
    if not isinstance(items, list):
        sys.exit(f"could not find an item list at '{cfg['fields']['itemsPath']}'")

    data = C.aggregate(items, cfg["fields"], cfg["options"])
    html = C.render_html(data, template_path=template_path, branding=branding,
                         source_name=os.path.basename(input_path))
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    k = data["kpis"]
    print(f"✓ {os.path.relpath(output_path)}")
    print(f"  items: {k['execs']}  models: {k['models']}  pipelines: {k['pipelines']}  users: {k['users']}")
    print(f"  total cost: ${k['grandCost']}  (token ${k['tokenCost']} + image ${k['imageCost']})")
    print(f"  period: {data['meta']['dateMin']} → {data['meta']['dateMax']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Shared core for the billing-consumption dashboard.

Single source of truth used by both:
  - server.py            (the local web app)
  - generate_dashboard.py (the offline CLI)

Responsibilities:
  load_config()  -> read conf/config.yaml, normalize snake_case -> internal camelCase
  fetch_all()    -> cursor-paginate the Airia totalconsumption API (mirrors the .sh)
  aggregate()    -> roll executions up into the dashboard data dict
  render_html()  -> inject data + branding into dashboard_template.html

Standard library + PyYAML only.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import yaml

# python.org framework builds ship no system CA bundle (the "Install
# Certificates.command" step is often skipped), so urllib TLS verification fails
# with "unable to get local issuer certificate". Use certifi's bundle if present.
try:
    import certifi
    _CA_FILE = certifi.where()
except Exception:
    _CA_FILE = None


def _ssl_context(verify_tls=True):
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if _CA_FILE and os.path.exists(_CA_FILE):
        return ssl.create_default_context(cafile=_CA_FILE)
    return ssl.create_default_context()

# ---------------------------------------------------------------------------
# Paths (resolved relative to the project root, i.e. the parent of /src)
# ---------------------------------------------------------------------------
# Executions with no project association carry this all-zero GUID. Combined with
# a null executionSourceType, they are non-pipeline direct model / Gateway calls.
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "conf", "config.yaml")
DEFAULT_TEMPLATE = os.path.join(SRC_DIR, "dashboard_template.html")
DEFAULT_OUTPUT_DIR = os.path.join(ROOT_DIR, "output")


# ---------------------------------------------------------------------------
# Config loading + snake_case -> camelCase normalization
# ---------------------------------------------------------------------------
def _snake_to_camel(s):
    parts = s.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _camelize_keys(obj):
    """Recursively convert all dict keys from snake_case to camelCase."""
    if isinstance(obj, dict):
        return {_snake_to_camel(k): _camelize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camelize_keys(v) for v in obj]
    return obj


# Defaults so a partial config still works against the standard Airia export.
DEFAULT_FIELDS = {
    "itemsPath": "items", "createdAt": "createdAt", "inputTokens": "inputTokens",
    "outputTokens": "outputTokens", "totalCost": "totalCost", "success": "success",
    "pipelineName": "pipelineName", "projectId": "projectId", "userName": "userName",
    "executionSourceType": "executionSourceType", "additionalCharges": "additionalCharges",
    "executionId": "executionId",
    "steps": "steps",
    "step": {
        "modelDisplayName": "modelDisplayName", "modelName": "modelName", "provider": "provider",
        "inputTokens": "inputTokens", "outputTokens": "outputTokens", "tokensCost": "tokensCost",
        "modelCallDuration": "modelCallDuration",
    },
}
DEFAULT_OPTIONS = {
    "includeImageCharges": True, "attributeImageCostToModel": False,
    "topModelsStacked": 8, "topModelsBar": 12, "topPipelines": 15, "topUsers": 15,
}
DEFAULT_BRANDING = {
    "title": "Airia Consumption Dashboard",
    "subtitle": "model cost & token analytics", "logoText": "A",
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=DEFAULT_CONFIG):
    """Read the unified YAML config and return a normalized dict.

    Returns: {api, query, output, branding, options, fields, raw}
    - api/query/output keep their snake_case keys (used by the fetch layer).
    - branding/options/fields are camelCased to match aggregate()/template expectations.
    """
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    dash = raw.get("dashboard", {}) or {}
    branding = _deep_merge(DEFAULT_BRANDING, _camelize_keys(dash.get("branding", {})))
    options = _deep_merge(DEFAULT_OPTIONS, _camelize_keys(dash.get("options", {})))
    fields = _deep_merge(DEFAULT_FIELDS, _camelize_keys(dash.get("fields", {})))

    return {
        "api": raw.get("api", {}) or {},
        "query": raw.get("query", {}) or {},
        "output": raw.get("output", {}) or {},
        "dashboard_io": {"input": dash.get("input"), "output": dash.get("output"),
                         "template": dash.get("template")},
        "branding": branding,
        "options": options,
        "fields": fields,
        "raw": raw,
    }


def resolve_api_key(ui_key, cfg):
    """UI field -> env var (named by api.api_key_env) -> literal api.api_key in config."""
    if ui_key and ui_key.strip():
        return ui_key.strip()
    api = cfg.get("api", {})
    env_name = api.get("api_key_env") or "AIRIA_API_KEY"
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val
    return api.get("api_key")  # optional literal fallback; usually None


# ---------------------------------------------------------------------------
# API fetch — cursor pagination, mirrors total_consumption_paginated.sh
# ---------------------------------------------------------------------------
# query key (internal/snake) -> API query-param name (PascalCase)
_QUERY_PARAM_MAP = [
    ("user_email", "UserEmail"),
    ("agent_name", "AgentName"),
    ("model_name", "ModelName"),
    ("start_date", "StartDate"),
    ("end_date", "EndDate"),
    ("page_size", "PageSize"),
    ("include_total_count", "IncludeTotalCount"),
    ("descending", "Descending"),
]


def _qval(v):
    """Render a query value the way the API/.sh expects (lowercase bools)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def fetch_all(base_url, api_key, query, progress=None, timeout=120, max_pages=100000,
              verify_tls=True):
    """Page through the totalconsumption endpoint and return aggregated raw response.

    `query` is a dict using snake_case keys (user_email, start_date, page_size, ...).
    `progress(event)` is called once per page with a dict:
        {type:"page", page, items, collected, total, hasCursor}
    `verify_tls=False` skips certificate verification (local-tool escape hatch).
    Raises RuntimeError (with HTTP code + body snippet) on API/connection failure.
    """
    if not api_key:
        raise RuntimeError("No API key. Enter one in the form, or set the "
                           "AIRIA_API_KEY env var / .env, or api.api_key in config.")

    ctx = _ssl_context(verify_tls)

    base_params = {}
    for key, api_key_name in _QUERY_PARAM_MAP:
        v = query.get(key)
        if v is not None and v != "":
            base_params[api_key_name] = _qval(v)

    correlation_id = f"{int(time.time())}-{os.getpid()}"
    items, cursor, page, total = [], None, 1, 0

    while True:
        params = dict(base_params)
        if cursor:
            params["Cursor"] = cursor
        url = base_url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "x-correlation-id": correlation_id,
            "User-Agent": "Mozilla/5.0 (compatible; cost-dashboard/1.0)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                snippet = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} from API. {snippet} "
                               "(a 401 usually means the base_url is wrong)")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e.reason}")

        page_items = data.get("items") or []
        total = data.get("totalCount") or total
        items.extend(page_items)
        next_cursor = data.get("nextPageCursor")

        if progress:
            progress({"type": "page", "page": page, "items": len(page_items),
                      "collected": len(items), "total": total,
                      "hasCursor": bool(next_cursor)})

        if not next_cursor:
            break
        cursor = next_cursor
        page += 1
        if page > max_pages:
            break

    return {"items": items, "totalCount": total, "itemsCount": len(items),
            "pagesFetched": page}


# ---------------------------------------------------------------------------
# Aggregation — cost = sum(step tokensCost) + additionalCharges
# (top-level totalCost is sparsely populated and intentionally ignored)
# ---------------------------------------------------------------------------
def _parse_duration(s):
    if not s or not isinstance(s, str):
        return 0.0
    try:
        h, m, rest = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    except Exception:
        return 0.0


def aggregate(items, fields, options):
    sf = fields["step"]
    include_img = options["includeImageCharges"]
    attr_img = options["attributeImageCostToModel"]

    def num(x):
        return x if isinstance(x, (int, float)) else 0

    def exec_image_cost(it):
        ac = it.get(fields["additionalCharges"]) or {}
        return sum(num(v) for v in ac.values()) if isinstance(ac, dict) else 0.0

    daily = defaultdict(lambda: {"cost": 0.0, "tokenCost": 0.0, "imageCost": 0.0,
                                  "inTok": 0, "outTok": 0, "execs": 0})
    by_model = defaultdict(lambda: {"cost": 0.0, "inTok": 0, "outTok": 0, "calls": 0,
                                     "execs": 0, "provider": "", "dur": 0.0})
    by_provider = defaultdict(lambda: {"cost": 0.0, "inTok": 0, "outTok": 0, "calls": 0})
    by_pipeline = defaultdict(lambda: {"cost": 0.0, "tokenCost": 0.0, "imageCost": 0.0,
                                        "inTok": 0, "outTok": 0, "execs": 0, "fail": 0})
    by_user = defaultdict(lambda: {"cost": 0.0, "inTok": 0, "outTok": 0, "execs": 0})
    by_project = defaultdict(lambda: {"cost": 0.0, "inTok": 0, "outTok": 0, "execs": 0})
    by_source = defaultdict(lambda: {"cost": 0.0, "inTok": 0, "outTok": 0, "execs": 0})
    model_daily = defaultdict(lambda: defaultdict(float))

    tot_token_cost = tot_image_cost = 0.0
    tot_in = tot_out = 0
    n_success = n_fail = n_none = 0
    detail, dates = [], []

    for it in items:
        steps = it.get(fields["steps"]) or []
        tcost = sum(num(s.get(sf["tokensCost"])) for s in steps)
        icost = exec_image_cost(it) if include_img else 0.0
        cost = tcost + icost
        tot_token_cost += tcost
        tot_image_cost += icost

        in_tok = num(it.get(fields["inputTokens"]))
        out_tok = num(it.get(fields["outputTokens"]))
        tot_in += in_tok
        tot_out += out_tok

        succ = it.get(fields["success"])
        if succ is True:
            n_success += 1
        elif succ is False:
            n_fail += 1
        else:
            n_none += 1

        created = it.get(fields["createdAt"]) or ""
        day = created[:10]
        dates.append(created)
        d = daily[day]
        d["cost"] += cost; d["tokenCost"] += tcost; d["imageCost"] += icost
        d["inTok"] += in_tok; d["outTok"] += out_tok; d["execs"] += 1

        primary_model, primary_prov, best = "", "", -1
        models_seen = set()
        for s in steps:
            mdl = s.get(sf["modelDisplayName"]) or s.get(sf["modelName"]) or "Unknown"
            prov = s.get(sf["provider"]) or "Unknown"
            si = num(s.get(sf["inputTokens"]))
            so = num(s.get(sf["outputTokens"]))
            sc = num(s.get(sf["tokensCost"]))
            bm = by_model[mdl]
            bm["cost"] += sc; bm["inTok"] += si; bm["outTok"] += so
            bm["calls"] += 1; bm["provider"] = prov
            bm["dur"] += _parse_duration(s.get(sf["modelCallDuration"]))
            bp = by_provider[prov]
            bp["cost"] += sc; bp["inTok"] += si; bp["outTok"] += so; bp["calls"] += 1
            model_daily[mdl][day] += sc
            models_seen.add(mdl)
            if (si + so) > best:
                best, primary_model, primary_prov = si + so, mdl, prov
        # count this execution once per distinct model it touched
        for m in models_seen:
            by_model[m]["execs"] += 1

        if attr_img and icost and primary_model:
            by_model[primary_model]["cost"] += icost
            prov = by_model[primary_model]["provider"]
            if prov:
                by_provider[prov]["cost"] += icost
            model_daily[primary_model][day] += icost

        pname = it.get(fields["pipelineName"]) or "(none)"
        bp = by_pipeline[pname]
        bp["cost"] += cost; bp["tokenCost"] += tcost; bp["imageCost"] += icost
        bp["inTok"] += in_tok; bp["outTok"] += out_tok; bp["execs"] += 1
        if succ is False:
            bp["fail"] += 1

        uname = it.get(fields["userName"]) or "(unknown)"
        bu = by_user[uname]
        bu["cost"] += cost; bu["inTok"] += in_tok; bu["outTok"] += out_tok; bu["execs"] += 1

        pid = it.get(fields["projectId"]) or "(none)"
        bpr = by_project[pid]
        bpr["cost"] += cost; bpr["inTok"] += in_tok; bpr["outTok"] += out_tok; bpr["execs"] += 1

        src = it.get(fields["executionSourceType"])
        if not src:
            # Null source + no project = direct model spend not tied to any pipeline.
            src = "Direct / Gateway" if pid in (None, "", "(none)", ZERO_GUID) else "Unknown"
        bs = by_source[src]
        bs["cost"] += cost; bs["inTok"] += in_tok; bs["outTok"] += out_tok; bs["execs"] += 1

        detail.append({
            "id": it.get(fields["executionId"]) or "",
            "t": created[:19].replace("T", " "),
            "pipe": pname, "model": primary_model or "—", "prov": primary_prov or "—",
            "src": src, "in": in_tok, "out": out_tok, "cost": round(cost, 6),
            "ok": succ, "user": uname,
        })

    grand_cost = tot_token_cost + tot_image_cost

    def rnd(o):
        for kk in ("cost", "tokenCost", "imageCost", "dur"):
            if kk in o:
                o[kk] = round(o[kk], 6)
        return o

    days = sorted(daily.keys())
    timeseries = [{"day": d, **rnd(dict(daily[d]))} for d in days]
    models = sorted(({"name": k, **rnd(dict(v))} for k, v in by_model.items()),
                    key=lambda x: x["cost"], reverse=True)
    providers = sorted(({"name": k, **rnd(dict(v))} for k, v in by_provider.items()),
                       key=lambda x: x["cost"], reverse=True)
    pipelines = sorted(({"name": k, **rnd(dict(v))} for k, v in by_pipeline.items()),
                       key=lambda x: x["cost"], reverse=True)
    users = sorted(({"name": k, **rnd(dict(v))} for k, v in by_user.items()),
                   key=lambda x: x["cost"], reverse=True)
    projects = sorted(({"id": k, **rnd(dict(v))} for k, v in by_project.items()),
                      key=lambda x: x["cost"], reverse=True)
    sources = sorted(({"name": k, **rnd(dict(v))} for k, v in by_source.items()),
                     key=lambda x: x["cost"], reverse=True)

    top_models = [m["name"] for m in models[:options["topModelsStacked"]]]
    stacked = {"days": days, "series": []}
    other_by_day = defaultdict(float)
    for mdl, dd in model_daily.items():
        if mdl in top_models:
            stacked["series"].append({"name": mdl,
                                       "data": [round(dd.get(d, 0.0), 6) for d in days]})
        else:
            for d, c in dd.items():
                other_by_day[d] += c
    if other_by_day:
        stacked["series"].append({"name": "Other",
                                   "data": [round(other_by_day.get(d, 0.0), 6) for d in days]})
    order = {n: i for i, n in enumerate(top_models)}
    stacked["series"].sort(key=lambda s: order.get(s["name"], 999))

    valid_dates = [d for d in dates if d]
    return {
        "meta": {
            "dateMin": min(valid_dates)[:10] if valid_dates else "—",
            "dateMax": max(valid_dates)[:10] if valid_dates else "—",
        },
        "kpis": {
            "grandCost": round(grand_cost, 4), "tokenCost": round(tot_token_cost, 4),
            "imageCost": round(tot_image_cost, 4),
            "totalIn": tot_in, "totalOut": tot_out, "totalTokens": tot_in + tot_out,
            "execs": len(items), "success": n_success, "fail": n_fail, "none": n_none,
            "pipelines": len(by_pipeline), "users": len(by_user),
            "projects": len(by_project), "models": len(by_model),
            "avgCostPerExec": round(grand_cost / max(1, len(items)), 6),
        },
        "options": {
            "topModelsBar": options["topModelsBar"],
            "topPipelines": options["topPipelines"],
            "topUsers": options["topUsers"],
        },
        "timeseries": timeseries, "stacked": stacked, "models": models,
        "providers": providers, "pipelines": pipelines, "users": users,
        "projects": projects, "sources": sources, "detail": detail,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render_html(data, template_path=DEFAULT_TEMPLATE, branding=None, source_name=""):
    branding = branding or DEFAULT_BRANDING
    data = dict(data)
    meta = dict(data.get("meta", {}))
    meta["generated"] = time.strftime("%Y-%m-%d %H:%M")
    meta["source"] = source_name
    data["meta"] = meta

    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    with open(template_path) as f:
        tpl = f.read()
    return (tpl.replace("__TITLE__", branding.get("title", ""))
               .replace("__SUBTITLE__", branding.get("subtitle", ""))
               .replace("__LOGO__", branding.get("logoText", "A"))
               .replace("__DATA__", payload))

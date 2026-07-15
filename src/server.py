#!/usr/bin/env python3
"""
Local web app for the billing-consumption dashboard.

Serves a control-panel UI, proxies the Airia totalconsumption API (so the browser
never deals with CORS / the API key), streams per-page fetch progress live, then
builds and serves the dashboard.

Run:
    cd src && python3 server.py
    # then open http://127.0.0.1:8787

Standard library + PyYAML only. Bound to localhost — this is a local tool.
"""
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import dashboard_core as C

HOST = "127.0.0.1"
PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))
INDEX_HTML = os.path.join(C.SRC_DIR, "index.html")
OUTPUT_DIR = os.path.join(C.ROOT_DIR, "output")


# ---------------------------------------------------------------------------
# .env loader (tiny KEY=VALUE reader; no python-dotenv dependency)
# ---------------------------------------------------------------------------
def load_dotenv(path=os.path.join(C.ROOT_DIR, ".env")):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)


def norm_date(s):
    """datetime-local ('2026-04-14T00:00') -> API form ('2026-04-14T00:00:00Z')."""
    if not s:
        return s
    s = s.strip()
    if not s or "T" not in s:
        return s
    if s.endswith("Z"):
        return s
    date, _, t = s.partition("T")
    if t.count(":") == 1:
        t += ":00"
    return f"{date}T{t}Z"


# Loaded once at startup
CFG = C.load_config()
load_dotenv()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # connection-close framing — simplest for streaming
    server_version = "BillingDash/1.0"

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def log_message(self, fmt, *args):  # quieter, single-line console logs
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- routing ----
    def do_GET(self):
        route = urlparse(self.path).path
        try:
            if route == "/" or route == "/index.html":
                if not os.path.exists(INDEX_HTML):
                    return self._send(500, "index.html missing", "text/plain")
                return self._send_file(INDEX_HTML, "text/html; charset=utf-8")

            if route == "/api/defaults":
                return self._send(200, json.dumps(self._defaults()))

            if route.startswith("/output/"):
                return self._serve_output(unquote(route[len("/output/"):]))

            return self._send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._send(500, json.dumps({"error": "server error"}))
            except Exception:
                pass

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/run":
            return self._run()
        return self._send(404, "not found", "text/plain")

    # ---- /api/defaults ----
    def _defaults(self):
        q = CFG["query"]
        api = CFG["api"]
        env_name = api.get("api_key_env") or "AIRIA_API_KEY"
        has_key = bool(os.environ.get(env_name) or api.get("api_key"))
        return {
            "base_url": api.get("base_url", ""),
            "start_date": q.get("start_date", ""),
            "end_date": q.get("end_date", ""),
            "user_email": q.get("user_email", ""),
            "agent_name": q.get("agent_name", ""),
            "model_name": q.get("model_name", ""),
            "page_size": q.get("page_size", 100),
            "branding": CFG["branding"],
            "has_default_key": has_key,
            "key_source": env_name,
        }

    # ---- /output/<file> (sanitized) ----
    def _serve_output(self, name):
        name = os.path.basename(name)  # strip any path components
        path = os.path.join(OUTPUT_DIR, name)
        if not os.path.isfile(path):
            return self._send(404, "file not found", "text/plain")
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else \
                "application/json" if name.endswith(".json") else "application/octet-stream"
        return self._send_file(path, ctype)

    # ---- /api/run (streaming NDJSON) ----
    def _run(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"type": "error", "message": "bad JSON body"}))

        # streaming response: write NDJSON lines as we go, flush each one
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            try:
                self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                raise

        try:
            base_url = (req.get("base_url") or CFG["api"].get("base_url", "")).strip()
            api_key = C.resolve_api_key(req.get("api_key"), CFG)

            # build query: config defaults overlaid with request values
            q = dict(CFG["query"])
            for k in ("user_email", "agent_name", "model_name", "start_date", "end_date", "page_size"):
                if k in req and req[k] not in (None, ""):
                    q[k] = req[k]
            q["start_date"] = norm_date(q.get("start_date"))
            q["end_date"] = norm_date(q.get("end_date"))

            emit({"type": "start", "base_url": base_url,
                  "start_date": q.get("start_date"), "end_date": q.get("end_date"),
                  "page_size": q.get("page_size")})
            print(f"▶ run: {q.get('start_date')} → {q.get('end_date')} "
                  f"page_size={q.get('page_size')} url={base_url}")

            def progress(ev):
                print(f"  page {ev['page']}: +{ev['items']} "
                      f"({ev['collected']}/{ev['total']})")
                emit(ev)

            # TLS verification on by default; allow opt-out via config or request
            verify_tls = CFG["api"].get("verify_tls", True)
            if "verify_tls" in req:
                verify_tls = bool(req["verify_tls"])

            result = C.fetch_all(base_url, api_key, q, progress=progress,
                                 verify_tls=verify_tls)
            items = result["items"]
            emit({"type": "fetched", "items": result["itemsCount"],
                  "total": result["totalCount"], "pages": result["pagesFetched"]})

            # save raw JSON (same shape as the .sh output)
            ts = time.strftime("%Y%m%d_%H%M%S")
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            json_name = f"billing_totalconsumption_{ts}.json"
            with open(os.path.join(OUTPUT_DIR, json_name), "w") as f:
                json.dump(result, f, separators=(",", ":"))
            emit({"type": "saved", "json": json_name})

            # aggregate + render dashboard
            emit({"type": "building"})
            data = C.aggregate(items, CFG["fields"], CFG["options"])
            html = C.render_html(data, branding=CFG["branding"], source_name=json_name)
            html_name = f"billing_dashboard_{ts}.html"
            with open(os.path.join(OUTPUT_DIR, html_name), "w") as f:
                f.write(html)

            emit({"type": "done",
                  "dashboard": f"/output/{html_name}",
                  "json": f"/output/{json_name}",
                  "kpis": data["kpis"], "meta": data["meta"]})
            print(f"✓ done: {html_name}  cost=${data['kpis']['grandCost']}")
        except BrokenPipeError:
            print("  (client disconnected)")
        except Exception as e:
            traceback.print_exc()
            try:
                emit({"type": "error", "message": str(e)})
            except Exception:
                pass


def main():
    if not os.path.exists(INDEX_HTML):
        print(f"warning: {INDEX_HTML} not found — UI route will 500", file=sys.stderr)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print("  Billing Consumption Dashboard — local server")
    print("=" * 60)
    print(f"  serving on {url}")
    print(f"  config:    {C.DEFAULT_CONFIG}")
    print(f"  output:    {OUTPUT_DIR}")
    print(f"  api url:   {CFG['api'].get('base_url')}")
    env_name = CFG['api'].get('api_key_env') or 'AIRIA_API_KEY'
    print(f"  api key:   {'found via ' + env_name if os.environ.get(env_name) else 'NOT set (enter in UI)'}")
    print(f"\n  Open {url} in your browser.  Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()

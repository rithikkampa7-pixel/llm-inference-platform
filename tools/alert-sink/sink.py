"""Minimal Alertmanager webhook receiver.

Prints each delivered alert to stdout so `docker compose logs -f alert-sink`
shows notifications arriving. This exists so the repo demonstrates the full
path -- rule fires, Alertmanager routes, receiver is notified -- without
needing a PagerDuty or Slack secret.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        route = parse_qs(urlparse(self.path).query).get("route", ["default"])[0]
        for alert in payload.get("alerts", []):
            labels = alert.get("labels", {})
            print(
                "ALERT route={route} status={status} name={name} "
                "severity={severity} burn={burn} summary={summary}".format(
                    route=route,
                    status=alert.get("status", "?"),
                    name=labels.get("alertname", "?"),
                    severity=labels.get("severity", "?"),
                    burn=labels.get("burn_rate", "-"),
                    summary=alert.get("annotations", {}).get("summary", ""),
                ),
                flush=True,
            )

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        # Suppress per-request access logging; the ALERT lines are the signal.
        pass


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"alert-sink listening on :{port}", flush=True)
    HTTPServer(("", port), Handler).serve_forever()

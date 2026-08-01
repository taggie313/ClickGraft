#!/usr/bin/env python3
"""Accepts crash/bug reports from ClickGraft and writes them to a file.

Deliberately tiny and deliberately dumb. It stores what it is given and does
nothing else: no parsing, no execution, no outbound calls, no database. The
whole attack surface is "someone fills a disk", which nginx caps.

The user has already read the exact text before their copy of ClickGraft sends
it — see sendReport() in ClickGraft.swift. Nothing arrives here that was not
shown to a person first.
"""
import datetime
import http.server
import os
import re

REPORTS = "/srv/reports"
MAX_BYTES = 256 * 1024

# Belt and braces: the client scrubs home directories before sending, but a
# report is written to disk here and kept, so it gets scrubbed again on arrival.
# Doing it in one place only means one bug away from storing someone's name.
HOME_RE = re.compile(rb"/Users/[^/\s\"']+")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "clickgraft-collector"

    def do_POST(self):                      # noqa: N802
        if self.path.rstrip("/") != "/report":
            return self._reply(404, b"no\n")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._reply(400, b"bad length\n")
        if n <= 0 or n > MAX_BYTES:
            return self._reply(413, b"too big\n")

        body = HOME_RE.sub(b"/Users/~", self.rfile.read(n))

        os.makedirs(REPORTS, exist_ok=True)
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        # Country only, never the address. Enough to spot "every report is from
        # one place", not enough to identify a reporter.
        cc = re.sub(r"[^A-Za-z]", "", self.headers.get("CF-IPCountry", "") or "")[:2]
        with open(os.path.join(REPORTS, f"report-{stamp}-{cc or 'xx'}.txt"), "wb") as f:
            f.write(body)
        self._reply(200, b"thanks\n")

    def do_GET(self):                       # noqa: N802
        self._reply(200, b"ok\n") if self.path == "/healthz" else self._reply(404, b"no\n")

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass                                # nginx already logs; don't double up


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

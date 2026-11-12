#!/usr/bin/env python3
"""Static file server with HTTP Range support, for the farmland map.

Python's SimpleHTTPRequestHandler ignores the Range header and returns the
whole file with a 200. PMTiles reads its index and tiles with byte-range
requests, so without this every tile fetch would download the whole archive.
This handler answers ranges with 206 Partial Content.

Usage: python serve.py [--port 8787]
Then open http://127.0.0.1:8787/web/
"""

from __future__ import annotations

import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        m = RANGE_RE.match(rng.strip()) if rng else None
        if not m or os.path.isdir(path):
            return super().send_head()
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        start_s, end_s = m.groups()
        if start_s == "":                       # suffix range: last N bytes
            start, end = max(0, size - int(end_s or 0)), size - 1
        else:
            start, end = int(start_s), int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        return _Limited(f, end - start + 1)


class _Limited:
    """File wrapper that yields at most n bytes, so copyfile stops there."""

    def __init__(self, fh, n):
        self.fh, self.n = fh, n

    def read(self, size=-1):
        if self.n <= 0:
            return b""
        if size is None or size < 0 or size > self.n:
            size = self.n
        b = self.fh.read(size)
        self.n -= len(b)
        return b

    def close(self):
        self.fh.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), partial(RangeHandler, directory=str(ROOT)))
    srv.daemon_threads = True
    print(f"open http://127.0.0.1:{args.port}/web/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Serve the Agora outbound-call dashboard with a local audio-download proxy.

Browser-side `fetch()` cannot zip cross-origin OSS recordings (CORS preflight).
This server exposes POST `/audio-zip` so the HTML can hand off a URL list and
get back a single zip — the download happens server-side via urllib, bypassing
CORS entirely.

Usage:
  serve_dashboard.py <input.csv-or-xlsx> [--port 8765]

The server stays running until Ctrl+C.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import io
import json
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Single-request hard cap on audio file count. Each fetched file is held in memory
# briefly (one per worker), but if a user accidentally requests 10k+ recordings we
# refuse rather than risk OOM or hanging the server for 30+ minutes.
MAX_FILES_PER_REQUEST = 3000

# Parallel workers for OSS fetches. 16 saturates typical home/office bandwidth.
FETCH_WORKERS = 16


class _NonSeekableWriter:
    """Forces zipfile into streaming mode (data descriptors) by hiding tell/seek.

    Lets us pipe zip bytes straight into HTTP wfile so the full archive never
    sits in memory at once — peak RSS stays at ~ one file per worker.
    """
    __slots__ = ("fp",)
    def __init__(self, fp): self.fp = fp
    def write(self, b): return self.fp.write(b)
    def flush(self): self.fp.flush()

# Run as `python -m` style or direct execution.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from build_dashboard import load_table, enrich, build_html  # noqa: E402


HTML_BYTES = b""  # populated in main()


def _fetch_one(task: tuple[str, str]) -> tuple[str, str, bytes | None, str | None]:
    """Pull one audio file. Returns (zip_path, url, data, error)."""
    name, url = task
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agora-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (name, url, resp.read(), None)
    except Exception as e:  # noqa: BLE001
        return (name, url, None, str(e))


def _encode_header_safe(value: str) -> str:
    """Best-effort RFC 5987 encoding for headers containing non-ASCII (filenames).

    Falls back to a plain ASCII placeholder so the response itself never fails to
    send. The client already controls the actual saved filename via a.download.
    """
    if "filename" not in value.lower():
        return urllib.parse.quote(value)
    # Strip the inner quoted filename and add filename*= UTF-8 percent-encoded.
    # e.g. attachment; filename="..." → attachment; filename="export.zip"; filename*=UTF-8''...
    encoded = urllib.parse.quote(value.split("filename=", 1)[-1].strip('"'), safe="")
    return f"attachment; filename=\"export.zip\"; filename*=UTF-8''{encoded}"


class Handler(http.server.BaseHTTPRequestHandler):

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            # HTTP headers must encode latin-1; non-ASCII (e.g. Chinese filenames in
            # Content-Disposition) need RFC 5987 percent-encoding via filename*=.
            try:
                v.encode("latin-1")
                self.send_header(k, v)
            except UnicodeEncodeError:
                self.send_header(k, _encode_header_safe(v))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_BYTES, "text/html; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if self.path != "/audio-zip":
            self._send(404, b"not found", "text/plain")
            return
        try:
            body = self._parse_post_body()
            zip_filename = body.get("zip_filename", "agora-export.zip")
            groups = body.get("groups", [])
            total_files = sum(len(g.get("files", [])) for g in groups)
            if total_files > MAX_FILES_PER_REQUEST:
                msg = (f"refused: {total_files} files > {MAX_FILES_PER_REQUEST} limit "
                       f"per request. Filter the dashboard further (eg. by duration "
                       f"or turn count) and try again.").encode("utf-8")
                self._send(413, msg, "text/plain; charset=utf-8")
                return
            self._stream_zip(zip_filename, groups, total_files)
        except Exception:
            traceback.print_exc()
            # Try to send a clean error, but if we already started streaming we
            # just close the connection and let the client surface the failure.
            try:
                self._send(500, traceback.format_exc().encode(), "text/plain")
            except Exception:
                pass

    def _parse_post_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            return json.loads(raw.decode("utf-8"))
        # form-encoded (used by hidden-iframe download to avoid client-side blob buffering)
        params = urllib.parse.parse_qs(raw.decode("utf-8"))
        payload = params.get("payload", [""])[0]
        return json.loads(payload) if payload else {}

    def _stream_zip(self, zip_filename: str, groups: list[dict], total_files: int) -> None:
        """Stream a zip archive directly into the HTTP response body.

        Memory profile: ~ FETCH_WORKERS × avg_file_size (e.g. 16 × 1 MB = 16 MB)
        regardless of total archive size, because each audio is written + freed
        immediately as its fetch completes.
        """
        # Single Content-Disposition header: ASCII fallback + RFC 5987 unicode filename.
        # RFC 6266 §5: if both filename and filename* are present, recipient MUST
        # prefer filename*. Sending both in one header is the spec-compliant way to
        # support non-ASCII names without breaking older clients.
        encoded = urllib.parse.quote(zip_filename, safe="")
        disp_header = (
            f'attachment; filename="agora-export.zip"; '
            f"filename*=UTF-8''{encoded}"
        )

        print(f"[audio-zip] START · {total_files} files · streaming to client", flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        # No Content-Length → HTTP/1.0 connection-close framing (handler default).
        # Browser still streams the body to its download manager.
        self.send_header("Content-Disposition", disp_header)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        # Flush headers so the browser commits to "this is a download" before we
        # start the long parallel fetch (otherwise the iframe / download UI can
        # silently abandon the request).
        try:
            self.wfile.flush()
        except Exception:
            pass

        # Pre-collect tasks
        tasks: list[tuple[str, str]] = []  # (zip_path, url)
        inserts: list[tuple[str, bytes]] = []  # (zip_path, xlsx bytes)
        for g in groups:
            folder = g.get("folder", "")
            prefix = (folder + "/") if folder else ""
            if g.get("xlsx_b64"):
                inserts.append((prefix + g.get("xlsx_filename", "export.xlsx"),
                                base64.b64decode(g["xlsx_b64"])))
            for item in g.get("files", []):
                tasks.append((prefix + item["filename"], item["url"]))

        failed: list[tuple[str, str, str]] = []
        t0 = time.time()

        with zipfile.ZipFile(_NonSeekableWriter(self.wfile), "w",
                             zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for name, data in inserts:
                zf.writestr(name, data)

            if tasks:
                workers = min(FETCH_WORKERS, max(1, len(tasks)))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    fut_to_task = {ex.submit(_fetch_one, t): t for t in tasks}
                    for fut in as_completed(fut_to_task):
                        name, url, data, err = fut.result()
                        if data is not None:
                            zf.writestr(name, data)
                        else:
                            failed.append((name, url, err or "empty response"))

            if failed:
                rows = ["Path\tURL\tError"]
                rows += [f"{n}\t{u}\t{e}" for n, u, e in failed]
                zf.writestr("failed_downloads.txt", "\n".join(rows))

        elapsed = time.time() - t0
        print(f"[audio-zip] DONE  · streamed {total_files - len(failed)}/{total_files} files "
              f"in {elapsed:.1f}s, {len(failed)} failed", flush=True)

    # _build_zip removed — _stream_zip now writes directly into the HTTP response,
    # so we never hold the full archive in memory.

    def log_message(self, format, *args):  # noqa: A002
        # Quieter default logging; surface only errors via traceback prints above.
        return


def make_server(handler_cls, preferred: int) -> tuple[socketserver.TCPServer, int]:
    """Try preferred port first; on any failure, let the OS assign one (port=0).

    Binding the actual HTTPServer here (not a probe socket) eliminates TOCTOU
    where the port becomes occupied between probe and serve.
    """
    class ReusableTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    for candidate in [preferred, 0]:  # 0 = OS-assigned
        try:
            httpd = ReusableTCPServer(("127.0.0.1", candidate), handler_cls)
            return httpd, httpd.server_address[1]
        except OSError:
            continue
    raise RuntimeError("Could not bind to any local port")


def main() -> int:
    p = argparse.ArgumentParser(description="Serve Agora dashboard locally with audio-zip proxy")
    p.add_argument("input", help="CSV or XLSX path")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    args = p.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    df = load_table(inp)
    enriched = enrich(df)
    html = build_html(enriched, source=inp.name)
    global HTML_BYTES
    HTML_BYTES = html.encode("utf-8")

    httpd, port = make_server(Handler, args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Agora dashboard → {url}")
    if port != args.port:
        print(f"  (requested port {args.port} was busy; using OS-assigned {port})")
    print(f"  source: {inp.name}  (rows: {len(df)})")
    print("  Ctrl+C to stop")

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

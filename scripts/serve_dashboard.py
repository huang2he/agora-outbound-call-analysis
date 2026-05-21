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
import os
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

# LLM config — read once at startup from env vars or ~/.config/<skill>/env. Kept
# outside the repo so the API key never gets committed.
LLM_CONFIG_PATHS = [
    Path.home() / ".config" / "agora-outbound-call-analysis" / "env",
    Path.home() / ".config" / "agora-skill" / "env",
]


def _load_llm_env() -> dict[str, str]:
    """Read KEY=VALUE lines from the config files (last writer wins) and overlay
    real env vars on top. Returns dict — never raises if files are missing."""
    out: dict[str, str] = {}
    for path in LLM_CONFIG_PATHS:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as e:  # noqa: BLE001
            print(f"[llm] failed to read {path}: {e}", flush=True)
    for k in ("DASHSCOPE_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        if k in os.environ:
            out[k] = os.environ[k]
    return out


LLM_ENV = _load_llm_env()
LLM_BASE_URL = LLM_ENV.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = LLM_ENV.get("LLM_MODEL", "qwen-plus")
LLM_API_KEY = LLM_ENV.get("LLM_API_KEY") or LLM_ENV.get("DASHSCOPE_API_KEY") or ""

LLM_WORKERS = 16  # higher concurrency = ~2x throughput vs 8; rate-limited by DashScope quotas

# Auto-job state — set once on server start, modified by background workers.
# Browser polls /llm-intent-status to render progress + final results.
LLM_JOB = {
    "status": "idle",   # idle | running | done | error | skipped
    "total": 0,
    "done": 0,
    "results": [],
    "model": "",
    "started_at": None,
    "elapsed_s": 0,
    "error": None,
}
LLM_JOB_LOCK = threading.Lock()

INTENT_PROMPT_SYSTEM = (
    "你是外呼通话质检员。给你一通已经被规则判为「购车意向=是」的通话，"
    "你要进一步判断这个客户是真有购买意向还是只是客气敷衍。"
)

INTENT_PROMPT_TEMPLATE = """【收集到的结构化字段】
{structured}

【完整 transcript】
{transcript}

判断规则：
- "真意向"：客户主动给具体信息（品牌/车型/时间/城市/预算/手机尾号等），主动问价格/优惠/4S 店地址，明确同意接 4S 店回电
- "假意向"：客户只有"好的""考虑一下""有需要会联系""挺好的"这种敷衍话术，没有给出可验证的具体动作或承诺
- "模糊"：既没明显敷衍也没明确承诺，难以判断

只输出 JSON，schema 严格如下，不要加任何 markdown 围栏：
{{"verdict": "真意向" | "假意向" | "模糊", "reason": "≤30 字依据", "evidence": "客户原话证据 ≤ 50 字"}}
"""


def _judge_one(call: dict) -> dict:
    """One LLM judgement keyed by call_id. Used by both /llm-intent-check
    (synchronous) and the background auto-job."""
    r = _call_llm(call.get("transcript", ""), call.get("structured"))
    return {"call_id": call.get("call_id", ""), **r}


def kickoff_intent_job(df_enriched) -> None:
    """Fire-and-forget LLM judgement of every intent call. Runs in a daemon
    thread so the HTTP server can start serving immediately."""
    intent_df = df_enriched[df_enriched["_intent"]]
    n = len(intent_df)
    with LLM_JOB_LOCK:
        LLM_JOB["model"] = LLM_MODEL
        LLM_JOB["results"] = []
        LLM_JOB["done"] = 0
        LLM_JOB["total"] = n
        LLM_JOB["started_at"] = time.time()
        LLM_JOB["elapsed_s"] = 0
        LLM_JOB["error"] = None
        if n == 0:
            LLM_JOB["status"] = "done"
            return
        if not LLM_API_KEY:
            LLM_JOB["status"] = "skipped"
            LLM_JOB["error"] = ("LLM_API_KEY 未配置。在 "
                                "~/.config/agora-outbound-call-analysis/env 里写 "
                                "DASHSCOPE_API_KEY=... 然后重启服务即可启用自动分析。")
            return
        LLM_JOB["status"] = "running"

    calls = [
        {
            "call_id": r.get("Call ID", ""),
            "transcript": transcript_readable(r["_transcript"]),
            "structured": r["_structured"],
        }
        for _, r in intent_df.iterrows()
    ]

    def runner():
        t0 = time.time()
        try:
            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as ex:
                futures = [ex.submit(_judge_one, c) for c in calls]
                for fut in as_completed(futures):
                    res = fut.result()
                    with LLM_JOB_LOCK:
                        LLM_JOB["results"].append(res)
                        LLM_JOB["done"] = len(LLM_JOB["results"])
                        LLM_JOB["elapsed_s"] = round(time.time() - t0, 1)
            with LLM_JOB_LOCK:
                LLM_JOB["status"] = "done"
                LLM_JOB["elapsed_s"] = round(time.time() - t0, 1)
            print(f"[llm-intent-auto] DONE {LLM_JOB['done']}/{LLM_JOB['total']} "
                  f"in {LLM_JOB['elapsed_s']}s · model={LLM_MODEL}", flush=True)
        except Exception as e:  # noqa: BLE001
            with LLM_JOB_LOCK:
                LLM_JOB["status"] = "error"
                LLM_JOB["error"] = str(e)[:300]
            traceback.print_exc()

    threading.Thread(target=runner, daemon=True, name="llm-intent-auto").start()
    print(f"[llm-intent-auto] START · {n} intent calls · model={LLM_MODEL} "
          f"· {LLM_WORKERS} workers", flush=True)


def _call_llm(transcript: str, structured: dict | None) -> dict:
    """One LLM call. Returns parsed JSON dict or {error: ...} on failure."""
    if not LLM_API_KEY:
        return {"error": "LLM_API_KEY 未配置 (~/.config/agora-outbound-call-analysis/env)"}

    struct_str = json.dumps(structured, ensure_ascii=False, indent=2) if structured else "(无)"
    # Cap transcript size to keep prompt reasonable. ~3000 chars covers most outbound calls.
    trans = transcript[:3000] + ("\n...(已截断)" if len(transcript) > 3000 else "")
    user_msg = INTENT_PROMPT_TEMPLATE.format(structured=struct_str, transcript=trans)

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": INTENT_PROMPT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        method="POST",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return {"error": f"HTTP {e.code}: {err_body[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


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

from build_dashboard import load_table, enrich, build_html, transcript_readable  # noqa: E402


HTML_BYTES = b""  # populated in main()
ENRICHED_DF = None  # 给 /reload-html 用，保留 dataframe 引用以热重新 build HTML
CURRENT_SOURCE_PATH = ""


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
        elif self.path == "/reload-html":
            # 热重载: 只 reload HTML 构造相关模块, 不要 reload llm_fail_analysis -
            # 那会把模块级 LLM_FAIL_JOB 重置成 idle, 丢掉已跑完的 LLM 结果.
            try:
                import importlib
                import lib.agent_kda as _kda
                import build_dashboard as _bd
                importlib.reload(_kda)
                importlib.reload(_bd)
                g = globals()
                _html = _bd.build_html(g["ENRICHED_DF"], source=g["CURRENT_SOURCE_PATH"])
                g["HTML_BYTES"] = _html.encode("utf-8")
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:  # noqa: BLE001
                msg = json.dumps({"ok": False, "error": str(e)[:300]},
                                 ensure_ascii=False).encode("utf-8")
                self._send(500, msg, "application/json; charset=utf-8")
        elif self.path == "/llm-intent-status":
            with LLM_JOB_LOCK:
                snap = {
                    "status": LLM_JOB["status"],
                    "total": LLM_JOB["total"],
                    "done": LLM_JOB["done"],
                    "results": list(LLM_JOB["results"]),
                    "elapsed_s": LLM_JOB["elapsed_s"],
                    "error": LLM_JOB["error"],
                    "model": LLM_JOB["model"],
                }
            payload = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/llm-fail-status":
            try:
                from lib import llm_fail_analysis as F
            except ImportError:
                from scripts.lib import llm_fail_analysis as F
            payload = json.dumps(F.status_snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/llm-verify-status":
            try:
                from lib import llm_full_conv_verify as V
            except ImportError:
                from scripts.lib import llm_full_conv_verify as V
            payload = json.dumps(V.status_snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if self.path == "/audio-zip":
            return self._post_audio_zip()
        if self.path == "/llm-intent-check":
            return self._post_llm_intent()
        self._send(404, b"not found", "text/plain")

    def _post_audio_zip(self):
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
            try:
                self._send(500, traceback.format_exc().encode(), "text/plain")
            except Exception:
                pass

    def _post_llm_intent(self):
        """Run the intent-judgement LLM over a list of calls. Returns ALL results
        in one JSON response (no streaming for now — 8 parallel workers keep wall
        time under a minute for typical batches of 20-200 intent calls)."""
        try:
            body = self._parse_post_body()
            calls = body.get("calls", [])
            if not LLM_API_KEY:
                self._send(503, json.dumps({
                    "error": "LLM API key missing. Set DASHSCOPE_API_KEY in "
                             "~/.config/agora-outbound-call-analysis/env and restart the server.",
                }, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            if not calls:
                self._send(400, b'{"error":"empty calls"}', "application/json")
                return

            def judge(c):
                r = _call_llm(c.get("transcript", ""), c.get("structured"))
                return {"call_id": c.get("call_id", ""), **r}

            t0 = time.time()
            results = []
            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as ex:
                futures = [ex.submit(judge, c) for c in calls]
                for fut in as_completed(futures):
                    results.append(fut.result())
            elapsed = time.time() - t0
            print(f"[llm-intent] {len(results)} calls in {elapsed:.1f}s "
                  f"(model={LLM_MODEL})", flush=True)

            payload = json.dumps({
                "model": LLM_MODEL,
                "elapsed_s": round(elapsed, 1),
                "results": results,
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        except Exception:
            traceback.print_exc()
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


def make_server(handler_cls, preferred: int, host: str) -> tuple[socketserver.TCPServer, int]:
    """Try preferred port first; on any failure, let the OS assign one (port=0).

    Binding the actual HTTPServer here (not a probe socket) eliminates TOCTOU
    where the port becomes occupied between probe and serve.
    """
    class ReusableTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    for candidate in [preferred, 0]:  # 0 = OS-assigned
        try:
            httpd = ReusableTCPServer((host, candidate), handler_cls)
            return httpd, httpd.server_address[1]
        except OSError:
            continue
    raise RuntimeError(f"Could not bind to {host} on any port")


def list_lan_ips() -> list[str]:
    """All non-loopback IPv4 addresses on this host. Multiple interfaces are
    common (Wi-Fi + Ethernet + VPN + Docker bridge), and the "right" one for
    coworkers depends on which network they're on — so we list them all and
    let the user pick."""
    ips: list[str] = []
    # Method 1: socket.connect trick — gets the IP of the default-route interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip not in ips:
            ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    # Method 2: socket.getaddrinfo on hostname — usually catches LAN aliases
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return ips


def main() -> int:
    p = argparse.ArgumentParser(description="Serve Agora dashboard locally with audio-zip proxy")
    p.add_argument("input", help="CSV or XLSX path")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--host", default="127.0.0.1",
        help='Bind address. Default 127.0.0.1 (loopback only). '
             'Use "0.0.0.0" to expose on the LAN so coworkers can open the '
             'dashboard from their machines. macOS will pop a firewall '
             'permission dialog the first time.',
    )
    p.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    args = p.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    df = load_table(inp)
    enriched = enrich(df)
    html = build_html(enriched, source=inp.name)
    global HTML_BYTES, ENRICHED_DF, CURRENT_SOURCE_PATH
    HTML_BYTES = html.encode("utf-8")
    ENRICHED_DF = enriched
    CURRENT_SOURCE_PATH = inp.name

    httpd, port = make_server(Handler, args.port, args.host)
    local_url = f"http://127.0.0.1:{port}/"
    print(f"Agora dashboard → {local_url}")
    if port != args.port:
        print(f"  (requested port {args.port} was busy; using OS-assigned {port})")
    # When bound to all interfaces, list every non-loopback IPv4 so the user can
    # pick the right one for their coworkers (Wi-Fi vs VPN vs Docker bridge).
    if args.host in ("0.0.0.0", "::"):
        ips = list_lan_ips()
        if ips:
            print(f"  LAN access:  (挑一个发给同事，VPN / 公司 Wi-Fi 用不同 IP)")
            for ip in ips:
                print(f"    http://{ip}:{port}/")
        else:
            print("  (无法列出 LAN IP；用 ifconfig 自己查)")
    print(f"  source: {inp.name}  (rows: {len(df)})")
    print("  Ctrl+C to stop")

    # Kick off the LLM intent-truth job in background so by the time the user
    # actually clicks the LLM button results are usually already done.
    kickoff_intent_job(enriched)

    # Tab 2 失败分析 LLM 任务（独立于意向真伪，分别用不同模型可能）
    try:
        from lib import llm_fail_analysis as F
    except ImportError:
        from scripts.lib import llm_fail_analysis as F
    # 优先从环境变量指定的快照加载, 避免重启 server 再跑一遍 LLM (跑一遍 ~15 分钟).
    snapshot = os.environ.get("LLM_FAIL_SNAPSHOT", "").strip()
    if snapshot and F.load_snapshot(snapshot):
        print(f"[llm-fail-auto] using snapshot {snapshot}, skipping LLM run", flush=True)
    else:
        # 通过环境变量可选择采样：OPENAI_FAIL_SAMPLE_PER_BUCKET=20 → 每 (agent, pass_n)
        # bucket 随机取 20 通跑 LLM。默认全量。
        sample = os.environ.get("OPENAI_FAIL_SAMPLE_PER_BUCKET")
        try:
            sample_limit = int(sample) if sample else None
        except ValueError:
            sample_limit = None
        F.kickoff(enriched, sample_limit=sample_limit)

    # Tab 1 带车型完整转换 真实性校验 (小批量, 不重)
    try:
        from lib import llm_full_conv_verify as V
    except ImportError:
        from scripts.lib import llm_full_conv_verify as V
    verify_snapshot = os.environ.get("LLM_VERIFY_SNAPSHOT", "").strip()
    if verify_snapshot and V.load_snapshot(verify_snapshot):
        print(f"[llm-verify-auto] using snapshot {verify_snapshot}", flush=True)
    else:
        V.kickoff(enriched)

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(local_url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

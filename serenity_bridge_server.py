#!/usr/bin/env python3
"""
Serenity Bridge Server — n8n 通过 HTTP 远程调用 serenity 脚本

n8n HTTP Request 节点调用:
    GET  http://host.docker.internal:9388/api/health
    POST http://host.docker.internal:9388/api/serenity/fetch-history
    POST http://host.docker.internal:9388/api/serenity/rescore
    POST http://host.docker.internal:9388/api/serenity/adjust-weights
    POST http://host.docker.internal:9388/api/serenity/factor-report
    POST http://host.docker.internal:9388/api/serenity/daily-workflow
    POST http://host.docker.internal:9388/api/send          # 分发消息 (weixin/telegram 等)

启动 (launchd 管理，自动重启):
    python3 serenity_bridge_server.py [--port 9388]
"""

import json
import hmac
import ipaddress
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from functools import wraps
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# --- Config ---
SERENITY_DIR = os.path.expanduser("~/workspace/SerenityMonitor")
HOST = "0.0.0.0"
PORT = 9388
MAX_BODY = 1024 * 512          # 512KB max request body
REQUEST_TIMEOUT = 60           # seconds per request
SEND_RETRIES = 2               # retry count for hermes send
SEND_RETRY_DELAY = 2           # seconds between retries
TASK_TIMEOUT = 120             # seconds per serenity script
SEND_TIMEOUT = 30              # seconds per hermes send
BRIDGE_TOKEN = os.environ.get("SERENITY_BRIDGE_TOKEN") or os.environ.get("SERENITY_API_TOKEN") or ""

TASKS = {
    "fetch-history":   ["python3", "fetch_history.py"],
    "rescore":         ["python3", "cli.py", "rescore"],
    "adjust-weights":  ["python3", "cli.py", "adjust-weights"],
    "factor-report":   ["python3", "cli.py", "factor-report"],
    "daily-report":    ["python3", "daily_report.py"],
    "daily-workflow":  ["python3", "daily_workflow.py"],
    "status":          ["python3", "cli.py", "status"],
}

_SHUTDOWN = False


from utils import host_part as _host_part

def _is_private_host(value: str) -> bool:
    host = _host_part(value)
    if host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local") or host.endswith(".lan") or host.endswith(".internal")
    return ip.is_loopback or ip.is_private


def _rate_limit(max_per_sec=10):
    """Simple rate limiter: returns True if within limit, False if exceeded."""
    if not hasattr(_rate_limit, "bucket"):
        _rate_limit.bucket = [0.0] * max_per_sec
        _rate_limit.idx = 0
    now = time.monotonic()
    idx = _rate_limit.idx
    _rate_limit.idx = (idx + 1) % max_per_sec
    if now - _rate_limit.bucket[idx] < 1.0:
        return False
    _rate_limit.bucket[idx] = now
    return True


class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT

    def _json(self, status: int, data: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _request_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self.headers.get("X-Serenity-Token", "")

    def _authorized(self) -> bool:
        if BRIDGE_TOKEN:
            return hmac.compare_digest(self._request_token(), BRIDGE_TOKEN)
        host = self.headers.get("Host", "")
        client = self.client_address[0] if self.client_address else ""
        return _is_private_host(host) and _is_private_host(client)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {
            "ok": False,
            "error": "bridge write/task endpoints require local/LAN access or SERENITY_BRIDGE_TOKEN",
        })
        return False

    def _read_body(self) -> dict:
        """Read and parse request body with size limit."""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            raise ValueError(f"body too large: {length} > {MAX_BODY}")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def do_GET(self):
        if _SHUTDOWN:
            self._json(503, {"ok": False, "error": "shutting down"})
            return
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "serenity-bridge"})
        if path.startswith("/api/serenity/"):
            return self._json(405, {"ok": False, "error": "use POST for serenity tasks"})
        self._json(404, {"ok": False, "error": f"not found: {path}"})

    def do_POST(self):
        if _SHUTDOWN:
            self._json(503, {"ok": False, "error": "shutting down"})
            return
        path = urlparse(self.path).path
        if path == "/api/send":
            if not self._require_auth():
                return
            return self._handle_send()
        if path.startswith("/api/serenity/"):
            if not self._require_auth():
                return
            task = path[len("/api/serenity/"):]
            return self._run_task(task)
        self._json(404, {"ok": False, "error": f"not found: {path}"})

    def _handle_send(self) -> None:
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"ok": False, "error": f"invalid body: {e}"})

        platform = body.get("platform", "weixin")
        content = body.get("content", "")
        title = body.get("title", "")
        content_type = body.get("content_type", "markdown")

        if not content:
            return self._json(400, {"ok": False, "error": "content is required"})

        hermes = shutil.which("hermes")
        if not hermes:
            return self._json(500, {"ok": False, "error": "hermes not found in PATH"})

        cmd = [hermes, "send", "--to", platform]
        if title:
            cmd += ["--subject", title]
        cmd += [content]

        # Retry loop
        last_error = None
        for attempt in range(1 + SEND_RETRIES):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT,
                )
                if result.returncode == 0:
                    return self._json(200, {
                        "ok": True,
                        "platform": platform,
                        "stdout": result.stdout.strip()[-2000:],
                    })
                last_error = result.stderr.strip() or result.stdout.strip()[-500:]
                if attempt < SEND_RETRIES:
                    time.sleep(SEND_RETRY_DELAY)
            except subprocess.TimeoutExpired:
                last_error = "timed out"
                if attempt < SEND_RETRIES:
                    time.sleep(SEND_RETRY_DELAY)

        self._json(502, {
            "ok": False,
            "platform": platform,
            "error": f"send failed after {1+SEND_RETRIES} attempts",
            "detail": last_error[-500:],
        })

    def _run_task(self, task: str) -> None:
        if task not in TASKS:
            return self._json(400, {
                "ok": False, "error": f"unknown task: {task}",
                "available": list(TASKS.keys())
            })
        cmd = TASKS[task]
        try:
            result = subprocess.run(
                cmd, cwd=SERENITY_DIR,
                capture_output=True, text=True, timeout=TASK_TIMEOUT,
            )
            self._json(200, {
                "ok": result.returncode == 0,
                "task": task,
                "exit_code": result.returncode,
                "stdout": result.stdout[-5000:],
                "stderr": result.stderr[-2000:],
            })
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "script timed out"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        if args[0] == "GET" and args[1] == "/api/health":
            return  # suppress health check noise
        print(f"[serenity-bridge] {args[0]} {args[1]} {args[2]}")


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _SHUTDOWN
    _SHUTDOWN = True
    print(f"\n[serenity-bridge] signal {signum} received, draining...")


def main():
    global _SHUTDOWN
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--port" else PORT

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    server = HTTPServer((HOST, port), Handler)
    server.socket.settimeout(1.0)  # allow periodic check of _SHUTDOWN

    print(f"🧩 Serenity Bridge running on http://{HOST}:{port}")
    print(f"   Endpoints:")
    for t in TASKS:
        print(f"     POST /api/serenity/{t}")
    print(f"     POST /api/send")
    print(f"     GET  /api/health")
    print(f"   Limits: max_body={MAX_BODY//1024}KB, timeout={REQUEST_TIMEOUT}s")
    if SEND_RETRIES:
        print(f"   Send retry: {SEND_RETRIES}×, delay={SEND_RETRY_DELAY}s")

    try:
        while not _SHUTDOWN:
            server.handle_request()
    except (TimeoutError, socket.timeout):
        pass
    finally:
        _SHUTDOWN = True
        server.server_close()
        print("[serenity-bridge] shut down cleanly")


if __name__ == "__main__":
    main()

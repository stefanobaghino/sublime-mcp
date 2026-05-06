"""In-container stdio↔HTTP bridge for sublime-mcp.

Runs as PID 1 of the container. Speaks newline-delimited JSON-RPC on
stdio and proxies each request to the plugin's HTTP server on the
container's loopback. When stdin closes (parent docker CLI dies),
the bridge exits and the entrypoint trap tears ST and Xvfb down.

This module is dormant in the image until the entrypoint is switched
to `exec python3 /bridge.py`. While dormant, the host-side harness
keeps proxying — see harness.py.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request

CONTAINER_PORT = 47823
ENDPOINT_URL = "http://127.0.0.1:%d/mcp" % CONTAINER_PORT
READINESS_TIMEOUT_S = 60.0
PROXY_HTTP_TIMEOUT_S = 70.0  # exec snippets cap at 60s plugin-side
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(component)s]  "
    "req=%(req_id)s  %(message)s"
)
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

_thread_local = threading.local()


class _ContextFilter(logging.Filter):
    """Stamp every record with `component` (from logger name) and `req_id`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.component = record.name.rsplit(".", 1)[-1]
        record.req_id = getattr(_thread_local, "request_id", "-") or "-"
        return True


def _configure_logging(level: str) -> None:
    root = logging.getLogger("sublime_mcp")
    root.setLevel(level.upper())
    root.propagate = False
    if any(getattr(h, "_sublime_mcp_configured", False) for h in root.handlers):
        return
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler.addFilter(_ContextFilter())
    handler._sublime_mcp_configured = True  # type: ignore[attr-defined]
    root.addHandler(handler)


logger = logging.getLogger("sublime_mcp.bridge")


def http_post(url: str, payload: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_for_ready(deadline: float) -> None:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "bridge-ping", "method": "ping"}
    ).encode("utf-8")
    last_err: Exception | None = None
    started = time.monotonic()
    while time.monotonic() < deadline:
        try:
            status, _ = http_post(ENDPOINT_URL, payload, timeout=2.0)
            if status == 200:
                logger.info("plugin HTTP responded after %.2fs", time.monotonic() - started)
                return
            logger.debug("ping returned status=%d, retrying", status)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            logger.debug("ping failed: %r", exc)
        time.sleep(0.5)
    raise RuntimeError(
        "plugin HTTP server did not respond within %.0fs: %r"
        % (READINESS_TIMEOUT_S, last_err)
    )


def wait_for_window(deadline: float) -> None:
    """Block until ST has at least one window. Avoids headless-guard surprises on first call."""
    body = {
        "jsonrpc": "2.0",
        "id": "bridge-windows",
        "method": "tools/call",
        "params": {
            "name": "exec_sublime_python",
            "arguments": {"code": "print(len(sublime.windows()))"},
        },
    }
    payload = json.dumps(body).encode("utf-8")
    last_state = "(no response yet)"
    started = time.monotonic()
    while time.monotonic() < deadline:
        try:
            status, raw = http_post(ENDPOINT_URL, payload, timeout=5.0)
            if status == 200:
                resp = json.loads(raw.decode("utf-8"))
                content = resp.get("result", {}).get("content") or []
                if content and content[0].get("type") == "text":
                    outcome = json.loads(content[0]["text"])
                    output = (outcome.get("output") or "").strip()
                    last_state = "output=%r error=%r" % (output, outcome.get("error"))
                    if output.isdigit() and int(output) >= 1:
                        logger.info("ST window opened after %.2fs", time.monotonic() - started)
                        return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_state = repr(exc)
        logger.debug("waiting for ST window: %s", last_state)
        time.sleep(0.5)
    raise RuntimeError(
        "Sublime Text never opened a window (last state: %s)" % last_state
    )


_EOF = object()


def _stdin_reader(q: queue.Queue) -> None:
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            q.put(_EOF)
            return
        q.put(line)


def _peek_request(stripped: bytes) -> tuple[str | None, str | None]:
    try:
        body = json.loads(stripped.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    raw_id = body.get("id")
    req_id = str(raw_id) if raw_id is not None else None
    method = body.get("method")
    return req_id, method if isinstance(method, str) else None


def _make_error_response(request_bytes: bytes, message: str) -> bytes:
    req_id = None
    try:
        req = json.loads(request_bytes.decode("utf-8"))
        if isinstance(req, dict):
            req_id = req.get("id")
    except (ValueError, UnicodeDecodeError):
        pass
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32603, "message": message},
    }
    return json.dumps(body).encode("utf-8")


def proxy_loop() -> None:
    stdout = sys.stdout.buffer
    q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_stdin_reader, args=(q,), daemon=True)
    reader.start()
    while True:
        try:
            item = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is _EOF:
            return
        stripped = item.strip()
        if not stripped:
            continue
        req_id, method = _peek_request(stripped)
        _thread_local.request_id = req_id or "-"
        try:
            logger.debug("forwarding method=%s bytes=%d", method, len(stripped))
            try:
                status, raw = http_post(ENDPOINT_URL, stripped, timeout=PROXY_HTTP_TIMEOUT_S)
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                logger.warning("plugin HTTP error: %r", exc)
                err = _make_error_response(stripped, "plugin HTTP error: %r" % exc)
                stdout.write(err + b"\n")
                stdout.flush()
                continue
            if status == 202 or not raw:
                logger.debug("received status=%d (notification, no body)", status)
                continue
            if status != 200:
                logger.warning(
                    "plugin returned HTTP %d body=%r",
                    status,
                    raw[:1000].decode("utf-8", "replace"),
                )
                err = _make_error_response(
                    stripped,
                    "plugin returned HTTP %d: %s" % (status, raw[:200].decode("utf-8", "replace")),
                )
                stdout.write(err + b"\n")
                stdout.flush()
                continue
            logger.debug("received status=%d bytes=%d", status, len(raw))
            stdout.write(raw)
            if not raw.endswith(b"\n"):
                stdout.write(b"\n")
            stdout.flush()
        finally:
            _thread_local.request_id = "-"


def main() -> int:
    level = os.environ.get("SUBLIME_MCP_LOG_LEVEL", "INFO").upper()
    if level not in LOG_LEVELS:
        level = "INFO"
    _configure_logging(level)
    deadline = time.monotonic() + READINESS_TIMEOUT_S
    try:
        wait_for_ready(deadline)
        wait_for_window(deadline)
    except Exception:
        logger.exception("readiness probe failed")
        return 1
    logger.info("ready, accepting JSON-RPC on stdio")
    proxy_loop()
    logger.info("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

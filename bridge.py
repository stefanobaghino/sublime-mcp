"""In-container stdio↔HTTP bridge for sublime-mcp.

Runs as PID 1 of the container. Speaks newline-delimited JSON-RPC on
stdio and proxies each request to the plugin's HTTP server on the
container's loopback. When stdin closes (parent docker CLI dies),
the bridge exits and the entrypoint trap tears ST and Xvfb down.

Most JSON-RPC traffic is byte-forwarded to the plugin. The bridge owns
two MCP tools that have to work even when the plugin host is
unreachable: `inspect_environment` (worker-thread-only diagnostic of
container-level state — processes, HTTP server, X display) and
`restart_st` (kill ST + plugin host, relaunch via `subl --stay`, poll
readiness). For these tools the bridge intercepts `tools/call`; for
`tools/list` it forwards to the plugin and injects the bridge-owned
descriptors into the response.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

CONTAINER_PORT = 47823
ENDPOINT_URL = "http://127.0.0.1:%d/mcp" % CONTAINER_PORT
READINESS_TIMEOUT_S = 60.0
RESTART_READY_TIMEOUT_S = 30.0
PROXY_HTTP_TIMEOUT_S = 70.0  # exec snippets cap at 60s plugin-side
WORKSPACE_PATH = "/work"
SUBL_LOG_PATH = "/var/log/sublime.log"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(component)s]  "
    "req=%(req_id)s  %(message)s"
)
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

_thread_local = threading.local()
_BRIDGE_STARTUP_MONOTONIC: float | None = None


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
        "plugin HTTP server did not respond within deadline: %r" % last_err
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


def _peek_request(stripped: bytes) -> tuple[str | None, str | None, str | None]:
    """Return (req_id, method, tool_name).

    `tool_name` is populated only when method == "tools/call"; otherwise None.
    On any parse failure, returns (None, None, None) — the proxy then forwards
    the bytes verbatim and the plugin produces the canonical JSON-RPC error.
    """
    try:
        body = json.loads(stripped.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None, None
    if not isinstance(body, dict):
        return None, None, None
    raw_id = body.get("id")
    req_id = str(raw_id) if raw_id is not None else None
    method = body.get("method")
    method = method if isinstance(method, str) else None
    tool_name = None
    if method == "tools/call":
        params = body.get("params") or {}
        if isinstance(params, dict):
            name = params.get("name")
            if isinstance(name, str):
                tool_name = name
    return req_id, method, tool_name


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


# Bridge-owned MCP tools — registered below their handlers are defined.
# Maps tool name → {"descriptor": dict, "handler": callable(dict) -> dict}.
_BRIDGE_TOOLS: dict = {}


def _proc_state(pid: int) -> str | None:
    """Read `/proc/<pid>/stat` and return the process-state character ('R', 'S', 'Z', ...).
    Returns None if the process is gone or /proc is unreadable."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            content = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # Format: pid (comm) state ppid ... — `comm` can contain spaces and
    # parens, so find the *last* ')' before parsing the rest.
    rparen = content.rfind(")")
    if rparen < 0:
        return None
    rest = content[rparen + 1:].strip().split()
    if not rest:
        return None
    return rest[0]


def _pgrep_pids(pattern: str, *, exclude_zombies: bool = True) -> list[int]:
    """Return PIDs whose `ps` line matches `pattern`; empty list on no match or error.

    Zombies (`Z` state in `/proc/N/stat`) are treated as gone and excluded
    by default. The bridge runs as PID 1 inside the container; ST processes
    daemonized via `subl --stay` get reparented to it, and any that exit
    without being `wait()`-ed for stay listed by `pgrep` until reaped.
    `restart_st` reaps via `_reap_children` after the kill, but the
    state-character filter is the authoritative "is this process alive?"
    signal regardless.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if result.returncode not in (0, 1):  # 1 = no match, others = error
        return []
    pids = []
    for line in result.stdout.split():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        if exclude_zombies and _proc_state(pid) == "Z":
            continue
        pids.append(pid)
    return pids


def _pgrep_first_pid(pattern: str) -> int | None:
    pids = _pgrep_pids(pattern)
    return pids[0] if pids else None


def _reap_children() -> int:
    """Non-blocking `waitpid(-1, WNOHANG)` loop. Returns the number of reaped PIDs.

    The bridge runs as PID 1 inside the container; daemonized ST processes
    that exit reparent to it and stay zombie until `wait()`-ed for. Called
    after `pkill -KILL` in `restart_st` so subsequent `pgrep` reads see
    them as gone.
    """
    reaped = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        except OSError:
            return reaped
        if pid == 0:
            return reaped
        reaped += 1


def _probe_plugin_http() -> dict:
    """Single 1.0 s `ping` POST to the plugin's HTTP server."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "bridge-inspect", "method": "ping"}
    ).encode("utf-8")
    started = time.monotonic()
    try:
        status, _ = http_post(ENDPOINT_URL, payload, timeout=1.0)
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        return {
            "http_server_listening": False,
            "http_probe_elapsed_s": round(time.monotonic() - started, 3),
            "http_probe_error": repr(exc),
        }
    return {
        "http_server_listening": status == 200,
        "http_probe_elapsed_s": round(time.monotonic() - started, 3),
        "http_probe_error": None if status == 200 else "HTTP %d" % status,
    }


def _xdpyinfo_ok() -> bool:
    try:
        result = subprocess.run(
            ["xdpyinfo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return result.returncode == 0


def _xwininfo_tree(byte_cap: int = 2048) -> str | None:
    try:
        result = subprocess.run(
            ["xwininfo", "-root", "-tree"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout
    if len(out) > byte_cap:
        return out[:byte_cap] + "\n…(truncated)"
    return out


def _uptime_s() -> int:
    if _BRIDGE_STARTUP_MONOTONIC is None:
        return 0
    return int(time.monotonic() - _BRIDGE_STARTUP_MONOTONIC)


def _run_inspect_environment(_args: dict) -> dict:
    """Worker-thread-only snapshot of container-level state.

    Best-effort: any individual subprocess failure surfaces as `None` /
    `False` for that field; the rest of the payload still returns. Never
    raises so the caller always gets *something* to read.
    """
    payload: dict = {
        "bridge_pid": os.getpid(),
        "sublime_text_pids": _pgrep_pids("sublime_text"),
        "plugin_host_pid": _pgrep_first_pid("plugin_host"),
        "xvfb_pid": _pgrep_first_pid("Xvfb"),
    }
    payload.update(_probe_plugin_http())
    payload["display_reachable"] = _xdpyinfo_ok()
    payload["x_windows"] = _xwininfo_tree()
    payload["container_id"] = os.environ.get("HOSTNAME") or None
    payload["workspace_path"] = WORKSPACE_PATH
    payload["uptime_s"] = _uptime_s()
    logger.info(
        "inspect_environment st_pids=%s plugin_host=%s http=%s display=%s",
        payload["sublime_text_pids"],
        payload["plugin_host_pid"],
        payload["http_server_listening"],
        payload["display_reachable"],
    )
    return payload


def _wait_until_processes_gone(pattern: str, timeout_s: float) -> float | None:
    """Poll until no live (non-zombie) match remains. Return elapsed seconds, or None on timeout.

    Reaps zombies on every iteration so processes that exited but stayed
    listed as defunct (because the bridge is PID 1 and hasn't `wait()`-ed
    for them) clear out without an explicit reap step. The state-character
    filter in `_pgrep_pids` means this is correct even when the reap
    loop misses (a process whose parent isn't the bridge can still be a
    zombie without being our child).
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        _reap_children()
        if not _pgrep_pids(pattern):
            return time.monotonic() - started
        time.sleep(0.1)
    return None


def _restart_failure(
    started: float,
    error: str,
    log_lines: list[str],
    before_st: list[int],
    before_plugin: int | None,
) -> dict:
    return {
        "success": False,
        "elapsed_s": round(time.monotonic() - started, 3),
        "error": error,
        "sublime_text_pids_before": before_st,
        "sublime_text_pids_after": _pgrep_pids("sublime_text"),
        "plugin_host_pid_before": before_plugin,
        "plugin_host_pid_after": _pgrep_first_pid("plugin_host"),
        "http_ready_after_s": None,
        "log_lines": log_lines,
    }


def _run_restart_st(_args: dict) -> dict:
    """Kill ST + plugin host, relaunch `subl --stay`, poll the plugin HTTP
    server until it's responsive again. Last-resort recovery when
    `health_check` reports a wedged main thread (#73 part 2).

    TERM-then-KILL escalation: SIGTERM first, wait up to 5 s for graceful
    exit, escalate to SIGKILL if still alive. Then `subl --stay` (which
    self-daemonizes and the launcher exits), and `wait_for_ready`.
    """
    log_lines: list[str] = []
    started = time.monotonic()

    before_st = _pgrep_pids("sublime_text")
    before_plugin = _pgrep_first_pid("plugin_host")
    log_lines.append(
        "before: sublime_text_pids=%s plugin_host_pid=%s" % (before_st, before_plugin)
    )

    if before_st:
        try:
            term = subprocess.run(
                ["pkill", "-TERM", "-f", "sublime_text"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            log_lines.append("pkill -TERM -f sublime_text rc=%d" % term.returncode)
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            return _restart_failure(
                started, "pkill -TERM failed: %r" % exc, log_lines, before_st, before_plugin
            )
        graceful = _wait_until_processes_gone("sublime_text", 5.0)
        if graceful is not None:
            log_lines.append("ST exited gracefully after %.2fs" % graceful)
        else:
            log_lines.append("ST did not exit within 5s; escalating to KILL")
            try:
                kill = subprocess.run(
                    ["pkill", "-KILL", "-f", "sublime_text"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                log_lines.append("pkill -KILL -f sublime_text rc=%d" % kill.returncode)
            except (subprocess.SubprocessError, FileNotFoundError) as exc:
                return _restart_failure(
                    started, "pkill -KILL failed: %r" % exc, log_lines, before_st, before_plugin
                )
            # Reap any zombies whose parent is the bridge (PID 1) so the
            # subsequent pgrep doesn't list them as "still running".
            reaped = _reap_children()
            if reaped:
                log_lines.append("reaped %d zombie children" % reaped)
            forced = _wait_until_processes_gone("sublime_text", 3.0)
            if forced is None:
                return _restart_failure(
                    started, "sublime_text still running after KILL+3s",
                    log_lines, before_st, before_plugin,
                )
            log_lines.append("ST exited after KILL in %.2fs" % forced)
    else:
        log_lines.append("no sublime_text processes before restart; skipping kill")

    workspace = WORKSPACE_PATH if os.path.isdir(WORKSPACE_PATH) else "/tmp"
    try:
        log_fh = open(SUBL_LOG_PATH, "ab")
    except OSError as exc:
        log_lines.append("could not open %s: %r — using DEVNULL" % (SUBL_LOG_PATH, exc))
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        try:
            launcher = subprocess.Popen(
                ["subl", "--stay", workspace],
                stdout=log_fh,
                stderr=subprocess.STDOUT if log_fh is not subprocess.DEVNULL else log_fh,
                start_new_session=True,
            )
            log_lines.append(
                "subl --stay %s launched (launcher_pid=%d)" % (workspace, launcher.pid)
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _restart_failure(
                started, "subl launch failed: %r" % exc, log_lines, before_st, before_plugin
            )
    finally:
        if hasattr(log_fh, "close"):
            try:
                log_fh.close()
            except Exception:
                pass

    # `subl --stay` self-daemonizes and the launcher exits within ~1 s. We
    # wait briefly so log lines about "launcher pid" don't precede its exit.
    try:
        launcher.wait(timeout=10.0)
        log_lines.append("subl launcher exited rc=%d" % launcher.returncode)
    except subprocess.TimeoutExpired:
        log_lines.append("warning: subl launcher did not exit within 10s")

    deadline = time.monotonic() + RESTART_READY_TIMEOUT_S
    ready_started = time.monotonic()
    try:
        wait_for_ready(deadline)
    except RuntimeError as exc:
        return _restart_failure(
            started, "plugin HTTP did not come back: %s" % exc,
            log_lines, before_st, before_plugin,
        )
    http_elapsed = time.monotonic() - ready_started
    log_lines.append("plugin HTTP responsive after %.2fs (post-launch)" % http_elapsed)

    after_st = _pgrep_pids("sublime_text")
    after_plugin = _pgrep_first_pid("plugin_host")
    log_lines.append(
        "after: sublime_text_pids=%s plugin_host_pid=%s" % (after_st, after_plugin)
    )

    elapsed = time.monotonic() - started
    logger.info(
        "restart_st success elapsed=%.2fs plugin_host_pid=%s→%s",
        elapsed, before_plugin, after_plugin,
    )
    return {
        "success": True,
        "elapsed_s": round(elapsed, 3),
        "sublime_text_pids_before": before_st,
        "sublime_text_pids_after": after_st,
        "plugin_host_pid_before": before_plugin,
        "plugin_host_pid_after": after_plugin,
        "http_ready_after_s": round(http_elapsed, 3),
        "log_lines": log_lines,
    }


_BRIDGE_TOOLS["inspect_environment"] = {
    "descriptor": {
        "name": "inspect_environment",
        "description": (
            "Bridge-owned diagnostic snapshot of container-level state — "
            "process PIDs (Xvfb, sublime_text, plugin_host, bridge), "
            "plugin HTTP reachability, X display reachability, top-level "
            "X windows, container_id, workspace_path, uptime. Worker-"
            "thread-only on the bridge: returns within ~3s even when ST's "
            "main thread or the entire plugin host is unresponsive. Use "
            "after `health_check` reports `main_thread_responsive: false` "
            "to triage whether the plugin is alive at all and whether an "
            "X dialog might be the cause; pair with `xdotool key Escape` "
            "from an `exec_sublime_python` snippet for soft recovery, or "
            "fall back to `restart_st` for hard recovery."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "handler": _run_inspect_environment,
}

_BRIDGE_TOOLS["restart_st"] = {
    "descriptor": {
        "name": "restart_st",
        "description": (
            "Last-resort in-agent recovery: kill the running Sublime Text "
            "+ plugin host, relaunch `subl --stay <workspace>`, and poll "
            "the plugin's HTTP server until it's responsive again. "
            "TERM-then-KILL escalation (5s graceful budget). Returns "
            "within ~30s with `success`, before/after PIDs, and a "
            "`log_lines` array describing each step. Use after "
            "`health_check` reports `main_thread_responsive: false` and "
            "soft recovery (e.g. `xdotool key Escape` to dismiss an "
            "invisible dialog) has not unblocked main. Destructive: open "
            "views, scratch buffers, `temp_packages_link` registry state "
            "do not survive — by design."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "handler": _run_restart_st,
}


def _bridge_tool_response(req_id: object, name: str, payload: dict) -> bytes:
    text = json.dumps(payload, indent=2, default=str)
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": payload.get("success") is False,
        },
    }
    return json.dumps(body).encode("utf-8")


def _bridge_error_response(req_id: object, code: int, message: str) -> bytes:
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }
    return json.dumps(body).encode("utf-8")


def _handle_bridge_tool_call(stripped: bytes) -> bytes:
    """Dispatch a `tools/call` for a bridge-owned tool. Returns response bytes."""
    body = json.loads(stripped.decode("utf-8"))
    req_id = body.get("id")
    params = body.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _bridge_error_response(req_id, -32602, "arguments must be an object")
    spec = _BRIDGE_TOOLS.get(name)
    if spec is None:  # pragma: no cover — guarded by caller
        return _bridge_error_response(req_id, -32602, "unknown bridge tool: %s" % name)
    try:
        payload = spec["handler"](arguments)
    except Exception as exc:
        logger.exception("bridge tool %s raised", name)
        return _bridge_error_response(req_id, -32603, "%s raised: %r" % (name, exc))
    return _bridge_tool_response(req_id, name, payload)


def _inject_bridge_tools_into_list(raw: bytes) -> bytes:
    """Append bridge-owned descriptors to a `tools/list` response. Pass-through on parse failure."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("could not parse tools/list response; passing through unmodified")
        return raw
    if not isinstance(body, dict):
        return raw
    result = body.get("result")
    if not isinstance(result, dict):
        return raw
    tools = result.get("tools")
    if not isinstance(tools, list):
        return raw
    existing = {t.get("name") for t in tools if isinstance(t, dict)}
    for name, spec in _BRIDGE_TOOLS.items():
        if name not in existing:
            tools.append(spec["descriptor"])
    return json.dumps(body).encode("utf-8")


def _forward_to_plugin(stripped: bytes) -> tuple[int, bytes] | None:
    """Forward `stripped` to the plugin's HTTP server. Returns (status, body) or None on transport error."""
    try:
        return http_post(ENDPOINT_URL, stripped, timeout=PROXY_HTTP_TIMEOUT_S)
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        logger.warning("plugin HTTP error: %r", exc)
        return None


def _emit(stdout, raw: bytes) -> None:
    stdout.write(raw)
    if not raw.endswith(b"\n"):
        stdout.write(b"\n")
    stdout.flush()


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
        req_id, method, tool_name = _peek_request(stripped)
        _thread_local.request_id = req_id or "-"
        try:
            # 1. Bridge-owned tools: handle locally, never forward.
            if method == "tools/call" and tool_name in _BRIDGE_TOOLS:
                logger.info("dispatching bridge tool name=%s", tool_name)
                response = _handle_bridge_tool_call(stripped)
                _emit(stdout, response)
                continue

            # 2. Everything else: forward to the plugin.
            logger.debug("forwarding method=%s bytes=%d", method, len(stripped))
            forwarded = _forward_to_plugin(stripped)
            if forwarded is None:
                err = _make_error_response(stripped, "plugin HTTP transport error")
                _emit(stdout, err)
                continue
            status, raw = forwarded
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
                _emit(stdout, err)
                continue

            # 3. tools/list: inject bridge-owned descriptors before emitting.
            if method == "tools/list":
                raw = _inject_bridge_tools_into_list(raw)

            logger.debug("received status=%d bytes=%d", status, len(raw))
            _emit(stdout, raw)
        finally:
            _thread_local.request_id = "-"


def main() -> int:
    global _BRIDGE_STARTUP_MONOTONIC
    _BRIDGE_STARTUP_MONOTONIC = time.monotonic()
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

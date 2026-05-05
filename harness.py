"""Stdio MCP harness that runs sublime-mcp inside a Docker container.

The harness is the user-facing entrypoint of sublime-mcp: an agent
registers it as a stdio MCP server (`claude mcp add --transport stdio
sublime-text -- sublime-mcp …`), and the harness boots a container with
Sublime Text + the plugin, waits for the in-container HTTP server to
come up, and proxies JSON-RPC messages between the agent (over stdio)
and the plugin (over HTTP).

One harness owns one container; one agent's session spawns one harness.
Multiple agents on the same machine each get their own harness +
container — host ports are kernel-assigned, so no coordination is
needed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_IMAGE_TAG = "sublime-mcp-harness:latest"
CONTAINER_PORT = 47823
CONTAINER_LICENSE_DIR = "/root/.config/sublime-text/Local"
READINESS_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 1.0
PROXY_HTTP_TIMEOUT_S = 70.0  # exec snippets cap at 60s server-side
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(component)s]  "
    "req=%(req_id)s  %(message)s"
)
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


# Per-request correlation id, set by `proxy_loop` before each http_post
# and cleared in the finally. Read by `_ContextFilter` and stamped onto
# every log record. The bridge has its own threadlocal in its own
# process; the id flows across the boundary via the JSON-RPC `id`
# field.
_thread_local = threading.local()


class _ContextFilter(logging.Filter):
    """Stamp every record with `component` (from logger name) and `req_id`."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.component = record.name.rsplit(".", 1)[-1]
        record.req_id = getattr(_thread_local, "request_id", "-") or "-"
        return True


def _configure_logging(level: str) -> None:
    """Idempotent root setup for `sublime_mcp.*` loggers.

    Attaches a single stderr handler with the unified format. Safe to
    call once per process; subsequent calls are no-ops aside from
    level updates. Timestamps are UTC so the harness's lines and the
    bridge's lines (emitted from the container, where TZ defaults to
    UTC) sort and correlate in one stream regardless of host TZ.
    """
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


logger = logging.getLogger("sublime_mcp.harness")


# ---------------------------------------------------------------------
# Argument parsing


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sublime-mcp",
        description="Stdio MCP harness for sublime-mcp inside Docker.",
    )
    parser.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="HOST:CONTAINER",
        help="Bind-mount HOST into the container at CONTAINER. Repeatable. "
             "Recommended: --mount $PWD:/work",
    )
    parser.add_argument(
        "--image-tag",
        default=DEFAULT_IMAGE_TAG,
        help="Image tag to look up / build. Default: %(default)s",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force `docker build` even if the image already exists.",
    )
    parser.add_argument(
        "--license-file",
        metavar="PATH",
        help="Mount a Sublime Text license file into the container.",
    )
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=os.environ.get("SUBLIME_MCP_LOG_LEVEL", "INFO").upper(),
        help="Logging level for harness + bridge (also $SUBLIME_MCP_LOG_LEVEL). "
             "Default: %(default)s.",
    )
    args = parser.parse_args(argv)
    if args.log_level not in LOG_LEVELS:
        parser.error("--log-level must be one of %s, got %r" % (LOG_LEVELS, args.log_level))
    for spec in args.mount:
        if ":" not in spec or spec.startswith(":") or spec.endswith(":"):
            parser.error("--mount expects HOST:CONTAINER, got %r" % spec)
    if args.license_file and not Path(args.license_file).is_file():
        parser.error("--license-file %r is not a regular file" % args.license_file)
    return args


# ---------------------------------------------------------------------
# Build context


def build_context_dir() -> Path:
    """Locate the Dockerfile + sublime_mcp.py + entrypoint.sh.

    Two cases: running from a source checkout (next to harness.py) or
    installed as a package (alongside the module). In both cases the
    files sit next to this file.
    """
    here = Path(__file__).resolve().parent
    required = ["Dockerfile", "sublime_mcp.py", "docker/entrypoint.sh"]
    missing = [name for name in required if not (here / name).is_file()]
    if missing:
        raise RuntimeError(
            "build context incomplete next to %s: missing %s"
            % (here, ", ".join(missing))
        )
    return here


def stage_build_context(src: Path) -> Path:
    """Copy the build context into a temp dir for `docker build`.

    Avoids passing the source directory directly so transient files
    (e.g. .pyc, editor swap files) don't get pulled into the image.
    """
    dst = Path(tempfile.mkdtemp(prefix="sublime-mcp-harness-build-"))
    shutil.copy2(src / "Dockerfile", dst / "Dockerfile")
    shutil.copy2(src / "sublime_mcp.py", dst / "sublime_mcp.py")
    (dst / "docker").mkdir()
    shutil.copy2(src / "docker" / "entrypoint.sh", dst / "docker" / "entrypoint.sh")
    return dst


# ---------------------------------------------------------------------
# Docker calls


def docker_available() -> tuple[bool, str]:
    """Check that `docker` is on PATH and the daemon is reachable.

    Returns (ok, reason). `reason` is empty when ok.
    """
    try:
        subprocess.run(
            ["docker", "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, "`docker` not found on PATH"
    except subprocess.CalledProcessError:
        return False, "`docker --version` failed"
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        msg = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = msg[-1] if msg else "(no stderr)"
        return False, "docker daemon unreachable (%s)" % tail
    return True, ""


def image_exists(tag: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_image(tag: str, context: Path) -> None:
    logger.info("building image %s (this can take a few minutes on first run)…", tag)
    subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        check=True,
    )
    logger.info("image %s built", tag)


def ensure_image(tag: str, force: bool, context: Path) -> None:
    """Build the image with a host-side flock so concurrent first-runs serialise."""
    cache_dir = Path.home() / ".cache" / "sublime-mcp-harness"
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / "build.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Re-check inside the lock — another harness might have just
        # finished building.
        if force or not image_exists(tag):
            staged = stage_build_context(context)
            try:
                build_image(tag, staged)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
        else:
            logger.info("reusing image %s", tag)


CONTAINER_LOG_FILE = "/tmp/sublime-mcp.log"


def make_bridge_log_file() -> str:
    """Create a host-side scratch file for the bridge to log into.

    Mounted at `CONTAINER_LOG_FILE` inside the container; the harness
    tails it directly to avoid `docker exec` overhead. Caller is
    responsible for removing the file on shutdown.

    Pinned to `/tmp` (rather than the platform default `tempfile.gettempdir()`)
    so the path is always inside Docker Desktop's default filesystem
    sharing on macOS — `/var/folders/...` (the default TMPDIR there)
    is not always shared. `chmod 0666` so any UID inside the container
    can append to the bind-mounted file (Linux Docker doesn't always
    map container root to host root in CI).
    """
    fd, path = tempfile.mkstemp(prefix="sublime-mcp-bridge-", suffix=".log", dir="/tmp")
    os.close(fd)
    os.chmod(path, 0o666)
    return path


def run_container(
    tag: str,
    mounts: list[str],
    license_file: str | None,
    log_level: str = "INFO",
    bridge_log_path: str | None = None,
) -> str:
    args = [
        "docker", "run",
        "-d",
        "--rm",
        "--label", "sublime-mcp-harness=%d" % os.getpid(),
        "-p", "127.0.0.1:0:%d" % CONTAINER_PORT,
        "-e", "SUBLIME_MCP_LOG_LEVEL=%s" % log_level,
    ]
    if bridge_log_path:
        args += [
            "-e", "SUBLIME_MCP_LOG_FILE=%s" % CONTAINER_LOG_FILE,
            "-v", "%s:%s" % (bridge_log_path, CONTAINER_LOG_FILE),
        ]
    for spec in mounts:
        args += ["-v", spec]
    if license_file:
        args += [
            "-v",
            "%s:%s/License.sublime_license:ro"
            % (str(Path(license_file).resolve()), CONTAINER_LICENSE_DIR),
        ]
    args.append(tag)
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    cid = result.stdout.strip()
    if not cid:
        raise RuntimeError("docker run produced no container id")
    return cid


def host_port(cid: str) -> int:
    result = subprocess.run(
        ["docker", "port", cid, "%d/tcp" % CONTAINER_PORT],
        check=True,
        capture_output=True,
        text=True,
    )
    # Lines look like `127.0.0.1:54321` — take the first IPv4 mapping.
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("[::]"):
            continue
        host, _, port = line.rpartition(":")
        if host and port.isdigit():
            return int(port)
    raise RuntimeError("docker port returned no IPv4 mapping: %r" % result.stdout)


def stop_container(cid: str) -> None:
    """Stop the container with a short grace, then kill if it lingers."""
    logger.info("stopping container cid=%s grace=%.1fs", cid[:12], SHUTDOWN_GRACE_S)
    try:
        subprocess.run(
            ["docker", "stop", "--time", str(int(SHUTDOWN_GRACE_S)), cid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=SHUTDOWN_GRACE_S + 5.0,
        )
    except subprocess.TimeoutExpired:
        pass
    # `docker stop` returning doesn't guarantee the container is gone
    # if it ignored SIGTERM; force-kill to be sure.
    subprocess.run(
        ["docker", "kill", cid],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


# ---------------------------------------------------------------------
# In-container MCP HTTP client


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


def wait_for_ready(port: int, deadline: float) -> None:
    """Poll the in-container MCP endpoint until it answers ping."""
    url = "http://127.0.0.1:%d/mcp" % port
    payload = json.dumps({"jsonrpc": "2.0", "id": "harness-ping", "method": "ping"}).encode("utf-8")
    last_err: Exception | None = None
    started = time.monotonic()
    while time.monotonic() < deadline:
        try:
            status, _ = http_post(url, payload, timeout=2.0)
            if status == 200:
                logger.info("MCP HTTP responded after %.2fs", time.monotonic() - started)
                return
            logger.debug("ping returned status=%d, retrying", status)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            logger.debug("ping failed: %r", exc)
        time.sleep(0.5)
    raise RuntimeError(
        "in-container MCP server did not respond within %.0fs: %r"
        % (READINESS_TIMEOUT_S, last_err)
    )


def wait_for_window(port: int, deadline: float) -> None:
    """Verify ST has at least one window open before declaring ready.

    Protects against the headless guard at sublime_mcp.py:442-450
    surprising the first agent call.
    """
    url = "http://127.0.0.1:%d/mcp" % port
    body = {
        "jsonrpc": "2.0",
        "id": "harness-windows",
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
            status, raw = http_post(url, payload, timeout=5.0)
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
        "Sublime Text never opened a window (last state: %s). "
        "The container's entrypoint should have run `subl --stay /work`; "
        "check `docker logs` for Xvfb or licensing errors." % last_state
    )


# ---------------------------------------------------------------------
# Stdio proxy


_EOF = object()


def _stdin_reader(q: queue.Queue) -> None:
    """Push each stdin line onto `q`, then `_EOF` when stdin closes.

    Runs as a daemon thread so the main thread can observe a stop
    signal between queue polls — a blocking readline() on the main
    thread would not notice SIGTERM until the next byte arrives.
    """
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            q.put(_EOF)
            return
        q.put(line)


def _peek_request(stripped: bytes) -> tuple[str | None, str | None]:
    """Best-effort extraction of (request_id, method) from a JSON-RPC line.

    Returns (None, None) for malformed input — the proxy still forwards
    the bytes verbatim and lets the bridge respond with the structured
    parse error.
    """
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


def proxy_loop(port: int, stop_event: threading.Event) -> None:
    """Forward newline-delimited JSON-RPC between stdin/stdout and the container."""
    url = "http://127.0.0.1:%d/mcp" % port
    stdout = sys.stdout.buffer
    q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_stdin_reader, args=(q,), daemon=True)
    reader.start()
    while not stop_event.is_set():
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
                status, raw = http_post(url, stripped, timeout=PROXY_HTTP_TIMEOUT_S)
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                logger.warning("container HTTP error: %r", exc)
                err = _make_error_response(stripped, "container HTTP error: %r" % exc)
                stdout.write(err + b"\n")
                stdout.flush()
                continue
            if status == 202 or not raw:
                # Notification → no body, no stdout write.
                logger.debug("received status=%d (notification, no body)", status)
                continue
            if status != 200:
                logger.warning(
                    "container returned HTTP %d body=%r",
                    status,
                    raw[:1000].decode("utf-8", "replace"),
                )
                err = _make_error_response(
                    stripped,
                    "container returned HTTP %d: %s" % (status, raw[:200].decode("utf-8", "replace")),
                )
                stdout.write(err + b"\n")
                stdout.flush()
                continue
            logger.debug("received status=%d bytes=%d", status, len(raw))
            # Pass through verbatim. The plugin already speaks MCP JSON-RPC.
            stdout.write(raw)
            if not raw.endswith(b"\n"):
                stdout.write(b"\n")
            stdout.flush()
        finally:
            _thread_local.request_id = "-"


def _make_error_response(request_bytes: bytes, message: str) -> bytes:
    """Synthesise a JSON-RPC error response keyed to the request's id."""
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


# ---------------------------------------------------------------------
# Bridge log tail


def _tail_bridge_log(path: str, stop_event: threading.Event) -> None:
    """Tail the host-side bridge log file into the harness's unified stream.

    The bridge writes to `CONTAINER_LOG_FILE` inside the container,
    which is bind-mounted onto `path` on the host. ST self-daemonizes
    so its plugin host's `sys.stderr` doesn't reach `docker logs`; the
    file mount is the load-bearing surface for the bridge's lines.

    Lines are pre-formatted by the bridge — pass them through verbatim
    onto stderr. Multi-line `faulthandler` dumps (no `[bridge]` prefix)
    inherit through the same passthrough so the wedged-thread stack
    appears next to the ERROR line that triggered it.

    Exits on `stop_event` set OR a graceful EOF after `stop_event` is
    raised (container teardown). Polls the file rather than using
    `docker logs --follow` because ST's daemonization detaches its I/O
    from PID 1.

    On exit, logs the byte count it forwarded and the file's final
    size — divergence between the two surfaces a "tail saw nothing
    but bridge did write" failure mode (and vice versa) instead of
    leaving it indistinguishable from "bridge silently never logged".
    """
    stderr = sys.stderr
    forwarded_bytes = 0
    try:
        f = open(path, "r")
    except OSError as exc:
        logger.warning("tail thread: cannot open bridge log %r: %r", path, exc)
        return
    try:
        while not stop_event.is_set():
            chunk = f.read()
            if chunk:
                stderr.write(chunk)
                stderr.flush()
                forwarded_bytes += len(chunk.encode("utf-8", "replace"))
            else:
                time.sleep(0.1)
        # Drain anything written between the last poll and shutdown.
        chunk = f.read()
        if chunk:
            stderr.write(chunk)
            stderr.flush()
            forwarded_bytes += len(chunk.encode("utf-8", "replace"))
    finally:
        try:
            f.close()
        except OSError:
            pass
        try:
            final_size = os.path.getsize(path)
        except OSError:
            final_size = -1
        logger.info(
            "tail thread: forwarded_bytes=%d file_final_size=%d path=%s",
            forwarded_bytes,
            final_size,
            path,
        )
        # If the file was non-trivial but we never forwarded anything,
        # the polling read missed the writes — dump the file content
        # for diagnosis. Bound at 16 KiB so a runaway write doesn't
        # flood stderr.
        if final_size > 0 and forwarded_bytes == 0:
            try:
                with open(path, "r") as f2:
                    stderr.write("--- bridge log content (post-mortem) ---\n")
                    stderr.write(f2.read(16 * 1024))
                    stderr.write("\n--- end bridge log content ---\n")
                    stderr.flush()
            except OSError:
                pass


# ---------------------------------------------------------------------
# Top-level


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    _configure_logging(args.log_level)

    ok, reason = docker_available()
    if not ok:
        logger.error(
            "docker unavailable: %s. Install Docker Desktop or the Docker Engine, "
            "ensure the daemon is running, and retry.",
            reason,
        )
        return 2

    try:
        ctx = build_context_dir()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    try:
        ensure_image(args.image_tag, args.rebuild, ctx)
    except subprocess.CalledProcessError as exc:
        logger.error("docker build failed (exit %d)", exc.returncode)
        return exc.returncode or 1

    bridge_log_path = make_bridge_log_file()
    try:
        cid = run_container(
            args.image_tag,
            args.mount,
            args.license_file,
            args.log_level,
            bridge_log_path=bridge_log_path,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("docker run failed (exit %d): %s", exc.returncode, exc.stderr or "")
        try:
            os.unlink(bridge_log_path)
        except OSError:
            pass
        return exc.returncode or 1
    logger.info("container started cid=%s bridge_log=%s", cid[:12], bridge_log_path)

    stop_event = threading.Event()

    def shutdown(signum: int | None = None, frame: object | None = None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    tail_thread = threading.Thread(
        target=_tail_bridge_log,
        args=(bridge_log_path, stop_event),
        name="sublime-mcp-tail",
        daemon=True,
    )
    tail_thread.start()

    try:
        port = host_port(cid)
        logger.info("port mapping: 127.0.0.1:%d -> container:%d", port, CONTAINER_PORT)
        deadline = time.monotonic() + READINESS_TIMEOUT_S
        wait_for_ready(port, deadline)
        wait_for_window(port, deadline)
        logger.info("ready on 127.0.0.1:%d (container %s)", port, cid[:12])
        proxy_loop(port, stop_event)
        return 0
    except Exception as exc:
        logger.error("%s", exc)
        return 1
    finally:
        # Snapshot the bridge's view of the log file from inside the
        # container *before* we tear it down. If the host-side tail
        # forwarded zero bytes, this distinguishes "bridge wrote
        # something the bind-mount didn't propagate" from "bridge
        # never wrote at all."
        try:
            result = subprocess.run(
                ["docker", "exec", cid, "sh", "-c",
                 "ls -la %s 2>&1; echo '---bridge-log-content---'; cat %s 2>/dev/null | head -c 4096; "
                 "echo '---init-sentinel---'; cat /tmp/sublime-mcp-init.log 2>&1 | head -c 2048"
                 % (CONTAINER_LOG_FILE, CONTAINER_LOG_FILE)],
                capture_output=True, text=True, timeout=5,
            )
            logger.info(
                "in-container bridge log: rc=%d stdout=%r",
                result.returncode,
                result.stdout[:3000],
            )
        except Exception as exc:
            logger.debug("in-container diagnostic skipped: %r", exc)
        try:
            stop_container(cid)
        except Exception as exc:
            logger.warning("cleanup of container %s failed: %r", cid[:12], exc)
        # Tell the tail thread to wind down. EOF on stdin walks us
        # here without ever setting `stop_event` (signal handlers do
        # that), so without this the daemon-thread exits when main()
        # returns and its finally — including the tail-byte dump —
        # never runs.
        stop_event.set()
        # Give the tail thread a beat to drain the final bridge writes
        # (including any faulthandler dump) before unlinking the file.
        tail_thread.join(timeout=1.0)
        try:
            os.unlink(bridge_log_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())

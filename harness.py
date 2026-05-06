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
import re
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

IMAGE_REPO = "sublime-mcp-harness"
# Build-time label key applied to every image we build, so cleanup can
# target this project's images precisely:
#   docker images --filter "label=sublime-mcp-harness-image" -q
# Distinct from the *container* label `sublime-mcp-harness=<pid>`
# applied by `run_container`, which serves a different purpose.
IMAGE_LABEL_KEY = "sublime-mcp-harness-image"
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


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_AGENT_NAME_SUB = re.compile(r"[^a-z0-9-]+")
CONTAINER_NAME_MAX = 253  # Docker container name limit.


def _sanitize_agent_name(name: str) -> str:
    """Lowercase + collapse non-`[a-z0-9-]` runs into a single `-`."""
    cleaned = _AGENT_NAME_SUB.sub("-", name.lower())
    return cleaned.strip("-")


def _container_name(agent: str, session_id: str) -> str:
    """Build `st-<agent>-<session-id>`, truncating agent if name is too long."""
    name = "st-%s-%s" % (agent, session_id)
    if len(name) > CONTAINER_NAME_MAX:
        budget = CONTAINER_NAME_MAX - len("st--") - len(session_id)
        name = "st-%s-%s" % (agent[:max(budget, 1)], session_id)
    return name


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
        default=None,
        help="Override the image tag. Default: derived from "
             "`git rev-parse HEAD` against the harness checkout as "
             "`sublime-mcp-harness:<sha12>`. The harness refuses to run "
             "when the source isn't a git repo or its work tree is dirty; "
             "passing this flag bypasses both checks.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force `docker build` even if an image with the resolved "
             "tag already exists. Useful only to recover from a "
             "corrupted local image cache.",
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
    parser.add_argument(
        "--agent-name",
        required=True,
        metavar="AGENT",
        help="Name of the Claude Code agent owning this session. "
             "Used to name the container as st-<agent>-<session-id>.",
    )
    parser.add_argument(
        "--session-id",
        required=True,
        metavar="UUID",
        help="Claude Code session UUID. Used to name the container.",
    )
    args = parser.parse_args(argv)
    if args.log_level not in LOG_LEVELS:
        parser.error("--log-level must be one of %s, got %r" % (LOG_LEVELS, args.log_level))
    for spec in args.mount:
        if ":" not in spec or spec.startswith(":") or spec.endswith(":"):
            parser.error("--mount expects HOST:CONTAINER, got %r" % spec)
    if args.license_file and not Path(args.license_file).is_file():
        parser.error("--license-file %r is not a regular file" % args.license_file)
    sanitized_agent = _sanitize_agent_name(args.agent_name)
    if not sanitized_agent:
        parser.error(
            "--agent-name %r contains no [a-z0-9-] characters after sanitization"
            % args.agent_name
        )
    args.agent_name = sanitized_agent
    if not _SESSION_ID_RE.match(args.session_id):
        parser.error(
            "--session-id %r must match [A-Za-z0-9-]{1,64}" % args.session_id
        )
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
# Git-derived image tag


def derive_image_tag(src: Path) -> str:
    """Return `sublime-mcp-harness:<git-sha12>` for the harness checkout at `src`.

    Refuses if `src` isn't inside a git work tree or the work tree is
    dirty (any staged, unstaged, or untracked change). The tag must
    unambiguously identify a committed state. Pass `--image-tag` to
    bypass both checks.
    """
    inside = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise RuntimeError(
            "harness source at %s is not a git repository — "
            "pass --image-tag to override or run from a clone of the "
            "sublime-mcp source repo" % src
        )
    status = subprocess.run(
        ["git", "-C", str(src), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "harness source at %s has uncommitted changes — "
            "commit or stash before running, or pass --image-tag to "
            "override.\nDirty paths:\n%s" % (src, status.stdout.rstrip())
        )
    head = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return "%s:%s" % (IMAGE_REPO, head.stdout.strip()[:12])


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
        [
            "docker", "build",
            "--label", "%s=%s" % (IMAGE_LABEL_KEY, tag),
            "-t", tag,
            str(context),
        ],
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


CONTAINER_LOG_FILE = "/tmp/sublime-mcp-bridge.log"


def run_container(
    tag: str,
    mounts: list[str],
    license_file: str | None,
    log_level: str = "INFO",
    *,
    name: str,
) -> str:
    args = [
        "docker", "run",
        "-d",
        "--rm",
        "--name", name,
        "--label", "sublime-mcp-harness=%d" % os.getpid(),
        "-p", "127.0.0.1:0:%d" % CONTAINER_PORT,
        "-e", "SUBLIME_MCP_LOG_LEVEL=%s" % log_level,
        "-e", "SUBLIME_MCP_LOG_FILE=%s" % CONTAINER_LOG_FILE,
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


def _tail_bridge_log(cid: str, stop_event: threading.Event) -> None:
    """Tail the bridge log file from inside the container into harness stderr.

    The bridge writes to `CONTAINER_LOG_FILE` inside the container —
    not a bind-mounted host file. ST's plugin host can't reliably
    write to a host bind-mounted file under Linux native Docker (the
    plugin host appears to be running with restrictions that block
    writes to files owned by other UIDs even at 0666 mode), so the
    bridge owns the file lifecycle and the harness reads it via
    `docker exec ... tail -F`.

    Lines are pre-formatted by the bridge — pass them through verbatim
    onto stderr. Multi-line `faulthandler` dumps (no `[bridge]` prefix)
    inherit through the same passthrough so the wedged-thread stack
    appears next to the ERROR line that triggered it.

    Exits on `stop_event` set OR EOF from `tail` (container exited).
    The `-n +1` flag streams the file from the start, so boot-time
    bridge events make it through even if the tail thread starts
    after they were emitted.
    """
    stderr = sys.stderr
    proc = None
    try:
        # `stdin=DEVNULL` is load-bearing: without it, Popen lets the
        # subprocess inherit the harness's stdin, and `docker exec`'s
        # session reader competes with `_stdin_reader` for the test
        # client's JSON-RPC writes. (Dropping `-i` from the exec args
        # is not enough — the subprocess still inherits the parent fd
        # by default; the explicit DEVNULL is the guarantee.)
        proc = subprocess.Popen(
            ["docker", "exec", cid, "tail", "-F", "-n", "+1", CONTAINER_LOG_FILE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        logger.warning("tail thread: failed to spawn `docker exec tail`: %r", exc)
        return
    forwarded_bytes = 0
    assert proc.stdout is not None
    try:
        while not stop_event.is_set():
            line = proc.stdout.readline()
            if not line:
                logger.info("tail thread: docker exec tail returned EOF — container has exited")
                return
            stderr.write(line)
            stderr.flush()
            forwarded_bytes += len(line.encode("utf-8", "replace"))
    finally:
        logger.info("tail thread: forwarded_bytes=%d", forwarded_bytes)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
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

    if args.image_tag is None:
        try:
            tag = derive_image_tag(ctx)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        logger.info("derived image tag %s from git HEAD", tag)
    else:
        tag = args.image_tag

    try:
        ensure_image(tag, args.rebuild, ctx)
    except subprocess.CalledProcessError as exc:
        logger.error("docker build failed (exit %d)", exc.returncode)
        return exc.returncode or 1

    container_name = _container_name(args.agent_name, args.session_id)
    try:
        cid = run_container(
            tag, args.mount, args.license_file, args.log_level,
            name=container_name,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("docker run failed (exit %d): %s", exc.returncode, exc.stderr or "")
        return exc.returncode or 1
    logger.info("container started cid=%s name=%s", cid[:12], container_name)

    stop_event = threading.Event()

    def shutdown(signum: int | None = None, frame: object | None = None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    tail_thread = threading.Thread(
        target=_tail_bridge_log,
        args=(cid, stop_event),
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
        # Tell the tail thread to wind down BEFORE stopping the
        # container so its final lines can drain through the docker
        # exec pipe; signal handlers set `stop_event` themselves but
        # the EOF-on-stdin path walks here without setting it.
        stop_event.set()
        tail_thread.join(timeout=2.0)
        try:
            stop_container(cid)
        except Exception as exc:
            logger.warning("cleanup of container %s failed: %r", cid[:12], exc)


if __name__ == "__main__":
    sys.exit(main())

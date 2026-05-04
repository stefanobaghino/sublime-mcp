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


def log(msg: str) -> None:
    """Print to stderr — stdout is the MCP transport."""
    print("[sublime-mcp-harness] %s" % msg, file=sys.stderr, flush=True)


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
    args = parser.parse_args(argv)
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
    log("building image %s (this can take a few minutes on first run)…" % tag)
    subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        check=True,
    )
    log("image %s built" % tag)


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
            log("reusing image %s" % tag)


def run_container(
    tag: str,
    mounts: list[str],
    license_file: str | None,
) -> str:
    args = [
        "docker", "run",
        "-d",
        "--rm",
        "--label", "sublime-mcp-harness=%d" % os.getpid(),
        "-p", "127.0.0.1:0:%d" % CONTAINER_PORT,
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
    while time.monotonic() < deadline:
        try:
            status, _ = http_post(url, payload, timeout=2.0)
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
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
                        return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_state = repr(exc)
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
        try:
            status, raw = http_post(url, stripped, timeout=PROXY_HTTP_TIMEOUT_S)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            err = _make_error_response(stripped, "container HTTP error: %r" % exc)
            stdout.write(err + b"\n")
            stdout.flush()
            continue
        if status == 202 or not raw:
            # Notification → no body, no stdout write.
            continue
        if status != 200:
            err = _make_error_response(
                stripped,
                "container returned HTTP %d: %s" % (status, raw[:200].decode("utf-8", "replace")),
            )
            stdout.write(err + b"\n")
            stdout.flush()
            continue
        # Pass through verbatim. The plugin already speaks MCP JSON-RPC.
        stdout.write(raw)
        if not raw.endswith(b"\n"):
            stdout.write(b"\n")
        stdout.flush()


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
# Top-level


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))

    ok, reason = docker_available()
    if not ok:
        log("ERROR: %s. Install Docker Desktop or the Docker Engine, "
            "ensure the daemon is running, and retry." % reason)
        return 2

    try:
        ctx = build_context_dir()
    except RuntimeError as exc:
        log("ERROR: %s" % exc)
        return 2

    try:
        ensure_image(args.image_tag, args.rebuild, ctx)
    except subprocess.CalledProcessError as exc:
        log("ERROR: docker build failed (exit %d)" % exc.returncode)
        return exc.returncode or 1

    try:
        cid = run_container(args.image_tag, args.mount, args.license_file)
    except subprocess.CalledProcessError as exc:
        log("ERROR: docker run failed (exit %d): %s" % (exc.returncode, exc.stderr or ""))
        return exc.returncode or 1

    stop_event = threading.Event()

    def shutdown(signum: int | None = None, frame: object | None = None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        port = host_port(cid)
        deadline = time.monotonic() + READINESS_TIMEOUT_S
        wait_for_ready(port, deadline)
        wait_for_window(port, deadline)
        log("ready on 127.0.0.1:%d (container %s)" % (port, cid[:12]))
        proxy_loop(port, stop_event)
        return 0
    except Exception as exc:
        log("ERROR: %s" % exc)
        return 1
    finally:
        try:
            stop_container(cid)
        except Exception as exc:
            log("warning: cleanup of container %s failed: %r" % (cid[:12], exc))


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end smoke test for the stdio harness.

Spawns harness.py as a subprocess, drives it over stdin/stdout, and
asserts the round-trip works against a real Docker container running
Sublime Text + the plugin.

Skips if `docker --version` does not work — the test is meaningless
without Docker. CI is expected to pre-`docker build` the image so the
first invocation here only pays container boot, not image build.

Run standalone (`python3 tests/test_harness_smoke.py`) or via pytest;
the script self-skips on either entrypoint.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "harness.py"
# Matches the unified-log "ready on 127.0.0.1:<port> (container ...)" line.
# Re-stamping the harness's `log()` helper to use `logging` shifted the
# prefix from `[sublime-mcp-harness] ready` to `[harness]  req=-  ready`;
# the substring `ready on` is stable across both.
READY_LINE = b"ready on 127.0.0.1"
READY_TIMEOUT_S = 600.0  # cold-build budget; subsequent runs are fast


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _wait_ready(proc: subprocess.Popen, deadline: float) -> None:
    """Read stderr until we see the ready marker, mirroring lines through."""
    assert proc.stderr is not None
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    "harness exited before readiness (status %d)" % proc.returncode
                )
            time.sleep(0.05)
            continue
        sys.stderr.write(line.decode("utf-8", "replace"))
        if READY_LINE in line:
            return
    raise RuntimeError("harness did not signal readiness within %.0fs" % READY_TIMEOUT_S)


def _send(proc: subprocess.Popen, message: dict) -> None:
    assert proc.stdin is not None
    payload = (json.dumps(message) + "\n").encode("utf-8")
    proc.stdin.write(payload)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen, timeout_s: float) -> dict:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            return json.loads(line.decode("utf-8"))
        if proc.poll() is not None:
            raise RuntimeError("harness exited while awaiting response")
        time.sleep(0.05)
    raise RuntimeError("no response from harness within %.0fs" % timeout_s)


def run() -> int:
    if not _docker_available():
        print("SKIP: docker not available", file=sys.stderr)
        return 0

    # `python -u` keeps stdout/stderr unbuffered so we see ready promptly.
    proc = subprocess.Popen(
        [sys.executable, "-u", str(HARNESS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO),
    )
    deadline = time.monotonic() + READY_TIMEOUT_S
    try:
        _wait_ready(proc, deadline)

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "harness-smoke", "version": "0"},
            },
        })
        resp = _recv(proc, timeout_s=15.0)
        assert resp.get("id") == 1, resp
        server = resp.get("result", {}).get("serverInfo", {})
        assert server.get("name") == "sublime-mcp", resp

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "exec_sublime_python",
                "arguments": {"code": "print(sublime.version())"},
            },
        })
        resp = _recv(proc, timeout_s=30.0)
        assert resp.get("id") == 2, resp
        content = resp.get("result", {}).get("content") or []
        assert content and content[0].get("type") == "text", resp
        outcome = json.loads(content[0]["text"])
        output = (outcome.get("output") or "").strip()
        assert output.isdigit(), outcome
        assert int(output) >= 4000, outcome  # ST 4 build
        print("PASS: harness round-trips, ST build %s" % output)
        return 0
    finally:
        if proc.poll() is None:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        # Drain anything left on stderr so it shows up in CI logs.
        if proc.stderr is not None:
            tail = proc.stderr.read()
            if tail:
                sys.stderr.write(tail.decode("utf-8", "replace"))


def test_harness_round_trips() -> None:
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())

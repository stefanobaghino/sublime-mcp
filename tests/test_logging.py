"""End-to-end test for the unified log stream.

Spawns harness.py and asserts on the shape and content of its stderr
trail across a real Docker round-trip. Skips if Docker is unavailable.

Coverage:

(a) Happy-path log shape and the request-correlation trail across
    components for a `tools/call` at the default INFO level.
(b) Wedge legibility regression — a synthetic snippet that wedges ST's
    main thread; assert the unified trail makes the wedge unambiguous.
    Slow (>60 s); gated behind RUN_SLOW_LOG_TESTS=1.

The tail-thread soundness scenarios (stress, fd-leak,
container-died-mid-stream) from the §Verification table aren't here:
the stress and fd-leak require many cycles; container-died-mid-stream
needs a separate runner because killing the container while the
harness is mid-call interferes with the other (a/b) tests. They're
checked by hand in the dev loop until they justify a test fixture.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "harness.py"
READY_TOKEN = b"ready on 127.0.0.1"
READY_TIMEOUT_S = 600.0
DEFAULT_CALL_TIMEOUT_S = 30.0


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# Log line: <ts>.<ms>  <LEVEL>  [<component>]  req=<id>  <message>
# Two spaces between columns, exactly. Allow trailing message to be
# anything (including spaces).
LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})"
    r"  (?P<level>DEBUG|INFO|WARNING|ERROR)\s*"
    r"  \[(?P<component>harness|bridge|st)\]"
    r"  req=(?P<req>\S+)"
    r"  (?P<msg>.*)$"
)


def _spawn_harness(env: dict | None = None) -> subprocess.Popen:
    full_env = os.environ.copy()
    full_env["PYTHONUNBUFFERED"] = "1"
    if env:
        full_env.update(env)
    return subprocess.Popen(
        [sys.executable, "-u", str(HARNESS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO),
        env=full_env,
    )


def _wait_ready(proc: subprocess.Popen, captured: list, deadline: float) -> None:
    """Read stderr lines into `captured` until the ready marker appears."""
    assert proc.stderr is not None
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    "harness exited before readiness (status %d)\n%s"
                    % (proc.returncode, b"".join(captured).decode("utf-8", "replace"))
                )
            time.sleep(0.05)
            continue
        captured.append(line)
        if READY_TOKEN in line:
            return
    raise RuntimeError("harness did not signal readiness within %.0fs" % READY_TIMEOUT_S)


def _drain_stderr(proc: subprocess.Popen, captured: list, until_s: float = 1.0) -> None:
    """Pull whatever stderr lines are sitting in the pipe up to `until_s`."""
    assert proc.stderr is not None
    deadline = time.monotonic() + until_s
    # Flip the stderr pipe to non-blocking for a short window so we can
    # bail out cleanly when no more data is queued.
    import fcntl
    fd = proc.stderr.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while time.monotonic() < deadline:
            chunk = proc.stderr.read(65536)
            if chunk is None:
                time.sleep(0.05)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            captured.append(chunk)
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)


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


def _shutdown(proc: subprocess.Popen) -> None:
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


def _parsed_lines(captured: list) -> list:
    """Return the list of parsed log lines (those matching `LOG_LINE_RE`)."""
    blob = b"".join(captured).decode("utf-8", "replace")
    out = []
    for line in blob.splitlines():
        m = LOG_LINE_RE.match(line)
        if m:
            out.append(m.groupdict())
    return out


# UnitTesting (the SublimeText/UnitTesting GitHub action) auto-discovers
# every `test_*.py` under `tests/` and runs it inside ST's plugin host.
# This module drives a Docker subprocess, so it would always error out
# in that context — gate the whole class so it's recorded as skipped.
_RUNNING_IN_SUBLIME = "sublime" in sys.modules


@unittest.skipIf(
    _RUNNING_IN_SUBLIME,
    "test_logging.py drives a host-side Docker harness; not applicable inside ST plugin host",
)
class TestUnifiedLogging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _docker_available():
            raise unittest.SkipTest("docker not available")

    def test_info_trail_for_tools_call(self) -> None:
        """At INFO level, a single `tools/call` produces a clean req-correlated trail."""
        proc = _spawn_harness()
        captured: list = []
        try:
            _wait_ready(proc, captured, time.monotonic() + READY_TIMEOUT_S)
            _send(proc, {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "exec_sublime_python",
                    "arguments": {"code": "_ = sublime.version()"},
                },
            })
            resp = _recv(proc, timeout_s=DEFAULT_CALL_TIMEOUT_S)
            self.assertEqual(resp.get("id"), 99, resp)
            # Give the bridge time to flush its trailing log lines into
            # the unified stream before we start parsing.
            _drain_stderr(proc, captured, until_s=1.5)
        finally:
            _shutdown(proc)
            if proc.stderr is not None:
                tail = proc.stderr.read()
                if tail:
                    captured.append(tail)

        lines = _parsed_lines(captured)
        self.assertGreater(len(lines), 0, "no log lines parsed at all")
        # Format invariant: every line has the expected components.
        for ln in lines:
            self.assertIn(ln["level"], {"DEBUG", "INFO", "WARNING", "ERROR"})
            self.assertIn(ln["component"], {"harness", "bridge", "st"})

        # The trail for req=99 should include worker entered + exec
        # begin + exec done from the bridge, all sharing req=99.
        for_99 = [ln for ln in lines if ln["req"] == "99"]
        bridge_for_99 = [ln for ln in for_99 if ln["component"] == "bridge"]
        msgs = " | ".join(ln["msg"] for ln in bridge_for_99)
        # Dump the full stderr blob inline on failure so CI logs reveal
        # whether bridge lines never arrived vs. arrived but with the
        # wrong req-id.
        full_blob = b"".join(captured).decode("utf-8", "replace")
        diag = "\n--- captured stderr ---\n%s\n--- end captured ---" % full_blob
        self.assertIn("worker entered", msgs, "missing 'worker entered': %s%s" % (msgs, diag))
        self.assertIn("snippet exec begin", msgs, "missing 'snippet exec begin': %s%s" % (msgs, diag))
        self.assertIn("snippet exec done", msgs, "missing 'snippet exec done': %s%s" % (msgs, diag))

    def test_format_columns_present_at_default_level(self) -> None:
        """Boot-time log lines render in the expected column shape."""
        proc = _spawn_harness()
        captured: list = []
        try:
            _wait_ready(proc, captured, time.monotonic() + READY_TIMEOUT_S)
        finally:
            _shutdown(proc)
            if proc.stderr is not None:
                tail = proc.stderr.read()
                if tail:
                    captured.append(tail)

        lines = _parsed_lines(captured)
        self.assertGreater(len(lines), 0, "no parseable log lines from boot")
        # At least one INFO [harness] line about boot lifecycle.
        info_harness = [ln for ln in lines if ln["level"] == "INFO" and ln["component"] == "harness"]
        self.assertGreater(len(info_harness), 0, "expected INFO [harness] lines from boot")

    @unittest.skipUnless(
        os.environ.get("RUN_SLOW_LOG_TESTS") == "1",
        "set RUN_SLOW_LOG_TESTS=1 to run the >60s wedge regression",
    )
    def test_wedge_logs_faulthandler(self) -> None:
        """A snippet that wedges ST's main thread produces the canonical #73 trail."""
        proc = _spawn_harness()
        captured: list = []
        try:
            _wait_ready(proc, captured, time.monotonic() + READY_TIMEOUT_S)
            # Schedule a 70 s sleep on ST's main thread, then call
            # run_on_main with a long inner timeout so the worker's
            # 60 s ceiling fires first — that's the path that emits
            # the ERROR + faulthandler dump we want to assert on. With
            # a short inner timeout (<60 s) the worker exits cleanly
            # via TimeoutError before the wedge regression seam
            # triggers, and the trail is materially different.
            wedge_code = (
                "import time\n"
                "sublime.set_timeout(lambda: time.sleep(70), 0)\n"
                "run_on_main(lambda: None, timeout=120.0)\n"
            )
            _send(proc, {
                "jsonrpc": "2.0",
                "id": 7373,
                "method": "tools/call",
                "params": {
                    "name": "exec_sublime_python",
                    "arguments": {"code": wedge_code},
                },
            })
            resp = _recv(proc, timeout_s=80.0)
            self.assertEqual(resp.get("id"), 7373, resp)
            outcome = json.loads(resp["result"]["content"][0]["text"])
            self.assertIn("timed out", outcome.get("error") or "", outcome)
            _drain_stderr(proc, captured, until_s=2.0)
        finally:
            _shutdown(proc)
            if proc.stderr is not None:
                tail = proc.stderr.read()
                if tail:
                    captured.append(tail)

        blob = b"".join(captured).decode("utf-8", "replace")
        # ERROR line at the 60 s ceiling, with the live req-id and the
        # `is_alive=True` flag distinguishing wedge from worker death.
        self.assertRegex(
            blob,
            r"ERROR\s+\[bridge\]\s+req=7373\s+worker did not complete in 60\.0s; worker thread is_alive=True",
            "expected worker-timeout ERROR with req=7373",
        )
        # faulthandler dumps every Python thread with its frame stack.
        # The wedged worker should show in `run_on_main` waiting on
        # `threading.wait`. Looser assertion — the literal string
        # `Thread 0x` appears only in faulthandler output.
        self.assertIn(
            "Thread 0x",
            blob,
            "expected faulthandler thread dump",
        )
        self.assertIn(
            "run_on_main",
            blob,
            "expected wedged worker frame in run_on_main",
        )


def run() -> int:
    if not _docker_available():
        print("SKIP: docker not available", file=sys.stderr)
        return 0
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestUnifiedLogging)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run())

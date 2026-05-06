"""Headless-ST integration smoke test.

Pins `open_view`'s headless guard against *real* ST: closes all
windows from inside ST's own plugin host (via `window.run_command(
"close_window")`), calls `open_view` over MCP, asserts the response
carries `isError: true` with `RuntimeError`, `no open window`, and
`install.md` in the error string.

Distinct from `tests/test_helpers.py::TestHeadlessGuard`, which
overrides the `_get_active_window` / `_get_windows` seams in the
snippet's globals: that test only confirms the guard's predicate
matches what we mock those calls to return. This script confirms the
predicate matches what *real* ST reports under zero-window state. If
a future ST release changed e.g. `windows()` to raise rather than
return `[]`, the hermetic test would still pass but production
behaviour would shift; this script catches that.

Window control runs entirely through MCP (no `osascript`).
GitHub-hosted macOS runners have no interactive user session, so
`tell application "Sublime Text" to close every window` blocks for
the AppleEvent default timeout (~120 s) and then errors with -1712.
Driving ST from inside its own plugin host bypasses that limitation
and is also cross-platform — this script works wherever ST and
Python 3 are installed.

Standalone (not under UnitTesting). Pre-conditions:
  - Sublime Text is running with the sublime-mcp plugin loaded.
  - The plugin is listening on `MCP_URL` (default
    `http://127.0.0.1:47823/mcp`).
  - At least one ST window is open at script start.

Local invocation example:

    open -a "Sublime Text"
    python3 tests/headless_smoke.py

Save or discard unsaved work first — the close call uses
`run_command("close_window")` and would block on the unsaved-buffer
dialog the same way the manual `osascript` recipe in
`skills/sublime-mcp/install.md` does.

CI: invoked via `.github/workflows/headless.yml`.

Exit 0 on success; non-zero with a diagnostic on any pre-condition
or assertion failure.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

MCP_URL = os.environ.get("SUBLIME_MCP_URL", "http://127.0.0.1:47823/mcp")
PROBE_PATH = "/tmp/sublime_mcp_headless_smoke_probe"
RECOVERY_TIMEOUT_S = 15.0
SERVER_POLL_TIMEOUT_S = 30.0
WINDOW_TRANSITION_TIMEOUT_S = 30.0


def _post(payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _exec(code, timeout=10):
    return _post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "exec_sublime_python",
            "arguments": {"code": code},
        },
    }, timeout=timeout)


def _outcome(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def _wait_for_server():
    deadline = time.time() + SERVER_POLL_TIMEOUT_S
    last_exc = None
    while time.time() < deadline:
        try:
            _post({
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "headless-smoke", "version": "0"},
                },
            }, timeout=2)
            return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.5)
    raise SystemExit(
        "headless_smoke: MCP server at %s did not respond within %ss "
        "(last error: %r). Pre-condition: ST must be running with the "
        "sublime-mcp plugin loaded." % (MCP_URL, SERVER_POLL_TIMEOUT_S, last_exc)
    )


def _window_count():
    resp = _exec("print(len(sublime.windows()))")
    o = _outcome(resp)
    if o.get("error"):
        raise SystemExit(
            "headless_smoke: window-count probe failed: %s" % o["error"]
        )
    return int(o["output"].strip())


def _close_all_windows():
    # Drive close_window per window from inside ST's plugin host.
    # set_timeout dispatches to the main thread (run_command requires
    # it). The closure is async; the worker observes the steady state
    # via window-count polling after this call returns.
    code = (
        "def _close_all():\n"
        "    for w in list(sublime.windows()):\n"
        "        w.run_command('close_window')\n"
        "sublime.set_timeout(_close_all, 0)\n"
    )
    resp = _exec(code)
    o = _outcome(resp)
    if o.get("error"):
        raise SystemExit(
            "headless_smoke: close_all dispatch failed: %s" % o["error"]
        )


def _open_window():
    # `new_window` is an application-level command; dispatch via the
    # `sublime.run_command` form (no window object required). Idempotent
    # against an already-recovered state.
    resp = _exec(
        "sublime.set_timeout(lambda: sublime.run_command('new_window'), 0)"
    )
    o = _outcome(resp)
    if o.get("error"):
        raise SystemExit(
            "headless_smoke: new_window dispatch failed: %s" % o["error"]
        )


def _wait_for_window_count(predicate, timeout):
    # Returns (final_count, elapsed_s). final_count is None only if every
    # poll raised before the deadline (ST unreachable). Otherwise it's
    # the last observed integer count, even when the predicate never
    # matched within the budget — useful for diagnostics in the caller.
    deadline = time.time() + timeout
    start = time.time()
    last_n = None
    while time.time() < deadline:
        try:
            n = _window_count()
            last_n = n
        except Exception:
            n = None
        if n is not None and predicate(n):
            return n, time.time() - start
        time.sleep(0.2)
    return last_n, time.time() - start


def main():
    _wait_for_server()

    # Baseline: ST must have at least one window. Failing this
    # pre-condition is operator error, not a regression in the
    # guard. Fail loudly with the recovery hint.
    n = _window_count()
    if n < 1:
        raise SystemExit(
            "headless_smoke: pre-condition failed — ST has %d windows "
            "open at script start; need >=1. Run `open -a 'Sublime "
            "Text'` (or platform equivalent) and retry." % n
        )

    # Drive the scenario: close all windows, confirm zero, probe.
    _close_all_windows()
    n, elapsed = _wait_for_window_count(
        lambda n: n == 0, timeout=WINDOW_TRANSITION_TIMEOUT_S
    )
    if n != 0:
        raise SystemExit(
            "headless_smoke: ST still reports %r windows after "
            "%.1fs of close_window dispatch (budget: %.1fs). Possible "
            "causes: unsaved-buffer dialog blocking close, ST main "
            "thread saturated, or ST process not reachable."
            % (n, elapsed, WINDOW_TRANSITION_TIMEOUT_S)
        )

    resp = _exec("open_view(%r)" % PROBE_PATH)
    outcome = _outcome(resp)

    # Recover *before* asserting: we don't want a failed assertion to
    # leave ST headless on the user's session. Idempotent on already-
    # recovered state.
    _open_window()
    _wait_for_window_count(lambda n: n >= 1, timeout=RECOVERY_TIMEOUT_S)

    failures = []
    if not resp.get("result", {}).get("isError"):
        failures.append(
            "expected isError=true; got %r" % resp.get("result", {}).get("isError")
        )
    err = outcome.get("error") or ""
    for needle in ("RuntimeError", "no open window", "install.md"):
        if needle not in err:
            failures.append("expected %r in error string; got %r" % (needle, err[:200]))

    if failures:
        raise SystemExit(
            "headless_smoke: %d assertion failure(s):\n  - %s"
            % (len(failures), "\n  - ".join(failures))
        )

    print("headless_smoke: OK — guard fired with expected error string")
    return 0


if __name__ == "__main__":
    sys.exit(main())

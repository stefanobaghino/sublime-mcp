"""
sublime-mcp: a Sublime Text plugin that hosts a Model Context Protocol
server. Exposes a single tool, `exec_sublime_python`, over MCP Streamable
HTTP on loopback. The tool runs arbitrary Python inside ST's plugin host
so MCP clients (e.g. AI coding agents) can drive the editor
programmatically.

Security: binds 127.0.0.1 only; exec'ing arbitrary Python is equivalent to
an open console. Do not expose the port beyond localhost.
"""

import contextlib
import io
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sublime
import sublime_plugin  # noqa: F401  (exposed to exec'd snippets)


HOST = "127.0.0.1"
PORT = 47823
ENDPOINT = "/mcp"

EXEC_TIMEOUT_SECONDS = 60.0
OPEN_FILE_TIMEOUT_SECONDS = 5.0

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "sublime-mcp"
SERVER_VERSION = "0.1.0"


TOOL_DESCRIPTION = """\
Run Python inside Sublime Text's plugin host (Python 3.8) and get its
output back. Use this to answer "what does ST actually do" questions:
scope at a cursor, whether a syntax test passes, what resources ST
knows about, etc.

## What's in scope

The snippet runs on ST's async worker thread (`sublime.set_timeout_async`),
so it can wait on file loads and build panels without deadlocking ST's
UI. The following names are preloaded:

- `sublime`, `sublime_plugin` — the ST Python API modules.
- `scope_at(path, row, col) -> str` — opens the file, returns
  `view.scope_name` at the 0-indexed (row, col). Rows and cols are
  0-indexed, matching ST's API (a syntax-test assertion on line 181
  col 9 corresponds to `row=180, col=8`).
- `run_syntax_tests(path) -> dict` — opens the file and runs the
  "Syntax Tests" build variant. Returns
  `{"ok": bool, "summary": str, "output": str}`. `ok` is True when
  the build panel ends with "assertions passed" and no failures.
- `reload_syntax(resource_path) -> None` — force-reloads a
  `.sublime-syntax` resource. Useful when ST cached an older version
  (e.g. after an external edit via symlink).
- `find_resources(pattern) -> list[str]` — wraps
  `sublime.find_resources(pattern)`.
- `open_view(path) -> sublime.View` — opens the file, polls
  `is_loading()` up to 5 s, returns the View.

## Output protocol

Anything you `print(...)` is captured and returned as `output`. If you
assign a value to `_`, its `repr` is returned as `result`. Exceptions
are caught, formatted, and returned as `error` (with whatever was
printed up to that point still in `output`). The response shape is:

```
{"ok": bool, "output": str, "result": str|null, "error": str|null}
```

## Recipes

### Scope at a position

```python
# syntax_test_Generics.cs line 181 col 9 → row=180, col=8
print(scope_at("/path/to/Packages/C#/tests/syntax_test_Generics.cs", 180, 8))
```

### Run syntax tests on a file

```python
r = run_syntax_tests("/path/to/Packages/C#/tests/syntax_test_Generics.cs")
print(r["summary"])
if not r["ok"]:
    print(r["output"])
```

### Reload a syntax file after an external edit

```python
reload_syntax("Packages/C#/C#.sublime-syntax")
```

### List resources by pattern

```python
_ = find_resources("*.sublime-syntax")
```

### Inspect the active window / view

```python
w = sublime.active_window()
v = w.active_view()
print(v.file_name(), v.sel()[0], v.scope_name(v.sel()[0].a))
```

## Gotchas

- Hard timeout per call is 60 s.
- The snippet runs on ST's async worker thread. Most of the ST API is
  thread-safe, but a few mutating operations (`TextCommand` edit tokens)
  require the main thread — call `sublime.set_timeout(lambda: ..., 0)`
  from within the snippet if you need that, and poll for completion.
- File paths must be absolute for `scope_at` / `run_syntax_tests` /
  `open_view`. `find_resources` uses ST's `Packages/...` virtual paths.
- `run_syntax_tests` is async inside ST; this helper polls the build
  panel for up to ~15 s for completion.
"""


HELPERS_SOURCE = r'''
import time as _time


def open_view(path, timeout=5.0):
    window = sublime.active_window()
    view = window.open_file(path)
    deadline = _time.time() + timeout
    while view.is_loading() and _time.time() < deadline:
        _time.sleep(0.02)
    if view.is_loading():
        raise TimeoutError("open_view: still loading after %ss: %s" % (timeout, path))
    window.focus_view(view)
    return view


def scope_at(path, row, col):
    view = open_view(path)
    point = view.text_point(row, col)
    return view.scope_name(point).rstrip()


def reload_syntax(resource_path):
    # Touch the resource via sublime_plugin to force ST to re-read it.
    # sublime_plugin.reload_plugin is for .py plugins; for .sublime-syntax
    # we leverage the fact that ST reloads a syntax when a view using it
    # is reactivated after the resource changes. The pragmatic workaround
    # is to re-open any view bound to the syntax.
    for window in sublime.windows():
        for view in window.views():
            settings = view.settings()
            if settings.get("syntax") == resource_path:
                view.assign_syntax(resource_path)


def find_resources(pattern):
    return list(sublime.find_resources(pattern))


def run_syntax_tests(path, poll_timeout=15.0):
    window = sublime.active_window()
    view = open_view(path)
    window.run_command("build", {"variant": "Syntax Tests"})
    panel = window.find_output_panel("exec") or window.get_output_panel("exec")
    deadline = _time.time() + poll_timeout
    last = ""
    while _time.time() < deadline:
        text = panel.substr(sublime.Region(0, panel.size()))
        if text and text == last and (
            "assertions passed" in text
            or "FAILED" in text
            or "[Finished" in text
        ):
            break
        last = text
        _time.sleep(0.1)
    text = panel.substr(sublime.Region(0, panel.size()))
    summary_line = ""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if "assertions" in stripped or "FAILED" in stripped:
            summary_line = stripped
            break
    ok = "assertions passed" in text and "FAILED" not in text
    return {"ok": ok, "summary": summary_line, "output": text}
'''


def _exec_on_worker(code):
    """Run `code` on ST's async worker thread and collect output.

    Returns a dict with keys `ok`, `output`, `result`, `error`.
    """
    done = threading.Event()
    result = {"ok": False, "output": "", "result": None, "error": None}

    def run():
        buf_out = io.StringIO()
        namespace = {
            "__name__": "sublime_mcp_exec",
            "__builtins__": __builtins__,
            "sublime": sublime,
            "sublime_plugin": sublime_plugin,
        }
        try:
            exec(compile(HELPERS_SOURCE, "<sublime-mcp-helpers>", "exec"), namespace)
        except Exception:
            result["error"] = "helper init failed:\n" + traceback.format_exc()
            done.set()
            return
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_out):
                exec(compile(code, "<sublime-mcp-snippet>", "exec"), namespace)
            result["ok"] = True
            if "_" in namespace and namespace["_"] is not None:
                try:
                    result["result"] = repr(namespace["_"])
                except Exception:
                    result["result"] = "<repr failed>"
        except Exception:
            result["error"] = traceback.format_exc()
        finally:
            result["output"] = buf_out.getvalue()
            done.set()

    # Dispatch on ST's async worker thread, not the main UI thread. Snippets
    # typically wait on ST state (file loads, build panels), and waiting on
    # the main thread would deadlock — ST's event loop couldn't progress.
    # Most of the ST API is thread-safe; the async thread is the documented
    # home for long-running plugin work.
    sublime.set_timeout_async(run, 0)
    if not done.wait(EXEC_TIMEOUT_SECONDS):
        return {
            "ok": False,
            "output": "",
            "result": None,
            "error": "exec timed out after %ss" % EXEC_TIMEOUT_SECONDS,
        }
    return result


def _tool_descriptor():
    return {
        "name": "exec_sublime_python",
        "description": TOOL_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source to exec inside Sublime Text's plugin host.",
                }
            },
            "required": ["code"],
        },
    }


def _dispatch(message):
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [_tool_descriptor()]},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "exec_sublime_python":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "unknown tool: %s" % name},
            }
        code = arguments.get("code")
        if not isinstance(code, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "arguments.code must be a string"},
            }
        outcome = _exec_on_worker(code)
        text = json.dumps(outcome, indent=2, default=str)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": not outcome["ok"],
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "method not found: %s" % method},
    }


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (SERVER_NAME, SERVER_VERSION)

    def do_POST(self):
        if self.path.split("?", 1)[0] != ENDPOINT:
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            message = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            self._send_json(400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error: %s" % exc},
            })
            return

        if isinstance(message, list):
            responses = [r for r in (_dispatch(m) for m in message) if r is not None]
            if not responses:
                self.send_response(202)
                self.end_headers()
                return
            self._send_json(200, responses)
            return

        response = _dispatch(message)
        if response is None:
            self.send_response(202)
            self.end_headers()
            return
        self._send_json(200, response)

    def do_GET(self):
        self._send_json(405, {"error": "this server does not serve a GET stream"})

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def _send_json(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


_server = None
_thread = None


def plugin_loaded():
    global _server, _thread
    if _server is not None:
        return
    try:
        _server = ThreadingHTTPServer((HOST, PORT), MCPHandler)
    except OSError as exc:
        print("[sublime-mcp] failed to bind %s:%d — %s" % (HOST, PORT, exc))
        _server = None
        return
    _thread = threading.Thread(
        target=_server.serve_forever,
        name="sublime-mcp-http",
        daemon=True,
    )
    _thread.start()
    print("[sublime-mcp] listening on %s:%d%s" % (HOST, PORT, ENDPOINT))


def plugin_unloaded():
    global _server, _thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        finally:
            _server = None
            _thread = None
            print("[sublime-mcp] stopped")

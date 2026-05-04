"""
sublime-mcp: a Sublime Text plugin that hosts a Model Context Protocol
server. Exposes a single tool, `exec_sublime_python`, over MCP Streamable
HTTP on loopback. The tool runs arbitrary Python inside ST's plugin host
so MCP clients (e.g. AI coding agents) can drive the editor
programmatically.

Security: binds 127.0.0.1 only; exec'ing arbitrary Python is equivalent to
an open console. Do not expose the port beyond localhost.
"""

import builtins as _builtins
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
Query Sublime Text for ground-truth answers about scopes, syntax
resolution, or syntax-test outcomes. Backed by ST's Python plugin
host (3.8) — run arbitrary Python against a live ST instance and
capture its output.

## What's in scope

The snippet runs on a dedicated daemon thread so it can wait on file
loads and build panels without deadlocking ST's UI. Most of the ST
API is thread-safe from any thread; for the few operations that
require the main UI thread (e.g. `TextCommand` edit tokens), wrap
them in `run_on_main(...)`. The following names are preloaded:

- `sublime`, `sublime_plugin` — the ST Python API modules.
- `scope_at(path, row, col) -> dict` — opens the file, returns
  `{"scope": str, "resolved_syntax": str | None}`. `scope` is
  `view.scope_name` at the 0-indexed (row, col); rows and cols are
  0-indexed, matching ST's API (a syntax-test assertion on line 181
  col 9 corresponds to `row=180, col=8`). `resolved_syntax` is
  `view.syntax().path` — the URI of the syntax ST actually loaded,
  or `None` if no syntax resolved (extensionless files, bogus URIs).
  **Use `scope_at_test` for files with no extension. `scope_at` does
  not parse the `# SYNTAX TEST` header**: ST falls back to Plain Text
  (`resolved_syntax == "Packages/Text/Plain text.tmLanguage"`,
  `scope == "text.plain"`) and the caller can detect this by checking
  `resolved_syntax`.
- `scope_at_test(path, row, col) -> dict` — like `scope_at`, but
  parses the `SYNTAX TEST "Packages/..."` header on line 0 and
  assigns that syntax to the view before sampling the scope. Returns
  `{"scope", "resolved_syntax", "requested_syntax"}` — `requested_syntax`
  is the URI from the header; `resolved_syntax` is what ST loaded
  (`view.syntax().path`, or `None` if the URI doesn't resolve to a
  real syntax). The right helper for extension-less syntax-test files.
- `resolve_position(path, row, col, syntax_path=None) -> dict` —
  returns the full position disambiguation for `(row, col)`. See
  "text_point overflow" below. Optional `syntax_path` calls
  `assign_syntax_and_wait` on the view first. Response carries
  `requested_syntax` (echo of the `syntax_path` arg, or `None`) and
  `resolved_syntax` (ST's `view.syntax().path`, or `None`).
- `run_syntax_tests(path) -> dict` — returns
  `{"state": str, "summary": str, "output": str, "failures": list[str]}`.
  `state` is one of `"passed"` (all assertions matched) or
  `"failed"` (the runner completed but some assertions did not
  match). `summary` is an assertion-count headline. `failures` is
  one entry per failed assertion with ST's own diagnostic
  (file:row:col, "error: scope does not match", then the
  expected/actual snippet); populated only when
  `state == "failed"`. Cases where ST cannot complete the run
  (resource not yet indexed, path outside `sublime.packages_path()`,
  private `sublime_api.run_syntax_test` missing) raise
  `RuntimeError`, surfaced in the top-level `error` field of the
  MCP response — the same channel any other helper failure uses.
  Files outside Packages/ must be symlinked in (the helper walks
  symlinks); see SKILL.md section 4.
- `run_inline_syntax_test(content, name) -> dict` — for synthetic
  probes ("what does ST do on this case?"). Writes `content` to a
  per-call temp dir under `Packages/User/`, runs ST's syntax-test
  runner, cleans up. Same `{state, summary, output, failures}`
  contract as `run_syntax_tests`, plus a third state
  `"inconclusive"` when ST never indexes the temp resource within
  the wait budget (looser than `run_syntax_tests`, which raises —
  fresh-resource probes hit indexing latency too often for raise to
  be the right default). The header inside `content` chooses the
  syntax under test; the syntax must already be reachable.
- `reload_syntax(resource_path) -> None` — force-reloads a
  `.sublime-syntax` resource. Useful when ST cached an older version
  (e.g. after an external edit via symlink).
- `find_resources(pattern) -> list[str]` — wraps
  `sublime.find_resources(pattern)`.
- `open_view(path) -> sublime.View` — opens the file, polls
  `is_loading()` up to 5 s, returns the View.
- `assign_syntax_and_wait(view, resource_path, timeout=2.0) -> None`
  — assigns a syntax and best-effort waits for tokenisation to touch
  point 0. Stage 1 (wait for the syntax setting to apply) is
  deterministic; stage 2 (tokenisation) is best-effort — ST has no
  public tokenisation-complete signal. For large files, re-read
  `scope_name` after use rather than trusting the helper.
- `run_on_main(callable, timeout=2.0)` — schedules `callable` on
  ST's main thread, waits for it to finish, returns its value (or
  re-raises whatever it raised). Use it for buffer-mutating
  `TextCommand` calls (`view.run_command("append", ...)` and friends)
  which silently no-op when invoked from the worker thread.

## text_point overflow

`view.text_point(row, col)` does **not** clamp `col` to the row's
content length when the column overflows past EOL. Instead, it
advances linearly into subsequent rows' offsets. Example:

```python
# Row 71 is 28 chars + "\\n". Asking for col 29:
view.text_point(71, 29)    # => 2809
view.rowcol(2809)          # => (72, 0)  ← overflowed to next row
```

This is load-bearing for ST's `syntax_test` framework: past-EOL
assertion columns evaluate against the corresponding column on the
*next* line, which is why some "impossible" past-EOL assertions
actually pass on ST. Use `resolve_position` to surface this
explicitly — its `overflow` (wrapped into a later row) and `clamped`
(request was past EOF; point == view.size()) fields disambiguate the
cases. **`overflow` and `clamped` are mutually exclusive**: if the
request is past EOF, `clamped` wins and `overflow` stays False
regardless of row wrapping.

## Output protocol

Anything you `print(...)` is captured and returned as `output`. If you
assign a value to `_`, its `repr` is returned as `result`. Exceptions
are caught, formatted, and returned as `error` (with whatever was
printed up to that point still in `output`). Only `print(...)` is
captured — direct writes to `sys.stderr` / `sys.stdout` are not,
because the capture is a per-call `print` override rather than a
global stream redirect (needed for thread-safety under concurrent
requests). The response shape is:

```
{
  "output": str,
  "result": str|null,
  "error": str|null,
  "st_version": int,
  "st_channel": str
}
```

`error is null` means the snippet ran to completion. Helper failures
(e.g. `run_syntax_tests` cannot complete the run) raise and surface
in this same `error` field — there is no separate helper-level
error channel. `st_version` (e.g. `4200`) and `st_channel` (e.g.
`"stable"`, `"dev"`) echo the running Sublime Text build on every
response, so callers can detect channel mismatches before treating
scope output as ground truth.

## Recipes

### Scope at a position

```python
# syntax_test_Generics.cs line 181 col 9 → row=180, col=8
r = scope_at("/path/to/Packages/C#/tests/syntax_test_Generics.cs", 180, 8)
print(r["scope"], "via", r["resolved_syntax"])
```

### Scope on an extension-less syntax-test file

```python
# File has no extension; `scope_at` would default to "text.plain".
# scope_at_test parses `# SYNTAX TEST "Packages/..."` on line 0.
r = scope_at_test("/path/to/syntax_test_git_config", 71, 28)
print(r["scope"])
```

### Resolve a past-EOL position

```python
r = resolve_position("/path/to/syntax_test_git_config", 71, 29,
                     syntax_path="Packages/Git Formats/Git Config.sublime-syntax")
if r["overflow"]:
    print("wrapped into row", r["actual"][0], "col", r["actual"][1])
elif r["clamped"]:
    print("past EOF; point clamped to", r["point"])
print("scope:", r["scope"])
```

### Run syntax tests on a file

```python
r = run_syntax_tests("/path/to/Packages/C#/tests/syntax_test_Generics.cs")
print(r["summary"])
for msg in r["failures"]:
    print(msg)
```

### Probe a synthetic case inline

```python
r = run_inline_syntax_test(
    '# SYNTAX TEST "Packages/Python/Python.sublime-syntax"\n'
    'x = 1\n'
    '# ^ source.python\n',
    "syntax_test_probe",
)
print(r["state"], r["summary"])
```

### Compare a syntect baseline failure against ST

The three-primitive workflow for "is this a syntect bug or a harness-
semantics divergence?". Each step answers a distinct question:

```python
# 1. What scope does ST actually report at the failing position?
#    Compare requested_syntax vs resolved_syntax to detect silent
#    fallback (e.g. ST loaded a built-in version of a syntax that the
#    test was authored against).
r = scope_at_test("/path/to/Packages/Git Formats/tests/syntax_test_git_config", 71, 28)
print(r["scope"], "via", r["resolved_syntax"])
assert r["resolved_syntax"] == r["requested_syntax"], r

# 2. Did syntect and ST even agree on which row/col to sample?
#    (past-EOL overflow is a common hidden source of divergence)
r = resolve_position(
    "/path/to/Packages/Git Formats/tests/syntax_test_git_config", 71, 29,
    syntax_path="Packages/Git Formats/Git Config.sublime-syntax",
)
print("overflow:", r["overflow"], "clamped:", r["clamped"], "actual:", r["actual"])
print("resolved:", r["resolved_syntax"])

# 3. What does ST's own assertion runner say about this file?
#    If ST cannot complete the run, run_syntax_tests raises and the
#    snippet dies — the caller sees the cause in the top-level `error`.
r = run_syntax_tests("/path/to/Packages/Git Formats/tests/syntax_test_git_config")
if r["state"] == "passed":
    print("ST passes all assertions → syntect harness diverges from ST")
else:
    print("ST fails these too → test data itself has the issue:")
    for msg in r["failures"]:
        print(msg)
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

### Mutate a buffer from a snippet

`view.run_command(...)` requires ST's main thread. Wrap it in
`run_on_main` — direct calls from the worker thread silently no-op.

```python
v = sublime.active_window().new_file()
run_on_main(lambda: v.run_command("append", {"characters": "hello"}))
print(v.size())  # 5
v.set_scratch(True)
v.close()
```

## Gotchas

- Hard timeout per call is 60 s.
- The snippet runs on a dedicated daemon thread (not ST's async
  worker and not the main UI thread). Most of the ST API is
  thread-safe, but a few mutating operations (`TextCommand` edit
  tokens) require the main thread. Use `run_on_main(callable)` —
  it owns the `set_timeout` schedule, the completion signal, and
  the timeout error path. Direct calls to `view.run_command(...)`
  from the worker thread silently no-op.
- File paths must be absolute for `scope_at` / `scope_at_test` /
  `resolve_position` / `run_syntax_tests` / `open_view`.
  `find_resources` uses ST's `Packages/...` virtual paths, and
  `run_syntax_tests` accepts that form too.
- `run_syntax_tests` uses the private `sublime_api.run_syntax_test`
  (synchronous, structured). The path must resolve under
  `sublime.packages_path()` (directly or via a symlink in that
  directory); paths outside the Packages tree raise.
  `sublime_api.run_syntax_test` is **private and undocumented**; if
  ST removes it, `run_syntax_tests` raises rather than silently
  degrading.

## Companion skill

A Claude Code skill with workflow recipes for this tool is bundled at
`skills/sublime-mcp/SKILL.md` in the sublime-mcp repo.
"""


HELPERS_SOURCE = r'''
import os as _os
import re as _re
import time as _time

try:
    import sublime_api as _sublime_api
except ImportError:
    _sublime_api = None


_SYNTAX_TEST_HEADER = _re.compile(r'SYNTAX TEST\s+"([^"]+)"')


def open_view(path, timeout=5.0):
    window = sublime.active_window()
    if window is None or len(sublime.windows()) == 0:
        raise RuntimeError(
            "open_view: Sublime Text has no open window. The plugin host is "
            "running but headless. Launch ST with a window (e.g. "
            "`open -a 'Sublime Text'` on macOS) and retry. See "
            "skills/sublime-mcp/install.md for platform-specific options."
        )
    view = window.open_file(path)
    deadline = _time.time() + timeout
    while view.is_loading() and _time.time() < deadline:
        _time.sleep(0.02)
    if view.is_loading():
        raise TimeoutError("open_view: still loading after %ss: %s" % (timeout, path))
    # `is_loading()` tracks file load, not tokenisation. On cold ST the
    # initial tokeniser pass lags file-load; `scope_name(0)` returns ""
    # until it completes. Poll briefly for the first scope to appear so
    # callers that sample scopes immediately after open don't race.
    # Fall through without raising — a still-empty scope after 1 s is
    # tolerable; callers who need stricter guarantees can re-read.
    tokenise_deadline = _time.time() + 1.0
    while _time.time() < tokenise_deadline:
        if view.scope_name(0):
            break
        _time.sleep(0.02)
    window.focus_view(view)
    return view


def scope_at(path, row, col):
    view = open_view(path)
    point = view.text_point(row, col)
    # `view.syntax()` returns None when ST didn't actually load a syntax —
    # the honest signal. `view.settings().get("syntax")` echoes any
    # `assign_syntax` argument verbatim, even bogus ones, so it can't tell
    # silent-fallback-to-plain apart from a genuine plain-text resolution.
    syntax = view.syntax()
    return {
        "scope": view.scope_name(point).rstrip(),
        "resolved_syntax": syntax.path if syntax is not None else None,
    }


def assign_syntax_and_wait(view, resource_path, timeout=2.0):
    # Invariant: `view` came from `open_view`, which refuses to return
    # views from a headless ST (no window) — so `view.size() > 0` need
    # not be re-asserted here. A direct caller bypassing `open_view` on
    # a zero-size view will time out on stage 1 below.
    # ST exposes no public tokenisation-complete signal. Stage 1 waits for
    # view.settings()["syntax"] to reflect the requested path (usually one
    # tick, but guards against a typo landing silently). Stage 2 is a
    # best-effort poll for tokenisation to touch point 0 — it's a smarter
    # sleep, not a correctness guarantee. Callers with large files should
    # re-read scope_name after use rather than trust this helper.
    # Fallback for views whose initial scope is empty (mid-load); in
    # practice callers come through open_view and this branch doesn't fire,
    # but it prevents stage 2 from exiting on the first populated scope
    # regardless of syntax.
    pre_scope = view.scope_name(0) or "text.plain "
    view.assign_syntax(resource_path)
    stage1_deadline = _time.time() + timeout
    while _time.time() < stage1_deadline:
        if view.settings().get("syntax") == resource_path:
            break
        _time.sleep(0.02)
    else:
        raise TimeoutError(
            "assign_syntax_and_wait: syntax setting not applied in %ss" % timeout
        )
    stage2_deadline = _time.time() + 0.2
    while _time.time() < stage2_deadline:
        if view.scope_name(0) != pre_scope:
            return
        _time.sleep(0.02)
    # Fall through — caller gets whatever scope is current.


def _parse_syntax_test_header(view):
    # First line of every ST syntax-test file carries
    #   <comment> SYNTAX TEST "Packages/.../Some.sublime-syntax"
    # where <comment> varies (#, //, <!--, ;, --, etc). Grab the first
    # quoted substring after the marker.
    line_region = view.line(0)
    first_line = view.substr(line_region)
    m = _SYNTAX_TEST_HEADER.search(first_line)
    if not m:
        raise ValueError(
            "no SYNTAX TEST header on line 0: %r" % first_line[:120]
        )
    return m.group(1)


def scope_at_test(path, row, col):
    view = open_view(path)
    resource_path = _parse_syntax_test_header(view)
    assign_syntax_and_wait(view, resource_path)
    point = view.text_point(row, col)
    syntax = view.syntax()
    return {
        "scope": view.scope_name(point).rstrip(),
        "requested_syntax": resource_path,
        "resolved_syntax": syntax.path if syntax is not None else None,
    }


def resolve_position(path, row, col, syntax_path=None):
    view = open_view(path)
    if syntax_path is not None:
        assign_syntax_and_wait(view, syntax_path)
    point = view.text_point(row, col)
    real_row, real_col = view.rowcol(point)
    size = view.size()
    clamped = point == size
    # `>` rather than `!=`: text_point is monotonically non-decreasing so
    # behaviourally equivalent today, but the stronger invariant defends
    # against future inputs that resolve to a *smaller* row (negative
    # rows, CRLF edge cases) — those would be bugs, not overflows.
    overflow = real_row > row and not clamped
    syntax = view.syntax()
    return {
        "point": point,
        "requested": [row, col],
        "actual": [real_row, real_col],
        "scope": view.scope_name(point).rstrip(),
        "overflow": overflow,
        "clamped": clamped,
        "requested_syntax": syntax_path,
        "resolved_syntax": syntax.path if syntax is not None else None,
    }


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


def run_on_main(callable_, timeout=2.0):
    # Snippets exec on a worker thread. ST's TextCommand edit tokens
    # (and a handful of other mutating operations) silently no-op when
    # called off the main thread — view.run_command(...) returns
    # cleanly, view.size() reports zero. Schedule on the main thread
    # via set_timeout, signal completion through threading.Event, then
    # propagate the return value or re-raise the exception on the
    # worker thread so traceback capture in _exec_on_worker sees it.
    import threading as _threading
    done = _threading.Event()
    box = {}
    def runner():
        try:
            box["result"] = callable_()
        except BaseException as exc:
            box["exc"] = exc
        finally:
            done.set()
    sublime.set_timeout(runner, 0)
    if not done.wait(timeout):
        raise TimeoutError(
            "run_on_main: callable did not complete within %ss" % timeout
        )
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


def _to_resource_path(path):
    # sublime_api.run_syntax_test only accepts resource paths of the form
    # "Packages/...". Three cases:
    # 1. Already in resource form: passthrough.
    # 2. Filesystem path under sublime.packages_path() directly (or via
    #    a symlink-name path like ~/.../Packages/Markdown/foo.md):
    #    strip the prefix. abspath preserves the symlink-name path so
    #    relpath against packages_root produces a clean result.
    # 3. Filesystem path under a realpath target reached by a symlink in
    #    packages_root: walk symlinks, realpath each, reverse-map. ST
    #    indexes resources by the symlink name, so the URI must use the
    #    symlink name even when the input is the realpath target. realpath
    #    both sides so platform symlink chains (e.g. /tmp -> /private/tmp
    #    on macOS) don't cause a false miss.
    # Returns None if the path isn't under the Packages tree (directly or
    # via symlink); caller decides whether to fall back.
    if path.startswith("Packages/") or path.startswith("Packages\\"):
        return path
    packages_root = sublime.packages_path()
    abs_path = _os.path.abspath(path)
    try:
        rel = _os.path.relpath(abs_path, packages_root)
    except ValueError:
        rel = None
    if rel is not None and not rel.startswith("..") and not _os.path.isabs(rel):
        return "Packages/" + rel.replace(_os.sep, "/")
    # Don't cache the listing: developers commonly add/remove symlinks in
    # packages_path() while iterating on a package, and a stale cache
    # would silently return wrong URIs (or None for a newly-added
    # symlink). One listdir on a small directory is cheap; correctness
    # wins.
    try:
        entries = _os.listdir(packages_root)
    except OSError:
        return None
    abs_path_real = _os.path.realpath(abs_path)
    for name in entries:
        entry_path = _os.path.join(packages_root, name)
        if not _os.path.islink(entry_path):
            continue
        try:
            target_real = _os.path.realpath(entry_path)
        except OSError:
            continue
        if (abs_path_real == target_real
                or abs_path_real.startswith(target_real + _os.sep)):
            try:
                rel_under_target = _os.path.relpath(abs_path_real, target_real)
            except ValueError:
                continue
            if rel_under_target == ".":
                return "Packages/" + name
            return ("Packages/" + name + "/"
                    + rel_under_target.replace(_os.sep, "/"))
    return None


def _run_syntax_tests_via_api(path):
    # sublime_api.run_syntax_test returns (total_assertions, [error_msgs]).
    # Each error_msg is a multi-line string:
    #   Packages/User/foo/syntax_test_mix.py:4:3
    #   error: scope does not match
    #   4 | y = 2
    #   5 | # ^ keyword.control.flow
    #     |   ^ this location did not match
    #   actual:
    #     |   ^ source.python keyword.operator.assignment.python
    # For files newly created on disk, ST's resource index may be a few
    # hundred ms behind the filesystem — the API then returns a single
    # "unable to read file" message even though the file exists. Poll
    # sublime.find_resources until the resource is visible, then retry.
    resource = _to_resource_path(path)
    if resource is None:
        return None
    total, messages = _sublime_api.run_syntax_test(resource)
    if _is_unable_to_read(messages):
        _wait_for_resource(resource)
        total, messages = _sublime_api.run_syntax_test(resource)
    if _is_unable_to_read(messages):
        # Under Packages but still not indexed: surface the miss as an
        # exception so it propagates up as the top-level MCP `error`.
        raise RuntimeError(
            "Sublime Text has not indexed the resource at %s: %s"
            % (resource, "\n".join(messages))
        )
    failures = list(messages)
    if failures:
        summary = "FAILED: %d of %d assertions failed" % (len(failures), total)
        state = "failed"
    else:
        summary = "%d assertions passed" % total
        state = "passed"
    return {
        "state": state,
        "summary": summary,
        "output": "\n".join(failures) if failures else summary,
        "failures": failures,
    }


def _is_unable_to_read(messages):
    return any("unable to read file" in m for m in messages)


def _wait_for_resource(resource_path, timeout=3.0):
    # ST's resource index can lag behind the filesystem by seconds, not
    # milliseconds, on cold-disk or post-write indexing — one observed
    # cold-register latency was 15 s for a freshly-written
    # .sublime-syntax (#6). Default budget bumped from 1.0 s to 3.0 s
    # to cover the realistic upper bound of common cases without
    # waiting forever on genuine misses.
    # Past two-thirds of the budget without resolution, fire one
    # `refresh_folder_list` to nudge ST's indexer; idempotent and
    # bounded by the same total budget. Dispatched through set_timeout
    # because run_command is application-level and the safe default
    # off the worker thread is main-thread scheduling.
    basename = resource_path.rsplit("/", 1)[-1]
    start = _time.time()
    deadline = start + timeout
    refresh_threshold = start + (timeout * 2.0 / 3.0)
    refreshed = False
    while _time.time() < deadline:
        if resource_path in sublime.find_resources(basename):
            return True
        if not refreshed and _time.time() >= refresh_threshold:
            sublime.set_timeout(
                lambda: sublime.run_command("refresh_folder_list"), 0
            )
            refreshed = True
        _time.sleep(0.02)
    return False


_TEMP_DIR_PREFIX = "__sublime_mcp_temp_"
_TEMP_DIR_SUFFIX = "__"
_TEMP_DIR_MAX_AGE_SECONDS = 60.0


def _sweep_stale_temp_dirs():
    # Cross-call defensive cleanup for run_inline_syntax_test:
    # SIGKILL / OS panic bypass the within-call try/finally. List
    # Packages/User entries matching the nonce scheme; remove ones
    # older than the max-age threshold so a concurrent in-flight call
    # isn't clobbered.
    user_dir = _os.path.join(sublime.packages_path(), "User")
    try:
        entries = _os.listdir(user_dir)
    except OSError:
        return
    now = _time.time()
    for name in entries:
        if not (name.startswith(_TEMP_DIR_PREFIX) and name.endswith(_TEMP_DIR_SUFFIX)):
            continue
        full = _os.path.join(user_dir, name)
        try:
            age = now - _os.path.getmtime(full)
        except OSError:
            continue
        if age < _TEMP_DIR_MAX_AGE_SECONDS:
            continue
        import shutil as _shutil
        _shutil.rmtree(full, ignore_errors=True)


def _new_temp_dir():
    import uuid as _uuid
    nonce = _uuid.uuid4().hex[:12]
    name = "%s%s%s" % (_TEMP_DIR_PREFIX, nonce, _TEMP_DIR_SUFFIX)
    full = _os.path.join(sublime.packages_path(), "User", name)
    _os.makedirs(full)
    return name, full


def run_inline_syntax_test(content, name):
    # Write `content` to Packages/User/<nonce>/<name>, run ST's syntax-
    # test runner against it, return the same {state, summary, output,
    # failures} shape as run_syntax_tests. The header inside `content`
    # (e.g. `# SYNTAX TEST "Packages/Python/Python.sublime-syntax"`)
    # determines which syntax is exercised; the syntax must already be
    # reachable to ST (bundled or via temp_packages_link).
    # Two-layer cleanup: within-call try/finally for Python-visible
    # failures, _sweep_stale_temp_dirs at the head for SIGKILL paths.
    # Returns state="inconclusive" rather than raising on indexing
    # miss — fresh-resource probes hit indexing latency commonly
    # enough that a looser contract beats burning the snippet.
    if _sublime_api is None or not hasattr(_sublime_api, "run_syntax_test"):
        raise RuntimeError(
            "sublime_api.run_syntax_test is unavailable on this Sublime "
            "Text build; run_inline_syntax_test has no working fallback"
        )
    _sweep_stale_temp_dirs()
    nonce_name, temp_dir = _new_temp_dir()
    try:
        file_path = _os.path.join(temp_dir, name)
        with open(file_path, "w") as f:
            f.write(content)
        resource = "Packages/User/%s/%s" % (nonce_name, name)
        if not _wait_for_resource(resource):
            return {
                "state": "inconclusive",
                "summary": "Sublime Text has not indexed the resource at %s" % resource,
                "output": "",
                "failures": [],
            }
        total, messages = _sublime_api.run_syntax_test(resource)
        if _is_unable_to_read(messages):
            _wait_for_resource(resource)
            total, messages = _sublime_api.run_syntax_test(resource)
        if _is_unable_to_read(messages):
            return {
                "state": "inconclusive",
                "summary": "Sublime Text could not read %s after indexing wait" % resource,
                "output": "",
                "failures": [],
            }
        failures = list(messages)
        if failures:
            return {
                "state": "failed",
                "summary": "FAILED: %d of %d assertions failed" % (len(failures), total),
                "output": "\n".join(failures),
                "failures": failures,
            }
        return {
            "state": "passed",
            "summary": "%d assertions passed" % total,
            "output": "%d assertions passed" % total,
            "failures": [],
        }
    finally:
        import shutil as _shutil
        _shutil.rmtree(temp_dir, ignore_errors=True)


def run_syntax_tests(path):
    # No fallback for paths outside Packages/: programmatic dispatch of
    # the "Syntax Tests" build system surfaces an empty output panel
    # and never fires the runner, so a fallback would be dead code.
    # Callers must symlink outside-Packages files in (see
    # _to_resource_path).
    if _sublime_api is None or not hasattr(_sublime_api, "run_syntax_test"):
        raise RuntimeError(
            "sublime_api.run_syntax_test is unavailable on this Sublime "
            "Text build; run_syntax_tests has no working fallback"
        )
    result = _run_syntax_tests_via_api(path)
    if result is None:
        raise RuntimeError(
            "Path %r is not under sublime.packages_path(); symlink the "
            "containing directory into Packages/ (see SKILL.md section 4) "
            "and pass the symlinked path or its Packages/... URI" % path
        )
    return result
'''


# Compile HELPERS_SOURCE once at module load and reuse the code object per
# call. SyntaxError in the helpers will fail plugin_loaded loudly (and the
# MCP server won't bind) rather than reaching the user via per-call tracebacks
# — earlier loud failure is the right trade-off during development.
_HELPERS_CODE = compile(HELPERS_SOURCE, "<sublime-mcp-helpers>", "exec")


def _exec_on_worker(code):
    """Run `code` on a dedicated daemon thread and collect output.

    Returns a dict with keys `output`, `result`, `error`, `st_version`,
    `st_channel`. `error is None` means the snippet ran to completion.
    `st_version` / `st_channel` echo the running ST build so callers can
    detect when they're driving (e.g.) ST stable while their question
    was authored against ST DEV — read per-call so an in-place ST
    upgrade is reflected without restart.
    """
    done = threading.Event()
    # ST guarantees `sublime.version()` is a stringified integer; cast
    # at the boundary so callers don't reparse.
    result = {
        "output": "",
        "result": None,
        "error": None,
        "st_version": int(sublime.version()),
        "st_channel": sublime.channel(),
    }

    def run():
        buf_out = io.StringIO()
        # Override `print` in the snippet's namespace rather than
        # redirecting sys.stdout/sys.stderr globally. The global redirect
        # isn't thread-safe: under concurrent HTTP handlers, stderr from
        # an unrelated request's error handler would bleed into this
        # snippet's captured output.
        def _print(*args, **kwargs):
            kwargs.setdefault("file", buf_out)
            _builtins.print(*args, **kwargs)
        namespace = {
            "__name__": "sublime_mcp_exec",
            "__builtins__": __builtins__,
            "sublime": sublime,
            "sublime_plugin": sublime_plugin,
            "print": _print,
        }
        try:
            exec(_HELPERS_CODE, namespace)
        except Exception:
            result["error"] = "helper init failed:\n" + traceback.format_exc()
            done.set()
            return
        try:
            exec(compile(code, "<sublime-mcp-snippet>", "exec"), namespace)
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

    # Dispatch on a dedicated daemon thread rather than
    # sublime.set_timeout_async. The async worker's scheduling is coupled
    # to ST's main event loop — when the caller (notably a sync
    # DeferrableTestCase under UnitTesting) holds the main thread, the
    # async callback never runs and every request returns "exec timed out
    # after 60s". Most of the ST API is thread-safe from any thread, so a
    # plain threading.Thread works; snippets that need the main UI thread
    # can still use sublime.set_timeout(lambda: ..., 0) inside the snippet
    # and poll. `daemon=True` so a runaway snippet doesn't block unload.
    worker = threading.Thread(target=run, name="sublime-mcp-exec", daemon=True)
    worker.start()
    if not done.wait(EXEC_TIMEOUT_SECONDS):
        # Reuse the pre-populated `st_version` / `st_channel` from the
        # initial dict so the envelope shape stays uniform across the
        # success and timeout paths.
        result["error"] = "exec timed out after %ss" % EXEC_TIMEOUT_SECONDS
        return result
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
                "isError": outcome["error"] is not None,
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

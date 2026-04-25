"""Tests for the helper surface exposed inside `exec_sublime_python`.

Each test fires a `tools/call` against the live MCP endpoint and asserts
on the outcome. Running through MCP (rather than importing helpers
directly) exercises the JSON-RPC transport and the outer response-shape
contract in addition to the helper logic itself.

The MCP round-trip is done on a background thread and the test method
is a generator that yields while the request is in flight. Without
this, sync `urlopen` holds ST's main thread; ST APIs used inside the
snippet (`window.open_file`, the filesystem watcher behind
`sublime.find_resources`) never progress, and every MCP call returns
`"exec timed out after 60s"`.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import sublime
from unittesting import DeferrableTestCase


MCP_URL = "http://127.0.0.1:47823/mcp"
SERVER_STARTUP_POLL_ATTEMPTS = 30
SERVER_STARTUP_POLL_INTERVAL_S = 0.1
CALL_YIELD_INTERVAL_MS = 50

HEADER = '# SYNTAX TEST "Packages/Python/Python.sublime-syntax"\n'
HEADER_PIPE_MD = '| SYNTAX TEST "Packages/Markdown/Markdown.sublime-syntax"\n'
HEADER_HTML_COMMENT = '<!-- SYNTAX TEST "Packages/HTML/HTML.sublime-syntax" -->\n'


def _post(payload, timeout=65):
    # Default matches EXEC_TIMEOUT_SECONDS (60) in the plugin with a 5 s
    # network margin. Used for `initialize` in the startup poll; test
    # methods go through `_call_tool_yielding` instead so the main
    # thread is released while ST processes events.
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_tool(code, request_id=1, timeout=65):
    return _post({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "exec_sublime_python",
            "arguments": {"code": code},
        },
    }, timeout=timeout)


def _call_tool_yielding(code, request_id=1, timeout=65):
    """Generator: spawns the MCP call on a daemon thread and yields
    `CALL_YIELD_INTERVAL_MS` until it returns. DeferrableTestCase pauses
    the generator during each yield, letting ST's main thread run the
    snippet's ST API calls to completion.
    """
    holder = {}
    def worker():
        try:
            holder["resp"] = _call_tool(code, request_id=request_id, timeout=timeout)
        except BaseException as exc:
            holder["exc"] = exc
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.time() + timeout + 5
    while t.is_alive() and time.time() < deadline:
        yield CALL_YIELD_INTERVAL_MS
    if "exc" in holder:
        raise holder["exc"]
    if "resp" not in holder:
        raise TimeoutError("MCP call did not complete in %ss" % (timeout + 5))
    return holder["resp"]


def _outcome(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def _wait_for_server():
    # Synchronous poll so setUpClass works — can't yield from there.
    last_exc = None
    for _ in range(SERVER_STARTUP_POLL_ATTEMPTS):
        try:
            _post({
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "helpers-test", "version": "0"},
                },
            }, timeout=2)
            return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(SERVER_STARTUP_POLL_INTERVAL_S)
    raise AssertionError("MCP server not reachable: %r" % (last_exc,))


def _probe_api_path_available():
    # Tests run inside ST's plugin host, so we can import sublime_api
    # directly instead of going through MCP. Avoids the cost of a round-
    # trip — and avoids the chicken-and-egg where the probe itself would
    # need `yield` support that setUpClass can't provide.
    try:
        import sublime_api
        return hasattr(sublime_api, "run_syntax_test")
    except ImportError:
        return False


class HelperTestBase(DeferrableTestCase):
    FIXTURE_SUBDIR = "sublime_mcp_test_fixtures"
    api_path_available = False

    @classmethod
    def setUpClass(cls):
        _wait_for_server()
        cls.api_path_available = _probe_api_path_available()
        if not cls.api_path_available:
            print(
                "[sublime-mcp tests] sublime_api.run_syntax_test missing — "
                "API-path tests skipped"
            )
        cls.fixture_dir = os.path.join(
            sublime.packages_path(), "User", cls.FIXTURE_SUBDIR
        )
        if os.path.isdir(cls.fixture_dir):
            shutil.rmtree(cls.fixture_dir)
        os.makedirs(cls.fixture_dir)

    @classmethod
    def tearDownClass(cls):
        # Close any views that still reference fixture_dir before deleting
        # the dir itself. Tests avoid sharing fixture paths via
        # `_testMethodName` prefixing, so setUp makes zero MCP calls; the
        # trade-off is that ST accumulates dangling views unless we clean
        # up here. tearDownClass runs on ST's main thread in the plugin
        # host, so we can call the ST API directly — no MCP round-trip,
        # which would deadlock the same way setUp used to.
        try:
            for w in sublime.windows():
                for v in list(w.views()):
                    fn = v.file_name() or ""
                    if fn.startswith(cls.fixture_dir):
                        v.set_scratch(True)
                        v.close()
        except Exception as exc:
            print("[sublime-mcp tests] view cleanup failed: %r" % (exc,))
        if os.path.isdir(cls.fixture_dir):
            shutil.rmtree(cls.fixture_dir)

    def _write_fixture(self, name, content):
        # Prefix by test method name so no two tests in the same class
        # share a fixture path — setUp stays synchronous (no MCP calls)
        # and tests are isolated from each other's view state.
        path = os.path.join(self.fixture_dir, "%s__%s" % (self._testMethodName, name))
        with open(path, "w") as f:
            f.write(content)
        return path


class TestResponseShape(HelperTestBase):
    """Outer MCP response contract: no outer `ok` field; `error is null`
    on success; `isError` tracks the presence of `error`. Distinct from
    the inner `ok` some helpers return (e.g. `run_syntax_tests(...)["ok"]`)."""

    def test_success_has_no_outer_ok(self):
        resp = yield from _call_tool_yielding("print('hi')")
        outcome = _outcome(resp)
        self.assertNotIn("ok", outcome)
        self.assertIsNone(outcome["error"])
        self.assertEqual(outcome["output"], "hi\n")
        self.assertFalse(resp["result"]["isError"])

    def test_snippet_exception_sets_error_and_isError(self):
        resp = yield from _call_tool_yielding("raise RuntimeError('boom')")
        outcome = _outcome(resp)
        self.assertNotIn("ok", outcome)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("RuntimeError: boom", outcome["error"])
        self.assertTrue(resp["result"]["isError"])


class TestScopeAtExtensionless(HelperTestBase):
    """The landmine the feedback flagged: `scope_at` on extension-less files
    silently returns `text.plain`. `scope_at_test` fixes it by parsing the
    header."""

    def setUp(self):
        self.fixture_path = self._write_fixture(
            "syntax_test_probe", HEADER + "x = 1\n"
        )

    def test_scope_at_returns_text_plain_on_extensionless(self):
        resp = yield from _call_tool_yielding(
            "print(scope_at(%r, 1, 0))" % self.fixture_path
        )
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "text.plain")

    def test_scope_at_test_parses_header_and_returns_real_scope(self):
        resp = yield from _call_tool_yielding(
            "print(scope_at_test(%r, 1, 0))" % self.fixture_path
        )
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertIn("source.python", outcome["output"])


class TestScopeAtTestNoHeader(HelperTestBase):
    """`scope_at_test` must fail loudly when the header is missing, not
    silently fall through to Plain Text."""

    def setUp(self):
        self.fixture_path = self._write_fixture(
            "plain_no_header.py", "x = 1\n"
        )

    def test_no_header_raises_value_error(self):
        resp = yield from _call_tool_yielding(
            "scope_at_test(%r, 0, 0)" % self.fixture_path
        )
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("no SYNTAX TEST header", outcome["error"])


class TestResolvePosition(HelperTestBase):
    """`resolve_position` surfaces `text_point`'s past-EOL overflow and
    past-EOF clamping as distinct, mutually-exclusive fields."""

    def setUp(self):
        # Row 0 is the header. Row 1 = "x = 1" (5 chars). Row 2 = "y = 2".
        self.fixture_path = self._write_fixture(
            "syntax_test_resolve", HEADER + "x = 1\ny = 2\n"
        )

    def _resolve(self, row, col):
        # Generator: callers must `yield from self._resolve(...)`.
        code = (
            "import json\n"
            "_ = resolve_position(%r, %d, %d, "
            "syntax_path='Packages/Python/Python.sublime-syntax')\n"
            "print(json.dumps(_))\n"
            % (self.fixture_path, row, col)
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        return json.loads(outcome["output"])

    def test_in_bounds_no_flags_set(self):
        r = yield from self._resolve(1, 2)
        self.assertFalse(r["overflow"])
        self.assertFalse(r["clamped"])
        self.assertEqual(r["actual"], [1, 2])
        self.assertEqual(r["requested"], [1, 2])

    def test_past_eol_overflows_into_next_row(self):
        # Row 1 is 5 chars + \n; col 10 lands in a later row.
        r = yield from self._resolve(1, 10)
        self.assertTrue(r["overflow"])
        self.assertFalse(r["clamped"])
        self.assertGreater(r["actual"][0], 1)

    def test_past_eof_clamps_to_view_size(self):
        r = yield from self._resolve(99999, 99999)
        self.assertTrue(r["clamped"])
        self.assertFalse(r["overflow"])
        # View size in chars — point must be exactly view.size().
        size_resp = yield from _call_tool_yielding(
            "v = open_view(%r)\nprint(v.size())" % self.fixture_path
        )
        size = int(_outcome(size_resp)["output"].strip())
        self.assertEqual(r["point"], size)


class TestRunSyntaxTestsApiPath(HelperTestBase):
    """Primary path via `sublime_api.run_syntax_test` — synchronous,
    structured, no panel scraping."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.api_path_available:
            raise unittest.SkipTest(
                "sublime_api.run_syntax_test unavailable on this platform"
            )

    def test_all_assertions_pass(self):
        path = self._write_fixture(
            "syntax_test_pass",
            HEADER + "x = 1\n# ^ source.python\n",
        )
        r = yield from self._run(path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["failures"], [])
        self.assertIn("assertions passed", r["summary"])

    def test_mixed_pass_fail_returns_structured_failures(self):
        # One passing assertion, one failing.
        path = self._write_fixture(
            "syntax_test_mix",
            HEADER
            + "x = 1\n# ^ source.python\n"
            + "y = 2\n# ^ keyword.control.flow\n",
        )
        r = yield from self._run(path)
        self.assertFalse(r["ok"], r)
        self.assertEqual(len(r["failures"]), 1, r)
        msg = r["failures"][0]
        self.assertIn("syntax_test_mix", msg)
        self.assertIn("scope does not match", msg)

    def test_consistent_across_repeated_runs(self):
        # Verifies determinism across repeated calls on the primary path.
        path = self._write_fixture(
            "syntax_test_repeat",
            HEADER
            + "x = 1\n# ^ keyword.control.flow\n",
        )
        first = yield from self._run(path)
        self.assertFalse(first["ok"], first)
        self.assertEqual(len(first["failures"]), 1, first)
        for _ in range(4):
            r = yield from self._run(path)
            self.assertEqual(r["ok"], first["ok"])
            self.assertEqual(len(r["failures"]), len(first["failures"]))

    def _run(self, path):
        # Generator: callers must `yield from self._run(...)`.
        code = (
            "import json\n"
            "_ = run_syntax_tests(%r)\n"
            "print(json.dumps(_))\n" % path
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        return json.loads(outcome["output"])


class TestRunSyntaxTestsFallback(HelperTestBase):
    """Fallback path kicks in for files outside `sublime.packages_path()`.
    The critical contract: never return a silent empty result."""

    def test_outside_packages_self_describes_empty(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write("just text\n")
            code = (
                "import json\n"
                "_ = run_syntax_tests(%r, timeout=3.0)\n"
                "print(json.dumps(_))\n" % path
            )
            resp = yield from _call_tool_yielding(code)
            outcome = _outcome(resp)
            self.assertIsNone(outcome["error"], outcome.get("error"))
            r = json.loads(outcome["output"])
            self.assertFalse(r["ok"])
            self.assertEqual(r["failures"], [])
            # Self-describing marker rather than "".
            self.assertTrue(
                r["summary"].startswith("<") and r["summary"].endswith(">"),
                "expected self-describing marker, got %r" % r["summary"],
            )
        finally:
            os.unlink(path)


class TestToResourcePath(HelperTestBase):
    """`_to_resource_path` maps abs paths under Packages/ to resource form
    and rejects paths outside."""

    def test_abs_path_under_packages(self):
        abs_path = os.path.join(
            sublime.packages_path(), "User", "foo", "bar.py"
        )
        code = "print(_to_resource_path(%r))" % abs_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(
            outcome["output"].strip(), "Packages/User/foo/bar.py"
        )

    def test_already_resource_form_returned_as_is(self):
        code = "print(_to_resource_path('Packages/User/foo/bar.py'))"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(
            outcome["output"].strip(), "Packages/User/foo/bar.py"
        )

    def test_outside_packages_returns_none(self):
        code = "print(_to_resource_path('/tmp/not-a-package/foo.py'))"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "None")


class TestScopeAtTestPipeHeader(HelperTestBase):
    """`scope_at_test` must accept markdown's `|` comment-token header.
    The fixture is extensionless so ST can't infer the syntax — a parser
    failure on `|` would surface as a `text.plain` scope, not Markdown.
    """

    def setUp(self):
        self.fixture_path = self._write_fixture(
            "syntax_test_pipe_md", HEADER_PIPE_MD + "# heading\n"
        )

    def test_pipe_comment_header_parses(self):
        resp = yield from _call_tool_yielding(
            "print(scope_at_test(%r, 1, 0))" % self.fixture_path
        )
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertIn("text.html.markdown", outcome["output"])


class TestScopeAtTestHtmlComment(HelperTestBase):
    """`scope_at_test` must accept HTML's `<!--` comment-token header.
    Extensionless fixture for the same reason as the pipe-header test —
    without that, ST would infer the syntax from the extension and a
    broken header parser would not surface.
    """

    def setUp(self):
        self.fixture_path = self._write_fixture(
            "syntax_test_html_comment", HEADER_HTML_COMMENT + "<p>x</p>\n"
        )

    def test_html_comment_header_parses(self):
        resp = yield from _call_tool_yielding(
            "print(scope_at_test(%r, 1, 0))" % self.fixture_path
        )
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        # `text.html.basic` rather than `text.html`: the looser prefix
        # would also match `text.html.markdown`, so a regression that
        # mis-applied Markdown syntax to the HTML fixture would slip
        # through.
        self.assertIn("text.html.basic", outcome["output"])


class TestHeadlessGuard(HelperTestBase):
    """`open_view` raises `RuntimeError` when ST has no open window.

    Both clauses of the guard (`active_window() is None` AND
    `len(sublime.windows()) == 0`) are exercised in CI by patching
    `sublime` module attributes via `unittest.mock.patch.object`. Each
    test patches BOTH names, even though only one clause is under test:
    the other patch forces the non-tested clause's predicate False so a
    regression in the tested clause cannot be silently rescued by the
    other (e.g., a CI environment that happens to have no real window).

    Stability invariant: these tests rely on `open_view` reading
    `sublime.active_window` / `sublime.windows` *through the module at
    call time* (sublime_mcp.py:268-269). A future refactor that captures
    references at module import would silently neuter both tests because
    the patch would land on names the helper no longer reads.

    Blast-radius caveat: while a `patch.object` context is active, the
    override is process-global within the ST plugin host and visible to
    autosave timers, indexers, concurrent MCP daemon-thread requests,
    and ST's UI thread. Keep the patch window as narrow as possible —
    wrap *only* the `open_view` call, not surrounding setUp / fixture
    writes / assertions. A multi-helper patch under one context is a
    code smell; split into smaller scoped patches instead.
    """

    GUARD_SNIPPET = (
        "import unittest.mock\n"
        "with unittest.mock.patch.object(sublime, 'windows', new={windows_mock}), \\\n"
        "     unittest.mock.patch.object(sublime, 'active_window', new={aw_mock}):\n"
        "    open_view('/tmp/sublime_mcp_headless_test')\n"
    )

    def test_open_view_raises_on_zero_windows(self):
        # Load-bearing case: non-None active_window, len(windows()) == 0.
        # `aw_mock=lambda: object()` forces the `is None` clause False so
        # this test can't pass for the wrong reason in a CI environment
        # where the harness happens to have no real window.
        code = self.GUARD_SNIPPET.format(
            windows_mock="lambda: []",
            aw_mock="lambda: object()",
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("RuntimeError", outcome["error"])
        self.assertIn("no open window", outcome["error"])
        self.assertIn("install.md", outcome["error"])

    def test_open_view_raises_on_none_active_window(self):
        # Defensive case: active_window() is None. `windows_mock=lambda:
        # [object()]` forces the `windows() == 0` clause False so a
        # regression in the `is None` branch cannot be rescued by the
        # other clause.
        code = self.GUARD_SNIPPET.format(
            windows_mock="lambda: [object()]",
            aw_mock="lambda: None",
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("RuntimeError", outcome["error"])
        self.assertIn("no open window", outcome["error"])
        self.assertIn("install.md", outcome["error"])

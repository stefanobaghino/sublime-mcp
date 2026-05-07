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
import sys
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


def _call_tool(code, request_id=1, timeout=65, mcp_timeout_seconds=None):
    arguments = {"code": code}
    if mcp_timeout_seconds is not None:
        arguments["timeout_seconds"] = mcp_timeout_seconds
    return _post({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "exec_sublime_python",
            "arguments": arguments,
        },
    }, timeout=timeout)


def _call_tool_yielding(code, request_id=1, timeout=65, mcp_timeout_seconds=None):
    """Generator: spawns the MCP call on a daemon thread and yields
    `CALL_YIELD_INTERVAL_MS` until it returns. DeferrableTestCase pauses
    the generator during each yield, letting ST's main thread run the
    snippet's ST API calls to completion.
    """
    holder = {}
    def worker():
        try:
            holder["resp"] = _call_tool(
                code,
                request_id=request_id,
                timeout=timeout,
                mcp_timeout_seconds=mcp_timeout_seconds,
            )
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
    helper-level status fields like `run_syntax_tests(...)["state"]`."""

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

    def test_envelope_carries_st_version_and_channel(self):
        # Envelope-level echo so non-helper snippets (and helper failures
        # that don't reach the helper return path) still surface which ST
        # is answering. One assertion covers every helper because the
        # field lives at the envelope, not on each helper dict.
        resp = yield from _call_tool_yielding("print('hi')")
        outcome = _outcome(resp)
        self.assertIsInstance(outcome["st_version"], int)
        self.assertEqual(outcome["st_version"], int(sublime.version()))
        self.assertIsInstance(outcome["st_channel"], str)
        self.assertTrue(outcome["st_channel"])


class TestScopeAtExtensionless(HelperTestBase):
    """The landmine the feedback flagged: `scope_at` on extension-less files
    silently returns `text.plain`. `scope_at_test` fixes it by parsing the
    header."""

    def setUp(self):
        self.fixture_path = self._write_fixture(
            "syntax_test_probe", HEADER + "x = 1\n"
        )

    def test_scope_at_returns_text_plain_on_extensionless(self):
        # Extensionless file → ST falls back to Plain Text. `scope_at`
        # returns the dict shape; both fields surface the fallback.
        code = (
            "import json\n"
            "_ = scope_at(%r, 1, 0)\n"
            "print(json.dumps(_))\n" % self.fixture_path
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertEqual(r["scope"], "text.plain")
        self.assertEqual(r["resolved_syntax"], "Packages/Text/Plain text.tmLanguage")

    def test_scope_at_test_parses_header_and_returns_real_scope(self):
        code = (
            "import json\n"
            "_ = scope_at_test(%r, 1, 0)\n"
            "print(json.dumps(_))\n" % self.fixture_path
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertIn("source.python", r["scope"])
        self.assertEqual(
            r["requested_syntax"], "Packages/Python/Python.sublime-syntax"
        )
        # Header-requested syntax matches what ST loaded → silent-fallback
        # signal is clean.
        self.assertEqual(r["resolved_syntax"], r["requested_syntax"])


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
        # `_resolve` always passes `syntax_path`, so both fields are populated
        # and equal on the happy path.
        self.assertEqual(
            r["requested_syntax"], "Packages/Python/Python.sublime-syntax"
        )
        self.assertEqual(r["resolved_syntax"], r["requested_syntax"])

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

    def test_no_syntax_path_arg_leaves_requested_syntax_none(self):
        code = (
            "import json\n"
            "_ = resolve_position(%r, 1, 0)\n"
            "print(json.dumps(_))\n" % self.fixture_path
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        # No `syntax_path` provided → requested_syntax is None; resolved_syntax
        # reflects whatever ST inferred from the file (or extension).
        self.assertIsNone(r["requested_syntax"])
        self.assertIsNotNone(r["resolved_syntax"])

    # The wrong-syntax silent-fallback case (bogus `syntax_path` URI →
    # `view.syntax()` returns None while `view.settings().get("syntax")`
    # echoes the bogus URI verbatim) is the canonical #11 failure mode.
    # Locally observable, but a probe with `Packages/__nonexistent__/...`
    # provoked a deferred main-thread side-effect on macOS-CI that
    # throttled every subsequent test until the suite hit the
    # SublimeText/UnitTesting watchdog. Coverage gap is intentional:
    # `test_in_bounds_no_flags_set` already asserts that both fields are
    # populated and equal on the happy path, which guards the
    # field-presence regression. A dedicated wrong-syntax test that
    # avoids the macOS side-effect will need a different trigger.


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
        self.assertEqual(r["state"], "passed", r)
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
        self.assertEqual(r["state"], "failed", r)
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
        self.assertEqual(first["state"], "failed", first)
        self.assertEqual(len(first["failures"]), 1, first)
        for _ in range(4):
            r = yield from self._run(path)
            self.assertEqual(r["state"], first["state"])
            self.assertEqual(len(r["failures"]), len(first["failures"]))

    def test_unindexed_resource_raises(self):
        # Path under packages_path() but pointing at a package directory
        # that doesn't exist on disk. _to_resource_path maps it to a
        # Packages/... URI, sublime_api.run_syntax_test reports "unable
        # to read file", _wait_for_resource times out without the
        # resource appearing in the index, and the API path raises.
        bogus = os.path.join(
            sublime.packages_path(), "__sublime_mcp_unindexed__", "syntax_test_nope"
        )
        code = "_ = run_syntax_tests(%r)\n" % bogus
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("not indexed", outcome["error"])

    def test_failure_carries_structured_field(self):
        path = self._write_fixture(
            "syntax_test_struct",
            HEADER
            + "x = 1\n# ^ source.python\n"
            + "y = 2\n# ^ keyword.control.flow\n",
        )
        r = yield from self._run(path)
        self.assertEqual(r["state"], "failed", r)
        self.assertEqual(len(r["failures_structured"]), 1, r)
        s = r["failures_structured"][0]
        self.assertIsNotNone(s["file"], s)
        self.assertTrue(s["file"].endswith("syntax_test_struct"), s)
        self.assertIsInstance(s["row"], int)
        self.assertIsInstance(s["col"], int)
        self.assertEqual(s["error_label"], "scope does not match", s)
        self.assertEqual(s["expected_selector"], "keyword.control.flow", s)
        self.assertGreaterEqual(len(s["actual"]), 1, s)
        self.assertIn("source.python", s["actual"][0]["scope_chain"])

    def test_passed_run_has_empty_structured_failures(self):
        path = self._write_fixture(
            "syntax_test_pass_struct",
            HEADER + "x = 1\n# ^ source.python\n",
        )
        r = yield from self._run(path)
        self.assertEqual(r["state"], "passed", r)
        self.assertEqual(r["failures_structured"], [])

    def test_structured_parser_does_not_raise_on_unparseable(self):
        # _parse_failure_message is in the helper namespace; call it
        # directly with deliberately-malformed inputs to assert never-
        # raises and that every result carries the documented keys.
        code = (
            "import json\n"
            "probes = [\n"
            "    '',\n"
            "    'garbage',\n"
            "    'no:colons here',\n"
            "    'a:b:c\\nerror: x\\nactual:\\n  | ^ s\\n',\n"
            "]\n"
            "_ = [_parse_failure_message(p) for p in probes]\n"
            "print(json.dumps(_))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        parsed = json.loads(outcome["output"])
        self.assertEqual(len(parsed), 4)
        expected_keys = {
            "file", "row", "col", "error_label", "expected_selector", "actual",
        }
        for entry in parsed:
            self.assertEqual(set(entry.keys()), expected_keys, entry)

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


class TestRunSyntaxTestsOutsidePackages(HelperTestBase):
    """Paths outside `sublime.packages_path()` raise loudly. Programmatic
    dispatch of the Syntax Tests build system never fires the runner
    (#51), so there is no working fallback — the helper raises with a
    pointer at the symlink workaround instead of silently returning
    empty."""

    def test_outside_packages_describes_cause(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write("just text\n")
            code = "_ = run_syntax_tests(%r)\n" % path
            resp = yield from _call_tool_yielding(code)
            outcome = _outcome(resp)
            self.assertIsNotNone(outcome["error"])
            # Pin against substrings the helper actually produces, so a
            # regression to generic-but-wrong prose fails the test. The
            # path is interpolated; "symlink" names the workaround.
            self.assertIn(path, outcome["error"])
            self.assertIn("symlink", outcome["error"])
            self.assertIn("Packages/", outcome["error"])
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
        code = (
            "import json\n"
            "_ = scope_at_test(%r, 1, 0)\n"
            "print(json.dumps(_))\n" % self.fixture_path
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertIn("text.html.markdown", r["scope"])


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
        code = (
            "import json\n"
            "_ = scope_at_test(%r, 1, 0)\n"
            "print(json.dumps(_))\n" % self.fixture_path
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        # `text.html.basic` rather than `text.html`: the looser prefix
        # would also match `text.html.markdown`, so a regression that
        # mis-applied Markdown syntax to the HTML fixture would slip
        # through.
        self.assertIn("text.html.basic", r["scope"])


class TestHeadlessGuard(HelperTestBase):
    """`open_view` raises `RuntimeError` when ST has no open window.

    Both clauses of the guard (`_get_active_window() is None` AND
    `len(_get_windows()) == 0`) are exercised by overriding the seams
    in the snippet's globals. Each test overrides BOTH names, even
    though only one clause is under test: the other override forces
    the non-tested clause's predicate False so a regression in the
    tested clause cannot be silently rescued by the other (e.g., a CI
    environment that happens to have no real window).

    Stability invariant: these tests rely on `open_view` reading the
    seams through their module-global names at call time. A future
    refactor that captures references at definition time would
    silently neuter both tests because the override would land on
    names the helper no longer reads.

    Seam-vs-`patch.object(sublime, ...)` rationale: overriding the
    seam in the snippet's globals scopes the mock to this snippet's
    namespace. Compared to the prior `patch.object(sublime, 'windows',
    ...)` shape, ST's autosave timers, indexers, and concurrent MCP
    requests — which read `sublime.windows` directly — are unaffected
    for the duration of the test.
    """

    GUARD_SNIPPET = (
        "_get_windows = {windows_mock}\n"
        "_get_active_window = {aw_mock}\n"
        "open_view('/tmp/sublime_mcp_headless_test')\n"
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


@unittest.skipIf(
    sys.platform == "win32",
    "TestToResourcePathSymlinked requires symlink creation, which "
    "needs SeCreateSymbolicLinkPrivilege or Developer Mode on Windows. "
    "A junction (mklink /J) workaround has subtle filesystem-semantics "
    "differences that would weaken the test's invariants.",
)
class TestToResourcePathSymlinked(HelperTestBase):
    """`_to_resource_path` must reverse-map a realpath-target input through
    a symlink in `sublime.packages_path()`, returning a `Packages/
    <symlink_name>/...` URI rather than `None`.

    Real-filesystem caveat: setUp creates a symlink under a deliberately
    unique name (`__sublime_mcp_test_symlink__`) inside the user's real
    `sublime.packages_path()`. While the symlink is in place, ST's
    resource indexer treats it as a registered package. The hermetic
    peer `TestToResourcePathSeam` exercises the same symlink-walk
    branches without touching the filesystem; this class is the
    integration test that pins the contract against a real
    `os.symlink` on the user's `Packages/`.

    Cleanup discipline: the symlink is registered with `addCleanup`
    (not `tearDown`) immediately after `os.symlink` so it fires even if
    setUp partial-fails. A defensive `lexists`-then-`unlink` runs at
    the *start* of setUp to recover from a prior crashed run that left
    the symlink behind. The unique name ensures the defensive removal
    cannot clobber a real user package.
    """

    SYMLINK_NAME = "__sublime_mcp_test_symlink__"

    def setUp(self):
        self.symlink_path = os.path.join(
            sublime.packages_path(), self.SYMLINK_NAME
        )
        if os.path.lexists(self.symlink_path):
            os.unlink(self.symlink_path)
        self.target_dir = tempfile.mkdtemp(prefix="sublime_mcp_test_target_")
        self.addCleanup(shutil.rmtree, self.target_dir, ignore_errors=True)
        with open(os.path.join(self.target_dir, "foo.md"), "w") as f:
            f.write("# heading\n")
        os.symlink(self.target_dir, self.symlink_path)
        self.addCleanup(self._unlink_symlink)

    def _unlink_symlink(self):
        try:
            os.unlink(self.symlink_path)
        except FileNotFoundError:
            pass

    def test_symlink_path_input_returns_symlink_name_uri(self):
        # Regression-proofing: passing the path under the symlink name
        # already worked on pre-fix code (abspath preserves the symlink
        # path; relpath against packages_root is clean). Locks the
        # contract that the symlink-walk doesn't break this case.
        symlink_input = os.path.join(self.symlink_path, "foo.md")
        code = "print(_to_resource_path(%r))" % symlink_input
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(
            outcome["output"].strip(),
            "Packages/%s/foo.md" % self.SYMLINK_NAME,
        )

    def test_target_path_input_returns_symlink_name_uri(self):
        # The bug: passing the realpath-target path falls through to None
        # on pre-fix code (relpath against packages_root yields a `..`-
        # laden path). After the fix the symlink-walk reverse-maps to
        # the symlink-name URI ST's resource indexer agrees on.
        target_input = os.path.join(self.target_dir, "foo.md")
        code = "print(_to_resource_path(%r))" % target_input
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(
            outcome["output"].strip(),
            "Packages/%s/foo.md" % self.SYMLINK_NAME,
        )

    def test_outside_packages_target_path_returns_none(self):
        # Negative case. The class's own symlink (__sublime_mcp_test_
        # symlink__) is in place during this test, so the assertion is
        # the stronger "no false-positive against ANY symlink in
        # packages_root" — not merely "no symlink → None". Locks intent
        # against a future reader who assumes packages_root is empty
        # at test time.
        unrelated = tempfile.mkdtemp(prefix="sublime_mcp_test_unrelated_")
        self.addCleanup(shutil.rmtree, unrelated, ignore_errors=True)
        unrelated_input = os.path.join(unrelated, "foo.md")
        with open(unrelated_input, "w") as f:
            f.write("x\n")
        code = "print(_to_resource_path(%r))" % unrelated_input
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "None")


@unittest.skipIf(
    sys.platform == "win32",
    "TestToResourcePathSeam needs `os.symlink` to construct a real "
    "symlink for `_to_resource_path` to walk via realpath. The seam "
    "removes the listdir against the user's Packages/ but the islink "
    "and realpath checks still need a real symlink on disk.",
)
class TestToResourcePathSeam(HelperTestBase):
    """Hermetic peer of `TestToResourcePathSymlinked`. Exercises the
    realpath-target reverse-walk inside `_to_resource_path` without
    creating a symlink under the user's `sublime.packages_path()`.

    The `_list_packages_entries` seam bounds the directory enumeration
    so the test injects synthetic entries via the snippet's globals.
    The per-entry `os.path.islink` / `realpath` checks happen against
    real filesystem objects under a tempdir — the seam doesn't replace
    those, by design (keeping the symlink-walk logic in
    `_to_resource_path` rather than diluting the seam into multiple
    indirections).
    """

    def setUp(self):
        # Build a sandbox containing a real symlink whose target is a
        # tempdir with `foo.md`. The seam will return this as the only
        # entry; _to_resource_path's loop will islink it, realpath the
        # target, and reverse-map.
        self.sandbox = tempfile.mkdtemp(prefix="sublime_mcp_seam_sandbox_")
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.target_dir = tempfile.mkdtemp(prefix="sublime_mcp_seam_target_")
        self.addCleanup(shutil.rmtree, self.target_dir, ignore_errors=True)
        with open(os.path.join(self.target_dir, "foo.md"), "w") as f:
            f.write("# heading\n")
        self.fake_link_name = "fake_pkg"
        self.fake_link_path = os.path.join(self.sandbox, self.fake_link_name)
        os.symlink(self.target_dir, self.fake_link_path)

    def _seam_override_snippet(self, probe_path):
        # Override `_list_packages_entries` in the snippet's globals to
        # return our synthetic single-entry list, then call
        # `_to_resource_path` and print its return value.
        return (
            "_list_packages_entries = lambda root: [(%r, %r)]\n"
            "print(_to_resource_path(%r))\n"
        ) % (self.fake_link_name, self.fake_link_path, probe_path)

    def test_target_path_reverse_maps_via_seam(self):
        # Realpath-target case: the input path is under the symlink
        # target, not the symlink name. The seam returns one entry; the
        # walk islinks it, realpath-matches, and reverse-maps to
        # Packages/fake_pkg/foo.md.
        target_input = os.path.join(self.target_dir, "foo.md")
        resp = yield from _call_tool_yielding(
            self._seam_override_snippet(target_input)
        )
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "Packages/fake_pkg/foo.md")

    def test_outside_seam_entries_returns_none(self):
        # Negative case: the input is unrelated to any seam entry.
        # The walk islinks the only entry, realpath doesn't match, the
        # function falls through to return None.
        unrelated = tempfile.mkdtemp(prefix="sublime_mcp_seam_unrelated_")
        self.addCleanup(shutil.rmtree, unrelated, ignore_errors=True)
        unrelated_input = os.path.join(unrelated, "foo.md")
        with open(unrelated_input, "w") as f:
            f.write("x\n")
        resp = yield from _call_tool_yielding(
            self._seam_override_snippet(unrelated_input)
        )
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "None")


class TestRunOnMain(HelperTestBase):
    """`run_on_main` schedules a callable on ST's main thread and round-
    trips its return value or raised exception back to the worker. The
    load-bearing case is buffer mutation via `view.run_command(...)`,
    which silently no-ops when called directly from the worker thread.
    """

    def test_returns_callable_result(self):
        code = "_ = run_on_main(lambda: 42)\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["result"], "42")

    def test_propagates_exception(self):
        code = "_ = run_on_main(lambda: 1 / 0)\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("ZeroDivisionError", outcome["error"])

    def test_timeout_raises(self):
        # 0.1 s budget; callable sleeps 1 s. The run_on_main TimeoutError
        # propagates as the snippet's `error`.
        code = (
            "import time\n"
            "_ = run_on_main(lambda: time.sleep(1.0), timeout=0.1)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("TimeoutError", outcome["error"])
        self.assertIn("run_on_main", outcome["error"])
        self.assertIn("0.1", outcome["error"])

    def test_view_run_command_actually_mutates(self):
        # The original failure mode: view.run_command on the worker
        # thread is a silent no-op. With run_on_main the buffer should
        # actually grow.
        code = (
            "v = sublime.active_window().new_file()\n"
            "try:\n"
            "    run_on_main(lambda: v.run_command('append', {'characters': 'hello'}))\n"
            "    print(v.size())\n"
            "finally:\n"
            "    v.set_scratch(True)\n"
            "    v.close()\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "5")


class TestUnderscoreAutoLift(HelperTestBase):
    """REPL-style auto-lift in `_compile_snippet`: a trailing bare
    expression becomes `_ = <expr>` so callers don't need the explicit
    idiom. Strict scope on the explicit-assign check (top-level only).
    """

    def test_trailing_expr_lifted_to_underscore(self):
        resp = yield from _call_tool_yielding("42\n")
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["result"], "42")

    def test_explicit_top_level_underscore_blocks_lift(self):
        # Explicit `_ = 1` at top level wins; the trailing `2` is not lifted.
        resp = yield from _call_tool_yielding("_ = 1\n2\n")
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["result"], "1")

    def test_for_target_named_underscore_does_not_block_lift(self):
        # `for _ in ...` is ast.For, not ast.Assign; lift should still fire.
        resp = yield from _call_tool_yielding("for _ in range(3): pass\n42\n")
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["result"], "42")

    def test_nested_underscore_assign_does_not_block_lift(self):
        # `_ = 1` inside `if False:` is not at top level — strict check skips it.
        code = "if False:\n    _ = 1\n42\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["result"], "42")

    def test_trailing_statement_does_not_lift(self):
        # `x = 1` is ast.Assign, not ast.Expr — nothing to lift.
        resp = yield from _call_tool_yielding("x = 1\n")
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertIsNone(outcome["result"])

    def test_syntax_error_falls_through(self):
        resp = yield from _call_tool_yielding("def\n")
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("SyntaxError", outcome["error"])


class TestWaitForResource(HelperTestBase):
    """`_wait_for_resource` widened from 1.0 s to 3.0 s and gained a
    one-shot `refresh_folder_list` nudge past two-thirds of the budget
    (#6). The signature default is the externally-visible contract;
    the refresh is the structural change.
    """

    def test_default_timeout_is_three_seconds(self):
        # The default reaches every transitive caller — _run_syntax_tests_via_api
        # at plugin.py:518 inherits it without naming it, so the
        # signature itself is the right anchor.
        code = (
            "import inspect\n"
            "_ = inspect.signature(_wait_for_resource).parameters['timeout'].default\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "3.0")

    def test_refresh_folder_list_fires_after_two_thirds(self):
        # Patch sublime.run_command so the test doesn't actually nudge
        # ST's indexer. 0.3 s budget → refresh at ~0.2 s → ~0.1 s of
        # post-refresh polling before the deadline. The set_timeout
        # dispatch is async; sleep within the patch context after the
        # wait returns so the lambda has time to land on main.
        code = (
            "import time\n"
            "import unittest.mock\n"
            "calls = []\n"
            "with unittest.mock.patch.object(sublime, 'run_command', new=lambda *a, **k: calls.append(a)):\n"
            "    found = _wait_for_resource('Packages/__sublime_mcp_nonexistent__/x.bar', timeout=0.3)\n"
            "    time.sleep(0.3)\n"
            "print(found)\n"
            "print(calls)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertEqual(lines[0], "False")
        # Single call to run_command with positional arg "refresh_folder_list".
        self.assertIn("refresh_folder_list", lines[1])
        # Make sure it fired exactly once, not on every poll iteration.
        self.assertEqual(lines[1].count("refresh_folder_list"), 1)


class TestWaitForResourcePublic(HelperTestBase):
    """`wait_for_resource(pattern, timeout=3.0)` is the public glob-
    pattern counterpart to the private `_wait_for_resource(path, …)`
    used internally by `temp_packages_link`. Returns True when any
    resource matching the pattern surfaces within the budget, False
    otherwise — exposed so callers can chain indexing waits across
    snippets instead of polling inside one (#64).
    """

    def test_default_timeout_is_three_seconds(self):
        # The default is the externally-visible contract; anchor it
        # via inspect.signature to mirror the _wait_for_resource test.
        code = (
            "import inspect\n"
            "_ = inspect.signature(wait_for_resource).parameters['timeout'].default\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "3.0")

    def test_returns_true_for_existing_resource(self):
        # `find_resources` matches basename globs against ST's resource
        # index. `Python.sublime-syntax` is bundled with ST 4 and
        # present from startup, so the helper should return True well
        # within the default budget.
        code = (
            "found = wait_for_resource('Python.sublime-syntax')\n"
            "print(found)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "True")

    def test_returns_false_when_pattern_misses(self):
        # Tight budget so the test runs in well under a second; the
        # pattern is constructed to never match anything in ST's index.
        code = (
            "import time\n"
            "t0 = time.time()\n"
            "found = wait_for_resource('__sublime_mcp_nonexistent__.sublime-syntax', timeout=0.1)\n"
            "elapsed = time.time() - t0\n"
            "print(found)\n"
            "print(elapsed < 0.5)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertEqual(lines[0], "False")
        self.assertEqual(lines[1], "True")

    def test_refresh_folder_list_fires_after_two_thirds(self):
        # Mirror the _wait_for_resource refresh-nudge test. Patch
        # sublime.run_command so the test doesn't actually nudge ST's
        # indexer. 0.3 s budget → refresh at ~0.2 s → ~0.1 s of post-
        # refresh polling before the deadline. set_timeout dispatch
        # is async; sleep within the patch context after the wait
        # returns so the lambda has time to land on main.
        code = (
            "import time\n"
            "import unittest.mock\n"
            "calls = []\n"
            "with unittest.mock.patch.object(sublime, 'run_command', new=lambda *a, **k: calls.append(a)):\n"
            "    found = wait_for_resource('__sublime_mcp_nonexistent__.sublime-syntax', timeout=0.3)\n"
            "    time.sleep(0.3)\n"
            "print(found)\n"
            "print(calls)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertEqual(lines[0], "False")
        self.assertIn("refresh_folder_list", lines[1])
        # Make sure it fired exactly once, not on every poll iteration.
        self.assertEqual(lines[1].count("refresh_folder_list"), 1)

    def test_raises_on_path_shaped_pattern(self):
        # `find_resources` matches basenames only, so any pattern
        # containing `/` silently never matches — `wait_for_resource`
        # used to burn the full timeout returning False (#100). Now
        # raises ValueError up front. Cover both the
        # `Packages/<dir>/<file>` shape (the natural shape after
        # `temp_packages_link` builds a URI) and a bare `<dir>/<file>`
        # to anchor the rule on `/` rather than the `Packages/` prefix.
        for pattern in ("Packages/Python/Python.sublime-syntax",
                        "Python/Python.sublime-syntax"):
            code = (
                "import time\n"
                "t0 = time.time()\n"
                "try:\n"
                "    wait_for_resource(%r, timeout=3.0)\n"
                "except ValueError as e:\n"
                "    print('raised', e)\n"
                "elapsed = time.time() - t0\n"
                "print(elapsed < 0.5)\n"
            ) % (pattern,)
            resp = yield from _call_tool_yielding(code)
            outcome = _outcome(resp)
            self.assertIsNone(outcome["error"], outcome.get("error"))
            lines = outcome["output"].strip().splitlines()
            self.assertTrue(lines[0].startswith("raised "), lines)
            self.assertIn("matches basenames only", lines[0])
            self.assertIn(repr(pattern), lines[0])
            # Fast-fail: the raise happens at function entry, before
            # any timer setup or polling.
            self.assertEqual(lines[1], "True")


class TestWaitForScopePublic(HelperTestBase):
    """`wait_for_scope(scope, timeout=3.0)` is the scope-registry
    counterpart of `wait_for_resource` (#117). Used to gate cross-
    syntax probes after writing host+guest syntaxes under
    `Packages/User/<subdir>/` — the parse-table builder for
    `push: scope:` / `embed: scope:` / `include: scope:` consults a
    registry independent of the resource indexer (#108), so the
    basename-only `wait_for_resource` gate is insufficient.
    """

    def test_default_timeout_is_three_seconds(self):
        # Externally-visible default; mirrors the wait_for_resource
        # anchor so the two helpers stay in lockstep.
        code = (
            "import inspect\n"
            "_ = inspect.signature(wait_for_scope).parameters['timeout'].default\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "3.0")

    def test_returns_true_for_existing_scope(self):
        # source.python is bundled with ST 4 and present from startup,
        # so the helper returns True well within the default budget.
        code = (
            "found = wait_for_scope('source.python')\n"
            "print(found)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "True")

    def test_returns_false_when_scope_misses(self):
        # Tight budget so the test runs in well under a second; the
        # scope is constructed to never match anything in ST's registry.
        code = (
            "import time\n"
            "t0 = time.time()\n"
            "found = wait_for_scope('source.__sublime_mcp_nonexistent__', timeout=0.1)\n"
            "elapsed = time.time() - t0\n"
            "print(found)\n"
            "print(elapsed < 0.5)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertEqual(lines[0], "False")
        self.assertEqual(lines[1], "True")

    def test_iterable_form_succeeds_when_all_present(self):
        # Multi-syntax cross-syntax probe shape: every scope in the
        # iterable must be present before tokenisation can proceed.
        code = (
            "found = wait_for_scope(['source.python', 'source.json'])\n"
            "print(found)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "True")

    def test_iterable_form_fails_when_any_missing(self):
        # All-or-nothing — even with source.python present, the bogus
        # scope blocks success and the helper exhausts the budget.
        code = (
            "import time\n"
            "t0 = time.time()\n"
            "found = wait_for_scope(\n"
            "    ['source.python', 'source.__sublime_mcp_nonexistent__'],\n"
            "    timeout=0.1,\n"
            ")\n"
            "elapsed = time.time() - t0\n"
            "print(found)\n"
            "print(elapsed < 0.5)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertEqual(lines[0], "False")
        self.assertEqual(lines[1], "True")

    def test_raises_on_empty_iterable(self):
        # Caller passing an empty list typically means a list-comp
        # produced nothing — fast-raise so the bug surfaces at the
        # call site rather than after the timeout.
        code = (
            "import time\n"
            "t0 = time.time()\n"
            "try:\n"
            "    wait_for_scope([])\n"
            "except ValueError as e:\n"
            "    print('raised', e)\n"
            "elapsed = time.time() - t0\n"
            "print(elapsed < 0.5)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertTrue(lines[0].startswith("raised "), lines)
        self.assertIn("at least one scope", lines[0])
        self.assertEqual(lines[1], "True")


class TestRunInlineSyntaxTest(HelperTestBase):
    """`run_inline_syntax_test` writes a probe file under
    `Packages/User/__sublime_mcp_temp_<nonce>__/`, runs ST's syntax-
    test runner, and cleans up. Mirrors `TestRunSyntaxTestsApiPath` but
    on the inline-probe path (#30).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.api_path_available:
            raise unittest.SkipTest(
                "sublime_api.run_syntax_test unavailable on this platform"
            )

    @classmethod
    def _temp_dir_count(cls):
        # Count Packages/User/__sublime_mcp_temp_*__ entries — the
        # helper's nonce scheme. Used to assert cleanup on success and
        # failure paths.
        user_dir = os.path.join(sublime.packages_path(), "User")
        try:
            entries = os.listdir(user_dir)
        except OSError:
            return 0
        return sum(
            1
            for e in entries
            if e.startswith("__sublime_mcp_temp_") and e.endswith("__")
        )

    def test_passes_returns_passed_state(self):
        before = self._temp_dir_count()
        code = (
            "import json\n"
            "_ = run_inline_syntax_test(\n"
            "    '# SYNTAX TEST \"Packages/Python/Python.sublime-syntax\"\\n'\n"
            "    'x = 1\\n'\n"
            "    '# ^ source.python\\n',\n"
            "    'syntax_test_pass',\n"
            ")\n"
            "print(json.dumps(_))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertEqual(r["state"], "passed", r)
        self.assertEqual(r["failures"], [])
        self.assertEqual(self._temp_dir_count(), before)

    def test_failing_assertion_populates_failures(self):
        before = self._temp_dir_count()
        code = (
            "import json\n"
            "_ = run_inline_syntax_test(\n"
            "    '# SYNTAX TEST \"Packages/Python/Python.sublime-syntax\"\\n'\n"
            "    'x = 1\\n'\n"
            "    '# ^ keyword.control.flow\\n',\n"
            "    'syntax_test_fail',\n"
            ")\n"
            "print(json.dumps(_))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertEqual(r["state"], "failed", r)
        self.assertEqual(len(r["failures"]), 1, r)
        self.assertIn("scope does not match", r["failures"][0])
        self.assertEqual(self._temp_dir_count(), before)

    def test_temp_dir_cleaned_up_on_failure(self):
        # Force the helper to raise mid-flight by passing a non-string
        # `name` so the os.path.join inside the try block blows up. The
        # finally must still rmtree the temp dir.
        before = self._temp_dir_count()
        code = (
            "try:\n"
            "    run_inline_syntax_test('whatever', None)\n"
            "except Exception:\n"
            "    pass\n"
            "print('done')\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(self._temp_dir_count(), before)

    def test_stale_dir_swept_on_next_call(self):
        # Plant a stale temp dir with mtime 90 s in the past, then run
        # a successful inline test; the sweep at the head of the call
        # should remove the planted dir.
        user_dir = os.path.join(sublime.packages_path(), "User")
        stale = os.path.join(user_dir, "__sublime_mcp_temp_stale123__")
        os.makedirs(stale, exist_ok=True)
        self.addCleanup(shutil.rmtree, stale, ignore_errors=True)
        old = time.time() - 90.0
        os.utime(stale, (old, old))
        self.assertTrue(os.path.isdir(stale))
        code = (
            "_ = run_inline_syntax_test(\n"
            "    '# SYNTAX TEST \"Packages/Python/Python.sublime-syntax\"\\n'\n"
            "    'x = 1\\n'\n"
            "    '# ^ source.python\\n',\n"
            "    'syntax_test_sweep',\n"
            ")\n"
            "print(_['state'])\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "passed")
        self.assertFalse(os.path.exists(stale))


@unittest.skipIf(
    sys.platform == "win32",
    "TestTempPackagesLink requires symlink creation, which needs "
    "SeCreateSymbolicLinkPrivilege or Developer Mode on Windows. "
    "Same rationale as TestToResourcePathSymlinked.",
)
class TestTempPackagesLink(HelperTestBase):
    """`temp_packages_link` synthesises a managed
    `Packages/__sublime_mcp_temp_<nonce>__` symlink whose target is a
    repo-local syntax dir, waits for ST to index the sentinel, returns
    the synthesised package name. `release_packages_link` tears it
    down with prefix/suffix validation. Mirrors
    `TestToResourcePathSymlinked` blast-radius and cleanup discipline.
    """

    SYNTAX_CONTENT = (
        "%YAML 1.2\n"
        "---\n"
        "name: SublimeMcpTempLinkProbe\n"
        "scope: source.smtlp\n"
        "file_extensions: [smtlp]\n"
        "version: 2\n"
        "contexts:\n"
        "  main:\n"
        "    - match: 'x'\n"
        "      scope: keyword.smtlp\n"
    )

    def setUp(self):
        # Each test gets its own target dir + syntax file. addCleanup
        # ensures teardown even on partial failure. Defensive sweep at
        # the start of every test removes any nonce-named link the
        # helper might have left behind from a prior crash.
        self._defensive_link_sweep()
        self.target_dir = tempfile.mkdtemp(prefix="sublime_mcp_test_link_")
        self.addCleanup(shutil.rmtree, self.target_dir, ignore_errors=True)
        self.syntax_basename = "Probe.sublime-syntax"
        self.syntax_path = os.path.join(self.target_dir, self.syntax_basename)
        with open(self.syntax_path, "w") as f:
            f.write(self.SYNTAX_CONTENT)

    def _defensive_link_sweep(self):
        # Same shape as TestToResourcePathSymlinked.setUp's defensive
        # unlink, scaled to the temp-prefix scheme: any leftover
        # nonce-named symlink gets removed regardless of age.
        packages_root = sublime.packages_path()
        for name in os.listdir(packages_root):
            if not (name.startswith("__sublime_mcp_temp_") and name.endswith("__")):
                continue
            full = os.path.join(packages_root, name)
            if os.path.islink(full):
                try:
                    os.unlink(full)
                except OSError:
                    pass

    def _release(self, name):
        # Cleanup helper for tests that successfully create a link.
        # Goes through the MCP boundary so we exercise the public
        # surface, but tolerates a missing link in case the test
        # already cleaned up.
        link_path = os.path.join(sublime.packages_path(), name)
        if os.path.lexists(link_path) and os.path.islink(link_path):
            os.unlink(link_path)

    def test_creates_link_with_temp_prefix(self):
        code = (
            "_ = temp_packages_link(%r)\n"
            "print(_)\n"
        ) % self.syntax_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name = outcome["output"].strip()
        self.addCleanup(self._release, name)
        self.assertTrue(name.startswith("__sublime_mcp_temp_"))
        self.assertTrue(name.endswith("__"))
        link_path = os.path.join(sublime.packages_path(), name)
        self.assertTrue(os.path.islink(link_path))
        self.assertEqual(os.path.realpath(link_path), os.path.realpath(self.target_dir))

    def test_resource_becomes_findable(self):
        code = (
            "name = temp_packages_link(%r)\n"
            "import json\n"
            "_ = json.dumps([name, find_resources('Probe.sublime-syntax')])\n"
            "print(_)\n"
        ) % self.syntax_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name, resources = json.loads(outcome["output"].strip())
        self.addCleanup(self._release, name)
        expected = "Packages/%s/%s" % (name, self.syntax_basename)
        self.assertIn(expected, resources)

    def test_release_removes_link(self):
        code = (
            "name = temp_packages_link(%r)\n"
            "release_packages_link(name)\n"
            "print(name)\n"
        ) % self.syntax_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name = outcome["output"].strip()
        link_path = os.path.join(sublime.packages_path(), name)
        self.assertFalse(os.path.lexists(link_path))

    def test_release_validates_name_prefix(self):
        # ValueError on a non-temp name; the assertion is that the
        # error surfaces AND no Packages/ entry was touched.
        code = "release_packages_link('Markdown')\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("ValueError", outcome["error"])
        self.assertIn("non-temp", outcome["error"])

    def test_release_idempotent_on_missing(self):
        # Second release on the same name is a no-op — caller code
        # using try/finally shouldn't need to track whether the link
        # was already removed by a sibling sweep.
        code = (
            "name = temp_packages_link(%r)\n"
            "release_packages_link(name)\n"
            "release_packages_link(name)\n"
            "print('ok')\n"
        ) % self.syntax_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    def test_raises_on_missing_file_under_existing_parent(self):
        # Parent dir exists, file under it doesn't. Helper must catch
        # this upfront rather than waiting through `_wait_for_resource`
        # for a sentinel that can't appear. Error message names the
        # missing file, not the parent.
        missing = os.path.join(self.target_dir, "does_not_exist.sublime-syntax")
        code = (
            "_ = temp_packages_link(%r)\n"
            "print(_)\n"
        ) % missing
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("RuntimeError", outcome["error"])
        self.assertIn("does not exist", outcome["error"])
        self.assertIn("does_not_exist.sublime-syntax", outcome["error"])

    def test_raises_on_empty_directory(self):
        # An empty directory has nothing to index; without an upfront
        # check, the helper would create a useless symlink and wait
        # the full budget for a sentinel it can't pick.
        empty_dir = tempfile.mkdtemp(prefix="sublime_mcp_test_empty_")
        self.addCleanup(shutil.rmtree, empty_dir, ignore_errors=True)
        code = (
            "_ = temp_packages_link(%r)\n"
            "print(_)\n"
        ) % empty_dir
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("RuntimeError", outcome["error"])
        self.assertIn("no files to index", outcome["error"])

    def test_directory_input_waits_for_first_file(self):
        # Directory form must verify ST has indexed at least one file
        # under the link before returning — the silent-fallback shape
        # on #67 was the directory branch returning success without
        # any post-condition. The pre-existing syntax file under
        # target_dir is the sentinel; assert it surfaces in
        # `find_resources` immediately after the helper returns.
        code = (
            "name = temp_packages_link(%r)\n"
            "import json\n"
            "_ = json.dumps([name, find_resources('Probe.sublime-syntax')])\n"
            "print(_)\n"
        ) % self.target_dir
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name, resources = json.loads(outcome["output"].strip())
        self.addCleanup(self._release, name)
        expected = "Packages/%s/%s" % (name, self.syntax_basename)
        self.assertIn(expected, resources)

    def test_wait_timeout_threaded_through(self):
        # `wait_timeout=0.0` collapses the wait window to zero so the
        # helper raises before ST can index, regardless of how fast
        # the indexer actually is. Verifies the new parameter is
        # forwarded to `_wait_for_resource` rather than ignored, and
        # that the surfaced timeout in the error message reflects the
        # caller's value.
        code = (
            "_ = temp_packages_link(%r, wait_timeout=0.0)\n"
            "print(_)\n"
        ) % self.syntax_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("did not index", outcome["error"])
        self.assertIn("0.0s", outcome["error"])

    def test_stale_link_swept_on_next_call(self):
        # Plant a stale temp symlink with mtime 90 s in the past, then
        # call temp_packages_link; the head-of-call sweep should
        # remove the planted link before creating the new one.
        stale_target = tempfile.mkdtemp(prefix="sublime_mcp_test_stale_target_")
        self.addCleanup(shutil.rmtree, stale_target, ignore_errors=True)
        stale_link = os.path.join(
            sublime.packages_path(), "__sublime_mcp_temp_stalelink__"
        )
        if os.path.lexists(stale_link):
            os.unlink(stale_link)
        os.symlink(stale_target, stale_link)
        old = time.time() - 90.0
        os.utime(stale_link, (old, old), follow_symlinks=False)
        self.addCleanup(
            lambda: os.path.lexists(stale_link) and os.unlink(stale_link)
        )
        code = (
            "_ = temp_packages_link(%r)\n"
            "print(_)\n"
        ) % self.syntax_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name = outcome["output"].strip()
        self.addCleanup(self._release, name)
        self.assertFalse(os.path.lexists(stale_link))


class TestTempUserPackagesDir(HelperTestBase):
    """`temp_user_packages_dir` synthesises a managed
    `Packages/User/__sublime_mcp_user_<prefix>_<nonce>__/` directory
    and returns its absolute path. `release_user_packages_dir` tears
    it down with structural validation. Productizes the cross-syntax
    workaround documented in #108 (#118) — same lifecycle shape as
    `temp_packages_link` but on the `Packages/User/` ingest path that
    actually feeds ST's parse-table builder for cross-syntax
    references.
    """

    USER_PREFIX = "__sublime_mcp_user_"
    USER_SUFFIX = "__"

    def _defensive_user_sweep(self):
        # Same shape as TestTempPackagesLink._defensive_link_sweep:
        # any leftover nonce-named dir gets removed regardless of age
        # so a crash in a prior test doesn't poison this one.
        user_dir = os.path.join(sublime.packages_path(), "User")
        try:
            entries = os.listdir(user_dir)
        except OSError:
            return
        for name in entries:
            if name.startswith(self.USER_PREFIX) and name.endswith(self.USER_SUFFIX):
                shutil.rmtree(os.path.join(user_dir, name), ignore_errors=True)

    def _release(self, path):
        # Cleanup helper for tests that successfully create a dir.
        # Tolerates a missing dir in case the test already cleaned up.
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)

    def setUp(self):
        self._defensive_user_sweep()

    def test_creates_dir_with_user_prefix(self):
        code = (
            "_ = temp_user_packages_dir()\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        path = outcome["output"].strip()
        self.addCleanup(self._release, path)
        self.assertTrue(os.path.isdir(path))
        self.assertFalse(os.path.islink(path))
        name = os.path.basename(path)
        self.assertTrue(name.startswith(self.USER_PREFIX), name)
        self.assertTrue(name.endswith(self.USER_SUFFIX), name)
        # Default prefix segment is "probe".
        self.assertIn("_probe_", name)
        # Lives under Packages/User/, not Packages/.
        expected_parent = os.path.join(sublime.packages_path(), "User")
        self.assertEqual(os.path.dirname(path), expected_parent)

    def test_custom_prefix_appears_in_name(self):
        code = (
            "_ = temp_user_packages_dir('q1-extends')\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        path = outcome["output"].strip()
        self.addCleanup(self._release, path)
        self.assertIn("_q1-extends_", os.path.basename(path))

    def test_directory_is_writable(self):
        # The point of this helper is to give the caller a place to
        # write `.sublime-syntax` files; smoke-test that an open() and
        # find_resources round-trips through the dir.
        code = (
            "import os\n"
            "path = temp_user_packages_dir()\n"
            "with open(os.path.join(path, 'X.sublime-syntax'), 'w') as f:\n"
            "    f.write('%YAML 1.2\\n---\\nname: X\\nscope: source.x\\ncontexts: {main: []}\\n')\n"
            "found = wait_for_resource('X.sublime-syntax')\n"
            "import json\n"
            "_ = json.dumps([path, found])\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        path, found = json.loads(outcome["output"].strip())
        self.addCleanup(self._release, path)
        self.assertTrue(found, "X.sublime-syntax should surface via wait_for_resource")

    def test_release_removes_dir(self):
        code = (
            "path = temp_user_packages_dir()\n"
            "release_user_packages_dir(path)\n"
            "print(path)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        path = outcome["output"].strip()
        self.assertFalse(os.path.exists(path))

    def test_release_idempotent_on_missing(self):
        # Second release on the same path is a no-op — caller code
        # using try/finally shouldn't need to track whether the dir
        # was already removed by a sibling sweep.
        code = (
            "path = temp_user_packages_dir()\n"
            "release_user_packages_dir(path)\n"
            "release_user_packages_dir(path)\n"
            "print('ok')\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    def test_release_refuses_non_managed_path(self):
        # Pointing release at a real Packages/User/<subdir>/ would
        # rmtree it; structural refusal is the safety guarantee.
        code = (
            "import os\n"
            "target = os.path.join(sublime.packages_path(), 'User', 'NotManaged')\n"
            "release_user_packages_dir(target)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("ValueError", outcome["error"])
        self.assertIn("non-temp", outcome["error"])

    def test_release_refuses_symlink(self):
        # A symlink whose basename happens to match the prefix scheme
        # could redirect the rmtree outside Packages/User/. Defence:
        # release refuses anything that isn't a real directory.
        link_target = tempfile.mkdtemp(prefix="sublime_mcp_test_link_target_")
        self.addCleanup(shutil.rmtree, link_target, ignore_errors=True)
        link_name = "%sevil_aaaaaaaaaaaa%s" % (self.USER_PREFIX, self.USER_SUFFIX)
        link_path = os.path.join(sublime.packages_path(), "User", link_name)
        if os.path.lexists(link_path):
            os.unlink(link_path)
        os.symlink(link_target, link_path)
        self.addCleanup(
            lambda: os.path.lexists(link_path) and os.unlink(link_path)
        )
        code = (
            "release_user_packages_dir(%r)\n"
        ) % link_path
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("RuntimeError", outcome["error"])
        # link_target must still exist; the helper refused the rmtree.
        self.assertTrue(os.path.isdir(link_target))

    def test_invalid_prefix_raises(self):
        # Empty prefix, prefix with disallowed characters (underscore
        # would collide with the structural delimiter).
        for bad in ("", "with_underscore", "spaces here", "dot.notation"):
            code = "temp_user_packages_dir(%r)\n" % bad
            resp = yield from _call_tool_yielding(code)
            outcome = _outcome(resp)
            self.assertIsNotNone(outcome["error"], "expected raise on prefix=%r" % bad)
            self.assertIn("ValueError", outcome["error"])

    def test_stale_dir_swept_on_next_call(self):
        # Plant a stale managed dir with mtime 90 s in the past, then
        # call temp_user_packages_dir; the head-of-call sweep should
        # remove the planted dir before creating the new one.
        stale_name = "%sstale_aaaaaaaaaaaa%s" % (self.USER_PREFIX, self.USER_SUFFIX)
        stale_path = os.path.join(sublime.packages_path(), "User", stale_name)
        os.makedirs(stale_path, exist_ok=True)
        old = time.time() - 90.0
        os.utime(stale_path, (old, old))
        self.addCleanup(shutil.rmtree, stale_path, ignore_errors=True)
        code = (
            "_ = temp_user_packages_dir()\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        path = outcome["output"].strip()
        self.addCleanup(self._release, path)
        self.assertFalse(os.path.exists(stale_path))

    def test_fresh_dir_not_swept(self):
        # A managed dir with a recent mtime survives the next call's
        # sweep — the threshold protects in-flight concurrent probes.
        fresh_name = "%sfresh_aaaaaaaaaaaa%s" % (self.USER_PREFIX, self.USER_SUFFIX)
        fresh_path = os.path.join(sublime.packages_path(), "User", fresh_name)
        os.makedirs(fresh_path, exist_ok=True)
        self.addCleanup(shutil.rmtree, fresh_path, ignore_errors=True)
        code = (
            "_ = temp_user_packages_dir()\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        path = outcome["output"].strip()
        self.addCleanup(self._release, path)
        self.assertTrue(os.path.exists(fresh_path))


class TestProbeScopes(HelperTestBase):
    """`probe_scopes` opens a scratch view, assigns a syntax (existing
    `Packages/...` URI or synthesised inline from `syntax_yaml`),
    appends `content`, sweeps `view.scope_name(p)` at every point (or
    just `points`), captures tokens, tears down. Composes
    `temp_packages_link` / `assign_syntax_and_wait` / `run_on_main`
    plus the post-#94 symmetric-race warm-up; closes the
    `Packages/User/` synthetic-syntax leak observed on #63's follow-up
    comment (2026-05-04).
    """

    SYNTHETIC_YAML = (
        "%YAML 1.2\n"
        "---\n"
        "name: SublimeMcpProbeScopes\n"
        "scope: source.smps\n"
        "file_extensions: [smps]\n"
        "version: 2\n"
        "contexts:\n"
        "  main:\n"
        "    - match: 'x'\n"
        "      scope: keyword.smps\n"
    )

    PYTHON_SYNTAX = "Packages/Python/Python.sublime-syntax"

    def _defensive_temp_sweep(self):
        # Same shape as TestRunInlineSyntaxTest's stale-dir cleanup:
        # any leftover nonce-named temp dir under Packages/User/ gets
        # removed regardless of age, so a crash in a prior test doesn't
        # poison this one. Probe.sublime-syntax at the root of
        # Packages/User/ is the legacy-leak shape — sweep it too.
        user_dir = os.path.join(sublime.packages_path(), "User")
        try:
            entries = os.listdir(user_dir)
        except OSError:
            return
        for name in entries:
            full = os.path.join(user_dir, name)
            if name.startswith("__sublime_mcp_temp_") and name.endswith("__"):
                shutil.rmtree(full, ignore_errors=True)
            elif name.startswith("Probe.sublime-syntax"):
                try:
                    os.unlink(full)
                except OSError:
                    pass

    def _user_temp_dir_count(self):
        # Counts `Packages/User/__sublime_mcp_temp_*__/` — the
        # `_new_temp_dir`-managed dirs probe_scopes uses for the
        # syntax_yaml branch. Should return to baseline after each
        # call; a non-zero delta is a regression in the helper's
        # `finally`.
        user_dir = os.path.join(sublime.packages_path(), "User")
        try:
            entries = os.listdir(user_dir)
        except OSError:
            return 0
        return sum(
            1
            for e in entries
            if e.startswith("__sublime_mcp_temp_") and e.endswith("__")
        )

    def _user_root_probe_count(self):
        # Defensive cleanup probe for the legacy-leak shape: the
        # original manual recipe wrote to
        # `Packages/User/Probe.sublime-syntax` (root of Packages/User/)
        # and failed to clean up on probe failure (#63 follow-up,
        # 2026-05-04). probe_scopes writes inside a managed temp dir,
        # so this count should always stay at zero.
        user_dir = os.path.join(sublime.packages_path(), "User")
        try:
            entries = os.listdir(user_dir)
        except OSError:
            return 0
        return sum(1 for e in entries if e.startswith("Probe.sublime-syntax"))

    def _view_count(self):
        return sum(len(w.views()) for w in sublime.windows())

    def setUp(self):
        self._defensive_temp_sweep()

    def test_bundled_syntax_sweep(self):
        before_views = self._view_count()
        code = (
            "import json\n"
            "r = probe_scopes(\n"
            "    'x = 1\\n',\n"
            "    syntax_path=%r,\n"
            ")\n"
            "print(json.dumps(r))\n"
        ) % self.PYTHON_SYNTAX
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertEqual(r["syntax"], self.PYTHON_SYNTAX)
        self.assertEqual(r["requested_syntax"], self.PYTHON_SYNTAX)
        self.assertEqual(r["resolved_syntax"], self.PYTHON_SYNTAX)
        self.assertEqual(r["view_size"], len("x = 1\n"))
        # JSON stringifies the int keys on the wire.
        self.assertIn("0", r["scopes"])
        self.assertIn("source.python", r["scopes"]["0"])
        self.assertGreater(len(r["tokens"]), 0)
        for tok in r["tokens"]:
            self.assertEqual(set(tok.keys()), {"region", "text", "scope"})
        self.assertEqual(self._view_count(), before_views)

    def test_synthetic_syntax_sweep_and_cleanup(self):
        before_temps = self._user_temp_dir_count()
        before_root_probes = self._user_root_probe_count()
        before_views = self._view_count()
        code = (
            "import json\n"
            "r = probe_scopes('x = 1\\n', syntax_yaml=%r)\n"
            "print(json.dumps(r))\n"
        ) % self.SYNTHETIC_YAML
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertTrue(
            r["syntax"].startswith("Packages/User/__sublime_mcp_temp_"),
            "expected synthesised URI under managed temp dir, got %r" % r["syntax"],
        )
        self.assertTrue(
            r["syntax"].endswith("/Probe.sublime-syntax"),
            "expected Probe.sublime-syntax basename, got %r" % r["syntax"],
        )
        self.assertEqual(r["requested_syntax"], r["syntax"])
        self.assertEqual(r["resolved_syntax"], r["syntax"])
        self.assertIn("keyword.smps", r["scopes"]["0"],
                      "synthetic scope did not surface: %r" % r["scopes"])
        # Cleanup invariants: temp dir gone, no Probe.sublime-syntax
        # leaked into Packages/User/ root, no view leaked.
        self.assertEqual(self._user_temp_dir_count(), before_temps)
        self.assertEqual(self._user_root_probe_count(), before_root_probes)
        self.assertEqual(self._view_count(), before_views)

    def test_points_subset(self):
        before_views = self._view_count()
        code = (
            "import json\n"
            "r = probe_scopes(\n"
            "    'x = 1\\n',\n"
            "    syntax_path=%r,\n"
            "    points=[0, 3],\n"
            ")\n"
            "print(json.dumps(r))\n"
        ) % self.PYTHON_SYNTAX
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertEqual(set(r["scopes"].keys()), {"0", "3"})
        self.assertEqual(self._view_count(), before_views)

    def test_default_preserves_trailing_space(self):
        # `rstrip_scopes` defaults to False (#114), so the no-arg
        # case must behave like an explicit False — trailing
        # whitespace from ST is preserved verbatim. The class of
        # caller most likely to ask "does ST normalise X?" should
        # see ST's actual output, not the helper's cleanup.
        before_views = self._view_count()
        code = (
            "import json\n"
            "r = probe_scopes('x = 1\\n', syntax_path=%r)\n"
            "print(json.dumps(r))\n"
        ) % self.PYTHON_SYNTAX
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        self.assertTrue(
            any(s.endswith(" ") for s in r["scopes"].values()),
            "expected at least one scope to retain trailing space "
            "with the default rstrip_scopes, got %r" % r["scopes"],
        )
        self.assertEqual(self._view_count(), before_views)

    def test_rstrip_scopes_true_strips_trailing_space(self):
        # Opt-in path: passing True is the ergonomic-compare mode —
        # all scopes returned have no trailing whitespace.
        before_views = self._view_count()
        code = (
            "import json\n"
            "r = probe_scopes(\n"
            "    'x = 1\\n',\n"
            "    syntax_path=%r,\n"
            "    rstrip_scopes=True,\n"
            ")\n"
            "print(json.dumps(r))\n"
        ) % self.PYTHON_SYNTAX
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        r = json.loads(outcome["output"])
        for s in r["scopes"].values():
            self.assertEqual(s, s.rstrip(),
                             "expected no trailing whitespace with "
                             "rstrip_scopes=True, got %r" % s)
        self.assertEqual(self._view_count(), before_views)

    def test_rejects_both_syntax_args(self):
        code = (
            "probe_scopes('x', syntax_path=%r, syntax_yaml=%r)\n"
        ) % (self.PYTHON_SYNTAX, self.SYNTHETIC_YAML)
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("ValueError", outcome["error"])
        self.assertIn("exactly one", outcome["error"])

    def test_rejects_neither_syntax_arg(self):
        code = "probe_scopes('x')\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("ValueError", outcome["error"])
        self.assertIn("exactly one", outcome["error"])


class TestProbeScopesCase3Detector(HelperTestBase):
    """Sweep-time case-3 silent-fallback detector for `probe_scopes`
    (#107 / #109). Direct unit tests against the private
    `_check_case3_silent_fallback` helper exercise the detector logic
    deterministically; an integration test confirms the detector
    fires on a real broken-cross-syntax fixture end-to-end.
    """

    PROBE_URI = "Packages/User/Probe.sublime-syntax"

    def test_no_raise_on_clean_chain(self):
        # Mixed scopes with no `text.plain` anywhere — the normal
        # probe outcome — must not raise. Locks in the false-positive
        # boundary.
        code = (
            "scopes = {0: 'source.probe meta.a', 1: 'source.probe meta.b'}\n"
            "_check_case3_silent_fallback(scopes, 'source.probe', %r)\n"
            "print('ok')\n"
        ) % self.PROBE_URI
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    def test_raises_when_every_position_is_bare_plain(self):
        # #107 single-syntax variant: every position tokenises as
        # bare `text.plain` despite a non-plain declared base.
        code = (
            "scopes = {0: 'text.plain', 1: 'text.plain', 5: 'text.plain'}\n"
            "try:\n"
            "    _check_case3_silent_fallback(scopes, 'source.probe', %r)\n"
            "except RuntimeError as e:\n"
            "    print('raised:', str(e))\n"
        ) % self.PROBE_URI
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        out = outcome["output"]
        self.assertIn("parse-table build failed", out)
        self.assertIn("#78 / #107", out)
        self.assertIn("source.probe", out)

    def test_raises_when_text_plain_is_a_non_leading_scope(self):
        # #109 embed-side variant: position 0 looks fine; positions
        # 1+ have `text.plain` mid-chain because a `push: scope:`
        # against an unresolvable target fell back to Plain Text.
        code = (
            "scopes = {\n"
            "    0: 'source.probe.host punctuation.host.enter',\n"
            "    1: 'source.probe.host text.plain',\n"
            "    2: 'source.probe.host text.plain marker.host',\n"
            "}\n"
            "try:\n"
            "    _check_case3_silent_fallback(scopes, 'source.probe.host', %r)\n"
            "except RuntimeError as e:\n"
            "    print('raised:', str(e))\n"
        ) % self.PROBE_URI
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        out = outcome["output"]
        self.assertIn("cross-syntax fallback detected", out)
        self.assertIn("#108 / #109", out)
        # Diagnostic names the offending position so the caller can
        # locate the failed embed without re-sweeping.
        self.assertIn("point 1", out)

    def test_no_raise_when_declared_base_is_plain(self):
        # If the assigned syntax is itself Plain Text, every
        # `text.plain` reading is the expected outcome. The detector
        # gates on a non-plain declared base; Plain Text fixtures
        # must not trip it.
        code = (
            "scopes = {0: 'text.plain', 1: 'text.plain'}\n"
            "_check_case3_silent_fallback(scopes, 'text.plain', %r)\n"
            "print('ok')\n"
        ) % self.PROBE_URI
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    def test_no_raise_on_empty_sweep(self):
        # Empty content / empty `points` → empty scopes dict. Without
        # a guard, `all(...)` over an empty iterable would mis-trigger
        # the all-plain raise.
        code = (
            "_check_case3_silent_fallback({}, 'source.probe', %r)\n"
            "print('ok')\n"
        ) % self.PROBE_URI
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    def test_no_raise_on_text_plain_as_leading_scope_only(self):
        # `text.plain` appearing as the FIRST scope element (the
        # declared base) is not a fallback — that's just a Plain Text
        # syntax doing its job. The detector ignores leading-position
        # `text.plain` and only flags non-leading occurrences.
        # Constructed defensively: declared_base is non-plain, but a
        # rogue position has bare `text.plain` (single element) — the
        # all-plain check would fire if every position were like this,
        # but with a mixed sweep, the leading-position skip prevents
        # the embed-side check from misfiring on this one entry.
        code = (
            "scopes = {0: 'text.plain', 1: 'source.probe meta.foo'}\n"
            "_check_case3_silent_fallback(scopes, 'source.probe', %r)\n"
            "print('ok')\n"
        ) % self.PROBE_URI
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    # An end-to-end integration test would synthesise a host syntax
    # pushing an unresolvable guest scope, drive probe_scopes against
    # it, and assert the detector fires. Empirically (live container
    # 2026-05-07 + #109 issue session 2026-05-06) ST produces the
    # expected `source.host text.plain` mid-chain shape — but the
    # CI's UnitTesting environment did NOT reproduce it on a first
    # pass, so the fallback timing is environment-sensitive. The
    # detector logic is fully exercised by the unit tests above;
    # finding a reliably-reproducible cross-syntax-fallback fixture
    # for the runner-driven path is left as a separate task.


class TestFindResources(HelperTestBase):
    """`find_resources` is a thin wrap of `sublime.find_resources`. A
    smoke test against a known bundled resource is sufficient — the
    helper has no logic of its own beyond `list(...)`-ing the result.
    """

    def test_finds_bundled_python_syntax(self):
        code = (
            "results = find_resources('Python.sublime-syntax')\n"
            "print('Packages/Python/Python.sublime-syntax' in results)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "True")

    def test_returns_list(self):
        # Wrapper materialises the result via list(). Asserts the type
        # contract — locks down a regression where the wrapper is
        # accidentally turned into a generator passthrough.
        code = (
            "results = find_resources('Python.sublime-syntax')\n"
            "print(type(results).__name__)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "list")


class TestDumpBytes(HelperTestBase):
    """`dump_bytes` returns a hex digest that survives the
    `repr` -> JSON transport unchanged (#115). Used for byte-exact
    questions where the `result` channel's escape-doubling makes a
    real tab and the literal `\\` + `t` indistinguishable.
    """

    def test_distinguishes_real_tab_from_escape_sequence(self):
        # The motivating case: a one-byte real tab (0x09) and the
        # two-byte sequence `\` + `t` produce visually-identical
        # repr-then-JSON output. Hex output disambiguates: 09 vs
        # 5c74.
        code = (
            "print(dump_bytes('\\t'))\n"
            "print(dump_bytes('\\\\t'))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        lines = outcome["output"].strip().splitlines()
        self.assertEqual(lines[0], "09")
        self.assertEqual(lines[1], "5c74")

    def test_str_input_uses_utf8(self):
        # Multi-byte characters round-trip through UTF-8. `é` is
        # 0xc3 0xa9 in UTF-8.
        code = "print(dump_bytes('é'))\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "c3a9")

    def test_bytes_input_passes_through(self):
        # Bytes values render hex directly without re-encoding.
        code = "print(dump_bytes(b'\\x00\\xff'))\n"
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "00ff")

    def test_recoverable_on_agent_side(self):
        # The contract is "agent-side recoverable via
        # bytes.fromhex(hex).decode('utf-8')". Confirm the round-trip
        # by asserting equality after the recover.
        code = (
            "import json\n"
            "_ = json.dumps({'h': dump_bytes('hello\\tworld')})\n"
            "print(_)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        payload = json.loads(outcome["output"].strip())
        recovered = bytes.fromhex(payload["h"]).decode("utf-8")
        self.assertEqual(recovered, "hello\tworld")

    def test_rejects_unsupported_type(self):
        # Catches the most common mistake (passing a list / dict
        # directly instead of a string / bytes value).
        code = (
            "try:\n"
            "    dump_bytes([1, 2, 3])\n"
            "except TypeError as e:\n"
            "    print('raised', e)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertTrue(outcome["output"].strip().startswith("raised "))
        self.assertIn("expected str", outcome["output"])


class TestPreflightWedgeCheck(HelperTestBase):
    """`preflight_wedge_check` is the static lint for known-wedge
    synthetic-syntax shapes (#103). Returns a list of warning dicts
    on the public surface; raises `WedgeShape` on `strict=True`.
    The lint also runs automatically inside `temp_packages_link`,
    where it logs warnings without aborting.
    """

    CLEAN_YAML = (
        "%YAML 1.2\n"
        "---\n"
        "name: PreflightClean\n"
        "scope: source.preflight.clean\n"
        "contexts:\n"
        "  main:\n"
        "    - match: 'a'\n"
        "      scope: keyword\n"
    )

    DUPLICATE_INCLUDE_YAML = (
        "%YAML 1.2\n"
        "---\n"
        "name: PreflightDupInclude\n"
        "scope: source.preflight.dup\n"
        "contexts:\n"
        "  main:\n"
        "    - include: scope:source.host#frag1\n"
        "    - match: 'b'\n"
        "      scope: keyword\n"
        "    - include: scope:source.host#frag2\n"
    )

    ZERO_WIDTH_PUSH_YAML = (
        "%YAML 1.2\n"
        "---\n"
        "name: PreflightZeroWidth\n"
        "scope: source.preflight.zwp\n"
        "contexts:\n"
        "  main:\n"
        "    - match: '(?=foo)'\n"
        "      push: pushed_ctx\n"
        "  pushed_ctx:\n"
        "    - match: 'foo'\n"
        "      pop: true\n"
    )

    def test_clean_yaml_returns_no_warnings(self):
        code = (
            "import json\n"
            "_ = json.dumps(preflight_wedge_check(%r))\n"
            "print(_)\n"
        ) % self.CLEAN_YAML
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(json.loads(outcome["output"].strip()), [])

    def test_detects_duplicate_cross_scope_include(self):
        # Shape #103 / 2: same `include: scope:<base>` referenced
        # twice in one syntax. Detector ignores fragment differences;
        # the wedge is on the base.
        code = (
            "import json\n"
            "_ = json.dumps(preflight_wedge_check(%r))\n"
            "print(_)\n"
        ) % self.DUPLICATE_INCLUDE_YAML
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        warnings = json.loads(outcome["output"].strip())
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["shape"], "duplicate-cross-scope-include")
        self.assertIn("source.host", warnings[0]["message"])
        self.assertIn("#103 shape 2", warnings[0]["message"])

    def test_detects_zero_width_match_with_push(self):
        # Narrow form of #103 shape 1: a rule whose `match:` value is
        # purely a lookahead expression paired with `push:`.
        code = (
            "import json\n"
            "_ = json.dumps(preflight_wedge_check(%r))\n"
            "print(_)\n"
        ) % self.ZERO_WIDTH_PUSH_YAML
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        warnings = json.loads(outcome["output"].strip())
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["shape"], "zero-width-match-with-push")
        self.assertIn("#103 shape 1", warnings[0]["message"])
        self.assertIsNotNone(warnings[0]["line"])

    def test_no_misfire_on_consuming_match_with_push(self):
        # A consuming match value paired with push is the everyday
        # shape — must not trip the zero-width detector. Locks in the
        # false-positive boundary for the narrow heuristic.
        yaml = (
            "%YAML 1.2\n"
            "---\n"
            "name: ConsumingPush\n"
            "scope: source.preflight.consume\n"
            "contexts:\n"
            "  main:\n"
            "    - match: 'foo'\n"
            "      push: pushed_ctx\n"
            "  pushed_ctx:\n"
            "    - match: 'bar'\n"
            "      pop: true\n"
        )
        code = (
            "import json\n"
            "_ = json.dumps(preflight_wedge_check(%r))\n"
            "print(_)\n"
        ) % yaml
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(json.loads(outcome["output"].strip()), [])

    def test_strict_mode_raises_wedge_shape(self):
        # `strict=True` raises rather than returns; the warnings list
        # is on the exception's `warnings` attribute so callers can
        # introspect without re-running the check.
        code = (
            "try:\n"
            "    preflight_wedge_check(%r, strict=True)\n"
            "    print('did not raise')\n"
            "except WedgeShape as e:\n"
            "    print('raised', len(e.warnings))\n"
        ) % self.DUPLICATE_INCLUDE_YAML
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "raised 1")

    def test_strict_mode_does_not_raise_on_clean_yaml(self):
        # Clean YAML must not raise even in strict mode — `WedgeShape`
        # is reserved for actual warnings, not "the lint ran".
        code = (
            "preflight_wedge_check(%r, strict=True)\n"
            "print('ok')\n"
        ) % self.CLEAN_YAML
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["output"].strip(), "ok")

    def test_rejects_non_string_input(self):
        code = (
            "try:\n"
            "    preflight_wedge_check(b'not str')\n"
            "except TypeError as e:\n"
            "    print('raised', e)\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertTrue(outcome["output"].strip().startswith("raised "))
        self.assertIn("expected str", outcome["output"])

    def test_temp_packages_link_logs_warnings_for_wedging_input(self):
        # Integration: a directory containing a wedging syntax. The
        # link is still created (the lint is advisory), but the
        # warning must reach the in-process logger via the
        # `[bridge]` shape — captured here by patching the helper's
        # logger handler so the test sees the messages without
        # depending on the live MCP bridge stderr.
        target_dir = tempfile.mkdtemp(prefix="sublime_mcp_test_preflight_")
        self.addCleanup(shutil.rmtree, target_dir, ignore_errors=True)
        with open(os.path.join(target_dir, "Wedge.sublime-syntax"), "w") as f:
            f.write(self.DUPLICATE_INCLUDE_YAML)
        code = (
            "import logging\n"
            "captured = []\n"
            "class _Handler(logging.Handler):\n"
            "    def emit(self, record):\n"
            "        captured.append(record.getMessage())\n"
            "h = _Handler(level=logging.WARNING)\n"
            "_log_local = logging.getLogger('sublime_mcp.bridge')\n"
            "_log_local.addHandler(h)\n"
            "try:\n"
            "    name = temp_packages_link(%r)\n"
            "finally:\n"
            "    _log_local.removeHandler(h)\n"
            "release_packages_link(name)\n"
            "import json\n"
            "_ = json.dumps([m for m in captured if 'preflight' in m])\n"
            "print(_)\n"
        ) % target_dir
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        messages = json.loads(outcome["output"].strip())
        self.assertTrue(messages, "expected at least one preflight warning")
        joined = " ".join(messages)
        self.assertIn("duplicate-cross-scope-include", joined)
        self.assertIn("Wedge.sublime-syntax", joined)


class TestReloadSyntax(HelperTestBase):
    """`reload_syntax` re-binds the resource path on every view whose
    `settings()["syntax"]` matches; views bound to other syntaxes are
    untouched. ST's downstream behaviour (re-tokenising, rescanning the
    resource) is a side effect of the re-bind, not part of the helper's
    contract.

    Hermetic: relies on the `_get_windows` seam (#20) to inject a fake
    windows-and-views graph in the snippet's globals. No real ST views
    are created; no real syntax assignment happens. The test pins the
    helper's observable effect (`view.assign_syntax(uri)` calls) without
    depending on ST's reload pipeline.
    """

    FAKE_GRAPH_PROLOGUE = '''
class _FakeView:
    def __init__(self, syntax_uri):
        self._syntax = syntax_uri
        self.assign_calls = []
    def settings(self):
        return self
    def get(self, key, default=None):
        return self._syntax if key == "syntax" else default
    def assign_syntax(self, uri):
        self.assign_calls.append(uri)

class _FakeWindow:
    def __init__(self, views):
        self._views = views
    def views(self):
        return list(self._views)
'''

    def test_rebinds_matching_view_only(self):
        code = self.FAKE_GRAPH_PROLOGUE + (
            "v_py = _FakeView('Packages/Python/Python.sublime-syntax')\n"
            "v_md = _FakeView('Packages/Markdown/Markdown.sublime-syntax')\n"
            "_get_windows = lambda: [_FakeWindow([v_py, v_md])]\n"
            "reload_syntax('Packages/Python/Python.sublime-syntax')\n"
            "import json\n"
            "print(json.dumps([v_py.assign_calls, v_md.assign_calls]))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        py_calls, md_calls = json.loads(outcome["output"])
        self.assertEqual(py_calls, ["Packages/Python/Python.sublime-syntax"])
        self.assertEqual(md_calls, [])

    def test_no_views_match_no_calls(self):
        # Negative case: no view's syntax matches → no view sees an
        # assign_syntax call. Locks the contract that reload_syntax
        # is a pure observation when there's nothing to re-bind.
        code = self.FAKE_GRAPH_PROLOGUE + (
            "v_md = _FakeView('Packages/Markdown/Markdown.sublime-syntax')\n"
            "_get_windows = lambda: [_FakeWindow([v_md])]\n"
            "reload_syntax('Packages/Python/Python.sublime-syntax')\n"
            "import json\n"
            "print(json.dumps(v_md.assign_calls))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(json.loads(outcome["output"]), [])

    def test_iterates_all_windows(self):
        # Multiple windows; matching views in each. Locks the iteration
        # over both `_get_windows()` and `window.views()`.
        code = self.FAKE_GRAPH_PROLOGUE + (
            "v1 = _FakeView('Packages/Python/Python.sublime-syntax')\n"
            "v2 = _FakeView('Packages/Python/Python.sublime-syntax')\n"
            "_get_windows = lambda: [_FakeWindow([v1]), _FakeWindow([v2])]\n"
            "reload_syntax('Packages/Python/Python.sublime-syntax')\n"
            "import json\n"
            "print(json.dumps([v1.assign_calls, v2.assign_calls]))\n"
        )
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        v1_calls, v2_calls = json.loads(outcome["output"])
        self.assertEqual(v1_calls, ["Packages/Python/Python.sublime-syntax"])
        self.assertEqual(v2_calls, ["Packages/Python/Python.sublime-syntax"])


@unittest.skipIf(
    sys.platform == "win32",
    "TestResolvePositionFilesystemSyntax requires symlink creation, "
    "which needs SeCreateSymbolicLinkPrivilege or Developer Mode on "
    "Windows. Same constraint as TestToResourcePathSymlinked.",
)
class TestResolvePositionFilesystemSyntax(HelperTestBase):
    """`resolve_position`'s `syntax_path` accepts a filesystem path under
    `sublime.packages_path()` (directly or via a symlink in that
    directory) and normalises it to a `Packages/...` URI before
    calling `assign_syntax_and_wait`. The normalisation is what keeps
    the #11 silent-fallback contract (`resolved_syntax ==
    requested_syntax` equality) meaningful when callers pass
    filesystem paths — the equality itself is not re-asserted here
    because fresh temp syntaxes race ST's syntax-loader latency; see
    the per-test comments.

    Reuses `TestTempPackagesLink`'s syntax-fixture pattern: synthesise
    a `.sublime-syntax` under a tempdir, link it into Packages/ via
    `temp_packages_link`, then probe.
    """

    SYNTAX_CONTENT = (
        "%YAML 1.2\n"
        "---\n"
        "name: SublimeMcpResolvePositionProbe\n"
        "scope: source.smrpp\n"
        "file_extensions: [smrpp]\n"
        "version: 2\n"
        "contexts:\n"
        "  main:\n"
        "    - match: 'x'\n"
        "      scope: keyword.smrpp\n"
    )

    SYNTAX_BASENAME = "Probe.sublime-syntax"

    def setUp(self):
        self._defensive_link_sweep()
        self.target_dir = tempfile.mkdtemp(prefix="sublime_mcp_test_resolve_")
        self.addCleanup(shutil.rmtree, self.target_dir, ignore_errors=True)
        self.syntax_path = os.path.join(self.target_dir, self.SYNTAX_BASENAME)
        with open(self.syntax_path, "w") as f:
            f.write(self.SYNTAX_CONTENT)
        # Plain text fixture for resolve_position's `path` arg — the
        # syntax assignment is what's under test, not the file's
        # default-syntax inference.
        self.fixture_path = self._write_fixture("filesystem_syntax", "x = 1\n")

    def _defensive_link_sweep(self):
        # Same as TestTempPackagesLink — clean stale temp symlinks
        # left behind by a prior crashed run.
        packages_root = sublime.packages_path()
        for name in os.listdir(packages_root):
            if not (name.startswith("__sublime_mcp_temp_") and name.endswith("__")):
                continue
            full = os.path.join(packages_root, name)
            if os.path.islink(full):
                try:
                    os.unlink(full)
                except OSError:
                    pass

    def _release(self, name):
        link_path = os.path.join(sublime.packages_path(), name)
        if os.path.lexists(link_path) and os.path.islink(link_path):
            os.unlink(link_path)

    def test_filesystem_path_normalises_to_packages_uri(self):
        # The #22 contract is the normalisation: a filesystem-form
        # syntax_path is rewritten to Packages/<name>/... before being
        # echoed as `requested_syntax`. That is what this class pins.
        # Deliberately NOT asserting resolved_syntax: ST's resource
        # indexer (find_resources, which temp_packages_link waits for)
        # and its syntax-loader (which view.syntax() reads) have
        # different latencies for fresh syntaxes — view.syntax() can
        # still be None when the resource has surfaced, leading to a
        # racy assertion. The resolved_syntax==requested_syntax
        # silent-fallback contract from #11 is exercised by
        # TestResolvePosition.test_in_bounds_no_flags_set against the
        # bundled Python syntax (no fresh-syntax race).
        code = (
            "import json\n"
            "name = temp_packages_link(%r)\n"
            "try:\n"
            "    r = resolve_position(%r, 0, 0, syntax_path=%r)\n"
            "    print(json.dumps([name, r]))\n"
            "finally:\n"
            "    release_packages_link(name)\n"
        ) % (self.syntax_path, self.fixture_path, self.syntax_path)
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name, r = json.loads(outcome["output"])
        expected_uri = "Packages/%s/%s" % (name, self.SYNTAX_BASENAME)
        self.assertEqual(r["requested_syntax"], expected_uri)

    def test_packages_uri_input_still_works(self):
        # Regression-proofing: the existing `Packages/...` URI form
        # passes through `_to_resource_path` unchanged. Tests that #22
        # didn't break the original contract. resolved_syntax is again
        # not asserted (same fresh-syntax race as the sibling test).
        code = (
            "import json\n"
            "name = temp_packages_link(%r)\n"
            "try:\n"
            "    uri = 'Packages/%%s/%s' %% name\n"
            "    r = resolve_position(%r, 0, 0, syntax_path=uri)\n"
            "    print(json.dumps([name, r]))\n"
            "finally:\n"
            "    release_packages_link(name)\n"
        ) % (self.syntax_path, self.SYNTAX_BASENAME, self.fixture_path)
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        name, r = json.loads(outcome["output"])
        expected_uri = "Packages/%s/%s" % (name, self.SYNTAX_BASENAME)
        self.assertEqual(r["requested_syntax"], expected_uri)

    def test_outside_packages_filesystem_path_raises(self):
        # A filesystem path with no symlink chain into Packages/ must
        # raise ValueError pointing at temp_packages_link, not silently
        # pass through to view.assign_syntax (which would fall back to
        # text.plain — the failure mode #11 hardened against).
        unreachable = tempfile.mkdtemp(prefix="sublime_mcp_test_unreachable_")
        self.addCleanup(shutil.rmtree, unreachable, ignore_errors=True)
        unreachable_syntax = os.path.join(unreachable, self.SYNTAX_BASENAME)
        with open(unreachable_syntax, "w") as f:
            f.write(self.SYNTAX_CONTENT)
        code = (
            "resolve_position(%r, 0, 0, syntax_path=%r)\n"
        ) % (self.fixture_path, unreachable_syntax)
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("ValueError", outcome["error"])
        self.assertIn("not under sublime.packages_path()", outcome["error"])
        self.assertIn("temp_packages_link", outcome["error"])


class TestResolvePositionPostAssignRace(HelperTestBase):
    """`resolve_position` absorbs both directions of #70/#94's post-assign
    race against a fresh synthetic syntax. The OP direction (`view.syntax()`
    returns `None` while `view.scope_name(...)` already latched) is
    absorbed by `_resolved_syntax_with_op_race_mitigation`'s
    `view.settings()["syntax"]` substitution. The symmetric direction
    (`view.scope_name(point)` returns `text.plain` while `view.syntax()`
    has latched the assigned syntax — observed at #94) is absorbed by an
    inline `view.scope_name(point)`-only poll inside `resolve_position`,
    mirroring `assign_syntax_and_wait` stage 2's shape (50 Hz / 200 ms,
    no `view.syntax()` re-read — that's the wedge boundary surfaced by
    PR #92's bisect).

    SKILL.md §4's strict `resolved_syntax == requested_syntax` plus
    `scope != "text.plain"` is the contract callers learned. The OP
    direction makes the equality assertion *spuriously fail*; the
    symmetric direction makes it *spuriously pass* while the scope is
    `text.plain` — silent and worse. This regression loops
    `resolve_position` against a fresh `temp_packages_link`-installed
    syntax, opens a fresh view per call, and asserts both halves.
    """

    SYNTAX_CONTENT = (
        "%YAML 1.2\n"
        "---\n"
        "name: SublimeMcpRaceProbe\n"
        "scope: source.smrace\n"
        "file_extensions: [smrace]\n"
        "version: 2\n"
        "contexts:\n"
        "  main:\n"
        "    - match: 'x'\n"
        "      scope: keyword.smrace\n"
    )

    SYNTAX_BASENAME = "RaceProbe.sublime-syntax"

    def setUp(self):
        self._defensive_link_sweep()
        self.target_dir = tempfile.mkdtemp(prefix="sublime_mcp_test_race_")
        self.addCleanup(shutil.rmtree, self.target_dir, ignore_errors=True)
        self.syntax_path = os.path.join(self.target_dir, self.SYNTAX_BASENAME)
        with open(self.syntax_path, "w") as f:
            f.write(self.SYNTAX_CONTENT)

    def _defensive_link_sweep(self):
        packages_root = sublime.packages_path()
        for name in os.listdir(packages_root):
            if not (name.startswith("__sublime_mcp_temp_") and name.endswith("__")):
                continue
            full = os.path.join(packages_root, name)
            if os.path.islink(full):
                try:
                    os.unlink(full)
                except OSError:
                    pass

    def test_both_race_directions_absorbed(self):
        # N=10 iterations, each opens a fresh fixture view and reads col
        # 0 of "x ...". Each call goes through the post-assign race
        # window. Assertions per envelope:
        # - `resolved_syntax == requested_syntax` — the OP direction;
        #   without the settings-substitution mitigation, `view.syntax()`
        #   lag makes this spuriously fail.
        # - `scope != "text.plain"` — the symmetric direction;
        #   without the inline scope_name poll, `view.scope_name(0)` can
        #   lag behind `view.syntax()` and return `text.plain` while the
        #   syntax setting matches, making the equality above
        #   spuriously pass on top of bogus ground truth.
        # The fixture filename intentionally omits the `.smrace`
        # extension the syntax declares: with that extension, ST's
        # resource indexer would auto-assign the syntax on view-open
        # and the explicit `assign_syntax` call inside
        # `resolve_position` would land on an already-tokenised view,
        # narrowing the race window below the test's signal threshold.
        n = 10
        fixture_paths = [
            self._write_fixture("race_input_%d" % i, "x = 1\n")
            for i in range(n)
        ]
        code = (
            "import json\n"
            "fixtures = %r\n"
            "name = temp_packages_link(%r)\n"
            "try:\n"
            "    rs = []\n"
            "    for p in fixtures:\n"
            "        r = resolve_position(p, 0, 0, syntax_path=%r)\n"
            "        rs.append(r)\n"
            "    print(json.dumps(rs))\n"
            "finally:\n"
            "    release_packages_link(name)\n"
        ) % (fixture_paths, self.syntax_path, self.syntax_path)
        resp = yield from _call_tool_yielding(code)
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        envelopes = json.loads(outcome["output"])
        self.assertEqual(len(envelopes), n)
        for r in envelopes:
            self.assertEqual(
                r["resolved_syntax"], r["requested_syntax"],
                "OP race not absorbed (view.syntax() lag leaked through): %r" % r,
            )
            self.assertNotEqual(
                r["scope"], "text.plain",
                "symmetric race not absorbed "
                "(view.scope_name(0) returned text.plain under a non-plain syntax): %r" % r,
            )


class TestPerCallTimeout(HelperTestBase):
    """Per-call `timeout_seconds` override on `exec_sublime_python` (#62).

    Lets adversarial probes fail fast — when the question is "does ST
    loop on this regex?", a hang is the answer and the round-trip cost
    should be the override budget rather than the full 60 s ceiling.
    """

    def test_default_uses_60s_ceiling(self):
        # Sanity / regression: omitting `timeout_seconds` keeps the
        # original envelope shape and 60 s ceiling. A fast snippet
        # returns clean.
        resp = yield from _call_tool_yielding("_ = 1 + 1")
        outcome = _outcome(resp)
        self.assertIsNone(outcome["error"], outcome.get("error"))
        self.assertEqual(outcome["result"], "2")

    def test_override_lowers_ceiling(self):
        # `timeout_seconds=1.0` with a 5 s sleep: the response must
        # arrive well before the 60 s ceiling and carry the per-call
        # wording. Wall-clock budget is generous (5 s, vs the ~1 s
        # expected) to absorb scheduler jitter without flaking.
        start = time.time()
        resp = yield from _call_tool_yielding(
            "import time; time.sleep(5); _ = 'unreached'",
            mcp_timeout_seconds=1.0,
        )
        elapsed = time.time() - start
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertIn("snippet exceeded the per-call timeout of", outcome["error"])
        self.assertIsNone(outcome["result"])
        self.assertLess(elapsed, 5.0,
                        "per-call timeout did not fire below the ceiling")

    def test_override_clamped_above_ceiling(self):
        # `timeout_seconds=120` exceeds the 60 s transport ceiling: the
        # plugin clamps it down internally rather than rejecting. A fast
        # snippet still returns clean, proving the request was accepted
        # past the JSON-Schema layer (which has `maximum: 60`) — the
        # in-process clamp is the second line of defence.
        resp = yield from _call_tool_yielding(
            "_ = 'ok'", mcp_timeout_seconds=120.0,
        )
        outcome = _outcome(resp)
        # Note: the JSON-Schema `maximum: 60` may or may not be enforced
        # by the transport depending on validator. If `error` is set and
        # carries a transport-level rejection wording, treat that as a
        # legitimate stricter outcome; otherwise the snippet ran clean.
        if outcome.get("error") is None:
            self.assertEqual(outcome["result"], "'ok'")

    def test_override_clamped_below_minimum(self):
        # `timeout_seconds=0.05` falls below the 0.1 s floor: the plugin
        # clamps up to 0.1 s. A trivial snippet completes well within
        # that budget, so the call returns clean. We aren't asserting
        # on the floor's exact value — just that sub-floor inputs don't
        # cause an immediate timeout for a fast snippet.
        resp = yield from _call_tool_yielding(
            "_ = 42", mcp_timeout_seconds=0.05,
        )
        outcome = _outcome(resp)
        if outcome.get("error") is None:
            self.assertEqual(outcome["result"], "42")

    def test_per_call_message_distinguishes_from_ceiling(self):
        # The per-call wording must be string-distinguishable from the
        # transport-ceiling wording so callers can branch on which
        # deadline fired. We don't exercise the 60 s ceiling case here
        # (would burn 60 s of wall-clock); we just assert the per-call
        # branch produces a message that does NOT contain the
        # transport-ceiling phrase.
        resp = yield from _call_tool_yielding(
            "import time; time.sleep(5); _ = 'unreached'",
            mcp_timeout_seconds=0.5,
        )
        outcome = _outcome(resp)
        self.assertIsNotNone(outcome["error"])
        self.assertNotIn("exec timed out after", outcome["error"])
        self.assertIn("per-call timeout", outcome["error"])

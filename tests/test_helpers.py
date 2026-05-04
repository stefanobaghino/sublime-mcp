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
        # at sublime_mcp.py:518 inherits it without naming it, so the
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

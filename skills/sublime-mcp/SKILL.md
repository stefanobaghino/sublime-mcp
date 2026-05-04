---
name: sublime-mcp
description: |
  Ask Sublime Text directly what scope it assigns, which syntax it
  resolved, or whether a syntax-test file passes. Use when you need
  ST's ground-truth answer to a scope / syntax-resolution /
  syntax-test question, are comparing another parser's output
  against ST, or are about to add `print`/logging to inspect
  something ST can just answer via `scope_at` / `run_syntax_tests`.

  Do NOT use for Sublime Text plugin authoring, ST UI automation or
  keybinding tests, general text editing, or anything answerable by
  static code inspection alone.
allowed-tools: Bash, Read, Grep, Glob, mcp__sublime-text__exec_sublime_python
---

# Sublime Text ground-truth via MCP

This skill drives the `sublime-mcp` server to get authoritative answers from Sublime Text itself — what scope it assigns at a point, which `.sublime-syntax` it resolved, whether an assertion file passes ST's built-in runner — via one tool, `exec_sublime_python`, which runs Python inside ST's plugin host. The server answers at `127.0.0.1:47823` while ST is open with the plugin loaded.

## 1. Preflight — check before driving the tool

If `mcp__sublime-text__exec_sublime_python` appears anywhere in your tool surface — either listed in the deferred-tools system-reminder or already resolved in your toolbox — skip to §2.

If it's missing, diagnose with:

```bash
claude mcp list | grep sublime-text
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"exec_sublime_python","arguments":{"code":"print(len(sublime.windows()))"}}}'
```

Expected: `claude mcp list` shows `sublime-text ✓ Connected`; the second call returns a JSON-RPC `result` with a `protocolVersion`; the third call's `output` is `"1\n"` or higher. If `claude mcp list` or the second call fails, point the user at `install.md` in this skill's directory. If the third call's `output` is `"0\n"`, ST's plugin host is running but headless — ask the user to open an ST window (`open -a "Sublime Text"` on macOS) before proceeding; helpers that drive views (`open_view`, `scope_at`, `scope_at_test`, `resolve_position`) raise `RuntimeError` in this state. Do not attempt to fall back to manual ST UI inspection without first telling the user the skill cannot run.

## 2. Decide whether this skill is the right call

Reach for this skill when the question is "what does Sublime Text do / see / say at this point?" and the alternative is guessing, paraphrasing from memory, or asking the user to click through ST's UI.

- **Use this skill** for: scope at a specific row/col; whether ST's built-in syntax-test runner passes an assertion file; which `.sublime-syntax` ST resolved for a given path (bundled vs repo-local); any comparison where ST is the reference implementation for a downstream parser (e.g. syntect).
- **Recommend `Read` / `Grep` instead** when the answer is in source — `.sublime-syntax` authoring, `.tmLanguage` conversion, plugin API lookup from docstrings.
- **Not this skill** for Sublime Text UI automation, keybinding tests, or packaging questions. Hand back to the user.

If borderline, say which way you're leaning in one sentence, then proceed.

## 3. The one tool and its contract

`mcp__sublime-text__exec_sublime_python({ code })` runs `code` on a dedicated daemon thread inside ST's plugin host (Python 3.8) and returns:

```json
{ "output": "<captured print()>", "result": "<repr(_) or null>", "error": "<traceback or null>", "st_version": 4200, "st_channel": "stable", "isError": false }
```

- Assign to `_` inside the snippet to get its `repr` back as `result`.
- `error` is populated on uncaught exception; `isError` is derived from `error is not None`. Helper failures (e.g. `run_syntax_tests` cannot complete the run) raise and surface in this same `error` field — there is no separate helper-level error channel.
- `st_version` (int) and `st_channel` (str, e.g. `"stable"` / `"dev"`) echo the running ST build on every response. Use these to detect channel mismatches when probing grammars whose CI gates on a non-stable channel.
- `run_syntax_tests(...)["state"]` reports the assertion-run outcome (`passed` / `failed`).
- Preloaded helpers (`scope_at`, `scope_at_test`, `resolve_position`, `run_syntax_tests`, `open_view`, `assign_syntax_and_wait`, `find_resources`, `reload_syntax`) are in scope without import.

For the full helper surface, threading guarantees, and the authoritative `text_point` overflow semantics, read the tool's own `description` via `tools/list`. If this skill contradicts it, `tools/list` is right.

## 4. Recipes

Each recipe is one `exec_sublime_python` call. Rows and columns are **0-indexed** — a test-file assertion on line 181 col 9 is `row=180, col=8`.

### Scope at a position

```python
r = scope_at("/path/to/Packages/C#/tests/syntax_test_Generics.cs", 180, 8)
print(r["scope"], "via", r["resolved_syntax"])
```

`scope_at` returns `{"scope": str, "resolved_syntax": str | None}`. `resolved_syntax` is the URI ST actually loaded (`view.syntax().path`) — `None` when no syntax resolved, `"Packages/Text/Plain text.tmLanguage"` when ST defaulted to Plain Text. Branch on `resolved_syntax` to detect silent fallback before treating `scope` as ground truth.

**Landmine: extension-less syntax-test files** (`syntax_test_git_config`, no suffix) silently fall back to Plain Text via `scope_at` — `scope == "text.plain"` and `resolved_syntax == "Packages/Text/Plain text.tmLanguage"`. Use `scope_at_test` — it parses the `# SYNTAX TEST "Packages/..."` header and assigns that syntax before sampling.

```python
r = scope_at_test("/path/to/syntax_test_git_config", 71, 28)
print(r["scope"])
```

The header parser is comment-token-agnostic — it accepts `#`, `//`, `<!--`, `;`, `--`, `|`, etc. Markdown's pipe-comment header works the same way:

```python
r = scope_at_test("/path/to/syntax_test_markdown.md", 12, 4)
print(r["scope"])
```

### Run syntax tests against a file

```python
r = run_syntax_tests("/path/to/Packages/C#/tests/syntax_test_Generics.cs")
print(r["summary"])
for msg in r["failures"]:
    print(msg)
```

Branch on `state` for the assertion-run outcome:

| `state`     | meaning                                                                          | `summary` shape                                  | `failures`     |
| ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------ | -------------- |
| `"passed"`  | runner completed; every assertion matched                                        | assertion-count headline                         | `[]`           |
| `"failed"`  | runner completed; some assertions did not match — read `failures` for specifics  | `"FAILED: N of M assertions failed"`             | populated      |

When ST cannot complete the run, `run_syntax_tests` raises `RuntimeError` and the cause surfaces in the top-level `error` of the MCP response — `isError` is true. The reachable causes are: resource not yet indexed, path outside `sublime.packages_path()` (symlink it in first — see "Confirm which syntax ST assigned (and handle repo-local syntaxes)" below), and the private `sublime_api.run_syntax_test` missing on this ST build. For ground-truth questions that don't need the assertion runner, fall back to `scope_at` / `scope_at_test` or `resolve_position`.

### Probe a synthetic case inline

For "what does ST do on this case?" probes, `run_inline_syntax_test(content, name)` owns the file-write, indexing wait, runner call, and cleanup. The header inside `content` selects the syntax under test; the syntax must already be reachable to ST (bundled or via `temp_packages_link`).

```python
r = run_inline_syntax_test(
    '# SYNTAX TEST "Packages/Python/Python.sublime-syntax"\n'
    'x = 1\n'
    '# ^ source.python\n',
    "syntax_test_probe",
)
print(r["state"], r["summary"])
```

Same `{state, summary, output, failures}` shape as `run_syntax_tests`, with one extra state `"inconclusive"` when ST never indexes the temp resource within the wait budget. The probe's temp dir is removed on every code path (within-call `try/finally`); a cross-call sweep at the start of each call cleans up SIGKILL-orphaned dirs older than 60 s.

### Confirm which syntax ST assigned (and handle repo-local syntaxes)

`view.assign_syntax` takes a `Packages/...` resource URI, not an arbitrary filesystem path. The older `view.set_syntax_file` has the same constraint but fails silently when given a filesystem path: `view.settings().get("syntax")` echoes the assigned absolute path, ST surfaces a "file not found" popup, `view.scope_name(...)` returns `text.plain` for every position, and the Python call doesn't raise. Prefer `assign_syntax_and_wait`.

For a syntax file that lives outside ST's Packages tree (e.g. a syntect `testdata/Packages/...` copy), use `temp_packages_link` to manage a per-call symlink. The helper synthesises `Packages/__sublime_mcp_temp_<nonce>__`, waits for ST's resource indexer to surface the sentinel, and returns the synthesised package name. The caller builds URIs against it and tears down via `release_packages_link`.

```python
name = temp_packages_link("/path/to/repo/testdata/Packages/Java/Java.sublime-syntax")
try:
    r = resolve_position(
        "/path/to/syntax_test_file", row=71, col=29,
        syntax_path="Packages/%s/Java.sublime-syntax" % name,
    )
    print(r["scope"], "overflow:", r["overflow"], "clamped:", r["clamped"])
    assert r["resolved_syntax"] == r["requested_syntax"], r
finally:
    release_packages_link(name)
```

The returned dict also carries `overflow` (past-EOL request wrapped into a later row), `clamped` (past-EOF, point at `view.size()`) — mutually exclusive flags that surface a quiet `text_point` behaviour; the full semantics are in `TOOL_DESCRIPTION`'s "text_point overflow" section. `requested_syntax` echoes the `syntax_path` argument and `resolved_syntax` is `view.syntax().path` — assert they match before treating `scope` as ground truth, since `view.assign_syntax` accepts any string and silently falls through to Plain Text when the URI doesn't resolve.

`temp_packages_link` synthesises a unique nonce-named package, so the bundled `Packages/Java` continues to load alongside it — `requested_syntax != resolved_syntax` still flags any silent fallback to a built-in. The per-syntax mode is sufficient for synthetic probes and single-grammar regression triage; cross-grammar investigations where the testdata grammar embeds another testdata grammar (e.g. C# embedding RegExp) need a whole-tree mirror that shadows the built-ins, tracked separately in §6.

`scope_at_test` parses the URI from the file's `SYNTAX TEST` header (conventionally `Packages/...` already) and exposes the same `requested_syntax` / `resolved_syntax` pair without needing a symlink. `run_syntax_tests` accepts any path under `sublime.packages_path()` (directly or via symlink); pair it with `temp_packages_link` to cover paths outside the Packages tree.

### Compare a parser's output against ST

Three-step divergence triage:

```python
# 1. What does ST report at the failing position?
r = scope_at_test("/path/to/syntax_test_git_config", 71, 28)
print(r["scope"], "via", r["resolved_syntax"])

# 2. Did both engines sample the same point? (past-EOL divergence is common)
r = resolve_position(
    "/path/to/syntax_test_git_config", 71, 29,
    syntax_path="Packages/Git Formats/Git Config.sublime-syntax",
)
print("overflow:", r["overflow"], "clamped:", r["clamped"], "actual:", r["actual"])

# 3. Does ST's own runner agree?
r = run_syntax_tests("/path/to/syntax_test_git_config")
print(r["summary"])
```

If step 3 passes, the downstream parser diverges from ST — file the bug against the parser. If step 3 fails too, the test data itself has the issue; fix the data, not the parser.

### Mutate a buffer from a snippet

Snippets exec on a worker thread; `view.run_command(...)` requires ST's main thread and silently no-ops if called directly. Wrap the call in `run_on_main` — it owns the `set_timeout` schedule, the completion signal, and the timeout error path.

```python
v = sublime.active_window().new_file()
run_on_main(lambda: v.run_command("append", {"characters": "hello"}))
print(v.size())  # 5
v.set_scratch(True); v.close()
```

`run_on_main(callable, timeout=2.0)` returns the callable's value; exceptions raised inside the callable propagate to the worker thread (and surface as the snippet's `error`).

### Bulk probes

`scope_name` on an already-tokenised view is thread-safe and runs concurrent with ST's UI, so a several-hundred-row sweep in one `exec_sublime_python` call comfortably fits the 60 s per-call budget. The cold-view cost is a one-time tokenisation pass on the first helper call against a given path.

```python
scopes = [scope_at("/path/to/big_file", row, 0)["scope"] for row in range(3020, 3039)]
_ = scopes  # returns via `result`
```

Measured per-call latency is tracked in #10.

### Filter find_resources output through load_resource

`find_resources` reports whatever ST's resource index says exists, which can lag behind reality. A path like `Packages/C#/Embeddings/Regex (for C#).sublime-syntax` may appear in the listing yet raise `FileNotFoundError` from `sublime.load_resource(...)` when the underlying file is gone (cache survives source). Filter at the call site:

```python
def _safe_load(p):
    try:
        sublime.load_resource(p)
        return True
    except FileNotFoundError:
        return False

candidates = [p for p in find_resources("*.sublime-syntax") if _safe_load(p)]
_ = candidates
```

The filter is not pushed inside `find_resources` itself: silent filtering would mask the underlying ST behaviour and cost a `load_resource` per entry on every listing.

### Probe a large syntax-test file in pieces

`run_syntax_tests` drives the private `sublime_api.run_syntax_test`, which is synchronous. For files with thousands of assertions (e.g. `~14k` on a large grammar's `syntax_test_*` fixture) the runner exceeds the 60 s `EXEC_TIMEOUT_SECONDS` ceiling on `exec_sublime_python` and the call returns with `error: "exec timed out after 60s"` rather than a structured `failed` / `passed` payload. No `timeout` parameter on `run_syntax_tests` rescues this — the ceiling is on the snippet call, not the helper.

When that happens, enumerate failing positions externally and probe each one:

```python
# failing_positions = [(row, col), ...] — produced separately, e.g. by
# syntect's examples/syntest harness against the same file.
results = [scope_at_test("/abs/path/to/syntax_test_huge", r, c) for r, c in failing_positions]
_ = results
```

`scope_at_test` reads the `# SYNTAX TEST` header and assigns the syntax once per call; the loop pays a one-time tokenisation on first call and then runs at the per-`scope_name` rate noted in *Bulk probes* above. Each call is independent of the 60 s budget.

## 5. Output discipline

- **Return raw scopes.** `source.python keyword.control.flow` is the answer — don't paraphrase to "it's a Python keyword in a control-flow context." The caller can read the scope; paraphrase drops information.
- **`summary` before full panels.** For `run_syntax_tests`, the summary is usually enough. Print `output` or iterate `failures` only when the caller needs the specific failed assertions.
- **One question per call.** `exec_sublime_python` captures `print()` line-for-line; don't cram unrelated investigations into one snippet. A probe loop is fine; a second unrelated question is not.
- **Assign structured values to `_`.** If you're returning a dict or list, assign to `_` and let `repr(_)` come back as `result` — less shell-escaping, clearer for the caller than `json.dumps`'ing into `output`.

## 6. Known limitations / tracking

_Last synced with issue state: 2026-05-04._

- **#7** — parameterise the test suite's hardcoded `HEADER` across syntaxes.
- **#8** — concurrency cap on the exec daemon-thread pool.
- **#22** — `resolve_position` `syntax_path` accepts filesystem paths directly (URI flexibility on top of `temp_packages_link`).
- **whole-tree mirror** (follow-up to #24) — `temp_packages_link` covers per-syntax probing, but cross-grammar investigations where one testdata grammar embeds another (e.g. C# embedding RegExp) need the testdata tree to *shadow* ST's built-ins, not coexist with them. Different lifecycle (parent symlink, per-entry shadowing); not yet implemented.
- **#10** — documented per-call latency for bulk probes + daemon-thread / cold-tokenisation clarification.
- **#34** — `find_resources` can list stale `Packages/...` paths whose `load_resource` raises `FileNotFoundError`; documented in §4 ("Filter find_resources output through load_resource").

## 7. Reference — preloaded helpers

- `scope_at(path, row, col) -> dict` — open file, return `{"scope", "resolved_syntax"}`. `resolved_syntax` is `view.syntax().path` (or `None`); compare against the canonical plain-text URI to detect extension-less / no-syntax fallback.
- `scope_at_test(path, row, col) -> dict` — parse `# SYNTAX TEST` header, assign that syntax, return `{"scope", "requested_syntax", "resolved_syntax"}`. `requested_syntax != resolved_syntax` flags silent fallback to the wrong syntax.
- `resolve_position(path, row, col, syntax_path=None) -> dict` — full position disambiguation with `overflow` / `clamped` flags; also carries `requested_syntax` / `resolved_syntax`.
- `run_syntax_tests(path) -> dict` — run ST's built-in syntax-test runner. `{state, summary, output, failures}`. Path must resolve under `sublime.packages_path()` (directly or via a symlink in that directory); paths outside the Packages tree raise.
- `run_inline_syntax_test(content, name) -> dict` — synthetic-probe variant: writes `content` to a managed temp dir under `Packages/User/`, runs the runner, cleans up. Same shape as `run_syntax_tests` plus a `"inconclusive"` state when ST never indexes the temp resource.
- `open_view(path, timeout=5.0) -> View` — open a file, poll `is_loading` and initial tokenisation.
- `assign_syntax_and_wait(view, resource_path, timeout=2.0) -> None` — assign a syntax and wait for the setting to apply + best-effort tokenisation.
- `run_on_main(callable, timeout=2.0)` — schedule `callable` on ST's main thread; return its value (or re-raise its exception). Required wrapper for `view.run_command(...)` and other `TextCommand` mutations.
- `temp_packages_link(filesystem_path) -> str` / `release_packages_link(name) -> None` — synthesise / tear down a per-call `Packages/__sublime_mcp_temp_<nonce>__` symlink for repo-local syntaxes. Returns the synthesised package name; build URIs as `Packages/<name>/<basename>`.
- `find_resources(pattern) -> list[str]` — wrap `sublime.find_resources`.
- `reload_syntax(resource_path) -> None` — force-reload a `.sublime-syntax` resource via view reactivation.

Full signatures, gotchas, and threading guarantees live in `TOOL_DESCRIPTION` (read via `tools/list`). This reference is a cheat-sheet.

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

When ST cannot complete the run, `run_syntax_tests` raises and the cause surfaces in the top-level `error` of the MCP response — `isError` is true. Type split: `TimeoutError` for the no-output-before-deadline case; `RuntimeError` for build-panel missing (tracked as #17), unindexed resources, and unparsable build-panel output. Fall back to the "Scope at a position" recipe (`scope_at` / `scope_at_test`) or "Confirm which syntax ST assigned (and handle repo-local syntaxes)" (`resolve_position`) — these answer the underlying ground-truth question without going through ST's build path.

### Confirm which syntax ST assigned (and handle repo-local syntaxes)

`view.assign_syntax` takes a `Packages/...` resource URI, not an arbitrary filesystem path. The older `view.set_syntax_file` has the same constraint but fails silently when given a filesystem path: `view.settings().get("syntax")` echoes the assigned absolute path, ST surfaces a "file not found" popup, `view.scope_name(...)` returns `text.plain` for every position, and the Python call doesn't raise. Prefer `assign_syntax_and_wait`.

To test a syntax file that lives outside ST's Packages tree (e.g. a syntect `testdata/Packages/...` copy), symlink it in first:

```bash
ln -s /path/to/repo/testdata/Packages/Java \
      "$HOME/Library/Application Support/Sublime Text/Packages/Java"
```

Then pass the `Packages/...` URI to `resolve_position`:

```python
r = resolve_position(
    "/path/to/syntax_test_file", row=71, col=29,
    syntax_path="Packages/Java/Java.sublime-syntax",
)
print(r["scope"], "overflow:", r["overflow"], "clamped:", r["clamped"])
assert r["resolved_syntax"] == r["requested_syntax"], r
```

The returned dict also carries `overflow` (past-EOL request wrapped into a later row), `clamped` (past-EOF, point at `view.size()`) — mutually exclusive flags that surface a quiet `text_point` behaviour; the full semantics are in `TOOL_DESCRIPTION`'s "text_point overflow" section. `requested_syntax` echoes the `syntax_path` argument and `resolved_syntax` is `view.syntax().path` — assert they match before treating `scope` as ground truth, since `view.assign_syntax` accepts any string and silently falls through to Plain Text when the URI doesn't resolve.

The symlink is the workaround for `resolve_position`'s `syntax_path` parameter; beware that ST might resolve to a same-named bundled syntax if the symlink ordering is wrong — `requested_syntax != resolved_syntax` flags this. #22 will let `resolve_position` accept filesystem paths instead of `Packages/...` URIs, but the `ln -s` step itself stays load-bearing — eliminating that requires helper-managed temporary symlinks (#24). `scope_at_test` parses the URI from the file's `SYNTAX TEST` header (conventionally `Packages/...` already) and exposes the same `requested_syntax` / `resolved_syntax` pair. `run_syntax_tests` is unaffected post-PR #16 (`_to_resource_path` walks symlinked entries directly).

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

### Bulk probes

`scope_name` on an already-tokenised view is thread-safe and runs concurrent with ST's UI, so a several-hundred-row sweep in one `exec_sublime_python` call comfortably fits the 60 s per-call budget. The cold-view cost is a one-time tokenisation pass on the first helper call against a given path.

```python
scopes = [scope_at("/path/to/big_file", row, 0)["scope"] for row in range(3020, 3039)]
_ = scopes  # returns via `result`
```

Measured per-call latency is tracked in #10.

## 5. Output discipline

- **Return raw scopes.** `source.python keyword.control.flow` is the answer — don't paraphrase to "it's a Python keyword in a control-flow context." The caller can read the scope; paraphrase drops information.
- **`summary` before full panels.** For `run_syntax_tests`, the summary is usually enough. Print `output` or iterate `failures` only when the caller needs the specific failed assertions.
- **One question per call.** `exec_sublime_python` captures `print()` line-for-line; don't cram unrelated investigations into one snippet. A probe loop is fine; a second unrelated question is not.
- **Assign structured values to `_`.** If you're returning a dict or list, assign to `_` and let `repr(_)` come back as `result` — less shell-escaping, clearer for the caller than `json.dumps`'ing into `output`.

## 6. Known limitations / tracking

_Last synced with issue state: 2026-05-03._

- **#6** — bump `_wait_for_resource` timeout 1s → 2-3s for cold-disk indexing.
- **#7** — parameterise the test suite's hardcoded `HEADER` across syntaxes.
- **#8** — concurrency cap on the exec daemon-thread pool.
- **#22** — `resolve_position` `syntax_path` accepts filesystem paths (URI flexibility only — does not eliminate the `ln -s` step in §4).
- **#24** — helper-managed temporary symlinks for repo-local syntaxes. Lands the `ln -s`-elimination half of #9's body. Once landed, the §4 workaround paragraph (and #22 / #24 entries) become removable.
- **#10** — documented per-call latency for bulk probes + daemon-thread / cold-tokenisation clarification.
- **#30** — `run_inline_syntax_test(content, name)` lifecycle helper. Collapses the write-to-`Packages/User/`-then-poll-then-cleanup dance into one call; pairs with #24 on the on-disk side.
- **#33** — daemon-thread `view.run_command(...)` is a silent no-op without `set_timeout`. Until the doc gotcha or `run_on_main` helper lands, callers mutating buffers from snippet code need to schedule via `set_timeout` and gate on a `threading.Event`.
- **#34** — `find_resources` lists `Packages/...` paths whose `load_resource` raises `FileNotFoundError` (cache-survives-source case observed against `Packages/C#/Embeddings/Regex (for C#).sublime-syntax`); characterise before deciding doc vs code fix.

## 7. Reference — preloaded helpers

- `scope_at(path, row, col) -> dict` — open file, return `{"scope", "resolved_syntax"}`. `resolved_syntax` is `view.syntax().path` (or `None`); compare against the canonical plain-text URI to detect extension-less / no-syntax fallback.
- `scope_at_test(path, row, col) -> dict` — parse `# SYNTAX TEST` header, assign that syntax, return `{"scope", "requested_syntax", "resolved_syntax"}`. `requested_syntax != resolved_syntax` flags silent fallback to the wrong syntax.
- `resolve_position(path, row, col, syntax_path=None) -> dict` — full position disambiguation with `overflow` / `clamped` flags; also carries `requested_syntax` / `resolved_syntax`.
- `run_syntax_tests(path, timeout=30.0) -> dict` — run ST's built-in syntax-test runner. `{state, summary, output, failures}`.
- `open_view(path, timeout=5.0) -> View` — open a file, poll `is_loading` and initial tokenisation.
- `assign_syntax_and_wait(view, resource_path, timeout=2.0) -> None` — assign a syntax and wait for the setting to apply + best-effort tokenisation.
- `find_resources(pattern) -> list[str]` — wrap `sublime.find_resources`.
- `reload_syntax(resource_path) -> None` — force-reload a `.sublime-syntax` resource via view reactivation.

Full signatures, gotchas, and threading guarantees live in `TOOL_DESCRIPTION` (read via `tools/list`). This reference is a cheat-sheet.

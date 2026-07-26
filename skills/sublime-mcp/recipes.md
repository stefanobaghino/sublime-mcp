# sublime-mcp recipes

Companion to `SKILL.md` — read the recipe that matches the task at hand. Tool contracts are in SKILL.md §3, output discipline in SKILL.md §5.

Each recipe is one `exec_sublime_python` call. Rows and columns are **0-indexed** — a test-file assertion on line 181 col 9 is `row=180, col=8`. Paths shown are container-side; the user typically mounts their working tree at `/work`.

**Host-side file-write tools.** If you're driving this skill from an agent harness with its own host-side write tool (Claude Code's `Write`, Cursor's edit tool, anything similar), don't pre-write probe files to host paths and then pass those paths into `exec_sublime_python` helpers. The container only sees paths under `--mount` directories (typically `/work`) plus its own `/tmp`; anything else is invisible regardless of how the path looks on the host. The failure shape is a hang or indexer-budget timeout, not a clean `FileNotFoundError`. Write probe files inside the snippet instead — see *Probe a synthetic case inline* and *Probe a synthetic syntax against a synthetic input* below.

### Recover from a wedged main thread

When `health_check` returns `main_thread_responsive: false`, walk this escalation rather than retrying main-thread snippets (every retry burns another 60s on the wedged path). The flow goes from cheapest signal to most disruptive recovery; stop at the first step that puts main back.

1. **`inspect_environment`** to triage. Read `http_server_listening`, `display_reachable`, and `x_windows`:
   - `http_server_listening: false` → plugin host is dead. Skip to step 4.
   - `http_server_listening: true` and `x_windows` lists an unexpected window (anything not the ST editor) → likely an invisible dialog blocking main. Try step 2.
   - `http_server_listening: true` and `x_windows` looks normal → wedge isn't dialog-shaped. Skip to step 4.
   - `display_reachable: false` → Xvfb is gone; restart can't help. Skip to step 5.

2. **Soft recovery via `xdotool`.** From an `exec_sublime_python` snippet, dismiss the dialog and check whether main came back:

   ```python
   import subprocess
   r = subprocess.run(
       ["xdotool", "key", "--clearmodifiers", "Escape"],
       capture_output=True, text=True, timeout=5,
   )
   print(r.returncode, r.stderr[:200])
   ```

   Then call `health_check` again. If `main_thread_responsive: true`, you're done — re-issue the probe that timed out. If still wedged, try `xdotool key Return` (some dialogs only accept the default action), then re-check.

3. **`xkill`-style escalation** is rarely worth the round-trip — if Escape and Return don't dismiss, jump to step 4 instead.

4. **`restart_st`** for hard recovery. Returns within ~30s with `success: true` and a fresh `plugin_host_pid_after`. After success, `health_check` should return `main_thread_responsive: true` immediately. Re-issue the original probe. Open files, scratch buffers, and the in-memory `_TEMP_LINKS` registry are gone — by design.

5. **`docker kill <cid>` (final fallback).** When `restart_st` returns `success: false` (process unkillable, plugin HTTP never re-bound, etc.), surface the `container_id` (from any prior response) and ask the user to `docker kill <cid>`. They re-trigger `/mcp` (re-open, not just reconnect — see SKILL.md §3.2 stale-transport note); the harness shim spawns a fresh container.

Don't skip step 1 — guessing at the cause without `inspect_environment` wastes turns: a soft-recovery attempt against a dead plugin host doesn't help, and a `restart_st` against a working plugin host whose only problem is a dialog is unnecessarily disruptive.

### Scope at a position

```python
r = scope_at("/work/Packages/C#/tests/syntax_test_Generics.cs", 180, 8)
print(r["scope"], "via", r["resolved_syntax"])
```

`scope_at` returns `{"scope": str, "resolved_syntax": str | None}`. `resolved_syntax` is the URI ST actually loaded (`view.syntax().path`) — `None` when no syntax resolved, `"Packages/Text/Plain text.tmLanguage"` when ST defaulted to Plain Text. Branch on `resolved_syntax` to detect silent fallback before treating `scope` as ground truth.

**Landmine: extension-less syntax-test files** (`syntax_test_git_config`, no suffix) silently fall back to Plain Text via `scope_at` — `scope == "text.plain"` and `resolved_syntax == "Packages/Text/Plain text.tmLanguage"`. Use `scope_at_test` — it parses the `# SYNTAX TEST "Packages/..."` header and assigns that syntax before sampling.

```python
r = scope_at_test("/work/syntax_test_git_config", 71, 28)
print(r["scope"])
```

The header parser is comment-token-agnostic — it accepts `#`, `//`, `<!--`, `;`, `--`, `|`, etc. Markdown's pipe-comment header works the same way:

```python
r = scope_at_test("/work/syntax_test_markdown.md", 12, 4)
print(r["scope"])
```

### Run syntax tests against a file

```python
r = run_syntax_tests("/work/Packages/C#/tests/syntax_test_Generics.cs")
print(r["summary"])
for msg in r["failures"]:
    print(msg)
```

Branch on `state` for the assertion-run outcome:

| `state`     | meaning                                                                          | `summary` shape                                  | `failures` / `failures_structured` |
| ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------ | ---------------------------------- |
| `"passed"`  | runner completed; every assertion matched                                        | assertion-count headline                         | `[]` / `[]`                        |
| `"failed"`  | runner completed; some assertions did not match — read `failures` for specifics  | `"FAILED: N of M assertions failed"`             | populated                          |

`failures_structured[i]` is the parsed peer of `failures[i]` — `{file, row, col, error_label, expected_selector, actual: [{col_range, scope_chain}, ...]}`. The parser is best-effort: on an unexpected line shape any field can be `None` / empty and `failures[i]` remains the canonical record.

When ST cannot complete the run, `run_syntax_tests` raises `RuntimeError` and the cause surfaces in the top-level `error` of the MCP response — `isError` is true. The reachable causes are: resource not yet indexed, path outside `sublime.packages_path()` (symlink it in first — see "Confirm which syntax ST assigned (and handle repo-local syntaxes)" below), and the private `sublime_api.run_syntax_test` missing on this ST build. For ground-truth questions that don't need the assertion runner, fall back to `scope_at` / `scope_at_test` or `resolve_position`.

The `^` alignment rule that defines what each assertion line targets is documented under *Probe a synthetic case inline* below.

### Read the scope chain via the runner's failure diagnostic

When ST is headless (no window), `scope_at` / `scope_at_test` / `resolve_position` raise `RuntimeError`. The runner-driven equivalent: author a syntax test asserting against a guaranteed-failing selector at the position of interest; the runner's failure diagnostic carries the live scope chain at every column the assertion covers.

```python
r = run_inline_syntax_test(
    '# SYNTAX TEST "Packages/Python/Python.sublime-syntax"\n'
    'def foo(): pass\n'
    '#^^^ probe.scope.never\n',
    "syntax_test_scope_probe",
)
chain = r["failures_structured"][0]["actual"][0]["scope_chain"]
# -> "source.python meta.function.python keyword.declaration.function.python …"
```

Each `^` on the assertion line tests the column it sits in on the content line directly above — see *Probe a synthetic case inline* below for the alignment rule. `failures_structured[i].actual[j]` is `{col_range, scope_chain}`; the chain is ST's full hierarchical scope at that column, identical to what `scope_at` would return windowed (modulo trailing whitespace, which the parser preserves verbatim).

When the syntax under test is also synthetic, pair this with `temp_packages_link` exactly as the *Probe a synthetic syntax against a synthetic input* recipe below does — the runner reads through the link the same way `resolve_position` does.

Use this when ST is headless, or when `assign_syntax_and_wait` is racing the indexer (the runner doesn't go through `assign_syntax`); prefer `scope_at` / `scope_at_test` / `resolve_position` when a window is available — they accept any column directly without `^`-alignment constraints.

### Probe a synthetic case inline

For "what does ST do on this case?" probes against a syntax that's *already reachable to ST* — bundled, or linked into `Packages/` via `temp_packages_link` — `run_inline_syntax_test(content, name)` owns the file-write, indexing wait, runner call, and cleanup. The header inside `content` selects the syntax under test.

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

**Assertion-line `^` alignment.** Each `^` in an assertion line tests the column it sits in on the assertion line — the same column on the content line directly above. The leading columns are taken up by the comment marker (`#` ⇒ col 0 unreachable; `//` ⇒ cols 0–1 unreachable, with the conventional trailing space pushing the testable region to col 3+). Probes targeting those leading columns of the content line cannot be expressed through `^`. Pad the content with leading spaces if you need to test the leading region, or prefer the single-char `# SYNTAX TEST` header that maximises the reachable range. For "scope at point" probes that don't need assertion-runner output, prefer `scope_at` / `scope_at_test` / `resolve_position` — they accept any column directly.

This helper writes only the *test file*. When the syntax under test is also synthetic, pair `temp_packages_link` (own the syntax) with `resolve_position` / `scope_at` (sample the input) — see the next recipe.

### Probe a synthetic syntax against a synthetic input

When *both* the syntax and the input it's probed against are synthetic — "I just authored this syntax in `/tmp`; what scope does ST assign at row R col C of this synthetic input string?" — neither `run_inline_syntax_test` (test-file only) nor the existing `temp_packages_link` recipe (existing input file) covers it on its own. Compose them: `temp_packages_link(dir)` to own the syntax, write the input under any path, sweep `resolve_position` for scope-at-point.

```python
# /tmp/probe/Foo.sublime-syntax and /tmp/probe/test.foo already written.
input_text = "AB"
name = temp_packages_link("/tmp/probe")          # directory form: links the dir directly
syntax_uri = "Packages/%s/Foo.sublime-syntax" % name
try:
    chains = []
    for c in range(len(input_text)):
        r = resolve_position("/tmp/probe/test.foo", 0, c, syntax_path=syntax_uri)
        assert r["resolved_syntax"] == r["requested_syntax"], r
        chains.append(r["scope"])
finally:
    release_packages_link(name)
_ = chains
```

`resolve_position` over `scope_at` here: it surfaces `requested_syntax` / `resolved_syntax`, so a typo in the synthetic syntax that makes ST silently fall back to Plain Text trips the assertion instead of returning misleading scopes. The input file does not need to live under the symlinked dir — `resolve_position` opens any filesystem path. Co-locating it next to the syntax (as above) is a cleanup convention, not a requirement; the link only exists so ST can resolve the synthetic syntax.

**Trap when assembling the YAML inline.** `.sublime-syntax` files start with the directive `%YAML 1.2`. Do not build the file body via Python `%`-formatting — `"""%YAML 1.2\n…""" % var` raises `ValueError: unsupported format character 'Y' (0x59) at index 1` because Python parses `%Y` as an attempted format spec. Use f-strings, `str.format`, or plain string concatenation; only `%`-formatting trips the trap.

For iterating one-rule variants of the same syntax, overwrite `Foo.sublime-syntax` under the link between sweeps and call `reload_syntax(syntax_uri)` to force ST to reparse — cheaper than tearing down and re-linking.

When ST is headless, `resolve_position` raises — use the *Read the scope chain via the runner's failure diagnostic* recipe above to sweep scopes against synthetic syntaxes without a window. Pair it with the same `temp_packages_link` setup this recipe uses; the runner reads through the link the same way `resolve_position` does.

#### Cross-syntax / multi-syntax probes

The recipe above works for single-syntax probes because `view.assign_syntax(URI)` resolves the linked syntax through ST's resource indexer. **Cross-syntax references inside the linked syntax — `push: scope:source.X`, `set: scope:...`, `embed: scope:...`, `include: scope:...`, file-path forms of all four — silently fall back to Plain Text under `temp_packages_link`.** ST resolves those through a parse-table builder that doesn't pick up linked syntaxes the way the resource indexer and direct URI assignment do; every position inside the embedded region tokenises as `text.plain` (the Plain Text syntax's `meta_scope`) regardless of the guest's contributions, while the host's scopes everywhere else look correct — the result *appears* coherent, so the existing `requested == resolved` invariant doesn't trip. **`extends:` is path-based but resolved at load time through ST's resource lookup; it is not affected by this gap and works under `temp_packages_link`.**

Workaround: own a managed `Packages/User/__sublime_mcp_user_<prefix>_<nonce>__/` directory via `temp_user_packages_dir`, write the syntaxes into it, then use `wait_for_scope` to gate on each guest scope surfacing in `sublime.find_syntax_by_scope`. The `Packages/User/<subdir>/` ingest path *does* feed ST's cross-syntax resolver, so `push:` / `set:` / `embed:` / `include:` against a guest scope resolve correctly. The basename-only `wait_for_resource` gate is insufficient here — the scope registry is a separate ingest from the resource indexer, so use the scope-registry helper. Note that `Packages/User/<subdir>/` itself can fail to register intermittently — `wait_for_scope`'s timeout is the right backstop.

```python
# Cross-syntax recipe: managed dir under Packages/User/, gate on the registry.
import os
base = temp_user_packages_dir("xsyn")  # /…/Packages/User/__sublime_mcp_user_xsyn_<nonce>__
try:
    with open(os.path.join(base, "Host.sublime-syntax"), "w") as f:
        f.write(host_yaml)            # contains `push: scope:source.guest`
    with open(os.path.join(base, "Guest.sublime-syntax"), "w") as f:
        f.write(guest_yaml)           # `scope: source.guest`
    assert wait_for_scope(["source.host", "source.guest"]), "guests never registered"
    # now resolve_position / probe_scopes against the host see the guest's scopes.
finally:
    release_user_packages_dir(base)
```

`temp_user_packages_dir(prefix="probe", …)` productizes the workaround's lifecycle: nonce'd dir name, cross-call sweep of SIGKILL-orphaned dirs older than 60 s, `release_user_packages_dir(path)` for explicit teardown with structural refusal of non-managed paths. `wait_for_scope(scope, timeout=3.0)` accepts a single scope or an iterable — iterable form succeeds only when every scope surfaces, matching the host+guest shape above. `sublime.find_syntax_by_scope(scope)` itself returns `list[Syntax]` (typically empty or single-element), not a single `Syntax`; `wait_for_scope` bakes the truthy-context handling in.

### Confirm which syntax ST assigned (and handle repo-local syntaxes)

`view.assign_syntax` takes a `Packages/...` resource URI, not an arbitrary filesystem path. The older `view.set_syntax_file` has the same constraint but fails silently when given a filesystem path: `view.settings().get("syntax")` echoes the assigned absolute path, ST surfaces a "file not found" popup, `view.scope_name(...)` returns `text.plain` for every position, and the Python call doesn't raise. Prefer `assign_syntax_and_wait`.

For a syntax file that lives outside ST's Packages tree (e.g. a syntect `testdata/Packages/...` copy mounted at `/work/testdata/...`), use `temp_packages_link` to manage a per-call symlink. The helper synthesises `Packages/__sublime_mcp_temp_<nonce>__`, waits for ST's resource indexer to surface the sentinel, and returns the synthesised package name. Pass the syntax's filesystem path directly to `resolve_position` / `assign_syntax_and_wait` — the helpers reverse-map filesystem inputs through any symlink in `sublime.packages_path()` to the matching `Packages/...` URI. (Constructing the URI by hand as `"Packages/%s/Java.sublime-syntax" % name` still works.) The caller tears down via `release_packages_link`.

```python
syntax_path = "/work/testdata/Packages/Java/Java.sublime-syntax"
name = temp_packages_link(syntax_path)
try:
    r = resolve_position(
        "/work/syntax_test_file", row=71, col=29,
        syntax_path=syntax_path,
    )
    print(r["scope"], "overflow:", r["overflow"], "clamped:", r["clamped"])
    assert r["resolved_syntax"] == r["requested_syntax"], r
finally:
    release_packages_link(name)
```

The returned dict also carries `overflow` (past-EOL request wrapped into a later row), `clamped` (past-EOF, point at `view.size()`) — mutually exclusive flags that surface a quiet `text_point` behaviour; the full semantics are in `TOOL_DESCRIPTION`'s "text_point overflow" section. `requested_syntax` echoes the `syntax_path` argument and `resolved_syntax` is `view.syntax().path` — assert they match before treating `scope` as ground truth, since `view.assign_syntax` accepts any string and silently falls through to Plain Text when the URI doesn't resolve.

`temp_packages_link` synthesises a unique nonce-named package, so the bundled `Packages/Java` continues to load alongside it — `requested_syntax != resolved_syntax` still flags any silent fallback to a built-in. The per-syntax mode is sufficient for synthetic probes and single-grammar regression triage; cross-grammar investigations where the testdata grammar embeds another testdata grammar (e.g. C# embedding RegExp) need a whole-tree mirror that shadows the built-ins, tracked separately in SKILL.md §6.

This recipe only works because the syntax is consumed via direct URI assignment. If the linked syntax contains any cross-syntax reference (`push:` / `set:` / `embed:` / `include:` against a `scope:source.X` or a file-path target), ST silently falls back to Plain Text inside the embedded region — use the *Cross-syntax / multi-syntax probes* recipe above (`temp_user_packages_dir` + `wait_for_scope`) instead.

When a caller writes additional `.sublime-syntax` files into the already-linked dir between snippets — incremental probing — wait for them to surface via `wait_for_resource("MyProbe*.sublime-syntax")` from a follow-up snippet, *not* an in-snippet `find_resources` poll. An in-snippet poll that overruns `EXEC_TIMEOUT_SECONDS` is killed at the transport, but the main-thread state it touched can leave ST wedged for the rest of the session.

`scope_at_test` parses the URI from the file's `SYNTAX TEST` header (conventionally `Packages/...` already) and exposes the same `requested_syntax` / `resolved_syntax` pair without needing a symlink. `run_syntax_tests` accepts any path under `sublime.packages_path()` (directly or via symlink); pair it with `temp_packages_link` to cover paths outside the Packages tree.

### Compare a parser's output against ST

Three-step divergence triage:

```python
# 1. What does ST report at the failing position?
r = scope_at_test("/work/syntax_test_git_config", 71, 28)
print(r["scope"], "via", r["resolved_syntax"])

# 2. Did both engines sample the same point? (past-EOL divergence is common)
r = resolve_position(
    "/work/syntax_test_git_config", 71, 29,
    syntax_path="Packages/Git Formats/Git Config.sublime-syntax",
)
print("overflow:", r["overflow"], "clamped:", r["clamped"], "actual:", r["actual"])

# 3. Does ST's own runner agree?
r = run_syntax_tests("/work/syntax_test_git_config")
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

For the common case of synthesising a buffer purely to sweep scopes, prefer `probe_scopes(content, syntax_path=..., syntax_yaml=...)` — it bundles the lifecycle (open / assign / append / size-poll / sweep / close) and the optional synthetic-syntax cleanup, so the recipe above is only needed when the probe shape doesn't fit `probe_scopes` (e.g. incremental edits across multiple runs).

`probe_scopes`'s `scopes` dict is mode-dependent: integer keys via `result` (the canonical channel — see SKILL.md §5 "Assign structured values to `_`"), string keys via JSON `output`. Index `r["scopes"][position]` with an `int` if you read via `_` / `result`; cast back with `int(k)` if you parsed `output` through JSON. Picking the canonical channel avoids the defensive `r["scopes"].get(str(i), r["scopes"].get(i))` boilerplate.

### Bulk probes

A `view.scope_name(point)` call on an already-tokenised view costs around 150 µs (measured: 5 × 500-sample medians on a 1.2k-line Python source view, ST 4200 stable). It's also thread-safe and runs concurrent with ST's UI, so a several-hundred-row sweep in one `exec_sublime_python` call comfortably fits the 60 s per-call budget — three orders of magnitude of headroom. The cold-view cost is a one-time tokenisation pass on the first helper call against a given path.

```python
scopes = [scope_at("/work/big_file", row, 0)["scope"] for row in range(3020, 3039)]
_ = scopes  # returns via `result`
```

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
results = [scope_at_test("/work/syntax_test_huge", r, c) for r, c in failing_positions]
_ = results
```

`scope_at_test` reads the `# SYNTAX TEST` header and assigns the syntax once per call; the loop pays a one-time tokenisation on first call and then runs at the per-`scope_name` rate noted in *Bulk probes* above. Each call is independent of the 60 s budget.

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

This skill drives the `sublime-mcp` server to get authoritative answers from Sublime Text itself — what scope it assigns at a point, which `.sublime-syntax` it resolved, whether an assertion file passes ST's built-in runner — via one tool, `exec_sublime_python`, which runs Python inside ST's plugin host.

**Transport.** The server is a stdio MCP shim (`sublime-mcp`) that runs Sublime Text inside a Docker container. The shim execs into `docker run -i --rm`; the in-container `bridge.py` proxies JSON-RPC stdio↔HTTP to the plugin's loopback HTTP server. One agent session, one container; `dockerd` reaps the container when the parent docker CLI dies. The agent never sees Docker — only the `mcp__sublime-text__exec_sublime_python` tool.

## 1. Preflight — check before driving the tool

If `mcp__sublime-text__exec_sublime_python` appears anywhere in your tool surface — either listed in the deferred-tools system-reminder or already resolved in your toolbox — skip to §2.

If it's missing, diagnose with:

```bash
claude mcp list | grep sublime-text
docker ps --format '{{.ID}} {{.Image}} {{.Status}}' | grep sublime-mcp
```

Expected: `claude mcp list` shows `sublime-text ✓ Connected`; `docker ps` shows one running `sublime-mcp:local` container per active agent session. If the registration is missing or shows ✗, point the user at `install.md` in this skill's directory. If the registration is healthy but the container is missing, the bridge is failing to come up — read the MCP server stderr (Claude Code surfaces it in the connection log; `claude mcp logs sublime-text` if available).

Common boot-time failures the bridge signals on stderr (look for `ERROR  [bridge]`):

- `docker: command not found` / `Cannot connect to the Docker daemon` — install Docker and ensure the daemon is running.
- `Sublime Text never opened a window` — Xvfb or licensing issue inside the container; run `docker run --rm -it sublime-mcp:local` manually and inspect `/var/log/sublime.log` and `/var/log/xvfb.log`.
- `docker build` failure during shim startup — `cd` into the checkout and run `docker build -t sublime-mcp:local .` directly to see the full output.

Steady-state failures (timeout, hang, surprising scope) have their own diagnostic surface — see §1.1 below.

Do not attempt to fall back to manual ST UI inspection without first telling the user the skill cannot run.

## 1.1 Reading the log stream

The bridge emits a single stderr stream that the parent docker CLI forwards to Claude Code's MCP server log. Lines are formatted as:

```
2026-05-05T14:22:08.118  DEBUG    [bridge]  req=42  forwarding method=tools/call bytes=1284
2026-05-05T14:22:08.119  INFO     [bridge]  req=42  worker entered
2026-05-05T14:22:08.120  INFO     [bridge]  req=42  snippet exec begin code_bytes=312
2026-05-05T14:22:08.123  INFO     [bridge]  req=42  snippet exec done error=no output_bytes=0
2026-05-05T14:22:08.124  DEBUG    [bridge]  req=42  received status=200 bytes=189
```

Columns: `<wall-clock ISO-8601>`  `<LEVEL>`  `<[component]>`  `req=<JSON-RPC id>`  `<message>`. The bridge logs as `[bridge]`. The plugin running inside ST's plugin host also writes to stderr; those lines reach `docker logs <cid>` (not the live stream — ST self-daemonizes and severs stdio from PID 1).

**Read channels.** Live (bridge proxy events): `claude mcp logs sublime-text` if your build of Claude Code surfaces it, otherwise whatever MCP-server stderr surface the host platform exposes. Historical / plugin-host events: `docker logs <cid>` — captures ST's own stdout/stderr (plugin tracebacks, `package_control` chatter, anything ST writes to its inherited streams). Expect it to be sparse on a healthy run because ST detaches early.

**Levels.**

- `ERROR` — a request will fail to return useful data. Worker timeout always fires a `faulthandler.dump_traceback(all_threads=True)` on the same line for every Python thread's stack.
- `WARNING` — silent-fallback shapes the caller is likely to misinterpret (`requested_syntax != resolved_syntax`, `run_on_main` 2 s timeout fires before the worker's 60 s ceiling, `assign_syntax_and_wait` stage-1 timeout).
- `INFO` — boundary events: container boot/ready/shutdown, sweep removals, **and** `worker entered` / `snippet exec begin` / `snippet exec done` per call. Default level — sufficient for #73-class diagnosis without DEBUG firehose.
- `DEBUG` — proxy-loop trail (`forwarding`/`received`), helper-entry traces (`assign_syntax_and_wait` etc.), `_compile_snippet` auto-lift branch.

**Troubleshooting workflow.**

1. **Observe** the failure (timeout, error response, surprising scope).
2. **Read backward** in the live MCP log to see the INFO trail of `[bridge]` events leading up to the failure. Grep for the `req=<id>` of the failing request to isolate its path through the bridge. Use `docker logs <cid>` for plugin-host tracebacks and ST's own output.
3. **If the INFO trail isn't enough**, bump the in-process plugin logger to DEBUG live — no restart needed: drive `exec_sublime_python` with `import logging; logging.getLogger("sublime_mcp.bridge").setLevel(logging.DEBUG)` and reproduce. Only works while the plugin is responsive (i.e. before a wedge); during an active wedge, bumping the level is moot — the diagnostic information is in the `faulthandler` dump that already fired at ERROR.
4. **For the bridge process itself**, set `SUBLIME_MCP_LOG_LEVEL=DEBUG` in the agent's environment before connecting; the shim forwards it via `-e` to the container.

**Common patterns.**

| Symptom (in `error` field) | Trail shape | Likely cause |
|----------------------------|-------------|--------------|
| `exec timed out after 60.0s` | no preceding `[bridge] worker entered` | bridge couldn't dispatch the worker (rare; check for plugin host crash). |
| `exec timed out after 60.0s` | `[bridge] worker entered`, `[bridge] snippet exec begin`, no `[bridge] snippet exec done`, ends in `[bridge] ERROR worker did not complete in 60.0s; worker thread is_alive=True` plus a multi-line `faulthandler` traceback | snippet wedged on ST's main thread (canonical #73). The `faulthandler` dump pinpoints the thread waiting on `run_on_main` or similar. |
| `plugin HTTP error: ...` | no preceding `docker logs` traceback | container died (likely OOM / SIGKILL). Check `docker ps`. |
| Plugin-host Python traceback in `docker logs` with no further `[bridge]` lines | bridge thread crashed on an uncaught plugin-host exception | restart the agent session; consider filing the traceback as a bridge bug. |

**Surfacing to the user.** Don't dump the whole trail — pull the ~30 lines around the failure boundary and grep for the failing `req=<id>`. The user's session already has the bridge stderr; you're highlighting the relevant slice.

## 1.2 Capturing ST's own console output (don't bother)

When ST silently rejects a `.sublime-syntax` (parse-table-build failure — see #78), it writes the parse error to its console panel. That panel is *not* reachable from inside `exec_sublime_python` via any obvious path — every approach below has been verified empty in dogfooding sessions:

- `window.find_output_panel("console")` returns `None` even when `window.panels()` lists `'console'`. `create_output_panel("console")` produces an empty View (size 0). Variants `'output.console'`, `'exec'`, `'Console'` are all empty.
- Redirecting `sys.stdout` / `sys.stderr` over the parse-table-build window captures zero bytes.
- `os.dup2(2, …)` over the same window also captures zero bytes.

ST's loader writes its diagnostics through a path none of these reach. Don't burn a turn going through the standard-panel-API door. If your probe needs to know *why* a syntax was rejected, fall back to differential structural probing (write a known-good control alongside the suspect form, compare which one ST resolves) and surface the observation as "rejected at some layer beyond YAML parse" without naming the layer. See #79 for the open question on a usable capture path.

## 2. Decide whether this skill is the right call

Reach for this skill when the question is "what does Sublime Text do / see / say at this point?" and the alternative is guessing, paraphrasing from memory, or asking the user to click through ST's UI.

- **Use this skill** for: scope at a specific row/col; whether ST's built-in syntax-test runner passes an assertion file; which `.sublime-syntax` ST resolved for a given path (bundled vs repo-local); any comparison where ST is the reference implementation for a downstream parser (e.g. syntect).
- **Recommend `Read` / `Grep` instead** when the answer is in source — `.sublime-syntax` authoring, `.tmLanguage` conversion, plugin API lookup from docstrings.
- **Not this skill** for Sublime Text UI automation, keybinding tests, or packaging questions. Hand back to the user.

If borderline, say which way you're leaning in one sentence, then proceed.

## 3. The tools and their contracts

### 3.1 exec_sublime_python

`mcp__sublime-text__exec_sublime_python({ code, timeout_seconds? })` runs `code` on a dedicated daemon thread inside the containerised ST's plugin host (Python 3.8) and returns:

```json
{ "output": "<captured print()>", "result": "<repr(_) or null>", "error": "<traceback or null>", "st_version": 4200, "st_channel": "stable", "container_id": "<docker cid>", "workspace_path": "/work", "isError": false }
```

- A trailing bare expression is auto-lifted into `_`, or assign to `_` explicitly at top level. Either way, `repr(_)` is returned as `result`.
- `error` is populated on uncaught exception; `isError` is derived from `error is not None`. Helper failures (e.g. `run_syntax_tests` cannot complete the run) raise and surface in this same `error` field — there is no separate helper-level error channel.
- `st_version` (int) and `st_channel` (str, e.g. `"stable"` / `"dev"`) echo the running ST build on every response. Use these to detect channel mismatches when probing grammars whose CI gates on a non-stable channel.
- `container_id` is the Docker short cid of the container handling the call. When recovery requires `docker kill` / `docker exec`, use this field rather than `docker ps -q` (which lists *every* container — multiple Claude Code sessions can run concurrently).
- `workspace_path` is the in-container mount root paths resolve against — always `/work` when the user followed the install instructions. Treat it as the contract anchor: every path argument you pass to `scope_at` / `run_syntax_tests` / `open_view` is interpreted against this root.
- Optional `timeout_seconds` (clamped to `[0.1, 60.0]`) lowers the 60 s ceiling for a single call. On expiry the response carries `error: "snippet exceeded the per-call timeout of <X>s"`, distinct from the transport-ceiling `error: "exec timed out after 60.0s"`. Use it for adversarial probes where a hang is the probe's answer ("does ST loop on this regex?") so the round-trip cost is the override budget rather than the full 60 s.
- `run_syntax_tests(...)["state"]` reports the assertion-run outcome (`passed` / `failed`). `failures` is ST's raw multi-line diagnostic per assertion; `failures_structured` is the same list parsed into `{file, row, col, error_label, expected_selector, actual}` dicts for programmatic consumers (best-effort; `failures` remains canonical on parser miss).
- Preloaded helpers (`scope_at`, `scope_at_test`, `resolve_position`, `run_syntax_tests`, `probe_scopes`, `open_view`, `assign_syntax_and_wait`, `find_resources`, `wait_for_resource`, `wait_for_scope`, `reload_syntax`) are in scope without import.

The helpers split into two families. **View-driving** helpers (`scope_at`, `scope_at_test`, `resolve_position`, `probe_scopes`, `open_view`, `assign_syntax_and_wait`) require a window — they raise `RuntimeError` in headless ST. **Runner-driving** helpers (`run_syntax_tests`, `run_inline_syntax_test`) and resource queries (`find_resources`, `wait_for_resource`, `reload_syntax`, `temp_packages_link` / `release_packages_link`) work fine headless. When `len(sublime.windows()) == 0`, runner-driving experiments still proceed; only view-driving snippets need the user to open a window first (`open -a "Sublime Text"` on macOS).

For the full helper surface, threading guarantees, and the authoritative `text_point` overflow semantics, read the tool's own `description` via `tools/list`. If this skill contradicts it, `tools/list` is right.

**Paths are container-side.** Every path you pass into `exec_sublime_python` (to `scope_at`, `run_syntax_tests`, etc.) is resolved inside the container, not on the host. The user mounts host directories into the container at registration time; the recommended mount is `--mount $PWD:/work` so a host `~/Projects/foo/syntax_test_x.cs` becomes `/work/foo/syntax_test_x.cs` in calls. If a path you'd expect to resolve raises `FileNotFoundError`, check the user's mount before retrying; ask them rather than guessing the host-to-container mapping. If the call hangs or times out instead of raising, the host-side-write footgun is the likely cause — same root, different shape; see §4. `/tmp` is per-container scratch — safe to write synthetic syntax/input files into when the user's working tree shouldn't be touched.

### 3.2 health_check

`mcp__sublime-text__health_check({})` is a worker-thread-only probe that detects when ST's main thread is wedged. It returns within ~2.5s regardless of main-thread state and never goes near the 60s `exec_sublime_python` ceiling. Response shape:

```json
{ "main_thread_responsive": true, "main_thread_probe_elapsed_s": 0.01, "plugin_host_pid": 2060, "uptime_s": 142, "container_id": "<docker cid>", "workspace_path": "/work", "st_version": 4200, "st_channel": "stable" }
```

**Call pattern.** When an `exec_sublime_python` call times out at 60s on something that touched the main thread (`scope_at`, `find_resources`, `open_file`, `assign_syntax_and_wait`, anything wrapped in `run_on_main`), call `health_check` *before* the next main-thread snippet. If `main_thread_responsive` is `false`, stop issuing main-thread snippets — every one will burn another 60s. Ask the user to restart the container; do not retry. If `main_thread_responsive` is `true`, the previous timeout was about that specific snippet, not a session-wide wedge — retrying is fine.

**`/mcp` reconnect does not clear a wedged main thread.** Per #73 (2026-05-05): the slash-command reports `Reconnected to sublime-text.` and re-establishes the MCP transport, but the underlying ST process keeps running with the same wedged main thread — the next `set_timeout(callback, 0); event.wait(...)` still returns False. Recovery requires a true container restart (use the `container_id` from a previous response: `docker kill <cid>`), then re-connect. Don't read "Reconnected" as "wedge cleared."

**`/mcp` reconnect can also land on stale transport.** A `Reconnected to sublime-text.` message does not guarantee the transport has re-bound to the new container. If the next call returns `ConnectionRefusedError(61)`, the transport is still pointed at the previous container's stdio — dismiss `/mcp` and re-open (not just re-trigger) to force a re-bind. Independent of the wedge surface above: the stale-transport shape fires whenever the previous container went away (wedge recovery via `docker kill`, container OOM, container restart) and Claude Code's reconnect attempt landed before the new container was discoverable (#104). Guard pattern: after `docker kill` + reconnect, fire a single `health_check`; on `ConnectionRefusedError`, the user needs to re-open `/mcp` rather than the agent retrying.

## 4. Recipes

Each recipe is one `exec_sublime_python` call. Rows and columns are **0-indexed** — a test-file assertion on line 181 col 9 is `row=180, col=8`. Paths shown are container-side; the user typically mounts their working tree at `/work`.

**Host-side file-write tools.** If you're driving this skill from an agent harness with its own host-side write tool (Claude Code's `Write`, Cursor's edit tool, anything similar), don't pre-write probe files to host paths and then pass those paths into `exec_sublime_python` helpers. The container only sees paths under `--mount` directories (typically `/work`) plus its own `/tmp`; anything else is invisible regardless of how the path looks on the host. The failure shape is a hang or indexer-budget timeout (the #67 / #73 surfaces), not a clean `FileNotFoundError`. Write probe files inside the snippet instead — see *Probe a synthetic case inline* and *Probe a synthetic syntax against a synthetic input* below.

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

**Trap when assembling the YAML inline (#116).** `.sublime-syntax` files start with the directive `%YAML 1.2`. Do not build the file body via Python `%`-formatting — `"""%YAML 1.2\n…""" % var` raises `ValueError: unsupported format character 'Y' (0x59) at index 1` because Python parses `%Y` as an attempted format spec. Use f-strings, `str.format`, or plain string concatenation; only `%`-formatting trips the trap.

For iterating one-rule variants of the same syntax, overwrite `Foo.sublime-syntax` under the link between sweeps and call `reload_syntax(syntax_uri)` to force ST to reparse — cheaper than tearing down and re-linking.

When ST is headless, `resolve_position` raises — use the *Read the scope chain via the runner's failure diagnostic* recipe above to sweep scopes against synthetic syntaxes without a window. Pair it with the same `temp_packages_link` setup this recipe uses; the runner reads through the link the same way `resolve_position` does.

#### Cross-syntax / multi-syntax probes

The recipe above works for single-syntax probes because `view.assign_syntax(URI)` resolves the linked syntax through ST's resource indexer. **Cross-syntax references inside the linked syntax — `push: scope:source.X`, `set: scope:...`, `embed: scope:...`, `include: scope:...`, file-path forms of all four — silently fall back to Plain Text under `temp_packages_link` (#108).** ST resolves those through a parse-table builder that doesn't pick up linked syntaxes the way the resource indexer and direct URI assignment do; every position inside the embedded region tokenises as `text.plain` (the Plain Text syntax's `meta_scope`) regardless of the guest's contributions, while the host's scopes everywhere else look correct — the result *appears* coherent, so the existing `requested == resolved` invariant doesn't trip. **`extends:` is path-based but resolved at load time through ST's resource lookup; it is not affected by this gap and works under `temp_packages_link` (#113).**

Workaround: write the syntaxes under `<sublime.packages_path()>/User/<subdir>/` and use `wait_for_scope` to gate on each guest scope surfacing in `sublime.find_syntax_by_scope`. The `Packages/User/<subdir>/` ingest path *does* feed ST's cross-syntax resolver, so `push:` / `set:` / `embed:` / `include:` against a guest scope resolve correctly. The basename-only `wait_for_resource` gate is insufficient here — the scope registry is a separate ingest from the resource indexer, so use the scope-registry helper. Note the [#24 third comment](https://github.com/stefanobaghino/sublime-mcp/issues/24#issuecomment-4355566219) caveat that `Packages/User/<subdir>/` itself can fail to register intermittently.

```python
# Cross-syntax recipe: write under Packages/User/<subdir>/, gate on the registry.
import os
subdir = os.path.join(sublime.packages_path(), "User", "probe_xsyn")
os.makedirs(subdir, exist_ok=True)
with open(os.path.join(subdir, "Host.sublime-syntax"), "w") as f:
    f.write(host_yaml)            # contains `push: scope:source.guest`
with open(os.path.join(subdir, "Guest.sublime-syntax"), "w") as f:
    f.write(guest_yaml)           # `scope: source.guest`
assert wait_for_scope(["source.host", "source.guest"]), "guests never registered"
# now resolve_position / probe_scopes against the host see the guest's scopes.
```

`wait_for_scope(scope, timeout=3.0)` (#117) accepts a single scope or an iterable — the iterable form succeeds only when every scope surfaces, matching the host+guest shape above. `sublime.find_syntax_by_scope(scope)` itself returns `list[Syntax]` (typically empty or single-element), not a single `Syntax` (#111); `wait_for_scope` bakes the truthy-context handling in.

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

`temp_packages_link` synthesises a unique nonce-named package, so the bundled `Packages/Java` continues to load alongside it — `requested_syntax != resolved_syntax` still flags any silent fallback to a built-in. The per-syntax mode is sufficient for synthetic probes and single-grammar regression triage; cross-grammar investigations where the testdata grammar embeds another testdata grammar (e.g. C# embedding RegExp) need a whole-tree mirror that shadows the built-ins, tracked separately in §6.

This recipe only works because the syntax is consumed via direct URI assignment. If the linked syntax contains any cross-syntax reference (`push:` / `set:` / `embed:` / `include:` against a `scope:source.X` or a file-path target), ST silently falls back to Plain Text inside the embedded region — see the *Cross-syntax / multi-syntax probes* recipe above and write the syntaxes under `Packages/User/<subdir>/` instead.

When a caller writes additional `.sublime-syntax` files into the already-linked dir between snippets — incremental probing — wait for them to surface via `wait_for_resource("MyProbe*.sublime-syntax")` from a follow-up snippet, *not* an in-snippet `find_resources` poll. An in-snippet poll that overruns `EXEC_TIMEOUT_SECONDS` is killed at the transport, but the main-thread state it touched can leave ST wedged for the rest of the session (#64).

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

`probe_scopes`'s `scopes` dict is mode-dependent (#112): integer keys via `result` (the canonical channel — see §5 "Assign structured values to `_`"), string keys via JSON `output`. Index `r["scopes"][position]` with an `int` if you read via `_` / `result`; cast back with `int(k)` if you parsed `output` through JSON. Picking the canonical channel avoids the defensive `r["scopes"].get(str(i), r["scopes"].get(i))` boilerplate.

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

## 5. Output discipline

- **Return raw scopes.** `source.python keyword.control.flow` is the answer — don't paraphrase to "it's a Python keyword in a control-flow context." The caller can read the scope; paraphrase drops information.
- **`summary` before full panels.** For `run_syntax_tests`, the summary is usually enough. Print `output` or iterate `failures` only when the caller needs the specific failed assertions.
- **One question per call.** `exec_sublime_python` captures `print()` line-for-line; don't cram unrelated investigations into one snippet. A probe loop is fine; a second unrelated question is not.
- **Assign structured values to `_`.** If you're returning a dict or list, assign to `_` and let `repr(_)` come back as `result` — less shell-escaping, clearer for the caller than `json.dumps`'ing into `output`.

## 6. Known limitations / tracking

_Last synced with issue state: 2026-05-06._

- **Log levels are part of the contract; log line format is best-effort.** The four-level meaning (ERROR / WARNING / INFO / DEBUG) and the column positions of `req=<id>` are stable within a release line. The exact wording of individual messages and their phrasing may change between releases.
- **Parse-table-build silent fallback (case-3 of three).** ST sometimes registers a syntax structurally — `sublime.list_syntaxes()` shows it with the declared scope, `view.syntax().path` echoes the requested URI — but its parse-table builder rejects something deeper (e.g. an action shape ST doesn't compile, like multi-target `embed: [a, b]`). The signature is `requested_syntax == resolved_syntax` AND `view.scope_name(p) == "text.plain"` for every position despite a non-plain declared base. The `requested == resolved` invariant from §7 does not catch this on its own. `probe_scopes` raises `RuntimeError` on this shape: it samples `view.scope_name(0)` after `assign_syntax_and_wait`'s stage-2 poll, re-polls 200 ms for race tolerance, and raises if the scope is still `text.plain` under a non-plain declared base. For the position-resolution helpers (`scope_at_test`, `resolve_position`), the dict-level detection is `resolved_syntax != "Packages/Text/Plain text.tmLanguage"` AND `scope == "text.plain"` AND `sublime.syntax_from_path(resolved).scope` is non-plain.
- **#7** — parameterise the test suite's hardcoded `HEADER` across syntaxes.
- **#8** — concurrency cap on the exec daemon-thread pool.
- **whole-tree mirror** (follow-up to #24) — `temp_packages_link` covers per-syntax probing, but cross-grammar investigations where one testdata grammar embeds another (e.g. C# embedding RegExp) need the testdata tree to *shadow* ST's built-ins, not coexist with them. Different lifecycle (parent symlink, per-entry shadowing); not yet implemented.
- **#108** — `temp_packages_link` synthesises a `Packages/__sublime_mcp_temp_<nonce>__/` symlink whose contents are reachable via `find_resources` and `view.assign_syntax(URI)`, but ST's parse-table builder for cross-syntax references (`push:` / `set:` / `embed:` / `include:` against `scope:source.X` and file-path forms) doesn't pick them up — every position in the embedded region tokenises as `text.plain` while `requested == resolved` still holds on the host syntax, so the existing detectors don't trip. Workaround: write under `<sublime.packages_path()>/User/<subdir>/` and poll `find_syntax_by_scope` (see §4 *Cross-syntax / multi-syntax probes*). The `find_syntax_by_scope` registry itself does eventually surface linked syntaxes given enough wait, so it's not a reliable signal on its own — the parse-table builder is a separate ingest with its own failure mode.
- **#34** — `find_resources` can list stale `Packages/...` paths whose `load_resource` raises `FileNotFoundError`; documented in §4 ("Filter find_resources output through load_resource").

## 7. Reference — preloaded helpers

- `scope_at(path, row, col) -> dict` — open file, return `{"scope", "resolved_syntax"}`. `resolved_syntax` is `view.syntax().path` (or `None`); compare against the canonical plain-text URI to detect extension-less / no-syntax fallback.
- `scope_at_test(path, row, col) -> dict` — parse `# SYNTAX TEST` header, assign that syntax, return `{"scope", "requested_syntax", "resolved_syntax"}`. `requested_syntax != resolved_syntax` flags silent fallback to the wrong syntax. Tolerates both directions of the post-assign race (`view.syntax()` lagging `view.scope_name(...)`, and vice versa) by substituting `view.settings()["syntax"]` when `view.syntax()` is `None` and by polling `view.scope_name(point)` for ~200 ms when it returns `text.plain` under a known non-plain syntax. Persistent `text.plain` past that budget under a non-plain syntax indicates a silent fallback (parse-table build failure or similar — see #78), not a transient race.
- `resolve_position(path, row, col, syntax_path=None) -> dict` — full position disambiguation with `overflow` / `clamped` flags; also carries `requested_syntax` / `resolved_syntax`. Tolerates both directions of the post-assign race (`view.syntax()` lagging `view.scope_name(...)`, and vice versa) by substituting `view.settings()["syntax"]` when `view.syntax()` is `None` and by polling `view.scope_name(point)` for ~200 ms when it returns `text.plain` under a known non-plain syntax. Persistent `text.plain` past that budget under a non-plain syntax indicates a silent fallback (parse-table build failure or similar — see #78), not a transient race.
- `run_syntax_tests(path) -> dict` — run ST's built-in syntax-test runner. `{state, summary, output, failures, failures_structured}`. Path must resolve under `sublime.packages_path()` (directly or via a symlink in that directory); paths outside the Packages tree raise.
- `run_inline_syntax_test(content, name) -> dict` — synthetic-probe variant: writes `content` to a managed temp dir under `Packages/User/`, runs the runner, cleans up. Same shape as `run_syntax_tests` plus a `"inconclusive"` state when ST never indexes the temp resource.
- `probe_scopes(content, syntax_path=None, syntax_yaml=None, points=None, rstrip_scopes=True) -> dict` — scratch-view full-buffer scope sweep. Pass `syntax_path` to probe an already-reachable syntax (`Packages/...` URI or filesystem path under `packages_path()`), `syntax_yaml` to synthesise a grammar inline (helper writes to a managed `Packages/User/__sublime_mcp_temp_<nonce>__/` dir and removes it on exit, no `Packages/User/Probe.sublime-syntax` leak). Returns `{"scopes": {int: str}, "tokens": [...], "view_size", "syntax", "requested_syntax", "resolved_syntax"}`. `requested_syntax` (#119) is the canonical `Packages/...` URI passed to `view.assign_syntax`, mirroring `scope_at_test` / `resolve_position`, so the same `assert r["requested_syntax"] == r["resolved_syntax"]` defensive guard against silent fallback works here too; `syntax` is retained as an alias for back-compat. The `scopes` dict's key type is mode-dependent (#112) — `int` via `result` (the canonical channel, see §5); JSON serialisation stringifies them when read via `output`. Read via `_` / `result` to avoid casting; see the §4 "Mutate a buffer" callout for the canonical mode. Tolerates the post-assign race via the same `view.scope_name` warm-up shape as `scope_at_test` / `resolve_position` — single warm-up at the highest sweep point. Raises `RuntimeError` on the case-3 silent fallback from #78 (parse-table build failure: declared base is non-plain but `view.scope_name(0)` is still `text.plain` past the stage-2 + re-poll budget — see §6).
- `open_view(path, timeout=5.0) -> View` — open a file, poll `is_loading` and initial tokenisation.
- `assign_syntax_and_wait(view, resource_path, timeout=2.0) -> None` — assign a syntax and wait for the setting to apply + best-effort tokenisation.
- `run_on_main(callable, timeout=2.0)` — schedule `callable` on ST's main thread; return its value (or re-raise its exception). Required wrapper for `view.run_command(...)` and other `TextCommand` mutations.
- `temp_packages_link(filesystem_path) -> str` / `release_packages_link(name) -> None` — synthesise / tear down a per-call `Packages/__sublime_mcp_temp_<nonce>__` symlink for repo-local syntaxes. `filesystem_path` accepts either a `.sublime-syntax` file (links its parent directory) or a directory (links it directly). Returns the synthesised package name; build URIs as `Packages/<name>/<basename>`. Cross-syntax references (`push:` / `set:` / `embed:` / `include:`) inside a linked syntax silently fall back to Plain Text under the link path (#108) — for cross-syntax probes write under `<sublime.packages_path()>/User/<subdir>/` instead, see §4 *Cross-syntax / multi-syntax probes*.
- `find_resources(pattern) -> list[str]` — wrap `sublime.find_resources`.
- `wait_for_resource(pattern, timeout=3.0) -> bool` — poll `find_resources(pattern)` until any match surfaces or the budget expires. Use across snippets to wait for a file written in a prior `exec_sublime_python` to surface in ST's resource index — chaining cross-snippet avoids the wedge risk of in-snippet polling that overruns `EXEC_TIMEOUT_SECONDS`. `pattern` is matched as a glob against resource basenames only (`sublime.find_resources` semantics) — full `Packages/<dir>/<file>` paths never match. Raises `ValueError` if `pattern` contains `/` rather than silently returning `False` after burning the timeout (#100).
- `wait_for_scope(scope, timeout=3.0) -> bool` — poll `sublime.find_syntax_by_scope(scope)` until every scope surfaces or the budget expires. Scope-registry counterpart of `wait_for_resource` (#117) — separate ingest from the resource indexer, required gate for cross-syntax probes (`push: scope:` / `set: scope:` / `embed: scope:` / `include: scope:`) per #108. Accepts a single scope string or an iterable; iterable form succeeds only when every scope is registered. Raises `ValueError` on an empty iterable.
- `reload_syntax(resource_path) -> None` — force-reload a `.sublime-syntax` resource via view reactivation.

Full signatures, gotchas, and threading guarantees live in `TOOL_DESCRIPTION` (read via `tools/list`). This reference is a cheat-sheet.

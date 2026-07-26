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

Columns: `<wall-clock ISO-8601>`  `<LEVEL>`  `<[component]>`  `req=<JSON-RPC id>`  `<message>`. The bridge process (PID 1 in the container) logs as `[bridge]` to stderr; that stream is what the parent `docker run` forwards to Claude Code's MCP log. The plugin running inside ST's plugin host uses the same `[bridge]` component name through a separate handler — it writes to `$SUBLIME_MCP_LOG_FILE` if a host file is mounted at that path, otherwise to a stderr ST severed from PID 1 at self-daemonisation. The default `sublime-mcp` shim does not set up that mount, so on a stock install plugin-host bridge events are not host-readable.

**Read channels.** Bridge process (PID 1, live): `claude mcp logs sublime-text` if your build of Claude Code surfaces it, otherwise whatever MCP-server stderr surface the host platform exposes. Plugin-host bridge events: host-readable only when a host file is mounted at `$SUBLIME_MCP_LOG_FILE` (the default shim does not). ST's own stdout/stderr — `subl --stay`'s output, `package_control` chatter, plugin tracebacks — is redirected by the container's `entrypoint.sh` to `/var/log/sublime.log` *inside* the container; `docker exec <cid> cat /var/log/sublime.log` reads it. The plugin-host startup env sentinel sits at `/tmp/sublime-mcp-init.log` (also via `docker exec`). **`docker logs <cid>` returns nothing useful** for either channel — ST detaches before it writes to PID 1's inherited streams.

**Levels.**

- `ERROR` — a request will fail to return useful data. Worker timeout always fires a `faulthandler.dump_traceback(all_threads=True)` on the same line for every Python thread's stack.
- `WARNING` — silent-fallback shapes the caller is likely to misinterpret (`requested_syntax != resolved_syntax`, `run_on_main` 2 s timeout fires before the worker's 60 s ceiling, `assign_syntax_and_wait` stage-1 timeout).
- `INFO` — boundary events: container boot/ready/shutdown, sweep removals, **and** `worker entered` / `snippet exec begin` / `snippet exec done` per call. Default level — sufficient for main-thread-wedge diagnosis without DEBUG firehose.
- `DEBUG` — proxy-loop trail (`forwarding`/`received`), helper-entry traces (`assign_syntax_and_wait` etc.), `_compile_snippet` auto-lift branch.

**Troubleshooting workflow.**

1. **Observe** the failure (timeout, error response, surprising scope).
2. **Read backward** in the live MCP log to see the INFO trail of `[bridge]` events leading up to the failure. Grep for the `req=<id>` of the failing request to isolate its path through the bridge. For plugin-host tracebacks and ST's own output, `docker exec <cid> cat /var/log/sublime.log` (and `/tmp/sublime-mcp-init.log` for the plugin's startup env sentinel).
3. **If the INFO trail isn't enough**, bump the in-process plugin logger to DEBUG live — no restart needed: drive `exec_sublime_python` with `import logging; logging.getLogger("sublime_mcp.bridge").setLevel(logging.DEBUG)` and reproduce. Only works while the plugin is responsive (i.e. before a wedge); during an active wedge, bumping the level is moot — the diagnostic information is in the `faulthandler` dump that already fired at ERROR.
4. **For the bridge process itself**, the PID-1 logger reads `SUBLIME_MCP_LOG_LEVEL` from the container environment (default `INFO`). The shim forwards `SUBLIME_MCP_LOG_LEVEL` (and `SUBLIME_MCP_LOG_FILE`) from its own env via `docker run -e`, so the supported path is to re-register with the variable set: `claude mcp remove sublime-text && claude mcp add --scope user --transport stdio -e SUBLIME_MCP_LOG_LEVEL=DEBUG sublime-text -- "$PWD/sublime-mcp" --mount "$PWD:/work"`. As fallbacks: edit the shim to hardcode the level, or rebuild the image with it baked in. The plugin-host logger reads the same variable through the same forwarding.

**Common patterns.**

| Symptom (in `error` field) | Trail shape | Likely cause |
|----------------------------|-------------|--------------|
| `exec timed out after 60.0s` | no preceding `[bridge] worker entered` | bridge couldn't dispatch the worker (rare; check for plugin host crash). |
| `exec timed out after 60.0s` | `[bridge] worker entered`, `[bridge] snippet exec begin`, no `[bridge] snippet exec done`, ends in `[bridge] ERROR worker did not complete in 60.0s; worker thread is_alive=True` plus a multi-line `faulthandler` traceback | snippet wedged on ST's main thread (the canonical wedge shape). The `faulthandler` dump pinpoints the thread waiting on `run_on_main` or similar. |
| `plugin HTTP error: ...` | no preceding `docker logs` traceback | container died (likely OOM / SIGKILL). Check `docker ps`. |
| Plugin-host Python traceback in `docker logs` with no further `[bridge]` lines | bridge thread crashed on an uncaught plugin-host exception | restart the agent session; consider filing the traceback as a bridge bug. |

**Surfacing to the user.** Don't dump the whole trail — pull the ~30 lines around the failure boundary and grep for the failing `req=<id>`. The user's session already has the bridge stderr; you're highlighting the relevant slice.

## 1.2 Capturing ST's own console output (don't bother)

When ST silently rejects a `.sublime-syntax` (parse-table-build failure), the rejection reason is written to ST's in-memory console panel and **does not cross any syscall boundary the harness can intercept** — every in-process and out-of-process capture surface has been verified empty (panel APIs, stdio redirection, `dup2`, `strace` on all ST processes, `sublime.log_*` toggles; the full evidence lives in this section's git history). If a probe needs to know *why* a syntax was rejected, fall back to differential structural probing (write a known-good control alongside the suspect form, compare which one ST resolves) and surface the observation as "rejected at some layer beyond YAML parse" without naming the layer.

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
- Preloaded helpers (`scope_at`, `scope_at_test`, `resolve_position`, `run_syntax_tests`, `probe_scopes`, `open_view`, `assign_syntax_and_wait`, `find_resources`, `wait_for_resource`, `wait_for_scope`, `temp_user_packages_dir`, `dump_bytes`, `preflight_wedge_check`, `reload_syntax`) are in scope without import.

The helpers split into two families. **View-driving** helpers (`scope_at`, `scope_at_test`, `resolve_position`, `probe_scopes`, `open_view`, `assign_syntax_and_wait`) require a window — they raise `RuntimeError` in headless ST. **Runner-driving** helpers (`run_syntax_tests`, `run_inline_syntax_test`) and resource queries (`find_resources`, `wait_for_resource`, `reload_syntax`, `temp_packages_link` / `release_packages_link`) work fine headless. When `len(sublime.windows()) == 0`, runner-driving experiments still proceed; only view-driving snippets need the user to open a window first (`open -a "Sublime Text"` on macOS).

For the full helper surface, threading guarantees, and the authoritative `text_point` overflow semantics, read the tool's own `description` via `tools/list`. If this skill contradicts it, `tools/list` is right.

**Paths are container-side.** Every path you pass into `exec_sublime_python` (to `scope_at`, `run_syntax_tests`, etc.) is resolved inside the container, not on the host. The user mounts host directories into the container at registration time; the recommended mount is `--mount $PWD:/work` so a host `~/Projects/foo/syntax_test_x.cs` becomes `/work/foo/syntax_test_x.cs` in calls. If a path you'd expect to resolve raises `FileNotFoundError`, check the user's mount before retrying; ask them rather than guessing the host-to-container mapping. If the call hangs or times out instead of raising, the host-side-write footgun is the likely cause — same root, different shape; see the preamble of `recipes.md`. `/tmp` is per-container scratch — safe to write synthetic syntax/input files into when the user's working tree shouldn't be touched.

### 3.2 health_check

`mcp__sublime-text__health_check({})` is a worker-thread-only probe that detects when ST's main thread is wedged. It returns within ~2.5s regardless of main-thread state and never goes near the 60s `exec_sublime_python` ceiling. Response shape:

```json
{ "main_thread_responsive": true, "main_thread_probe_elapsed_s": 0.01, "plugin_host_pid": 2060, "uptime_s": 142, "container_id": "<docker cid>", "workspace_path": "/work", "st_version": 4200, "st_channel": "stable" }
```

**Call pattern.** When an `exec_sublime_python` call times out at 60s on something that touched the main thread (`scope_at`, `find_resources`, `open_file`, `assign_syntax_and_wait`, anything wrapped in `run_on_main`), call `health_check` *before* the next main-thread snippet. If `main_thread_responsive` is `false`, stop issuing main-thread snippets — every one will burn another 60s. Drive the recovery flow in recipes.md (*Recover from a wedged main thread*) rather than retrying. If `main_thread_responsive` is `true`, the previous timeout was about that specific snippet, not a session-wide wedge — retrying is fine.

**`/mcp` reconnect does not clear a wedged main thread.** The slash-command reports `Reconnected to sublime-text.` and re-establishes the MCP transport, but the underlying ST process keeps running with the same wedged main thread — the next `set_timeout(callback, 0); event.wait(...)` still returns False. In-agent recovery is `restart_st` (§3.4); the docker-kill route (use the `container_id` from a previous response: `docker kill <cid>`) remains as a final fallback. Don't read "Reconnected" as "wedge cleared."

**`/mcp` reconnect can also land on stale transport.** A `Reconnected to sublime-text.` message does not guarantee the transport has re-bound to the new container. If the next call returns `ConnectionRefusedError(61)`, the transport is still pointed at the previous container's stdio — dismiss `/mcp` and re-open (not just re-trigger) to force a re-bind. Independent of the wedge surface above: the stale-transport shape fires whenever the previous container went away (wedge recovery via `docker kill`, container OOM, container restart) and Claude Code's reconnect attempt landed before the new container was discoverable. Guard pattern: after `docker kill` + reconnect, fire a single `health_check`; on `ConnectionRefusedError`, the user needs to re-open `/mcp` rather than the agent retrying.

### 3.3 inspect_environment

`mcp__sublime-text__inspect_environment({})` is a bridge-owned diagnostic snapshot of container-level state. Worker-thread-only on the bridge — never touches the plugin host — so it returns within ~3s even when ST main is wedged or the entire plugin host is dead. Response shape:

```json
{
  "bridge_pid": 1,
  "sublime_text_pids": [42],
  "plugin_host_pid": 56,
  "xvfb_pid": 18,
  "http_server_listening": true,
  "http_probe_elapsed_s": 0.05,
  "http_probe_error": null,
  "display_reachable": true,
  "x_windows": "<xwininfo -root -tree, capped at ~2KB>",
  "container_id": "<docker cid>",
  "workspace_path": "/work",
  "uptime_s": 142
}
```

**Call pattern.** Use after `health_check` returns `main_thread_responsive: false` to triage *which* recovery to attempt. Read it as a decision tree:

- `http_server_listening: false` → plugin host is dead (not just wedged). `health_check` would also be unreachable. Go straight to `restart_st` (§3.4).
- `http_server_listening: true` and unexpected entries in `x_windows` (a dialog title that isn't ST's main editor window) → soft recovery first: `subprocess.run(["xdotool", "key", "Escape"], capture_output=True, timeout=5)` from an `exec_sublime_python` snippet, then `health_check` again. If main is back, continue.
- `http_server_listening: true` and `x_windows` looks normal → wedge isn't dialog-shaped; soft recovery won't help. Use `restart_st`.
- `display_reachable: false` → Xvfb itself is gone. `restart_st` won't help (it relaunches `subl --stay` against a missing display); ask the user to restart the container.

Best-effort: any individual subprocess failure surfaces as `null` / `false` for that field; the rest of the payload still returns. The tool never raises — read each field independently.

### 3.4 restart_st

`mcp__sublime-text__restart_st({})` is the in-agent escape hatch for a wedge that soft recovery (§3.5 `xdotool`) doesn't clear. Bridge-owned: kills ST + plugin host (TERM, then KILL after 5s), relaunches `subl --stay <workspace>`, polls until the plugin's HTTP server is responsive again. Returns within ~30s. Response shape:

```json
{
  "success": true,
  "elapsed_s": 8.3,
  "sublime_text_pids_before": [42],
  "sublime_text_pids_after": [161],
  "plugin_host_pid_before": 56,
  "plugin_host_pid_after": 187,
  "http_ready_after_s": 6.1,
  "log_lines": ["…", "…"]
}
```

**On success.** The new plugin host is fully reinitialised — `plugin_loaded()` ran, `health_check` should return `main_thread_responsive: true` immediately, and `exec_sublime_python` round-trips work again. Re-issue the original probe. `plugin_host_pid_before != plugin_host_pid_after` is the in-payload signal that the restart actually cycled the process.

**On failure.** `success: false`, `error` carries a short message, `log_lines` shows which step got through. Common shapes:

- `"sublime_text still running after KILL+3s"` — process is unkillable from PID 1's perspective (rare; typically a kernel-level zombie). Ask the user to `docker kill <cid>`.
- `"plugin HTTP did not come back: ..."` — `subl --stay` was launched but the plugin host never started its HTTP server within 30s. Either the relaunch silently bounced off a startup dialog (check `x_windows` via `inspect_environment`) or ST hit a worse failure mode. Ask the user to `docker kill <cid>`.
- `"subl launch failed: ..."` — `subl` binary is unavailable. Container-image bug; file an issue.

**Destructive — does not preserve view state.** Open files, scratch buffers, the in-memory `_TEMP_LINKS` registry from `temp_packages_link` calls are all gone after the restart. Symlinks under `Packages/__sublime_mcp_temp_*` are reaped by the next helper invocation's lazy sweep. Don't reach for `restart_st` for ergonomics — only when a wedge is the real cause.

### 3.5 X-debug binaries (`xdotool`, `xdpyinfo`, `xwininfo`, `xprop`, `xkill`)

The image ships these stable Ubuntu utilities for inspecting / interacting with the Xvfb display from inside the container. All are callable from `subprocess.run([...], capture_output=True, timeout=...)` inside `exec_sublime_python` — they execute on the worker thread, so they keep working when ST main is wedged.

- `xdotool key Escape` / `xdotool key Return` — synthesise key events; the soft-recovery workhorse for invisible startup or modal dialogs that ST's headless build can't dismiss on its own.
- `xdpyinfo` (exit code) — quick "is the X display reachable?" probe. Already wrapped in `inspect_environment.display_reachable`.
- `xwininfo -root -tree` — enumerate top-level windows. Already wrapped in `inspect_environment.x_windows`; reach for it directly if you need more than the truncated 2KB snapshot.
- `xprop -id <id>` — read window properties (`WM_CLASS`, `WM_NAME`) once a suspect window's id is known.
- `xkill` — last-resort window kill via X protocol, before reaching for `restart_st`.

Usage example (from a snippet, after `inspect_environment` flagged a candidate dialog):

```python
import subprocess
r = subprocess.run(
    ["xdotool", "search", "--name", ".*", "key", "Escape"],
    capture_output=True, text=True, timeout=5,
)
print(r.returncode, r.stderr[:200])
```

These binaries are not on the agent's MCP surface — they're shell tools, used through `exec_sublime_python`. The bridge wrappers in §3.3 cover the common diagnostic shape.

## 4. Recipes — in `recipes.md`

The copy-paste recipes live in `recipes.md` in this skill's directory. Read the matching recipe before improvising a probe:

- *Recover from a wedged main thread* — the escalation flow after `health_check` reports `main_thread_responsive: false`.
- *Scope at a position* — `scope_at` / `scope_at_test`, including the extension-less-file Plain Text landmine.
- *Run syntax tests against a file* — `run_syntax_tests` result contract and failure causes.
- *Read the scope chain via the runner's failure diagnostic* — scope probing when ST is headless.
- *Probe a synthetic case inline* — `run_inline_syntax_test` and the assertion-line `^` alignment rule.
- *Probe a synthetic syntax against a synthetic input* — `temp_packages_link` + `resolve_position`; includes the *Cross-syntax / multi-syntax probes* sub-recipe (`temp_user_packages_dir` + `wait_for_scope`).
- *Confirm which syntax ST assigned (and handle repo-local syntaxes)* — silent-fallback detection, repo-local symlinking.
- *Compare a parser's output against ST* — three-step divergence triage.
- *Mutate a buffer from a snippet* — the `run_on_main` requirement.
- *Bulk probes* — cost model for large scope sweeps.
- *Filter find_resources output through load_resource* — stale-index filtering.
- *Probe a large syntax-test file in pieces* — when the runner exceeds the 60 s ceiling.

Two invariants apply to every recipe: rows/cols are **0-indexed**, and all paths are container-side — host files must sit under a mount (typically `/work`), and probe files must be written from inside the snippet, never pre-written to unmounted host paths (the failure shape is a hang, not `FileNotFoundError`).

## 5. Output discipline

- **Return raw scopes.** `source.python keyword.control.flow` is the answer — don't paraphrase to "it's a Python keyword in a control-flow context." The caller can read the scope; paraphrase drops information.
- **`summary` before full panels.** For `run_syntax_tests`, the summary is usually enough. Print `output` or iterate `failures` only when the caller needs the specific failed assertions.
- **One question per call.** `exec_sublime_python` captures `print()` line-for-line; don't cram unrelated investigations into one snippet. A probe loop is fine; a second unrelated question is not.
- **Assign structured values to `_`.** If you're returning a dict or list, assign to `_` and let `repr(_)` come back as `result` — less shell-escaping, clearer for the caller than `json.dumps`'ing into `output`.
- **For byte-exact strings, use `dump_bytes`.** Strings containing tabs, newlines, or other whitespace controls round-trip ambiguously through `repr` → JSON: a real tab (`0x09`) and the literal sequence `\` + `t` produce visually-identical agent-side strings. When the question is "did ST normalise this byte sequence?" — newline canonicalisation in raw-string contexts, `\r\n` vs `\n`, NUL handling, BOM at scope boundaries — `print(dump_bytes(value))` returns a hex digest that survives the transport unchanged.

## 6. Known limitations

- **Log line format is best-effort.** The four-level meaning (ERROR / WARNING / INFO / DEBUG) and the column positions of `req=<id>` are stable within a release line. The exact wording of individual messages and their phrasing may change between releases.
- **Parse-table-build silent fallback.** ST sometimes registers a syntax structurally — `sublime.list_syntaxes()` shows it with the declared scope, `view.syntax().path` echoes the requested URI — but its parse-table builder rejects something deeper (e.g. an action shape ST doesn't compile, like multi-target `embed: [a, b]`), or a `push: scope:` / `embed: scope:` / `include: scope:` against an unresolvable target falls back to Plain Text inside the embedded frame. The `requested == resolved` invariant from `helpers.md` does not catch either shape. `probe_scopes` raises `RuntimeError` from a sweep-time detector covering two shapes: (a) every position bare `text.plain` under a non-plain declared base; (b) any position carrying `text.plain` as a non-leading scope element — the embed-side variant where a cross-syntax reference fell back to Plain Text whose `meta_scope` is `text.plain`. For the single-position helpers (`scope_at_test`, `resolve_position`), the caller-side check is `requested_syntax != "Packages/Text/Plain text.tmLanguage"` AND `scope == "text.plain"` — they can't construct the cross-position view (b) relies on, so the detector stays at the assigned-syntax level.
- **Cross-syntax references under `temp_packages_link` silently fall back to Plain Text.** Synthesised `Packages/__sublime_mcp_temp_<nonce>__/` symlinks are reachable via `find_resources` and `view.assign_syntax(URI)`, but ST's parse-table builder for cross-syntax references (`push:` / `set:` / `embed:` / `include:` against `scope:source.X` and file-path forms) doesn't pick them up — every position in the embedded region tokenises as `text.plain` while `requested == resolved` still holds on the host syntax, so the existing detectors don't trip. Workaround: own a managed dir via `temp_user_packages_dir` under `<sublime.packages_path()>/User/` and gate via `wait_for_scope`; see recipes.md *Cross-syntax / multi-syntax probes*. The `find_syntax_by_scope` registry itself does eventually surface linked syntaxes given enough wait, so it's not a reliable signal on its own — the parse-table builder is a separate ingest with its own failure mode.
- **No whole-tree mirror.** `temp_packages_link` covers per-syntax probing, but cross-grammar investigations where one testdata grammar embeds another (e.g. C# embedding RegExp) need the testdata tree to *shadow* ST's built-ins, not coexist with them. Different lifecycle (parent symlink, per-entry shadowing); not yet implemented.
- **`find_resources` can list stale paths.** ST's resource index can outlive the underlying file, so entries returned by `find_resources` may raise `FileNotFoundError` from `load_resource`. Filter at the call site — see recipes.md *Filter find_resources output through load_resource*.
- **`preflight_wedge_check` rule set is narrow.** Two static rules cover known-wedge synthetic-syntax shapes: duplicate cross-scope includes, and zero-width-only match paired with push. Multi-file shapes (e.g. back-to-back cross-syntax fragment includes under one link) are out of single-YAML scope; expansion is gated on a multi-file lint surface landing.

## 7. Reference — preloaded helpers

Per-helper contracts — signatures, return shapes, race-tolerance and silent-fallback notes — live in `helpers.md` in this skill's directory (the helper names are listed in §3.1). Read `helpers.md` before relying on a helper's exact return shape or failure mode. Full signatures and threading guarantees live in `TOOL_DESCRIPTION` (read via `tools/list`); if the docs disagree, `tools/list` is right.


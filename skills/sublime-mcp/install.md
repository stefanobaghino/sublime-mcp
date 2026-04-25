# Installing sublime-mcp for Claude Code

This file is loaded only when `SKILL.md`'s preflight check (§1) fails. The top-level [`README.md`](../../README.md) covers the full install; this file is the minimum to unblock the skill.

## What has to be true

- Sublime Text is running with `sublime_mcp.py` loaded. The ST console (**View → Show Console**) shows `[sublime-mcp] listening on 127.0.0.1:47823`.
- Claude Code has the server registered under the name `sublime-text`:
  ```bash
  claude mcp list | grep sublime-text
  ```
  Expected: `sublime-text ✓ Connected`.

## One-command smoke check

```bash
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"exec_sublime_python",
                 "arguments":{"code":"print(sublime.version())"}}}'
```

Returns ST's build number in `output` on a healthy setup. Connection refused → ST isn't running the plugin. 404 → wrong endpoint path (should be `/mcp`).

## If the plugin isn't loaded

Re-run the symlink from [`README.md#install`](../../README.md#install) and reopen ST (or save any `.py` file under `Packages/User/`) to trigger `plugin_loaded()`. The ST console should show the listening line.

## If Sublime Text has no open window

ST's plugin host can run while no editor window is open (the app stays alive in the background, especially on macOS). Helpers that drive views (`open_view`, `scope_at`, `scope_at_test`, `resolve_position`) raise `RuntimeError: open_view: Sublime Text has no open window` in this state. Open one:

```bash
# macOS
open -a "Sublime Text"

# Linux: launch from your desktop entry, or invoke the binary directly
# (often `sublime_text` or `subl` if you've set up a symlink on PATH).
# Windows: launch via the Start menu / taskbar shortcut. There is no
# universal CLI helper.
```

To reproduce headlessness deliberately and verify the guard end-to-end (macOS recipe — Linux/Windows have no equivalent for AppleScript-driven window control, so verify there by quitting all windows manually instead):

```bash
# Step 1. Save or discard any unsaved work — the close call below is a
# polite per-window quit and will block on the unsaved-buffer dialog.
# Do NOT pass `saving no` (destructive, risks data loss).
osascript -e 'tell app "Sublime Text" to close every window'
osascript -e 'tell app "Sublime Text" to count of windows'   # → 0

# Step 2. Confirm the plugin host still sees zero windows.
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"exec_sublime_python","arguments":{"code":"print(len(sublime.windows()))"}}}'
# Expected: response `output` contains "0\n".

# Step 3. Trigger the guard. Any path works — the guard fires before
# the file is read.
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"exec_sublime_python","arguments":{"code":"open_view(\"/tmp/anything\")"}}}'
# Expected: response `error` contains "no open window" and "install.md".

# Step 4. Recover by opening any window, then retry the original call.
open -a "Sublime Text"
```

## Verifying symlinked-package URI resolution

`_to_resource_path` reverse-maps a path under a symlinked entry of `sublime.packages_path()` (e.g. `testdata/Packages/Markdown` symlinked as `~/Library/.../Packages/Markdown`) to a `Packages/<symlink_name>/...` URI that ST's resource indexer agrees on. To verify end-to-end against your real ST install:

> **Warning:** this recipe creates a symlink in your real ST Packages directory. Step 3 removes it. If you skip cleanup, ST's resource indexer keeps treating the target as a registered package across sessions until you manually `unlink` it. The recipe uses a deliberately unique symlink name (`__sublime_mcp_verify__`) so it cannot collide with any real package or shadow ST's bundled syntax handling.

macOS paths shown — adjust for Linux's `~/.config/sublime-text/Packages/` or your platform's equivalent.

```bash
# Step 1. Pre-check (recovers from a prior interrupted run; -L matches
# only symlinks, so it cannot clobber a real package), then create.
[ -L ~/Library/Application\ Support/Sublime\ Text/Packages/__sublime_mcp_verify__ ] && \
  unlink ~/Library/Application\ Support/Sublime\ Text/Packages/__sublime_mcp_verify__
ln -s /path/to/your/Packages/Markdown \
      ~/Library/Application\ Support/Sublime\ Text/Packages/__sublime_mcp_verify__

# Step 2. Call run_syntax_tests with the *target path* — the input form
# that triggered <no build panel found> on pre-fix code. The symlink-
# walk now reverse-maps it to Packages/__sublime_mcp_verify__/...
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"exec_sublime_python","arguments":{"code":"r = run_syntax_tests(\"/path/to/your/Packages/Markdown/tests/syntax_test_markdown.md\"); print(r[\"summary\"])"}}}'
# Success: `summary` is a numeric "N assertions passed" or
# "FAILED: M of N assertions failed". Explicitly NOT
# "<no build panel found>" (the API path didn't fire — _to_resource_path
# returned None) and NOT "<resource not indexed by Sublime Text>" (the
# API path ran but ST's resource indexer disagreed with the
# reconstructed URI).

# Step 3. Cleanup (do not skip). unlink, NOT rm -r / rm -rf — those
# follow the symlink and would delete the target's contents.
unlink ~/Library/Application\ Support/Sublime\ Text/Packages/__sublime_mcp_verify__
```

## If Claude Code can't see the server

```bash
claude mcp add --transport http --scope user \
  sublime-text http://127.0.0.1:47823/mcp
```

The name `sublime-text` is load-bearing: this skill's `allowed-tools` hard-codes `mcp__sublime-text__exec_sublime_python`. Registered under a different name, the skill won't see the tool.

## Still stuck?

Open an issue at <https://github.com/stefanobaghino/sublime-mcp/issues> with:

- `claude mcp list` output
- The curl probe's response (stdout + stderr)
- ST console contents from the `[sublime-mcp]` line onwards

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

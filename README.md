# sublime-mcp

A [Sublime Text](https://www.sublimetext.com/) plugin that is also an
[MCP](https://modelcontextprotocol.io/) server. It exposes ST's Python API
to AI agents so they can query scopes, run syntax tests, reload syntax
files, and more — without a human in the loop copy-pasting build-panel
output.

Single file, standard library only, loopback only.

## Why

Sublime Text's sublime-syntax engine is the ground truth for scopes. When
a downstream consumer (e.g. [syntect](https://github.com/trishume/syntect))
disagrees with ST, you almost always want ST's answer, not the other way
around. Verifying "what does ST say" manually — symlink the package, open
ST, run **Tools → Build With → Syntax Tests**, copy the output panel —
is slow and error-prone.

This plugin runs inside ST's plugin host and serves a single MCP tool,
`exec_sublime_python`, that runs arbitrary Python inside ST's process. An
agent with this tool can script the checks that would otherwise need a
human.

## Requirements

- Sublime Text 4 (Python 3.8 plugin host).
- macOS paths below — trivial to port, but only Darwin is tested.

## Install

```sh
git clone https://github.com/stefanobaghino/sublime-mcp \
  ~/Projects/github.com/stefanobaghino/sublime-mcp

ln -s ~/Projects/github.com/stefanobaghino/sublime-mcp/sublime_mcp.py \
      "$HOME/Library/Application Support/Sublime Text/Packages/User/sublime_mcp.py"
```

Open ST (or save any `.py` file under `Packages/User/`) to trigger
`plugin_loaded()`. Open the console (**View → Show Console**) and look for:

```
[sublime-mcp] listening on 127.0.0.1:47823
```

## Configure Claude Code

```sh
claude mcp add --transport http --scope user \
  sublime-text http://127.0.0.1:47823/mcp
```

Or add directly to `~/.claude.json`:

```json
{
  "mcpServers": {
    "sublime-text": {
      "type": "http",
      "url": "http://127.0.0.1:47823/mcp"
    }
  }
}
```

## Verify

```sh
curl -s -X POST http://127.0.0.1:47823/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"exec_sublime_python",
                 "arguments":{"code":"print(sublime.version())"}}}'
```

Should return ST's build number in the `output` field.

The tool's own `description` (readable via `tools/list`) is a cookbook of
common recipes — scope-at, run syntax tests, reload a syntax file, list
resources. Agents should read it as their primary reference.

## Security

- Binds `127.0.0.1` only. Not reachable off the local machine.
- Runs arbitrary Python inside Sublime Text. That is the feature — but it
  means anyone with local network access to the loopback port has full
  control of your editor. Do not expose the port beyond localhost and do
  not run this plugin on a multi-user machine you don't trust.

## Uninstall

```sh
rm "$HOME/Library/Application Support/Sublime Text/Packages/User/sublime_mcp.py"
claude mcp remove sublime-text --scope user
```

# Installing sublime-mcp for Claude Code

This file is loaded only when `SKILL.md`'s preflight check (§1) fails. The top-level [`README.md`](../../README.md) covers the full install; this file is the minimum to unblock the skill.

## What has to be true

- **Docker is installed and the daemon is running.** `docker info` exits 0.
- **The harness is on `PATH`.** `sublime-mcp --help` works.
- **Claude Code has the server registered under the name `sublime-text`.**
  ```bash
  claude mcp list | grep sublime-text
  ```
  Expected: `sublime-text ✓ Connected`.

When a Claude Code session is using the skill, `docker ps --filter label=sublime-mcp-harness` shows one running container. The harness owns the container; closing the session reclaims it.

## Install the harness

The harness is shipped as a single Python module + a Dockerfile, distributed via the source repo. v1 is editable-install only; the runtime needs the bundled assets to live next to the module.

```bash
git clone https://github.com/stefanobaghino/sublime-mcp.git
cd sublime-mcp
uv tool install --editable .
```

`uv tool install --editable .` keeps `sublime-mcp` pointing at this checkout — pulling new commits picks them up; switching branches switches the harness code. `pipx install -e .` and plain `pip install -e .` work too if you already have one of those set up.

## Register with Claude Code

The harness requires `--agent-name` and `--session-id` so each container can be named `st-<agent>-<session>` and matched back to the session that owns it. Both must come from the live agent context, so register the harness via a tiny launcher script (Claude Code's MCP launch is a static argv; it can't template per-session values directly):

```bash
# ~/.local/bin/sublime-mcp-launcher
#!/usr/bin/env bash
exec sublime-mcp \
    --agent-name "${CLAUDE_AGENT_NAME:-claude-code}" \
    --session-id "${CLAUDE_SESSION_ID:?CLAUDE_SESSION_ID must be set}" \
    --mount "$PWD:/work" "$@"
```

```bash
claude mcp add --scope user --transport stdio sublime-text -- \
    sublime-mcp-launcher
```

The launcher is the place to thread agent/session info from whatever the surrounding harness exposes (env vars, hook input, etc.). The harness itself hard-fails if either flag is missing.

The name `sublime-text` is load-bearing: the skill's `allowed-tools` hard-codes `mcp__sublime-text__exec_sublime_python`. Registered under a different name, the skill won't see the tool.

`--mount $PWD:/work` makes the current working tree visible to ST inside the container. Repeat the flag for additional directories. **Without a `--mount`, every path you'd pass in `exec_sublime_python` calls is invisible to ST** — the skill recipes assume this mount.

The first agent connection triggers `docker build`; expect a few minutes the first time. Subsequent connections boot the container in a few seconds.

## Smoke check

After registering, in a Claude Code session that has the skill loaded:

```
mcp__sublime-text__exec_sublime_python({ code: "print(sublime.version())" })
```

Returns ST's build number in `output` on a healthy setup. If the call errors with `FileNotFoundError` for a path under `/work`, the user forgot the `--mount`; re-register with one.

## Troubleshooting

### Harness fails to start

The harness writes diagnostics on stderr prefixed with `[sublime-mcp-harness]`. Common cases:

- `ERROR: docker not found on PATH` — install Docker (Docker Desktop on macOS/Windows, `docker-ce` package on Linux), start the daemon, retry.
- `ERROR: docker build failed` — re-run `sublime-mcp --rebuild --mount …` to see the build output. Most often: transient apt-mirror failure; retry. If persistent, the apt repo for Sublime Text may have shifted; file an issue.
- `ERROR: Sublime Text never opened a window (last state: …)` — the container booted but ST didn't reach a windowed state inside the readiness budget. Check `docker logs <container_id>` for Xvfb errors or licensing dialogs blocking startup.

### `docker ps` shows the container but tool calls hang

The plugin host inside the container is wedged. Restart the agent session (closing it triggers `docker stop`); a fresh one will spawn a new container.

### Multi-agent: ports / containers

Each agent session spawns its own harness, which spawns its own container. Host ports are kernel-assigned (`-p 127.0.0.1:0:47823`), so concurrent agents don't collide. `docker ps --filter label=sublime-mcp-harness` lists them; container names are `st-<agent>-<session-uuid>`, so the agent and session that own each container are visible in `docker ps` directly. The `sublime-mcp-harness=<pid>` label is still set for backwards-compatible filtering.

Each ST instance uses ~100–300 MB RAM. If you routinely run many concurrent agents, watch overall memory pressure.

### Sublime Text license

ST runs in evaluation mode by default inside the container. The plugin is unaffected; under Xvfb the nag dialog is invisible. To suppress evaluation state, pass a license:

```bash
sublime-mcp --mount "$PWD:/work" --license-file ~/path/to/License.sublime_license
```

The file is mounted read-only into the container's `~/.config/sublime-text/Local/`.

## Verifying symlinked-package URI resolution

`_to_resource_path` reverse-maps a path under a symlinked entry of `sublime.packages_path()` to a `Packages/<symlink_name>/...` URI that ST's resource indexer agrees on. Inside the container, `sublime.packages_path()` is `/root/.config/sublime-text/Packages`. To verify end-to-end via the harness:

```python
# Inside an exec_sublime_python call:
import os
target = "/work/testdata/Packages/Markdown"   # mounted by the user
link = os.path.join(sublime.packages_path(), "__sublime_mcp_verify__")
if os.path.lexists(link):
    os.unlink(link)
os.symlink(target, link)
try:
    r = run_syntax_tests("/work/testdata/Packages/Markdown/tests/syntax_test_markdown.md")
    print(r["summary"])
finally:
    os.unlink(link)
```

Success: `summary` is a numeric "N assertions passed" or "FAILED: M of N assertions failed". A top-level `error` carrying "is not under sublime.packages_path()" means the symlink-walk failed to reverse-map.

## If Claude Code can't see the server

```bash
claude mcp remove sublime-text
claude mcp add --scope user --transport stdio sublime-text -- \
    sublime-mcp-launcher
```

The name `sublime-text` is load-bearing: this skill's `allowed-tools` hard-codes `mcp__sublime-text__exec_sublime_python`. Registered under a different name, the skill won't see the tool.

## Still stuck?

Open an issue at <https://github.com/stefanobaghino/sublime-mcp/issues> with:

- `claude mcp list` output
- `docker ps --filter label=sublime-mcp-harness` output
- `docker logs <cid>` for the failing container (if any)
- The harness's stderr (the `[sublime-mcp-harness]` lines)

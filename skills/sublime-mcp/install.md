# Installing sublime-mcp for Claude Code

This file is loaded only when `SKILL.md`'s preflight check (§1) fails. The top-level [`README.md`](../../README.md) covers the full install; this file is the minimum to unblock the skill.

## What has to be true

- **Docker is installed and the daemon is running.** `docker info` exits 0.
- **A POSIX shell is available.** The shim is `/bin/sh`.
- **Claude Code has the server registered under the name `sublime-text`.**
  ```bash
  claude mcp list | grep sublime-text
  ```
  Expected: `sublime-text ✓ Connected`.

When a Claude Code session is using the skill, `docker ps` shows one running container per session. Closing the session triggers `dockerd` to reap the container.

## Install the shim

The shim lives in the source repo. There's nothing to install on `PATH`; register Claude Code with the absolute path to the script.

```bash
git clone https://github.com/stefanobaghino/sublime-mcp.git
cd sublime-mcp
chmod +x sublime-mcp
```

## Register with Claude Code

```bash
claude mcp add --scope user --transport stdio sublime-text -- \
    "$PWD/sublime-mcp" --mount "$PWD:/work"
```

The name `sublime-text` is load-bearing: the skill's `allowed-tools` hard-codes `mcp__sublime-text__exec_sublime_python`. Registered under a different name, the skill won't see the tool.

`--mount $PWD:/work` makes the current working tree visible to ST inside the container. Repeat the flag for additional directories. **Without a `--mount`, every path you'd pass in `exec_sublime_python` calls is invisible to ST** — the skill recipes assume this mount.

The shim runs `docker build -q` on every connect. The first connect builds from scratch — expect a few minutes. Subsequent reconnects pick up local edits to `plugin.py` / `bridge.py` automatically because Docker's layer cache invalidates on the late `COPY` lines.

## Smoke check

After registering, in a Claude Code session that has the skill loaded:

```
mcp__sublime-text__exec_sublime_python({ code: "print(sublime.version())" })
```

Returns ST's build number in `output` on a healthy setup. If the call errors with `FileNotFoundError` for a path under `/work`, the user forgot the `--mount`; re-register with one.

## Troubleshooting

### Connection fails or hangs

The shim writes diagnostics to stderr; Claude Code surfaces them in the MCP connection log. Common cases:

- `docker: command not found` — install Docker (Docker Desktop on macOS/Windows, `docker-ce` package on Linux), start the daemon, retry.
- `docker build` failure — `cd` into the checkout and run `docker build -t sublime-mcp:local .` directly to see the full output. Most often: transient apt-mirror failure; retry. If persistent, the apt repo for Sublime Text may have shifted; file an issue.
- `Sublime Text never opened a window` — the container booted but ST didn't reach a windowed state inside the readiness budget. Run the container manually (`docker run --rm -it sublime-mcp:local`) and check `/var/log/sublime.log` and `/var/log/xvfb.log` inside the container for licensing dialogs or X server errors.

### `docker ps` shows the container but tool calls hang

The plugin host inside the container is wedged. The skill ships an in-agent recovery toolkit — see SKILL.md §3.2 (`health_check`), §3.3 (`inspect_environment`), §3.4 (`restart_st`), and the §4 *Recover from a wedged main thread* escalation flow. As a final fallback when `restart_st` itself fails, `docker kill <cid>` from the host (the `container_id` is echoed in every `exec_sublime_python` response) and `/mcp` re-open spawns a fresh container.

### Multi-agent

Each agent session runs its own shim, which spawns its own container via `docker run -i --rm`. Container names are auto-generated; concurrent sessions don't collide. Each ST instance uses ~100–300 MB RAM — watch memory pressure if you routinely run many concurrent agents.

### Sublime Text license

ST runs in evaluation mode by default inside the container. The plugin is unaffected; under Xvfb the nag dialog is invisible. To suppress evaluation state, mount a license through `--mount`:

```bash
"$PWD/sublime-mcp" --mount "$PWD:/work" \
    --mount "$HOME/path/to/License.sublime_license:/root/.config/sublime-text/Local/License.sublime_license"
```

The file is mounted into the container's `~/.config/sublime-text/Local/`.

## Verifying symlinked-package URI resolution

`_to_resource_path` reverse-maps a path under a symlinked entry of `sublime.packages_path()` to a `Packages/<symlink_name>/...` URI that ST's resource indexer agrees on. Inside the container, `sublime.packages_path()` is `/root/.config/sublime-text/Packages`. To verify end-to-end:

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
    "$PWD/sublime-mcp" --mount "$PWD:/work"
```

The name `sublime-text` is load-bearing: this skill's `allowed-tools` hard-codes `mcp__sublime-text__exec_sublime_python`. Registered under a different name, the skill won't see the tool.

## Still stuck?

Open an issue at <https://github.com/stefanobaghino/sublime-mcp/issues> with:

- `claude mcp list` output
- `docker ps` output for the failing container
- `docker logs <cid>` for the failing container (if any)
- The shim's stderr (bridge `[bridge]` lines)

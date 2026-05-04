# sublime-mcp

[![tests](https://github.com/stefanobaghino/sublime-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/stefanobaghino/sublime-mcp/actions/workflows/tests.yml)
[![harness-smoke](https://github.com/stefanobaghino/sublime-mcp/actions/workflows/harness-smoke.yml/badge.svg)](https://github.com/stefanobaghino/sublime-mcp/actions/workflows/harness-smoke.yml)

A [Sublime Text](https://www.sublimetext.com/) plugin that is also an
[MCP](https://modelcontextprotocol.io/) server, plus a stdio harness
that runs both inside Docker so an agent can drive ST without a human
in the loop.

The plugin file is single-file and standard-library only. The harness
is single-file and standard-library only. ST runs in a container with
Xvfb; the plugin's HTTP server stays on loopback inside the container,
and the harness proxies MCP between the agent (over stdio) and the
plugin (over HTTP).

## Why

Sublime Text's sublime-syntax engine is the ground truth for scopes.
When a downstream consumer (e.g. [syntect](https://github.com/trishume/syntect))
disagrees with ST, you almost always want ST's answer. Verifying "what
does ST say" manually — symlink the package, open ST, run **Tools →
Build With → Syntax Tests**, copy the output panel — is slow and
error-prone, and doesn't fit autonomous agent workflows at all.

The plugin runs inside ST's plugin host and serves a single MCP tool,
`exec_sublime_python`, which runs arbitrary Python inside ST. The
harness packages all of that — ST, Xvfb, the plugin — into a Docker
container and exposes it as a stdio MCP server an agent can register
directly.

## Requirements

- Docker (Engine or Desktop), with the daemon running.
- Python 3.10+ on the host (for the harness).
- A [Sublime Text](https://www.sublimetext.com/) license is
  recommended but not required — ST runs in evaluation mode by default
  inside the container.

## Install

```sh
git clone https://github.com/stefanobaghino/sublime-mcp
cd sublime-mcp
pipx install -e .
```

`pipx install -e .` keeps `sublime-mcp` pointing at this checkout
(the harness reads the bundled `Dockerfile`, `docker/entrypoint.sh`,
and `sublime_mcp.py` from `Path(__file__).parent`). Plain
`pip install -e .` works too if you already have a managed environment.

## Register with Claude Code

```sh
claude mcp add --scope user --transport stdio sublime-text -- \
    sublime-mcp --mount "$PWD:/work"
```

The name `sublime-text` is load-bearing: the bundled skill's
`allowed-tools` hard-codes `mcp__sublime-text__exec_sublime_python`.
Registered under a different name, the skill won't see the tool.

`--mount $PWD:/work` makes your working tree visible to ST inside the
container. Repeat the flag for additional paths. Without a mount, paths
you'd pass into `exec_sublime_python` calls won't resolve.

The first connection triggers `docker build`; expect a few minutes the
first time. Subsequent connections boot the container in a few seconds.

## Install the skill (optional, Claude Code only)

A [skill](https://docs.claude.com/en/docs/claude-code/skills) is bundled
at [`skills/sublime-mcp/`](./skills/sublime-mcp/SKILL.md) with workflow
guidance — when to reach for `scope_at` vs `scope_at_test`, how to
branch on `run_syntax_tests` summary sentinels, the three-step
divergence triage for comparing another parser's output against ST.
Install by symlinking it into the user-scope skills directory:

```sh
ln -s "$PWD/skills/sublime-mcp" ~/.claude/skills/sublime-mcp
```

Or `cp -R skills/sublime-mcp ~/.claude/skills/sublime-mcp` if symlinks
misbehave on your platform.

## Verify

In a Claude Code session that has the skill loaded:

```
mcp__sublime-text__exec_sublime_python({ code: "print(sublime.version())" })
```

Returns ST's build number in `output`. From a shell, the equivalent is
`tests/test_harness_smoke.py` — boots the harness, sends the same call,
asserts the round-trip.

The tool's own `description` (readable via `tools/list`) is a cookbook
of common recipes — scope-at, run syntax tests, reload a syntax file,
list resources. Agents should read it as their primary reference.

## Harness flags

```
sublime-mcp [--mount HOST:CONTAINER] [--image-tag TAG]
            [--rebuild] [--license-file PATH]
```

- `--mount HOST:CONTAINER` (repeatable): bind-mount HOST into the
  container at CONTAINER. Recommended: `--mount $PWD:/work`.
- `--image-tag TAG`: override the image tag (default
  `sublime-mcp-harness:latest`).
- `--rebuild`: force `docker build` even if the image already exists.
- `--license-file PATH`: mount a Sublime Text license file into the
  container's `~/.config/sublime-text/Local/`.

## Multi-agent

Each agent session spawns its own harness; each harness owns its own
container. Host ports are kernel-assigned, so concurrent agents on the
same machine don't collide. `docker ps --filter
label=sublime-mcp-harness` lists the running containers; the label
value is the harness's PID.

## Tests

Three surfaces, all in CI:

- [`tests/test_smoke.py`](tests/test_smoke.py) and
  [`tests/test_helpers.py`](tests/test_helpers.py) — plugin-level tests
  running inside Sublime Text via the
  [UnitTesting](https://github.com/SublimeText/UnitTesting) package
  ([`tests.yml`](.github/workflows/tests.yml)). They cover the helper
  surface in isolation against a host ST.
- [`tests/headless_smoke.py`](tests/headless_smoke.py) — pins
  `open_view`'s headless guard against a real ST instance with no
  windows ([`headless.yml`](.github/workflows/headless.yml), macOS).
- [`tests/test_harness_smoke.py`](tests/test_harness_smoke.py) — boots
  the harness end-to-end against Docker, drives `initialize` +
  `tools/call exec_sublime_python` over stdio
  ([`harness-smoke.yml`](.github/workflows/harness-smoke.yml), Linux).

The host-ST surface (`tests.yml`, `headless.yml`) covers plugin
correctness in isolation; the harness surface covers the user-facing
path. Both are gated on PR.

## Security

- The plugin's HTTP server binds `127.0.0.1` *inside the container*,
  not on the host. The harness's port mapping uses `-p 127.0.0.1:0:…`
  so the host port is also loopback-only.
- Anyone with shell access to your machine can connect to the
  container's host port and run arbitrary Python in the ST instance —
  same blast radius as having ST open and a debugger attached.
- The container runs as root inside its own namespace; the harness
  passes through volumes you explicitly mount with `--mount`.

## Contributor: running the plugin against host ST

Useful for plugin-level work where booting Docker per change is
overkill. The host-ST install path:

```sh
ln -s "$PWD/sublime_mcp.py" \
      "$HOME/Library/Application Support/Sublime Text/Packages/User/sublime_mcp.py"
```

Open ST and look for `[sublime-mcp] listening on 127.0.0.1:47823` in
the console. The host-ST CI workflows (`tests.yml`, `headless.yml`) use
this layout. Users should not register this directly with Claude Code;
the harness is the supported user path.

## Uninstall

```sh
claude mcp remove sublime-text --scope user
pipx uninstall sublime-mcp-harness
docker image rm sublime-mcp-harness:latest
```

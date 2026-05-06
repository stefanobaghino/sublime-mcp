#!/bin/sh
# Boot Xvfb, launch Sublime Text against the workspace, then exec the
# stdio↔HTTP bridge as PID 1. The plugin_loaded() hook in plugin.py
# binds 127.0.0.1:47823 once ST starts; bridge.py proxies the parent
# `docker run -i`'s stdio to that loopback HTTP server.

set -eu

# Pick a workspace path that exists. The shim recommends
# `--mount $PWD:/work`; if the caller skipped it, fall back to /tmp so
# `subl --stay <path>` opens a window (the plugin's open_view guard
# requires len(sublime.windows()) > 0).
if [ -d /work ]; then
    WORKSPACE=/work
else
    WORKSPACE=/tmp
fi

# Xvfb on :1, matching DISPLAY in the Dockerfile.
Xvfb :1 -screen 0 1024x768x24 -nolisten tcp >/var/log/xvfb.log 2>&1 &

# Give Xvfb a moment to create the socket before ST tries to connect.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ -S /tmp/.X11-unix/X1 ]; then break; fi
    sleep 0.1
done

# `subl` re-execs as `--detached --stay <workspace>`; the launcher exits
# once ST is daemonized. Redirect stdio to a log file so that even if
# ST's daemon ever writes to its inherited streams, those bytes never
# reach PID 1's stdout (the MCP protocol channel post-exec).
subl --stay "$WORKSPACE" >>/var/log/sublime.log 2>&1

# Forward SIGTERM/SIGINT to ST + Xvfb so `docker stop` is clean.
# `pkill -f sublime_text` matches both the daemon and the plugin
# hosts; `pkill Xvfb` covers the display.
cleanup() {
    pkill -TERM -f sublime_text 2>/dev/null || true
    pkill -TERM Xvfb 2>/dev/null || true
}
trap 'cleanup; exit 0' TERM INT

# Bridge owns PID 1's stdio. It exits on stdin EOF (parent docker CLI
# died); the trap fires on `docker stop` and tears ST + Xvfb down.
exec python3 /bridge.py

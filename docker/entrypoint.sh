#!/bin/sh
# Boot Xvfb, launch Sublime Text against the workspace, and stay alive
# until the container is stopped. The plugin_loaded() hook in
# sublime_mcp.py binds 127.0.0.1:47823 once ST starts.
#
# ST self-daemonizes (re-execs as `--detached`) and the launching
# `subl` wrapper exits as soon as the daemon is up. So we cannot
# `wait` on the launcher PID to keep PID 1 alive — instead we hold
# PID 1 with a trap-friendly sleep loop. `docker stop` sends SIGTERM
# to PID 1; the trap fires, ST and Xvfb get SIGTERM, and the
# container exits.

set -eu

# Pick a workspace path that exists. The harness recommends
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

# `subl` is a thin wrapper around `sublime_text --fwdargv0`. ST then
# re-execs as `--detached --stay <workspace>` and the wrapper exits.
# Run it synchronously — control returns once ST is daemonized and
# our work is to keep PID 1 alive afterwards.
#
# The plugin reads SUBLIME_MCP_HOST at module import time. Bind on
# 0.0.0.0 so Docker Desktop's userland port-forwarder (which connects
# from the bridge IP, not loopback) can reach the server. Host-side
# exposure is still loopback-only via `-p 127.0.0.1:0:47823`.
export SUBLIME_MCP_HOST=0.0.0.0
subl --stay "$WORKSPACE"

# Forward SIGTERM/SIGINT to ST + Xvfb so `docker stop` is clean.
# `pkill -f sublime_text` matches both the daemon and the plugin
# hosts; `pkill Xvfb` covers the display.
cleanup() {
    pkill -TERM -f sublime_text 2>/dev/null || true
    pkill -TERM Xvfb 2>/dev/null || true
}
trap 'cleanup; exit 0' TERM INT

# Hold PID 1 forever, in a way that lets the trap fire on signals.
# `wait` on a backgrounded sleep returns when SIGTERM interrupts it.
while :; do
    sleep 3600 &
    wait $!
done

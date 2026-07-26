#!/bin/sh
# Pre-start Sublime Text so UnitTesting's run-tests action attaches to a
# fully settled instance. On macOS, a cold ST start under run-tests
# wedges UnitTesting's schedule runner before it writes any output;
# headless.yml launches ST the same way.
set -eu

# Let any Sublime Text instance left over from the setup action finish
# shutting down before launching ours.
for i in $(seq 1 30); do
  pgrep 'sublime_text|plugin_host' >/dev/null || break
  sleep 1
done

subl --stay &

# Wait for the MCP server (ST plugin loaded) to listen.
for i in $(seq 1 60); do
  if curl -s -o /dev/null http://127.0.0.1:47823/mcp; then
    echo "plugin loaded after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "MCP bridge not up after 60s" >&2
exit 1

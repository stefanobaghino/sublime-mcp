"""Smoke test: the MCP server binds on loopback and responds to initialize.

Runs inside Sublime Text via the UnitTesting package (locally or via
SublimeText/UnitTesting GitHub Actions). The HTTP call hits the plugin's
own server running in the same ST process, so a successful round-trip
confirms both that plugin_loaded() fired and that the MCP endpoint is
reachable.
"""

import json
import urllib.error
import urllib.request

from unittesting import DeferrableTestCase


MCP_URL = "http://127.0.0.1:47823/mcp"
POLL_INTERVAL_MS = 100
POLL_ATTEMPTS = 30  # ~3 s total


class TestMCPSmoke(DeferrableTestCase):

    def test_initialize_round_trips(self):
        last_exc = None
        for _ in range(POLL_ATTEMPTS):
            try:
                body = _initialize()
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_exc = exc
                yield POLL_INTERVAL_MS
                continue
            self.assertEqual(body.get("jsonrpc"), "2.0")
            result = body.get("result") or {}
            server_info = result.get("serverInfo") or {}
            self.assertEqual(server_info.get("name"), "sublime-mcp")
            return
        raise AssertionError(
            "MCP server did not respond within %d ms: %r"
            % (POLL_INTERVAL_MS * POLL_ATTEMPTS, last_exc)
        )


def _initialize():
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "sublime-mcp-smoke-test", "version": "0"},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))

"""Real stdio MCP handshake and tool calls; isolated state, no production daemon."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

STATE = tempfile.TemporaryDirectory(prefix="tabc-mcp-stdio-")
os.environ["TABC_HOME"] = STATE.name
os.environ["TABC_DB"] = str(Path(STATE.name, "test.db"))
ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
from tabus import daemon, mcp_server as adapter, nodekey

HAS_MCP = sys.version_info >= (3, 10) and importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAS_MCP, "Install .[mcp] on Python 3.10+ for stdio tests")
class StdioTests(unittest.TestCase):
    def test_handshake_schema_and_roundtrip(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        daemon.init_extras()
        server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), daemon.BusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        adapter.BASE = f"http://127.0.0.1:{server.server_port}"
        for node in ("desktop-test", "sender-test"):
            adapter.NODE = node
            self.assertTrue(adapter._request("POST", "/register", {
                "node": node, "kind": "generic",
                "pubkey": nodekey.public_key_b58(nodekey.key_path(node))})["ok"])
        mid = adapter.tabc_send("desktop-test", "stdio", "real MCP roundtrip")["id"]

        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                args=["-m", "tabus.mcp_server"], env={
                    "PYTHONPATH": ROOT, "TABC_NODE": "desktop-test",
                    "TABC_HOME": STATE.name, "TABC_BUS_URL": adapter.BASE})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    tools = {tool.name: tool for tool in listed.tools}
                    self.assertEqual(len(tools), 10)
                    self.assertFalse(tools["tabc_who"].annotations.readOnlyHint)
                    self.assertTrue(tools["tabc_sent"].annotations.readOnlyHint)
                    self.assertFalse(tools["tabc_pull"].annotations.readOnlyHint)
                    self.assertNotIn("sender", tools["tabc_send"].inputSchema["properties"])
                    for name, arguments in (
                        ("tabc_who", {}), ("tabc_pull", {}),
                        ("tabc_open", {"message_id": mid}),
                        ("tabc_ack", {"message_id": mid}),
                        ("tabc_send", {"to": "sender-test", "subject": "reply", "body": "received"}),
                    ):
                        result = await session.call_tool(name, arguments)
                        self.assertFalse(result.isError, result)
                        self.assertNotIn("error", result.structuredContent, result)
                    invalid = await session.call_tool("tabc_dm", {"limit": 0})
                    self.assertTrue(invalid.isError)

        try:
            asyncio.run(asyncio.wait_for(exercise(), timeout=30))
            adapter.NODE = "sender-test"
            self.assertEqual(adapter.tabc_pull()["messages"][0]["body"], "received")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            STATE.cleanup()


if __name__ == "__main__":
    unittest.main()

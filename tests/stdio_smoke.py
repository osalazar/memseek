"""Host-side test of the exact local MCP launch command (requires native MCP SDK)."""
import asyncio
from pathlib import Path
import sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable,
        args=[str(root/"scripts/launch.py"), "mcp"])
    async with Client(stdio_client(params)) as client:
        result = await client.list_tools()
        tools = result.tools if hasattr(result, "tools") else result
        assert {t.name for t in tools} == {"context", "hard_rules", "preferences", "remember", "record", "learn_style", "styles"}
    print("PASS: local stdio launcher, MCP handshake and seven tools.")

if __name__ == "__main__":
    asyncio.run(main())

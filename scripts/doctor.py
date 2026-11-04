"""Read-only health/auth/catalog/MCP checks. Does not call a model or print credentials."""
import asyncio
import os
from pathlib import Path

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    url = os.environ.get("MEMSEEK_URL", "http://api:8000").rstrip("/")
    key = Path(os.environ.get("MEMSEEK_KEY_FILE", "/state/api_key")).read_text().strip()
    async with httpx.AsyncClient(base_url=url, timeout=15) as client:
        (await client.get("/health")).raise_for_status()
        assert (await client.get("/tools")).status_code == 401, "API must reject missing auth"
        assert (await client.post("/mcp", json={})).status_code == 401, "MCP must reject missing auth"
        tools = await client.get("/tools", headers={"Authorization": f"Bearer {key}"})
        tools.raise_for_status()
        assert tools.json()["package"]["name"] == "personal_context"
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
        async with Client(streamable_http_client(url+"/mcp", http_client=http, terminate_on_close=False)) as mcp:
            tools = await mcp.list_tools()
            names = {t.name for t in tools.tools} if hasattr(tools, "tools") else {t.name for t in tools}
            assert names == {"context", "hard_rules", "preferences", "remember", "record", "learn_style", "styles", "people", "friendship_suggestions", "extended_context"}, names
            result = await mcp.call_tool("hard_rules", {"owner": os.environ.get("MEMSEEK_OWNER", "owner"), "now": 0})
            assert not result.is_error, result
    mode = "FAKE: infrastructure only" if os.environ.get("LLM_FAKE", "0").lower() in {"1", "true"} else "real provider mode"
    print(f"PASS: API, auth rejection, native MCP handshake/list/call, catalog ({mode}).")
    print("Worker/extraction are tested separately by launch.py live-test. No personal data was written.")

if __name__ == "__main__":
    asyncio.run(main())

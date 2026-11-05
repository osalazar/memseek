"""Context paging through the HTTP API and the MCP tool.

``offset=40`` is the continuation the context block reports as next_offset.
It must be accepted even when the stored artifact only declared ``*_offset``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "memseek" / "src"))

FOREVER = 253402300799


def _pref(index: int) -> dict:
    text = f"For the client email, keep rule {index:02d}."
    return {
        "collection": "preferences",
        "type": "preference",
        "key": f"hard-{index:02d}",
        "text": text,
        "content": {
            "text": text,
            "activation": "confirmed",
            "strength": "hard",
            "priority": 10,
            "actions": ["any"],
            "channels": ["any"],
            "audiences": ["any"],
            "recipients": ["any"],
            "domains": ["any"],
            "valid_until": FOREVER,
        },
    }


class AdvertisedContextSchema(unittest.TestCase):
    def test_tool_schema_exposes_offset_without_catalog_params(self):
        from memseek.artifacts import artifact_parameter_schema
        from memseek.mcp_server import _as_mcp_tool

        schema = artifact_parameter_schema({})
        for name in (
            "offset",
            "hard_offset",
            "soft_offset",
            "context_offset",
            "people_offset",
            "style_offset",
            "extended_offset",
        ):
            field = schema["properties"][name]
            self.assertEqual(field["type"], "integer")
            self.assertEqual(field["minimum"], 0)
            self.assertIsInstance(field["minimum"], int)
        tool = _as_mcp_tool(
            {
                "name": "context",
                "kind": "artifact",
                "description": "Bounded context for one action.",
                "input_schema": {
                    "type": "object",
                    "properties": {"owner": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        )
        self.assertIn("offset", tool.input_schema["properties"])
        self.assertIn("context_offset", tool.input_schema["properties"])
        self.assertIn("next_offset", tool.description or "")
        self.assertIn("hard_offset", tool.description or "")

    def test_extended_context_schema_lists_paging_and_a_cut_page_continues(self):
        from memseek.mcp_server import _as_mcp_tool
        from memseek.search.pagination import fits_in_render, view_input_schema

        class _View:
            name = "extended_context"
            kind = "search"
            query = {"mode": "hybrid", "k": 6}
            parameters: dict = {}

        schema = view_input_schema(_View())
        for name in ("limit", "offset", "snapshot"):
            self.assertIn(name, schema["properties"])
        self.assertIs(schema["additionalProperties"], False)
        tool = _as_mcp_tool(
            {
                "name": "extended_context",
                "kind": "view",
                "description": "Retrieve owner-endorsed extended context.",
                "input_schema": {
                    "type": "object",
                    "properties": {"owner": {"type": "string"}, "task": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        )
        for name in ("limit", "offset", "snapshot"):
            self.assertIn(name, tool.input_schema["properties"])
        self.assertIs(tool.input_schema["additionalProperties"], False)
        self.assertIn("next_offset", tool.description or "")
        # 126 records do not fit. The page keeps a prefix and the rest is the next page.
        kept = fits_in_render([400] * 126, budget=1000, overhead=100)
        self.assertGreater(kept, 0)
        self.assertLess(kept, 126)
        has_more = kept < 126
        rendered_truncated = False
        self.assertTrue(has_more)
        self.assertFalse(rendered_truncated and not has_more)


class ContextOffsetPaging(unittest.TestCase):
    def test_http_and_mcp_offset_returns_page_two(self):
        if not os.environ.get("DATABASE_URL"):
            self.skipTest("DATABASE_URL is required")
        url = os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000")
        os.chdir(ROOT / "vendor" / "memseek")
        asyncio.run(self._run(url))

    async def _run(self, url: str) -> None:
        import httpx
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from memseek.auth import create_workspace
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from memseek.sdk import MemseekClient

        async with httpx.AsyncClient(timeout=5) as probe:
            health = await probe.get(url + "/health")
            if health.status_code != 200:
                self.skipTest("api is not up")
        workspace = "paging-" + uuid4().hex[:12]
        owner = "paging-owner"
        async with pool_lifespan(Settings()) as pool:
            credential = await create_workspace(pool, workspace)
        body = {
            "owner": owner,
            "now": int(time.time()),
            "task": "email the client",
            "actions": ["communicate"],
            "channels": ["email"],
            "audiences": ["client"],
            "recipients": ["any"],
            "domains": ["any"],
            "visibility": ["private", "shareable"],
        }
        async with MemseekClient(url, credential.api_key) as mem:
            await mem.catalog.publish(package="personal_context@0.5.0", directory=str(ROOT / "catalog"))
            inserted = []
            for start in range(0, 45, 20):
                result = await mem.records.ingest_many([_pref(index) | {"entity": owner} for index in range(start, min(start + 20, 45))])
                inserted.extend(item["id"] for item in result["inserted"])
            deadline = time.monotonic() + 90
            for record_id in inserted:
                while not (await mem.record(record_id))["ready"]:
                    if time.monotonic() > deadline:
                        self.fail("rules were not embedded")
                    await asyncio.sleep(0.25)
            headers = {"Authorization": "Bearer " + credential.api_key}
            async with httpx.AsyncClient(base_url=url, headers=headers, timeout=60) as http:
                first = await http.post("/artifacts/interaction_context@5/render", json=body)
                self.assertEqual(first.status_code, 200, first.text)
                page1 = first.json()["manifest"]["blocks"]["hard"]
                self.assertTrue(page1["has_more"])
                self.assertEqual(page1["next_offset"], 40)
                second = await http.post(
                    "/artifacts/interaction_context@5/render",
                    json={**body, "offset": 40},
                )
                self.assertEqual(second.status_code, 200, second.text)
                page2 = second.json()["manifest"]["blocks"]["hard"]
                self.assertEqual(page2["offset"], 40)
                self.assertFalse(page2["has_more"])
                self.assertEqual(len(page2["ids"]), 5)
                self.assertFalse(set(page1["ids"]) & set(page2["ids"]))
            async with httpx2.AsyncClient(headers=headers, timeout=60) as transport:
                async with Client(
                    streamable_http_client(url + "/mcp", http_client=transport, terminate_on_close=False)
                ) as client:
                    tools = (await client.list_tools()).tools
                    context = next(tool for tool in tools if tool.name == "context")
                    properties = context.input_schema["properties"]
                    for name in (
                        "offset",
                        "hard_offset",
                        "soft_offset",
                        "context_offset",
                        "people_offset",
                        "style_offset",
                        "extended_offset",
                    ):
                        self.assertIn(name, properties)
                    self.assertIn("next_offset", context.description or "")
                    for name in ("hard_rules", "preferences", "people"):
                        schema = next(tool.input_schema for tool in tools if tool.name == name)
                        self.assertIn("offset", schema["properties"], name)
                    result = await client.call_tool("context", {**body, "offset": 40})
                    self.assertFalse(result.is_error, str(result))
                    payload = result.structured_content or json.loads(
                        next(block.text for block in result.content if block.type == "text")
                    )
                    mcp_page = payload["manifest"]["blocks"]["hard"]
                    self.assertEqual(mcp_page["ids"], page2["ids"])

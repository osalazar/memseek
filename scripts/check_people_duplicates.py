#!/usr/bin/env python3
"""Count archivist candidates that copy a reviewed person. Read-only.

A copy is a ``people`` candidate whose folded full name or alias equals a
reviewed person's name or alias in the same workspace and entity. The script
prints that count. It does not delete or update any row.

    python3 scripts/check_people_duplicates.py --direct --owner OWNER
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

_PEOPLE_SQL = """
    select row.entity, row.id::text as id, row.content
    from record row
    where row.workspace = %s
      and (%s::text is null or row.entity = %s)
      and row.collection = 'people'
      and row.status = 'active'
      and not coalesce((row.content->>'tombstone')::boolean, false)
      and (
        row.key is null or not exists (
          select 1 from record newer
          where newer.workspace = row.workspace
            and newer.entity = row.entity
            and newer.collection = row.collection
            and newer.key = row.key
            and newer.status = row.status
            and newer.seq > row.seq
        )
      )
"""


def count_duplicate_candidates(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How many archivist rows copy a reviewed person in the same entity."""

    from memseek.search.names import name_identity
    from memseek.search.recall_rank import _owner_reviewed

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("entity") or ""), []).append(row)
    duplicates = 0
    for group in grouped.values():
        reviewed: set[str] = set()
        for row in group:
            content = row.get("content") or {}
            if _owner_reviewed(content):
                reviewed.update(name_identity(content))
        for row in group:
            content = row.get("content") or {}
            if _owner_reviewed(content) or content.get("source") != "archivist-file":
                continue
            if name_identity(content) & reviewed:
                duplicates += 1
    return {"duplicates": duplicates}


async def fetch_people(pool: Any, *, workspace: str, owner: str | None) -> list[dict[str, Any]]:
    """Current people rows. ``owner`` None reads every entity in the workspace."""

    async with pool.connection() as conn:
        result = await conn.execute(_PEOPLE_SQL, [workspace, owner, owner])
        loaded = await result.fetchall()
    rows: list[dict[str, Any]] = []
    for item in loaded:
        content = item["content"] if isinstance(item["content"], dict) else {}
        rows.append({"entity": str(item["entity"]), "id": str(item["id"]), "content": content})
    return rows


async def reviewed_name_keys(pool: Any, *, workspace: str, owner: str) -> set[str]:
    """Folded names and aliases of reviewed people for one owner."""

    from memseek.search.names import name_identity
    from memseek.search.recall_rank import _owner_reviewed

    keys: set[str] = set()
    for row in await fetch_people(pool, workspace=workspace, owner=owner):
        content = row["content"]
        if _owner_reviewed(content):
            keys.update(name_identity(content))
    return keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", action="store_true", help="read DATABASE_URL; never writes")
    parser.add_argument("--workspace", default=os.environ.get("MEMSEEK_WORKSPACE", "personal"))
    parser.add_argument("--owner", default="", help="entity to check; omit for every entity")
    args = parser.parse_args(argv)
    if not args.direct:
        parser.error("pass --direct; this check only reads the database")

    from direct_runtime import prepare_engine, workspace_name

    prepare_engine()
    workspace = workspace_name(args.workspace)
    owner = args.owner.strip() or None

    async def run() -> dict[str, int]:
        from memseek.config import Settings
        from memseek.db import pool_lifespan

        async with pool_lifespan(Settings()) as pool:
            rows = await fetch_people(pool, workspace=workspace, owner=owner)
        return count_duplicate_candidates(rows)

    report = asyncio.run(run())
    report["workspace"] = workspace
    report["owner"] = owner or "*"
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

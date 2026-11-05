#!/usr/bin/env python3
"""Embed existing rules, and any people still missing a vector, in small slices.

The worker crashed on 31 Oct when a backlog kept it busy. This script therefore
asks for one short backfill at a time (``max_rows``, default 32), waits until
that slice is done, then sleeps before the next slice. Selection is presence
based, so stopping and starting again does not redo a row that already has the
embedding annotation. A 409 means a slice is already live; the script attaches
to it instead of starting a second one.

Default targets, after personal_context@0.5.0 is published:

* ``preferences@1`` / ``embedding_rules`` (the rules that have no vectors today)
* ``people@1`` / ``embedding_people`` (confirmed people are already embedded;
  this finishes candidates and anything the loader left behind)

Cost estimate for ``text-embedding-3-small`` at the published OpenAI rate of
$0.02 per 1,000,000 input tokens (https://platform.openai.com/docs/pricing,
the small embedding model). Rules are short (catalog max 450 characters,
about 80 tokens). A people line is longer (about 140 tokens). 543 rules and
2,300 people are about 365,000 tokens, about $0.008. Query embeddings at read
time are a few hundred tokens per context call and are not part of this
backfill. The figure is an estimate: the provider bills the tokens it receives.

The API key is read from ``MEMSEEK_API_KEY`` or ``MEMSEEK_KEY_FILE`` and is
never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Published list price for text-embedding-3-small input tokens.
USD_PER_MILLION = 0.02
DEFAULT_TARGETS = (
    ("preferences", 1, "embedding_rules", 543, 80),
    ("people", 1, "embedding_people", 22, 140),
    ("people", 2, "embedding_people", 2300, 140),
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def estimate_cost(targets: list[tuple[str, int, str, int, int]] | None = None) -> dict:
    """Return the token and dollar estimate for the default corpus sizes."""

    chosen = list(targets or DEFAULT_TARGETS)
    rows = []
    tokens = 0
    for collection, version, processor, count, per_row in chosen:
        row_tokens = count * per_row
        tokens += row_tokens
        rows.append(
            {
                "collection": collection,
                "version": version,
                "processor": processor,
                "rows": count,
                "assumed_tokens_per_row": per_row,
                "tokens": row_tokens,
            }
        )
    return {
        "model": "text-embedding-3-small",
        "usd_per_million_input_tokens": USD_PER_MILLION,
        "targets": rows,
        "tokens": tokens,
        "usd": round(tokens / 1_000_000 * USD_PER_MILLION, 4),
        "note": (
            "Estimate from assumed tokens per row, not a provider invoice. "
            "Already-embedded rows are skipped, so a second run costs less."
        ),
    }


def _api_key() -> str:
    key = os.environ.get("MEMSEEK_API_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("MEMSEEK_KEY_FILE", "")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    raise SystemExit("Set MEMSEEK_API_KEY or MEMSEEK_KEY_FILE")


def _request(url: str, key: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": "http", "detail": raw}
        return exc.code, payload


def _live_id(url: str, key: str, collection: str, version: int, processor: str, detail: str) -> str | None:
    match = _UUID.search(detail or "")
    if match:
        return match.group(0)
    status, payload = _request(url, key, "GET", "/backfill")
    if status != 200:
        return None
    for item in payload.get("backfills") or []:
        if (
            item.get("collection") == collection
            and item.get("version") == version
            and item.get("processor") == processor
            and item.get("state") in {"queued", "running"}
        ):
            return str(item["id"])
    return None


def run_target(
    url: str,
    key: str,
    *,
    collection: str,
    version: int,
    processor: str,
    max_rows: int,
    pause: float,
    poll: float,
) -> dict:
    """Drive one target to completion, one gentle slice at a time."""

    slices = 0
    annotated = 0
    scanned = 0
    while True:
        status, payload = _request(
            url,
            key,
            "POST",
            "/backfill",
            {
                "collection": collection,
                "version": version,
                "processor": processor,
                "max_rows": max_rows,
            },
        )
        if status == 409 and payload.get("error") == "backfill_exists":
            backfill_id = _live_id(url, key, collection, version, processor, str(payload.get("detail") or ""))
            if backfill_id is None:
                raise SystemExit(f"backfill already running and its id could not be read: {payload}")
        elif status not in {200, 202}:
            raise SystemExit(f"POST /backfill failed ({status}): {payload}")
        else:
            backfill_id = str(payload["id"])
        handle = _wait(url, key, backfill_id, poll)
        slices += 1
        annotated += int(handle.get("annotated") or 0)
        scanned += int(handle.get("scanned") or 0)
        if handle.get("state") == "failed":
            raise SystemExit(f"backfill {backfill_id} failed: {handle.get('last_error')}")
        # An empty slice means nothing lacked the annotation. Stop.
        if int(handle.get("scanned") or 0) == 0 and int(handle.get("annotated") or 0) == 0:
            break
        # A short slice that did not fill the budget is also finished: the
        # cursor walked off the end of the missing rows.
        if int(handle.get("scanned") or 0) < max_rows:
            break
        if pause:
            time.sleep(pause)
    return {
        "collection": collection,
        "version": version,
        "processor": processor,
        "slices": slices,
        "scanned": scanned,
        "annotated": annotated,
    }


def _wait(url: str, key: str, backfill_id: str, poll: float) -> dict:
    while True:
        status, payload = _request(url, key, "GET", f"/backfill/{backfill_id}")
        if status != 200:
            raise SystemExit(f"GET /backfill/{backfill_id} failed ({status}): {payload}")
        if payload.get("state") in {"done", "failed", "cancelled"}:
            return payload
        time.sleep(poll)


async def _enqueue(pool, workspace: str, *, collection: str, version: int, processor: str, max_rows: int) -> dict:
    from memseek.backfill import BackfillError, list_backfills, request_backfill
    from memseek.definitions import load_definition_catalog
    from memseek.workspace_catalog import WorkspaceCatalogRegistry
    from memseek.config import Settings

    settings = Settings()
    registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
    catalog = await registry.get(workspace)
    try:
        handle = await request_backfill(
            pool,
            workspace=workspace,
            collection=collection,
            version=version,
            processor=processor,
            catalog=catalog,
            max_rows=max_rows,
        )
    except BackfillError as exc:
        if exc.code != "backfill_exists":
            raise SystemExit(f"{exc.code}: {exc.detail}") from exc
        found = await list_backfills(pool, workspace=workspace)
        handle = next(
            (
                item
                for item in found
                if item.collection == collection
                and item.collection_version == version
                and item.processor == processor
                and item.live
            ),
            None,
        )
        if handle is None:
            raise SystemExit(exc.detail) from exc
    return handle.as_json()


def enqueue_direct(
    workspace: str,
    *,
    collection: str,
    version: int,
    processor: str,
    max_rows: int,
) -> dict:
    """Register one backfill slice through the database. Does not wait."""

    import asyncio

    from direct_runtime import prepare_engine

    prepare_engine()

    async def run() -> dict:
        from memseek.config import Settings
        from memseek.db import pool_lifespan

        settings = Settings()
        async with pool_lifespan(settings) as pool:
            return await _enqueue(
                pool,
                workspace,
                collection=collection,
                version=version,
                processor=processor,
                max_rows=max_rows,
            )

    return asyncio.run(run())


def run_target_direct(
    workspace: str,
    *,
    collection: str,
    version: int,
    processor: str,
    max_rows: int,
    pause: float,
    poll: float,
) -> dict:
    """Same gentle slice loop as the HTTP client, using the database."""

    import asyncio

    from direct_runtime import prepare_engine

    prepare_engine()

    async def run() -> dict:
        from memseek.backfill import get_backfill
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from uuid import UUID

        settings = Settings()
        slices = annotated = scanned = 0
        async with pool_lifespan(settings) as pool:
            while True:
                payload = await _enqueue(
                    pool,
                    workspace,
                    collection=collection,
                    version=version,
                    processor=processor,
                    max_rows=max_rows,
                )
                backfill_id = UUID(str(payload["id"]))
                while True:
                    handle = await get_backfill(pool, workspace=workspace, backfill_id=backfill_id)
                    if handle.state in {"done", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(poll)
                body = handle.as_json()
                slices += 1
                annotated += int(body.get("annotated") or 0)
                scanned += int(body.get("scanned") or 0)
                if body.get("state") == "failed":
                    raise SystemExit(f"backfill {backfill_id} failed: {body.get('last_error')}")
                if int(body.get("scanned") or 0) == 0 and int(body.get("annotated") or 0) == 0:
                    break
                if int(body.get("scanned") or 0) < max_rows:
                    break
                if pause:
                    await asyncio.sleep(pause)
        return {
            "collection": collection,
            "version": version,
            "processor": processor,
            "slices": slices,
            "scanned": scanned,
            "annotated": annotated,
        }

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--max-rows", type=int, default=32, help="rows per worker slice")
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between slices")
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="collection@version/processor, repeatable. Default: rules then people.",
    )
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--direct", action="store_true", help="use DATABASE_URL, no API key")
    parser.add_argument("--workspace", default=os.environ.get("MEMSEEK_WORKSPACE", "personal"))
    args = parser.parse_args(argv)
    if args.max_rows < 1:
        parser.error("--max-rows must be positive")
    targets = []
    if args.target:
        for raw in args.target:
            collection, _, rest = raw.partition("@")
            version, _, processor = rest.partition("/")
            if not collection or not version.isdigit() or not processor:
                parser.error(f"--target must look like preferences@1/embedding_rules, got {raw!r}")
            targets.append((collection, int(version), processor))
    else:
        targets = [(name, version, processor) for name, version, processor, _count, _tokens in DEFAULT_TARGETS]
    estimate = estimate_cost()
    print(json.dumps({"estimate": estimate}, indent=2))
    if args.estimate_only:
        return 0
    if args.direct:
        reports = []
        for collection, version, processor in targets:
            print(f"backfill {collection}@{version}/{processor}", file=sys.stderr)
            reports.append(
                run_target_direct(
                    args.workspace,
                    collection=collection,
                    version=version,
                    processor=processor,
                    max_rows=args.max_rows,
                    pause=args.pause,
                    poll=args.poll,
                )
            )
            if args.pause:
                time.sleep(args.pause)
        print(json.dumps({"backfills": reports}, indent=2))
        return 0
    key = _api_key()
    reports = []
    for collection, version, processor in targets:
        print(f"backfill {collection}@{version}/{processor}", file=sys.stderr)
        reports.append(
            run_target(
                args.url,
                key,
                collection=collection,
                version=version,
                processor=processor,
                max_rows=args.max_rows,
                pause=args.pause,
                poll=args.poll,
            )
        )
        if args.pause:
            time.sleep(args.pause)
    print(json.dumps({"backfills": reports}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

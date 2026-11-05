#!/usr/bin/env python3
"""Publish a catalog directory.

Over HTTP (needs ``MEMSEEK_API_KEY``)::

    python3 scripts/publish_catalog.py --package personal_context@0.5.0 --directory catalog --dry-run
    python3 scripts/publish_catalog.py --package personal_context@0.5.0 --directory catalog

Inside the api container, with no API key (``DATABASE_URL`` is already set)::

    python3 /app/scripts/publish_catalog.py --direct --workspace personal \\
        --package personal_context@0.5.0 --directory /app/catalog --dry-run
    python3 /app/scripts/publish_catalog.py --direct --workspace personal \\
        --package personal_context@0.5.0 --directory /app/catalog

``--dry-run`` plans the publish and writes nothing. ``--direct`` calls the same
install path as ``POST /catalog``. Roll back by pointing ``--directory`` at
``rollback/catalog-0.4.0`` and ``--package personal_context@0.4.0``.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def catalog_files(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise SystemExit(f"catalog directory does not exist: {directory}")
    files: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.suffix not in {".yaml", ".yml"} or not path.is_file():
            continue
        files[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
    if not files:
        raise SystemExit(f"no YAML files in {directory}")
    return files


def _api_key() -> str:
    key = os.environ.get("MEMSEEK_API_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("MEMSEEK_KEY_FILE", "")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    raise SystemExit("Set MEMSEEK_API_KEY or MEMSEEK_KEY_FILE")


def publish(url: str, key: str, package: str, directory: Path, *, dry_run: bool) -> tuple[int, dict]:
    files = catalog_files(directory)
    body = json.dumps({"package": package, "files": files}).encode("utf-8")
    query = "?dry_run=true" if dry_run else ""
    request = urllib.request.Request(
        url.rstrip("/") + "/catalog" + query,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": "http", "detail": raw}
        return exc.code, payload


def publish_direct(workspace: str, package: str, directory: Path, *, dry_run: bool) -> dict:
    """Install or preflight through the engine's database connection."""

    import asyncio

    from direct_runtime import prepare_engine, workspace_name

    directory = directory.resolve()
    files = catalog_files(directory)
    prepare_engine()

    async def run() -> dict:
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from memseek.definitions import load_definition_catalog
        from memseek.workspace_catalog import WorkspaceCatalogRegistry, WorkspaceCatalogRequest

        settings = Settings()
        request = WorkspaceCatalogRequest(package=package, files=files)
        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            if dry_run:
                report, *_rest = await registry.preflight(workspace_name(workspace), request)
                return report.as_json()
            result = await registry.install(workspace_name(workspace), request)
            return result.as_json()

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="for example personal_context@0.5.0")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--direct", action="store_true", help="use DATABASE_URL, no API key")
    parser.add_argument("--workspace", default=os.environ.get("MEMSEEK_WORKSPACE", "personal"))
    parser.add_argument("--url", default=os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args(argv)
    directory = args.directory
    if not directory.is_absolute():
        directory = (Path.cwd() / directory).resolve()
    if args.direct:
        payload = publish_direct(args.workspace, args.package, directory, dry_run=args.dry_run)
        print(json.dumps(payload, indent=2))
        blockers = payload.get("blockers") or []
        return 0 if not blockers else 1
    status, payload = publish(
        args.url, _api_key(), args.package, directory, dry_run=args.dry_run
    )
    print(json.dumps(payload, indent=2))
    return 0 if status in {200, 202} else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""The new code must load the stored 0.4.0 catalog at its original hash."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "memseek" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

STORED_HASH = "0858fa5c95586ea26a6418ed8f7f9cde22f4f9d5b18799b22b901df5a731a3dc"


def _files(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.yaml"))
    }


class StoredCatalogHash(unittest.TestCase):
    def test_rollback_catalog_keeps_the_live_hash_and_050_dry_run_publishes(self):
        if not os.environ.get("DATABASE_URL"):
            self.skipTest("DATABASE_URL is required")
        os.chdir(ROOT / "vendor" / "memseek")
        from memseek.auth import create_workspace
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from memseek.definitions import load_definition_catalog
        from memseek.workspace_catalog import (
            WorkspaceCatalogRegistry,
            WorkspaceCatalogRequest,
            _compile_overlay,
        )

        import asyncio

        settings = Settings(llm_fake=True)
        compiled = _compile_overlay(settings, _files(ROOT / "rollback" / "catalog-0.4.0"))
        self.assertEqual(compiled.catalog_hash, STORED_HASH)
        recall = next(view for view in compiled.views.values() if view.kind == "search")
        self.assertNotIn("recall", recall.model_dump(mode="json"))

        workspace = "hash-" + uuid.uuid4().hex[:12]

        async def run():
            async with pool_lifespan(settings) as pool:
                await create_workspace(pool, workspace)
                registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
                installed = await registry.install(
                    workspace,
                    WorkspaceCatalogRequest(
                        package="personal_context@0.4.0",
                        files=_files(ROOT / "rollback" / "catalog-0.4.0"),
                    ),
                )
                self.assertEqual(installed.catalog_hash, STORED_HASH)
                report, *_rest = await registry.preflight(
                    workspace,
                    WorkspaceCatalogRequest(
                        package="personal_context@0.5.0",
                        files=_files(ROOT / "catalog"),
                    ),
                )
                self.assertTrue(report.publishable, report.as_json())
                self.assertEqual(report.blockers, ())
                await registry.install(
                    workspace,
                    WorkspaceCatalogRequest(
                        package="personal_context@0.5.0",
                        files=_files(ROOT / "catalog"),
                    ),
                )
                return workspace

        workspace = asyncio.run(run())
        import load_people
        import backfill_embeddings
        from memseek.backfill import cancel_backfill
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from uuid import UUID

        text = "Cristian Alcívar|HOLE|friend|P1|ES\n"
        first = load_people.load_direct(
            text, workspace=workspace, owner="owner", source="stdin",
            dry_run=False, batch_size=20, pause=0,
        )
        second = load_people.load_direct(
            text, workspace=workspace, owner="owner", source="stdin",
            dry_run=False, batch_size=20, pause=0,
        )
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["duplicates"], 0)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 1)
        loaded, _notes = load_people.parse_people(text)
        self.assertIn("(unreviewed)", loaded[0]["text"])
        self.assertEqual(loaded[0]["content"]["source"], "archivist-file")

        handle = backfill_embeddings.enqueue_direct(
            workspace, collection="preferences", version=1, processor="embedding_rules", max_rows=1,
        )
        self.assertTrue(handle.get("id"))

        async def cancel():
            async with pool_lifespan(Settings(llm_fake=True)) as pool:
                try:
                    await cancel_backfill(pool, workspace=workspace, backfill_id=UUID(handle["id"]))
                except Exception as exc:
                    self.assertIn(getattr(exc, "code", ""), {"not_live", "not_found"})

        asyncio.run(cancel())


class PeopleStdin(unittest.TestCase):
    def test_dry_run_reads_stdin_and_does_not_need_a_key(self):
        import io
        from unittest.mock import patch

        import load_people

        sample = "\n".join(
            [
                "Name|reach|role",
                "Cristian Alcívar|HOLE|friend",
                "Elizabeth Barba Orozco|barba@example.com|physician",
            ]
        )
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(sample)), patch("sys.stdout", out):
            code = load_people.main(["--dry-run", "--owner", "owner"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn('"source": "stdin"', text)
        self.assertIn('"parsed": 2', text)
        people, _notes = load_people.parse_people(sample)
        self.assertIn("Alcívar", people[0]["text"])
        self.assertIn("(unreviewed)", people[0]["text"])
        self.assertIs(people[0]["content"]["reviewed"], False)
        self.assertEqual(people[0]["content"]["source"], "archivist-file")


if __name__ == "__main__":
    unittest.main()

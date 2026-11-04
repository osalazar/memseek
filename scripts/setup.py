"""Idempotent native workspace creation and catalog publication. Never prints secrets."""
import asyncio
import os
from pathlib import Path
from memseek.auth import WorkspaceAlreadyExists, create_workspace, authenticate_api_key
from memseek.config import Settings
from memseek.db import pool_lifespan
from memseek.sdk import MemseekClient

async def main():
    keyfile = Path(os.environ.get("MEMSEEK_KEY_FILE", "/state/api_key"))
    workspace = os.environ.get("MEMSEEK_WORKSPACE", "personal")
    async with pool_lifespan(Settings()) as pool:
        if keyfile.exists():
            key = keyfile.read_text().strip()
            if await authenticate_api_key(pool, key) != workspace:
                raise SystemExit("Existing key does not match this database/workspace. Restore the matching configuration; no credentials were overwritten.")
        else:
            try:
                credential = await create_workspace(pool, workspace)
            except WorkspaceAlreadyExists:
                raise SystemExit("Workspace exists but key file is missing. Restore its backup; do not delete the database. See OPERATIONS.md.")
            key = credential.api_key
            fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
    async with MemseekClient(os.environ.get("MEMSEEK_URL", "http://api:8000"), key) as mem:
        await mem.catalog.publish(package="personal_context@0.4.0",
            directory=os.environ.get("MEMSEEK_CATALOG_DIR", "/app/catalog"))
    print(f"Workspace ready; native personal_context@0.4.0 published. Credential saved privately at {keyfile}.")

if __name__ == "__main__":
    asyncio.run(main())

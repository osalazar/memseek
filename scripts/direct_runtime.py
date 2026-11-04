"""Database access for the in-container scripts.

``railway ssh`` into the api service has ``DATABASE_URL`` and no workspace API
key. These helpers run the engine against that database. The process working
directory is moved to ``vendor/memseek`` so ``conf/models.yaml`` resolves the
same way the service does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def engine_root() -> Path:
    root = Path(__file__).resolve().parents[1] / "vendor" / "memseek"
    if not (root / "conf" / "models.yaml").is_file():
        raise SystemExit(f"memseek engine not found at {root}")
    return root


def prepare_engine() -> None:
    root = engine_root()
    os.chdir(root)
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def workspace_name(explicit: str | None) -> str:
    name = explicit or os.environ.get("MEMSEEK_WORKSPACE") or "personal"
    if not name.strip():
        raise SystemExit("workspace name is empty")
    return name

"""Verify the shipped source files against the manifest after extraction."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root/"MANIFEST.json").read_text())
for name, digest in manifest["files"].items():
    path = root/name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit("Missing or changed distribution file: " + name)
print(f"Verified {len(manifest['files'])} distribution files. Runtime .env/.state are not part of this manifest.")

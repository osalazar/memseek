"""Create a credential-free, integrity-manifested source distribution using an allowlist."""
import hashlib
import json
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = {"START_HERE.md", "README.md", "AGENTS.md", "LAUNCH_PROMPT.txt", "LICENSE",
       "THIRD_PARTY_NOTICES.md", "Dockerfile", "compose.yaml", "compose.https.yaml",
       ".env.example", ".gitignore", ".dockerignore"}
DIRS = {"catalog", "personal_context", "scripts", "tests", "examples", "docs", "deploy", "vendor"}
EXCLUDED = {".env", ".state", ".venv", ".git", "__pycache__", "node_modules", ".vercel", ".DS_Store"}

def main():
    files = {}
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts) or path.suffix == ".pyc":
            continue
        if relative.parts[0] not in DIRS and relative.as_posix() not in TOP:
            continue
        if path.is_symlink():
            raise SystemExit(f"Refusing symlink: {relative}")
        if not path.is_file():
            continue
        if path.name.startswith(".env") and relative.as_posix() != ".env.example":
            raise SystemExit(f"Refusing environment file: {relative}")
        data = path.read_bytes()
        # Compare against actual local secrets without printing or bundling them.
        key = os.environ.get("OPENAI_API_KEY", "")
        if key and key.encode() in data:
            raise SystemExit(f"Credential found in {relative}")
        files[relative.as_posix()] = data
    for private in [ROOT/".state/api_key", ROOT/".env"]:
        if private.exists():
            values = [private.read_text().strip()] if private.name == "api_key" else [
                line.split("=",1)[1].strip() for line in private.read_text().splitlines()
                if line.startswith(("OPENAI_API_KEY=", "POSTGRES_PASSWORD="))]
            for value in values:
                if len(value) > 15:
                    for name, data in files.items():
                        if value.encode() in data:
                            raise SystemExit(f"Local secret found in {name}")
    manifest = {"format": 1, "version": "0.2.1", "memseek_commit": "0930e1077d56d34c3ad2ccc0246fcd180b595b3b",
                "runtime_patches": ["artifact-fulltext-v1"],
                "files": {name: hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    output = ROOT/"dist";output.mkdir(exist_ok=True)
    archive = output/"memseek-personal-context-v0.2.1.zip"
    prefix = "memseek-personal-context/"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name,data in files.items():
            info = zipfile.ZipInfo(prefix+name, date_time=(2026,9,28,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
        z.writestr(prefix+"MANIFEST.json", json.dumps(manifest, indent=2)+"\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    print(f"Packaged {len(files)} files; {archive.stat().st_size:,} bytes: {archive}")
    print("SHA256 " + digest)

if __name__ == "__main__":
    main()

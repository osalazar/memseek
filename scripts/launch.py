"""Host launcher: standard Python 3 only; all Memseek dependencies live in Docker."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def init():
    state = ROOT / ".state"
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    env = ROOT / ".env"
    if env.exists():
        print("Existing .env preserved.")
        return
    text = (ROOT / ".env.example").read_text()
    text = text.replace("GENERATED_BY_INIT", secrets.token_hex(24))
    text = text.replace("HOST_UID=1000", f"HOST_UID={os.getuid() if hasattr(os, 'getuid') else 1000}")
    text = text.replace("HOST_GID=1000", f"HOST_GID={os.getgid() if hasattr(os, 'getgid') else 1000}")
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    print("Created private .env. Add OPENAI_API_KEY, then run: python3 scripts/launch.py up")

def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["inspect", "init", "up", "down", "doctor", "test", "live-test", "mcp", "status"])
    args = p.parse_args()
    if args.action == "inspect":
        from preflight import main as inspect_main
        inspect_main(); return
    if args.action == "init":
        init(); return
    if not (ROOT / ".env").exists():
        p.error("Run init and set your provider key first.")
    if args.action == "up":
        compose("up", "-d", "--build", "postgres", "api", "worker")
        compose("run", "--rm", "setup")
        compose("run", "--rm", "-T", "cli", "python", "/app/scripts/doctor.py")
    elif args.action == "down":
        compose("down")  # Intentionally keeps volumes and keys.
    elif args.action == "status":
        compose("ps", "-a")
    elif args.action == "mcp":
        compose("run", "--rm", "-T", "--no-deps", "cli", "python", "/app/scripts/mcp_stdio.py")
    elif args.action == "doctor":
        compose("run", "--rm", "-T", "cli", "python", "/app/scripts/doctor.py")
    else:
        extra = ["--live"] if args.action == "live-test" else []
        compose("run", "--rm", "-T", "cli", "python", "/app/tests/smoke.py", *extra)

if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"Launch failed ({type(exc).__name__}). See docs/OPERATIONS.md; data was preserved.", file=sys.stderr)
        sys.exit(1)

"""Read-only local setup report. Never prints credentials or calls a provider."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def value_status(value):
    if not value.strip():
        return "missing"
    if "$" in value or value.strip().lower() in {"your_api_key", "your-api-key", "replace_me", "changeme"}:
        return "requires_resolution"
    return "present_unverified"

def provider_status(root, environ):
    # Compose uses the process environment ahead of .env, including an explicitly empty value.
    if "OPENAI_API_KEY" in environ:
        return {"status": value_status(environ["OPENAI_API_KEY"]), "location": "environment:OPENAI_API_KEY"}
    path = root / ".env"
    if not path.is_file():
        return {"status": "missing", "location": None}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {"status": "unreadable", "location": ".env:OPENAI_API_KEY"}
    status = "missing"
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?OPENAI_API_KEY\s*=(.*)$", line)
        if match:
            # No expansion/evaluation. Nontrivial or multiline dotenv syntax needs inspection
            # by the launching agent; a presence check must never pretend to validate a key.
            try:
                values = shlex.split(match.group(1), comments=True, posix=True)
                status = value_status(values[0]) if len(values) == 1 else ("missing" if not values else "requires_resolution")
            except ValueError:
                status = "requires_resolution"
    return {"status": status, "location": ".env:OPENAI_API_KEY"}

def command_status(args):
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=8, check=False)
        return "available" if result.returncode == 0 else "unavailable"
    except subprocess.TimeoutExpired:
        return "timed_out"
    except OSError:
        return "unavailable"

def inspect_environment(root=ROOT, environ=None):
    environ = os.environ if environ is None else environ
    docker = shutil.which("docker") is not None
    keyfile = Path(environ.get("MEMSEEK_KEY_FILE", str(root / ".state/api_key")))
    memseek = ("present_unverified" if environ.get("MEMSEEK_API_KEY", "").strip()
               else "key_file_present_unverified" if keyfile.is_file() else "not_configured")
    return {
        "scope": "current execution environment; ownership and persistence must be established with the user",
        "python": {"version": ".".join(map(str, sys.version_info[:3])), "supported_host_version": sys.version_info >= (3, 9)},
        "docker_cli": "available" if docker else "unavailable",
        "docker_engine": command_status(["docker", "info", "--format", "{{.ServerVersion}}"]) if docker else "not_checked",
        "docker_compose": command_status(["docker", "compose", "version", "--short"]) if docker else "not_checked",
        "uv": "available" if shutil.which("uv") else "unavailable",
        "configuration": {"env_file_present": (root / ".env").is_file(),
                          "saved_onboarding_present": (root / ".state/onboarding.json").is_file()},
        "credentials": {"provider_OPENAI_API_KEY": provider_status(root, environ), "memseek": memseek},
        "provider_access": "not_tested; launch.py live-test makes paid provider calls after launch",
        "next_step": "Follow docs/ONBOARDING.md; resolve bot location, hosting, timing, access and memory import before launching.",
    }

def main():
    print(json.dumps(inspect_environment(), indent=2))

if __name__ == "__main__":
    main()

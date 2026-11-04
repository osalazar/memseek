"""Native MCP transport, with its key read privately from a file."""
import os
from pathlib import Path

os.environ["MEMSEEK_API_KEY"] = Path(os.environ.get("MEMSEEK_KEY_FILE", "/state/api_key")).read_text().strip()
os.execvp("memseek", ["memseek", "mcp"])

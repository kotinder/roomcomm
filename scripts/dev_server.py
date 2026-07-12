"""Local dev server. Run from the repo root:  python scripts/dev_server.py

Exists because plain `uvicorn app.main:app` breaks locally: the repo-local
`mcp/` folder (MCP client skill) shadows the installed `mcp` SDK. Pre-load
the real SDK with the repo root off sys.path first (same trick as
tests/conftest.py), then start uvicorn.

Sets a well-known dev admin token unless one is provided via env. Never used
in prod — the Docker image doesn't contain this file's env defaults.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_root(p: str) -> bool:
    try:
        return Path(p or ".").resolve() == ROOT
    except OSError:
        return False


_saved = [p for p in sys.path if _is_root(p)]
for _p in _saved:
    sys.path.remove(_p)
try:
    import mcp.server.fastmcp  # noqa: F401
    import mcp.server.transport_security  # noqa: F401
    import mcp.types  # noqa: F401
finally:
    sys.path[:0] = _saved or [str(ROOT)]

os.environ.setdefault("ROOMCOMM_ADMIN_TOKEN", "dev-admin-token")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("DEV_PORT", "8021"))
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)

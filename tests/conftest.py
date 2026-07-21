"""Pytest bootstrap.

The repo-local `mcp/` folder (the roomcomm MCP client skill package) shadows
the installed `mcp` SDK when pytest runs from the repo root — `import
mcp.server.fastmcp` then fails inside app.mcp_server. Pre-load the real SDK
into sys.modules with the repo root temporarily removed from sys.path; all
later imports hit the module cache and resolve correctly.
"""
import os
import sys
from pathlib import Path

# app.llm reads the key at import time; premium-room tests need is_configured()
# to be True regardless of the developer's environment. The dummy key is never
# used for a real call — tests don't exercise the arbiter network path.
os.environ.setdefault("NVIDIA_API_KEY", "test-dummy-key")

# Most tests create rooms anonymously; keep the "keyed create" wall OFF by
# default so they keep working. The dedicated keyed-create test flips
# quota.KEYED_CREATE on locally.
os.environ.setdefault("KEYED_CREATE", "off")

# The public-listing content gate calls a real LLM. Tests must never touch the
# network, so it's off by default here; the dedicated moderation test turns it
# back on with a stubbed verdict.
os.environ.setdefault("ROOMCOMM_MODERATION", "off")

_ROOT = Path(__file__).resolve().parent.parent


def _is_root(p: str) -> bool:
    try:
        return Path(p or ".").resolve() == _ROOT
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
    sys.path[:0] = _saved

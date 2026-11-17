"""Exercise the browser formatter with Node, including lossless JSON handling."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_message_json_formatter():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser formatter tests")
    subprocess.run([node, "--test", "tests/message_json.cjs"],
                   cwd=Path(__file__).resolve().parents[1], check=True,
                   capture_output=True, text=True, timeout=20)

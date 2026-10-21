"""__version__ must track pyproject.toml — the single source of truth.

The hardcoded copy in __init__ drifted once (read 0.4.0 while shipping
0.5.0); __version__ is now derived from installed metadata, and this test
fails the release if the two ever disagree again.
"""

import re
from pathlib import Path

import fastapi_mcp_neo


def test_dunder_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M)
    assert match, "pyproject.toml lost its version field"
    assert fastapi_mcp_neo.__version__ == match.group(1), (
        f"__version__ ({fastapi_mcp_neo.__version__!r}) drifted from "
        f"pyproject.toml ({match.group(1)!r}) — bump both or rely on the "
        "metadata derivation in __init__.py"
    )

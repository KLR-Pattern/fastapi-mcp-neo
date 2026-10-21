"""Examples must consume the library like an external consumer would.

This is the guardrail that keeps in-repo examples honest: they may import
only the public API surface (``from fastapi_mcp_neo import ...``). Without
it, an example quietly drifts into internal imports and stops proving that
a plain FastAPI app can wire the whole bridge from the outside.
"""

from __future__ import annotations

from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def python_sources() -> list[Path]:
    files = [
        p
        for p in sorted(EXAMPLES.rglob("*.py"))
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    ]
    assert files, "examples/ vanished — update this guard accordingly"
    return files


def test_examples_import_public_api_only():
    offenders: list[str] = []
    for path in python_sources():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if (stripped.startswith("from ") or stripped.startswith("import ")) and (
                "fastapi_mcp_neo." in stripped
            ):
                offenders.append(f"{path.relative_to(EXAMPLES)}: {stripped}")
    assert not offenders, (
        "Examples must import the public API only "
        "(from fastapi_mcp_neo import ...), not library internals:\n  "
        + "\n  ".join(offenders)
    )

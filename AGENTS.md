# Agent.md — release process and attention points

Operating instructions for agents (and humans) releasing this repo.

## Release flow

Prerequisites: on `master`, in sync with origin, working tree clean, CI green
(`gh run list`). The Unreleased section of `CHANGELOG.md` should carry the
changes you are shipping.

1. **Decide the version.** This repo is 0.x: breaking changes bump the minor
   (0.1 → 0.2 → …), additive changes may too, fixes alone patch. Look at the
   Unreleased entries: anything marked *(breaking)* forces a minor bump.
2. **Bump the version in TWO places** — this is the classic miss:

   | File | What to change |
   |---|---|
   | `pyproject.toml` | `version = "X.Y.Z"` (+ `uv lock` to refresh the self-entry) |
   | `CHANGELOG.md` | `## Unreleased` → `## X.Y.Z (YYYY-MM-DD)` |

   `__version__` needs NO hand-edit: since 0.6.0 it is **derived from
   installed package metadata** (`importlib.metadata`), so pyproject is the
   single source of truth. (0.5.0 shipped with a hardcoded `__version__`
   still at 0.4.0 — the published package reported the wrong runtime
   version; deriving removed that release step entirely.)
   `tests/guards/test_version.py` fails the suite if `__version__` and pyproject
   ever disagree again.

   Verify before committing:

   ```bash
   grep '^version' pyproject.toml
   uv run python -c "import fastapi_mcp_neo; print(fastapi_mcp_neo.__version__)"
   # both must print the same X.Y.Z
   ```

3. **Commit**: `chore(release): X.Y.Z`, one-line summary of the wave.
4. **Tag and push**:

   ```bash
   git tag vX.Y.Z
   git push origin master vX.Y.Z
   ```

5. **Publish is automatic.** The `v*` tag triggers `.github/workflows/publish.yml`:
   `uv build` → `uv publish` (token from the `PYPI_PUBLISHER` secret). No
   GitHub Release is created — the CHANGELOG is the release notes. Watch it:

   ```bash
   gh run watch $(gh run list --workflow publish.yml --limit 1 --json databaseId -q '.[0].databaseId')
   ```

6. **Verify before announcing**:

   ```bash
   curl -s https://pypi.org/pypi/fastapi-mcp-neo/json | python3 -c \
     "import sys,json; print(json.load(sys.stdin)['info']['version'])"
   pip download fastapi-mcp-neo==X.Y.Z --no-deps -d /tmp/check && rm -rf /tmp/check
   ```

   Stronger check (used for 0.4.0): run the example off the published wheel —
   from `examples/notes_oauth`, bypassing the editable source:

   ```bash
   OTEL_OTLP_ENDPOINT=http://localhost:4317 uv run --no-project \
     --with 'fastapi-mcp-neo[mcp]==X.Y.Z' \
     --with fastapi --with uvicorn --with python-dotenv --with asgi-lifespan \
     --with opentelemetry-sdk --with opentelemetry-exporter-otlp \
     python -c "import fastapi_mcp_neo; assert 'site-packages' in fastapi_mcp_neo.__file__; from app.__main__ import main; main()"
   ```

7. **After release**: new work goes under a fresh `## Unreleased` heading in
   the CHANGELOG (Added / Fixed / Changed subsections).

## Attention points

- **Release only from `master`.** Feature branches merge (fast-forward when
  linear) before any version bump.
- **Test suite layers.** `tests/unit/` pins one module without handler
  execution, `tests/integration/` goes through `RouterGraphQLHandler`,
  `tests/e2e/` drives ASGI / fastmcp clients, `tests/guards/` holds meta
  pins. Shared sample models, app factories, and protocol helpers live in
  `tests/support/` — import via `from tests.support import apps, models`
  (every directory keeps an `__init__.py`). New tests go where their
  SUBJECT lives, not where the ticket came from; wave/ticket IDs belong in
  docstrings, never in test names.
- **The tag is the trigger.** Pushing the tag publishes to PyPI — a broken
  release cannot be unpublished, only superseded by the next version. Never
  move or delete a published tag.
- **PyPI token**: `PYPI_PUBLISHER` in repo secrets. First release of a new
  project name needs an account-wide token; afterwards rotate to a
  project-scoped one. Also claim the project on PyPI after first publish.
- **`__version__` consistency** is enforced by `tests/guards/test_version.py`
  (derived from package metadata; pyproject is the single source) — the
  wheel's runtime string must match the tag.
- **CHANGELOG discipline**: entries land in Unreleased as they ship, not in
  a batch at release time; each entry says *what changed and why*, breaking
  changes are marked *(breaking)*.
- **Commit hygiene**: conventional commits (`feat:`, `fix:`, `docs:`,
  `chore(release):`); CHANGELOG hunks split into the commit they describe
  when a release groups several commits.
- **Coverage gate**: CI runs pytest with `--cov-fail-under=95` (baseline
  96.7% after the layered-suite refactor); local `uv run pytest` stays
  ungated for fast partial runs.
- **Locks**: the repo-root `uv.lock` is committed; `examples/*` and
  `comparison/bench/*` locks are ignored (they carry local path sources).
- **Known trap**: port 8020 — check `lsof -nP -iTCP:8020 -sTCP:LISTEN`
  before "the server won't start" debugging; yesterday's instance may still
  be holding it (`python -c`-launched processes dodge `pkill -f "python -m app"`).

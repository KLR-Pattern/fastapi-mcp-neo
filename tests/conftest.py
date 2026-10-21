"""Shared fixtures for the whole suite (kept deliberately tiny).

Suites with stateful or fragile fixtures (document_cache's parse/validate
counters, otel's global provider) keep them file-local on purpose.
"""

from collections.abc import AsyncIterator, Callable

import pytest
from fastapi import FastAPI

from fastapi_mcp_neo.handler import RouterGraphQLHandler


@pytest.fixture
async def make_handler() -> AsyncIterator[Callable[..., RouterGraphQLHandler]]:
    """Build RouterGraphQLHandler instances that are closed after the test.

    aclose() is a no-op on a handler whose invoker never started (both the
    HTTP client and the lifespan are created lazily), so closing every
    handler unconditionally is safe.
    """
    created: list[RouterGraphQLHandler] = []

    def _make(app: FastAPI, **kwargs) -> RouterGraphQLHandler:
        handler = RouterGraphQLHandler(app, **kwargs)
        created.append(handler)
        return handler

    yield _make
    for handler in created:
        await handler.aclose()

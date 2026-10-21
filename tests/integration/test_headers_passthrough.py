"""Per-call header passthrough at the handler altitude: config
normalization, invoker extra_headers, the execute() context chain, and
the adversarial cache/caller-isolation scenarios.

The security model under test: untrusted caller headers are filtered ONCE
at the untrusted boundary, after which ``execute(headers=...)`` carries
the caller's credentials — the SINGLE identity source (no server-side
provider). Default whitelist is ("authorization",); an explicitly empty
sequence disables forwarding.
"""

import asyncio

import pytest
from fastapi import FastAPI, Header, HTTPException
from graphql import GraphQLError

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from tests.support.apps import WHOAMI_QUERY, auth_app
from tests.support.models import NoteOut, WhoOut


class TestHandlerConfig:
    def test_normalizes_config_lowercase(self):
        handler = RouterGraphQLHandler(auth_app(), passthrough_headers=["Authorization"])
        assert handler.passthrough_headers == ("authorization",)

    def test_default_is_authorization_only(self):
        assert RouterGraphQLHandler(auth_app()).passthrough_headers == ("authorization",)

    def test_empty_sequence_disables(self):
        assert RouterGraphQLHandler(auth_app(), passthrough_headers=[]).passthrough_headers == ()

    def test_blank_entries_dropped(self):
        handler = RouterGraphQLHandler(
            auth_app(), passthrough_headers=["", "  ", "Authorization"]
        )
        assert handler.passthrough_headers == ("authorization",)


class TestInvokeExtraHeaders:
    async def test_extra_headers_reach_route(self, make_handler):
        handler = make_handler(auth_app())
        route = next(r for r in handler.routes if r.path == "/whoami")
        result = await handler.invoker.invoke(
            route, {}, extra_headers={"authorization": "Bearer jwt-alice"}
        )
        assert result == {"user": "alice"}

    async def test_extra_headers_none_is_noop(self, make_handler):
        handler = make_handler(auth_app())
        route = next(r for r in handler.routes if r.path == "/whoami")
        # No headers, no identity: no server-side credential to fall back on.
        with pytest.raises(GraphQLError) as exc:
            await handler.invoker.invoke(route, {}, extra_headers=None)
        assert exc.value.extensions["code"] == "HTTP_401"


class TestExecuteChain:
    async def test_headers_reach_route(self, make_handler):
        handler = make_handler(auth_app())
        result = await handler.execute(
            WHOAMI_QUERY, headers={"authorization": "Bearer jwt-alice"}
        )
        assert result["data"]["iam"]["whoami"] == {"user": "alice"}

    async def test_without_headers_unchanged(self, make_handler):
        handler = make_handler(auth_app())
        result = await handler.execute(WHOAMI_QUERY)
        assert result["data"]["iam"]["whoami"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_401"

    async def test_concurrent_executions_do_not_cross(self, make_handler):
        handler = make_handler(auth_app())

        async def as_user(token: str) -> str:
            r = await handler.execute(
                WHOAMI_QUERY, headers={"authorization": f"Bearer {token}"}
            )
            return r["data"]["iam"]["whoami"]["user"]

        alice, bob = await asyncio.gather(as_user("jwt-alice"), as_user("jwt-bob"))
        assert (alice, bob) == ("alice", "bob")


class TestAuthPassthroughAdversarial:
    """Reddit feedback (r/mcp): pin the auth-passthrough claim adversarially.

    1. Warm the document cache with one caller, then let a SECOND caller's
       token expire: on the shared cache-hit path the expired caller must
       fail authentication while the other still gets only their own data —
       the compile cache must never carry credentials.
    2. One document mixing an allowed endpoint with a forbidden one: assert
       BOTH the surviving data and the field-level error."""

    @staticmethod
    def _app(state: dict) -> FastAPI:
        app = FastAPI()

        @app.get("/whoami", response_model=WhoOut, tags=["iam"])
        async def whoami(
            authorization: str | None = Header(default=None),
        ) -> WhoOut:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            if token not in state["valid"]:
                raise HTTPException(status_code=401, detail="expired or invalid token")
            return WhoOut(user=state["users"][token])

        @app.get("/secrets", response_model=NoteOut, tags=["iam"])
        async def secrets(
            authorization: str | None = Header(default=None),
        ) -> NoteOut:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            if token not in state["valid"]:
                raise HTTPException(status_code=401, detail="expired or invalid token")
            if state["users"][token] != "admin":
                raise HTTPException(status_code=403, detail="admin only")
            return NoteOut(author="admin", text="42")

        return app

    @staticmethod
    def _state() -> dict:
        return {
            "valid": {"tok-alice", "tok-admin"},
            "users": {"tok-alice": "alice", "tok-admin": "admin"},
        }

    async def test_cache_hit_with_expired_token_isolates_callers(self, make_handler):
        state = self._state()
        handler = make_handler(self._app(state))
        query = "{ iam { whoami { user } } }"

        # Warm the compile cache as alice (token still valid)...
        warm = await handler.execute(query, headers={"authorization": "Bearer tok-alice"})
        assert warm["data"]["iam"]["whoami"] == {"user": "alice"}

        # ...then alice's token expires AFTER the cache was warmed.
        state["valid"].discard("tok-alice")

        # Same DOCUMENT (compile cache HIT for both), different credentials,
        # launched concurrently.
        async def as_token(token: str):
            return await handler.execute(
                query, headers={"authorization": f"Bearer {token}"}
            )

        alice, admin = await asyncio.gather(as_token("tok-alice"), as_token("tok-admin"))
        assert alice["data"]["iam"]["whoami"] is None
        assert alice["errors"][0]["extensions"]["code"] == "HTTP_401"
        assert admin["data"]["iam"]["whoami"] == {"user": "admin"}
        assert "errors" not in admin

    async def test_mixed_allowed_and_forbidden_in_one_document(self, make_handler):
        state = self._state()
        handler = make_handler(self._app(state))
        result = await handler.execute(
            "{ iam { whoami { user } secrets { text } } }",
            headers={"authorization": "Bearer tok-alice"},  # member, not admin
        )
        # The allowed sibling survives; only the forbidden field nulls itself.
        assert result["data"]["iam"]["whoami"] == {"user": "alice"}
        assert result["data"]["iam"]["secrets"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_403"
        assert len(result["errors"]) == 1

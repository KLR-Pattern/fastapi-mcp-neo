"""GitHub OAuth login flow and the session dependency."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    BASE_URL,
    GITHUB_AUTHORIZE_URL,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_TOKEN_URL,
    GITHUB_USER_URL,
    oauth_configured,
)
from app.credentials import resolve_user
from app.models import GitHubUser, TokenOut
from app.session import SESSION_COOKIE, encode_session

router = APIRouter(prefix="/auth", tags=["auth"])


async def current_user(request: Request) -> dict[str, Any] | None:
    """The caller's identity, or None when unauthenticated.

    Carriers (cookie / session-string Bearer / externally verified Bearer)
    are resolved in ``app.credentials`` — this dependency stays agnostic.
    """
    return await resolve_user(request)


async def require_user(
    user: Annotated[dict[str, Any] | None, Depends(current_user)]
) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="login required (/auth/login)")
    return user


@router.get("/login")
async def login() -> Response:
    """Start the GitHub OAuth flow."""
    if not oauth_configured():
        raise HTTPException(
            status_code=500,
            detail="GitHub OAuth not configured: set GITHUB_CLIENT_ID / "
            "GITHUB_CLIENT_SECRET in .env (see .env.example)",
        )
    state = secrets.token_urlsafe(16)
    url = (
        f"{GITHUB_AUTHORIZE_URL}"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={BASE_URL}/auth/callback"
        f"&scope=read:user"
        f"&state={state}"
    )
    response = RedirectResponse(url)
    response.set_cookie("oauth_state", state, httponly=True, max_age=600)
    return response


@router.get("/callback")
async def callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    """Exchange the OAuth code for the GitHub identity and open a session."""
    if error:
        raise HTTPException(status_code=400, detail=f"GitHub denied: {error}")
    expected = request.cookies.get("oauth_state")
    if not code or not state or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(status_code=400, detail="invalid oauth state")

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{BASE_URL}/auth/callback",
            },
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=401, detail="token exchange failed")

        user_response = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        user_response.raise_for_status()
        profile = user_response.json()

    session = encode_session(
        {
            "login": profile["login"],
            "name": profile.get("name"),
            "avatar_url": profile.get("avatar_url"),
        }
    )
    response = RedirectResponse("/auth/me")
    response.set_cookie(
        SESSION_COOKIE, session, httponly=True, max_age=24 * 3600, samesite="lax"
    )
    response.delete_cookie("oauth_state")
    return response


@router.get("/me", response_model=GitHubUser, tags=["auth"])
async def me(user: Annotated[dict[str, Any] | None, Depends(current_user)]) -> GitHubUser:
    """Who am I? (public shape; returns 401 when not logged in)"""
    if user is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return GitHubUser.model_validate(
        {k: user.get(k) for k in ("login", "name", "avatar_url")}
    )


@router.post("/token", response_model=TokenOut, tags=["auth"])
async def issue_token(user: Annotated[dict[str, Any], Depends(require_user)]) -> TokenOut:
    """Exchange the current session for a Bearer token.

    Same signed payload as the cookie — for MCP clients and other callers
    that authenticate with ``Authorization: Bearer <token>`` instead.
    """
    payload = {k: user.get(k) for k in ("login", "name", "avatar_url")}
    return TokenOut(access_token=encode_session(payload))


@router.post("/logout")
async def logout() -> Response:
    response = RedirectResponse("/")
    response.delete_cookie(SESSION_COOKIE)
    return response


HOME_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>fastapi-mcp-neo demo</title>
<style>
 body {{ font-family: -apple-system, sans-serif; max-width: 40rem;
       margin: 4rem auto; line-height: 1.6; }}
 code {{ background: #f4f4f4; padding: 0 .3rem; border-radius: 4px; }}
 li {{ margin: .3rem 0; }}
</style></head><body>
<h1>fastapi-mcp-neo demo</h1>
<p>A Notes API behind GitHub OAuth, exposed to agents via GraphQL + MCP.</p>
<ol>
 <li><a href="/auth/login">Login with GitHub</a> (needs <code>.env</code>)</li>
 <li><a href="/docs">REST docs</a> — try the protected <code>/api/notes</code></li>
 <li><a href="/graphiql">GraphiQL</a> — query
     <code>{{ notes {{ mine {{ list_notes {{ title }} }} }} }}</code> with your session cookie</li>
 <li><a href="/mcp/">MCP endpoint</a> — connect an MCP client
     (headers carry your session)</li>
</ol>
<p>OAuth configured: <b>{configured}</b></p>
</body></html>"""


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(HOME_HTML.format(configured=oauth_configured()))

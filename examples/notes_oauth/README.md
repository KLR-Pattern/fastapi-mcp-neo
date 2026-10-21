# Example: notes-oauth — GitHub OAuth + Notes

A consumer example for [fastapi-mcp-neo](../../README.md): a Notes API
behind **GitHub OAuth**, exposed to agents via **GraphQL + MCP**. The point
is the full-flow experience: you log in with GitHub, and the same identity
lets you — or an agent acting as you — query, create and delete notes.

It consumes the library exactly like an external app (public API only,
editable path dependency on the repo root) —
`tests/test_example_public_api.py` guards that boundary.

## Setup

### 1. Create the GitHub OAuth App

GitHub → Settings → Developer settings → [OAuth Apps](https://github.com/settings/developers)
→ **New OAuth App**:

| Field | Value |
|---|---|
| Application name | anything, e.g. `fastapi-mcp-neo demo` |
| Homepage URL | `http://localhost:8020` |
| Authorization callback URL | `http://localhost:8020/auth/callback` |

One app serves both the browser login and the MCP OAuth login (the proxy's
callback `/auth/callback/mcp` is a subdirectory of the registered callback,
which GitHub allows). Copy the **Client ID** and generate a **Client
Secret** — the secret is shown only once, save it immediately.

### 2. Configure and run

From the repo root:

```bash
cd examples/notes_oauth
cp .env.example .env        # then fill in the values below
uv sync
uv run python -m app        # serves http://localhost:8020
```

| `.env` key | What it is |
|---|---|
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | from the OAuth App above — drives BOTH the browser login and the MCP OAuth proxy |
| `SESSION_SECRET` | random string signing the session cookie and Bearer tokens (`openssl rand -hex 32`) |

Notes: the note store is in-memory — data resets on every restart. Leaving
GitHub creds empty still runs the app (REST/GraphQL work; the MCP endpoint
is then open instead of OAuth-protected).

### 3. Validate the full flow

1. **Login** — open http://localhost:8020/auth/ → *Login with GitHub* →
   authorize → you land on `/auth/me` showing your GitHub identity (session
   cookie set).
2. **REST** — http://localhost:8020/docs : `GET /api/notes` works with the
   cookie, returns 401 in a private window.
3. **GraphiQL** — http://localhost:8020/graphiql : your cookie is forwarded
   automatically, try:

   ```graphql
   { notes { mine { list_notes(q: "agent") { id title owner } } }
     meta { stats { notes users }
            # dict[str, Any] endpoint → JSON scalar pass-through
            overview } }
   ```

   and a mutation:

   ```graphql
   mutation { notes { mine { create_note(payload: {title: "from graphiql", body: "hi"}) { id owner } } } }
   ```

4. **MCP OAuth login (agent as you)** — the same GitHub app protects the MCP
   endpoint too: register the server **without any credentials**,

   ```bash
   claude mcp add --transport http notes-demo http://localhost:8020/mcp
   ```

   On connect, Claude Code follows the OAuth discovery (401 → protected
   resource → authorization server), opens the browser for the proxy's
   consent page → GitHub login, and stores the token itself. Afterwards the
   agent runs as YOUR GitHub identity. The three MCP tools it gets:

   | Tool | Use |
   |---|---|
   | `get_schema` | full GraphQL SDL — the data shape at a glance |
   | `graphql_query` | read-only queries (mutation documents are rejected with a hint) |
   | `graphql_mutation` | writes — create/delete notes as you |

   GitHub OAuth not configured? The endpoint is open and static headers
   work:

   ```bash
   claude mcp add --transport http notes-demo \
     --header "authorization: Bearer <token from POST /auth/token>" \
     http://localhost:8020/mcp
   ```
5. **No-credential behavior** — query `notes { mine { list_notes { id } } }`
   from a private-window GraphiQL (no cookie): the field returns null with
   `extensions.code = HTTP_401`, sibling public fields (`meta.stats`) keep
   working. An MCP client without a valid OAuth token is rejected at the
   endpoint (401 + discovery challenge) before any field runs.

### Smoke test without GitHub credentials

```bash
uv run python scripts/smoke.py
```

Mints a fake session cookie locally and walks: /graphql without cookie (401
field) → with cookie (data) → MCP over HTTP with the cookie header →
mutation → cleanup. With the MCP OAuth proxy configured, the MCP leg is
skipped (that login is interactive — use Claude Code); everything else
runs headlessly.

## Optional: OpenTelemetry tracing (Jaeger)

Spans come from fastmcp (tool level) and FastAPI >= 0.142 (route level)
natively — no instrumentation code. Opt in with a dependency group and one
env var:

```bash
docker run -d --name jaeger-smoke -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
uv sync --group otel
OTEL_OTLP_ENDPOINT=http://localhost:4317 uv run python -m app
```

Then watch http://localhost:16686 — service `notes-oauth`. Every MCP query
is one waterfall: `tools/call graphql_query > graphql.execute` with each
fanned-out route (`GET /api/notes`, …) nested underneath (the bridge
injects W3C traceparent — see [otel_smoke.md](../otel_smoke.md)).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SDK auth failed: HTTP 502 … oauth metadata` | the app isn't running on :8020 (e.g. it was launched from a terminal that since closed) — restart it |
| `Got new credentials, but … rejected them on reconnect` | usually a restart raced the token rotation — `/mcp` → re-login; restart Claude Code if it persists |
| `redirect_uri_mismatch` at GitHub | the OAuth App's callback isn't `http://localhost:8020/auth/callback` (the proxy appends `/mcp` as a subdirectory) |
| MCP tools connect but fields return `HTTP_401` | the caller's token isn't forwarded — check `passthrough_headers` in `app/main.py` still lists `authorization` |
| Notes disappeared | the store is in-memory; it resets on restart |

## How the library is wired

`app/main.py`, the wiring block at the bottom:

```python
mcp = FastAPIMCP(app, name="notes-demo", allow_mutation=True,
                exclude=["/auth/token"],   # protocol endpoint stays REST-only
                passthrough_headers=["authorization", "cookie"],
                auth=mcp_oauth.provider())  # GitHub OAuth 2.1 proxy when configured
mcp.mount_to(app, "/mcp", auth_at_root=True)  # endpoint at /mcp, OAuth routes at /
mcp.handler.mount_graphql(app)     # GraphiQL + POST /graphql
```

Everything else is a plain FastAPI app — no decorators, no model changes.
Each route carries exactly one domain tag (`notes:mine`, `meta`), giving
every field a single address in the schema; endpoint function names become
field names (`list_notes`, `create_note`, …).

Three interchangeable credential carriers reach the routes, resolved in
`app/credentials.py`: the browser's session cookie, a Bearer token minted
from it (`POST /auth/token`), and the MCP OAuth proxy's reference token
after a Claude Code login (`app/mcp_oauth.py` registers the verifier at
assembly time — route auth itself stays transport-agnostic).

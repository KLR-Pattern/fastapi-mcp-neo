# OTel smoke test runbook (an operations manual for the executing agent)

Branch: **`otel-smoke`** (based on master, adds `examples/otel_smoke.py`)

Goal: verify fastapi-mcp-neo observability — with an OpenTelemetry SDK
installed, fastmcp's tool-level spans and FastAPI ≥0.142's route-level spans
appear for free, and the bridge-injected W3C traceparent stitches the two
layers (plus the `graphql.execute` orchestration span) into **one trace**.
**All dependencies install temporarily via `uv run --with`; the project
environment is never modified.**

---

## Step 0: enter the repo and branch

```bash
cd <repo path>
git fetch
git checkout otel-smoke
git log --oneline -1   # expect: 09f0b81 docs(examples): otel_smoke ...
```

Prerequisites: uv installed; mode 2 needs Docker.

---

## Step 1 (mode 1): console output — zero external dependencies, run this first

```bash
uv run --with opentelemetry-sdk python examples/otel_smoke.py --mode console
```

**Expected output**: ends with `query result: {'success': True, ...}`,
preceded by several JSON-formatted spans (about 12 `"name"` fields).

**Check span names** (presence = pass):

- `tools/call graphql_query` — the tool-level span fastmcp emits
- `GET /things`, `fastapi.dependencies`, `fastapi.endpoint`,
  `fastapi.serialization` — FastAPI 0.142's native route-level spans
- `server/discover` / `tools/list` — MCP handshake spans
- `graphql.execute` — the bridge's own GraphQL orchestration span

**Key observation**: `GET /things` shares its `trace_id` with
`tools/call graphql_query` and `graphql.execute`, nested as
`tools/call > graphql.execute > GET /things` — the bridge injects a W3C
traceparent into the in-process ASGI call (L3, already landed), so one MCP
query is one complete waterfall.

---

## Step 2 (mode 2): Jaeger visualization — span waterfalls in the browser

### 2.1 Start Jaeger

```bash
docker run -d --name jaeger-smoke \
    -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
```

If Docker Hub pulls time out, switch to a mirror registry prefix:

```bash
docker pull docker.m.daocloud.io/jaegertracing/all-in-one:latest
docker run -d --name jaeger-smoke \
    -p 16686:16686 -p 4317:4317 \
    docker.m.daocloud.io/jaegertracing/all-in-one:latest
```

### 2.2 Run the script (OTLP export)

```bash
uv run --with opentelemetry-sdk --with opentelemetry-exporter-otlp \
    python examples/otel_smoke.py --mode otlp
```

If grpcio installs slowly, add the Tsinghua mirror env var:

```bash
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
uv run --with opentelemetry-sdk --with opentelemetry-exporter-otlp \
    python examples/otel_smoke.py --mode otlp
```

### 2.3 Verify the spans arrived (either way works)

Browser: open http://localhost:16686 → Search → pick service
`fastapi-mcp-neo-smoke` → Find Traces.

Command line (an agent can judge directly):

```bash
curl -s "http://localhost:16686/api/services"          # must contain fastapi-mcp-neo-smoke
curl -s "http://localhost:16686/api/traces?service=fastapi-mcp-neo-smoke&limit=5"
```

**Expected**: 2–3 traces. The one containing `tools/call graphql_query`
also nests `graphql.execute` and `GET /things` + three `fastapi.*` spans —
one complete waterfall (the other two are the MCP handshake's
`server/discover` / `tools/list`).

---

## Step 3: cleanup

```bash
docker stop jaeger-smoke && docker rm jaeger-smoke
```

Packages installed via `--with` vanish with uv's temporary environment —
nothing to clean up.

---

## Pass criteria summary

| Check | Pass condition |
|---|---|
| Script run | prints `query result: {...success: True...}` |
| console spans | `tools/call graphql_query` and `GET /things` appear |
| Jaeger ingestion | `/api/services` contains `fastapi-mcp-neo-smoke` |
| One tree | `tools/call > graphql.execute > GET /things` share one traceID and nest correctly |

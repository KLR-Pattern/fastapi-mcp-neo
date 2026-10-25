# fastapi-mcp-neo

[![pypi](https://img.shields.io/pypi/v/fastapi-mcp-neo.svg)](https://pypi.python.org/pypi/fastapi-mcp-neo)
[![PyPI Downloads](https://static.pepy.tech/badge/fastapi-mcp-neo/month)](https://pepy.tech/projects/fastapi-mcp-neo)

The **agent access layer for FastAPI** — agents call one `graphql_query`
tool with full composition and per-caller credentials, instead of one tool
per endpoint. The GraphQL layer + MCP server are derived from your existing
app: zero decorators, zero model changes.

```python
from fastapi import FastAPI
from fastapi_mcp_neo import FastAPIMCP

app = FastAPI()

# ... your existing routes ...

mcp = FastAPIMCP(
    app,
    name="my-app",
    mode="graphql",  # or "progressive" / "tools" — the three shapes below
)
mcp.run()  # HTTP MCP server: get_schema + graphql_query + graphql_mutation
```

**Three modes, one engine** — pick how agents talk to your app
([full comparison](#modes)):

| mode | what the agent does |
|---|---|
| `graphql` (default) | reads the schema once, then one query composes any routes — projection, aliases, one round trip |
| `progressive` | the same queries, discovery served in fragments grouped by your route **tags** (tags = "domains" in the schema) — for large apps (constant ~1.5k-token catalog at any size) |
| `tools` | one typed tool per route — plain arguments, no GraphQL to write; clients gate each tool individually. The natural landing for [fastapi_mcp migrants](#migrating-from-fastapi_mcp): the tool-per-route shape carries over, so existing permission and client setups keep working (the other modes change the tool set) |

**Contents** — [Installation](#installation) · [Why](#why) ·
[How it works](#how-it-works) · [Capability boundaries](#capability-boundaries) · [Usage](#usage) · [Agent lifecycle](#what-the-agent-sees-connection-lifecycle) · [Authentication](#authentication) ·
[Observability](#observability-opentelemetry) · [Hardening](#hardening-the-bridge) ·
[Demo](#demo) · [Development](#development) · [Status](#status)

## Installation

Requires Python >= 3.10.

```bash
uv add fastapi-mcp-neo            # core: GraphQL handler
uv add 'fastapi-mcp-neo[mcp]'     # + MCP server (fastmcp)
```

## Why

Existing FastAPI→MCP bridges map **one tool per endpoint**: dozens of tools, no
composition, whole-payload responses. fastapi-mcp-neo instead derives a **GraphQL
schema** from your routes (Apollo's "GraphQL as the MCP contract" pattern), so
agents get:

- **a constant tool set** (3 in graphql mode, up to 7 with progressive
  disclosure) — never one tool per endpoint; discovery tools and
  `graphql_query` carry the `readOnlyHint` MCP annotation,
  `graphql_mutation` the `destructiveHint`, so agents can auto-run reads
  and confirm writes
- **field-level selection** — fetch `{ id name }`, not the whole payload
- **composition** — combine several routes in one query; a failing route nulls
  only its own field
- **your docs, verbatim** — docstrings and `description=` metadata travel into
  the schema the agent reads
- **real auth** — queries run through the actual ASGI app, so `Depends`,
  middleware and headers apply; pass credentials via per-caller header
  passthrough
- **nothing disappears** — if a route works over HTTP, it stays callable
  here: untyped responses, serialization-filtered responses, unions and
  model fields whose type has no GraphQL mapping degrade to a documented
  raw-JSON field (with a startup notice naming the
  cause and the fix), and `-> None` routes become Boolean success fields —
  migration keeps its feel instead of routes silently vanishing
- **recursive data comes back complete** — selecting a recursive field
  means "the whole subtree": one level of `children` selection returns the
  tree at its true depth (the route already computed it; there is no
  invisible truncation), with your selection shape repeating per level
  (up to the unroll limit — see *Rules worth knowing*)

**Where it wins, and where it doesn't**: with many routes and long-lived
agent sessions, the constant catalog dominates (at 100 routes: ~15,400 tok
for a one-tool-per-endpoint OpenAPI bridge vs ~2,480 simple / ~1,510
progressive here — smaller at every measured size). Single-call latency is
statistically tied with the flat bridge (1.39 vs 1.41 ms p50, same client
stack), and the flat bridge needs no GraphQL mental model and works on any
OpenAPI spec — but agent turns dominate, and composition collapses turns.

### Compared to the alternatives

| Project | Tool count | Field selection | Composition | Setup |
|---|---|---|---|---|
| [FastMCP.from_openapi](https://gofastmcp.com/servers/openapi) | one per endpoint | ✗ | ✗ | none |
| **fastapi-mcp-neo** | 2-6, constant | ✓ | ✓ | none |

Full head-to-head — context-growth curves, latency, auth models, selection
guidance, all measured on one shared app: **[comparison/](./comparison/)**.

<details>
<summary><b>What the comparison measures</b> (numbers below are real, from <code>comparison/bench/results.json</code>)</summary>

One app (`bench/shared_app.py`, a notes CRUD API), wired into both bridges,
driven from two venvs on identical stacks (fastmcp 4.1.0, fastapi 0.143.0,
same in-memory client; their tool calls ride an in-process ASGI httpx
client, ours invoke routes in-process):

| Measurement | FastMCP.from_openapi | fastapi-mcp-neo |
|---|---|---|
| tool catalog, 100 routes | ~15,400 tok (grows linearly, ~153 tok/route) | ~2,480 tok simple / **~1,510 tok progressive (constant)** |
| composed task (notes+stats) | **2 tool calls = 2 agent turns**, 2.35 ms | **1 `graphql_query`**, 1.69 ms |
| same list response | 2,285 B whole payload | 2,341 B full / **610 B with field projection** |
| single trivial call (p50, same client stack, 3 runs) | 1.41 ms | 1.39 ms — statistically tied; agent turns dominate, not ms |

Honest counterexamples from the same measurements: at ~5 routes the catalog
gap is small (1,075 vs 891 tok) and progressive disclosure costs MORE up
front (1,467 tok — it pays off from ~25 routes); and `from_openapi` needs
no GraphQL mental model, and works on any OpenAPI spec in any language —
we are FastAPI-only. Every number is reproducible
(`comparison/README.md` → Reproduce); environment, versions and run counts
are recorded in `results.json`.
</details>

## How it works

No decorators, no model changes — everything is derived from the app you
already have, in three steps:

1. **Scan** — `RouterScanner` reads `app.routes`: verb, path, params,
   `response_model`, tags, docstrings.
2. **Build** — a graphql-core schema is assembled: tags become domain groups,
   endpoint function names become field names, Pydantic models become GraphQL
   types, and your documentation becomes schema descriptions.
3. **Execute** — each field's resolver calls its route in-process through the
   real ASGI app, so `Depends`, middleware and auth behave exactly as over
   HTTP. Sibling fields resolve concurrently; a failing route nulls only its
   own field.

One route, end to end:

```python
# your code — unchanged
@app.get("/products", response_model=list[ProductOut], tags=["shop:catalog"])
async def list_products(filters: Annotated[ProductFilter, Query()]) -> list[ProductOut]:
    """Browse the product catalog."""
```

```graphql
# the schema the agent discovers (excerpt)
type Query { shop: ShopQuery! }
type ShopQuery { catalog: ShopCatalogQuery! }
type ShopCatalogQuery {
  list_products(category: String, in_stock: Boolean, limit: Int = 10): [ProductOut!]
}

# what the agent asks — field-level selection, routes combined in one query
{
  shop     { catalog { list_products(in_stock: true) { name } } }
  analytics { shop_stats { revenue_cents } }
}
```

The shape follows two rules:

- **Tags group the tree** — `tags=["shop:catalog"]` answers at
  `{ shop { catalog { … } } }`. Untagged routes join the domain of their first
  path segment, so every field has a group.
- **Function names name the leaves** — `async def list_products` becomes
  `list_products`, your own vocabulary with no URL reconstruction. Two routes
  sharing a function name fail fast with `DuplicateFieldError`.

<details>
<summary><b>Rules worth knowing</b> — mutation gating &amp; ordering, recursive true-depth, the unroll limit, route filters, tag semantics</summary>

- **Writes are exposed by default** — the bridge mirrors the app, and the
  app's own auth gates them per caller (`allow_mutation=False` for a
  read-only view; write scoping via `mutation_include` is deprecated with the
  other path globs — pair tag-filtered deployments instead). `graphql_mutation` carries `destructiveHint`, so clients
  confirm writes; `graphql_query` refuses mutation documents.
- **Mutation ordering**: per the GraphQL spec, mutation fields at the ROOT
  execute serially in declaration order — with the grouped schema that means
  writes in DIFFERENT domains are ordered; writes grouped under the SAME
  domain run in parallel like query fields. When write order matters, put the
  operations in separate domains or send separate mutation documents.
- **Recursive models return their true depth.** Selecting a recursive
  field means "the whole subtree": the selection shape where you stop
  repeats to whatever depth the data has, up to the unroll limit (next
  paragraph) — the route already computed the
  full tree, and the bridge hands it over complete instead of truncating
  at the document's depth (truncation there was invisible: `children: []`
  on a leaf is indistinguishable from a cut-off subtree). `max_depth`
  still guards the document you write; per-level field filtering applies
  at every depth. The recursive type's schema description states this
  contract in one line per type, so agents discover it from the SDL
  itself.
- **The unroll limit, and what happens past it.** Unrolling is bounded by
  the process recursion budget: `sys.getrecursionlimit() // 16` levels
  (~62 under the default 1000 frames — measured at ~12 frames per served
  level, the bound keeps the deepest walk well inside the budget). Data
  no deeper than the limit returns in full and untouched; deeper data is
  served as deep as the document goes with a DEFINITE error naming the
  limit and the way out (`sys.setrecursionlimit` — a raised budget serves
  deeper trees on NEW handlers; an existing one reports the error rather
  than silently truncating against its baked-in documents). Boundaries:
  mutual recursion (`A.b: B`, `B.a: A`) and fragment-carrying templates
  are consciously left at the written depth — only direct self-reference
  (`Node.children: [Node]`) unrolls, whatever model kind carries it
  (BaseModel, TypedDict, dataclass).
- **Dynamic shapes pass through as `JSON`** — `dict`/`Any` annotations bridge
  as the `JSON` scalar in both directions (a `JSON` argument lands as the raw
  request body); untyped routes, serialization-filtered responses, unions and
  unmappable model fields degrade the same way, each with a field note and a
  startup notice naming the fix — see [Capability boundaries](#capability-boundaries). Only routes
  that cannot be called correctly at all are skipped, with a logged reason;
  `handler.skips` lists them programmatically, and `readiness()` returns the
  full skip + degradation audit for CI assertions.
- `include`/`exclude` fnmatch globs scope which routes enter the schema.
  `include_tags`/`exclude_tags` do the same over route tags: a route matches
  when ANY of its string tags matches ANY pattern (`include_tags=["iam:*"]`
  keeps `tags=["iam:users"]`), `exclude_tags` wins, untagged routes drop
  under a tag whitelist, and tag filters AND with path filters.
  `exclude_deprecated=True` drops `deprecated=True` routes the same way;
  routes that stay carry their GraphQL-native `@deprecated` mark.
- Route tags form a **domain tree** (`tags=["billing:invoice"]`); large apps
  switch to **progressive disclosure** (below).
- A lone `Annotated[FilterModel, Query()]` flattens into individual query
  arguments (FastAPI Query Parameter Models).
- Same-named Pydantic classes from different modules get qualified type names.
- **Descriptions flow into the schema** — model docstrings → type
  descriptions, `Field(description=...)` → field descriptions, endpoint
  docstrings (or `summary=`) → field descriptions,
  `Query()/Path()/Body(description=...)` → argument descriptions. They surface
  in GraphiQL hover, introspection and every MCP discovery tool.
</details>

## Capability boundaries

The bridging promise: **if a route works over HTTP, it stays callable here.**
Every route lands in one of four buckets — classified once at scan time, so
the SDL field note, the startup warning, and `readiness()` always tell the
same story.

### Structured — the default

**Inputs** — every argument shape FastAPI validates maps to a GraphQL
argument:

| Your parameter | In the schema |
|---|---|
| path param, any Starlette convertor (`{id:int}`, `{p:path}`, unicode segments) | required argument; the URL is rendered by Starlette itself, so convertor syntax survives |
| query param (`Query()`, defaults, `Optional`) | optional argument carrying its default; unset optional params are simply not sent |
| a lone `Annotated[FilterModel, Query()]` | flattens into one argument per model field, alias-aware |
| single body: `BaseModel` / `TypedDict` / `@dataclass` / `dict` / scalar / list | the whole JSON body as one argument |
| multiple body params | embedded keys — FastAPI's own `embed` rule, reproduced verbatim |
| `Depends(...)` sub-parameters | merged into the flattened argument view |
| enums, `Literal` of one scalar type | Enum type / scalar argument with an allowed-values description |
| `UUID`, `Decimal`, datetime/date/time | custom scalars (string-wire), round-tripped |
| aliases (`validation_alias`/`alias`, `AliasChoices`) | one canonical wire key (the first string choice); illegal-identifier names are sanitized for GraphQL and translated back to the wire key at call time |

**Outputs** — a typed `response_model` (or return annotation) becomes a
selectable GraphQL type: `{ id name }`, nested models, `TypedDict`s, stdlib
`@dataclass`es, lists and sets, enums, `Literal`s (enum members normalize to
their values), custom scalars (`UUID`, `Decimal`, datetime…), generics
(`Page[Item]`), constrained/bound TypeVars (they normalize to their
constraint/bound), recursive models (true depth — see above), and output
aliases (`serialization_alias` first, then `alias`).

### Raw JSON fallback — still callable, just not field-selectable

| Your route | What happens |
|---|---|
| returns `dict` / `Any` | author-declared dynamic shape → the `JSON` scalar, no sub-selection |
| no return annotation, no `response_model` | same, plus the field description and a startup notice tell you to add one |
| `response_model_exclude_unset` / `_exclude_defaults` / `_include` / `_exclude` / `by_alias=False` | filtering runs after validation, so per-field promises cannot hold; the notice names the kwarg |
| returns a union (`Item \| Error`) | the union bridges as `JSON`; nested inside a model, only that field degrades |
| a model field no GraphQL type can express (`metadata: SomeCustomClass`) | only that field bridges as `JSON` — the model keeps its structured fields; `readiness().degraded_fields` names it |
| takes a union parameter (`value: A \| B`) | the argument bridges as `JSON`; FastAPI validates whichever member arrives (a 422 surfaces as a field error) |
| an **unbound TypeVar** at a route boundary (`def get() -> BareT`) | the route degrades to `JSON` with parameterize guidance in the SDL note and the report — still callable and audited |
| an unannotated body parameter (`payload = Body(...)`) | bridges as a required `JSON` argument |

`response_model_exclude_none` stays structured — it only drops keys that are
nullable anyway. Every fallback names its cause in the field description
**and** in a startup warning (`bridged N route(s) as raw JSON …`) that says
how to get field selection back.

### Boolean success — `-> None` routes

Deletes and other side-effect calls annotated `-> None` (204-style) become
`gone(id): Boolean`: `true` on 2xx, failures surface as field errors.

### Skipped — only when the route cannot be called correctly

Skips are logged at startup with the reason and the fix; `handler.skips`
exposes them (`path`, `method`, `reason`) for CI assertions, and
`readiness()` (below) wraps them into the fuller exposure audit.

| Condition | Why / what to do |
|---|---|
| `Form()` / `File()` body | MCP tool arguments are JSON; the protocol has no file channel yet (SEP-2631 draft). The route stays available over plain HTTP |
| required header/cookie parameter | headers are not GraphQL arguments — make it optional; caller credentials ride `passthrough_headers` |
| query-parameter model mixed with plain params | FastAPI itself cannot serve that shape on the wire; move the plain params into the model |
| write verbs, when you pass `allow_mutation=False` (read-only view) | writes are exposed by default; hide them with `allow_mutation=False`, or serve writes from a separate tag-filtered deployment |
| hidden route (`include_in_schema=False`) | opt in with `include_hidden=True` |
| returns a raw `Response` (streaming, plain text) | no typed body to expose |
| response/parameter annotation with no mapping at all (`-> bytes`, a bare custom class, mixed-type `Literal`) | no per-field shape exists to degrade to — annotate with a JSON-compatible type |

`include`/`exclude` globs — and `include_tags`/`exclude_tags` — also remove
routes by configuration; that is filtering you asked for, not a skip.

<details>
<summary><b>Conventions the bridge follows (and expects)</b> — the FastAPI-side derivation rules: tags/domains, function names, descriptions, requiredness per model family, exclude/deprecated, multi-verb, naming collisions</summary>

*Derive, don't decorate* — your existing code is the contract. These are the
rules it is read by; none of them require changes, but knowing them makes
the schema predictable:

| In your code | The bridge's rule |
|---|---|
| `tags=["shop:catalog"]` | the domain tree: `shop { catalog { … } }`; `:` nests. Untagged routes join the domain of their first path segment; non-string (Enum) tags are ignored for grouping |
| endpoint function name | the GraphQL field name, your vocabulary verbatim; two same-named functions in one domain fail fast (`DuplicateFieldError`) |
| docstrings & `description=` | model docstring → type description; `Field(description=)` → field description; endpoint docstring or `summary=` → field description; `Query()/Path()/Body(description=)` → argument description |
| `deprecated=True` | GraphQL-native `@deprecated` — hidden from plain introspection, still executable; `exclude_deprecated=True` drops the route instead |
| requiredness | BaseModel fields: pydantic's `is_required()`; TypedDicts: the required-key set; dataclasses: no `default` and no `default_factory`. A dataclass factory default carries no GraphQL default — calling the factory at build time would be a side effect |
| `Field(exclude=True)` | never a GraphQL **output** field (it never serializes) but still a valid **input** field — exclude is serialization-only |
| optional `Header()` / `Cookie()` params | never GraphQL arguments: optional ones are simply not sent, REQUIRED ones skip the route — credentials ride `passthrough_headers`, not arguments |
| `@app.api_route(methods=["GET", "POST"])` | the first verb in GET/POST/PUT/PATCH/DELETE priority wins; the dropped verbs are warned about |
| sync `def` endpoints | run through Starlette's anyio threadpool — transparent to the bridge |
| `BackgroundTasks` / `Request` / `Response` params | FastAPI injections, ignored as arguments; the route stays fully functional |
| same-named model classes from different modules | qualified type names (`Item_mymod`), with a warning |
| enum member names | must be legal GraphQL identifiers (letters/digits/`_`, not digit-first) |
| `Literal` member types | one scalar family per Literal — `str`/`int`/`bool`; mixed-type or float members cannot map and skip the route |
</details>

### Readiness checklist

The same audit the startup notices come from is callable as data — which
routes the bridge would skip, which it would degrade to raw JSON, and which
model fields would degrade. One classifier backs both, so the report and
the warnings can never drift apart. Pass the same filters your deployment
uses, and `assert report.ready` in CI to pin the exposure you expect.

<details>
<summary><b>readiness() report shape</b></summary>

```python
# standalone: scan + classify only — no schema build, no MCP server
from fastapi_mcp_neo import RouterScanner

report = RouterScanner(app, include_tags=["iam:*"]).readiness()
report.ready            # False when anything is skipped or degraded
report.skips            # tuple[SkipRecord(path, method, reason, tags), ...]
report.degraded         # tuple[DegradedRecord(path, method, field_name, reason, tags), ...]
report.degraded_fields  # tuple[(Model.field, reason), ...] — unions and unmappable field types

# or over an already-built deployment (stored scan results, no re-scan)
mcp.handler.readiness()
```
</details>

## Usage

### MCP server (HTTP)

`run()` serves streamable HTTP (the only transport — the wrapped app is a
service, and per-caller credential passthrough needs an HTTP request
context). Use `mount_to(app, "/mcp")` to serve MCP on the app's own port.

```python
mcp = FastAPIMCP(
    app,
    name="my-app",
    include_tags=["iam:*"],  # keep only routes tagged iam:… (untagged drop)
    # The caller's own Authorization header travels to the routes by default;
    # an empty list disables forwarding entirely.
    # passthrough_headers=["authorization"],
)
mcp.run()  # streamable HTTP, 127.0.0.1:8000 — mcp.run(host="0.0.0.0", port=9000)
```

### Modes

Three shapes over one engine. Every mode executes through the **same
GraphQL schema and the same in-process engine** — identical types,
per-caller credentials, `Depends`/middleware, error semantics and
projection results. The mode changes only the shape the agent
interacts with:

|  | `graphql` (default) | `progressive` | `tools` |
|---|---|---|---|
| toolset | 3 tools, constant | 7 tools, constant | one tool per route |
| catalog cost | ~0.6–2.5k tok, flat | ~1.5k tok, flat | ~250 tok per route |
| discovery | the whole SDL in one shot (`get_schema`) | tag-grouped fragments on demand (a route's tags are its "domain") | typed signatures inline in every tool |
| invocation | the agent writes GraphQL documents | same documents | plain typed arguments — no GraphQL to write |
| composition | one document combines any routes, one round trip | same | one route per call; clients compose via parallel tool calls |
| field selection | selection set inside the document | same | a `fields` argument per tool |
| GraphQL literacy | required | required | none |
| client-side permissions | one gate covers everything | one gate | **per-tool gating** — allow `get_note`, block `delete_note` |

The same task in the two shapes — "note 1's title, plus the total
count":

```text
graphql / progressive                             tools
──────────────────────────────────────           ──────────────────────────────────────
get_schema                  (once)                get_note(note_id=1, fields="{ title }")
graphql_query:                                    get_stats()          (parallel calls)
  { notes { get_note(note_id: 1) { title }
            get_stats { total } } }
```

**Pick by how you control access** — that is the real axis:

- **`tools` — control by operation.** The client approves and gates each
  tool individually: hand a third-party agent a curated subset
  (`include`/`exclude`), allow `get_note` while blocking `delete_note`
  at the approval layer. Also the fit for weaker models (no GraphQL to
  write) and [fastapi_mcp migrants](#migrating-from-fastapi_mcp).
- **`graphql` — control by kind, and you trust the agent.** One boundary
  between reads and writes: `graphql_query` auto-runs (`readOnlyHint`),
  `graphql_mutation` asks for confirmation (`destructiveHint`);
  `allow_mutation=False` gives analysts a pure read-only deployment and
  (deprecated `mutation_include` used to scope writes; pair
  tag-filtered deployments instead). Best for
  composition-heavy internal work — one round trip across many routes.
- **`progressive` — `graphql`'s contract for apps too big to hand over
  in one shot.** The agent learns one domain at a time (a route's
  FastAPI tags are its domain path — `tags=["billing:invoice"]` nests
  `billing → invoice`)
  (`list_domains` → fragments); the catalog stays ~1.5k tokens whether
  the API has 30 routes or 300.

Typical pairings: an internal agent gets `graphql` (read/write split by
tool annotations), the analytics dashboard gets a read-only deployment
(`graphql` + `allow_mutation=False`), the external partner agent gets
`tools` on a ten-route curated subset.

Nothing switches implicitly: `graphql` warns above
`progressive_threshold` (default 25), `tools` warns above 50 routes (the
linear catalog overtakes the constant modes there). `"simple"` is a
deprecated alias for `graphql`.

### What the agent sees (connection lifecycle)

When an MCP client (Claude Code, Cursor, any MCP-capable agent) connects
to the endpoint, this is what happens — and what it costs in context:

```
① Connect + handshake
   POST /mcp → MCP initialize
   Agent receives: tool list (2-6 constant tools) + instructions (yours)

② Discover the schema (one of two modes)
   simple:       get_schema → full SDL in one call (~1-2K tok)
   progressive:  list_domains → search_fields / list_queries
                 → get_query_schema for the domain it needs
                 (~1.5K tok total, flat regardless of API size)

③ Execute
   graphql_query — one call composes any routes: fields from different
   domains, aliases, field projection. A failing route nulls only its
   own field; siblings survive.
   graphql_mutation — writes (exposed by default; destructiveHint asks clients to confirm)

④ Credentials (if auth= is configured)
   First connect → OAuth 2.1 login (browser opens, user authorizes)
   → token stored by the client
   → every subsequent call carries it
   → the bridge forwards it to the routes via passthrough_headers
   → your Depends/middleware verify it — the agent acts as the user
```

The cost curve: **① is fixed (~600 tok), ② is your choice (simple vs
progressive), ③ is per-task, ④ is one-time per client.** An agent that
has already connected and discovered the schema pays only ③ — every
subsequent query is one `graphql_query` round trip.

#### `instructions`: the agent's first read

`instructions` rides the `initialize` handshake — clients inject it into
the conversation once per connection, before any tool call. A short guide
saves the agent its discovery round-trips (and you the tokens):

```python
mcp = FastAPIMCP(
    app,
    name="my-app",
    instructions=(
        "Notes service exposed as GraphQL. Domains: iam (users, roles), "
        "shop (catalog, orders). Call get_schema once, then compose "
        "several routes in a single graphql_query — a failing route nulls "
        "only its own field. Writes go through graphql_mutation."
    ),
)
```

<details>
<summary><b>Writing <code>instructions</code> well</b></summary>

- **Lead with the domain map** — the same names your `tags` produce, so
  the agent can aim `graphql_query` without reading the whole SDL first.
- **Say the composition rule** — one query can combine routes; that is the
  feature agents most often fail to discover on their own.
- **Name the write channel** — `graphql_mutation` (if enabled).
- **Keep it under ~120 tokens** — it is resident context for the whole
  session; the SDL (via `get_schema`) already carries the details.
- Progressive mode profits most: a domain map in `instructions` lets the
  agent skip `list_domains` and go straight to `list_queries("iam")`.
</details>

### Progressive disclosure (large apps)

With `mode="progressive"` (graphql mode warns and suggests it above
`progressive_threshold` routes — default 25, counted after path and tag
filtering), the toolset is a 5-layer walkthrough of the tag tree:

```
list_domains ──▶ list_queries("billing:invoice") ──▶ get_query_schema("billing:invoice") ──▶ graphql_query
                     (list_mutations with allow_mutation=True)
                     search_fields("billing", "invoice late fee")   ← BM25 shortcut for wide domains
```

Each domain SDL fragment re-wraps the real group types along the path, so it
shows **exactly the grouped query the agent must write** — nothing more, and
with every description attached. **Discovery is scoped; execution is not**:
`graphql_query` always runs against the full schema, so fields from different
domains combine freely. All three modes: `mode="graphql" | "progressive" | "tools"`.

### Mounted into the same app

```python
mcp.mount_to(app, "/mcp")            # streamable HTTP at /mcp/
mcp.handler.mount_graphql(app)       # GraphiQL at /graphiql + POST /graphql
```

<details>
<summary><b>Multi-worker deployments</b> — sticky sessions or stateless mode</summary>

Streamable HTTP sessions are stateful and live
in one process — behind several workers or pods, either pin the MCP path to
one worker / use sticky routing, or mount stateless:

```python
mcp.mount_to(app, "/mcp", stateless_http=True)  # no session affinity needed
mcp.run(stateless_http=True)                    # same flag on run()
```

Stateless mode runs one transport per request: it survives any load
balancer, at the cost of per-request session setup. `/graphql` (plain
GraphQL face) is stateless already.
</details>

### Multiple MCP deployments over one app

Different MCP consumers often need different slices of the same app. Build
one `FastAPIMCP` per use case, each scoped by its own tag filter, and mount
each at its own path — the instances share nothing but the wrapped app:

<details>
<summary><b>Example</b></summary>

```python
iam = FastAPIMCP(app, name="iam-api", include_tags=["iam:*"])
billing = FastAPIMCP(app, name="billing-api", include_tags=["billing:*"])
iam.mount_to(app, "/mcp-iam")        # streamable HTTP at /mcp-iam/
billing.mount_to(app, "/mcp-billing")
```
</details>

### Migrating from fastapi_mcp

[fastapi-mcp](https://github.com/tadata-org/fastapi_mcp) has been dormant
since August 2025 (last release v0.4.0, 2025-07; incompatible with the
current MCP spec). Migration here is usually two lines — the bridge
derives everything from your existing app — and `mode="tools"` gives you
the same one-tool-per-route shape with a typed soul:

| fastapi_mcp | here |
|---|---|
| `FastApiMCP(app)` | `FastAPIMCP(app, mode="tools")` |
| `include_operations=["get_note"]` | `include_tags=[...]` (path globs are deprecated) |
| `mcp.mount()` (lifespan never fires [#256](https://github.com/tadata-org/fastapi_mcp/issues/256); requests hang [#259](https://github.com/tadata-org/fastapi_mcp/issues/259)) | `mount_to(app, "/mcp")` — lifespan chained, e2e-tested |
| `AuthConfig` | `auth=` (TokenVerifier gate / OAuth provider) + `passthrough_headers` — each caller acts as themselves |
| tool names from `operation_id` (`list_notes_notes_get`) | tool names from function names (`list_notes`) |
| whole-payload responses, no output types | `fields` projection (`{ id title }`) + typed `output_schema` |
| every tool shows as "write" (no annotations [#252](https://github.com/tadata-org/fastapi_mcp/issues/252)) | readOnlyHint / destructiveHint derived from the HTTP method |

Issues from that repo that cannot happen here, by construction: cyclic
`$ref` RecursionError ([#287](https://github.com/tadata-org/fastapi_mcp/issues/287)
— no spec parsing), path-parameter URL encoding
([#354](https://github.com/tadata-org/fastapi_mcp/issues/354) — in-process
dispatch, percent-encoded assembly), OpenAPI dialect fields breaking
strict clients ([#218](https://github.com/tadata-org/fastapi_mcp/issues/218)
— schemas derive from GraphQL types), mcp 2.x SDK incompatibility
([#321](https://github.com/tadata-org/fastapi_mcp/issues/321) — rides
fastmcp 4.x on the current spec).

### Plain GraphQL (no MCP)

```python
from fastapi_mcp_neo import RouterGraphQLHandler

handler = RouterGraphQLHandler(app)
print(handler.get_sdl())
result = await handler.execute(
    "query($id: Int!) { iam { get_user(user_id: $id) { name } } }",
    variables={"id": 1},
)
```

## Authentication

Route calls travel through the real ASGI app in-process, so `Depends`,
middleware and security schemes behave exactly as over HTTP. Credentials have
a **single source: the caller** — the FastAPI security schemes are the only
verifiers, and this bridge never holds or manages tokens of its own.

- **Per-caller passthrough (default)**: each MCP/GraphQL client connects with
  its own credentials and `passthrough_headers` (default
  `("authorization",)`) forwards them to the routes — queries run as the
  caller, exactly as they would over HTTP. An explicitly empty list disables
  forwarding; headers are matched case-insensitively and only whitelisted
  names ever reach a route (no smuggling `x-internal-token` past the bridge).
- **Session cookies ride a header — whitelist `"cookie"`** (the common
  trip-up): on the wire a browser session *is* the `Cookie:` request header,
  so cookie-authenticated apps forward it with
  `passthrough_headers=["authorization", "cookie"]`. The default forwards
  `authorization` only — with a session-cookie app, protected routes answer
  `HTTP_401` over MCP until `cookie` is listed. FastAPI's `Cookie()` route
  parameters are the other, unrelated cookie: like `Header()` parameters
  they never become GraphQL arguments. The `POST /graphql` face (and
  GraphiQL) share the same whitelist, so browser sessions flow through it
  the same way. Wired example: [examples/notes_oauth](./examples/notes_oauth/).

Client side, the credential is just a header on every MCP request:

```bash
# Claude Code
claude mcp add --transport http my-api http://localhost:8000/mcp \
  --header "authorization: Bearer <token>"
# generic clients (mcp-remote bridge)
npx mcp-remote http://localhost:8000/mcp --header "Authorization:${AUTH_HEADER}"
```

<details>
<summary><b>No-credential behavior, and machine callers without a user context</b></summary>

- **Without credentials, protected routes fail** — field errors like
  `HTTP_401` in query results; nothing falls back to a server-side identity.
- **Machines without a user context** configure the service credential on the
  MCP client side (or, for programmatic use, pass
  `handler.execute(..., headers={...})` directly).
</details>

### Gating the MCP endpoint

`auth=` also accepts a bare token verifier — subclass
`fastmcp.server.auth.auth.TokenVerifier`, override `verify_token`, and the
endpoint answers `401` (a `WWW-Authenticate: Bearer` challenge) for every
request without a valid token:

```python
from fastmcp.server.auth.auth import TokenVerifier
from mcp.server.auth.provider import AccessToken

class SharedSecretVerifier(TokenVerifier):
    """One shared token guards the MCP endpoint — no OAuth machinery."""

    async def verify_token(self, token: str):
        if token != "demo-secret":
            return None  # None -> 401
        return AccessToken(token=token, client_id="local", scopes=[], expires_at=None)

mcp = FastAPIMCP(app, auth=SharedSecretVerifier())
```

One token, two checks: the gate verifies the endpoint, and the routes' own
security schemes verify business calls — the same Bearer reaches them via
`passthrough_headers`. A perimeter you already control (network ACL,
gateway auth on the `/mcp` path) works too. Combine with
`allow_mutation=False` (or a separate read-only, tag-filtered
  deployment) to keep writes out of reach.

### MCP endpoint OAuth

Pass a fastmcp auth provider and the MCP endpoint speaks OAuth 2.1: 401
discovery, dynamic client registration, PKCE, a consent page, and its own
reference tokens verifying every call:

```python
mcp = FastAPIMCP(app, auth=GitHubProvider(client_id=..., client_secret=..., base_url=...))
```

Ready-made providers: GitHub, Google, Auth0, Keycloak, AWS, Azure, Clerk,
Discord, Supabase, WorkOS, and more — plus `JWTVerifier` (verify an external
IdP's JWTs via JWKS) and `OAuthProxy` (adapt any upstream OAuth server).
Full list and configuration: [fastmcp auth
docs](https://gofastmcp.com/servers/auth).

With a full provider, no `--header` is needed: Claude Code follows the 401
OAuth discovery, opens a browser, the user logs in, and the agent's queries
run as that user. `mount_to(app, "/mcp", auth_at_root=True)` hosts the OAuth
routes at the app root (for reusing an IdP app whose registered callback
lives there). The bridge itself still verifies nothing. Full wired flow:
[examples/notes_oauth](./examples/notes_oauth/).

## Observability (OpenTelemetry)

Install an OpenTelemetry SDK next to your app — that's the whole setup. One
waterfall per query: fastmcp emits the tool span, the bridge emits
`graphql.execute` and injects W3C `traceparent` into every in-process route
call, and FastAPI >= 0.142 nests its route spans under it. Timeouts and
queue waits surface as span events.

<details>
<summary><b>The span levels, and where to look</b></summary>

- **fastmcp** emits the tool level (`tools/call graphql_query`);
- the bridge emits `graphql.execute` (the GraphQL orchestration layer) and
  injects W3C `traceparent` into every in-process route call — independent
  of `passthrough_headers`, a no-op without an SDK (`opentelemetry-api`
  only, non-recording by default);
- **FastAPI >= 0.142** emits the route level (`GET /things` plus
  `fastapi.dependencies/endpoint/serialization`) and extracts the injected
  context — so route spans nest under `graphql.execute`, one trace per
  query.

Route-call timeouts and concurrency queue waits surface as span events
(`route.timeout`, `route.queue`) on `graphql.execute`. A runnable proof
(plus the Jaeger walkthrough): [examples/otel_smoke.md](./examples/otel_smoke.md);
a live wired app: [examples/notes_oauth](./examples/notes_oauth) (env-gated
`app/observability.py`). Metrics (per-URL QPS/p99) are out of scope here —
derive them from spans with an OTel Collector `spanmetrics` connector.
</details>

## Hardening the bridge

Four knobs are built in and on by default:

- **`request_timeout`** (default 30s, `None` disables) — per-route-call
  deadline. The in-process ASGI call bypasses httpx's own timeout machinery,
  so enforcement lives in `asyncio.wait_for`; a timed-out field surfaces as a
  `TIMEOUT` error (http_status 504) while its siblings survive.
- **`max_depth`** (default 10, `None` disables) — maximum selection-set
  nesting per document. Recursive models make depth unbounded and an MCP
  caller is an LLM that can emit runaway nesting; overly deep documents are
  rejected with a validation-style error before anything executes.
- **`max_concurrency`** (default 16, `None` disables) — bound on in-flight
  route calls across all queries. Sibling fields resolve concurrently, so
  one wide query fans out; this protects the wrapped app's upstream from
  being hammered by its own bridge (queueing counts against
  `request_timeout`, default 30s).
- **`document_cache_size`** (default 128, 0 disables) — LRU capacity for the
  parse + depth-guard + validate front half of execution, keyed by the query
  string. Agents repeat documents constantly; a hit skips straight to
  execution (measured 1.52ms → 0.69ms on a 2-field query). Execution results
  are never cached — per-call credentials run for real every time.

All four are parameters of `RouterGraphQLHandler` and `FastAPIMCP`. For
anything policy-shaped, `validation_rules=` on the handler passes extra
graphql-core validation rules through (they extend the standard set).

<details>
<summary><b>Rate limiting &amp; response caps on the MCP face</b> (FastMCP middleware, zero bridge code)</summary>

`FastAPIMCP.mcp` is the underlying `FastMCP` instance:

```python
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware

mcp = FastAPIMCP(app)
mcp.mcp.add_middleware(RateLimitingMiddleware(max_requests_per_second=10))
mcp.mcp.add_middleware(ResponseLimitingMiddleware(max_size=1_000_000))
```

`RateLimitingMiddleware` limits **per client** by default (pass
`get_client_id=` to customize the key or `global_limit=True` for a shared
bucket); `ResponseLimitingMiddleware` truncates oversized tool responses
(default 1 MB, configurable suffix). The `POST /graphql` face does not go
through fastmcp — attach your own middleware to the host app for that
endpoint.
</details>

## Demo

[`examples/shop`](./examples/shop/) — a small shop app (users / catalog /
orders / stats, auth via `x-token: demo-secret`) with every feature in play:

```bash
uv run --extra mcp python -m examples.shop                     # REST + /mcp/ + /graphiql + /graphql on :8010
uv run --extra mcp python -m examples.shop.mcp_walkthrough     # agent's-eye MCP walkthrough, no client needed
```

<details>
<summary><b>What to look for in the demo</b></summary>

`python -m examples.shop` prints all endpoint URLs and serves the grouped
schema; `/now` is untyped on purpose so the skip warning is visible at startup.
`GET /categories` is the recursive showcase — `CategoryOut.children` is
self-referencing, so over MCP one level of `children` selection returns the
whole tree (the type's schema description states the contract). Full
**documentation coverage** means all four description chains are inspectable
in GraphiQL.

For the full consumer experience — a real app with **GitHub OAuth login,
session cookies, and MCP OAuth (Claude Code's browser login flow)** — see
[`examples/notes_oauth`](./examples/notes_oauth/): three interchangeable
credential carriers resolved in one place, the MCP endpoint protected by
an OAuth 2.1 proxy, and a smoke script that walks the protected paths
headlessly. For observability, [examples/otel_smoke.md](./examples/otel_smoke.md)
walks the one-waterfall-per-query proof in Jaeger.
</details>

## Development

```bash
uv sync && uv run pytest        # tests
uv run ruff check src tests
uv run mypy src
```

## Status

0.x — breaking changes can land in minor bumps; 1.0 will freeze the public
API. See [CHANGELOG.md](CHANGELOG.md). Ideas welcome, agent-first: a
`get_schema` variant that returns only the subgraph an agent asks about,
response shaping for token budgets, per-team scoped deployments (one MCP
deployment per tag set), response header pass-through.

Design extracted from [nexusx](https://github.com/KLR-Pattern/nexusx)
(SQLModel → GraphQL → MCP), rebuilt on graphql-core standard execution.

## License

MIT

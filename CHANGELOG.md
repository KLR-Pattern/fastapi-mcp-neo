# Changelog

## 0.19.0 (2026-10-25)

### Fixed

- **Parameter-collision skips are now decided at SCAN time** (the audited
  channel): the skip lands in `readiness().skips`, the route leaves the
  route list, and every downstream consumer stays consistent. The
  schema-build-time patch it replaces had three regressions — tools mode
  crashed with KeyError when a whole domain vanished, the skipped route
  kept its name registered and could still kill same-named healthy
  siblings, and readiness/`list_domains` advertised routes that no longer
  existed. A domain whose every route was skipped drops out of the group
  tree (no empty root types); misleading "add a GET route" guidance when
  GETs exist but were all skipped is gone with the same move.
- **Streaming responses that fail mid-body** (typed promise, streaming
  reality) surfaced as a bare JSONDecodeError with no context. Now a
  route-contextual field error (truncated-body, http_status 502); the
  mid-stream exception text never reaches the agent.
- **`debug=True` tracebacks** embedded in the 500 body no longer reach
  the GraphQL error message (reduced to the generic line); network
  parity is preserved, agent-side leakage is not.
- **Swallowed unhandled exceptions now leave a server trace** — the
  invoker logs route, method and status at ERROR before raising the
  sanitized field error (in-process mode used to be LESS diagnosable
  than uvicorn).
- **Heterogeneous-tuple degradation is audible** like unions:
  `degraded_fields` entry + reason; bare lowercase `tuple` gets the same
  verdict as `typing.Tuple`.

### Changed

- Removed dead `_Group.prune()/is_empty()` (superseded by the
  empty-branch `None` propagation).

## 0.18.0 (2026-10-25)

### Fixed

- **Unhandled route exceptions no longer leak.** httpx's ASGITransport
  defaulted to propagating app exceptions, so a route's raw error text
  became the GraphQL error message — while the same app over real HTTP
  answers 500 "Internal Server Error" with no detail. The transport now
  runs with raise_app_exceptions=False; parity pinned (message,
  extensions, no-leak, sibling survival, e2e isError).
- **Heterogeneous fixed tuples no longer silently mistype.**
  tuple[Item, int] mapped as [Item!] — the collection mapping took the
  first type argument, so the other positions vanished from the schema
  with no skip and no degraded-fields record. Differing-position tuples
  (and empty tuples) now bridge as the JSON scalar like unions;
  variadic and all-same tuples stay lists (pydantic emits tuples as
  arrays, so those are wire-accurate).

### Changed

- **A route whose parameters collide on one GraphQL argument now skips
  that route instead of failing the whole mount** — degrade-and-notify,
  consistent with bad fields degrading and bad routes skipping: the
  warning names both parameters and the fix; a group whose every route
  was skipped drops out so an empty root type is never produced.

### Added

- Scenario pins migrated from fastapi_mcp's and fastmcp's from_openapi
  test suites: nested body models with custom scalars end to end,
  UUID/datetime response serialization, whole-None list query params,
  responses={...} decorator smoke, model inheritance, SecretStr fields,
  raw text/plain bodies.

## 0.17.0 (2026-10-25)

### Added

- **`@mcp_query` / `@mcp_mutation`: force a route's read/write
  classification.** The verb default breaks on POST-for-read APIs
  (payload bodies beat query strings) and GET-with-side-effects routes;
  the marker (either side of the route decorator, kept adjacent) flips
  the classification at scan time — schema placement, tools-mode
  annotations, and the allow_mutation gate all align, while the physical
  HTTP verb on the wire is untouched.

## 0.16.0 (2026-10-25)

### Added

- **`@mcp_query` / `@mcp_mutation`: force a route's read/write
  classification.** The verb default breaks on POST-for-read APIs
  (payload bodies beat query strings) and GET-with-side-effects routes;
  the marker (sitting below the route decorator) flips the classification
  at scan time — schema placement, tools-mode annotations, and the
  allow_mutation gate all align, while the physical HTTP verb on the wire
  is untouched.

### Removed (breaking)

- **Path-glob route filters: `include` / `exclude` / `mutation_include`.**
  Tags are the filter dimension (`include_tags` / `exclude_tags`): they are
  what the whole bridge already speaks (the domain tree, progressive
  discovery, multi-deployment scoping all derive from tags), while paths
  are brittle — refactors move them, and two verbs sharing a path cannot
  be split. Scope writes with a tag-filtered read-only/fully-writable
  deployment pair instead of `mutation_include`.
- **`mode="simple"` alias** — removed with the other compatibility shims;
  use `mode="graphql"`.
- **`RouterMCP` class alias** — use `FastAPIMCP`.

No compatibility shims are carried in 0.x: breaking changes land directly
and this file is the migration channel.

## 0.15.0 (2026-10-24)

### Added

- **`mode="tools"`: one standalone-GraphQL-entry tool per route.** Typed
  args map 1:1 to the operation's variables, `fields` projects the
  response (GraphQL selection set), output_schema derives from the
  field's GraphQL type, annotations derive from the HTTP method
  (GET -> readOnlyHint, writes -> destructiveHint). For per-tool client
  permissions (MCP gates per tool) and fastapi_mcp migrants; warns above
  50 routes (~250 tok/route measured — use progressive or an
  include/exclude subset there). No executor tools in this mode: the
  tool is the granularity, clients compose via parallel tool calls;
  composition queries belong to graphql/progressive mode.

### Changed (breaking)

- **`mode` enum redesigned: `graphql` | `progressive` | `tools`, default
  `graphql`.** `simple` is a deprecated alias (DeprecationWarning);
  `auto` is REMOVED — it was a policy, not a mode, and silently flipped
  the tool set at the 25-route boundary (staging vs prod could differ).
  Replaced by an advisory startup WARNING above `progressive_threshold`.
  Passing `mode="auto"` (or any unknown value) now raises
  `GQLMCPConfigError` at construction — no compatibility shim; drop the
  parameter or pin a mode explicitly.
- **`allow_mutation` now defaults to True.** The bridge mirrors the app
  ("nothing disappears"), the app's own auth gates writes per caller,
  and `graphql_mutation` carries `destructiveHint`. Pass
  `allow_mutation=False` for a read-only view — previously the default.

## 0.14.0 (2026-10-22)

### Fixed

- **Path parameter values containing URL metacharacters round-trip
  intact.** Starlette's convertor `to_string()` returns values
  un-encoded, so httpx re-parsed a literal `?`/`#` inside a path
  argument as the query/fragment separator and silently truncated it
  (`x?q=1` arrived as `x`). Values are now percent-encoded before URL
  assembly; the route receives the original value. Analyzed against
  tadata-org/fastapi_mcp#354: the wrong-endpoint/traversal half of that
  bug never applied here (in-process dispatch does not normalize `..`,
  routing matches the literal path) — this was the data-loss half.
  Pinned by `tests/integration/test_path_param_metachars.py`.

## 0.13.0 (2026-10-22)

### Changed

- **Renamed the package: `fastapi-gql-mcp` → `fastapi-mcp-neo`**
  (import `fastapi_mcp_neo`; class `FastAPIMCP` unchanged). Pure rename —
  no behavior change; the full test suite, mypy, ruff and the CI demo
  checks pass as before. Releases now publish under the new PyPI name;
  the old name keeps its download history.

## 0.12.0 (2026-10-10)

### Added

- **`search_fields(domain, query)`** (progressive mode): BM25 over field
  names, descriptions and argument names — the shortcut for wide domains
  where reading the whole fragment costs more than searching for the one
  field you need. Field names come from endpoint function names (developer
  vocabulary, keyword-dense), so lexical search lands well without
  embeddings. Indexed lazily per domain, cached (schema is immutable).
  Measured on a 324-route app: a 1,743-token domain fragment collapses to
  a ~200-token search round trip.

## 0.11.1 (2026-10-10)

### Docs

- **Capability boundaries rewritten as the full input/output matrix**:
  a structured-inputs table (path convertors, query models, body shapes,
  Depends, scalars, alias wire contract), two new degradation rows (unbound
  TypeVar at a route boundary, unannotated body parameters), mixed-type
  Literal in the skip list, and a "Conventions the bridge follows" table
  collecting the FastAPI-side derivation rules (tags/domains, function
  names, descriptions, requiredness per model family, exclude/deprecated,
  optional headers, multi-verb priority, naming collisions).

### Fixed

- **TypeVar-degraded routes now carry the real reason in their SDL note.**
  Route-output classification runs ONCE at scan time
  (`scanner.OutputPlan`) and every reader — startup notice, readiness
  report, SDL field note — consumes the same plan, ending five verified
  disagreements between the three former detectors: the misleading
  "declares no response type" guidance on generic routes, first-match
  audit vs stacked SDL notes, void+filter audited as degraded (a bodyless
  response has nothing to filter), Any+filter showing two different
  stories, and the filter⇒JSON decision smeared across four sites.

### Changed

- **Input/output handling systematically reorganized** (four refactors,
  zero behavior change beyond the fix above):
  - type_builder's six near-clone field builders (model/TypedDict/
    dataclass × input/output) unified on `FieldSpec`: three family
    extractors produce normalized field records, one `_build_fields`
    loop owns sanitize/wire-key/degrade/union/empty handling that was
    copy-pasted six times (1052 → 1041 lines while gaining the
    abstraction)
  - `_build_route_info` (158 lines) decomposed into a gate sequence
    (`_FlatParams`, five named gates, `_route_params`, `_trial_route_types`)
    with `_make_param` as the single ParamInfo/gname producer
  - wire-name/default/note helpers single-sourced (`request_wire_name`,
    `output_wire_name`, `graphql_default`, `append_note`); all three
    cross-module private imports made public or moved home

- **Test suite reorganized into layers.** `tests/` now mirrors altitude
  instead of git history: `unit/` (one module under test, no handler
  execution), `integration/` (through `RouterGraphQLHandler`),
  `e2e/` (ASGI / fastmcp Client), `guards/` (meta pins), plus a shared
  `tests/support/` package (sample models, app factories, MCP/HTTP
  protocol helpers) that replaces ~19 copy-pasted factories and
  per-file model redefinitions. A `make_handler` conftest fixture owns
  handler lifecycle. Four assertion-level duplicates merged
  (filter_passthrough_headers ×2, introspection, list-type, path-param
  restatement); layered assertions deliberately kept. Ticket-wave labels
  (G5-G11/H1/M1, P0-2, R1/R4, B1, O1/O2, D1) moved from names and
  section markers into docstrings.

### Added

- **Seventeen gap tests** lift coverage from 95.1% to 96.7%:
  unknown-domain errors for the remaining progressive tools, fragment
  arms of the unroll-limit walk, HEAD/OPTIONS-only routes, empty
  TypedDict/dataclass input raises, output-side AliasChoices fallback,
  input-side hyphenated TypedDict keys, dataclass field degradation on
  both sides, non-JSON 2xx bodies, long error-body truncation, and
  syntax errors on the unguarded parse path.

## 0.11.0 (2026-10-09)

### Added

- **Stdlib `@dataclass` maps onto real object/input types.** A dataclass
  response (or a dataclass field nested inside a model, and vice versa)
  becomes a `GraphQLObjectType` / `GraphQLInputObjectType` like a
  `BaseModel` or `TypedDict`: annotations from `get_type_hints`, output
  nullability from the annotation (defaults always materialize), input
  requiredness and literal defaults from the field definitions
  (`default_factory` fields are optional without a GraphQL default —
  FastAPI materializes them, a 422 surfaces as a field error).
  `ClassVar`/`InitVar` never become fields. FastAPI treats dataclasses as
  first-class citizens on both sides, so validation and serialization ride
  the native stack — the runtime path needed no changes. Recursive
  dataclasses get the true-depth contract like any recursive type.

## 0.10.0 (2026-10-09)

### Changed

- **Four deduplications around the recursion machinery.** The NonNull/List
  unwrap and the direct-self-reference predicate each have ONE definition
  (`recursive_expand.unwrap` / `is_direct_self_reference`) shared by
  type_builder's schema note and `recursive_edges`, so the two detectors
  cannot drift; `UnsupportedFieldTypeError` carries a structured
  `field_reason` attribute that degraded-field reporting reads instead of
  sniffing exception message text; the field-build memo lives once inside
  the register-then-build shells instead of four per-builder cache
  wrappers.

- **Unmappable model fields degrade to raw JSON instead of skipping the
  route** (issue #3, General Fix Strategy #2). A field whose type has no
  GraphQL mapping (`metadata: SomeCustomClass`) bridges as the `JSON`
  scalar — the surrounding model keeps its structured, selectable fields,
  and the degradation lands in `readiness().degraded_fields` and the
  startup warning, exactly like union fields always have. Input fields
  degrade the same way (FastAPI validates whatever arrives; a 422 surfaces
  as a field error). Unbound-TypeVar generics now degrade their field
  (keeping the parameterize guidance) instead of skipping the route.
  Skips remain only where no fallback can express the shape: top-level
  response/parameter annotations with no mapping at all (`-> bytes`, a
  bare custom class); a wholly-unusable nested type (an empty model)
  rolls back transactionally and degrades its referencing field.
  `TypeBuilder.union_fields` is renamed to `degraded_fields` (unions and
  unmappable types, deduplicated per field+reason).

### Added

- **Recursive fields return their true depth.** A selection on a recursive
  model field now means "the whole subtree": the agent's stopping selection
  repeats as a template to the data's actual depth, at any position in the
  document (the chain need not start at the root). Previously the finite
  GraphQL document silently truncated recursive data — invisibly, since a
  leaf's `children: []` is indistinguishable from a cut-off subtree, and
  trees deeper than `max_depth` could not be fetched in full at all. The
  route already computed the complete tree (routes are called once; nested
  resolution is pure projection), so unrolling merely removes the document
  limitation. `max_depth` still guards the document as written; fragments
  in a recursive template and mutual recursion (A.b: B / B.a: A) are
  consciously left unexpanded. The unroll ceiling is a function of the
  process recursion budget (`sys.getrecursionlimit() // 16`, from a
  measured ~12 frames per served level) — the limit never promises depth
  the executor cannot deliver, and it scales automatically when an
  operator raises the limit to serve deeper trees; data deeper than the
  limit is served as deep as the document goes, with a definite error
  naming the limit (data stays — truncation is never silent). The
  recursive type's schema description carries a one-line contract ("full
  subtree at true depth; your selection repeats per level") once per
  type — local placement at ~15 tokens instead of per-field paragraphs,
  so agents discover it from the SDL.

- **`instructions=` passthrough on `FastAPIMCP`** — the MCP protocol's
  handshake usage guide, injected into the agent's context once per
  connection; `None` (default) sends nothing. README documents how to
  write one that saves discovery round-trips.
- **`stateless_http=True` on `mount_to()` / `run()`** — one transport per
  request (no session affinity), for multi-worker / multi-pod deployments
  behind non-sticky load balancers where stateful streamable HTTP sessions
  404. Passed through to fastmcp's `http_app`/`run_http_async`.
- **MCP tool annotations** — discovery tools and `graphql_query` now carry
  `readOnlyHint`, `graphql_mutation` carries `destructiveHint`, letting
  agents auto-run reads and confirm writes.
- **`exclude_deprecated=True` filter** — drops `deprecated=True` routes at
  the config level (silent drop, like path/tag filters); by default they
  stay with their GraphQL-native `@deprecated` mark.

### Fixed

- **Route-boundary TypeVars normalize or degrade — never skip the route**
  (issue #5). A constrained `TypeVar` is pydantic's spelling of "one of
  these" and a bound one of "this or narrower"; both now normalize to
  exactly that before mapping, so `list[list[T]]` parameters and
  responses map like the constraint union (JSON elements) or the bound
  (fully structured) instead of removing the whole route. A fully unbound
  `TypeVar` at a route boundary degrades the route to raw JSON with the
  parameterize guidance — the same fallback a TypeVar field gets inside a
  model — and `readiness().degraded` carries the reason (skips stay for
  shapes no fallback can express, e.g. `-> bytes`). Inside models, a
  constrained-TypeVar field now bridges with the union note naming the
  constraint shapes instead of the unbound-TypeVar wording.
- **The alias wire contract: GraphQL names are translated, not diverged.**
  A wire name FastAPI validates/serializes by but GraphQL cannot spell
  (`Query(alias="order-id")`, `Field(alias="item-sku")`) previously either
  crashed the whole handler build or produced a schema whose every call
  failed (input 422, output null) with the audit staying green. The schema
  now uses the sanitized name and translates at both boundaries: output
  fields resolve from the wire key, input objects rewrite their coerced
  dict via `out_type` (composing through nested models), top-level
  arguments are renamed back in the resolver, and `AliasChoices` names its
  first string choice. Field selection and types are fully preserved —
  nothing degrades, and the green audit is now honest.
- **A subselection-less recursive field is rejected, not crashed on.**
  Selecting a back-edge without a subselection (`children` with no
  `{ ... }`) is invalid GraphQL; it now comes back with the standard
  "must have a selection of subfields" validation error like any other
  field, instead of an uncaught `AssertionError` surfacing as an opaque
  500.
- **Custom validation rules see the document the agent wrote.** Unrolling
  is a system behavior; caller-supplied rules (cost guards, complexity
  caps) now validate the written document, so a 3-field recursive
  selection is no longer rejected for having ~60x the fields the agent
  asked for. The standard rule set still validates the expanded document
  the executor actually runs.
- **Union-field degradation is recorded on every side.** A union field
  bridging to raw JSON lands in `readiness().degraded_fields` and carries
  its schema note whether it sits on a model's output, a model's input, a
  TypedDict's output or a TypedDict's input — the audit no longer depends
  on which side of the API the field happens to be on.
- **The excess walk is gated by the document.** Edge-named keys inside a
  JSON passthrough payload are invisible to the document and can no
  longer flag a mixed query (recursive tree + colliding payload keys)
  that truncated nothing.
- **The unroll limit is honest — the crash band is gone.** The old floor
  `max(100, ...)` sat above the measured ~82-level execution ceiling, so
  trees between ~83 and 100 levels returned `null` with a bare "maximum
  recursion depth exceeded" instead of data. The limit is now
  `sys.getrecursionlimit() // 16` (the deepest walk it can produce stays
  at ~3/4 of the measured ceiling) and is pinned per handler instance at
  construction: raising the budget later serves deeper trees on NEW
  handlers, while an existing one reports a definite error on data beyond
  its pinned limit instead of silently truncating against cached,
  shallower documents.
- **Excess detection is a proof, not a guess.** The unrolled document's
  innermost repetition is a probe: data occupying it proves deeper data
  exists. Complete data leaves the probe empty and is never flagged (the
  old depth-counting heuristic fired on exactly-complete trees), and
  single-object recursive edges (`next: LNode | None`) count like list
  edges — they used to truncate with no report at all.
- **Only stamped documents pay the per-response excess walk.** Queries
  touching no recursive field never ran it and can no longer pick up
  false flags from JSON passthrough payloads whose keys collide with
  edge names; the walk also reuses edge names computed once per handler
  instead of rebuilding them per response.
- **Nested recursive chains unroll too.** Stamping a chain end makes
  graphql-core's `visit` rebuild every ancestor of the edited node, so the
  id-keyed chain-end table silently missed an outer chain end nested
  inside another chain's template — two recursive types in one document
  truncated invisibly (the very thing unrolling exists to prevent).
  Detection and stamping now share one bottom-up pass: chain-end-ness is
  decided from each node itself (its type context and its own
  selections), so no marking state has to survive the node rebuilds.
- **A template alias reserving the back-edge's response key no longer
  fails validation.** `children: name` inside the stopping selection
  collided with the stamped subtree (`children { ... }`) on the same
  response key, rejecting a legal query with ~100 `FieldsConflict`
  errors. Such chains now degrade to the written document — the agent's
  own key choice wins — the same honest fallback as fragment-carrying
  templates.
- **A failed model build no longer poisons the shared type cache** (issue
  #3, case 1). Object/input registration is now transactional: when a
  field fails to map, the half-built type (and its name) rolls back out of
  the caches. Previously the first route was skipped but the incomplete
  type stayed cached, so a second route reusing the same model passed
  scanning and the `GraphQLSchema` build then crashed for the WHOLE app
  (`TypeError: ... fields cannot be resolved`) — one incompatible model
  took every valid route down with it. Nested failures roll back every
  recursion frame, and each reuse of a bad model now gets its own skip
  record.
- **`set`/`frozenset` map onto GraphQL lists** (issue #3, case 3).
  Pydantic serializes sets to JSON arrays, so `set[T]` fields, parameters
  and responses bridge as `[T!]!` exactly like `list[T]` — previously the
  route was skipped with "Cannot map typing.Set".
- **Enum members inside `Literal` map to their underlying scalar** (issue
  #3, case 2). `Literal[Mode.A]` now normalizes `Mode.A` to `Mode.A.value`
  and rides the existing Literal path (`String`/`Int` + the
  "Allowed values" description); mixed members like
  `Literal[Mode.A, "other"]` work, and route-level Literal responses now
  carry the allowed-values note in their field description, matching model
  fields. Previously such routes were skipped with "Literal of Mode has no
  GraphQL scalar".
- **`TypedDict` maps onto real object/input types** (issue #3, case 6).
  A `TypedDict` response (or a TypedDict field nested inside a Pydantic
  model) becomes a `GraphQLObjectType` / `GraphQLInputObjectType` like a
  `BaseModel`: field types from `get_type_hints`, nullability from the
  required/optional key sets (`total=False` keys bridge as nullable — they
  may be absent from the JSON), recursive TypedDicts resolve through the
  same register-before-fields cycle handling, and failed builds roll back
  transactionally. Detection uses `typing_extensions.is_typeddict`, which
  recognizes classes declared via BOTH `typing.TypedDict` and
  `typing_extensions.TypedDict` (`typing.is_typeddict` misses the latter —
  verified on 3.14); `typing-extensions>=4.6` is now a declared dependency.
  Previously TypedDict responses were skipped with "Cannot map
  <class FlatRecord>".
- **Input unions bridge as the JSON scalar** (issue #3, case 4) — the input
  side now has the same fallback the output side always had. `A | B`
  parameters (query or body) map onto a `JSON` argument instead of skipping
  the route; the agent sends either member's value and FastAPI validates it
  — a mismatch surfaces as a GraphQL field error (422), never silently.
  This intentionally REVERSES the documented 0.9.0 behavior where
  request-side unions were skipped with "Cannot map"; symmetric degradation
  beats unavailability.
- **Unbound-TypeVar diagnostics** (issue #3, case 5). An unparameterized
  generic (`response_model=Envelope` where `value: T`) still skips only its
  own route, but the reason now names the fix —
  "GenericEnvelope.value: unbound TypeVar ~T — parameterize the generic so
  the field has a concrete type" — instead of a bare "Cannot map ~T".

## 0.9.0 (2026-10-08)

### Changed

- **`RouterMCP` renamed to `FastAPIMCP`.** The old name said Router while
  the class wraps a whole FastAPI app and IS the MCP server; `FastAPIMCP`
  names what it is and matches the package. `RouterMCP` remains as a
  deprecated subclass (DeprecationWarning on construction, removed at
  1.0) — existing imports keep working.

- ***(breaking)*** `RouterMCP.mode` property renamed to `resolved_mode` —
  it returns the mode actually in effect and never `"auto"`, unlike the
  `mode` constructor argument; the old name invited
  `assert mcp.mode == "auto"`, which could never pass.
- ***(breaking)*** `ReadinessReport.from_scan` removed from the public
  surface (added in 0.8.0): assembling a report required scan's internal
  products (`TypeBuilder`), which callers cannot obtain — the method was
  unusable externally and produced incomplete reports when forced.
  `ReadinessReport` is now pure data (three tuples + `ready`); get one
  from `RouterScanner(app, ...).readiness()` or `handler.readiness()`.
- `handler.skips` docstring now marks it a strict subset of
  `readiness().skips` and points to `readiness()` for the full audit.

## 0.8.0 (2026-10-08)

### Added

- **Readiness checklist as data**: `readiness()` returns the exposure audit
  the startup notices are built from — skipped routes (`SkipRecord`), raw-JSON
  degrades (`DegradedRecord`) — both carrying the route's string tags so each
  finding names its domain — and degraded union fields, plus a `ready` flag
  for CI pinning. Two entry points share one classifier:
  `RouterScanner(app, ...).readiness()` runs standalone (scan + classify
  only, no schema build or MCP server), `handler.readiness()` assembles from
  the stored scan results with no re-scan. Pass the same filters your
  deployment uses.
- **Tag-based route filtering**: `include_tags`/`exclude_tags` fnmatch globs
  scope which routes enter the schema, mirroring `include`/`exclude` (exclude
  wins, silent drop, AND-composed with path filters). `include_tags` is a
  strict whitelist — untagged routes drop; Enum tags are ignored. Enables
  several `RouterMCP` deployments over one app, each scoped to a different
  tag set and mounted at its own path.
- **Startup-notice showcase in `examples/shop`**: one route per notice
  reason, so booting the demo prints every warning the scanner can emit —
  six skips (form/file, raw `Response` return, required header param,
  hidden route, query model mixed with plain params, `set[int]` input) and
  three raw-JSON bridges (untyped, response-filtered, union response) plus
  the union-field notice (`SearchHit.hit`).

### Changed

- **`demo/` merged into `examples/shop/`.** All runnable demonstrations now
  live under one `examples/` tree (repo-only move, no library change; run
  `python -m examples.shop` / `examples.shop.mcp_walkthrough`). Framework
  tests no longer import demo code — the two `mount_to` tests build their
  own app, so `tests/` depends on nothing outside `tests/` and `src/`.

## 0.7.0 (2026-10-06)

### Added

- **Untyped routes now bridge instead of skipping.** An endpoint with no
  return annotation and no `response_model` becomes a raw `JSON` field
  (with a description noting the missing contract) instead of vanishing
  from the schema, and joins the degraded-routes startup notice —
  "no typed response — bridged as raw JSON; add a return annotation or
  response_model for a structured type" — so an annotation lost to
  refactoring stays loudly visible rather than silently degrading the
  schema. Coverage over strictness, with the warning as the quality
  gate.
- Routes explicitly annotated `-> None` (204-style deletes and
  side-effect calls) now bridge as a Boolean success field instead of
  being skipped as untyped: the call is the point, and "no response
  body" is an explicit contract — `gone(id): Boolean` returns true on
  2xx, failures surface as field errors as usual. The invoker parses
  204/empty bodies to None instead of a synthetic blob.

### Fixed

- Query-parameter lists with mixed None items no longer stringify None
  into the literal value `"None"` (`?tag=None`): None items are dropped,
  an all-None list is simply not sent.

### Changed

- The README gains a **Capability boundaries** section (and why.md a
  "THE BRIDGING PROMISE" passage): the four outcome buckets —
  structured by default, raw-JSON fallback with named causes, Boolean
  success for `-> None`, skips only when a route cannot be called
  correctly — so users can see the library's edges up front. A stale
  How-it-works bullet describing untyped routes as skipped was fixed
  in the same pass.
- CI enforces a **coverage gate** (`--cov-fail-under=95`, baseline
  95.7%); local pytest runs stay ungated.
- Every skip reason now states the remedy, not just the problem — e.g.
  "query parameter model mixed with plain query parameters is not
  supported — move the plain parameters into the model (FastAPI itself
  rejects the mixed form on the wire)"; required-header skips point at
  passthrough_headers, hidden-route skips at include_hidden, form/file
  skips note the route stays available over plain HTTP. Startup logs are
  the only place these explanations surface, so they teach rather than
  merely report.
- Parametrized generic models (`Page[Item]`) get clean GraphQL type names
  (`Page_Item`) derived from the generic origin plus argument names,
  instead of the sanitizer mangling the source spelling
  (`Page[Item].__name__` → `Page_Item_` with a warning). Uniqueness per
  parameterization is preserved and name collisions still dedup.

## 0.6.0 (2026-10-06)

### Added

- Non-Optional union responses (`-> Item | Error`, OpenAPI 3.1 oneOf style)
  now bridge as the JSON scalar instead of vanishing: which member arrives
  is a runtime decision, so the field carries no per-member promises —
  descriptions name the possible shapes ("raw JSON whose shape is one of:
  Item, Error"). Granularity is field-level: a union nested in a response
  model degrades only that field (`result: JSON!`), the rest stays
  selectable. Previously these routes (and any route with a union-typed
  field) were skipped entirely with "unsupported type". Request-body unions
  still skip — GraphQL has no input unions. Degraded routes are announced
  at startup ("bridged N route(s) as raw JSON (no field selection)" naming
  the union or the filtering kwarg, plus how to restructure to regain
  field selection) — the same notice covers response-filtering routes.
- Routes marked `deprecated=True` now map onto GraphQL-native deprecation:
  the field carries `@deprecated(reason: ...)` in the SDL, disappears from
  default introspection listings (`includeDeprecated: true` still shows
  it), and progressive-discovery briefs gain a `"deprecated": true` flag.
  Deprecated fields remain executable — deprecation is metadata, not
  access control. Previously the flag was silently ignored and deprecated
  routes surfaced as ordinary fields.

### Fixed

- `__version__` is now derived from installed package metadata instead of
  a hardcoded copy — the copy was left at 0.4.0 when 0.5.0 shipped, and
  the published package reported the wrong runtime version. pyproject is
  the single source of truth; a drift test pins it.
- Example: `legacy_stats` returned a coroutine (`return stats()` without
  `await`), so the deprecation showcase route failed validation.
- Routes using response-filtering serialization kwargs
  (`response_model_exclude_unset` / `_exclude_defaults` / `_include` /
  `_exclude` / `by_alias=False`) no longer break at runtime: the schema
  previously promised every model field as non-null, but filtering runs
  after validation, so a dropped (or retargeted) key nullified the whole
  object with "Cannot return null for non-nullable field". These routes
  now bridge as a raw `JSON` scalar — no skip, no field selection, no
  promises filtering would break — and their field description tells
  agents why. `exclude_none` stays structured: it only drops
  Optional-valued keys, which map to nullable fields anyway.
- Form/File routes are now skipped at scan time with an explicit reason
  ("form/file parameter 'x' cannot be bridged"). Previously a Form-only
  endpoint whose annotations were valid GraphQL scalars (`Annotated[str,
  Form()]`) slipped into the schema and always failed at runtime with 422
  (the invoker sends JSON bodies); File-based routes were skipped only by
  coincidence (`bytes`/`UploadFile` having no GraphQL mapping), not by an
  explicit rule. Side effect: the OAuth token endpoint (form-encoded
  credentials) can no longer leak into the schema without an exclude —
  the leak is now structurally impossible, with the exclude glob remaining
  defense-in-depth.
- Request bodies (and body-bound variables) carrying custom-scalar fields —
  `Decimal`, `UUID`, `datetime`/`date`/`time`, at any nesting depth —
  crashed at the wire with "Object of type Decimal is not JSON
  serializable": the scalars' parse_value produces typed Python objects
  inside resolver kwargs, and json.dumps cannot encode them. The invoker
  now converts them to their JSON wire forms at the request boundary
  (query/path params were already stringified). Found by a 19-case
  parameter-shape matrix probe.

## 0.5.0 (2026-10-05)

### Added

- **`document_cache_size` (default 128, 0 disables)** on
  `RouterGraphQLHandler` / `RouterMCP`: an LRU for the compile front half of
  execution — parse + depth guard + validate — keyed by the query string.
  Agents repeat documents constantly, and validation over an immutable
  schema is a pure function of the document, so a hit skips straight to
  execution (measured 1.52ms → 0.69ms on a 2-field query; new/unique
  queries are cost-neutral). Rejected documents cache their errors too.
  Execution results are never cached: per-call credentials run for real
  every time, and the cache lives on the handler instance so same-process
  handlers over different schemas never cross-contaminate.

### Changed

- The query string is parsed **once** per compile: the depth guard's parse
  is reused by execution (previously the guard parsed, discarded the AST,
  and graphql() re-parsed the same string). The handler now drives
  `validate` + `execute` directly instead of the `graphql()` wrapper.
- `validation_rules=` now **extends** the standard rule set instead of
  replacing it — passing a custom rule previously (and silently) dropped
  all 32 standard validation rules for that handler.

### Removed

- The GH Pages deploy workflow: `comparison/index.html` was deliberately
  folded into `comparison/README.md` in 0.4.0 (mermaid renders natively on
  GitHub), leaving the workflow deploying a directory with no entry page —
  the cause of its last failed run. The comparison report lives in the
  README; re-add a Pages workflow if a standalone visual page is ever
  wanted.

## 0.4.0 (2026-10-04)

### Added

- **Observability (OpenTelemetry), one waterfall per query**: the bridge
  injects W3C trace context (`traceparent`/`tracestate`/`baggage`) into
  every in-process route call — bridge-generated, so it flows regardless of
  `passthrough_headers`, and is a no-op without an SDK (new core dependency
  `opentelemetry-api`, non-recording by default). A `graphql.execute` span
  covers the GraphQL orchestration layer; route-call timeouts and
  concurrency queue waits surface as span events (`route.timeout`,
  `route.queue`). With fastmcp's tool spans and FastAPI >= 0.142's native
  route spans, one MCP query now lands as
  `tools/call > graphql.execute > GET /route` in a single trace (previously
  the route spans were orphan traces). Regression-locked by
  `tests/e2e/test_otel_propagation.py`; walkthrough in
  `examples/otel_smoke.md`.

- **`max_depth` (default 10, `None` disables)** on
  `RouterGraphQLHandler` / `RouterMCP`: recursive models make selection-set
  nesting unbounded and an MCP caller is an LLM that can emit runaway
  documents — overly deep ones are now rejected before execution with a
  validation-style error. Fragment spreads resolve inline (spreading a
  deep query across fragments cannot hide it; cyclic spreads are
  rejected), inline fragments are transparent, and the root selection set
  counts as depth 1. `validation_rules=` on the handler passes extra
  graphql-core validation rules through to `graphql()` for anything
  policy-shaped. README gained a "Hardening the bridge" section covering
  these knobs plus the fastmcp rate-limiting / response-limiting
  middleware (per-client by default) for the MCP face.

- **`max_concurrency` (default 16, `None` disables)** on
  `RouterGraphQLHandler` / `RouterMCP`: a bound on in-flight route calls
  across all queries. Sibling GraphQL fields resolve concurrently, so one
  wide query fans out into parallel ASGI calls that could hammer the
  wrapped app's upstream (DB, external APIs); the invoker-global semaphore
  bounds that fan-out. The slot is acquired inside the timeout window, so
  queueing time counts against `request_timeout` — a call waiting for a
  slot cannot outlive its own deadline.

- **`request_timeout` (default 30s, `None` disables)** on
  `RouterGraphQLHandler` / `RouterMCP`, threaded to the invoker. Enforced
  with `asyncio.wait_for` and surfaced as a field-level `TIMEOUT` error
  (http_status 504) — see Fixed below for why the httpx timeout alone
  could never fire.

- **`handler.skips`**: the scanner's skip report (path/method/reason per
  excluded route) is now caller-visible instead of log-only — CI can
  assert `skips == []` (or an expected set) so a route silently falling
  out of the schema fails the build. Completes the scanner's day-one
  "the caller decides whether skips are acceptable" contract.

- GraphiQL shows an actionable offline notice (10s) when the esm.sh CDN
  is unreachable, pointing at the raw `POST /graphql` endpoint.

### Fixed

- **max_depth no longer rejects introspection**: `__`-prefixed meta-fields
  don't count toward the depth guard — GraphiQL/codegen/IDE plugins ship a
  fixed ~15-deep introspection document, which the default `max_depth=10`
  rejected ("Error fetching schema" on a stock GraphiQL page). The guard
  still bounds runaway DATA selections.

- `RouteInvoker(timeout=...)` was dead configuration: httpx's
  `ASGITransport` never enforces timeouts (in-process calls bypass
  httpcore) — a route could hang forever regardless of the setting.
  Enforcement now lives in `asyncio.wait_for` inside `invoke()`.

- `content-type` / `accept` are refused by `filter_passthrough_headers`
  even when whitelisted: a forwarded `content-type` would retype the JSON
  body request (text/plain + json body → FastAPI 422). Custom headers
  still forward as before.

### Changed

- Progressive disclosure caches each domain's SDL fragment (the schema is
  immutable after build; agents re-explore the same domain often), and
  `DomainRegistry` answers `children()` from a precomputed index instead
  of a full node scan per `list_domains` call.

- `mount_to(auth_at_root=True)` now carries an explicit checklist comment
  of the fastmcp private surface it relies upon (per-route auth guard,
  app-level auth/request-context middleware, well-known routes) — the
  things to re-verify on any fastmcp major bump.

- **JSON pass-through for dynamic shapes**: endpoints and fields annotated
  `dict`, `dict[K, V]`, `Any` (and `list`s / model fields thereof) bridge
  as the `JSON` scalar instead of being skipped — an author declaring a
  dynamic shape gets reachability, not invisibility. The line drawn: an
  explicit `dict`/`Any` annotation passes through; a route with no
  annotation and no `response_model` still skips (no contract). Both
  directions: a `JSON` input argument lands as the raw request body.

- **`RouterMCP(auth=...)`**: optional fastmcp auth provider (e.g.
  `GitHubProvider`) passed through to `FastMCP` untouched — the MCP
  endpoint then speaks OAuth 2.1 (401 discovery, DCR, PKCE) and clients'
  Bearer tokens travel into route calls via `passthrough_headers` like any
  other caller's. The bridge verifies nothing. When `auth` is set,
  `mount_to` also re-exposes the provider's `/.well-known/*` discovery
  routes at the host app's root — the 401 challenge advertises them there
  (RFC 8414), but the mount alone would shift them under the mount path.
  `mount_to(..., auth_at_root=True)` goes further: MCP endpoint at `path`,
  OAuth/discovery routes at the host root — for reusing an IdP app whose
  registered callback URL lives at the root domain (subdirectory
  redirect_path). Implemented by splicing the wrapped app's routes (host
  routes keep precedence) and copying its middleware stack wholesale — a
  plain `Mount("")` catch-all would silently shadow host routes added
  later, and dropping the app-level middleware (auth verification,
  request-context capture) silently 401s every request.

- **Consumer example `examples/notes_oauth`**: a full Notes app behind
  GitHub OAuth — browser session cookie, `POST /auth/token` Bearer, and
  MCP OAuth 2.1 login (Claude Code) as three interchangeable credential
  carriers, with the fastmcp GitHub proxy reusing the app's OAuth callback
  via a redirect subdirectory (`auth_at_root`). Guarded by
  `tests/guards/test_example_public_api.py`: examples import the public API only.

### Fixed

- Python 3.10 support for recursive Pydantic models: CPython < 3.11 leaves
  ``list["Node"]`` with the plain string inside the PEP 585 generic (only
  ``typing.Union`` converts str args to ``ForwardRef``), and every evaluator
  only evaluates ``ForwardRef`` — so ``FieldInfo.annotation`` stayed
  unresolved and recursive models were skipped as unsupported types on 3.10
  while working on 3.11+. The type builder now rewraps string args as
  ``ForwardRef`` (leaving ``Literal`` values untouched — those strings are
  values, not types) and evaluates them against the model's namespaces,
  seeding the namespace with the model's own name (the enclosing scope binds
  a class name only after the class body runs, so neither module globals
  nor pydantic's parent-namespace snapshot can resolve a self-reference).

- The demo app imported ``datetime.UTC`` (Python 3.11+), failing import on
  3.10; it now uses ``timezone.utc``.

- A leaf field colliding with a child domain segment inside the same group
  (e.g. a `catalog` endpoint tagged `shop` plus routes tagged
  `shop:catalog`) was silently dropped: the child group field overwrote
  it in the object type, with no warning — and the progressive-disclosure
  index kept advertising the ghost field. Same-named leaves already failed
  fast with `DuplicateFieldError`; leaf-vs-subdomain collisions now fail
  fast the same way, naming both claimants.

- `mode="auto"` counted routes via `isinstance(app.routes, APIRoute)`,
  which counts ZERO on FastAPI >= 0.142 (include_router results are
  wrapped in `_IncludedRouter`) — a 30-route app stayed in simple mode,
  handing agents one giant SDL. It also ignored `include`/`exclude`: an
  app narrowed to 3 schema routes still switched to progressive. The
  threshold decision now uses the scanned route count (what actually
  enters the schema).

- Single non-model body parameters (e.g. `payload: dict[str, Any]`) were
  wrongly wrapped as `{"payload": ...}` — FastAPI gives a lone body param
  the WHOLE body unless `Body(embed=True)` or multiple body params are
  involved. `_body_embeds` now replicates FastAPI's predicate exactly.

### Changed

- **`headers_provider` removed; `passthrough_headers` defaults to
  `("authorization",)`** (breaking). Credentials now have a single source —
  the caller: each MCP/GraphQL client connects with its own credentials and
  the bridge forwards them (case-insensitive whitelist; an explicitly empty
  list disables forwarding). The server-side provider (and the
  credential-amplification risk it created when mounted publicly) is gone;
  machines without a user context configure the service credential on the
  MCP client side, or use `handler.execute(..., headers=...)` directly.
  With no HTTP request context (in-memory client) protected routes simply
  answer 401.

- **`RouterMCP.run()` is HTTP-only** (streamable HTTP with `host`/`port`
  parameters, default `127.0.0.1:8000`). The wrapped FastAPI app is a
  service whose routes speak HTTP, and per-caller credential passthrough
  needs an HTTP request context that stdio has no notion of — so the stdio
  transport and the `demo.mcp_stdio` entry point are removed (breaking).
  Use `mount_to(app, "/mcp")` to serve MCP on the app's own port.

## 0.3.0 (2026-10-03)

Schema shape + documentation wave (breaking).

### Changed

- **Renamed from `routerql` to `fastapi-mcp-neo`** (package
  `fastapi_mcp_neo`): the distribution name now carries both FastAPI and MCP.
  Exception prefixes `RouterQL*` became `GQLMCP*`; `RouterMCP` /
  `RouterGraphQLHandler` keep their Router-based names.

### Changed

- **Domain-grouped schema (UseCaseService-style hierarchy)**: fields live
  under their tag domain tree — a route tagged `shop:catalog` answers at
  `{ shop { catalog { list_products } } }`. Untagged routes join the domain of
  their first path segment. Domain SDL fragments now show the exact grouped
  address an agent should query.
- **GraphQL field names now come from the endpoint function name** (e.g.
  `async def get_user` → `get_user`) instead of being reconstructed from the
  URL path + verb. Path/query/body parameters still become the field's
  arguments. Two routes sharing a function name fail fast with
  `DuplicateFieldError` (previously the `_by_{param}` suffix silently
  disambiguated collection/item pairs).

### Added

- Upgraded the optional `mcp` extra to **fastmcp 4.x** (from 3.1): zero code
  changes needed — `FastMCP` construction, tool registration, `http_app`
  mounting and `run()` all carried over. Verified end-to-end (tools, queries,
  mutations over streamable HTTP) on 4.0.10.
- Mutation execution semantics pinned by tests: cross-domain writes are
  serial (spec-guaranteed at the mutation root); same-domain writes run in
  parallel. Mutation-only schemas now fail fast with guidance (GraphQL
  requires a Query root).
- **Argument descriptions**: `Query()/Path()/Body(description=...)` metadata
  maps onto GraphQL argument descriptions, completing the doc chain
  (model docstrings → types, `Field(description)` → fields, endpoint
  docstrings/`summary=` → fields).
- Runnable demo suite: `python -m demo` (all-in-one server, progressive mode),
  `demo.mcp_stdio`, `demo.mcp_walkthrough`; full documentation coverage in the
  demo app for inspection.

## 0.2.0 (2026-10-03)

Feature wave over the 0.1 core.

### Added

- **Progressive disclosure MCP tools** (4 layers over the tag tree):
  `list_domains` → `list_queries(domain)` / `list_mutations(domain)` →
  `get_query_schema(domain)` → `graphql_query`. Domain SDL fragments are
  filtered sub-schemas (only reachable types). Discovery is scoped per domain;
  execution always runs against the full schema. `mode="auto"` switches to
  progressive above `progressive_threshold` (default 25, configurable).
- **Query Parameter Models**: a lone `Annotated[Model, Query()]` flattens into
  individual alias-aware query arguments. Models mixed with plain query params
  are skipped with a reason.
- **GraphiQL playground + GraphQL-over-HTTP**: `handler.mount_graphql(app)`
  serves `/graphiql` (CDN build, explorer plugin) wired to `POST /graphql`
  accepting `{query, variables, operationName}`.
- **`mutation_include`** glob whitelist: with `allow_mutation=True`, write
  routes must match to become mutations.
- Restructured `demo/` nexusx-style.

### Fixed

- Type descriptions no longer inherit BaseModel's docstring when a model has
  none of its own.

## 0.1.0 (2026-10-03)

Initial release.

- RouterScanner: routes → field metadata (verb mapping, dependency-tree param
  flattening, SkipRecord taxonomy, tag domains).
- TypeBuilder: Pydantic → graphql-core (custom DateTime/Date/Time/UUID/Decimal
  scalars, Literal/Enum, cycle-safe registries, alias-first naming).
- RouteInvoker: in-process ASGI execution with lazy lifespan management and
  `headers_provider` credential passthrough; HTTP ≥ 400 → `GraphQLError`
  with `extensions.code=HTTP_{status}`.
- SchemaBuilder + RouterGraphQLHandler on graphql-core standard execution;
  route responses nullable per field so sibling results survive failures.
- RouterMCP: stdio/mount MCP server, `get_schema` + `graphql_query` /
  `graphql_mutation` with operation-type guards and hint-chain envelopes.

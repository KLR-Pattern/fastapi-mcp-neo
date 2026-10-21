Why fastapi-mcp-neo
Turn FastAPI into an agent-friendly, context-efficient MCP server

fastapi-mcp-neo turns any FastAPI app into an MCP server backed by a
GraphQL schema — with zero changes to your code. An agent gets 2-6
constant tools instead of one tool per endpoint, composes any routes into
a single query, and runs as the caller with the caller's own credentials.

THE BRIDGING PROMISE

If a route works over HTTP, it stays callable here. The bridge prefers
coverage over strictness: a response whose shape cannot be statically
promised (untyped, serialization-filtered, a union) degrades to a
documented raw-JSON field with a startup notice naming the cause and
the fix — never a silently vanishing route. `-> None` endpoints bridge
as Boolean success fields. Routes are dropped only when they cannot be
called correctly at all (Form/File bodies, which MCP's JSON tool
arguments cannot express; shapes GraphQL cannot type on the input
side). Migration should feel like adding a window onto your app, not
like passing an exam.

THE ORIGIN

We pointed an AI agent at our FastAPI service through an existing MCP
bridge. With five endpoints it worked. With fifty, the agent started every
task by swallowing a phone book of tool definitions, then walked the API
one endpoint per turn. The problem was never FastAPI, and never MCP — it
was the shape of the thing between them.

WHAT ONE-TOOL-PER-ENDPOINT BRIDGES GET WRONG AT SCALE

- Context grows linearly. ~110 tokens of tool catalog per endpoint:
  100 routes means ~11,000 tokens burned before the agent does anything,
  every session.
- No composition. One call = one endpoint; a three-part view costs three
  LLM turns. Turns, not milliseconds, are an agent's real latency.
- Whole payloads, all-or-nothing errors. No field selection (4.7x more
  bytes), and one failing endpoint kills the healthy results with it.
- Writes ungated, credentials borrowed. Everything writable by default;
  a server-side token means the agent stops acting as the user.

Fair note: below ~10 endpoints, this design is actually cheaper. The
failure is at scale — where real services live.

WHY NOW

Agents just became real clients of real APIs (MCP is now the default way
models touch software), context is still the scarce resource — priced per
token, paid every session — and the contract shape is still unsettled
(the spec's lazy-loading answer is a draft needing both sides). The window
is open while teams are still choosing.

THE GRAPHQL INSIGHT

GraphQL solved this exact problem ten years ago for mobile clients: ask a
large API for exactly what you need — typed, composable, one round trip,
partial results on partial failure. An agent is that client again,
sharper: the context window is the small screen, every tool call is a
round trip.

THE STRATEGY: DERIVE, DON'T DECORATE

1. Derive the contract from existing routes — tags become a domain tree,
   function names become field names, docstrings become schema
   descriptions. FastAPIMCP(app) is the whole setup.
2. Keep the tool set constant (2-6 tools); the schema is the contract, so
   context stays flat as the API grows.
3. Execute through the real app, as the caller — auth and middleware
   behave exactly as over HTTP; credentials come from the caller, writes
   are off by default.
4. Ship it production-grade — OAuth 2.1 login on the MCP endpoint,
   timeout/concurrency/depth guards, one OpenTelemetry trace per query.

STATUS

Early-stage (0.x), on PyPI. Suggestions are welcome and get acted on —
measured evidence and fast iteration over roadmap slides.

One line: MCP gave agents a way to connect; GraphQL gives them a contract
worth connecting to. fastapi-mcp-neo is the missing three lines of code
in between.

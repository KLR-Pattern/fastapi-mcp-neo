# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities privately:

- Email: allmonday@126.com
- Subject prefix: `[security] fastapi-mcp-neo`

Do **not** open a public GitHub issue for security matters.

You will receive an acknowledgment within 48 hours. We aim to release a
fix within 7 days for confirmed vulnerabilities affecting the default
configuration.

## Scope

- The `fastapi_mcp_neo` Python package published on PyPI
- The MCP server it exposes (transport, auth proxy, credential forwarding)
- Out of scope: vulnerabilities in your own FastAPI app's routes — the
  bridge executes them in-process with the caller's credentials and does
  not add its own authentication decisions

## Supported Versions

| Version | Supported |
|---|---|
| 0.12.x | ✅ |
| < 0.12 | ❌ |

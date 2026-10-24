"""An agent's-eye view of tools mode — no server needed.

    uv run --extra mcp python -m examples.tools_demo.walkthrough

Shows what the mode derives from the app: the per-route tool catalog with
annotations and typed schemas (body model and enum included), field
projection sizes, and the error channel.
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from examples.tools_demo.app import create_app
from fastapi_mcp_neo import FastAPIMCP


def show(title: str, payload: object) -> None:
    print(f"\n== {title} ==")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered if len(rendered) < 1200 else rendered[:1200] + "\n  …")


async def main() -> None:
    mcp = FastAPIMCP(create_app(), name="tools-demo", mode="tools")
    async with Client(mcp.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

        print("== 工具目录（一路由一工具，注解按 HTTP 方法推导）==")
        for name, t in sorted(tools.items()):
            ann = t.annotations
            hints = [
                h
                for h, on in (
                    ("readOnly", ann.read_only_hint),
                    ("destructive", ann.destructive_hint),
                )
                if on
            ]
            required = tools[name].inputSchema.get("required", [])
            print(f"  {name:12} [{','.join(hints) or '-'}]  required={required}")

        show(
            "create_note 的 inputSchema —— body 模型字段级类型 + enum",
            tools["create_note"].inputSchema,
        )

        full = await client.call_tool("get_note", {"note_id": 1})
        proj = await client.call_tool(
            "get_note", {"note_id": 1, "fields": "{ id title kind }"}
        )
        print("\n== 投影效果 ==")
        full_b = json.dumps(full.structured_content)
        proj_b = json.dumps(proj.structured_content)
        print(f"  整包  {len(full_b):3} B  {full_b}")
        print(f"  投影  {len(proj_b):3} B  {proj_b}")

        created = await client.call_tool(
            "create_note", {"payload": {"title": "Demo", "body": "via tools mode"}}
        )
        print("\n== 写工具（destructive 注解）==")
        print(f"  {json.dumps(created.structured_content)}")

        print("\n== 错误通道 ==")
        try:
            await client.call_tool("get_note", {"note_id": 999})
        except Exception as e:  # noqa: BLE001 - demo
            print(f"  404 → {type(e).__name__}: {str(e)[:70]}")
        try:
            await client.call_tool("get_note", {})
        except Exception as e:  # noqa: BLE001 - demo
            print(f"  缺参 → {type(e).__name__}: {str(e)[:70]}")
    await mcp.handler.aclose()


if __name__ == "__main__":
    asyncio.run(main())

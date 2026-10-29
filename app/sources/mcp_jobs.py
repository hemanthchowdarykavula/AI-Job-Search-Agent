from __future__ import annotations

import asyncio
import json
import time
from typing import Any


def _content_to_payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    # Some official MCP tools (e.g. HiringCafe) return structured job cards
    # in a namespaced _meta block and a human-readable Markdown content block.
    # Preserve both without changing existing structuredContent behavior.
    meta = getattr(result, "meta", None)
    if isinstance(meta, dict):
        for item in meta.values():
            if isinstance(item, dict) and isinstance(item.get("jobs"), list):
                payload = dict(item)
                payload["_mcp_text"] = "\n".join(
                    str(getattr(block, "text", "") or "")
                    for block in getattr(result, "content", []) or []
                )
                return payload
    texts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        texts.append(str(text))
        try:
            return json.loads(text)
        except Exception:
            continue
    # Full HiringCafe job details are Markdown-only text blocks, not JSON.
    # Preserve their content so resume tailoring can use the real complete JD.
    return {"_mcp_text": "\n".join(texts)} if texts else {}


def _exception_text(exc: BaseException) -> str:
    children = getattr(exc, "exceptions", None)
    if children:
        nested = "; ".join(_exception_text(child) for child in children)
        return f"{type(exc).__name__}: {exc} -> [{nested}]"
    return f"{type(exc).__name__}: {exc}"


async def _call(url: str, tool: str, arguments: dict) -> Any:
    try:
        from mcp import Client
    except ImportError as exc:
        raise RuntimeError("MCP job sources require the 'mcp' package. Run: pip install -r requirements.txt") from exc
    try:
        async with Client(url) as client:
            result = await client.call_tool(tool, arguments)
            return _content_to_payload(result)
    except BaseException as exc:
        raise RuntimeError(f"MCP call failed for {url} tool={tool}: {_exception_text(exc)}") from exc


def call_tool(url: str, tool: str, arguments: dict, retries: int = 3) -> Any:
    """Synchronous bridge with bounded retries for transient MCP/provider failures."""
    last_error = None
    for attempt in range(max(1, retries)):
        try:
            return asyncio.run(_call(url, tool, arguments))
        except RuntimeError as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(1.5 * (2 ** attempt))
    raise last_error or RuntimeError(f"MCP call failed for {url} tool={tool}")


def rows_from_payload(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("jobs", "results", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            for inner in ("jobs", "results", "items"):
                rows = value.get(inner)
                if isinstance(rows, list):
                    return [x for x in rows if isinstance(x, dict)]
    return []

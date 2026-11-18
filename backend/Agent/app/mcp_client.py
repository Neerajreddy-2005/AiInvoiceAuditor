"""MCP client: how the LangGraph agents discover and invoke their tools.

  discover()          tools/list  -> names and descriptions offered by the server
  call(name, **args)  tools/call  -> the tool's JSON result

Synchronous on purpose: workflow and RAG nodes run in worker threads, so
each call runs its own short event loop. The LangFuse trace context rides
along in the request's _meta, so tool spans nest under the calling node.
Arguments and results cross as JSON: amounts and dates arrive as strings.
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import llm, tracing

log = logging.getLogger(__name__)
_tools: dict[str, str] = {}
TOOL_TIMEOUT = timedelta(seconds=900)  # above extraction's own document deadline


class MCPError(RuntimeError):
    pass


def url() -> str:
    return os.getenv("MCP_URL", "http://127.0.0.1:8100/mcp/")


@asynccontextmanager
async def _open_session():
    """Replaced in tests by an in-memory connection to app.mcp_server."""
    async with streamablehttp_client(url(), timeout=30, sse_read_timeout=TOOL_TIMEOUT.total_seconds()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _with_session(fn):
    async with _open_session() as session:
        return await fn(session)


def discover() -> dict[str, str]:
    result = asyncio.run(_with_session(lambda s: s.list_tools()))
    _tools.clear()
    _tools.update({t.name: t.description or "" for t in result.tools})
    return dict(_tools)


def wait_until_ready(timeout: float = 120) -> dict[str, str]:
    """The server starts with the API, a moment after the monitor thread."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            tools = discover()
            log.info("MCP: discovered %d tools: %s", len(tools), ", ".join(sorted(tools)))
            return tools
        except Exception as exc:
            if time.monotonic() > deadline:
                raise MCPError(f"MCP server at {url()} is not reachable: {exc}") from exc
            time.sleep(1)


def call(name: str, **arguments) -> dict:
    if name not in _tools:
        discover()
        if name not in _tools:
            raise MCPError(f"MCP tool '{name}' is not offered by {url()}")
    meta = tracing.mcp_meta()
    result = asyncio.run(_with_session(lambda s: s.call_tool(
        name, arguments, read_timeout_seconds=TOOL_TIMEOUT, meta=meta)))
    if result.isError:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        text = text.removeprefix(f"Error executing tool {name}: ")  # keep the cause readable for reviewers
        if "daily limit of" in text:  # llm.DailyLimitReached: callers show their own message for it
            raise llm.DailyLimitReached(text)
        raise MCPError(f"{name}: {text}")
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)

"""LangFuse observability (LangFuse Cloud by default, Python SDK v2).

One trace per invoice workflow run and per question:
  trace  invoice-workflow (session = LangGraph thread id)
   ├─ span   node, e.g. translate
   │   └─ span   MCP tool, e.g. lang-bridge   (trace id carried in MCP _meta)
   │       └─ generation  LiteLLM -> Gemini  (linked via existing_trace_id)
   └─ ...

Tracing is optional: without LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
every helper here is a no-op, and LangFuse being down never fails a request
(the SDK sends in a background thread).
"""
import functools
import logging
import os

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


if enabled():
    from langfuse.decorators import langfuse_context, observe


def setup() -> None:
    if not enabled():
        log.info("LangFuse tracing disabled (no LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
        return
    import litellm
    litellm.success_callback = ["langfuse"]
    litellm.failure_callback = ["langfuse"]
    log.info("LangFuse tracing enabled (%s)", os.getenv("LANGFUSE_HOST"))


def current_ids() -> tuple[str | None, str | None]:
    """(trace id, observation id) of the active span, if any."""
    if not enabled():
        return None, None
    try:
        return langfuse_context.get_current_trace_id(), langfuse_context.get_current_observation_id()
    except Exception:
        return None, None


def name_trace(name: str, session_id: str | None = None, **metadata) -> None:
    if enabled():
        langfuse_context.update_current_trace(name=name, session_id=session_id, metadata=metadata or None)


def llm_metadata(task: str | None) -> dict:
    """LiteLLM metadata that attaches a generation to the current trace."""
    trace_id, observation_id = current_ids()
    meta = {"generation_name": task or "chat", "tags": ["agent", task or "chat"]}
    if trace_id:
        meta["existing_trace_id"] = trace_id
        if observation_id:
            meta["parent_observation_id"] = observation_id
    return meta


def mcp_meta() -> dict | None:
    """Trace context for an outgoing MCP request (like a traceparent header)."""
    trace_id, observation_id = current_ids()
    return {"langfuse_trace_id": trace_id, "langfuse_observation_id": observation_id} if trace_id else None


def continue_trace(name: str, fn, meta: dict | None):
    """Run fn() as a span named `name`, inside the caller's trace when meta carries one."""
    # Calls outside any trace (the monitor's idle folder scans) are not traced:
    # they would bury the real invoice and question traces.
    if not enabled() or not (meta or {}).get("langfuse_trace_id"):
        return fn()

    @observe(name=name)
    def _span():
        return fn()

    return _span(langfuse_parent_trace_id=meta["langfuse_trace_id"],
                 langfuse_parent_observation_id=meta.get("langfuse_observation_id"))


def score(scores: dict) -> None:
    """Attach the RAG Triad scores to the current trace (LangFuse 'Scores')."""
    if not enabled():
        return
    for name in ("answer_relevance", "groundedness", "context_relevance", "average"):
        if isinstance(scores.get(name), (int, float)):
            langfuse_context.score_current_trace(name=name, value=float(scores[name]))


def traced(name: str):
    """Decorator: a span (or a new trace at top level) named `name`."""
    def wrap(fn):
        return functools.wraps(fn)(observe(name=name)(fn)) if enabled() else fn
    return wrap

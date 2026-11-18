"""LangFuse observability (LangFuse Cloud, OpenTelemetry-based Python SDK v5).

One trace per invoice workflow run and per question:
  trace  invoice-workflow (session = LangGraph thread id)
   ├─ span   node, e.g. translate
   │   └─ span   MCP tool, e.g. mcp:lang-bridge   (trace id carried in MCP _meta)
   │       └─ generation  Gemini call (model, prompt, reply, token usage)
   └─ ...

Tracing is optional: without LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
every helper here is a no-op, and LangFuse being down never fails a request
(the SDK exports in a background thread).
"""
import functools
import logging
import os
from contextlib import contextmanager, nullcontext

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


if enabled():
    from langfuse import get_client, observe, propagate_attributes


def setup() -> None:
    if not enabled():
        log.info("LangFuse tracing disabled (no LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
        return
    get_client()
    log.info("LangFuse tracing enabled (%s)", os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST"))


def flush() -> None:
    if enabled():
        get_client().flush()


def current_ids() -> tuple[str | None, str | None]:
    """(trace id, observation id) of the active span, if any."""
    if not enabled():
        return None, None
    try:
        client = get_client()
        return client.get_current_trace_id(), client.get_current_observation_id()
    except Exception:
        return None, None


def attributes(session_id: str | None = None, **metadata):
    """Context manager: session and metadata for every observation started inside it."""
    if not enabled():
        return nullcontext()
    return propagate_attributes(session_id=session_id,
                                metadata={k: str(v) for k, v in metadata.items() if v is not None} or None)


@contextmanager
def generation(name: str, model: str, prompt, as_type: str = "generation"):
    """A model call as a LangFuse generation. Yields record(output, usage=None)."""
    if not enabled():
        yield lambda output, usage=None: None
        return
    failure = None
    with get_client().start_as_current_observation(name=name, as_type=as_type, model=model, input=prompt) as obs:
        def record(output, usage=None):
            details = {k: int(v) for k, v in {
                "input": getattr(usage, "prompt_tokens", None),
                "output": getattr(usage, "completion_tokens", None)}.items() if v is not None}
            obs.update(output=output, usage_details=details or None)
        try:
            yield record
        except Exception as exc:
            # Only the type: provider errors can include the request URL, and Gemini's carries the API key.
            obs.update(level="ERROR", status_message=type(exc).__name__)
            failure = exc
    if failure is not None:
        raise failure  # after the span closes, so the raw message is never recorded on it


def mcp_meta() -> dict | None:
    """Trace context for an outgoing MCP request (like a traceparent header)."""
    trace_id, observation_id = current_ids()
    return {"langfuse_trace_id": trace_id, "langfuse_observation_id": observation_id} if trace_id else None


def continue_trace(name: str, fn, meta: dict | None):
    """Run fn() as a span named `name`, inside the caller's trace when meta carries one."""
    # Calls outside any trace (an MCP client's idle discovery) are not traced:
    # they would bury the real invoice and question traces.
    if not enabled() or not (meta or {}).get("langfuse_trace_id"):
        return fn()

    @observe(name=name, capture_input=False)
    def _span():
        return fn()

    return _span(langfuse_trace_id=meta["langfuse_trace_id"],
                 langfuse_parent_observation_id=meta.get("langfuse_observation_id"))


def score(scores: dict) -> None:
    """Attach the RAG Triad scores to the current trace (LangFuse 'Scores')."""
    if not enabled():
        return
    for name in ("answer_relevance", "groundedness", "context_relevance", "average"):
        if isinstance(scores.get(name), (int, float)):
            get_client().score_current_trace(name=name, value=float(scores[name]))


def traced(name: str):
    """Decorator: a span (or a new trace at top level) named `name`."""
    def wrap(fn):
        return functools.wraps(fn)(observe(name=name)(fn)) if enabled() else fn
    return wrap

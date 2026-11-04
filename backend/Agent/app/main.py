"""Agent service: runs the invoice monitor and workflow, and the API the
frontend calls (invoices, feedback, re-validation, questions). Port 8000.
Every route needs a signed-in user, except the bare /health status.

Startup fails fast: if PostgreSQL, Redis, Qdrant, mock-erp or the Gemini
configuration is unavailable, the process exits with a clear error
instead of starting in a broken state.
"""
import ctypes
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import cache, db, repository as repo, workflow
from .auth import current_reviewer
from .erp_client import client as erp_client
from .monitor_agent import monitor
from .parsing import parse_date, parse_number
from .rag import indexing, store
from .rag.graph import answer_query
from .settings import agent_config, prompts, rules, settings

logging.basicConfig(stream=sys.stdout, level=settings().log_level,
                    format="%(asctime)s %(levelname)s [agent] %(name)s: %(message)s")
# Request logs would otherwise include Gemini URLs, which carry the API key.
for noisy in ("httpx", "httpx2", "httpcore", "LiteLLM", "litellm", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("agent")


def _startup_checks() -> None:
    s = settings()
    steps = [
        ("configuration", lambda: (rules(), agent_config(), prompts())),
        ("Gemini credentials", s.check_gemini_credentials),
        (f"PostgreSQL at {s.db_host}:{s.db_port}", db.init),
        ("Redis", lambda: cache.init(s.redis_url)),  # the URL holds a password, so it isn't logged
        (f"Qdrant at {s.qdrant_url}", store.ensure_collection),
        (f"mock-erp at {s.erp_base_url}", lambda: erp_client().health()),
        ("LangGraph checkpointer (schema langgraph)", workflow.init),
    ]
    for name, check in steps:
        try:
            check()
        except Exception as exc:
            log.critical("Startup check failed: %s: %s", name, exc)
            raise RuntimeError(f"{name}: {exc}") from exc
        log.info("Startup check passed: %s", name)
    if not s.incoming_dir.is_dir():
        raise RuntimeError(f"Incoming directory {s.incoming_dir} is not mounted")
    if not s.db_sslrootcert:
        log.warning("DB_SSLROOTCERT is not set: the database connection is encrypted, "
                    "but the server's identity is not verified")


def _hide_secrets_from_child_processes() -> None:
    """File parsers run as the same user; this stops them reading /proc/<agent>/environ."""
    PR_SET_DUMPABLE = 4
    ctypes.CDLL(None).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _hide_secrets_from_child_processes()
    _startup_checks()
    interrupted = repo.recover_interrupted()
    if interrupted:
        log.warning("Recovered invoices interrupted by a restart: %s", interrupted)
    threading.Thread(target=indexing.ensure_index_on_startup, name="index-check", daemon=True).start()
    monitor.start()
    yield
    monitor.stop()
    db.close()


MAX_BODY_BYTES = 64 * 1024  # the same limit as nginx, for hosts where nginx isn't in front


class _BodyTooLarge(Exception):
    pass


class EdgeGuards:
    """Body size limit and response headers, kept here so they survive a change of host."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > MAX_BODY_BYTES:
            await JSONResponse(status_code=413, content={"detail": "Request is too large"})(scope, receive, send)
            return
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY_BYTES:  # a chunked body declares no length
                raise _BodyTooLarge()
            return message

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message["headers"], (b"x-content-type-options", b"nosniff"),
                                      (b"cache-control", b"no-store")]
            await send(message)

        await self.inner(scope, limited_receive, with_headers)


app = FastAPI(title="AI Invoice Auditor – agent", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(EdgeGuards)
api = APIRouter(dependencies=[Depends(current_reviewer)])


# ---------------------------------------------------------------- rate limits

_recent_calls: dict[tuple[str, str], deque] = defaultdict(deque)
_recent_calls_lock = threading.Lock()


def rate_limited(action: str):
    """Per-reviewer limit from configs/agent.yaml. One agent process, so memory is enough."""

    def check(reviewer: str = Depends(current_reviewer)) -> str:
        limit = agent_config()["rate_limits"][action]
        now = time.monotonic()
        with _recent_calls_lock:
            calls = _recent_calls[(reviewer, action)]
            while calls and now - calls[0] >= limit["per_seconds"]:
                calls.popleft()
            if len(calls) >= limit["calls"]:
                retry_after = int(limit["per_seconds"] - (now - calls[0])) + 1
                raise HTTPException(429, f"Too many requests. Try again in {retry_after} seconds.",
                                    headers={"Retry-After": str(retry_after)})
            calls.append(now)
        return reviewer

    return check


# ---------------------------------------------------------------- health

PUBLIC_HEALTH_CACHE_SECONDS = 5
_public_health: tuple[float, bool, str] = (0.0, False, "degraded")
_public_health_refresh = threading.Lock()


def _health() -> tuple[bool, dict]:
    checks = {}
    for name, check in (("postgres", db.ping), ("redis", cache.ping),
                        ("qdrant", lambda: store.client().get_collections()),
                        ("mock_erp", lambda: erp_client().health())):
        try:
            check()
            checks[name] = "ok"
        except Exception:
            log.exception("Health check failed: %s", name)
            checks[name] = "error"
    healthy = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if healthy else "degraded", "checks": checks,
            "monitor": {"last_scan_at": monitor.last_scan_at, "last_error": monitor.last_error}}
    return healthy, body


@app.get("/health")
def health():
    """Public, for container health checks: says nothing about why, and can't be used to hammer the backends."""
    global _public_health
    # One caller refreshes at a time; the others get the last answer instead of queueing on a slow backend.
    if time.monotonic() - _public_health[0] >= PUBLIC_HEALTH_CACHE_SECONDS and _public_health_refresh.acquire(blocking=False):
        try:
            healthy, body = _health()
            _public_health = (time.monotonic(), healthy, body["status"])
        finally:
            _public_health_refresh.release()
    _, healthy, status = _public_health
    return JSONResponse(status_code=200 if healthy else 503, content={"status": status})


@api.get("/system/health")
def system_health():
    return _health()[1]


# ---------------------------------------------------------------- reads

@api.get("/invoices")
def list_invoices():
    return repo.list_invoices()


@api.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: UUID):
    detail = repo.invoice_detail(str(invoice_id))
    if detail is None:
        raise HTTPException(404, "Invoice not found")
    return detail


@api.get("/invoices/{invoice_id}/runs/{run_id}/report")
def run_report(invoice_id: UUID, run_id: int):
    report = repo.run_report(str(invoice_id), run_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    return report


# ---------------------------------------------------------------- processing

@api.post("/process/scan", status_code=202, dependencies=[Depends(rate_limited("scan"))])
def scan_now():
    monitor.trigger()
    return {"status": "scan requested"}


@api.post("/invoices/{invoice_id}/retry", status_code=202, dependencies=[Depends(rate_limited("retry"))])
def retry_invoice(invoice_id: UUID, background: BackgroundTasks):
    invoice_id = str(invoice_id)
    row = repo.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(404, "Invoice not found")
    if row["processing_status"] != "failed" or not row.get("thread_id"):
        raise HTTPException(409, f"Only failed invoices can be retried (status: {row['processing_status']})")
    if not repo.claim_retry(invoice_id):
        raise HTTPException(409, "This invoice is already being retried")

    def _run():
        try:
            workflow.resume(invoice_id, row["thread_id"])
        except workflow.WorkflowError as exc:
            log.error("Retry of %s failed: %s", invoice_id, exc)

    background.add_task(_run)
    return {"status": "retry started", "resumes_at": row.get("failed_stage")}


# ---------------------------------------------------------------- human in the loop

HEADER_TYPES = {"invoice_number": "text", "invoice_date": "date", "vendor_name": "text", "po_number": "text",
                "currency": "currency", "subtotal_amount": "decimal", "tax_rate_pct": "decimal",
                "tax_amount": "decimal", "total_amount": "decimal"}
LINE_TYPES = {"item_code": "text", "description": "text", "quantity": "decimal", "unit_price": "decimal",
              "line_total": "decimal"}


class FeedbackIn(BaseModel):
    feedback_type: Literal["correction", "decision", "comment"] = "correction"
    field_name: str | None = None
    line_number: int | None = None
    corrected_value: str | None = Field(None, max_length=1000)
    comment: str | None = Field(None, max_length=2000)


def _normalise(kind: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(422, "The corrected value is empty")
    if kind == "decimal":
        number = parse_number(value)
        if number is None or not number.is_finite() or abs(number) >= 10 ** 12 or abs(number.as_tuple().exponent) > 12:
            raise HTTPException(422, f"'{value}' is not a valid number")
        return str(number)
    if kind == "date":
        parsed = parse_date(value)
        if parsed is None:
            raise HTTPException(422, f"'{value}' is not a recognisable date (use YYYY-MM-DD)")
        return parsed.isoformat()
    if kind == "currency":
        if len(value) != 3 or not value.isalpha():
            raise HTTPException(422, "Currency must be a 3-letter ISO code such as USD")
        return value.upper()
    return value


@api.post("/invoices/{invoice_id}/feedback", status_code=201)
def add_feedback(invoice_id: UUID, body: FeedbackIn, reviewer: str = Depends(rate_limited("feedback"))):
    """Append-only: the original extraction is never modified."""
    invoice_id = str(invoice_id)
    row = repo.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(404, "Invoice not found")

    if body.feedback_type == "correction":
        if body.corrected_value is None:
            raise HTTPException(422, "corrected_value is required for a correction")
        if body.line_number is None:
            if body.field_name not in HEADER_TYPES:
                raise HTTPException(422, f"Field '{body.field_name}' cannot be corrected")
            original = row.get(body.field_name)
            value = _normalise(HEADER_TYPES[body.field_name], body.corrected_value)
        else:
            if body.field_name not in LINE_TYPES:
                raise HTTPException(422, f"Line field '{body.field_name}' cannot be corrected")
            line = next((i for i in repo.get_line_items(invoice_id) if i["line_number"] == body.line_number), None)
            if line is None:
                raise HTTPException(422, f"Invoice has no line {body.line_number}")
            original = line.get(body.field_name)
            value = _normalise(LINE_TYPES[body.field_name], body.corrected_value)
        feedback_id = repo.insert_feedback(
            invoice_id=invoice_id, feedback_type="correction", field_name=body.field_name,
            line_number=body.line_number, original_value=original, corrected_value=value,
            comment=body.comment, corrected_by=reviewer)
        return {"id": feedback_id, "original_value": original, "corrected_value": value}

    if body.feedback_type == "decision":
        if body.corrected_value not in ("approve", "review", "reject"):
            raise HTTPException(422, "A decision must be approve, review or reject")
        if row["processing_status"] != "completed":
            raise HTTPException(409, "Only fully processed invoices can be decided "
                                     f"(status: {row['processing_status']})")
        feedback_id = repo.insert_feedback(
            invoice_id=invoice_id, feedback_type="decision", field_name="recommendation", line_number=None,
            original_value=row.get("recommendation"), corrected_value=body.corrected_value,
            comment=body.comment, corrected_by=reviewer)
        return {"id": feedback_id}

    if not (body.comment or "").strip():
        raise HTTPException(422, "A comment is required")
    feedback_id = repo.insert_feedback(
        invoice_id=invoice_id, feedback_type="comment", field_name=None, line_number=None,
        original_value=None, corrected_value=None, comment=body.comment, corrected_by=reviewer)
    return {"id": feedback_id}


@api.post("/invoices/{invoice_id}/revalidate")
def revalidate(invoice_id: UUID, reviewer: str = Depends(rate_limited("revalidate"))):
    try:
        return workflow.revalidate(str(invoice_id), reviewer)
    except LookupError as exc:
        raise HTTPException(404, "Invoice not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except workflow.WorkflowError as exc:
        raise HTTPException(502, "The re-check could not finish. Please try again.") from exc


# ---------------------------------------------------------------- questions

class QueryIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    invoice_id: UUID | None = None


@api.post("/query", dependencies=[Depends(rate_limited("query")), Depends(rate_limited("query_daily"))])
def query(body: QueryIn):
    try:
        return answer_query(body.question, str(body.invoice_id) if body.invoice_id else None)
    except Exception as exc:
        log.exception("Query failed")
        raise HTTPException(502, "Could not answer the question. Please try again.") from exc


_reindex_running = threading.Lock()


@api.post("/rag/reindex", status_code=202, dependencies=[Depends(rate_limited("reindex"))])
def reindex(background: BackgroundTasks):
    """Rebuild the Qdrant collection from PostgreSQL alone."""
    if not _reindex_running.acquire(blocking=False):
        raise HTTPException(409, "A rebuild is already running")

    def _run():
        try:
            log.info("Rebuild result: %s", indexing.rebuild_all())
        finally:
            _reindex_running.release()

    background.add_task(_run)
    return {"status": "rebuild started"}


@api.get("/rag/stats")
def rag_stats():
    return {"collection": store.collection_name(), "points": store.point_count()}


app.include_router(api)

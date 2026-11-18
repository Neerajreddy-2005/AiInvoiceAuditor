"""Invoice workflow (LangGraph, checkpointed to PostgreSQL schema langgraph).

        ┌─► extract_pdf ───┐
 START ─┼─► extract_docx ──┼─► translate ─► validate_data ─► validate_business ─► report ─► index ─► END
        ├─► extract_image ─┘                      ▲
        └─► (revalidate) ─────────────────────────┘

Each node calls its tool over MCP (app.mcp_server; the helpers below are
what those tools run) and persists the result to the audit schema, so all
audit writes stay in the nodes. If a node fails
(Gemini throttled, mock-erp down), the invoice is marked failed and the
next attempt resumes from the failed node using the checkpoint, so OCR
and translation are never repeated.

State holds only JSON-safe values (amounts as strings, dates as ISO).
"""
import logging
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from . import db, guardrails, llm, mcp_client, tracing
from . import report as reporting
from . import repository as repo
from . import storage
from .erp_client import client as erp_client
from .extraction import extract, file_sha256
from .parsing import parse_invoice, parse_number
from .rag import indexing
from .settings import rules, settings
from .translate import used_model
from .validation import (discrepancy, normalise_invoice_number, recover_missing_fields, same_vendor,
                         validate_business, validate_invoice_data)

log = logging.getLogger(__name__)


class InvoiceState(TypedDict, total=False):
    mode: str                      # ingest | revalidate
    invoice_id: str
    file_name: str
    file_path: str
    file_type: str
    email_metadata: dict | None
    raw_text: str
    extraction_method: str
    invoice: dict
    line_items: list
    source_language: str | None
    translated_text: str
    translation_confidence: float | None
    ai_translated: bool
    recovered_fields: dict
    trigger: str
    triggered_by: str | None
    corrections_applied: list
    feedback_ids: list
    run_id: int
    run_number: int
    discrepancies: list
    missing_fields: list
    line_matches: list
    recommendation: str
    validation_status: str


class WorkflowError(RuntimeError):
    def __init__(self, stage: str | None, cause: Exception):
        super().__init__(f"{stage or 'workflow'} failed: {type(cause).__name__}: {cause}")
        self.stage = stage


# ---------------------------------------------------------------- nodes

def route_entry(state: InvoiceState) -> str:
    if state.get("mode") == "revalidate":
        return "validate_data"
    return {"pdf": "extract_pdf", "docx": "extract_docx", "image": "extract_image"}[state["file_type"]]


@contextmanager
def _local_file(file_path: str):
    """A file in the watched folder as it is, or an uploaded one as a temporary copy."""
    if not file_path.startswith(storage.SCHEME):
        yield Path(file_path)
        return
    key = file_path.removeprefix(storage.SCHEME)
    with tempfile.TemporaryDirectory() as folder:
        local = Path(folder) / f"upload{Path(key).suffix}"
        storage.download(key, local)
        yield local


def harvest(file_path: str, kind: str) -> dict:
    """The data-harvester tool: read a local file (in a limited child process) and parse it."""
    result = extract(Path(file_path), kind)
    parsed = parse_invoice(result.text, rules().get("currency_symbol_map"))
    extras = {**parsed["extras"], "extraction": {"pages": result.pages, "ocr_pages": result.ocr_pages}}
    return {"raw_text": result.text, "extraction_method": result.method, "fields": parsed["fields"],
            "line_items": parsed["line_items"], "extras": extras}


def _extract(state: InvoiceState, kind: str) -> dict:
    # The audit trail records one checksum, so it must describe the content audited here.
    claimed = repo.get_invoice(state["invoice_id"])["file_checksum"]
    with _local_file(state["file_path"]) as local:
        if file_sha256(local) != claimed:
            raise ValueError("The file changed after it was detected")
        result = mcp_client.call("data-harvester", file_path=str(local), file_type=kind)
    repo.save_extraction(state["invoice_id"], method=result["extraction_method"], raw_text=result["raw_text"],
                         fields=result["fields"], line_items=result["line_items"], extras=result["extras"])
    return {"raw_text": result["raw_text"], "extraction_method": result["extraction_method"],
            "invoice": result["fields"], "line_items": result["line_items"]}


@tracing.traced("extract_pdf")
def extract_pdf(state: InvoiceState) -> dict:
    return _extract(state, "pdf")


@tracing.traced("extract_docx")
def extract_docx(state: InvoiceState) -> dict:
    return _extract(state, "docx")


@tracing.traced("extract_image")
def extract_image(state: InvoiceState) -> dict:
    return _extract(state, "image")


@tracing.traced("translate")
def translate_node(state: InvoiceState) -> dict:
    hint = (state.get("email_metadata") or {}).get("language")
    # The sidecar comes from outside: only a short language code may reach the prompt.
    if not (isinstance(hint, str) and re.fullmatch(r"[A-Za-z-]{2,8}", hint)):
        hint = None
    result = mcp_client.call("lang-bridge", text=state["raw_text"], language_hint=hint)
    details = {k: v for k, v in result.items() if k not in ("translated_text",)}
    repo.save_translation(state["invoice_id"], source_language=result["source_language"],
                          translated_text=result["translated_text"], confidence=result["confidence"],
                          details=details)
    return {"source_language": result["source_language"], "translated_text": result["translated_text"],
            "translation_confidence": result["confidence"], "ai_translated": used_model(result["method"])}


def _normalise_recovered(field: str, value: str) -> str | None:
    if field == "currency":
        # Unknown codes stay missing, for review: the model's reply must not be able to cause a rejection.
        code = value.upper()
        return code if code in rules()["accepted_currencies"] else None
    if field == "total_amount":
        number = parse_number(value)
        return None if number is None else str(number)
    return value


def check_data(invoice_id: str, invoice: dict, line_items: list[dict], document_text: str,
               recovered_fields: dict, corrected: list[str], first_read: bool) -> dict:
    """The data-completeness-checker tool: rules.yaml checks, duplicates and the injection guardrail."""
    corrected = set(corrected)
    same_number = repo.find_same_number(invoice_id, normalise_invoice_number(invoice.get("invoice_number")),
                                        include_later=first_read or bool(corrected & {"invoice_number", "vendor_name"}))
    duplicates = [d for d in same_number if same_vendor(invoice.get("vendor_name"), d["vendor_name"], rules())]
    po_repeats = repo.find_earlier_on_po(invoice_id, invoice.get("po_number"),
                                         include_later=first_read or "po_number" in corrected)
    result = validate_invoice_data(invoice, line_items, rules(), duplicates, po_repeats, recovered_fields)
    hits = guardrails.find_injection(document_text)
    if hits:
        # Never auto-approved: a person reads what the document asked the AI to do.
        result["discrepancies"].append(discrepancy(
            "rai_guardrail", "document_text", action="manual_review", invoice_value=hits[0][:200],
            message=f'The document contains text that looks like an instruction to the AI ("{hits[0][:80]}"), '
                    "so a person must review it"))
    return result


@tracing.traced("validate_data")
def validate_data(state: InvoiceState) -> dict:
    invoice = dict(state["invoice"])
    first_read = state.get("mode") != "revalidate"
    recovered_fields = state.get("recovered_fields") or {}
    if first_read:
        wanted = [f for f in [*rules()["required_fields"]["header"], "po_number"] if not invoice.get(f)]
        if wanted:
            try:
                recovered = recover_missing_fields(state["raw_text"], wanted, rules().get("currency_symbol_map"))
                accepted = {f: v for f, v in ((f, _normalise_recovered(f, v))
                                              for f, v in recovered["accepted"].items()) if v}
                invoice.update(accepted)
                repo.fill_recovered_fields(state["invoice_id"], accepted, recovered["rejected"])
                recovered_fields = accepted
            except llm.LLMError as exc:
                log.warning("Missing-field recovery skipped: %s", exc)

    corrected = sorted({c["field"] for c in state.get("corrections_applied") or []})
    document_text = f"{state.get('raw_text') or ''}\n{state.get('translated_text') or ''}"
    result = mcp_client.call("data-completeness-checker", invoice_id=state["invoice_id"], invoice=invoice,
                             line_items=state["line_items"], document_text=document_text,
                             recovered_fields=recovered_fields, corrected=corrected, first_read=first_read)
    run_id, run_number = repo.start_run(state["invoice_id"], state["trigger"], state.get("triggered_by"),
                                        state.get("corrections_applied") or [], result["discrepancies"])
    return {"invoice": invoice, "run_id": run_id, "run_number": run_number,
            "discrepancies": result["discrepancies"], "missing_fields": result["missing_fields"]}


def check_business(invoice: dict, line_items: list[dict]) -> dict:
    """The business-validation tool: compare with the ERP (mock-erp HTTP API)."""
    return validate_business(invoice, line_items, rules(), erp_client())


@tracing.traced("validate_business")
def validate_business_node(state: InvoiceState) -> dict:
    result = mcp_client.call("business-validation", invoice=state["invoice"], line_items=state["line_items"])
    repo.insert_discrepancies(state["run_id"], state["invoice_id"], result["discrepancies"])
    if state.get("mode") != "revalidate":
        repo.set_vendor_id(state["invoice_id"], result["vendor_id"])
    return {"discrepancies": state["discrepancies"] + result["discrepancies"],
            "line_matches": result["line_matches"]}


REPORT_FIELDS = ("invoice_id", "run_number", "trigger", "triggered_by", "file_name", "extraction_method", "invoice",
                 "line_items", "source_language", "translation_confidence", "discrepancies", "missing_fields")


def build_audit_report(audit: dict, ai_translated: bool) -> dict:
    """The insight-reporter tool: rule-based recommendation, report, AI summary and HTML."""
    recommendation, status, reasons = reporting.recommend(
        audit["discrepancies"], audit.get("translation_confidence"), rules(), ai_translated)
    report = reporting.build_report(**audit, recommendation=recommendation, validation_status=status,
                                    reasons=reasons)
    report["summary"] = reporting.summarize(report)
    return {"report": report, "html": reporting.render_html(report)}


@tracing.traced("report")
def report_node(state: InvoiceState) -> dict:
    audit = {k: state.get(k) for k in REPORT_FIELDS}
    audit["line_matches"] = state.get("line_matches") or []
    audit["corrections_applied"] = state.get("corrections_applied") or []
    # A checkpoint from before ai_translated existed: don't assume English.
    out = mcp_client.call("insight-reporter", audit=audit, ai_translated=state.get("ai_translated", True))
    report, html = out["report"], out["html"]
    recommendation, status = report["recommendation"], report["validation_status"]
    try:
        json_path, html_path = reporting.write_files(report, html, settings().reports_dir)
    except OSError as exc:
        # The database is the system of record; files are a convenience.
        log.warning("Could not write report files: %s", exc)
        json_path = html_path = None
    repo.complete_run(state["run_id"], state["invoice_id"], report, html, json_path, html_path,
                      state.get("feedback_ids") or [])
    log.info("Invoice %s run %s: %s", state["file_name"], state["run_number"], recommendation)
    return {"recommendation": recommendation, "validation_status": status}


@tracing.traced("index")
def index_node(state: InvoiceState) -> dict:
    # Indexing is derived data; a failure here leaves indexed_at NULL and is
    # retried by the monitor's index sweep instead of failing the audit.
    try:
        mcp_client.call("vector-indexer", invoice_id=state["invoice_id"])
    except Exception as exc:
        log.error("Indexing invoice %s failed (will retry): %s", state["invoice_id"], exc)
    return {}


def build_graph(checkpointer):
    g = StateGraph(InvoiceState)
    g.add_node("extract_pdf", extract_pdf)
    g.add_node("extract_docx", extract_docx)
    g.add_node("extract_image", extract_image)
    g.add_node("translate", translate_node)
    g.add_node("validate_data", validate_data)
    g.add_node("validate_business", validate_business_node)
    g.add_node("report", report_node)
    g.add_node("index", index_node)

    g.add_conditional_edges(START, route_entry,
                            ["extract_pdf", "extract_docx", "extract_image", "validate_data"])
    for node in ("extract_pdf", "extract_docx", "extract_image"):
        g.add_edge(node, "translate")
    g.add_edge("translate", "validate_data")
    g.add_edge("validate_data", "validate_business")
    g.add_edge("validate_business", "report")
    g.add_edge("report", "index")
    g.add_edge("index", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------- runners

_graph = None
_saver = None
_revalidation_lock = threading.Lock()


def init() -> None:
    global _graph, _saver
    _saver = PostgresSaver(db.checkpoint_pool())
    _saver.setup()  # creates checkpoint tables in the langgraph schema
    _graph = build_graph(_saver)


@tracing.traced("invoice-workflow")
def _invoke(invoice_id: str, state: dict | None, thread_id: str, mark_failure: bool = True) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    with tracing.attributes(session_id=thread_id, invoice_id=invoice_id):
        try:
            return _graph.invoke(state, config)
        except Exception as exc:
            stage = None
            try:
                stage = ",".join(_graph.get_state(config).next) or None
            except Exception:
                pass
            log.error("Workflow for %s failed at %s: %s", invoice_id, stage, exc)
            if mark_failure:
                repo.mark_failed(invoice_id, stage, f"{type(exc).__name__}: {exc}")
            raise WorkflowError(stage, exc) from exc


def _initial_state(invoice_id: str, file_name: str, file_path: str, file_type: str,
                   email_metadata: dict | None) -> dict:
    return {"mode": "ingest", "invoice_id": invoice_id, "file_name": file_name, "file_path": file_path,
            "file_type": file_type, "email_metadata": email_metadata, "trigger": "initial",
            "triggered_by": "system", "corrections_applied": [], "feedback_ids": []}


def run_new(*, invoice_id: str, file_name: str, file_path: str, file_type: str,
            email_metadata: dict | None) -> dict:
    thread_id = f"invoice:{invoice_id}"
    repo.mark_processing(invoice_id, thread_id)
    return _invoke(invoice_id, _initial_state(invoice_id, file_name, file_path, file_type, email_metadata), thread_id)


def resume(invoice_id: str, thread_id: str) -> dict:
    """Continue an invoice claimed with repo.claim_retry from its last checkpoint.

    A restart before the first checkpoint leaves nothing to continue, so it starts over.
    """
    if _graph.get_state({"configurable": {"thread_id": thread_id}}).values:
        return _invoke(invoice_id, None, thread_id)
    row = repo.get_invoice(invoice_id)
    return _invoke(invoice_id, _initial_state(invoice_id, row["file_name"], row["file_path"], row["file_type"],
                                              row.get("email_metadata")), thread_id)


def build_effective_invoice(invoice_id: str) -> tuple[dict, dict, list[dict], list[dict]]:
    """Original extraction with the latest human correction per field layered on top."""
    row = repo.get_invoice(invoice_id)
    if row is None:
        raise LookupError(f"Invoice {invoice_id} not found")
    invoice = {c: row.get(c) for c in repo.HEADER_COLUMNS}
    items = repo.get_line_items(invoice_id)
    by_line = {i["line_number"]: i for i in items}
    applied = []
    for c in repo.pending_corrections(invoice_id):
        if c["line_number"] is None:
            invoice[c["field_name"]] = c["corrected_value"]
            label = c["field_name"]
        elif c["line_number"] in by_line:
            by_line[c["line_number"]][c["field_name"]] = c["corrected_value"]
            label = f"line {c['line_number']} {c['field_name']}"
        else:
            continue
        applied.append({"field": label, "original_value": c["original_value"],
                        "corrected_value": c["corrected_value"], "corrected_by": c["corrected_by"],
                        "corrected_at": c["corrected_at"]})
    return row, invoice, items, applied


def revalidate(invoice_id: str, requested_by: str) -> dict:
    # Read first: a correction saved after this point stays unapplied and isn't marked as used.
    feedback_ids = repo.unapplied_feedback_ids(invoice_id)
    row, invoice, items, applied = build_effective_invoice(invoice_id)
    if row["processing_status"] != "completed":
        raise ValueError("Only fully processed invoices can be re-validated "
                         f"(status is {row['processing_status']})")
    confidence = row.get("translation_confidence")
    extras = row.get("extracted_raw") or {}
    # Correcting a recovered field, even to the same value, is the person confirming it.
    recovered = {f: v for f, v in (extras.get("llm_recovered_fields") or {}).items()
                 if f not in {a["field"] for a in applied}}
    state = {"mode": "revalidate", "invoice_id": invoice_id, "file_name": row["file_name"],
             "raw_text": row.get("raw_extracted_text") or "", "extraction_method": row.get("extraction_method"),
             "invoice": invoice, "line_items": items, "source_language": row.get("source_language"),
             "translated_text": row.get("translated_text") or "",
             "translation_confidence": None if confidence is None else float(confidence),
             "ai_translated": used_model((extras.get("translation") or {}).get("method")),
             "recovered_fields": recovered,
             "trigger": "revalidation", "triggered_by": requested_by, "corrections_applied": applied,
             "feedback_ids": feedback_ids}
    # Refuse rather than queue: waiting callers would each hold an API worker thread.
    if not _revalidation_lock.acquire(blocking=False):
        raise ValueError("Another re-check is running. Please try again in a moment.")
    thread_id = f"invoice:{invoice_id}:reval:{uuid.uuid4().hex[:8]}"
    try:
        out = _invoke(invoice_id, state, thread_id, mark_failure=False)
    finally:
        _revalidation_lock.release()
        # A re-check is never resumed, and its checkpoints hold the whole invoice text.
        try:
            _saver.delete_thread(thread_id)
        except Exception as exc:
            log.warning("Could not remove re-check checkpoints: %s", exc)
    return {"invoice_id": invoice_id, "run_number": out["run_number"], "recommendation": out["recommendation"],
            "validation_status": out["validation_status"], "corrections_applied": applied,
            "discrepancies": out["discrepancies"]}

"""Indexing agent: PostgreSQL text -> chunks -> Gemini embeddings -> Qdrant.

Qdrant is a derived store. Everything indexed here is read back from
PostgreSQL (the stored extracted/translated text and the latest report),
never from the invoice file, so losing the Qdrant volume means a rebuild,
not data loss: no OCR is repeated and no invoice is re-read.
"""
import logging
import threading
import uuid

from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue, PointStruct

from .. import llm
from .. import repository as repo
from ..report import report_as_text
from ..settings import agent_config
from . import store

log = logging.getLogger(__name__)


rebuilding = threading.Lock()  # held during a rebuild, so the monitor's sweep doesn't index the same invoices


def _splitter() -> RecursiveCharacterTextSplitter:
    cfg = agent_config()["rag"]
    return RecursiveCharacterTextSplitter(chunk_size=cfg["chunk_size_chars"],
                                          chunk_overlap=cfg["chunk_overlap_chars"],
                                          separators=["\n\n", "\n", ". ", " ", ""])


def index_invoice(invoice_id: str) -> int:
    invoice = repo.get_invoice(invoice_id)
    if invoice is None or invoice["processing_status"] != "completed":
        return 0
    repo.clear_indexed(invoice_id)  # any failure below leaves this for the monitor's sweep to retry
    run = repo.latest_run(invoice_id)

    label = (f"[Invoice {invoice.get('invoice_number') or '(no number)'} | {invoice.get('vendor_name') or 'unknown vendor'}"
             f" | PO {invoice.get('po_number') or 'none'} | file {invoice['file_name']}]")
    documents: list[tuple[str, str]] = []
    invoice_text = invoice.get("translated_text") or invoice.get("raw_extracted_text") or ""
    for chunk in _splitter().split_text(invoice_text):
        documents.append(("invoice", f"{label}\n{chunk}"))
    if run and run.get("report_json"):
        for chunk in _splitter().split_text(report_as_text(run["report_json"])):
            documents.append(("report", f"{label} audit report\n{chunk}"))

    base_payload = {
        "invoice_id": invoice_id,
        "invoice_number": invoice.get("invoice_number"),
        "vendor": invoice.get("vendor_name"),
        "language": invoice.get("source_language"),
        "po_number": invoice.get("po_number"),
        "recommendation": invoice.get("recommendation"),
        "run_number": run["run_number"] if run else None,
    }
    points = []
    counters = {"invoice": 0, "report": 0}
    for source_type, text in documents:
        index = counters[source_type]
        counters[source_type] += 1
        points.append(PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{invoice_id}:{source_type}:{index}")),
            vector=llm.embed(text),
            payload={**base_payload, "source_type": source_type, "chunk_index": index, "chunk_text": text}))

    store.ensure_collection()
    client, name = store.client(), store.collection_name()
    # Replace this invoice's points (report chunks change after re-validation).
    client.delete(name, points_selector=FilterSelector(filter=Filter(
        must=[FieldCondition(key="invoice_id", match=MatchValue(value=invoice_id))])), wait=True)
    if points:
        client.upsert(name, points=points, wait=True)
    repo.mark_indexed(invoice_id)
    log.info("Indexed invoice %s: %s invoice chunks, %s report chunks", invoice_id,
             counters["invoice"], counters["report"])
    return len(points)


def rebuild_all() -> dict:
    """Recreate the collection from PostgreSQL alone."""
    with rebuilding:
        store.ensure_collection(recreate=True)
        repo.clear_all_indexed()  # the points are gone, so whatever this rebuild misses must be retried
        indexed, failed = 0, []
        for invoice_id in repo.indexable_invoice_ids():
            try:
                indexed += 1 if index_invoice(invoice_id) else 0
            except Exception as exc:  # keep going; report what failed
                log.error("Re-indexing %s failed: %s", invoice_id, exc)
                failed.append(invoice_id)
        return {"invoices_indexed": indexed, "failed": failed, "points": store.point_count()}


def ensure_index_on_startup() -> None:
    """If the Qdrant volume was lost but PostgreSQL still has the audit trail, rebuild."""
    try:
        if store.point_count() == 0 and repo.indexable_invoice_ids():
            log.warning("Qdrant collection is empty but PostgreSQL has processed invoices; rebuilding index")
            log.info("Rebuild result: %s", rebuild_all())
    except Exception as exc:
        log.error("Startup index check failed: %s", exc)

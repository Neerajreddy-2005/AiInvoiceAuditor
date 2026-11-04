"""All reads and writes of the audit schema.

The agent is the only service that writes application data. It never
DELETEs from the audit trail (its database role cannot) and never
UPDATEs extracted values after a human correction: corrections are rows
in audit.human_feedback.
"""
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from psycopg.types.json import Jsonb

from . import db

HEADER_COLUMNS = ("invoice_number", "invoice_date", "vendor_name", "po_number", "currency",
                  "subtotal_amount", "tax_rate_pct", "tax_amount", "total_amount")
LINE_COLUMNS = ("item_code", "description", "quantity", "unit_price", "line_total")


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _row_to_state(row: dict | None) -> dict | None:
    """DB row -> JSON-safe dict (Decimals and dates become strings)."""
    if row is None:
        return None
    return {k: _jsonable(v) for k, v in row.items()}


def _json(value) -> Jsonb:
    return Jsonb(json.loads(json.dumps(value, default=_jsonable)))


# ---------------------------------------------------------------- monitor

def get_by_checksum(checksum: str) -> dict | None:
    with db.connection() as conn:
        return conn.execute(
            "SELECT invoice_id::text, file_name, processing_status, attempts, thread_id, last_attempt_at "
            "FROM audit.invoice_audit WHERE file_checksum = %s", (checksum,)).fetchone()


def claim_file(*, file_name: str, file_path: str, checksum: str, file_type: str | None,
               size: int, email_metadata: dict | None) -> str | None:
    """Atomically registers a detected file. Returns None if another worker already has it."""
    status = "detected" if file_type else "unsupported"
    with db.connection() as conn:
        row = conn.execute(
            "INSERT INTO audit.invoice_audit (file_name, file_path, file_checksum, file_type, file_size_bytes, "
            "email_metadata, processing_status) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (file_checksum) DO NOTHING RETURNING invoice_id::text",
            (file_name, file_path, checksum, file_type, size,
             _json(email_metadata) if email_metadata is not None else None, status)).fetchone()
    return row["invoice_id"] if row else None


def pending_uploads(limit: int) -> list[dict]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT invoice_id::text, file_name, file_path, file_type, email_metadata FROM audit.invoice_audit "
            "WHERE processing_status = 'detected' AND file_path LIKE %s ORDER BY detected_at LIMIT %s",
            ("storage://%", limit)).fetchall()


def mark_unsupported(invoice_id: str, reason: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE audit.invoice_audit SET processing_status = 'unsupported', failed_stage = 'monitor', "
                     "error_message = %s WHERE invoice_id = %s", (reason, invoice_id))


def mark_processing(invoice_id: str, thread_id: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE audit.invoice_audit SET processing_status = 'processing', thread_id = %s, "
                     "attempts = attempts + 1, last_attempt_at = now(), failed_stage = NULL, error_message = NULL "
                     "WHERE invoice_id = %s", (thread_id, invoice_id))


def claim_retry(invoice_id: str) -> bool:
    """Atomically move a failed invoice to processing, so it can never be resumed twice at once."""
    with db.connection() as conn:
        row = conn.execute("UPDATE audit.invoice_audit SET processing_status = 'processing', attempts = attempts + 1, "
                           "last_attempt_at = now(), failed_stage = NULL, error_message = NULL "
                           "WHERE invoice_id = %s AND processing_status = 'failed' AND thread_id IS NOT NULL "
                           "RETURNING invoice_id", (invoice_id,)).fetchone()
    return row is not None


def recover_interrupted() -> list[str]:
    """Mark invoices left unfinished by a restart as failed, so the normal retry path picks them up.

    Safe at startup because this deployment runs a single agent, so nothing is really still processing.
    """
    with db.connection() as conn:
        rows = conn.execute("UPDATE audit.invoice_audit SET processing_status = 'failed', "
                            "error_message = 'Interrupted by an agent restart', "
                            "thread_id = COALESCE(thread_id, 'invoice:' || invoice_id::text), "
                            "last_attempt_at = COALESCE(last_attempt_at, now()) "
                            "WHERE processing_status IN ('detected', 'processing') RETURNING file_name").fetchall()
    return [r["file_name"] for r in rows]


def mark_failed(invoice_id: str, stage: str | None, error: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE audit.invoice_audit SET processing_status = 'failed', failed_stage = %s, "
                     "error_message = %s WHERE invoice_id = %s", (stage, error[:4000], invoice_id))


# ---------------------------------------------------------------- pipeline writes

def save_extraction(invoice_id: str, *, method: str, raw_text: str, fields: dict, line_items: list[dict],
                    extras: dict) -> None:
    with db.connection() as conn:
        conn.execute(
            "UPDATE audit.invoice_audit SET extraction_method = %s, raw_extracted_text = %s, "
            + ", ".join(f"{c} = %s" for c in HEADER_COLUMNS)
            + ", extracted_raw = extracted_raw || %s WHERE invoice_id = %s",
            (method, raw_text, *[fields.get(c) for c in HEADER_COLUMNS], _json(extras), invoice_id))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO audit.invoice_line_items (invoice_id, line_number, item_code, description, quantity, "
                "unit_price, line_total, raw_line) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (invoice_id, line_number) DO NOTHING",
                [(invoice_id, i["line_number"], *[i.get(c) for c in LINE_COLUMNS], i.get("raw_line"))
                 for i in line_items])


def save_translation(invoice_id: str, *, source_language: str | None, translated_text: str,
                     confidence, details: dict) -> None:
    with db.connection() as conn:
        conn.execute(
            "UPDATE audit.invoice_audit SET source_language = %s, translated_text = %s, "
            "translation_confidence = %s, extracted_raw = extracted_raw || %s WHERE invoice_id = %s",
            (source_language, translated_text, confidence, _json({"translation": details}), invoice_id))


def fill_recovered_fields(invoice_id: str, recovered: dict, rejected: dict) -> None:
    """LLM-recovered values only fill NULL columns; nothing extracted is overwritten."""
    with db.connection() as conn:
        for field, value in recovered.items():
            if field in HEADER_COLUMNS:
                conn.execute(f"UPDATE audit.invoice_audit SET {field} = COALESCE({field}, %s) "
                             "WHERE invoice_id = %s", (value, invoice_id))
        conn.execute("UPDATE audit.invoice_audit SET extracted_raw = extracted_raw || %s WHERE invoice_id = %s",
                     (_json({"llm_recovered_fields": recovered, "llm_rejected_fields": rejected}), invoice_id))


def set_vendor_id(invoice_id: str, vendor_id: str | None) -> None:
    if vendor_id:
        with db.connection() as conn:
            conn.execute("UPDATE audit.invoice_audit SET vendor_id = %s WHERE invoice_id = %s",
                         (vendor_id, invoice_id))


_DISCREPANCY_COLUMNS = ("check_type", "field_name", "line_number", "invoice_value", "erp_value", "expected_value",
                        "deviation_pct", "tolerance_pct", "severity", "action", "message")


def _insert_discrepancies(conn, run_id: int, invoice_id: str, discrepancies: list[dict]) -> None:
    if not discrepancies:
        return
    cols = _DISCREPANCY_COLUMNS
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO audit.discrepancies (invoice_id, run_id, {', '.join(cols)}) "
            f"VALUES (%s, %s, {', '.join(['%s'] * len(cols))})",
            [(invoice_id, run_id, *[d.get(c) for c in cols]) for d in discrepancies])


def start_run(invoice_id: str, trigger: str, triggered_by: str | None, corrections: list[dict],
              discrepancies: list[dict]) -> tuple[int, int]:
    """Creates the validation run and its data-validation discrepancies in one transaction."""
    with db.connection() as conn:
        row = conn.execute(
            "INSERT INTO audit.validation_runs (invoice_id, run_number, trigger, triggered_by, corrections_applied) "
            "SELECT %s, COALESCE(MAX(run_number), 0) + 1, %s, %s, %s FROM audit.validation_runs WHERE invoice_id = %s "
            "RETURNING run_id, run_number",
            (invoice_id, trigger, triggered_by, _json(corrections), invoice_id)).fetchone()
        _insert_discrepancies(conn, row["run_id"], invoice_id, discrepancies)
    return row["run_id"], row["run_number"]


def insert_discrepancies(run_id: int, invoice_id: str, discrepancies: list[dict]) -> None:
    with db.connection() as conn:
        _insert_discrepancies(conn, run_id, invoice_id, discrepancies)


def complete_run(run_id: int, invoice_id: str, report: dict, html: str, json_path: str | None,
                 html_path: str | None, feedback_ids: list[int]) -> None:
    with db.connection() as conn:
        conn.execute(
            "UPDATE audit.validation_runs SET validation_status = %s, recommendation = %s, "
            "recommendation_reasons = %s, missing_fields = %s, summary = %s, report_json = %s, report_html = %s, "
            "report_json_path = %s, report_html_path = %s, completed_at = now() WHERE run_id = %s",
            (report["validation_status"], report["recommendation"], _json(report["recommendation_reasons"]),
             _json(report["missing_fields"]), report.get("summary"), _json(report), html,
             json_path, html_path, run_id))
        conn.execute(
            "UPDATE audit.invoice_audit SET validation_status = %s, recommendation = %s, "
            "processing_status = 'completed', processed_at = now(), failed_stage = NULL, error_message = NULL "
            "WHERE invoice_id = %s",
            (report["validation_status"], report["recommendation"], invoice_id))
        if feedback_ids:
            conn.execute("UPDATE audit.human_feedback SET revalidated = TRUE, revalidated_run_id = %s "
                         "WHERE id = ANY(%s)", (run_id, feedback_ids))


def clear_indexed(invoice_id: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE audit.invoice_audit SET indexed_at = NULL WHERE invoice_id = %s", (invoice_id,))


def clear_all_indexed() -> None:
    with db.connection() as conn:
        conn.execute("UPDATE audit.invoice_audit SET indexed_at = NULL")


def last_indexed_at() -> str | None:
    with db.connection() as conn:
        return conn.execute("SELECT max(indexed_at)::text AS at FROM audit.invoice_audit").fetchone()["at"]


def mark_indexed(invoice_id: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE audit.invoice_audit SET indexed_at = now() WHERE invoice_id = %s", (invoice_id,))


# ---------------------------------------------------------------- reads

def get_invoice(invoice_id: str) -> dict | None:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM audit.invoice_audit WHERE invoice_id = %s", (invoice_id,)).fetchone()
    return _row_to_state(row)


def get_line_items(invoice_id: str) -> list[dict]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT line_number, item_code, description, quantity, unit_price, line_total, raw_line "
            "FROM audit.invoice_line_items WHERE invoice_id = %s ORDER BY line_number", (invoice_id,)).fetchall()
    return [_row_to_state(r) for r in rows]


def latest_run(invoice_id: str) -> dict | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT run_id, run_number, report_json, recommendation FROM audit.validation_runs "
            "WHERE invoice_id = %s AND completed_at IS NOT NULL ORDER BY run_number DESC LIMIT 1",
            (invoice_id,)).fetchone()
    return row


def list_invoices(limit: int = 500) -> list[dict]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT i.invoice_id, i.file_name, i.invoice_number, i.vendor_name, i.invoice_date, i.currency, "
            "i.total_amount, i.recommendation, i.validation_status, i.processing_status, i.attempts, i.failed_stage, "
            "i.error_message, i.detected_at, i.processed_at, r.top_reason, "
            # A re-check can change the outcome, so only a decision made after the latest run counts.
            "EXISTS (SELECT 1 FROM audit.human_feedback f WHERE f.invoice_id = i.invoice_id "
            "AND f.feedback_type = 'decision' AND f.corrected_at >= r.completed_at) AS decided, "
            "(SELECT f.corrected_value FROM audit.human_feedback f WHERE f.invoice_id = i.invoice_id "
            "AND f.feedback_type = 'decision' AND f.corrected_at >= r.completed_at "
            "ORDER BY f.corrected_at DESC, f.id DESC LIMIT 1) AS decision "
            "FROM audit.invoice_audit i LEFT JOIN LATERAL ("
            "  SELECT recommendation_reasons->>0 AS top_reason, completed_at FROM audit.validation_runs "
            "  WHERE invoice_id = i.invoice_id AND completed_at IS NOT NULL ORDER BY run_number DESC LIMIT 1"
            ") r ON true ORDER BY i.detected_at DESC LIMIT %s", (limit,)).fetchall()


def invoice_detail(invoice_id: str) -> dict | None:
    """Everything the review screen shows, except report bodies (fetched only when opened)."""
    with db.connection() as conn:
        invoice = conn.execute(
            "SELECT invoice_id, file_name, file_type, processing_status, attempts, failed_stage, error_message, "
            "extraction_method, raw_extracted_text, translated_text, source_language, translation_confidence, "
            "invoice_number, vendor_name, po_number, invoice_date, currency, subtotal_amount, tax_rate_pct, "
            "tax_amount, total_amount, validation_status, recommendation, detected_at, processed_at "
            "FROM audit.invoice_audit WHERE invoice_id = %s", (invoice_id,)).fetchone()
        if invoice is None:
            return None
        lines = conn.execute(
            "SELECT line_number, item_code, description, quantity, unit_price, line_total "
            "FROM audit.invoice_line_items WHERE invoice_id = %s ORDER BY line_number", (invoice_id,)).fetchall()
        runs = conn.execute(
            "SELECT run_id, run_number, trigger, triggered_by, recommendation, validation_status, summary, "
            "recommendation_reasons, started_at, completed_at "
            "FROM audit.validation_runs WHERE invoice_id = %s ORDER BY run_number DESC", (invoice_id,)).fetchall()
        latest = next((r for r in runs if r["completed_at"]), None)  # ignore unfinished or failed runs
        discrepancies = conn.execute(
            "SELECT id, severity, field_name, line_number, invoice_value, erp_value, expected_value, "
            "deviation_pct, message, action FROM audit.discrepancies WHERE run_id = %s "
            "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, line_number "
            "NULLS FIRST, id", (latest["run_id"],)).fetchall() if latest else []
        feedback = conn.execute(
            "SELECT id, feedback_type, field_name, line_number, original_value, corrected_value, comment, "
            "corrected_by, corrected_at, revalidated FROM audit.human_feedback WHERE invoice_id = %s "
            "ORDER BY corrected_at DESC, id DESC", (invoice_id,)).fetchall()
    return {"invoice": invoice, "line_items": lines, "runs": runs, "latest_run_id": latest["run_id"] if latest else None,
            "discrepancies": discrepancies, "feedback": feedback}


def run_report(invoice_id: str, run_id: int) -> dict | None:
    with db.connection() as conn:
        return conn.execute(
            "SELECT run_number, report_html, report_json FROM audit.validation_runs "
            "WHERE invoice_id = %s AND run_id = %s", (invoice_id, run_id)).fetchone()


def find_same_number(invoice_id: str, normalised_number: str) -> list[dict]:
    """Earlier invoices with the same number, ignoring case and punctuation.

    Only earlier ones, so re-validating an original never flags it for a later copy.
    """
    if not normalised_number:
        return []
    with db.connection() as conn:
        return conn.execute(
            "SELECT invoice_id::text, file_name, vendor_name, recommendation FROM audit.invoice_audit "
            "WHERE detected_at < (SELECT detected_at FROM audit.invoice_audit WHERE invoice_id = %s) "
            "AND regexp_replace(lower(invoice_number), '[^a-z0-9]', '', 'g') = %s "
            "AND processing_status <> 'unsupported' ORDER BY detected_at",
            (invoice_id, normalised_number)).fetchall()


def find_earlier_on_po(invoice_id: str, po_number: str | None, first_read: bool = False) -> list[dict]:
    """Earlier invoices billed against the same PO, whatever their invoice number.

    On a first read, a later invoice that already got its PO counts too: this one failed before
    its PO was read, so that invoice couldn't have seen it. A re-check doesn't count them.
    """
    if not po_number:
        return []
    with db.connection() as conn:
        return conn.execute(
            "SELECT invoice_id::text, file_name FROM audit.invoice_audit "
            "WHERE invoice_id <> %s AND (%s OR detected_at < (SELECT detected_at FROM audit.invoice_audit "
            "WHERE invoice_id = %s)) "
            "AND upper(po_number) = upper(%s) AND processing_status <> 'unsupported' ORDER BY detected_at",
            (invoice_id, first_read, invoice_id, po_number)).fetchall()


def pending_corrections(invoice_id: str) -> list[dict]:
    """Latest correction per (field, line), newest wins; includes already-applied ones."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (field_name, COALESCE(line_number, 0)) id, field_name, line_number, "
            "original_value, corrected_value, corrected_by, corrected_at, revalidated "
            "FROM audit.human_feedback WHERE invoice_id = %s AND feedback_type = 'correction' "
            "ORDER BY field_name, COALESCE(line_number, 0), corrected_at DESC, id DESC", (invoice_id,)).fetchall()
    return [_row_to_state(r) for r in rows]


def unapplied_feedback_ids(invoice_id: str) -> list[int]:
    with db.connection() as conn:
        rows = conn.execute("SELECT id FROM audit.human_feedback WHERE invoice_id = %s "
                            "AND feedback_type = 'correction' AND NOT revalidated", (invoice_id,)).fetchall()
    return [r["id"] for r in rows]


def insert_feedback(*, invoice_id: str, feedback_type: str, field_name: str | None, line_number: int | None,
                    original_value: str | None, corrected_value: str | None, comment: str | None,
                    corrected_by: str) -> int:
    with db.connection() as conn:
        row = conn.execute(
            "INSERT INTO audit.human_feedback (invoice_id, feedback_type, field_name, line_number, original_value, "
            "corrected_value, comment, corrected_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (invoice_id, feedback_type, field_name, line_number, original_value, corrected_value, comment,
             corrected_by)).fetchone()
    return row["id"]


def failed_for_retry(max_attempts: int, backoff_seconds: int) -> list[dict]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT invoice_id::text, file_name, file_path, thread_id, attempts FROM audit.invoice_audit "
            "WHERE processing_status = 'failed' AND thread_id IS NOT NULL AND attempts < %s "
            "AND last_attempt_at < now() - make_interval(secs => %s * attempts)",
            (max_attempts, backoff_seconds)).fetchall()


def unindexed_invoice_ids(limit: int) -> list[str]:
    with db.connection() as conn:
        rows = conn.execute("SELECT invoice_id::text FROM audit.invoice_audit WHERE processing_status = 'completed' "
                            "AND indexed_at IS NULL ORDER BY processed_at LIMIT %s", (limit,)).fetchall()
    return [r["invoice_id"] for r in rows]


def indexable_invoice_ids() -> list[str]:
    with db.connection() as conn:
        rows = conn.execute("SELECT invoice_id::text FROM audit.invoice_audit WHERE processing_status = 'completed' "
                            "ORDER BY processed_at").fetchall()
    return [r["invoice_id"] for r in rows]


# ---------------------------------------------------------------- structured (SQL) questions

_GROUP_COLUMNS = {"recommendation": "recommendation", "vendor": "vendor_name", "currency": "currency"}


def _fold_accents(sql_value: str) -> str:
    """SQL expression for case- and accent-insensitive matching ("Iberico" finds "Ibérico")."""
    return f"translate(lower({sql_value}), 'áàâäãåéèêëíìîïóòôöõúùûüñç', 'aaaaaaeeeeiiiiooooouuuunc')"


def _filters(f: dict) -> tuple[str, list]:
    clauses, params = ["processing_status <> 'unsupported'"], []
    if f.get("vendor"):
        clauses.append(f"{_fold_accents('vendor_name')} LIKE {_fold_accents('%s')}")
        params.append(f"%{f['vendor']}%")
    if f.get("recommendation") in ("approve", "review", "reject"):
        clauses.append("recommendation = %s")
        params.append(f["recommendation"])
    if f.get("processing_status") in ("completed", "failed", "processing", "detected", "unsupported"):
        clauses[0] = "processing_status = %s"
        params.insert(0, f["processing_status"])
    if f.get("currency"):
        clauses.append("currency = %s")
        params.append(str(f["currency"]).upper()[:3])
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        if f.get(key):
            try:
                params.append(date.fromisoformat(str(f[key])))
                clauses.append(f"invoice_date {op} %s")
            except ValueError:
                pass
    if f.get("invoice_number"):
        clauses.append("invoice_number ILIKE %s")
        params.append(f"%{f['invoice_number']}%")
    return " AND ".join(clauses), params


def structured_query(intent: str, filters: dict) -> tuple[list[str], list[dict]]:
    """Fixed, parameterised queries only. The model picks an intent; it never writes SQL."""
    where, params = _filters(filters)
    group = _GROUP_COLUMNS.get(filters.get("group_by") or "")
    if intent == "list_invoices":
        sql = ("SELECT invoice_number, vendor_name, invoice_date, currency, total_amount, recommendation, "
               f"processing_status, file_name FROM audit.invoice_audit WHERE {where} "
               "ORDER BY invoice_date DESC NULLS LAST LIMIT 50")
    elif intent == "count_invoices":
        sql = (f"SELECT {group} AS {filters['group_by']}, count(*) AS invoices FROM audit.invoice_audit "
               f"WHERE {where} GROUP BY 1 ORDER BY 2 DESC" if group else
               f"SELECT count(*) AS invoices FROM audit.invoice_audit WHERE {where}")
    elif intent == "sum_totals":
        extra = f", {group} AS {filters['group_by']}" if group and group != "currency" else ""
        # Invoices with no currency may be in different ones, so they are counted but never added up.
        sql = (f"SELECT currency{extra}, count(*) AS invoices, "
               "sum(total_amount) FILTER (WHERE currency IS NOT NULL) AS total "
               f"FROM audit.invoice_audit WHERE {where} GROUP BY currency{', 2' if extra else ''} ORDER BY 1")
    elif intent == "invoice_discrepancies":
        sql = ("SELECT a.invoice_number, d.field_name, d.line_number, d.invoice_value, d.erp_value, "
               "d.expected_value, d.deviation_pct, d.severity, d.message FROM audit.discrepancies d "
               "JOIN audit.invoice_audit a ON a.invoice_id = d.invoice_id "
               f"WHERE d.invoice_id IN (SELECT invoice_id FROM audit.invoice_audit WHERE {where}) "
               "AND d.run_id = (SELECT r.run_id FROM audit.validation_runs r WHERE r.invoice_id = d.invoice_id "
               "AND r.completed_at IS NOT NULL ORDER BY r.run_number DESC LIMIT 1) "
               "ORDER BY a.invoice_number, d.severity, d.line_number NULLS FIRST LIMIT 200")
    else:
        raise ValueError(f"unknown intent {intent}")
    with db.connection() as conn:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        columns = [c.name for c in cur.description]
    return columns, [_row_to_state(r) for r in rows]

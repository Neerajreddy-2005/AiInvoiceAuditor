"""Reporting agent: recommendation, JSON + HTML report, LLM summary.

The recommendation is computed by rules, not by the model, so every
decision can be traced to a discrepancy and a policy in rules.yaml.
The model (Gemini) only writes the human-readable summary.
"""
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import llm

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html", "j2"]))

RECOMMENDATION_LABELS = {"approve": "Approve", "review": "Manual review", "reject": "Reject"}


def recommend(discrepancies: list[dict], translation_confidence, rules: dict) -> tuple[str, str, list[str]]:
    """Returns (recommendation, validation_status, reasons)."""
    threshold = float(rules["validation_policies"]["auto_approve_confidence_threshold"])
    rejecting = [d for d in discrepancies if d["action"] == "reject"]
    reviewing = [d for d in discrepancies if d["action"] == "manual_review"]
    low_confidence = translation_confidence is not None and float(translation_confidence) < threshold

    if rejecting:
        reasons = [d["message"] for d in rejecting]
        return "reject", "failed", reasons
    reasons = [d["message"] for d in reviewing]
    if low_confidence:
        reasons.append(f"Translation confidence {float(translation_confidence):.2f} is below "
                       f"the auto-approve threshold {threshold:.2f}")
    if reasons:
        return "review", "partial", reasons
    return "approve", "passed", ["No blocking discrepancies; all checks passed"]


def build_report(*, invoice_id: str, run_number: int, trigger: str, triggered_by: str | None,
                 file_name: str, extraction_method: str | None, invoice: dict, line_items: list[dict],
                 source_language: str | None, translation_confidence, discrepancies: list[dict],
                 missing_fields: list[str], recommendation: str, validation_status: str,
                 reasons: list[str], line_matches: list[dict], corrections_applied: list[dict]) -> dict:
    severity_counts = Counter(d["severity"] for d in discrepancies)
    return {
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "invoice_id": invoice_id,
        "run_number": run_number,
        "trigger": trigger,
        "triggered_by": triggered_by,
        "file_name": file_name,
        "extraction_method": extraction_method,
        "invoice": invoice,
        "line_items": [{k: v for k, v in i.items() if k != "raw_line"} for i in line_items],
        "translation": {"source_language": source_language,
                        "confidence": None if translation_confidence is None else float(translation_confidence)},
        "missing_fields": missing_fields,
        "discrepancy_summary": {"total": len(discrepancies), "high": severity_counts.get("high", 0),
                                "medium": severity_counts.get("medium", 0), "low": severity_counts.get("low", 0)},
        "discrepancies": discrepancies,
        "line_matches": line_matches,
        "corrections_applied": corrections_applied,
        "validation_status": validation_status,
        "recommendation": recommendation,
        "recommendation_reasons": reasons,
        "summary": None,
    }


def summarize(report: dict) -> str | None:
    compact = {k: report[k] for k in ("invoice", "translation", "missing_fields", "discrepancy_summary",
                                      "recommendation", "recommendation_reasons", "corrections_applied")}
    compact["discrepancies"] = [{k: d[k] for k in ("field_name", "line_number", "invoice_value", "erp_value",
                                                   "expected_value", "deviation_pct", "severity", "message")}
                                for d in report["discrepancies"]]
    try:
        return llm.complete(llm.prompt_messages("report_summary", report=json.dumps(compact, ensure_ascii=False)),
                            max_tokens=400).strip()
    except llm.LLMError as exc:
        # The summary is explanatory only; the decision above does not depend on it.
        log.warning("Report summary unavailable: %s", exc)
        return None


def render_html(report: dict) -> str:
    return _env.get_template("report.html.j2").render(r=report, labels=RECOMMENDATION_LABELS)


def write_files(report: dict, html: str, reports_dir: Path) -> tuple[str, str]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = report["invoice"].get("invoice_number") or report["invoice_id"][:8]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)
    base = f"{stem}_{report['invoice_id'][:8]}_run{report['run_number']}"
    json_path, html_path = reports_dir / f"{base}.json", reports_dir / f"{base}.html"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return str(json_path), str(html_path)


def report_as_text(report: dict) -> str:
    """Plain-text rendering used for RAG indexing (source_type=report)."""
    inv = report["invoice"]
    lines = [
        f"Audit report for invoice {inv.get('invoice_number') or '(no number)'} from "
        f"{inv.get('vendor_name') or 'unknown vendor'}, PO {inv.get('po_number') or 'none'}, "
        f"dated {inv.get('invoice_date') or 'unknown'}, total {inv.get('total_amount') or '?'} "
        f"{inv.get('currency') or '(currency not stated)'}.",
        f"Recommendation: {RECOMMENDATION_LABELS.get(report['recommendation'], report['recommendation'])} "
        f"(validation status {report['validation_status']}, run {report['run_number']}, {report['trigger']}).",
        "Reasons: " + "; ".join(report["recommendation_reasons"]),
    ]
    tr = report["translation"]
    if tr["source_language"] and tr["source_language"] != "en":
        lines.append(f"Translated from {tr['source_language']} with confidence {tr['confidence']}.")
    if report["missing_fields"]:
        lines.append("Missing fields: " + ", ".join(report["missing_fields"]) + ".")
    for d in report["discrepancies"]:
        lines.append(f"Discrepancy ({d['severity']}, {d['action']}): {d['message']}.")
    for c in report.get("corrections_applied") or []:
        lines.append(f"Auditor correction: {c['field']} changed from {c['original_value']} to "
                     f"{c['corrected_value']} by {c['corrected_by']}.")
    if report.get("summary"):
        lines.append("Summary: " + report["summary"])
    return "\n".join(lines)

"""Fallback router used when the router model is unavailable (Gemini down, over its daily limit,
throttled or not permitted): obvious count / list / total questions still go
to the fixed SQL templates instead of failing. Open-ended questions return
{} and go to RAG as before.
"""
import re

_INVOICE_NO = re.compile(r"\b[A-Z]{2,4}-\d{3,}[\w-]*\b")


def keyword_route(question: str) -> dict:
    q = question.lower()
    if re.search(r"\bwhy\b|\bexplain\b|\bdescribe\b|\bwhat does\b", q):
        return {}
    filters = {}
    for word, rec in (("reject", "reject"), ("review", "review"), ("approv", "approve")):
        if word in q:
            filters["recommendation"] = rec
            break
    if "fail" in q:
        filters["processing_status"] = "failed"
    number = _INVOICE_NO.search(question)
    if number and re.search(r"discrepanc|mismatch|issue|problem", q):
        return {"route": "sql", "intent": "invoice_discrepancies", "filters": {"invoice_number": number.group(0)}}
    if re.search(r"\bhow many\b|\bcount\b|\bnumber of\b", q):
        intent = "count_invoices"
    elif re.search(r"\btotal\b|\bsum\b", q):
        intent = "sum_totals"
    elif re.search(r"\blist\b|\bshow\b|\bwhich\b", q):
        intent = "list_invoices"
    else:
        return {}
    return {"route": "sql", "intent": intent, "filters": filters}

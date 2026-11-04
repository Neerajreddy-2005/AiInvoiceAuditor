"""Augmentation agent: rerank retrieved chunks and assemble the context.

Rerank score = 0.7 * vector similarity + 0.3 * query-term coverage, with
a small boost for report chunks when the question asks "why"/"flagged"
(those are answered by the audit report, not the invoice text).
"""
import re

REPORT_WORDS = {"why", "flag", "flagged", "reject", "rejected", "review", "approve", "approved", "discrepancy",
                "discrepancies", "mismatch", "issue", "issues", "problem", "recommendation", "missing", "wrong"}
STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "was", "were", "what", "which",
        "how", "does", "did", "do", "this", "that", "with", "by", "from", "are", "it", "its", "invoice"}
FAKE_LABEL = re.compile(r"^\s*\[\d+\]", re.MULTILINE)


def _terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower()) if t not in STOP and len(t) > 1}


def rerank(question: str, hits: list[dict], keep: int) -> list[dict]:
    q_terms = _terms(question)
    wants_report = bool(q_terms & REPORT_WORDS)
    scored = []
    seen_text = set()
    for hit in hits:
        text = hit["payload"].get("chunk_text", "")
        if text in seen_text:
            continue
        seen_text.add(text)
        coverage = len(q_terms & _terms(text)) / len(q_terms) if q_terms else 0.0
        score = 0.7 * hit["score"] + 0.3 * coverage
        if wants_report and hit["payload"].get("source_type") == "report":
            score += 0.05
        scored.append({**hit, "rerank_score": round(score, 4), "term_coverage": round(coverage, 3)})
    scored.sort(key=lambda h: h["rerank_score"], reverse=True)
    return scored[:keep]


def build_context(hits: list[dict]) -> tuple[str, list[dict]]:
    blocks, sources = [], []
    for n, hit in enumerate(hits, start=1):
        p = hit["payload"]
        # Labels are set here. Text in a file can't imitate one, and the id tells apart invoices
        # that reuse a number (the number itself is printed by whoever wrote the file).
        text = FAKE_LABEL.sub("(quoted text)", p.get("chunk_text", ""))
        blocks.append(f"[{n}] ({p.get('source_type')}, invoice {p.get('invoice_number') or 'with no number'}, "
                      f"id {str(p.get('invoice_id'))[:8]})\n{text}")
        sources.append({"ref": n, "invoice_id": p.get("invoice_id"), "invoice_number": p.get("invoice_number"),
                        "vendor": p.get("vendor"), "source_type": p.get("source_type"),
                        "score": hit.get("rerank_score"), "excerpt": p.get("chunk_text", "")[:300]})
    return "\n\n".join(blocks), sources

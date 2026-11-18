"""Tests for the keyword fallback router. No network, database or Gemini.

Run inside the agent image, from the repository root:
  docker run --rm --network none -v "$PWD/backend/Agent/tests:/app/tests:ro" \
    -v "$PWD/backend/configs:/app/configs:ro" -v "$PWD/data/incoming:/data/incoming:ro" \
    aiinvoiceauditor/agent:1.0 python -m unittest tests.test_keyword_router -v
"""
import unittest

from app.rag.keyword_router import keyword_route


def sql(intent, **filters):
    return {"route": "sql", "intent": intent, "filters": filters}


class KeywordRouter(unittest.TestCase):
    def test_obvious_questions_go_to_the_sql_templates(self):
        cases = [("How many invoices were rejected?", sql("count_invoices", recommendation="reject")),
                 ("What is the total of approved invoices?", sql("sum_totals", recommendation="approve")),
                 ("List failed invoices", sql("list_invoices", processing_status="failed")),
                 ("Show discrepancies for INV-1001", sql("invoice_discrepancies", invoice_number="INV-1001"))]
        for question, expected in cases:
            with self.subTest(question):
                self.assertEqual(keyword_route(question), expected)

    def test_open_ended_questions_go_to_rag(self):
        self.assertEqual(keyword_route("Why was this invoice flagged?"), {})


if __name__ == "__main__":
    unittest.main()

"""The whole invoice pipeline, start to finish, on the real sample files.

Real: file parsing and OCR, field extraction, language detection, validation,
recommendations, report building and the LangGraph workflow with its checkpoints.
Replaced: the database (an in-memory stand-in, so SQL isn't covered), the ERP
(the purchase orders from setup.sql), Gemini (fixed replies) and the search index.

Run inside the agent image, from the repository root:
  docker run --rm --network none -v "$PWD/backend/Agent/tests:/app/tests:ro" \
    -v "$PWD/backend/configs:/app/configs:ro" -v "$PWD/data/incoming:/data/incoming:ro" \
    invoice-auditor/agent:1.0 python -m unittest tests.test_pipeline -v
"""
import os

os.environ.update({
    "DB_HOST": "db.invalid", "POSTGRES_DB": "x", "AGENT_DB_USER": "agent", "AGENT_DB_PASSWORD": "test-db-password",
    "SUPABASE_URL": "https://project.invalid", "REVIEWER_EMAILS": "Reviewer@example.com",
    "ERP_SERVICE_TOKEN": "test-erp-token", "QDRANT_API_KEY": "test-qdrant-key", "GEMINI_API_KEY": "test-gemini-key",
})

import copy
import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from langgraph.checkpoint.memory import MemorySaver

from app import llm, repository, storage, workflow
from app.extraction import file_sha256
from app.validation import normalise_invoice_number

SAMPLES = Path("/data/incoming")


def line(number, code, description, quantity, price):
    return {"line_number": number, "item_code": code, "description": description, "quantity": quantity,
            "unit_price": price}


# The same data as backend/Database/supabase/setup.sql.
ORDERS = {
    "PO-1001": ("VEND-001", "USD", [line(1, "SKU-001", "Pallet Wrapping Film", "50", "12.00"),
                                    line(2, "SKU-002", "Industrial Gloves", "120", "3.00"),
                                    line(3, "SKU-003", "Safety Helmets", "30", "15.00")]),
    "PO-1002": ("VEND-002", "USD", [line(1, "SKU-101", "Container Seals", "200", "1.25")]),
    "PO-1003": ("VEND-003", "EUR", [line(1, "SKU-201", "Cajas de carton", "100", "2.50"),
                                    line(2, "SKU-202", "Etiquetas adhesivas", "300", "0.30")]),
    "PO-1004": ("VEND-004", "EUR", [line(1, "SKU-301", "Transportkisten", "40", "25.00"),
                                    line(2, "SKU-302", "Sicherheitswesten", "60", "10.00")]),
    "PO-1005": ("VEND-005", "USD", [line(1, "SKU-401", "Fuel Surcharge", "1", "120.00"),
                                    line(2, "SKU-402", "Express Delivery Boxes", "25", "4.50")]),
    "PO-1006": ("VEND-006", "INR", [line(1, "SKU-501", "Container Transport", "5", "8000.00")]),
}
VENDORS = {"VEND-001": ("Global Logistics Ltd", "USD"), "VEND-002": ("BlueOcean Transport Co.", "USD"),
           "VEND-003": ("Transporte Ibérico S.A.", "EUR"), "VEND-004": ("HafenLogistik GmbH", "EUR"),
           "VEND-005": ("SwiftMove Couriers", "USD"), "VEND-006": ("OceanFreight Pvt Ltd", "INR")}


class FakeERP:
    def __init__(self, orders=None):
        self.orders = orders or ORDERS

    def get_po(self, number):
        if number not in self.orders:
            return None
        vendor, currency, lines = self.orders[number]
        return {"po_number": number, "vendor_id": vendor, "currency": currency, "line_items": copy.deepcopy(lines)}

    def get_vendor(self, vendor_id):
        name, currency = VENDORS[vendor_id]
        return {"vendor_id": vendor_id, "vendor_name": name, "currency": currency}

    def get_sku(self, code):
        return {"item_code": code, "gst_rate": "10"}


class FakeRepository:
    """Keeps what the workflow reads and writes, in the order invoices arrive."""

    def __init__(self):
        self.rows, self.runs, self.feedback = {}, {}, []

    def add(self, path, file_type):
        invoice_id = str(uuid.uuid4())
        self.rows[invoice_id] = {"invoice_id": invoice_id, "file_name": Path(path).name, "file_path": str(path),
                                 "file_type": file_type, "file_checksum": file_sha256(Path(path)),
                                 "processing_status": "detected", "line_items": []}
        return invoice_id

    def get_invoice(self, invoice_id):
        row = self.rows.get(invoice_id)
        return copy.deepcopy(row) if row else None

    def mark_processing(self, invoice_id, thread_id):
        self.rows[invoice_id].update(processing_status="processing", thread_id=thread_id)

    def mark_failed(self, invoice_id, stage, message):
        self.rows[invoice_id].update(processing_status="failed", failed_stage=stage, error_message=message)

    def save_extraction(self, invoice_id, *, method, raw_text, fields, line_items, extras):
        self.rows[invoice_id].update(fields, extraction_method=method, raw_extracted_text=raw_text,
                                     line_items=copy.deepcopy(line_items))

    def save_translation(self, invoice_id, *, source_language, translated_text, confidence, details):
        self.rows[invoice_id].update(source_language=source_language, translated_text=translated_text,
                                     translation_confidence=confidence)
        self.rows[invoice_id].setdefault("extracted_raw", {})["translation"] = details

    def fill_recovered_fields(self, invoice_id, accepted, rejected):
        self.rows[invoice_id].update(accepted)
        self.rows[invoice_id].setdefault("extracted_raw", {})["llm_recovered_fields"] = accepted

    def _earlier(self, invoice_id):
        for key, row in self.rows.items():
            if key == invoice_id:
                return
            yield row

    def _others(self, invoice_id, include_later):
        return [r for k, r in self.rows.items() if k != invoice_id] if include_later else list(self._earlier(invoice_id))

    def _effective(self, row, field):
        corrections = [f for f in self.feedback if f["invoice_id"] == row["invoice_id"]
                       and f["field_name"] == field and f["line_number"] is None]
        return corrections[-1]["corrected_value"] if corrections else row.get(field)

    def find_same_number(self, invoice_id, number, include_later=False):
        if not number:
            return []
        return [{**r, "vendor_name": self._effective(r, "vendor_name")} for r in self._others(invoice_id, include_later)
                if normalise_invoice_number(self._effective(r, "invoice_number")) == number]

    def find_earlier_on_po(self, invoice_id, po_number, include_later=False):
        return [r for r in self._others(invoice_id, include_later)
                if po_number and self._effective(r, "po_number") == po_number]

    def start_run(self, invoice_id, trigger, triggered_by, corrections, discrepancies):
        run_id = len(self.runs) + 1
        number = sum(1 for r in self.runs.values() if r["invoice_id"] == invoice_id) + 1
        self.runs[run_id] = {"invoice_id": invoice_id, "number": number, "trigger": trigger,
                             "corrections": corrections, "discrepancies": list(discrepancies)}
        return run_id, number

    def insert_discrepancies(self, run_id, invoice_id, discrepancies):
        self.runs[run_id]["discrepancies"].extend(discrepancies)

    def set_vendor_id(self, invoice_id, vendor_id):
        self.rows[invoice_id]["vendor_id"] = vendor_id

    def complete_run(self, run_id, invoice_id, report, html, json_path, html_path, feedback_ids):
        self.runs[run_id].update(report=report, html=html)
        self.rows[invoice_id].update(processing_status="completed", recommendation=report["recommendation"])
        for item in self.feedback:
            if item["id"] in feedback_ids:
                item["applied"] = True

    def get_line_items(self, invoice_id):
        return copy.deepcopy(self.rows[invoice_id]["line_items"])

    def _unapplied(self, invoice_id):
        return [f for f in self.feedback if f["invoice_id"] == invoice_id and not f.get("applied")]

    def unapplied_feedback_ids(self, invoice_id):
        return [f["id"] for f in self._unapplied(invoice_id)]

    def pending_corrections(self, invoice_id):
        return [f for f in self.feedback if f["invoice_id"] == invoice_id]

    def correct(self, invoice_id, field, value, line_number=None):
        original = None
        if line_number is None:
            original = self.rows[invoice_id].get(field)
        self.feedback.append({"id": len(self.feedback) + 1, "invoice_id": invoice_id, "field_name": field,
                              "line_number": line_number, "original_value": original, "corrected_value": value,
                              "corrected_by": "reviewer@example.com", "corrected_at": "2026-11-04T00:00:00Z"})

    def latest_run(self, invoice_id):
        return max((r for r in self.runs.values() if r["invoice_id"] == invoice_id), key=lambda r: r["number"])


def gemini_reply(messages, **_):
    """Translation echoes the invoice text, so every number survives. Anything else finds nothing to add."""
    prompt = messages[-1]["content"]
    if "translated_text" in prompt:
        text = prompt.rsplit("accurate>}", 1)[-1]  # the end of the instructions in prompts.yaml
        return json.dumps({"detected_language": "xx", "translated_text": text, "confidence": 0.95})
    return json.dumps({"matches": [], "fields": {}})


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepository()
        self.erp = FakeERP()
        self.saver = MemorySaver()
        names = [n for n in vars(FakeRepository) if not n.startswith("_") and hasattr(repository, n)]
        patches = [mock.patch.object(repository, n, getattr(self.repo, n)) for n in names]
        patches += [
            mock.patch.object(llm, "complete", side_effect=gemini_reply),
            mock.patch.object(workflow, "erp_client", lambda: self.erp),
            mock.patch.object(workflow.indexing, "index_invoice", return_value=0),
            mock.patch.object(workflow, "_graph", workflow.build_graph(self.saver)),
            mock.patch.object(workflow, "_saver", self.saver),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_file(self, name, file_type="pdf", folder=SAMPLES):
        path = Path(folder) / name
        invoice_id = self.repo.add(path, file_type)
        workflow.run_new(invoice_id=invoice_id, file_name=name, file_path=str(path), file_type=file_type,
                         email_metadata=None)
        return invoice_id

    def messages(self, invoice_id):
        return " | ".join(d["message"] for d in self.repo.latest_run(invoice_id)["discrepancies"])

    def test_every_sample_gets_its_documented_result(self):
        # Oldest first, as the monitor sees them: the README's table of expected results.
        expected = [("INV_EN_001.pdf", "pdf", "review"), ("INV_EN_002.pdf", "pdf", "approve"),
                    ("INV_ES_003.pdf", "pdf", "review"), ("INV_DE_004.pdf", "pdf", "review"),
                    ("INV_EN_005_scan.png", "image", "review"), ("INV_EN_006_malformed.pdf", "pdf", "review"),
                    ("INV_DE_007.docx", "docx", "review"), ("INV_EN_002_scanned.pdf", "pdf", "review")]
        for name, file_type, recommendation in expected:
            with self.subTest(name):
                invoice_id = self.run_file(name, file_type)
                row = self.repo.rows[invoice_id]
                self.assertEqual(row["processing_status"], "completed")
                self.assertEqual(row["recommendation"], recommendation, self.messages(invoice_id))

    def test_a_price_difference_is_named_in_the_report(self):
        invoice_id = self.run_file("INV_EN_001.pdf")
        self.assertIn("unit price 3.50 vs PO 3", self.messages(invoice_id))
        run = self.repo.latest_run(invoice_id)
        self.assertEqual(run["report"]["recommendation"], "review")
        self.assertIn("INV-1001", run["html"])

    def test_a_scanned_copy_is_read_by_ocr_and_flagged_as_a_duplicate(self):
        self.run_file("INV_EN_002.pdf")
        copy_id = self.run_file("INV_EN_002_scanned.pdf")
        self.assertEqual(self.repo.rows[copy_id]["extraction_method"], "tesseract")
        self.assertIn("already audited", self.messages(copy_id))

    def test_a_translated_invoice_keeps_its_language_and_confidence(self):
        invoice_id = self.run_file("INV_DE_004.pdf")
        row = self.repo.rows[invoice_id]
        self.assertEqual(row["source_language"], "xx")
        self.assertGreaterEqual(row["translation_confidence"], 0.9)

    def test_an_extreme_overcharge_is_rejected(self):
        cheap = copy.deepcopy(ORDERS)
        cheap["PO-1002"][2][0]["unit_price"] = "0.25"
        self.erp = FakeERP(cheap)
        invoice_id = self.run_file("INV_EN_002.pdf")
        self.assertEqual(self.repo.rows[invoice_id]["recommendation"], "reject", self.messages(invoice_id))

    def test_a_correction_and_recheck_change_the_result_without_touching_the_original(self):
        invoice_id = self.run_file("INV_EN_001.pdf")
        original_price = next(i for i in self.repo.rows[invoice_id]["line_items"] if i["line_number"] == 2)["unit_price"]
        self.repo.correct(invoice_id, "unit_price", "3.00", line_number=2)
        result = workflow.revalidate(invoice_id, "reviewer@example.com")
        self.assertEqual(result["run_number"], 2)
        run = self.repo.latest_run(invoice_id)
        self.assertEqual((run["trigger"], run["corrections"][0]["corrected_value"]), ("revalidation", "3.00"))
        self.assertNotIn("unit price 3.50", self.messages(invoice_id))
        stored = next(i for i in self.repo.rows[invoice_id]["line_items"] if i["line_number"] == 2)["unit_price"]
        self.assertEqual(stored, original_price)
        self.assertEqual(self.repo.unapplied_feedback_ids(invoice_id), [])

    def test_a_failure_is_recorded_and_a_retry_resumes_from_that_stage(self):
        erp = self.erp
        with mock.patch.object(erp, "get_po", side_effect=ConnectionError("ERP is down")):
            with self.assertRaises(workflow.WorkflowError):
                self.run_file("INV_EN_002.pdf")
        invoice_id, row = next(iter(self.repo.rows.items()))
        self.assertEqual((row["processing_status"], row["failed_stage"]), ("failed", "validate_business"))

        with mock.patch.object(workflow, "extract", side_effect=AssertionError("extraction ran twice")):
            workflow.resume(invoice_id, row["thread_id"])
        self.assertEqual(row["processing_status"], "completed")
        self.assertEqual(row["recommendation"], "approve")

    def test_two_invoices_on_one_po_are_flagged_even_if_the_first_failed_before_its_po_was_read(self):
        with mock.patch.object(workflow, "extract", side_effect=ConnectionError("storage is down")):
            with self.assertRaises(workflow.WorkflowError):
                self.run_file("INV_EN_002.pdf")
        first, row = next(iter(self.repo.rows.items()))
        self.assertIsNone(row.get("po_number"))
        self.run_file("INV_EN_002_scanned.pdf")  # sees no earlier invoice on the PO

        workflow.resume(first, row["thread_id"])
        self.assertEqual(row["recommendation"], "review")
        self.assertIn("was already invoiced by INV_EN_002_scanned.pdf", self.messages(first))

    def test_a_file_changed_after_detection_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invoice.pdf"
            shutil.copy(SAMPLES / "INV_EN_002.pdf", path)
            invoice_id = self.repo.add(path, "pdf")
            with path.open("ab") as file:
                file.write(b"\n% changed")
            with self.assertRaises(workflow.WorkflowError):
                workflow.run_new(invoice_id=invoice_id, file_name=path.name, file_path=str(path), file_type="pdf",
                                 email_metadata=None)
        self.assertIn("changed after", self.repo.rows[invoice_id]["error_message"])

    def test_an_uploaded_invoice_is_fetched_from_storage_and_audited(self):
        def fake_download(key, destination):
            self.assertEqual(key, "abc.pdf")
            shutil.copy(SAMPLES / "INV_EN_002.pdf", destination)

        invoice_id = self.repo.add(SAMPLES / "INV_EN_002.pdf", "pdf")
        self.repo.rows[invoice_id]["file_path"] = f"{storage.SCHEME}abc.pdf"
        with mock.patch.object(storage, "download", side_effect=fake_download):
            workflow.run_new(invoice_id=invoice_id, file_name="upload.pdf", file_path=f"{storage.SCHEME}abc.pdf",
                             file_type="pdf", email_metadata={"uploaded_by": "reviewer@example.com"})
        self.assertEqual(self.repo.rows[invoice_id]["recommendation"], "approve")

    def test_a_model_reply_cannot_change_the_recommendation(self):
        cheap = copy.deepcopy(ORDERS)
        cheap["PO-1002"][2][0]["unit_price"] = "0.25"
        self.erp = FakeERP(cheap)
        with mock.patch.object(llm, "complete", return_value='{"matches": [], "recommendation": "approve"}'):
            invoice_id = self.run_file("INV_EN_002.pdf")
        self.assertEqual(self.repo.rows[invoice_id]["recommendation"], "reject")

    def test_a_translated_invoice_goes_to_a_person_whatever_the_model_claims(self):
        def claims_perfect_english(messages, **_):
            reply = json.loads(gemini_reply(messages))
            if "translated_text" in reply:
                reply.update(confidence=1.0, detected_language="en")
            return json.dumps(reply)

        with mock.patch.object(llm, "complete", side_effect=claims_perfect_english):
            invoice_id = self.run_file("INV_DE_004.pdf")
            self.assertEqual(self.repo.rows[invoice_id]["recommendation"], "review")
            workflow.revalidate(invoice_id, "reviewer@example.com")
        self.assertEqual(self.repo.rows[invoice_id]["recommendation"], "review")

    def test_a_field_the_ai_recovered_stays_in_review_until_a_person_confirms_it(self):
        parse = workflow.parse_invoice
        printed = {}

        def number_unread(text, symbols):
            parsed = parse(text, symbols)
            printed["number"] = parsed["fields"]["invoice_number"]
            parsed["fields"]["invoice_number"] = None
            return parsed

        reply = lambda *_, **__: json.dumps({"matches": [], "fields": {"invoice_number": printed["number"]}})
        with mock.patch.object(workflow, "parse_invoice", number_unread), \
                mock.patch.object(llm, "complete", side_effect=reply):
            invoice_id = self.run_file("INV_EN_002.pdf")
            row = self.repo.rows[invoice_id]
            self.assertEqual(row["invoice_number"], printed["number"])
            self.assertEqual(row["recommendation"], "review", self.messages(invoice_id))

            workflow.revalidate(invoice_id, "reviewer@example.com")
            self.assertEqual(row["recommendation"], "review", self.messages(invoice_id))

            self.repo.correct(invoice_id, "invoice_number", printed["number"])
            workflow.revalidate(invoice_id, "reviewer@example.com")
        self.assertEqual(row["recommendation"], "approve", self.messages(invoice_id))

    def test_a_later_invoice_on_a_corrected_po_is_flagged(self):
        first = self.run_file("INV_EN_001.pdf")
        self.repo.correct(first, "po_number", "PO-1002")
        workflow.revalidate(first, "reviewer@example.com")
        second = self.run_file("INV_EN_002.pdf")
        self.assertEqual(self.repo.rows[second]["recommendation"], "review")
        self.assertIn("was already invoiced by INV_EN_001.pdf", self.messages(second))

    def test_correcting_to_a_po_a_later_invoice_uses_flags_both_on_their_re_checks(self):
        first, second = self.run_file("INV_EN_001.pdf"), self.run_file("INV_EN_002.pdf")
        self.repo.correct(first, "po_number", "PO-1002")
        workflow.revalidate(first, "reviewer@example.com")
        self.assertIn("was already invoiced by INV_EN_002.pdf", self.messages(first))
        workflow.revalidate(second, "reviewer@example.com")
        self.assertIn("was already invoiced by INV_EN_001.pdf", self.messages(second))

    def test_correcting_to_a_number_a_later_invoice_uses_flags_both_on_their_re_checks(self):
        first, second = self.run_file("INV_EN_001.pdf"), self.run_file("INV_EN_002.pdf")
        later = self.repo.rows[second]
        self.repo.correct(first, "invoice_number", later["invoice_number"])
        self.repo.correct(first, "vendor_name", later["vendor_name"])
        workflow.revalidate(first, "reviewer@example.com")
        self.assertIn("already audited from file INV_EN_002.pdf", self.messages(first))
        workflow.revalidate(second, "reviewer@example.com")
        self.assertIn("already audited from file INV_EN_001.pdf", self.messages(second))

    def test_correcting_only_the_vendor_finds_a_later_invoice_with_the_same_number(self):
        first, second = self.run_file("INV_EN_001.pdf"), self.run_file("INV_EN_002.pdf")
        later = self.repo.rows[second]
        self.repo.rows[first]["invoice_number"] = later["invoice_number"]  # printed the same, vendor misread
        self.repo.correct(first, "vendor_name", later["vendor_name"])
        workflow.revalidate(first, "reviewer@example.com")
        self.assertIn("already audited from file INV_EN_002.pdf", self.messages(first))

    def test_an_uncorrected_re_check_still_ignores_a_later_copy(self):
        original = self.run_file("INV_EN_002.pdf")
        self.run_file("INV_EN_002_scanned.pdf")
        workflow.revalidate(original, "reviewer@example.com")
        self.assertEqual(self.repo.rows[original]["recommendation"], "approve", self.messages(original))


if __name__ == "__main__":
    unittest.main()

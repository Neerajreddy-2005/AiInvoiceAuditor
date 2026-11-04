"""Regression tests for Mock-ERP's access rules. No network or database.

Run from the repository root:
  docker run --rm --network none -v "$PWD/backend/Mock-ERP/tests:/app/tests:ro" \
    invoice-auditor/mock-erp:1.0 python -m unittest discover -s tests -v
"""
import os

os.environ.update({"DB_HOST": "db.invalid", "POSTGRES_DB": "x", "ERP_DB_USER": "erp", "ERP_DB_PASSWORD": "test",
                   "ERP_SERVICE_TOKEN": "test-erp-token"})

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import main


class Access(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_data_routes_need_the_service_token(self):
        for path in ("/po/PO-1", "/vendor/V1", "/sku/S1", "/vendors", "/pos", "/skus"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
            self.assertEqual(self.client.get(path, headers={"X-Service-Token": "wrong"}).status_code, 401, path)

    def test_the_right_token_gets_data(self):
        with mock.patch.object(main, "_all", return_value=[]):
            self.assertEqual(self.client.get("/pos", headers={"X-Service-Token": "test-erp-token"}).status_code, 200)

    def test_api_documentation_is_off(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_health_hides_database_errors(self):
        with mock.patch.object(main, "_one", side_effect=RuntimeError("password authentication failed")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("password", response.text)

    def test_placeholder_token_is_refused(self):
        with mock.patch.dict(os.environ, {"ERP_DB_PASSWORD": "change-me-erp"}):
            with self.assertRaises(RuntimeError):
                main._conninfo()


if __name__ == "__main__":
    unittest.main()

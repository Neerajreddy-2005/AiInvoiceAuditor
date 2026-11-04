"""HTTP client for the mock-erp service.

The agent never reads the erp schema directly (its database role has no
privileges there); all ERP data comes through this API, addressed by
service name and container port: http://mock-erp:8000.
"""
from urllib.parse import quote

import httpx

from .settings import settings


class ERPUnavailable(RuntimeError):
    pass


class ERPClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self._client = httpx.Client(base_url=base_url or settings().erp_base_url, timeout=timeout,
                                    headers={"X-Service-Token": settings().erp_service_token})

    def _get(self, path: str) -> dict | None:
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise ERPUnavailable(f"mock-erp unreachable at {self._client.base_url}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ERPUnavailable(f"mock-erp {path} returned HTTP {response.status_code}")
        return response.json()

    def health(self) -> None:
        if self._get("/health") is None:
            raise ERPUnavailable("mock-erp /health not found")

    def get_po(self, po_number: str) -> dict | None:
        return self._get(f"/po/{quote(po_number, safe='')}")

    def get_vendor(self, vendor_id: str) -> dict | None:
        return self._get(f"/vendor/{quote(vendor_id, safe='')}")

    def get_sku(self, sku_code: str) -> dict | None:
        return self._get(f"/sku/{quote(sku_code, safe='')}")


_client: ERPClient | None = None


def client() -> ERPClient:
    global _client
    if _client is None:
        _client = ERPClient()
    return _client

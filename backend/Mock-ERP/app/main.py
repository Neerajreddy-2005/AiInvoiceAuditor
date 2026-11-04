"""Mock ERP service: read-only HTTP API over the erp schema.

This is the only route to ERP data. Its database role can read the erp
schema and nothing else.
"""
import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

logging.basicConfig(
    stream=sys.stdout,
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [mock-erp] %(name)s: %(message)s",
)
log = logging.getLogger("mock-erp")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name} (set it in .env)")
    return value


def _require_secret(name: str) -> str:
    """The placeholders in .env.example and setup.sql are public, so they are refused."""
    value = _require(name)
    if value.startswith(("change-me", "REPLACE_WITH")):
        raise RuntimeError(f"{name} still has its placeholder value. Set a real secret in .env.")
    return value


def _conninfo() -> str:
    # With Supabase's CA certificate the server's identity is verified as well as encrypted.
    rootcert = os.getenv("DB_SSLROOTCERT")
    tls = {"sslmode": "verify-full", "sslrootcert": rootcert} if rootcert else {"sslmode": "require"}
    return make_conninfo(
        host=_require("DB_HOST"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=_require("POSTGRES_DB"),
        user=_require("ERP_DB_USER"),
        password=_require_secret("ERP_DB_PASSWORD"),
        connect_timeout=10,
        application_name="mock-erp",
        **tls,
    )


SERVICE_TOKEN = _require_secret("ERP_SERVICE_TOKEN")


def _service_only(x_service_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_service_token.encode(), SERVICE_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Service token required")


pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    pool = ConnectionPool(_conninfo(), min_size=1, max_size=2, check=ConnectionPool.check_connection,
                          kwargs={"row_factory": dict_row}, open=True)
    try:
        # Fail fast: a mock-erp that cannot read its data must not report healthy.
        pool.wait(timeout=15)
        with pool.connection() as conn:
            count = conn.execute("SELECT count(*) AS n FROM erp.purchase_orders").fetchone()["n"]
        log.info("Connected to PostgreSQL; %s purchase orders available", count)
    except Exception as exc:
        log.critical("Cannot reach the erp schema in PostgreSQL: %s", exc)
        pool.close()
        raise
    yield
    pool.close()


app = FastAPI(title="Mock ERP", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
api = APIRouter(dependencies=[Depends(_service_only)])


class POLine(BaseModel):
    line_number: int
    item_code: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    currency: str
    line_items: list[POLine]


class Vendor(BaseModel):
    vendor_id: str
    vendor_name: str
    country: str | None
    currency: str


class Sku(BaseModel):
    item_code: str
    category: str | None
    uom: str | None
    gst_rate: Decimal


def _one(sql: str, *params):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()


def _all(sql: str, *params):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/health")
def health():
    try:
        _one("SELECT 1")
    except Exception:
        log.exception("Health check could not reach PostgreSQL")
        return JSONResponse(status_code=503, content={"status": "unhealthy"})
    return {"status": "ok"}


@api.get("/po/{po_number:path}", response_model=PurchaseOrder)
def get_po(po_number: str):
    header = _one("SELECT po_number, vendor_id, currency FROM erp.purchase_orders "
                  "WHERE upper(po_number) = upper(%s)", po_number.strip())
    if not header:
        raise HTTPException(status_code=404, detail=f"PO {po_number} not found")
    lines = _all("SELECT line_number, item_code, description, quantity, unit_price, currency "
                 "FROM erp.po_line_items WHERE po_number = %s ORDER BY line_number",
                 header["po_number"])
    return {**header, "line_items": lines}


@api.get("/vendor/{vendor_id:path}", response_model=Vendor)
def get_vendor(vendor_id: str):
    row = _one("SELECT vendor_id, vendor_name, country, currency FROM erp.vendors "
               "WHERE upper(vendor_id) = upper(%s)", vendor_id.strip())
    if not row:
        raise HTTPException(status_code=404, detail=f"Vendor {vendor_id} not found")
    return row


@api.get("/sku/{sku_code:path}", response_model=Sku)
def get_sku(sku_code: str):
    row = _one("SELECT item_code, category, uom, gst_rate FROM erp.sku_master "
               "WHERE upper(item_code) = upper(%s)", sku_code.strip())
    if not row:
        raise HTTPException(status_code=404, detail=f"SKU {sku_code} not found")
    return row


# Listing endpoints, for debugging and the demo.
@api.get("/vendors", response_model=list[Vendor])
def list_vendors():
    return _all("SELECT vendor_id, vendor_name, country, currency FROM erp.vendors ORDER BY vendor_id")


@api.get("/pos")
def list_pos():
    return _all("SELECT po_number, vendor_id, currency FROM erp.purchase_orders ORDER BY po_number")


@api.get("/skus", response_model=list[Sku])
def list_skus():
    return _all("SELECT item_code, category, uom, gst_rate FROM erp.sku_master ORDER BY item_code")


app.include_router(api)

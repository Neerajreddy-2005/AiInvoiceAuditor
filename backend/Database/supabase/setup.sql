-- =============================================================
-- Creates the invoice auditor's schemas, tables, seed data and
-- service roles in a Supabase project. Run once in the SQL Editor.
--
-- Before running, replace the two REPLACE_WITH_* values below in the
-- editor only, and never save the passwords back into this file.
-- All-or-nothing: if any statement fails, nothing is created.
-- =============================================================

BEGIN;

CREATE SCHEMA langgraph;
CREATE SCHEMA erp;
CREATE SCHEMA audit;

-- ---------------------------------------------------------------
-- erp: master data served only by the mock-erp service
-- ---------------------------------------------------------------
CREATE TABLE erp.vendors (
    vendor_id    VARCHAR(32)  PRIMARY KEY,
    vendor_name  VARCHAR(255) NOT NULL,
    country      VARCHAR(64),
    currency     CHAR(3)      NOT NULL
);

CREATE TABLE erp.sku_master (
    item_code    VARCHAR(64)  PRIMARY KEY,
    category     VARCHAR(64),
    uom          VARCHAR(32),
    gst_rate     NUMERIC(5,2) NOT NULL          -- percent, e.g. 10.00
);

CREATE TABLE erp.purchase_orders (
    po_number    VARCHAR(64)  PRIMARY KEY,
    vendor_id    VARCHAR(32)  NOT NULL REFERENCES erp.vendors(vendor_id),
    currency     CHAR(3)      NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE erp.po_line_items (
    id           BIGSERIAL     PRIMARY KEY,
    po_number    VARCHAR(64)   NOT NULL REFERENCES erp.purchase_orders(po_number),
    line_number  INTEGER       NOT NULL,
    item_code    VARCHAR(64)   NOT NULL REFERENCES erp.sku_master(item_code),
    description  TEXT          NOT NULL,
    quantity     NUMERIC(12,3) NOT NULL,
    unit_price   NUMERIC(14,4) NOT NULL,
    currency     CHAR(3)       NOT NULL,
    UNIQUE (po_number, line_number)
);

CREATE INDEX idx_po_vendor ON erp.purchase_orders(vendor_id);

INSERT INTO erp.vendors (vendor_id, vendor_name, country, currency) VALUES
    ('VEND-001', 'Global Logistics Ltd',      'UK',      'USD'),
    ('VEND-002', 'BlueOcean Transport Co.',   'USA',     'USD'),
    ('VEND-003', 'Transporte Ibérico S.A.',   'Spain',   'EUR'),
    ('VEND-004', 'HafenLogistik GmbH',        'Germany', 'EUR'),
    ('VEND-005', 'SwiftMove Couriers',        'USA',     'USD'),
    ('VEND-006', 'OceanFreight Pvt Ltd',      'India',   'INR');

INSERT INTO erp.sku_master (item_code, category, uom, gst_rate) VALUES
    ('SKU-001', 'Packaging', 'roll',    10),
    ('SKU-002', 'Safety',    'pair',    10),
    ('SKU-003', 'Safety',    'piece',   10),
    ('SKU-101', 'Logistics', 'piece',   10),
    ('SKU-201', 'Packaging', 'box',     10),
    ('SKU-202', 'Packaging', 'label',   10),
    ('SKU-301', 'Transport', 'crate',   10),
    ('SKU-302', 'Safety',    'vest',    10),
    ('SKU-401', 'Charges',   'service', 10),
    ('SKU-402', 'Packaging', 'box',     10),
    ('SKU-501', 'Transport', 'service', 10);

INSERT INTO erp.purchase_orders (po_number, vendor_id, currency) VALUES
    ('PO-1001', 'VEND-001', 'USD'),
    ('PO-1002', 'VEND-002', 'USD'),
    ('PO-1003', 'VEND-003', 'EUR'),
    ('PO-1004', 'VEND-004', 'EUR'),
    ('PO-1005', 'VEND-005', 'USD'),
    ('PO-1006', 'VEND-006', 'INR');

INSERT INTO erp.po_line_items (po_number, line_number, item_code, description, quantity, unit_price, currency) VALUES
    ('PO-1001', 1, 'SKU-001', 'Pallet Wrapping Film',   50,   12.00, 'USD'),
    ('PO-1001', 2, 'SKU-002', 'Industrial Gloves',      120,   3.00, 'USD'),
    ('PO-1001', 3, 'SKU-003', 'Safety Helmets',         30,   15.00, 'USD'),
    ('PO-1002', 1, 'SKU-101', 'Container Seals',        200,   1.25, 'USD'),
    ('PO-1003', 1, 'SKU-201', 'Cajas de carton',        100,   2.50, 'EUR'),
    ('PO-1003', 2, 'SKU-202', 'Etiquetas adhesivas',    300,   0.30, 'EUR'),
    ('PO-1004', 1, 'SKU-301', 'Transportkisten',        40,   25.00, 'EUR'),
    ('PO-1004', 2, 'SKU-302', 'Sicherheitswesten',      60,   10.00, 'EUR'),
    ('PO-1005', 1, 'SKU-401', 'Fuel Surcharge',         1,   120.00, 'USD'),
    ('PO-1005', 2, 'SKU-402', 'Express Delivery Boxes', 25,    4.50, 'USD'),
    ('PO-1006', 1, 'SKU-501', 'Container Transport',    5,  8000.00, 'INR');

-- ---------------------------------------------------------------
-- audit: the durable system of record, written only by the agent.
-- Human corrections are INSERTs into human_feedback, never UPDATEs
-- of the extracted values; every validation is its own run row.
-- ---------------------------------------------------------------
CREATE TABLE audit.invoice_audit (
    invoice_id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),

    -- the file (reference only)
    file_name              VARCHAR(255)  NOT NULL,
    file_path              VARCHAR(512)  NOT NULL,              -- reference, not the file
    file_checksum          CHAR(64)      NOT NULL UNIQUE,       -- sha256 of content; detects reprocessing
    file_type              VARCHAR(16),                         -- pdf | docx | image | NULL (unsupported)
    file_size_bytes        BIGINT,
    email_metadata         JSONB,                               -- sidecar <name>.meta.json, if any

    -- pipeline status (every detected file gets a row, including failures)
    processing_status      VARCHAR(32)   NOT NULL DEFAULT 'detected',
        -- detected | processing | completed | failed | unsupported
    attempts               INTEGER       NOT NULL DEFAULT 0,
    failed_stage           VARCHAR(64),
    error_message          TEXT,
    last_attempt_at        TIMESTAMPTZ,

    -- extraction (unstructured)
    extraction_method      VARCHAR(32),                         -- pdfplumber | pdfplumber+tesseract | docx | tesseract
    raw_extracted_text     TEXT,                                -- full text, pre-translation
    translated_text        TEXT,
    source_language        VARCHAR(8),
    translation_confidence NUMERIC(4,3),

    -- extraction (structured, typed)
    invoice_number         VARCHAR(64),
    vendor_name            VARCHAR(255),
    vendor_id              VARCHAR(32),                         -- resolved through the mock-erp API
    po_number              VARCHAR(64),
    invoice_date           DATE,
    currency               CHAR(3),
    subtotal_amount        NUMERIC(14,2),
    tax_rate_pct           NUMERIC(6,3),
    tax_amount             NUMERIC(14,2),
    total_amount           NUMERIC(14,2),

    -- extraction (variable extras)
    extracted_raw          JSONB         NOT NULL DEFAULT '{}'::jsonb,

    -- current outcome (latest validation run; full history in validation_runs)
    validation_status      VARCHAR(32),                         -- passed | partial | failed
    recommendation         VARCHAR(32),                         -- approve | review | reject

    thread_id              VARCHAR(128),                        -- links to langgraph checkpoints
    detected_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    processed_at           TIMESTAMPTZ,
    indexed_at             TIMESTAMPTZ,

    CONSTRAINT chk_status CHECK (processing_status IN
        ('detected','processing','completed','failed','unsupported')),
    CONSTRAINT chk_recommendation CHECK (recommendation IS NULL OR recommendation IN
        ('approve','review','reject'))
);

CREATE INDEX idx_invoice_vendor         ON audit.invoice_audit (vendor_name);
CREATE INDEX idx_invoice_date           ON audit.invoice_audit (invoice_date);
CREATE INDEX idx_invoice_recommendation ON audit.invoice_audit (recommendation);
CREATE INDEX idx_invoice_status         ON audit.invoice_audit (processing_status);
CREATE INDEX idx_invoice_number         ON audit.invoice_audit (invoice_number);

CREATE TABLE audit.invoice_line_items (
    id           BIGSERIAL     PRIMARY KEY,
    invoice_id   UUID          NOT NULL REFERENCES audit.invoice_audit(invoice_id),
    line_number  INTEGER       NOT NULL,
    item_code    VARCHAR(64),
    description  TEXT,
    quantity     NUMERIC(12,3),
    unit_price   NUMERIC(14,4),
    line_total   NUMERIC(14,2),
    raw_line     TEXT,                                          -- the source line it was parsed from
    UNIQUE (invoice_id, line_number)
);

-- One row per validation pass: the initial run and every re-validation.
CREATE TABLE audit.validation_runs (
    run_id                 BIGSERIAL     PRIMARY KEY,
    invoice_id             UUID          NOT NULL REFERENCES audit.invoice_audit(invoice_id),
    run_number             INTEGER       NOT NULL,
    trigger                VARCHAR(16)   NOT NULL,              -- initial | revalidation
    triggered_by           VARCHAR(128),
    validation_status      VARCHAR(32),
    recommendation         VARCHAR(32),
    recommendation_reasons JSONB,
    missing_fields         JSONB,
    corrections_applied    JSONB,                               -- corrections layered over the extraction
    summary                TEXT,                                -- LLM-written audit summary
    report_json            JSONB,
    report_html            TEXT,
    report_json_path       VARCHAR(512),
    report_html_path       VARCHAR(512),
    started_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    completed_at           TIMESTAMPTZ,
    UNIQUE (invoice_id, run_number),
    CONSTRAINT chk_run_trigger CHECK (trigger IN ('initial','revalidation'))
);

CREATE TABLE audit.discrepancies (
    id             BIGSERIAL     PRIMARY KEY,
    invoice_id     UUID          NOT NULL REFERENCES audit.invoice_audit(invoice_id),
    run_id         BIGINT        NOT NULL REFERENCES audit.validation_runs(run_id),
    check_type     VARCHAR(32)   NOT NULL,                      -- completeness | arithmetic | currency | duplicate | erp_*
    field_name     VARCHAR(64)   NOT NULL,
    line_number    INTEGER,                                     -- NULL for header-level fields
    invoice_value  TEXT,
    erp_value      TEXT,                                        -- value from the ERP (mock-erp API)
    expected_value TEXT,                                        -- value computed from the invoice itself
    deviation_pct  NUMERIC(10,4),
    tolerance_pct  NUMERIC(8,4),
    severity       VARCHAR(16)   NOT NULL,                      -- high | medium | low
    action         VARCHAR(16)   NOT NULL,                      -- reject | manual_review | info
    message        TEXT,
    detected_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX idx_discrepancy_invoice ON audit.discrepancies (invoice_id);
CREATE INDEX idx_discrepancy_run     ON audit.discrepancies (run_id);

-- Human-in-the-loop: append-only. The original extraction is never modified.
CREATE TABLE audit.human_feedback (
    id                 BIGSERIAL     PRIMARY KEY,
    invoice_id         UUID          NOT NULL REFERENCES audit.invoice_audit(invoice_id),
    feedback_type      VARCHAR(16)   NOT NULL,                  -- correction | decision | comment
    field_name         VARCHAR(64),                             -- e.g. invoice_number, unit_price
    line_number        INTEGER,                                 -- set for line-item corrections
    original_value     TEXT,                                    -- the extracted value at correction time
    corrected_value    TEXT,
    comment            TEXT,
    corrected_by       VARCHAR(128)  NOT NULL,
    corrected_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    revalidated        BOOLEAN       NOT NULL DEFAULT FALSE,
    revalidated_run_id BIGINT        REFERENCES audit.validation_runs(run_id),
    CONSTRAINT chk_feedback_type CHECK (feedback_type IN ('correction','decision','comment'))
);

CREATE INDEX idx_feedback_invoice ON audit.human_feedback (invoice_id);

-- ---------------------------------------------------------------
-- Service roles with least privilege. Nobody, including the agent,
-- can DELETE from the audit trail. Supabase's own API roles get no
-- access to these schemas.
-- ---------------------------------------------------------------
CREATE ROLE agent    LOGIN PASSWORD 'REPLACE_WITH_AGENT_DB_PASSWORD';
CREATE ROLE mock_erp LOGIN PASSWORD 'REPLACE_WITH_ERP_DB_PASSWORD';

-- langgraph: the agent's checkpointer creates and prunes its own tables
GRANT USAGE, CREATE ON SCHEMA langgraph TO agent;

-- erp: read-only, mock-erp only
GRANT USAGE ON SCHEMA erp TO mock_erp;
GRANT SELECT ON ALL TABLES IN SCHEMA erp TO mock_erp;

-- audit: the agent appends and updates pipeline status; no DELETE
GRANT USAGE ON SCHEMA audit TO agent;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA audit TO agent;
-- only the columns the pipeline changes: which file was detected, and when, can't be rewritten
GRANT UPDATE (processing_status, failed_stage, error_message, thread_id, attempts, last_attempt_at,
              extraction_method, raw_extracted_text, invoice_number, invoice_date, vendor_name, po_number,
              currency, subtotal_amount, tax_rate_pct, tax_amount, total_amount, extracted_raw,
              source_language, translated_text, translation_confidence, vendor_id,
              validation_status, recommendation, processed_at, indexed_at)
    ON audit.invoice_audit TO agent;
GRANT UPDATE (validation_status, recommendation, recommendation_reasons, missing_fields, summary,
              report_json, report_html, report_json_path, report_html_path, completed_at)
    ON audit.validation_runs TO agent;
-- extracted line items and discrepancies are immutable once written;
-- feedback rows can only be marked as used by a re-validation
GRANT UPDATE (revalidated, revalidated_run_id) ON audit.human_feedback TO agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA audit TO agent;

-- Supabase's API roles get nothing here, stated explicitly rather than assumed
REVOKE ALL ON SCHEMA erp, audit, langgraph FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA erp, audit, langgraph FROM PUBLIC, anon, authenticated;

COMMIT;

-- Expected: audit_tables 5, roles 2, vendors 6, skus 11, purchase_orders 6, po_lines 11
SELECT
    (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'audit') AS audit_tables,
    (SELECT count(*) FROM pg_roles WHERE rolname IN ('agent', 'mock_erp'))         AS roles,
    (SELECT count(*) FROM erp.vendors)                                             AS vendors,
    (SELECT count(*) FROM erp.sku_master)                                          AS skus,
    (SELECT count(*) FROM erp.purchase_orders)                                     AS purchase_orders,
    (SELECT count(*) FROM erp.po_line_items)                                       AS po_lines;

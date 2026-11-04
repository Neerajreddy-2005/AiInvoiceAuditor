-- For a database created before these limits were added to setup.sql.
-- Paste into the Supabase SQL Editor once. Safe to run again.

BEGIN;

-- The agent can change only the columns the pipeline changes.
REVOKE UPDATE ON audit.invoice_audit, audit.validation_runs FROM agent;
GRANT UPDATE (processing_status, failed_stage, error_message, thread_id, attempts, last_attempt_at,
              extraction_method, raw_extracted_text, invoice_number, invoice_date, vendor_name, po_number,
              currency, subtotal_amount, tax_rate_pct, tax_amount, total_amount, extracted_raw,
              source_language, translated_text, translation_confidence, vendor_id,
              validation_status, recommendation, processed_at, indexed_at)
    ON audit.invoice_audit TO agent;
GRANT UPDATE (validation_status, recommendation, recommendation_reasons, missing_fields, summary,
              report_json, report_html, report_json_path, report_html_path, completed_at)
    ON audit.validation_runs TO agent;

-- Supabase's API roles get nothing in these schemas.
REVOKE ALL ON SCHEMA erp, audit, langgraph FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA erp, audit, langgraph FROM PUBLIC, anon, authenticated;

COMMIT;

-- Expected: 0 rows. Any row is access the browser's public key should not have.
SELECT table_schema, table_name, grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema IN ('erp', 'audit', 'langgraph')
  AND grantee IN ('PUBLIC', 'anon', 'authenticated');

-- =============================================================
-- Row-level security for the service roles. Run once in the SQL
-- Editor, after setup.sql. Contains no secrets.
--
-- Supabase can switch RLS on for every new table, and RLS with no
-- policies hides all rows from non-owners. These policies mirror the
-- grants in setup.sql; nobody gets DELETE, and Supabase's API roles
-- (anon, authenticated) get nothing.
-- =============================================================

BEGIN;

ALTER TABLE erp.vendors              ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.sku_master           ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.purchase_orders      ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.po_line_items        ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.invoice_audit      ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.invoice_line_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.validation_runs    ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.discrepancies      ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.human_feedback     ENABLE ROW LEVEL SECURITY;

-- erp: read-only, mock-erp only
CREATE POLICY mock_erp_select ON erp.vendors         FOR SELECT TO mock_erp USING (true);
CREATE POLICY mock_erp_select ON erp.sku_master      FOR SELECT TO mock_erp USING (true);
CREATE POLICY mock_erp_select ON erp.purchase_orders FOR SELECT TO mock_erp USING (true);
CREATE POLICY mock_erp_select ON erp.po_line_items   FOR SELECT TO mock_erp USING (true);

-- audit: the agent reads and appends everywhere
CREATE POLICY agent_select ON audit.invoice_audit      FOR SELECT TO agent USING (true);
CREATE POLICY agent_select ON audit.invoice_line_items FOR SELECT TO agent USING (true);
CREATE POLICY agent_select ON audit.validation_runs    FOR SELECT TO agent USING (true);
CREATE POLICY agent_select ON audit.discrepancies      FOR SELECT TO agent USING (true);
CREATE POLICY agent_select ON audit.human_feedback     FOR SELECT TO agent USING (true);

CREATE POLICY agent_insert ON audit.invoice_audit      FOR INSERT TO agent WITH CHECK (true);
CREATE POLICY agent_insert ON audit.invoice_line_items FOR INSERT TO agent WITH CHECK (true);
CREATE POLICY agent_insert ON audit.validation_runs    FOR INSERT TO agent WITH CHECK (true);
CREATE POLICY agent_insert ON audit.discrepancies      FOR INSERT TO agent WITH CHECK (true);
CREATE POLICY agent_insert ON audit.human_feedback     FOR INSERT TO agent WITH CHECK (true);

-- audit: only these tables can be updated; the column grant in setup.sql
-- still limits human_feedback to its revalidated columns
CREATE POLICY agent_update ON audit.invoice_audit   FOR UPDATE TO agent USING (true) WITH CHECK (true);
CREATE POLICY agent_update ON audit.validation_runs FOR UPDATE TO agent USING (true) WITH CHECK (true);
CREATE POLICY agent_update ON audit.human_feedback  FOR UPDATE TO agent USING (true) WITH CHECK (true);

COMMIT;

-- Expected: policies 17, vendors 6, po_lines 11
SELECT
    (SELECT count(*) FROM pg_policies WHERE schemaname IN ('erp', 'audit')) AS policies,
    (SELECT count(*) FROM erp.vendors)                                      AS vendors,
    (SELECT count(*) FROM erp.po_line_items)                                AS po_lines;

-- 007 (B12/T3-3): Row-Level Security untuk tabel ber-customer yang
-- sebelumnya TANPA RLS: mt5_terminals, mt5_connect_jobs, mt5_accounts,
-- customers, auth_tokens, audit_logs.
--
-- Model scope (backend: db.set_customer_context / db.set_system_scope,
-- keduanya set_config(..., is_local => true)):
--   * customer scope : app.customer_id = <id>, app.scope = ''
--       -> hanya baris milik customer itu (baca DAN tulis; WITH CHECK
--          menolak INSERT/UPDATE yang memindahkan baris ke customer lain).
--   * system scope   : app.customer_id = '', app.scope = 'system'
--       -> lintas customer; HANYA dipasang jalur backend tepercaya:
--          lookup pra-autentikasi (token/login), admin, worker &
--          pemulihan.
--   * tanpa scope    : DEFAULT-DENY (tidak ada baris terlihat/terubah).
--   Bila keduanya entah bagaimana terpasang, customer scope menang
--   (paling ketat).
-- RLS tetap DEFENSE-IN-DEPTH — filter customer_id di query backend tetap
-- wajib. Owner database (migrasi/tool) tetap bypass (tanpa FORCE),
-- konsisten dengan 001/002. Pemeriksaan & aksi FOREIGN KEY (mis.
-- ON DELETE SET NULL audit_logs, CASCADE auth_tokens) tidak terkena RLS.
-- Rollback: migrations/postgres/rollback/007_rls_tenant_tables.down.sql

CREATE OR REPLACE FUNCTION public.app_rls_allows(row_customer bigint)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE
AS $$
  SELECT CASE
    WHEN NULLIF(current_setting('app.customer_id', true), '') IS NOT NULL
      THEN row_customer IS NOT NULL AND row_customer =
           NULLIF(current_setting('app.customer_id', true), '')::bigint
    ELSE COALESCE(current_setting('app.scope', true), '') = 'system'
  END
$$;
REVOKE ALL ON FUNCTION public.app_rls_allows(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.app_rls_allows(bigint) TO transcendence_app;

DO $$
DECLARE
  t   text;
  col text;
BEGIN
  FOR t, col IN SELECT * FROM (VALUES
      ('mt5_terminals',    'customer_id'),
      ('mt5_connect_jobs', 'customer_id'),
      ('mt5_accounts',     'customer_id'),
      ('customers',        'id'),
      ('auth_tokens',      'customer_id'),
      ('audit_logs',       'customer_id')) AS v(t, col)
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public' AND tablename = t
        AND policyname = t || '_tenant_isolation'
    ) THEN
      EXECUTE format(
        'CREATE POLICY %I ON %I USING (public.app_rls_allows(%I))'
        ' WITH CHECK (public.app_rls_allows(%I))',
        t || '_tenant_isolation', t, col, col);
    END IF;
  END LOOP;
END $$;

-- ROLLBACK 007 (B12/T3-3). Dijalankan MANUAL dengan DSN OWNER bila 007
-- harus dibatalkan. Tidak dipakai migrate() (subdirektori tidak di-glob).
-- Mengembalikan keadaan persis sebelum 007: RLS & policy enam tabel
-- dicabut, fungsi dihapus, ledger versi 7 dihapus. Data tidak disentuh.
-- RLS 001/002 (mt5_deals, ..., mt5_credentials) TIDAK disentuh.
BEGIN;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['mt5_terminals','mt5_connect_jobs',
                           'mt5_accounts','customers','auth_tokens',
                           'audit_logs']
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I',
                   t || '_tenant_isolation', t);
    EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
DROP FUNCTION IF EXISTS public.app_rls_allows(bigint);
DELETE FROM schema_migrations WHERE version = 7;
COMMIT;

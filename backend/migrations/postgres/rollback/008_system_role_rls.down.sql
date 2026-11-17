-- ROLLBACK 008 (B12.1) -> keadaan 007 (B12). Dijalankan MANUAL dengan DSN
-- OWNER, bersama rollback KODE ke B12 (kode B12.1 tidak memakai app.scope
-- dan kode B12 tidak memakai role sistem — keduanya berpasangan).
-- Data tidak disentuh. RLS 001/002 tidak disentuh. Role
-- transcendence_system bersifat cluster-wide: hak aksesnya di DB ini
-- dicabut; DROP ROLE manual hanya setelah dicabut di SEMUA database.
BEGIN;
CREATE OR REPLACE FUNCTION public.app_rls_allows(row_customer bigint)
RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $f$
  SELECT CASE
    WHEN NULLIF(current_setting('app.customer_id', true), '') IS NOT NULL
      THEN row_customer IS NOT NULL AND row_customer =
           NULLIF(current_setting('app.customer_id', true), '')::bigint
    ELSE COALESCE(current_setting('app.scope', true), '') = 'system'
  END
$f$;
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
      ('audit_logs',       'customer_id'),
      ('users',            'customer_id')) AS v(t, col)
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_app', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_system', t);
    IF t <> 'users' THEN
      EXECUTE format(
        'CREATE POLICY %I ON %I USING (public.app_rls_allows(%I))'
        ' WITH CHECK (public.app_rls_allows(%I))',
        t || '_tenant_isolation', t, col, col);
    END IF;
  END LOOP;
END $$;
ALTER TABLE users DISABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sync_logs_app ON sync_logs;
DROP POLICY IF EXISTS sync_logs_system ON sync_logs;
ALTER TABLE sync_logs DISABLE ROW LEVEL SECURITY;
DROP FUNCTION IF EXISTS public.app_customer_allows(bigint);
DROP FUNCTION IF EXISTS public.app_system_allows(bigint);
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM transcendence_system;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE USAGE, SELECT ON SEQUENCES FROM transcendence_system;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM transcendence_system;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM transcendence_system;
REVOKE USAGE ON SCHEMA public FROM transcendence_system;
-- B12.3: DEFAULT PRIVILEGES dipulihkan PERSIS ke keadaan sebelum 008
-- (snapshot transcendence_meta.default_acl_pre_008, creator & level apa
-- pun): hanya grant yang dicabut 008 (kepada transcendence_app/_system,
-- dan PUBLIC pada TABLES/SEQUENCES) yang diberikan kembali. Snapshot tidak
-- ada -> BERHENTI (tidak menebak keadaan awal).
DO $$
DECLARE
  s    record;
  x    record;
  kw   text;
  stmt text;
BEGIN
  IF to_regclass('transcendence_meta.default_acl_pre_008') IS NULL THEN
    RAISE EXCEPTION 'B12.3: snapshot default privileges pra-008 tidak ada '
      '(transcendence_meta.default_acl_pre_008) — rollback dihentikan';
  END IF;
  FOR s IN SELECT * FROM transcendence_meta.default_acl_pre_008 LOOP
    kw := CASE s.objtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                         WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES'
                         WHEN 'n' THEN 'SCHEMAS' END;
    FOR x IN SELECT * FROM aclexplode(s.acl) e
              WHERE e.grantee IN ('transcendence_app'::regrole,
                                  'transcendence_system'::regrole)
                 OR (e.grantee = 0 AND s.objtype IN ('r','S')) LOOP
      stmt := format('ALTER DEFAULT PRIVILEGES FOR ROLE %I', s.creator)
           || CASE WHEN s.nspname IS NULL THEN ''
                   ELSE format(' IN SCHEMA %I', s.nspname) END
           || format(' GRANT %s ON %s TO %s', x.privilege_type, kw,
                     CASE WHEN x.grantee = 0 THEN 'PUBLIC'
                          ELSE quote_ident(pg_get_userbyid(x.grantee)) END)
           || CASE WHEN x.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END;
      EXECUTE stmt;
    END LOOP;
  END LOOP;
END $$;
DROP TABLE transcendence_meta.default_acl_pre_008;
DROP SCHEMA transcendence_meta;          -- tanpa CASCADE: hanya bila kosong
DELETE FROM schema_migrations WHERE version = 8;
COMMIT;

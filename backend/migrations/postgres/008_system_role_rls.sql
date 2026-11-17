-- 008 (B12.1): scope SISTEM tidak lagi berupa GUC yang dapat dipalsukan.
--
-- Masalah 007: policy menerima app.scope='system'. GUC dapat diubah oleh
-- role aplikasi lewat set_config(), jadi SQL injection pada role aplikasi
-- dapat memalsukan scope sistem (dibuktikan tes B12.1).
--
-- Keputusan (lihat KEPUTUSAN-ARSITEKTUR-B12.1.md): ROLE TERPISAH.
--   * transcendence_app    (role request/customer, sudah ada sejak 002)
--       -> policy *_app: HANYA baris customer pada app.customer_id;
--          tanpa customer = default-deny. app.scope DIABAIKAN.
--   * transcendence_system (role BARU: worker, pemulihan, scheduler,
--       pra-autentikasi, admin terautentikasi) — login & kredensial
--       TERPISAH; role aplikasi BUKAN anggotanya (tidak bisa SET ROLE).
--       -> policy *_system: lintas customer bila app.customer_id kosong;
--          bila app.customer_id terpasang, DIBATASI ke customer itu
--          (langkah per-customer di worker tetap ditegakkan DB).
--   Keduanya NOBYPASSRLS. Owner tetap bypass (migrasi/tool), seperti
--   001/002. RLS 001/002 (mt5_deals..mt5_credentials, policy PUBLIC
--   berbasis app.customer_id) TIDAK diubah: tanpa customer context, role
--   sistem pun tidak melihat kredensial.
--
-- Tabel: 6 tabel 007 + users (customer_id) + sync_logs (via account_id).
-- Pengecualian BERALASAN (tanpa RLS): auth_throttle, auth_lockouts —
--   hanya key_hash SHA-256 + penghitung, tanpa relasi customer, wajib
--   dapat diakses pra-autentikasi oleh role aplikasi (rate limit/lockout).
-- Rollback: migrations/postgres/rollback/008_system_role_rls.down.sql

-- ---------------------------------------------- role sistem (B12.2: H3)
-- Role baru: dibuat dengan atribut aman. Role SUDAH ADA (cluster-wide,
-- mis. dari DB lain / dibuat manual): atribut yang salah DIPERBAIKI dengan
-- hanya MENCABUT hak (ALTER per atribut yang salah saja); bila role migrasi
-- tidak berhak memperbaikinya -> migrasi BERHENTI dengan pesan eksplisit.
-- Keanggotaan role & objek milik role TIDAK diubah otomatis (bisa milik
-- konfigurasi lain) -> migrasi BERHENTI; admin mencabut manual.
DO $$
DECLARE
  r     record;
  fixes text[] := '{}';
  f     text;
  bad   text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles
                 WHERE rolname = 'transcendence_system') THEN
    CREATE ROLE transcendence_system
      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
      NOINHERIT;
  END IF;
  SELECT * INTO r FROM pg_roles WHERE rolname = 'transcendence_system';
  IF r.rolsuper       THEN fixes := fixes || 'NOSUPERUSER'::text;   END IF;
  IF r.rolbypassrls   THEN fixes := fixes || 'NOBYPASSRLS'::text;   END IF;
  IF r.rolcreaterole  THEN fixes := fixes || 'NOCREATEROLE'::text;  END IF;
  IF r.rolcreatedb    THEN fixes := fixes || 'NOCREATEDB'::text;    END IF;
  IF r.rolreplication THEN fixes := fixes || 'NOREPLICATION'::text; END IF;
  IF r.rolinherit     THEN fixes := fixes || 'NOINHERIT'::text;     END IF;
  IF NOT r.rolcanlogin THEN fixes := fixes || 'LOGIN'::text;        END IF;
  FOREACH f IN ARRAY fixes LOOP
    BEGIN
      EXECUTE format('ALTER ROLE transcendence_system %s', f);
      RAISE NOTICE 'B12.2: atribut role transcendence_system diperbaiki: %',
        f;
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE EXCEPTION 'B12.2: role transcendence_system sudah ada dengan '
        'atribut tidak aman (%), dan role migrasi % tidak berhak '
        'memperbaikinya: % — perbaiki dengan superuser lalu ulangi migrasi',
        f, current_user, SQLERRM;
    END;
  END LOOP;
  -- keanggotaan: role sistem TIDAK boleh anggota role apa pun; anggota
  -- role sistem hanya boleh superuser / owner DB (ADMIN pembuat role).
  SELECT string_agg(format('%s -> %s', mm.rolname, gg.rolname), ', ')
    INTO bad
    FROM pg_auth_members m
    JOIN pg_roles mm ON mm.oid = m.member
    JOIN pg_roles gg ON gg.oid = m.roleid
   WHERE (m.member = r.oid)
      OR (m.roleid = r.oid AND NOT mm.rolsuper AND mm.oid <>
          (SELECT datdba FROM pg_database
            WHERE datname = current_database()));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'B12.2: keanggotaan role transcendence_system tidak '
      'aman: % — cabut manual (REVOKE ... FROM/TO) lalu ulangi migrasi',
      bad;
  END IF;
  IF pg_has_role('transcendence_app', r.oid, 'MEMBER') THEN
    RAISE EXCEPTION 'B12.2: transcendence_app dapat memakai role '
      'transcendence_system (keanggotaan tidak langsung) — cabut manual';
  END IF;
  -- B12.3: kepemilikan objek APA PUN (relasi, schema, function/
  -- procedure, type/domain/enum, operator, extension, FDW, publication,
  -- default ACL, database, tablespace, ...) di DB mana pun pada cluster,
  -- via pg_shdepend deptype 'o'. Objek internal PostgreSQL dimiliki
  -- bootstrap superuser (pinned, tidak tercatat) sehingga tidak terhitung.
  -- TIDAK dipindahkan otomatis (bisa dipakai komponen lain) -> BERHENTI.
  SELECT string_agg(CASE
           WHEN d.dbid = (SELECT oid FROM pg_database
                           WHERE datname = current_database())
             THEN pg_describe_object(d.classid, d.objid, d.objsubid)
           WHEN d.dbid = 0
             THEN pg_describe_object(d.classid, d.objid, d.objsubid)
                  || ' (shared)'
           ELSE format('objek di database lain (oid %s)', d.dbid)
         END, ', ' ORDER BY d.dbid, d.classid, d.objid)
    INTO bad
    FROM pg_shdepend d
   WHERE d.refclassid = 'pg_authid'::regclass
     AND d.refobjid = r.oid AND d.deptype = 'o';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'B12.3: transcendence_system memiliki objek (owner '
      'melewati RLS / dapat ALTER-DROP-REPLACE): % — pindahkan '
      'kepemilikan secara manual (ALTER ... OWNER TO <owner aplikasi>) '
      'lalu ulangi migrasi', bad;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = r.oid)
     OR EXISTS (SELECT 1 FROM pg_proc WHERE proowner = r.oid)
     OR EXISTS (SELECT 1 FROM pg_type WHERE typowner = r.oid)
     OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = r.oid)
     OR EXISTS (SELECT 1 FROM pg_database WHERE datdba = r.oid) THEN
    RAISE EXCEPTION 'B12.3: transcendence_system memiliki schema/'
      'function/procedure/type/relasi/database di database ini — '
      'pindahkan kepemilikan secara manual lalu ulangi migrasi';
  END IF;
  -- verifikasi akhir (gagal tertutup bila perbaikan tidak berlaku)
  SELECT * INTO r FROM pg_roles WHERE rolname = 'transcendence_system';
  IF r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb
     OR r.rolreplication OR r.rolinherit OR NOT r.rolcanlogin THEN
    RAISE EXCEPTION 'B12.2: atribut role transcendence_system masih tidak '
      'aman setelah perbaikan';
  END IF;
END $$;

-- ---------------------------------- hak akses minimum (B12.2: H4)
-- TIDAK ada GRANT ON ALL TABLES dan TIDAK ada DEFAULT PRIVILEGES untuk role
-- sistem: tabel baru (yang mungkin belum ber-RLS) TIDAK otomatis terbuka.
-- Daftar eksplisit = tabel yang dipakai jalur sistem (inventaris B12.2):
--   tenant (RLS 007/008) : mt5_terminals, mt5_connect_jobs, mt5_accounts,
--                          customers, users, auth_tokens       (DML)
--   audit_logs           : SELECT, INSERT (append-only)
--   sync_logs            : SELECT, INSERT (append-only)
--   RLS 001/002 (worker sync/cleanup dgn customer context):
--                          mt5_credentials, mt5_deals, mt5_orders,
--                          mt5_trades, mt5_symbols, mt5_sync_state (DML)
--   auth_lockouts        : DML (login pra-auth & lockout job gagal; hanya
--                          key_hash — pengecualian RLS beralasan)
-- auth_throttle & schema_migrations TIDAK diberikan (tidak dipakai).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM transcendence_system;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM transcendence_system;
GRANT USAGE ON SCHEMA public TO transcendence_system;
DO $$
DECLARE
  t   text;
  prv text;
  sq  text;
  c   record;
BEGIN
  FOR t, prv IN SELECT * FROM (VALUES
      ('mt5_terminals',    'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_connect_jobs', 'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_accounts',     'SELECT, INSERT, UPDATE, DELETE'),
      ('customers',        'SELECT, INSERT, UPDATE, DELETE'),
      ('users',            'SELECT, INSERT, UPDATE, DELETE'),
      ('auth_tokens',      'SELECT, INSERT, UPDATE, DELETE'),
      ('audit_logs',       'SELECT, INSERT'),
      ('sync_logs',        'SELECT, INSERT'),
      ('mt5_credentials',  'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_deals',        'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_orders',       'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_trades',       'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_symbols',      'SELECT, INSERT, UPDATE, DELETE'),
      ('mt5_sync_state',   'SELECT, INSERT, UPDATE, DELETE'),
      ('auth_lockouts',    'SELECT, INSERT, UPDATE, DELETE')) AS v(t, prv)
  LOOP
    EXECUTE format('GRANT %s ON TABLE %I TO transcendence_system', prv, t);
    IF prv LIKE '%INSERT%' THEN
      FOR c IN SELECT a.attname FROM pg_attribute a
                WHERE a.attrelid = format('public.%I', t)::regclass
                  AND a.attnum > 0 AND NOT a.attisdropped LOOP
        sq := pg_get_serial_sequence(format('public.%I', t), c.attname);
        IF sq IS NOT NULL THEN
          EXECUTE format('GRANT USAGE ON SEQUENCE %s TO transcendence_system',
                         sq);
        END IF;
      END LOOP;
    END IF;
  END LOOP;
END $$;

-- ------------------------- DEFAULT PRIVILEGES lintas creator (B12.3: H2)
-- ALTER DEFAULT PRIVILEGES tanpa FOR ROLE hanya mengubah default milik
-- role yang menjalankannya (dan hanya level yang disebut). Default yang
-- dibuat creator LAIN (role migrasi lain, owner tabel lain, level global
-- tanpa IN SCHEMA) tetap berlaku. Karena itu SETIAP entri pg_default_acl
-- (creator & level apa pun) diperiksa dan grant kepada:
--   * transcendence_app / transcendence_system (semua jenis objek), dan
--   * PUBLIC pada TABLES/SEQUENCES (data; PUBLIC termasuk kedua role)
-- dicabut per creator dengan FOR ROLE. Role migrasi yang tidak berhak
-- (bukan anggota creator) -> migrasi BERHENTI eksplisit. Keadaan sebelum
-- 008 disimpan untuk rollback di transcendence_meta (owner-only).
CREATE SCHEMA IF NOT EXISTS transcendence_meta;
REVOKE ALL ON SCHEMA transcendence_meta FROM PUBLIC;
CREATE TABLE IF NOT EXISTS transcendence_meta.default_acl_pre_008 (
  creator  text      NOT NULL,
  nspname  text,                 -- NULL = level global (tanpa IN SCHEMA)
  objtype  "char"    NOT NULL,
  acl      aclitem[] NOT NULL
);
REVOKE ALL ON transcendence_meta.default_acl_pre_008 FROM PUBLIC;
DO $$
DECLARE
  d     record;
  g     record;
  kw    text;
  stmt  text;
  bad   text;
  app   oid := 'transcendence_app'::regrole;
  sys   oid := 'transcendence_system'::regrole;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM transcendence_meta.default_acl_pre_008) THEN
    INSERT INTO transcendence_meta.default_acl_pre_008
      SELECT pg_get_userbyid(a.defaclrole), n.nspname, a.defaclobjtype,
             a.defaclacl
        FROM pg_default_acl a
        LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace;
  END IF;
  FOR d IN SELECT a.oid, a.defaclrole, a.defaclnamespace, a.defaclobjtype,
                  n.nspname
             FROM pg_default_acl a
             LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace LOOP
    kw := CASE d.defaclobjtype WHEN 'r' THEN 'TABLES'
                               WHEN 'S' THEN 'SEQUENCES'
                               WHEN 'f' THEN 'FUNCTIONS'
                               WHEN 'T' THEN 'TYPES'
                               WHEN 'n' THEN 'SCHEMAS' END;
    FOR g IN SELECT DISTINCT x.grantee
               FROM pg_default_acl a2, aclexplode(a2.defaclacl) x
              WHERE a2.oid = d.oid
                AND (x.grantee IN (app, sys)
                     OR (x.grantee = 0 AND d.defaclobjtype IN ('r','S'))) LOOP
      stmt := format('ALTER DEFAULT PRIVILEGES FOR ROLE %I',
                     pg_get_userbyid(d.defaclrole))
           || CASE WHEN d.defaclnamespace = 0 THEN ''
                   ELSE format(' IN SCHEMA %I', d.nspname) END
           || format(' REVOKE ALL ON %s FROM %s', kw,
                     CASE WHEN g.grantee = 0 THEN 'PUBLIC'
                          ELSE quote_ident(pg_get_userbyid(g.grantee)) END);
      BEGIN
        EXECUTE stmt;
      EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION 'B12.3: default privileges creator % memberi hak '
          'otomatis kepada % dan role migrasi % tidak berhak mencabutnya '
          '(%) — jalankan migrasi sebagai anggota creator/superuser',
          pg_get_userbyid(d.defaclrole),
          CASE WHEN g.grantee = 0 THEN 'PUBLIC'
               ELSE pg_get_userbyid(g.grantee) END, current_user, SQLERRM;
      END;
    END LOOP;
  END LOOP;
  -- verifikasi akhir: TIDAK ADA default (creator mana pun) yang memberi
  -- hak otomatis kepada kedua role, atau PUBLIC pada tabel/sequence.
  SELECT string_agg(format('%s/%s/%s', pg_get_userbyid(a.defaclrole),
                           COALESCE(n.nspname, '(global)'),
                           a.defaclobjtype), ', ')
    INTO bad
    FROM pg_default_acl a
    LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace
   WHERE EXISTS (SELECT 1 FROM aclexplode(a.defaclacl) x
                  WHERE x.grantee IN (app, sys)
                     OR (x.grantee = 0 AND a.defaclobjtype IN ('r','S')));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'B12.3: default privileges masih memberi hak otomatis: %',
      bad;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION public.app_customer_allows(row_customer bigint)
RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT row_customer IS NOT NULL
     AND NULLIF(current_setting('app.customer_id', true), '') IS NOT NULL
     AND row_customer =
         NULLIF(current_setting('app.customer_id', true), '')::bigint
$$;
CREATE OR REPLACE FUNCTION public.app_system_allows(row_customer bigint)
RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN NULLIF(current_setting('app.customer_id', true), '') IS NOT NULL
      THEN row_customer IS NOT NULL AND row_customer =
           NULLIF(current_setting('app.customer_id', true), '')::bigint
    ELSE true
  END
$$;
REVOKE ALL ON FUNCTION public.app_customer_allows(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.app_system_allows(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.app_customer_allows(bigint)
  TO transcendence_app, transcendence_system;
GRANT EXECUTE ON FUNCTION public.app_system_allows(bigint)
  TO transcendence_app, transcendence_system;

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
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I',
                   t || '_tenant_isolation', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_app', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_system', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL TO transcendence_app'
      ' USING (public.app_customer_allows(%I))'
      ' WITH CHECK (public.app_customer_allows(%I))',
      t || '_app', t, col, col);
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL TO transcendence_system'
      ' USING (public.app_system_allows(%I))'
      ' WITH CHECK (public.app_system_allows(%I))',
      t || '_system', t, col, col);
  END LOOP;
END $$;

-- sync_logs: customer diturunkan dari akun (account_id bisa NULL setelah
-- akun dihapus -> hanya role sistem tanpa customer context).
ALTER TABLE sync_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sync_logs_app ON sync_logs;
DROP POLICY IF EXISTS sync_logs_system ON sync_logs;
CREATE POLICY sync_logs_app ON sync_logs FOR ALL TO transcendence_app
  USING (public.app_customer_allows(
           (SELECT a.customer_id FROM mt5_accounts a
             WHERE a.id = sync_logs.account_id)))
  WITH CHECK (public.app_customer_allows(
           (SELECT a.customer_id FROM mt5_accounts a
             WHERE a.id = sync_logs.account_id)));
CREATE POLICY sync_logs_system ON sync_logs FOR ALL TO transcendence_system
  USING (public.app_system_allows(
           (SELECT a.customer_id FROM mt5_accounts a
             WHERE a.id = sync_logs.account_id))
         OR (sync_logs.account_id IS NULL AND
             NULLIF(current_setting('app.customer_id', true), '') IS NULL))
  WITH CHECK (public.app_system_allows(
           (SELECT a.customer_id FROM mt5_accounts a
             WHERE a.id = sync_logs.account_id))
         OR (sync_logs.account_id IS NULL AND
             NULLIF(current_setting('app.customer_id', true), '') IS NULL));

-- Fungsi 007 yang membaca app.scope dihapus: tidak ada lagi jalur yang
-- menerima GUC sebagai bukti scope sistem.
DROP FUNCTION IF EXISTS public.app_rls_allows(bigint);

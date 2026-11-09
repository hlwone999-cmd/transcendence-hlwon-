-- 002: role aplikasi non-owner + grant + penguatan RLS (audit #2).
--
-- TUJUAN: koneksi produksi memakai role NON-OWNER (transcendence_app)
-- yang tunduk pada Row-Level Security. Policy 001 membaca
-- current_setting('app.customer_id', true); context dipasang backend
-- per transaksi via set_config('app.customer_id', <id>, true)
-- (lihat db.PostgresConn.set_customer_context) — SET LOCAL semantics:
-- otomatis reset di akhir transaksi, tak bisa bocor antar request.
--
-- RLS adalah DEFENSE-IN-DEPTH (brief §5) — BUKAN pengganti filter
-- customer_id pada query backend. Owner database TETAP bypass RLS
-- (dipakai hanya untuk migrasi/administrasi).

-- ------------------------------------------------- role aplikasi
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transcendence_app') THEN
    CREATE ROLE transcendence_app
      LOGIN
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END $$;

-- ------------------------------------------------------- hak akses
-- Role aplikasi hanya boleh DML data aplikasi; TIDAK boleh DDL,
-- TIDAK boleh membaca/mengubah objek di luar skema public aplikasi.
GRANT USAGE ON SCHEMA public TO transcendence_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO transcendence_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO transcendence_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO transcendence_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO transcendence_app;

-- ------------------------------------------------ penguatan policy
-- Pastikan RLS aktif pada seluruh tabel ber-customer (idempoten; tabel
-- dari 001 sudah ENABLE, ini menjaga bila tabel ditambahkan lain waktu).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['mt5_deals','mt5_orders','mt5_trades',
                           'mt5_symbols','mt5_sync_state','mt5_credentials']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    -- Policy default-deny per tabel (idempoten: buat bila belum ada).
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public' AND tablename = t
        AND policyname = t || '_customer_isolation'
    ) THEN
      EXECUTE format(
        'CREATE POLICY %I ON %I USING (account_id IN ('
        ' SELECT id FROM mt5_accounts WHERE customer_id ='
        ' NULLIF(current_setting(''app.customer_id'', true), '''')::bigint))',
        t || '_customer_isolation', t);
    END IF;
  END LOOP;
END $$;

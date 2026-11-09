-- 003: rate-limit & lockout PERSISTEN (lintas worker & restart) +
--      status akun MT5 (riwayat jurnal dipertahankan setelah unbind).
--
-- Remediasi security review 2026-11-08 (T2/S3, keputusan produk #2/#4):
-- - auth_throttle : penghitung jendela tetap per (kunci, jendela) —
--                   kunci = SHA-256 dari aksi+IP/identitas (tanpa PII
--                   mentah). Dipakai dgn UPSERT atomik (... RETURNING).
-- - auth_lockouts : lockout kegagalan per kunci (SHA-256).
-- - mt5_accounts.state : 'active' | 'credentials_rejected' | 'unbound'
--                   | 'pending'. Unbind TIDAK lagi menghapus baris akun
--                   (riwayat deal/trade tetap), kredensial dihapus.
-- Tabel throttle TIDAK ber-RLS: tidak berisi data customer (hanya hash).

CREATE TABLE IF NOT EXISTS auth_throttle (
  key_hash  TEXT    NOT NULL,
  window_s  INTEGER NOT NULL,
  bucket    BIGINT  NOT NULL,
  hits      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key_hash, window_s, bucket)
);

CREATE TABLE IF NOT EXISTS auth_lockouts (
  key_hash      TEXT PRIMARY KEY,
  failures      INTEGER NOT NULL DEFAULT 0,
  window_start  DOUBLE PRECISION NOT NULL,
  locked_until  DOUBLE PRECISION
);

ALTER TABLE mt5_accounts ADD COLUMN IF NOT EXISTS state TEXT NOT NULL
  DEFAULT 'active';
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'mt5_accounts_state_check') THEN
    ALTER TABLE mt5_accounts ADD CONSTRAINT mt5_accounts_state_check
      CHECK (state IN ('pending','active','credentials_rejected','unbound'));
  END IF;
END $$;
ALTER TABLE mt5_accounts ADD COLUMN IF NOT EXISTS unbound_at TEXT;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transcendence_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON auth_throttle, auth_lockouts
      TO transcendence_app;
  END IF;
END $$;

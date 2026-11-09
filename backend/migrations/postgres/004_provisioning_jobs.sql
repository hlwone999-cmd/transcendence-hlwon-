-- 004: auto-provisioning terminal MT5 (keputusan produk #3).
--
-- mt5_terminals:
--   provision_id : ID kepemilikan acak (uuid4 hex) milik permintaan yang
--                  MEMBUAT terminal. Helper privileged menulis penanda yang
--                  sama di direktori customer; penghapusan hanya bila
--                  penanda cocok (resource tanpa bukti kepemilikan TIDAK
--                  pernah dihapus otomatis).
--   lifecycle    : provisioning -> starting -> verifying -> connected |
--                  failed ; stopping -> stopped (unbind).
-- mt5_connect_jobs: job aktivasi (self-link / reaktivasi / bind) yang
--   dijalankan worker latar; token sesi HANYA diterbitkan sekali, setelah
--   job 'connected' (verifikasi live fail-closed lolos). Rahasia polling
--   disimpan sebagai SHA-256. Tanpa FK ke customers agar status gagal
--   tetap terbaca setelah cleanup menghapus customer baru.

ALTER TABLE mt5_terminals ADD COLUMN IF NOT EXISTS provision_id TEXT;
ALTER TABLE mt5_terminals ADD COLUMN IF NOT EXISTS lifecycle TEXT NOT NULL
  DEFAULT 'connected';
ALTER TABLE mt5_terminals ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE mt5_terminals ADD COLUMN IF NOT EXISTS updated_at TEXT;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'mt5_terminals_lifecycle_check') THEN
    ALTER TABLE mt5_terminals ADD CONSTRAINT mt5_terminals_lifecycle_check
      CHECK (lifecycle IN ('provisioning','starting','verifying','connected',
                           'failed','stopping','stopped'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS mt5_connect_jobs (
  id                TEXT PRIMARY KEY,
  secret_hash       TEXT    NOT NULL,
  kind              TEXT    NOT NULL
                    CHECK (kind IN ('self_link','reactivate','bind')),
  state             TEXT    NOT NULL
                    CHECK (state IN ('pending','provisioning','starting',
                                     'verifying','connected','failed')),
  error_class       TEXT,
  customer_id       BIGINT,
  user_id           BIGINT,
  account_id        BIGINT,
  terminal_id       BIGINT,
  provision_id      TEXT,
  created_customer  INTEGER NOT NULL DEFAULT 0,
  created_account   INTEGER NOT NULL DEFAULT 0,
  created_terminal  INTEGER NOT NULL DEFAULT 0,
  prev_account_state TEXT,
  require_real      INTEGER NOT NULL DEFAULT 0,
  lock_ip_hash      TEXT,
  lock_acct_hash    TEXT,
  deadline_at       DOUBLE PRECISION NOT NULL,
  token_issued_at   TEXT,
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connect_jobs_state
  ON mt5_connect_jobs(state, deadline_at);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transcendence_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON mt5_connect_jobs
      TO transcendence_app;
  END IF;
END $$;

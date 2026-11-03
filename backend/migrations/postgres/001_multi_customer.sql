-- 001: skema multi-customer PostgreSQL (Tahap 3 brief TRANSCENDENCE MVP).
-- Kapasitas MVP: maksimal 4 customer aktif; 1 akun MT5 per customer
-- (ditegakkan UNIQUE(customer_id)); 1 terminal per customer.
--
-- Jenis waktu: kolom waktu transaksi memakai TEXT ISO-UTC
-- ("YYYY-MM-DDTHH:MM:SSZ") persis seperti skema SQLite lama —
-- pembandingan leksikografis identik sehingga SEMANTIK filter tanggal
-- yang sudah disetujui pengguna tidak berubah sedikit pun (regresi 0).
-- Kolom metadata (created_at) memakai default UTC teks yang sama.

-- ============================================================ identitas
CREATE TABLE IF NOT EXISTS customers (
  id            BIGSERIAL PRIMARY KEY,
  code          TEXT    NOT NULL UNIQUE,          -- C001..C004 (MVP)
  display_name  TEXT,
  status        TEXT    NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','disabled')),
  max_terminals INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT    NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);

CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  customer_id   BIGINT  NOT NULL UNIQUE REFERENCES customers(id)
                        ON DELETE CASCADE,        -- MVP: 1 user/customer
  email         TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,                 -- werkzeug pbkdf2:sha256
  created_at    TEXT    NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  last_login_at TEXT
);

-- Sesi token: server hanya menyimpan SHA-256 token (token asli hanya
-- dikirim sekali ke klien saat login). Masa berlaku + revocation.
CREATE TABLE IF NOT EXISTS auth_tokens (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  customer_id  BIGINT  NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token_hash   TEXT    NOT NULL UNIQUE,           -- hex sha-256, bukan token
  created_at   TEXT    NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  expires_at   TEXT    NOT NULL,
  revoked_at   TEXT,
  last_used_at TEXT,
  created_ip   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_customer ON auth_tokens(customer_id);

-- ================================================================ MT5
-- 1 akun per customer: UNIQUE(customer_id) WAJIB (brief §5).
-- 1 akun MT5 hanya boleh dimiliki 1 customer: UNIQUE(login, server).
CREATE TABLE IF NOT EXISTS mt5_accounts (
  id            BIGSERIAL PRIMARY KEY,
  customer_id   BIGINT  NOT NULL UNIQUE REFERENCES customers(id)
                        ON DELETE CASCADE,
  login         BIGINT  NOT NULL,
  server        TEXT    NOT NULL,                 -- broker/server per customer
  company       TEXT,
  currency      TEXT    DEFAULT 'USD',
  leverage      INTEGER,
  balance       DOUBLE PRECISION,
  equity        DOUBLE PRECISION,
  margin_free   DOUBLE PRECISION,
  first_sync_at TEXT,
  last_sync_at  TEXT,
  UNIQUE(login, server)
);

-- Kredensial MT5 TERENKRIPSI (Fernet / authenticated encryption).
-- Kunci TIDAK PERNAH di database/repo — dari env MT5_CRED_KEY (brief §5).
CREATE TABLE IF NOT EXISTS mt5_credentials (
  account_id   BIGINT PRIMARY KEY REFERENCES mt5_accounts(id)
               ON DELETE CASCADE,
  enc_password TEXT    NOT NULL,
  key_version  INTEGER NOT NULL DEFAULT 1,
  updated_at   TEXT    NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);

-- Terminal MT5 terisolasi per customer (Wine prefix + port RPyC sendiri).
CREATE TABLE IF NOT EXISTS mt5_terminals (
  id             BIGSERIAL PRIMARY KEY,
  customer_id    BIGINT  NOT NULL UNIQUE REFERENCES customers(id)
                         ON DELETE CASCADE,
  rpyc_port      INTEGER NOT NULL UNIQUE,        -- 18812..18815, localhost
  wine_prefix    TEXT    NOT NULL,
  terminal_dir   TEXT    NOT NULL,
  data_dir       TEXT    NOT NULL,
  service_name   TEXT    NOT NULL,                -- unit systemd
  status         TEXT    NOT NULL DEFAULT 'provisioning'
                 CHECK (status IN ('provisioning','ready','error','disabled')),
  last_health_at TEXT,
  created_at     TEXT    NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);

-- ------------------------------------------------------------- data MT5
CREATE TABLE IF NOT EXISTS mt5_deals (
  id           BIGSERIAL PRIMARY KEY,
  account_id   BIGINT  NOT NULL REFERENCES mt5_accounts(id)
               ON DELETE CASCADE,
  ticket       BIGINT  NOT NULL,
  order_ticket BIGINT,
  position_id  BIGINT,
  symbol       TEXT,
  deal_type    INTEGER NOT NULL,
  type_name    TEXT,
  entry        INTEGER NOT NULL,
  entry_name   TEXT,
  volume       DOUBLE PRECISION,
  price        DOUBLE PRECISION,
  profit       DOUBLE PRECISION DEFAULT 0,
  commission   DOUBLE PRECISION DEFAULT 0,
  swap         DOUBLE PRECISION DEFAULT 0,
  fee          DOUBLE PRECISION DEFAULT 0,
  comment      TEXT DEFAULT '',
  magic        BIGINT DEFAULT 0,
  reason       INTEGER,
  time_utc     TEXT NOT NULL,
  synced_at    TEXT NOT NULL,
  UNIQUE(account_id, ticket)
);
CREATE INDEX IF NOT EXISTS idx_deals_account_pos  ON mt5_deals(account_id, position_id);
CREATE INDEX IF NOT EXISTS idx_deals_account_time ON mt5_deals(account_id, time_utc);

CREATE TABLE IF NOT EXISTS mt5_orders (
  id             BIGSERIAL PRIMARY KEY,
  account_id     BIGINT  NOT NULL REFERENCES mt5_accounts(id)
                 ON DELETE CASCADE,
  ticket         BIGINT  NOT NULL,
  position_id    BIGINT,
  symbol         TEXT,
  order_type     INTEGER,
  type_name      TEXT,
  volume_initial DOUBLE PRECISION,
  volume_current DOUBLE PRECISION,
  price_open     DOUBLE PRECISION,
  price_sl       DOUBLE PRECISION,
  price_tp       DOUBLE PRECISION,
  state          INTEGER,
  state_name     TEXT,
  time_setup_utc TEXT,
  time_done_utc  TEXT,
  comment        TEXT DEFAULT '',
  magic          BIGINT DEFAULT 0,
  synced_at      TEXT NOT NULL,
  UNIQUE(account_id, ticket)
);
CREATE INDEX IF NOT EXISTS idx_orders_account_pos ON mt5_orders(account_id, position_id);

-- Posisi hasil rekonstruksi (nama tabel wajib brief: mt5_trades).
CREATE TABLE IF NOT EXISTS mt5_trades (
  id             BIGSERIAL PRIMARY KEY,
  account_id     BIGINT  NOT NULL REFERENCES mt5_accounts(id)
                 ON DELETE CASCADE,
  position_id    BIGINT  NOT NULL,
  symbol         TEXT    NOT NULL,
  direction      TEXT,
  status         TEXT    NOT NULL DEFAULT 'open',
  volume_in      DOUBLE PRECISION,
  volume_out     DOUBLE PRECISION,
  entry_price    DOUBLE PRECISION,
  exit_price     DOUBLE PRECISION,
  open_time_utc  TEXT,
  close_time_utc TEXT,
  profit         DOUBLE PRECISION DEFAULT 0,
  commission     DOUBLE PRECISION DEFAULT 0,
  swap           DOUBLE PRECISION DEFAULT 0,
  fee            DOUBLE PRECISION DEFAULT 0,
  net_profit     DOUBLE PRECISION DEFAULT 0,
  sl_initial     DOUBLE PRECISION,
  tp_initial     DOUBLE PRECISION,
  sl_current     DOUBLE PRECISION,
  tp_current     DOUBLE PRECISION,
  price_current  DOUBLE PRECISION,
  exit_reason    TEXT,
  magic          BIGINT,
  comment        TEXT,
  entry_deals    TEXT,
  exit_deals     TEXT,
  updated_at     TEXT    NOT NULL,
  UNIQUE(account_id, position_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_account_status ON mt5_trades(account_id, status, close_time_utc);
CREATE INDEX IF NOT EXISTS idx_trades_symbol         ON mt5_trades(account_id, symbol);

CREATE TABLE IF NOT EXISTS mt5_symbols (
  account_id       BIGINT  NOT NULL REFERENCES mt5_accounts(id)
                   ON DELETE CASCADE,
  symbol           TEXT    NOT NULL,
  point            DOUBLE PRECISION,
  digits           INTEGER,
  trade_tick_size  DOUBLE PRECISION,
  trade_tick_value DOUBLE PRECISION,
  pip_size         DOUBLE PRECISION,
  updated_at       TEXT    NOT NULL,
  PRIMARY KEY(account_id, symbol)
);

CREATE TABLE IF NOT EXISTS mt5_sync_state (
  account_id             BIGINT PRIMARY KEY REFERENCES mt5_accounts(id)
                         ON DELETE CASCADE,
  initial_done           INTEGER DEFAULT 0,
  last_deal_time_utc     TEXT,
  last_sync_started_at   TEXT,
  last_sync_finished_at  TEXT,
  last_sync_status       TEXT
);

CREATE TABLE IF NOT EXISTS sync_logs (
  id              BIGSERIAL PRIMARY KEY,
  account_id      BIGINT REFERENCES mt5_accounts(id) ON DELETE SET NULL,
  mode            TEXT,
  started_at      TEXT,
  finished_at     TEXT,
  status          TEXT,
  deals_upserted  INTEGER,
  orders_upserted INTEGER,
  trades_updated  INTEGER,
  open_positions  INTEGER,
  range_from_utc  TEXT,
  range_to_utc    TEXT,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_logs_account ON sync_logs(account_id, started_at);

-- ============================================================== audit
CREATE TABLE IF NOT EXISTS audit_logs (
  id          BIGSERIAL PRIMARY KEY,
  customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
  user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
  action      TEXT NOT NULL,                     -- login, bind, sync, ...
  entity      TEXT,                              -- mis. "mt5_account:3"
  ip          TEXT,
  user_agent  TEXT,
  detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TEXT    NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS"Z"')
);
CREATE INDEX IF NOT EXISTS idx_audit_customer ON audit_logs(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action   ON audit_logs(action);

-- =============================================== Row-Level Security (RLS)
-- DEFENSE-IN-DEPTH (brief §5): bukan pengganti authorization backend.
-- App role non-owner wajib SET app.customer_id per koneksi/transaction;
-- policy membatasi baris hanya milik customer itu. Owner database
-- (migrasi/tool) bypass RLS — didokumentasikan di docs keamanan.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['mt5_deals','mt5_orders','mt5_trades',
                           'mt5_symbols','mt5_sync_state','mt5_credentials']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I USING (account_id IN ('
      ' SELECT id FROM mt5_accounts WHERE customer_id ='
      ' NULLIF(current_setting(''app.customer_id'', true), '''')::bigint))',
      t || '_customer_isolation', t);
  END LOOP;
END $$;

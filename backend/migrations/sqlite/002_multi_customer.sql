-- 002: skema multi-customer utk SQLite (paritas dgn PG 001_multi_customer).
-- Dipakai dev/test lokal tanpa PostgreSQL. Produksi = PostgreSQL.

CREATE TABLE IF NOT EXISTS customers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  code          TEXT    NOT NULL UNIQUE,
  display_name  TEXT,
  status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  max_terminals INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id   INTEGER NOT NULL UNIQUE REFERENCES customers(id) ON DELETE CASCADE,
  email         TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  customer_id  INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token_hash   TEXT    NOT NULL UNIQUE,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  expires_at   TEXT    NOT NULL,
  revoked_at   TEXT,
  last_used_at TEXT,
  created_ip   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_customer ON auth_tokens(customer_id);

-- mt5_accounts selalu sudah ada dari migrasi 001 (tanpa customer_id).
-- SQLite tidak mendukung ADD CONSTRAINT -> pakai ADD COLUMN + unique index.
-- NULL customer_id = baris legacy; unique index SQLite mengizinkan banyak
-- NULL, sementara baris baru selalu diisi -> batasan 1 akun/customer kuat.
ALTER TABLE mt5_accounts ADD COLUMN customer_id INTEGER
  REFERENCES customers(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_customer_uniq
  ON mt5_accounts(customer_id);


CREATE TABLE IF NOT EXISTS mt5_credentials (
  account_id   INTEGER PRIMARY KEY REFERENCES mt5_accounts(id) ON DELETE CASCADE,
  enc_password TEXT    NOT NULL,
  key_version  INTEGER NOT NULL DEFAULT 1,
  updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mt5_terminals (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id    INTEGER NOT NULL UNIQUE REFERENCES customers(id) ON DELETE CASCADE,
  rpyc_port      INTEGER NOT NULL UNIQUE,
  wine_prefix    TEXT    NOT NULL,
  terminal_dir   TEXT    NOT NULL,
  data_dir       TEXT    NOT NULL,
  service_name   TEXT    NOT NULL,
  status         TEXT    NOT NULL DEFAULT 'provisioning'
                 CHECK (status IN ('provisioning','ready','error','disabled')),
  last_health_at TEXT,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  action      TEXT NOT NULL,
  entity      TEXT,
  ip          TEXT,
  user_agent  TEXT,
  detail      TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_customer ON audit_logs(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action   ON audit_logs(action);

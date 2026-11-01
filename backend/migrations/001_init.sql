-- 001: skema awal Jurnal MT5 (SQLite)
-- Unique key identitas akun + server + ticket agar sync berulang tidak duplikat.

CREATE TABLE IF NOT EXISTS mt5_accounts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  login         INTEGER NOT NULL,
  server        TEXT    NOT NULL,
  company       TEXT,
  currency      TEXT    DEFAULT 'USD',
  leverage      INTEGER,
  balance       REAL,
  equity        REAL,
  margin_free   REAL,
  first_sync_at TEXT,
  last_sync_at  TEXT,
  UNIQUE(login, server)
);

-- Raw deals apa adanya (termasuk deposit/withdrawal/bonus — data historis
-- TIDAK PERNAH dibuang). Trades dihasilkan dari sini lewat rekonstruksi.
CREATE TABLE IF NOT EXISTS mt5_deals (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   INTEGER NOT NULL REFERENCES mt5_accounts(id),
  ticket       INTEGER NOT NULL,
  order_ticket INTEGER,
  position_id  INTEGER,
  symbol       TEXT,
  deal_type    INTEGER NOT NULL,
  type_name    TEXT,
  entry        INTEGER NOT NULL,
  entry_name   TEXT,
  volume       REAL,
  price        REAL,
  profit       REAL DEFAULT 0,
  commission   REAL DEFAULT 0,
  swap         REAL DEFAULT 0,
  fee          REAL DEFAULT 0,
  comment      TEXT DEFAULT '',
  magic        INTEGER DEFAULT 0,
  reason       INTEGER,
  time_utc     TEXT NOT NULL,
  synced_at    TEXT NOT NULL,
  UNIQUE(account_id, ticket)
);
CREATE INDEX IF NOT EXISTS idx_deals_account_pos ON mt5_deals(account_id, position_id);
CREATE INDEX IF NOT EXISTS idx_deals_account_time ON mt5_deals(account_id, time_utc);

-- Raw history orders (dipakai untuk SL/TP awal posisi).
CREATE TABLE IF NOT EXISTS mt5_orders (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id     INTEGER NOT NULL REFERENCES mt5_accounts(id),
  ticket         INTEGER NOT NULL,
  position_id    INTEGER,
  symbol         TEXT,
  order_type     INTEGER,
  type_name      TEXT,
  volume_initial REAL,
  volume_current REAL,
  price_open     REAL,
  price_sl       REAL,
  price_tp       REAL,
  state          INTEGER,
  state_name     TEXT,
  time_setup_utc TEXT,
  time_done_utc  TEXT,
  comment        TEXT DEFAULT '',
  magic          INTEGER DEFAULT 0,
  synced_at      TEXT NOT NULL,
  UNIQUE(account_id, ticket)
);
CREATE INDEX IF NOT EXISTS idx_orders_account_pos ON mt5_orders(account_id, position_id);

-- Posisi hasil rekonstruksi dari deals (bukan 1 deal = 1 trade).
CREATE TABLE IF NOT EXISTS trades (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    INTEGER NOT NULL REFERENCES mt5_accounts(id),
  position_id   INTEGER NOT NULL,
  symbol        TEXT    NOT NULL,
  direction     TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  volume_in     REAL,
  volume_out    REAL,
  entry_price   REAL,
  exit_price    REAL,
  open_time_utc TEXT,
  close_time_utc TEXT,
  profit        REAL DEFAULT 0,
  commission    REAL DEFAULT 0,
  swap          REAL DEFAULT 0,
  fee           REAL DEFAULT 0,
  net_profit    REAL DEFAULT 0,
  sl_initial    REAL,
  tp_initial    REAL,
  sl_current    REAL,
  tp_current    REAL,
  price_current REAL,
  exit_reason   TEXT,
  magic         INTEGER,
  comment       TEXT,
  entry_deals   TEXT,
  exit_deals    TEXT,
  updated_at    TEXT NOT NULL,
  UNIQUE(account_id, position_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_account_status ON trades(account_id, status, close_time_utc);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(account_id, symbol);

-- Cache symbol_info (point/digits/tick) untuk hitung pip — per simbol.
CREATE TABLE IF NOT EXISTS mt5_symbols (
  account_id      INTEGER NOT NULL REFERENCES mt5_accounts(id),
  symbol          TEXT NOT NULL,
  point           REAL,
  digits          INTEGER,
  trade_tick_size REAL,
  trade_tick_value REAL,
  pip_size        REAL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY(account_id, symbol)
);

CREATE TABLE IF NOT EXISTS sync_state (
  account_id             INTEGER PRIMARY KEY REFERENCES mt5_accounts(id),
  initial_done           INTEGER DEFAULT 0,
  last_deal_time_utc     TEXT,
  last_sync_started_at   TEXT,
  last_sync_finished_at  TEXT,
  last_sync_status       TEXT
);

CREATE TABLE IF NOT EXISTS sync_logs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id      INTEGER,
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

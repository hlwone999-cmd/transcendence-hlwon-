-- 004: auto-provisioning terminal MT5 (paritas PG 004).
ALTER TABLE mt5_terminals ADD COLUMN provision_id TEXT;
ALTER TABLE mt5_terminals ADD COLUMN lifecycle TEXT NOT NULL
  DEFAULT 'connected'
  CHECK (lifecycle IN ('provisioning','starting','verifying','connected',
                       'failed','stopping','stopped'));
ALTER TABLE mt5_terminals ADD COLUMN last_error TEXT;
ALTER TABLE mt5_terminals ADD COLUMN updated_at TEXT;

CREATE TABLE IF NOT EXISTS mt5_connect_jobs (
  id                TEXT PRIMARY KEY,
  secret_hash       TEXT    NOT NULL,
  kind              TEXT    NOT NULL
                    CHECK (kind IN ('self_link','reactivate','bind')),
  state             TEXT    NOT NULL
                    CHECK (state IN ('pending','provisioning','starting',
                                     'verifying','connected','failed')),
  error_class       TEXT,
  customer_id       INTEGER,
  user_id           INTEGER,
  account_id        INTEGER,
  terminal_id       INTEGER,
  provision_id      TEXT,
  created_customer  INTEGER NOT NULL DEFAULT 0,
  created_account   INTEGER NOT NULL DEFAULT 0,
  created_terminal  INTEGER NOT NULL DEFAULT 0,
  prev_account_state TEXT,
  require_real      INTEGER NOT NULL DEFAULT 0,
  lock_ip_hash      TEXT,
  lock_acct_hash    TEXT,
  deadline_at       REAL    NOT NULL,
  token_issued_at   TEXT,
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connect_jobs_state
  ON mt5_connect_jobs(state, deadline_at);

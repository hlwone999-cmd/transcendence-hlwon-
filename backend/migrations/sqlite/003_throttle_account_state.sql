-- 003: rate-limit & lockout persisten + status akun MT5 (paritas PG 003).
CREATE TABLE IF NOT EXISTS auth_throttle (
  key_hash  TEXT    NOT NULL,
  window_s  INTEGER NOT NULL,
  bucket    INTEGER NOT NULL,
  hits      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key_hash, window_s, bucket)
);

CREATE TABLE IF NOT EXISTS auth_lockouts (
  key_hash      TEXT PRIMARY KEY,
  failures      INTEGER NOT NULL DEFAULT 0,
  window_start  REAL    NOT NULL,
  locked_until  REAL
);

ALTER TABLE mt5_accounts ADD COLUMN state TEXT NOT NULL DEFAULT 'active'
  CHECK (state IN ('pending','active','credentials_rejected','unbound'));
ALTER TABLE mt5_accounts ADD COLUMN unbound_at TEXT;

-- 005: mode RPC per terminal (paritas PG 005).
ALTER TABLE mt5_terminals ADD COLUMN rpc_mode TEXT NOT NULL DEFAULT 'classic'
  CHECK (rpc_mode IN ('classic','ro'));

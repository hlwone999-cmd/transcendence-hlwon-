-- 005: mode RPC per terminal — migrasi bertahap dari RPyC CLASSIC ke
-- layanan RPC READ-ONLY bertoken (docs/RPYC-SECURITY.md §6).
--   classic : mt5server (RPyC classic, kompatibilitas — terminal lama)
--   ro      : mt5client/ro_service.py (whitelist + token HMAC)
-- Terminal yang sudah ada tetap 'classic' (tidak ada perubahan perilaku);
-- terminal baru memakai MT5_RPC_MODE_DEFAULT.
ALTER TABLE mt5_terminals ADD COLUMN IF NOT EXISTS rpc_mode TEXT NOT NULL
  DEFAULT 'classic';
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'mt5_terminals_rpc_mode_check') THEN
    ALTER TABLE mt5_terminals ADD CONSTRAINT mt5_terminals_rpc_mode_check
      CHECK (rpc_mode IN ('classic','ro'));
  END IF;
END $$;

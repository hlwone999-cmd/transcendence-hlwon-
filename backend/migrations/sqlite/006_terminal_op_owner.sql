-- 006 (F2/T1): identitas operasi yang sedang MEMEGANG baris terminal.
--   NULL                : bebas (tidak ada job/penghapusan yang berjalan)
--   'job:<job_id>'      : dipakai job aktivasi (install/start/verify)
--   'remove:<uuid>'     : penghapusan sedang berjalan (klaim + lease)
--   'removal-pending'   : penghapusan tertunda/tidak pasti — TIDAK boleh
--                         dipakai job baru; hanya pemulihan/admin.
-- Semua transisi lewat UPDATE/DELETE bersyarat (lihat services/provisioning.py).
ALTER TABLE mt5_terminals ADD COLUMN op_owner TEXT;

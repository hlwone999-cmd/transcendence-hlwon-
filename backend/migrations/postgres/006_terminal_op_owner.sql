-- 006 (F2/T1): identitas operasi yang sedang MEMEGANG baris terminal
-- (paritas SQLite 006). NULL = bebas; 'job:<id>' = job aktivasi;
-- 'remove:<uuid>' = penghapusan berjalan (klaim + lease);
-- 'removal-pending' = penghapusan tertunda — tidak boleh dipakai job baru.
-- Kolom baru pada tabel yang sudah di-GRANT ke transcendence_app
-- (GRANT tingkat tabel mencakup kolom baru).
ALTER TABLE mt5_terminals ADD COLUMN IF NOT EXISTS op_owner TEXT;

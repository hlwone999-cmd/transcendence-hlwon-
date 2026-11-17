# Serah-terima — Transcendence MT5 Journal (lanjutan di sesi baru)

## Status proyek
- Repo: `hlwone999-cmd/transcendence-hlwon-`
- Branch kerja: `security/remediation-checkpoint-b2-b12`, HEAD `273c774cf63def01eb75af0514c9f6d504c39eb3` (Group 1 RPC Hardening, sudah di-push).
- Draft PR #6 (branch ini → `feat/jurnal-mt5-redesign`) belum di-merge.
- UI redesign ada di branch `ui/redesign-total`; backup UI lama ada di branch `UIUX-ORIGINAL-BACKUP`.
- **Group 2** (runtime & API integration): spesifikasi v7 selesai (`GROUP-2-IMPLEMENTATION-SPEC.md`), **belum diimplementasikan**.
- **Group 3** (worker migrasi classic → ro, snapshot, backup verify, admin socket): belum dimulai.

## Prioritas sekarang: EMERGENCY PRESERVATION VPS
- VPS `<VPS_IP>`, SSH port `<SSH_PORT>` (nilai asli disamarkan karena repo public). Masa aktif tinggal 1–2 minggu. Status backup belum diketahui.
- **Fase 1 (read-only):** jalankan `sudo bash emergency_preservation_audit.sh` (v3.1).
  - Default ringan: tanpa `du`, tanpa `COUNT` eksak, tanpa membaca isi dump.
  - Hasil hanya sah bila baris akhir `HASIL: AUDIT COMPLETE`.
- **Fase 2 (butuh persetujuan per langkah):**
  1. dump PostgreSQL + `pg_dumpall --globals-only` dari snapshot konsisten;
  2. arsip berkas penting: `.env` (berisi **MT5_CRED_KEY**, kritis), `/etc/transcendence/provisioner.json`, helper, template MT5, terminal, nginx/TLS, systemd, cron, sudoers, dan backup yang sudah ada;
  3. enkripsi dengan kunci publik pemilik;
  4. tarik arsip ke ≥ 2 lokasi di luar VPS, lalu cek `sha256sum -c`;
  5. **restore uji di host lain.** Baru setelah ini status backup = VERIFIED.
- **Fase 3:** setup VPS baru (rincian di `EMERGENCY-PRESERVATION-AUDIT.md` bagian G/H).
  - Terminal dibangun ulang dalam mode `ro`, **bukan** classic.
  - VPS lama tetap utuh sampai VPS baru terbukti jalan.

## Aturan kerja (tetap berlaku)
- Tidak ada restart/stop service, perubahan DB, migrasi, perubahan konfigurasi/firewall/DNS/Nginx tanpa persetujuan eksplisit.
- Jangan pernah menampilkan secret, password, token, private key, atau isi `.env`.
- MT5 READ-ONLY: tidak ada jalur trading atau order.
- Tidak ada commit/push/stash/checkout tanpa persetujuan.
- Jangan menganggap backup valid hanya karena berkasnya ada.
- Tandai yang belum terbukti sebagai NOT VERIFIED.

## Lampiran (sudah ada di repo, folder `docs/ops/`)
- `emergency_preservation_audit.sh` (v3.1, sha256 diawali `44d5434cec0504cb`)
- `EMERGENCY-PRESERVATION-AUDIT.md`
- `GROUP-2-IMPLEMENTATION-SPEC.md` (v7)

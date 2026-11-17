# Auto-provisioning terminal MT5 (keputusan produk #3)

Pelanggan memasukkan **nomor akun MT5 + server broker + password investor**;
sistem menyiapkan terminal secara otomatis. API **tidak** pernah memperoleh
hak root.

## Arsitektur

```
Flutter ──HTTPS──> nginx ──> API (user transcendence, non-root)
                               │  sudo -n (SATU perintah, argumen tervalidasi)
                               ▼
               /usr/local/sbin/transcendence-mt5-provision  (root-owned 0755)
                               │  systemctl enable/disable, berkas unit,
                               ▼  direktori per customer (template sendiri)
               transcendence-mt5-c00N.service (User=layanan, 127.0.0.1:port)
```

- **Koordinator** (`backend/services/provisioning.py`): job di tabel
  `mt5_connect_jobs`; lifecycle `pending → provisioning → starting →
  verifying → connected | failed`; terminal `provisioning/starting/
  verifying/connected/failed/stopping/stopped`.
- **Kepemilikan**: tiap terminal punya `provision_id` (uuid4 hex) di DB dan
  penanda `.provision_id` (0600) di direktori customer. Cleanup/unbind
  hanya menghapus bila keduanya cocok; tanpa bukti → terminal ditandai
  `failed`, slot tetap terpakai, admin menangani.
- **Token** hanya diterbitkan sekali setelah job `connected` (klaim atomik
  `token_issued_at`). Selama job berjalan: respons `202` + `job_id` +
  `job_secret` (disimpan sebagai SHA-256), polling
  `POST /api/v1/mt5/connect/status`.
- **Batas waktu** `MT5_PROVISION_TIMEOUT_SECONDS` (default 120): lewat →
  gagal + cleanup. Job terputus (proses mati/restart) dipulihkan saat
  startup / request berikutnya setelah deadline (klaim atomik — satu worker).
- **Unbind**: riwayat jurnal dipertahankan, kredensial dihapus, semua sesi
  dicabut, terminal dihentikan+dihapus dengan bukti kepemilikan → slot bebas.

## Pemasangan helper (VPS — BELUM dilakukan, perlu persetujuan)

```bash
sudo install -o root -g root -m 0755 \
  backend/provisioner/mt5_provision_helper.py \
  /usr/local/sbin/transcendence-mt5-provision
sudo install -d -o root -g root -m 0755 /etc/transcendence
sudo tee /etc/transcendence/provisioner.json >/dev/null <<'JSON'
{"base_dir": "/var/lib/transcendence/terminals",
 "template_dir": "/opt/transcendence/mt5-template",
 "unit_dir": "/etc/systemd/system",
 "service_user": "mt5term",
 "service_user_mode": "per_customer",
 "port_base": 18812, "max_slots": 4,
 "systemctl": "/usr/bin/systemctl", "wine_bin": "/usr/bin/wine"}
JSON
sudo chmod 0644 /etc/transcendence/provisioner.json   # root-owned, tidak writable
echo 'transcendence ALL=(root) NOPASSWD: /usr/local/sbin/transcendence-mt5-provision' \
  | sudo tee /etc/sudoers.d/transcendence-mt5 && sudo chmod 0440 /etc/sudoers.d/transcendence-mt5
sudo visudo -c
```

Catatan:
- `service_user_mode: "per_customer"` (disarankan): helper membuat user
  sistem `mt5-<kode>` + grup privat per customer (`useradd --system`),
  direktori `root:mt5-<kode> 0750`, isi `0700` — proses terminal satu
  customer tidak dapat membaca direktori/token customer lain; user dihapus
  saat `remove`. Mode `shared` (satu `service_user`) hanya untuk
  kompatibilitas. User API **berbeda** dari user terminal.
- `--rpc-mode ro` hanya diizinkan bila `ro_service: true` (lihat
  docs/RPYC-SECURITY.md §6).
- Jika konfigurasi memuat `rpc_secret`, ubah mode berkas menjadi `0600`
  (root saja) — helper menolak konfigurasi yang writable grup/lain.
- `.env` API: `TERMINAL_PROVISION_MODE=real`,
  `PROVISIONER_HELPER=/usr/local/sbin/transcendence-mt5-provision`.

## Validasi helper (ringkas)

Operasi tetap `install|start|stop|remove|status`; `--code ^C[0-9]{3,9}$`,
`--port` di jendela `[port_base, port_base+max_slots)`, `--provision-id
^[0-9a-f]{32}$`. Semua path/unit/user/binary dari berkas konfigurasi
root-only; subprocess argv list tanpa shell, env minimal; menolak port yang
dipakai unit customer lain, direktori tanpa/beda penanda, symlink. `status`
mensyaratkan listener `127.0.0.1:port` dimiliki proses di cgroup unit
customer tsb. Diuji di `backend/tests/test_provision_helper.py`.

## Batasan yang belum diverifikasi

- Helper TELAH diuji dengan sudo nyata, user/izin nyata, proses/cgroup
  nyata di container terisolasi (22 test) — TETAPI systemd (unit
  `Restart=`, `ProtectSystem`, `IPAddressDeny`) dan Wine/MT5 diganti shim:
  NOT VERIFIED.
- Waktu start terminal MT5 di Wine nyata (bisa > 60 s) — sesuaikan
  `MT5_PROVISION_TIMEOUT_SECONDS`.
- Pemeriksaan listener via `/proc/net/tcp` hanya IPv4 loopback.

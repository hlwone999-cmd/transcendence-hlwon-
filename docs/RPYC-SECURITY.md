# Asesmen keamanan RPyC & isolasi MT5 (Tahap 4)

Status: **asesmen + prototipe teruji lokal**. Penggantian di produksi
memerlukan langkah instalasi di VPS (Python Windows + paket MetaTrader5 di
dalam Wine) yang **belum** dilakukan dan **belum** diverifikasi pada Wine
nyata.

## 1. Temuan: RPyC classic = eksekusi kode arbitrer

`mt5server.exe` / `python -m mt5linux` menjalankan **server RPyC classic**
(`SlaveService`). Setiap proses yang dapat membuka `127.0.0.1:<port>`
mendapat:

- `conn.modules.<apa pun>`, `conn.eval(...)`, `conn.execute(...)`,
  `conn.builtins` — eksekusi kode Python arbitrer di dalam Wine sebagai
  user layanan terminal;
- akses penuh modul `MetaTrader5`, termasuk fungsi pengiriman/cek order.

Pembatasan READ-ONLY di `MT5LinuxProvider` hanya berlaku **di sisi klien**
(kode API kita). Proses lokal lain (atau API yang terkompromi) tidak
terikat oleh pembatasan itu. Server RPyC classic tidak memiliki
autentikasi.

## 2. Lapisan perlindungan yang ada sekarang

| Lapisan | Melindungi dari | Batasan |
|---|---|---|
| Password **investor** | Broker menolak order dari sesi investor | `trade_allowed=False` **bukan** bukti kriptografis (lihat §5) |
| Verifikasi fail-closed (`services/verification.py`) | Kredensial master / akun lain / data tidak lengkap | Bergantung pada laporan terminal |
| Sync menolak `trade_allowed != False` | Kredensial master yang tersimpan lewat jalur lama | Idem |
| Guard statis AST (tests) | Kode backend memanggil fungsi order | Hanya kode di repo ini |
| Bind `127.0.0.1` + unit systemd (`IPAddressDeny=any`, `ProtectSystem=strict`) | Akses jaringan publik ke RPyC | Proses lokal lain tetap bisa terhubung |
| Helper `status`: listener milik cgroup unit customer | Verifikasi berbicara dengan terminal customer lain | Linux `/proc`, IPv4 |

## 3. Rekomendasi (urutan prioritas)

### A. Batasi akses antarproses (segera, tanpa ubah kode)

Hanya user API (`transcendence`) yang boleh membuka port RPyC:

```bash
# izinkan hanya UID user API ke jendela port RPyC; tolak semua UID lain
sudo iptables -A OUTPUT -o lo -p tcp --dport 18812:18815 \
     -m owner --uid-owner transcendence -j ACCEPT
sudo iptables -A OUTPUT -o lo -p tcp --dport 18812:18815 -j REJECT
```

Trade-off: proses lain milik user `transcendence` tetap dapat terhubung.
Jalankan API dengan user yang tidak dipakai layanan lain.

### B. Ganti RPyC classic dengan layanan RPC read-only (prototipe ada)

`backend/mt5client/ro_service.py` + `backend/mt5client/ro_provider.py`
(`MT5_PROVIDER=mt5ro`):

- `rpyc.Service` biasa — hanya metode whitelist
  (`initialize, shutdown, last_error, account_info, terminal_info,
  symbol_info, positions_get, history_deals_get, history_orders_get`);
  tidak ada `modules/eval/execute`, tidak ada fungsi order;
- autentikasi token per terminal sebelum handshake (HMAC-SHA256 dari
  `MT5_RPC_SECRET` + `code:port:provision_id`, berkas 0600; tidak lewat
  argumen proses);
- argumen hanya tipe primitif; hasil berupa JSON (tanpa netref);
- `allow_public_attrs/all_attrs/setattr/delattr/pickle = False`;
- bind dipaksa `127.0.0.1`.

Diuji di `tests/test_rpyc_readonly.py` (server rpyc sungguhan + modul
MetaTrader5 palsu): tanpa/salah token ditolak, `order_send` tidak
terjangkau, kapabilitas classic tidak ada, argumen non-primitif ditolak,
provider klien end-to-end.

Langkah produksi (belum dilakukan): pasang Python Windows + `MetaTrader5` +
`rpyc` di template Wine; set di `/etc/transcendence/provisioner.json`:
`"ro_service": true, "wine_python": "C:\\Python311\\python.exe",
"rpc_secret": "<sama dengan MT5_RPC_SECRET>"`; salin `ro_service.py` ke
`template_dir`; set `MT5_PROVIDER=mt5ro`. Verifikasi di staging dengan
akun demo + password investor sebelum produksi.

Trade-off: komponen tambahan di dalam Wine; versi `rpyc` klien & server
harus kompatibel; token berbasis secret bersama (rotasi secret =
reinstall terminal).

### C. Alternatif jangka panjang

Protokol IPC minimal (JSON-RPC lewat named pipe / unix socket dengan
`SO_PEERCRED`) tanpa RPyC sama sekali. Paling sempit permukaannya, tetapi
memerlukan jembatan Windows↔Linux di Wine yang belum dievaluasi.

## 4. Apa yang dijamin oleh kode API

- Tidak ada pemanggilan `order_send`, `order_check`, `order_modify`,
  `order_delete`, `order_close`, `positions_close*`, `order_calc_*` di
  backend (guard AST di `tests/test_remediation_pr5.py` dan
  `tests/test_readonly_static.py`).
- Provider hanya mengekspos metode baca (`mt5client/provider.py`,
  `__getattr__` menolak lainnya).

## 5. Batasan jujur: investor-only

MetaTrader5 tidak menyediakan cara kriptografis untuk membuktikan bahwa
password yang dipakai adalah password **investor**. `trade_allowed=False`
juga muncul untuk password **master** pada akun yang hak trade-nya
dimatikan broker. Sistem ini karena itu **tidak mengklaim** dapat
membuktikan jenis password; jaminannya berlapis (§2), dan lapisan RPC
read-only (§3B) menutup jalur eksekusi order dari sisi terminal meskipun
kredensial master lolos.

## 6. Rencana migrasi RPyC classic → RPC read-only (tanpa memutus kompatibilitas)

Mode RPC kini **per terminal** (`mt5_terminals.rpc_mode`, migrasi 005):
`classic` (mt5server, perilaku lama) atau `ro` (layanan read-only bertoken).
Provider dipilih per terminal, health check memakai probe yang sesuai
(`probe_ro_session` mewajibkan token; probe classic tidak pernah dianggap
sehat di mode `ro`). Terminal yang sudah ada tetap `classic`.

| Fase | Langkah | Rollback |
|---|---|---|
| 0 (segera) | Firewall owner-match (§3A) + `service_user_mode=per_customer` di helper | hapus aturan |
| 1 (staging) | Bangun template Wine: Python Windows + `MetaTrader5` + `rpyc` + `ro_service.py`; set helper `ro_service=true`, `wine_python`, `rpc_secret`; `MT5_RPC_SECRET` di API | `ro_service=false` |
| 2 (staging) | `MT5_RPC_MODE_DEFAULT=ro` → hanya terminal BARU memakai `ro`; uji akun demo + password investor | kembali `classic` (terminal ro tetap jalan) |
| 3 (produksi bertahap) | Migrasi terminal lama satu per satu: unbind → connect ulang (terminal baru `ro`); riwayat jurnal dipertahankan | connect ulang dgn default `classic` |
| 4 | Setelah semua `ro`: hapus `mt5server.exe` dari template; tolak `rpc_mode=classic` di helper | — |

Selama fase 0–3, terminal `classic` TETAP rentan eksekusi kode arbitrer
oleh proses berhak akses ke port (dibuktikan di uji integrasi, §7);
firewall hanya mempersempit siapa yang dapat terhubung.

## 7. Bukti uji integrasi Linux terisolasi (container sandbox, bukan VPS)

Nyata: sudo + sudoers satu perintah, helper root-owned, `useradd/userdel`
per customer, ownership/izin berkas, proses & socket TCP, cgroup v1 kernel,
`/proc`, iptables owner-match, backend API asli sebagai user non-root.
Pengganti (NOT VERIFIED): systemd (shim `systemctl`), Wine (shim), MT5
(modul palsu). Hasil (22 test, stabil 3 putaran):

- `rpyc.classic.connect` dari user customer LAIN ke terminal `classic`
  → `conn.modules.os.getuid()` = UID terminal korban (**eksekusi kode**).
- Firewall owner-match: user lain/`nobody`/root → diblokir; user API →
  tetap terhubung **dan tetap mendapat eksekusi kode** → firewall saja
  TIDAK cukup.
- Mode `ro`: tanpa/salah token & handshake classic ditolak; `order_send`
  tidak terjangkau; tidak ada `conn.modules`.
- Listener port diambil proses customer lain → helper `status` running
  = False (verifikasi tidak dilakukan ke terminal milik orang lain).

## 8. Temuan tambahan: koneksi classic SIMETRIS (terminal → proses API)

`rpyc.classic.connect()` — dipakai paket `mt5linux` — juga menjalankan
`SlaveService` di **sisi klien**. Server terminal yang terkompromi dapat
memanggil `conn.root.getmodule("os")` dan mengeksekusi kode **di dalam
proses API** (yang memegang `MT5_CRED_KEY` dan akses DB semua customer).
Dibuktikan dengan server classic nyata di proses terpisah
(`tests/test_rpyc_classic_client.py`).

Perbaikan (PR #4, kompatibel dengan mt5server yang ada):
`MT5LinuxProvider` kini memakai `rpyc.connect(service=ClassicClient)`
(MasterService + FakeSlaveService bawaan rpyc) dengan konfigurasi klien
tanpa pickle/atribut publik; pemanggilan MT5 lewat helper sisi server
ber-whitelist yang mengembalikan JSON. Klien layanan read-only juga
memakai `VoidService` (diuji: server pengganti berbahaya tidak dapat
memanggil balik).

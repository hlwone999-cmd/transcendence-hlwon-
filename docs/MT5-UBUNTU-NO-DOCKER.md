# Panduan Deployment — Jurnal MT5 di VPS Ubuntu TANPA Docker

> Status: **dokumentasi saja** — belum dijalankan/diuji di VPS. Semua langkah
> di bawah ini adalah langkah manual yang akan Anda jalankan sendiri setelah
> Pull Request digabungkan.
>
> Aturan mutlak yang dipatuhi integrasi ini: **NO DOCKER · NO AUTO TRADING ·
> READ-ONLY · tidak ada eksekusi order.**

## 0. Ringkasan arsitektur

```
Akun MT5 Exness (login + server)
        │
        ▼
MetaTrader 5 Terminal berjalan via Wine (VPS Ubuntu, TANPA Docker)
        │
        ▼
mt5server.exe  (RPyC server, hanya 127.0.0.1:18812)
        │
        ▼
mt5linux  (Python, klien RPyC — whitelist READ-ONLY)
        │
        ▼
Flask backend  (SQLite, sinkronisasi, analitik, REST API)
        │
        ▼
nginx + HTTPS  ── internet ── Aplikasi Android "Transcendence" (tab MT5)
```

Kenapa begitu: package Python resmi `MetaTrader5` hanya jalan di Windows.
`mt5linux` menjembatani lewat RPyC ke `mt5server.exe` yang dieksekusi Wine.
Kita memakai **mode standalone Wine** (Opsi 3 di README mt5linux) — bukan
Docker, bukan noVNC.

## 1. Persyaratan VPS

| Item | Nilai target | Catatan |
| --- | --- | --- |
| OS | Ubuntu 22.04/24.04 **amd64 (x86_64)** | Wine modern tidak support arm64 untuk MT5 — pastikan VPS Intel/AMD |
| CPU | 4 vCPU | cukup: Wine+MT5 ~1 core saat idle |
| RAM | 3,8 GB + swap 2 GB | MT5 via Wine ±1–1,5 GB, backend ±150 MB, sisanya untuk OS |
| Storage | 19 GB | Wine prefix ±1,5 GB + terminal ±600 MB + history data |
| Docker | **TIDAK DIPAKAI** | tidak ada Docker/Podman/udocker di seluruh alur |
| Port | 22, 80, 443 (publik); **18812 hanya 127.0.0.1** | 18812 TIDAK boleh dibuka ke internet |
| Akses | sudo + domain A record ke IP VPS | untuk HTTPS Let's Encrypt |

Cek arsitektur: `uname -m` harus `x86_64`.

## 2. Instalasi Wine (tanpa Docker)

```bash
sudo dpkg --add-architecture i386
sudo mkdir -pm755 /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
sudo wget -NP /etc/apt/sources.list.d/ \
  https://dl.winehq.org/wine-builds/ubuntu/dists/$(lsb_release -sc)/winehq-$(lsb_release -sc).sources
sudo apt update
sudo apt install -y --install-recommends winehq-stable winbind
wine --version   # contoh: wine-9.0 / 10.x
```

Headless (VPS tanpa GUI): installer MT5 butuh tampilan minimal — cukup
Xvfb virtual framebuffer:

```bash
sudo apt install -y xvfb
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
```

> tips: taruh `export DISPLAY=:99` + `xvfb-run` di unit systemd nanti.

## 3. Instalasi terminal MT5 Exness

1. Unduh installer MT5 Exness (`mt5setup.exe`) dari halaman resmi Exness
   (area memberi Anda akun) — jangan dari mirror pihak ketiga.
2. Buat prefix khusus (terpisah dari aplikasi lain):

   ```bash
   export WINEPREFIX=$HOME/.wine-mt5
   winecfg -v win10           # set Windows 10
   wine mt5setup.exe          # ikuti installer GUI (via Xvfb/VNC)
   ```

3. Jalankan sekali, login akun Exness Anda (mode investor ATAU master —
   backend hanya baca, tapi login investor lebih aman), centang "Save
   password" jika ingin terminal auto-relogin. Biarkan terminal terbuka.
4. Matikan fitur berat di Tools → Options → Charts: "Max bars in chart"
   set 100000, matikan news. Hemat RAM di VPS 3,8 GB.

> CATATAN PENTING: jangan import package `MetaTrader5` Windows/Python ke
> Python Linux — tidak akan jalan. Jalur yang benar adalah mt5linux +
> mt5server.exe seperti di bawah.

## 4. Instalasi mt5server.exe + mt5linux

`mt5server.exe` adalah binary standalone (semua dependensi ada di dalam —
rilis resmi di halaman *Releases* repo `lucas-campagna/mt5linux`):

```bash
# versi — cek halaman releases, contoh:
MT5SRV_VER=$(curl -s https://api.github.com/repos/lucas-campagna/mt5linux/releases/latest | grep -oP '"tag_name": "\K[^"]+')
mkdir -p $HOME/mt5 && cd $HOME/mt5
wget -O mt5server.exe \
  "https://github.com/lucas-campagna/mt5linux/releases/download/${MT5SRV_VER}/mt5server.exe"

# jalankan via Wine (default port 18812, HANYA localhost):
export WINEPREFIX=$HOME/.wine-mt5
export WINEDEBUG=-all
wine mt5server.exe --port 18812 &      # tampilkan opsi: wine mt5server.exe --help
```

Lalu di sisi Linux Python (backend):

```bash
cd /opt/jurnal-mt5/backend        # hasil clone repo, folder backend/
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # termasuk mt5linux (klien RPyC)
```

Verifikasi koneksi manual (tanpa lewat Flask) — GANTI kredensial:

```bash
python - <<'PY'
from mt5linux import MetaTrader5
mt5 = MetaTrader5(host='127.0.0.1', port=18812)
print('initialize:', mt5.initialize(login=12345678, password='XXX', server='Exness-MT5Real'))
print('account:', mt5.account_info())
from datetime import datetime, timezone
print('deals 90h:', len(mt5.history_deals_get(
    datetime(2026, 8, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))))
mt5.shutdown()
PY
```

`initialize: True` + `account:` terisi = jalur MT5→Wine→RPyC→Python hidup.

## 5. Virtual environment & backend

```bash
sudo useradd -m -s /bin/bash jurnal            # user khusus (disarankan)
sudo mv /path/clone/transcendence-hlwon- /opt/jurnal-mt5 && cd /opt/jurnal-mt5/backend
sudo chown -R jurnal:jurnal /opt/jurnal-mt5
sudo -iu jurnal
cd /opt/jurnal-mt5/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 6. Environment variables

```bash
cp .env.example .env
nano .env
```

Yang wajib diisi:

```ini
MT5_PROVIDER=mt5linux
MT5_ENABLED=true
MT5_HOST=127.0.0.1
MT5_PORT=18812
MT5_LOGIN=12345678
MT5_PASSWORD=<password akun — HANYA di file ini>
MT5_SERVER=Exness-MT5Real
MT5_SYNC_INTERVAL_SECONDS=300
MT5_HISTORY_INITIAL_DAYS=90
API_TOKEN=<openssl rand -hex 32>     # dipakai APK di tab MT5
API_BIND=127.0.0.1                   # nginx yang ke internet, bukan Flask
API_PORT=8000
```

`chmod 600 .env` — file ini satu-satunya tempat password. Password tidak
pernah ditulis ke database/log/APK (diuji otomatis di test backend).

## 7. Menjalankan backend

```bash
cd /opt/jurnal-mt5/backend && source .venv/bin/activate
python run.py
# * Jurnal MT5 backend berjalan di http://127.0.0.1:8000
```

Uji dari server itu sendiri:

```bash
curl -s http://127.0.0.1:8000/api/v1/health
curl -s -H "X-API-Token: <API_TOKEN>" http://127.0.0.1:8000/api/v1/mt5/status
curl -s -X POST -H "X-API-Token: <API_TOKEN>" http://127.0.0.1:8000/api/v1/mt5/sync \
  -d '{"mode":"initial"}'
curl -s -H "X-API-Token: <API_TOKEN>" "http://127.0.0.1:8000/api/v1/journal/stats"
```

## 8. systemd: auto-start + restart

**A. mt5server (Wine)** — `/etc/systemd/system/mt5server.service`:

```ini
[Unit]
Description=mt5server.exe (RPyC bridge utk MT5, Wine)
After=network.target

[Service]
Type=simple
User=jurnal
Environment=WINEPREFIX=/home/jurnal/.wine-mt5
Environment=WINEDEBUG=-all
Environment=DISPLAY=:99
ExecStartPre=/usr/bin/Xvfb :99 -screen 0 1024x768x24
ExecStart=/usr/bin/wine /home/jurnal/mt5/mt5server.exe --port 18812
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

> Cara lebih bersih untuk Xvfb+MT5+server: jadikan satu shell wrapper
> `start-mt5.sh` (xvfb-run wine mt5server.exe) dan arahkan ExecStart ke
> wrapper tersebut.

**B. Backend Flask** — `/etc/systemd/system/jurnal-mt5.service`:

```ini
[Unit]
Description=Backend Jurnal MT5 (Flask read-only)
After=network.target mt5server.service
Wants=mt5server.service

[Service]
Type=simple
User=jurnal
WorkingDirectory=/opt/jurnal-mt5/backend
ExecStart=/opt/jurnal-mt5/backend/.venv/bin/python run.py
Restart=always
RestartSec=10
# Hardening ringan
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/opt/jurnal-mt5/backend/data

[Install]
WantedBy=multi-user.target
```

Aktifkan:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mt5server.service jurnal-mt5.service
systemctl status jurnal-mt5 --no-pager
journalctl -u jurnal-mt5 -f          # pantau log (tidak ada password di sini)
```

## 9. Menjaga terminal MT5 tetap aktif

- Terminal MT5 harus **tetap berjalan & login** — `mt5server.exe` hanya
  jembatan; data history dibaca dari terminal.
- Gunakan VNC/Xvfb terpisah bila perlu cek GUI; terminal bisa dijalankan
  via `wine` di prefix yang sama:
  `env WINEPREFIX=$HOME/.wine-mt5 wine "C:\Program Files\MetaTrader 5\terminal64.exe" &`
- Jadwalkan watchdog sederhana (cron `*/5 * * * *`) yang memastikan proses
  `terminal64.exe` hidup, else start ulang (contoh skrip di bawah):

```bash
#!/bin/bash
pgrep -f terminal64.exe >/dev/null || \
  (cd $HOME && env WINEPREFIX=$HOME/.wine-mt5 WINEDEBUG=-all DISPLAY=:99 \
   nohup wine "C:\Program Files\MetaTrader 5\terminal64.exe" >/dev/null 2>&1 &)
```

- Backend sendiri tahan banting: MT5 mati → `/mt5/status` balas
  `connected:false` dan scheduler terus mencoba tiap interval.

## 10. Firewall, autentikasi, HTTPS

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
# 18812 TIDAK dibuka — hanya 127.0.0.1
sudo ufw enable
```

nginx reverse proxy + TLS:

```bash
sudo apt install -y nginx python3-certbot-nginx
sudo nano /etc/nginx/sites-available/jurnal
```

```nginx
server {
    server_name jurnal.domainanda.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/jurnal /etc/nginx/sites-enabled/
sudo certbot --nginx -d jurnal.domainanda.com   # HTTPS otomatis
```

APK hanya butuh: **URL** `https://jurnal.domainanda.com` dan
**API token** — isi di tab MT5 → tombol SERVER.

## 11. Menguji login Exness & history deals

1. Langkah verifikasi manual ada di §4 (skrip python `history_deals_get`).
2. Setelah backend hidup: `POST /api/v1/mt5/sync {"mode":"initial"}` →
   balasan `status: ok` berarti deals tersimpan
   (`deals_upserted > 0`).
3. `GET /api/v1/journal/stats` → winrate/PF/expectancy terisi.
4. Buka tab **MT5** di aplikasi → pastikan akun, statistik, equity curve,
   dan daftar trade tampil.
5. **PENTING**: sampai langkah ini dijalankan pada VPS nyata dengan akun
   Exness asli, integrasi belum bisa dinyatakan "terhubung Exness" —
   semua test hanya memakai mock.

## 12. Troubleshooting & rollback

| Gejala | Sebab umum & solusi |
| --- | --- |
| `initialize: False` di mt5linux | terminal MT5 belum login / nama `MT5_SERVER` salah persis — samakan dengan dropdown server di terminal |
| RPyC connection refused | mt5server.exe tidak jalan: `pgrep -af mt5server`, cek `journalctl -u mt5server`; pastikan WINEPREFIX benar |
| `history_deals_get` kosong | rentang tanggal salah (pakai UTC) atau akun baru belum ada history — tarik dulu di terminal (Account History → All) |
| Wine crash saat install | coba `winecfg` → Windows 10, install `winetricks corefonts`, atau turunkan ke winehq-stable versi sebelumnya |
| Backend 502 di nginx | Flask mati — `systemctl status jurnal-mt5`, cek `.env` (API_PORT) |
| APK 401 | API_TOKEN di tab MT5 ≠ `API_TOKEN` di `.env` backend |
| RAM penuh | kecilkan "Max bars in chart", matikan chart di MT5, pastikan swap aktif (`swapon --show`) |
| Jam/UTC geser | waktu disimpan UTC; Exness server GMT+0 — jika broker lain beda zona, konversi tampilan di UI |

**Rollback** aman dan cepat:

```bash
sudo systemctl disable --now jurnal-mt5.service mt5server.service
# data historis tetap aman di file SQLite:
cp /opt/jurnal-mt5/backend/data/journal.db ~/backup-journal-$(date +%F).db
# kembalikan kode: git revert <commit-merge> di repo, redeploy
```

Karena integrasi ini murni **additive** (folder `backend/` + satu tab baru),
menghapus keduanya mengembalikan aplikasi ke kondisi sebelumnya tanpa
menyentuh kalkulator.

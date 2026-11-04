# Instalasi & Provisioning Terminal — VPS Ubuntu (Multi-Customer)

> Diperbarui setelah audit #1/#3: unit systemd kini menjalankan
> **terminal64.exe + server RPyC (mt5server)** per customer, health check
> koneksi nyata tersedia, dan path mengikuti VPS aktual:
> `/opt/transcendence-hlwon-` + `backend/.venv`.

## 1. Prasyarat VPS (contoh: Ubuntu 24.04, 2 vCPU / 4 GB RAM minimum)

```bash
sudo apt update && sudo apt install -y postgresql wine winbind xvfb
# owner database: hanya untuk migrasi/administrasi
sudo -u postgres createuser transcendence --pwprompt
sudo -u postgres createdb transcendence -O transcendence
# role aplikasi NON-OWNER (audit #2): tunduk RLS, dipakai backend sehari-hari
sudo -u postgres psql -c "CREATE ROLE transcendence_app LOGIN NOSUPERUSER \
  NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '<PASSWORD_APP>'"
# role ini + grant otomatis dibuat/diperbaiki migrasi 002_rls_roles.sql
sudo useradd -m -s /bin/bash transcendence
```

## 2. Kode & environment (path VPS aktual)

```bash
sudo mkdir -p /opt/transcendence-hlwon- && sudo chown transcendence: /opt/transcendence-hlwon-
# clone repo, checkout branch, lalu:
cd /opt/transcendence-hlwon-/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env   # lalu isi (lihat bagian 3)
```

## 3. File `.env` (kredensial TIDAK masuk repo)

```ini
# Backend sehari-hari MEMAKAI ROLE NON-OWNER (RLS aktif).
DATABASE_URL=postgresql://transcendence_app:PASSWORD_APP@127.0.0.1:5432/transcendence
MT5_CRED_KEY=<Fernet key: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
MT5_ENABLED=true
MAX_CUSTOMERS=4
TERMINAL_PROVISION_MODE=real
TERM_BASE_DIR=/var/lib/transcendence/terminals
API_TOKEN=<openssl rand -hex 32>   # hanya utk /admin/*
```

## 4. Migrasi PostgreSQL (pakai DSN owner, bukan DSN app)

```bash
# dari DB kosong:
DATABASE_URL=postgresql://transcendence:PASSWORD@127.0.0.1:5432/transcendence \
python3 -c "import db, config; c=config.load_config(); \
  conn=db.connect_postgres(c.db_url); print('migrasi:', db.migrate(conn)); conn.close()"
# dari deployment SQLite lama (data dipertahankan):
MT5_CRED_KEY=<kunci> python3 tools/migrate_sqlite_to_postgres.py \
  --sqlite data/journal.db --pg "$DATABASE_URL_OWNER" \
  --legacy-password "$MT5_PASSWORD_LAMA" --apply
```

Migrasi 002 membuat/membenahkan role `transcendence_app` + grant + policy
RLS secara idempoten.

## 5. Provisioning terminal per customer (otomatis saat bind)

Saat customer melakukan `POST /api/v1/accounts/bind`, backend otomatis:
1. Mengalokasikan port RPyC bebas (18812 → customer pertama, dst.)
   secara aman-race (advisory lock / `BEGIN IMMEDIATE` + `UNIQUE(rpyc_port)`),
2. Membuat direktori: `$TERM_BASE_DIR/<kode>/wine-prefix|terminal|data`,
3. Menulis **unit systemd + dua wrapper**:
   - `transcendence-mt5-<kode>.service`
   - `terminal/run-terminal.sh` — meluncurkan (1) `terminal64.exe /portable`
     di background dan (2) **server RPyC `mt5server` di foreground** yang
     HANYA bind `127.0.0.1:<port>` (jalur data `MT5LinuxProvider`);
     systemd mengawal server RPyC ini — bila mati, unit restart.
   - `terminal/stop-terminal.sh` — menghentikan proses customer ini saja.

Pasang unit terminal (sekali per customer, admin VPS):

```bash
sudo cp /var/lib/transcendence/terminals/C001/transcendence-mt5-c001.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now transcendence-mt5-c001
```

Instal MT5 Terminal portable + server RPyC (sekali per direktori terminal):

```bash
cd /var/lib/transcendence/terminals/C001/terminal
WINEPREFIX=../wine-prefix wine mt5setup.exe     # atau salin terminal64.exe portable
# Pasang python di Wine + MetaTrader5 + mt5linux, lalu sediakan mt5server:
#   WINEPREFIX=../wine-prefix wine python.exe -m pip install mt5linux MetaTrader5
# Default wrapper memakai $TERMINAL_DIR/mt5server.exe dengan argumen
#   <host> <port>  (mengikuti brief). Bila bridge Anda berbeda, override:
#   Environment=MT5SERVER_BIN="C:\python\python.exe -m mt5linux 127.0.0.1 18812"
# terminal64.exe hanya membaca — tidak ada EA/auto-trading di dalamnya.
```

Status `mt5_terminals.status` berubah `provisioning → ready` setelah
health check pertama berhasil.

## 6. Health check koneksi nyata (audit #1)

```bash
# a) probe port RPyC manual (koneksi TCP nyata):
curl -sS --max-time 3 127.0.0.1:18812 >/dev/null && echo port-up
# b) dari python backend:
#    from services.terminals import TerminalRegistry
#    reg.health_check(conn, customer_id)            -> TCP probe + catat waktu
#    reg.health_check(conn, customer_id, deep=True) -> + sesi RPyC read-only
#                                                      (terminal_info())
# Hasil ditulis ke mt5_terminals.last_health_at; tanpa listener nyata,
# hasilnya reachable=False — TIDAK pernah mengklaim live MT5 berhasil.
# c) systemctl status transcendence-mt5-c001   # unit menjaga server RPyC
```

> **Catatan jujur (audit):** uji LIVE (Wine + terminal64.exe + mt5server
> asli + akun Exness) TIDAK dilakukan di sandbox. Yang diuji otomatis:
> artefak unit/wrapper benar, probe TCP nyata, health check jujur.
> Live test wajib dijalankan admin di VPS sebelum produksi.

## 7. Service backend (path VPS aktual)

```ini
# /etc/systemd/system/transcendence-backend.service
[Unit]
Description=Transcendence Backend (multi-customer)
After=network.target postgresql.service

[Service]
User=transcendence
WorkingDirectory=/opt/transcendence-hlwon-/backend
EnvironmentFile=/opt/transcendence-hlwon-/backend/.env
ExecStart=/opt/transcendence-hlwon-/backend/.venv/bin/python run.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 8. Isolasi & keamanan jaringan

- Port RPyC (18812-18815) **hanya bind 127.0.0.1** — tidak ada publik.
- Backend `API_BIND=127.0.0.1` di balik reverse-proxy HTTPS (Caddy/Nginx).
- Restart satu terminal: `sudo systemctl restart transcendence-mt5-c001`
  — customer lain tidak terpengaruh (unit terpisah).
- Firewall: hanya 80/443 publik; 5432 & 18812-18815 internal.

## 9. Verifikasi

```bash
curl -s http://127.0.0.1:8000/api/v1/health
# register customer pertama, login, bind akun, sync manual — lihat
# docs/HASIL-TEST-MULTI-CUSTOMER.md untuk daftar perintah lengkap.
```

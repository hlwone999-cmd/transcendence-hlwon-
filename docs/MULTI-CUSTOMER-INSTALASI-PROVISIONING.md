# Instalasi & Provisioning Terminal — VPS Ubuntu (Multi-Customer)

## 1. Prasyarat VPS (contoh: Ubuntu 24.04, 2 vCPU / 4 GB RAM minimum)

```bash
sudo apt update && sudo apt install -y postgresql wine winbind xvfb
sudo -u postgres createuser transcendence --pwprompt
sudo -u postgres createdb transcendence -O transcendence
sudo useradd -m -s /bin/bash transcendence
```

## 2. Kode & environment

```bash
sudo mkdir -p /opt/transcendence && sudo chown transcendence: /opt/transcendence
# clone repo, checkout branch, lalu:
cd /opt/transcendence/backend
python3 -m venv /opt/transcendence/venv
/opt/transcendence/venv/bin/pip install -r requirements.txt psycopg2-binary cryptography

cp .env.example .env   # lalu isi (lihat bagian 3)
```

## 3. File `.env` (kredensial TIDAK masuk repo)

```ini
DATABASE_URL=postgresql://transcendence:PASSWORD@127.0.0.1:5432/transcendence
MT5_CRED_KEY=<Fernet key: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
MT5_ENABLED=true
MAX_CUSTOMERS=4
TERMINAL_PROVISION_MODE=real
TERM_BASE_DIR=/var/lib/transcendence/terminals
API_TOKEN=<openssl rand -hex 32>   # hanya utk /admin/*
```

## 4. Migrasi PostgreSQL

```bash
# dari DB kosong:
python3 -c "import db, config; c=config.load_config(); \
  conn=db.get_db(c); print('migrasi:', db.migrate(conn))"
# dari deployment SQLite lama (data dipertahankan):
MT5_CRED_KEY=<kunci> python3 tools/migrate_sqlite_to_postgres.py \
  --sqlite data/journal.db --pg "$DATABASE_URL" \
  --legacy-password "$MT5_PASSWORD_LAMA" --apply
```

## 5. Provisioning terminal per customer (otomatis saat bind)

Saat customer melakukan `POST /api/v1/accounts/bind`, backend otomatis:
1. Mengalokasikan port RPyC bebas (18812 → customer pertama, dst.),
2. Membuat direktori: `$TERM_BASE_DIR/<kode>/wine-prefix|terminal|data`,
3. Menulis unit systemd: `$TERM_BASE_DIR/<kode>/transcendence-mt5-<kode>.service`.

Pasang unit terminal (sekali per customer, admin VPS):

```bash
sudo cp /var/lib/transcendence/terminals/C001/transcendence-mt5-c001.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now transcendence-mt5-c001
```

Instal MT5 Terminal portable (sekali per direktori terminal):

```bash
cd /var/lib/transcendence/terminals/C001/terminal
WINEPREFIX=../wine-prefix wine mt5setup.exe     # atau salin terminal64.exe portable
# terminal64.exe hanya membaca — tidak ada EA/auto-trading di dalamnya
```

Status `mt5_terminals.status` berubah `provisioning → ready` setelah
admin menandainya; sync berjalan otomatis begitu port merespons.

## 6. Service backend

```ini
# /etc/systemd/system/transcendence-backend.service
[Unit]
Description=Transcendence Backend (multi-customer)
After=network.target postgresql.service

[Service]
User=transcendence
WorkingDirectory=/opt/transcendence/backend
EnvironmentFile=/opt/transcendence/backend/.env
ExecStart=/opt/transcendence/venv/bin/python run.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 7. Isolasi & keamanan jaringan

- Port RPyC (18812-18815) **hanya bind 127.0.0.1** — tidak ada publik.
- Backend `API_BIND=127.0.0.1` di balik reverse-proxy HTTPS (Caddy/Nginx).
- Restart satu terminal: `sudo systemctl restart transcendence-mt5-c001`
  — customer lain tidak terpengaruh (unit terpisah).
- Firewall: hanya 80/443 publik; 5432 & 18812-18815 internal.

## 8. Verifikasi

```bash
curl -s http://127.0.0.1:8000/api/v1/health
# register customer pertama, login, bind akun, sync manual — lihat
# docs/HASIL-TEST-MULTI-CUSTOMER.md untuk daftar perintah lengkap.
```

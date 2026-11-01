# Backend Jurnal MT5 (Flask + mt5linux)

Backend read-only yang mengambil riwayat transaksi akun MT5 (Exness) lewat
[mt5linux](https://github.com/lucas-campagna/mt5linux) dan menyajikannya
sebagai REST API untuk tab "MT5" di aplikasi Transcendence.

```
MT5 Exness ── MT5 Terminal (Wine, VPS Ubuntu) ── mt5server.exe (RPyC)
      │
      └── mt5linux (Python) ── Flask backend (file ini) ── REST API ── APK
```

## Menjalankan (lokal / VPS)

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # isi kredensial & API_TOKEN (openssl rand -hex 32)
python run.py             # default http://127.0.0.1:8000
```

Tanpa terminal MT5 backend **tetap jalan** — `/mt5/status` balas
`connected: false` (bukan crash). Set `MT5_ENABLED=false` untuk mode idle.

## Struktur

| Path | Isi |
| --- | --- |
| `config.py` | Konfigurasi dari `.env` (password tidak pernah di-log) |
| `db.py` | SQLite (WAL) + runner migrasi |
| `migrations/001_init.sql` | Skema: akun, raw deals/orders, trades, sync state/logs, cache simbol |
| `mt5client/provider.py` | **Whitelist READ-ONLY** — order_send/eval/execute diblokir `__getattr__` |
| `mt5client/mt5linux_provider.py` | Implementasi nyata (lazy import mt5linux) |
| `mt5client/mock.py` | Mock MT5 untuk test (skema field = package resmi MetaTrader5) |
| `services/sync.py` | Sinkronisasi initial/incremental/full + overlap aman + upsert dedup |
| `services/reconstruct.py` | Deals → trades (partial close, scale-in/out, hedging/netting, filter deposit) |
| `services/analytics.py` | Winrate, PF, expectancy, pip per simbol, RR (N/A tanpa SL), equity curve, drawdown |
| `services/scheduler.py` | Sync berkala (thread daemon, interval `MT5_SYNC_INTERVAL_SECONDS`) |
| `api/routes.py` | REST API + auth token + pagination + validasi |
| `tests/` | 47 test pytest (mock MT5, tanpa jaringan) |

## REST API (prefix `/api/v1`)

Auth: header `X-API-Token: <API_TOKEN>`. Wajib diisi di produksi.

| Method | Path | Fungsi |
| --- | --- | --- |
| GET | `/health` | Health check (tanpa auth) |
| GET | `/mt5/status` | Status koneksi MT5 + sinkron terakhir + jumlah data tersimpan |
| GET | `/mt5/account` | Snapshot akun (login, server, broker, balance, equity, leverage) |
| POST | `/mt5/sync` | Trigger sync. Body JSON opsional: `{"mode": "incremental"\|"initial"\|"full"\|"manual"}` |
| GET | `/mt5/sync/status` | State sync + 20 log terakhir + info scheduler |
| GET | `/journal/trades` | Daftar trade. Query: `page`, `page_size` (≤200), `from`, `to` (ISO UTC), `to_exclusive=1`, `symbol`, `status=open\|closed\|all`, `direction=buy\|sell`, `sort`, `order` |
| GET | `/journal/trades/{position_id}` | Detail trade + deals + orders mentah |
| GET | `/journal/stats` | Statistik: winrate, gross/net, PF, expectancy, per simbol, harian/mingguan/bulanan. Query tanggal sama dgn trades |
| GET | `/journal/equity-curve` | Kurva net kumulatif + ops balance + drawdown historis. Query tanggal sama |
| GET | `/journal/reconciliation` | Rekonsiliasi raw deals vs trades interval identik (`from_deals`, `by_position`, `by_close`, `balance_ops`, `delta_close_vs_deals`) — lihat `docs/DATE-TIMEZONE-RECONCILIATION.md` |
| GET | `/symbols` | Daftar simbol + definisi pip (untuk filter UI) |

Semantik filter tanggal: `from` inkusif, `to` default inkusif,
`to_exclusive=1` membuatnya eksklusif → interval `[from, to)`. Semua
UTC. Detail lengkap: `docs/DATE-TIMEZONE-RECONCILIATION.md`.

Error JSON konsisten: `{"error": {"code": 400, "message": "..."}}`.

## Keamanan

- **READ-ONLY**: hanya 5 method baca yang diekspos. `order_send`,
  `order_check`, `order_calc_*`, `eval`, `execute` diblokir dan diuji test.
- Password MT5 hanya di env/backend; tidak pernah masuk log, DB, atau API.
- RPyC `mt5server.exe` hanya boleh bind `127.0.0.1`; port 18812 ditutup
  firewall (lihat `docs/MT5-UBUNTU-NO-DOCKER.md`).
- API diakses APK lewat HTTPS (nginx/caddy reverse proxy).

## Test

```bash
pip install pytest
python -m pytest        # 63 test — sinkron, rekonstruksi, analitik, API, filter tanggal, rekonsiliasi, read-only
```

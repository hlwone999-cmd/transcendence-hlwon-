# Konfigurasi Multi-Broker — Transcendence Multi-Customer

## Prinsip

Tidak ada satu pun nama broker yang di-hardcode di backend. `server`,
`login`, dan `password` MT5 adalah **data per customer** yang dikirim
saat bind dan disimpan terenkripsi:

| Data | Lokasi | Catatan |
|---|---|---|
| `server` (mis. `Exness-MT5Real`, `ICMarketsSC-MT5`) | `mt5_accounts.server` | bebas teks, unik per (login, server) |
| `login` | `mt5_accounts.login` | angka login MT5 |
| `password` | `mt5_credentials.enc_password` | Fernet, kunci `MT5_CRED_KEY` |
| port RPyC | `mt5_terminals.rpyc_port` | dialokasikan otomatis per customer |

## Mengikat akun (API)

```bash
curl -X POST https://jurnal.domain.com/api/v1/accounts/bind \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"login": 12345678, "server": "Exness-MT5Real", "password": "..."}'
```

- Customer A boleh Exness, customer B boleh IC Markets/Deriv/broker lain —
  masing-masing mendapat terminal & Wine prefix sendiri.
- Satu akun MT5 hanya boleh dimiliki satu customer (`UNIQUE(login, server)`).
- Ganti broker = unbind → bind baru (data lama terhapus CASCADE).

## Instalasi terminal multi-broker

Setiap direktori terminal independen. Install satu terminal portable per
customer, login ke server broker masing-masing saat provisioning terminal
(lihat docs/MULTI-CUSTOMER-INSTALASI-PROVISIONING.md §5). Backend tidak
pernah menyentuh konfigurasi broker di dalam terminal — koneksi MT5
terjadi via RPyC dengan kredensial terdekripsi per akun.

## Simbol & pip

Definisi pip per simbol tetap dari `SYMBOL_CONFIG` + `symbol_info`
terminal (10 × point secara default) — tidak ada perubahan terhadap
perhitungan pip/kalkulator lot (protected).

## Batasan MVP

- Satu akun per customer; broker berbeda antar customer didukung penuh.
- Multi-akun untuk satu customer: skema sudah siap (hapus UNIQUE(customer_id)
  di fase berikutnya) — tidak bagus MVP ini.

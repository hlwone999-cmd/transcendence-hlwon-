"""Paket klien MT5 read-only.

Lapisan ini SATU-SATUNYA jalur ke MT5 dan hanya mengekspos method baca:
account_info, symbol_info, positions_get, history_deals_get,
history_orders_get (plus terminal_info utk status).

mt5linux/PYMT5 juga punya order_send, order_check, order_calc_*, bahkan
eval/execute via RPyC. Semua itu SENGAJA tidak diekspos di sini dan
diblokir oleh MT5ProviderBase.__getattr__ (lihat provider.py + test).
"""

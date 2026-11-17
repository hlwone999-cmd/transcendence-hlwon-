"""Verifikasi LIVE kredensial MT5 (investor-only, FAIL-CLOSED).

Dipakai /mt5/connect, /accounts/bind, dan koordinator provisioning.

BATASAN JUJUR (Tahap 3F): MetaTrader5 tidak menyediakan bukti
kriptografis bahwa sebuah password adalah password INVESTOR. Sinyal yang
tersedia hanyalah account_info().trade_allowed == False (sesi investor
selalu False). Nilai False JUGA dapat muncul untuk password MASTER pada
akun yang hak trade-nya dimatikan broker. Karena itu jaminan read-only
sistem ini berlapis dan TIDAK hanya bergantung pada flag tersebut:
(1) backend tidak pernah memanggil fungsi eksekusi order (guard statis
AST + provider whitelist), (2) sync menolak sesi trade_allowed != False,
(3) terminal/RPyC diisolasi per customer (docs/RPYC-SECURITY.md).
Sistem TIDAK mengklaim dapat membuktikan jenis password.
"""
from __future__ import annotations

import threading
import time

from mt5client.provider import (MT5ConnectionError, MT5Error,
                                looks_like_broker_auth_error)

ACCOUNT_TRADE_MODE_REAL = 2   # MetaTrader5 ACCOUNT_TRADE_MODE_REAL


def classify_account_info(info, *, enforce_investor_readonly: bool,
                          expected_login: int | None,
                          expected_server: str | None,
                          require_real: bool) -> str:
    """Klasifikasi FAIL-CLOSED hasil account_info() (remediasi S1).

    'ok' HANYA bila semua bukti ada: dict tidak kosong, login & server
    sama dengan yang diminta, trade_allowed is False (sinyal sesi
    investor — lihat batasan di atas) dan, bila diwajibkan, trade_mode
    REAL. Field hilang / tipe salah = TIDAK terbukti (gagal).
    """
    if not isinstance(info, dict) or not info:
        return "unverified"
    if "login" not in info or "server" not in info:
        return "unverified"
    if enforce_investor_readonly and "trade_allowed" not in info:
        return "unverified"
    if expected_login is not None:
        try:
            if int(info.get("login")) != int(expected_login):
                return "identity_mismatch"
        except (TypeError, ValueError):
            return "unverified"
    if expected_server is not None and (
            str(info.get("server") or "").strip().casefold()
            != str(expected_server).strip().casefold()):
        return "identity_mismatch"
    if enforce_investor_readonly and info.get("trade_allowed") is not False:
        return "master_password"
    if require_real:
        try:
            if int(info.get("trade_mode")) != ACCOUNT_TRADE_MODE_REAL:
                return "not_real"
        except (TypeError, ValueError):
            return "unverified"
    return "ok"


def live_verify_account(conn, registry, account_id: int, *, timeout: float,
                        enforce_investor_readonly: bool = True,
                        expected_login: int | None = None,
                        expected_server: str | None = None,
                        require_real: bool = False,
                        listener_check=None) -> dict:
    """Login NYATA ke broker via provider read-only + account_info()
    yang lolos classify_account_info, berbatas waktu [timeout].

    Klasifikasi: ok / auth_failed / master_password / identity_mismatch
    / unverified / not_real / unreachable / timeout / busy.

    - listener_check(): opsional (mode real) — True hanya bila listener
      RPyC di port terminal dimiliki unit customer INI (helper status);
      bila tidak, verifikasi TIDAK dilakukan (tidak pernah berbicara
      dengan terminal milik customer lain).
    - R5: lock runtime dilepas oleh thread verifikasi itu sendiri — bila
      timeout, lock tetap dipegang sampai thread benar-benar selesai.
    Kredensial TIDAK PERNAH masuk hasil maupun log.
    """
    if listener_check is not None:
        try:
            if not listener_check():
                return {"class": "unreachable",
                        "detail": "listener bukan milik terminal customer",
                        "account_info": None}
        except Exception as e:  # noqa: BLE001
            return {"class": "unreachable", "detail": type(e).__name__,
                    "account_info": None}
    try:
        rt = registry.runtime_for(conn, account_id)
    except Exception as e:  # noqa: BLE001 — akun/terminal tidak ada
        return {"class": "unreachable", "detail": f"{type(e).__name__}",
                "account_info": None}

    deadline = time.monotonic() + max(0.05, float(timeout))
    if not rt.lock.acquire(timeout=min(2.0, max(0.1, float(timeout)))):
        return {"class": "busy", "detail": "runtime sibuk",
                "account_info": None}
    out: dict = {}

    def _attempt():
        try:
            try:
                rt.provider.disconnect()
            except Exception:  # noqa: BLE001
                pass
            while True:
                try:
                    rt.provider.connect()
                    break
                except MT5ConnectionError as e:
                    if looks_like_broker_auth_error(str(e)):
                        out["err"] = ("auth_failed", str(e))
                        return
                    if time.monotonic() >= deadline:
                        out["err"] = ("unreachable", str(e))
                        return
                    time.sleep(0.5)
                except MT5Error as e:  # noqa: BLE001
                    out["err"] = ("unreachable", str(e))
                    return
            try:
                out["info"] = rt.provider.account_info()
            except MT5Error as e:
                out["err"] = ("unreachable", str(e))
            finally:
                try:
                    rt.provider.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001 — thread tak boleh meledak
            out["err"] = ("unreachable", type(e).__name__)
        finally:
            try:
                rt.lock.release()
            except RuntimeError:
                pass

    th = threading.Thread(target=_attempt, daemon=True,
                          name=f"live-verify-{account_id}")
    th.start()
    th.join(max(0.1, deadline - time.monotonic()))
    if th.is_alive():
        registry.evict(account_id)
        return {"class": "timeout",
                "detail": "verifikasi melewati batas waktu",
                "account_info": None}
    if "err" in out:
        kind, detail = out["err"]
        registry.evict(account_id)
        return {"class": kind, "detail": str(detail)[:200],
                "account_info": None}
    info = out.get("info")
    klass = classify_account_info(
        info, enforce_investor_readonly=enforce_investor_readonly,
        expected_login=expected_login, expected_server=expected_server,
        require_real=require_real)
    if klass != "ok":
        registry.evict(account_id)
        return {"class": klass, "detail": klass, "account_info": None}
    return {"class": "ok", "detail": "", "account_info": info}

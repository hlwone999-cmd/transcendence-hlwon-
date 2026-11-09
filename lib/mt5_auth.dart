// Transcendence — mt5_auth.dart
// Autentikasi multi-customer (brief §9): connect/logout/me, bind/unbind
// akun MT5, dan penyimpanan token sesi.
//
// REDESIGN JURNAL MT5 (D1/D3): jalur utama customer adalah self-link —
// [connectAccount] (POST /mt5/connect) dengan identitas nomor akun MT5 +
// server broker + password INVESTOR. Email/password portal/token manual
// tidak ada di UI; [login]/[register] tetap dipertahankan untuk API/admin.
// [disconnect] (POST /mt5/disconnect) HANYA mencabut token sesi (D3) —
// riwayat jurnal tidak dihapus; unbind tetap hanya via API/admin.
//
// Token sesi disimpan di flutter_secure_storage (encrypted keystore).
// Token plaintext lama di SharedPreferences hanya DIBACA untuk migrasi
// sekali ke secure storage; token baru TIDAK PERNAH ditulis plaintext —
// tanpa secure storage token hanya hidup di memori proses (remediasi R4).
//
// TIDAK ADA bagian dari file ini yang menyentuh kalkulator lot
// (lib/engine.dart, lib/montecarlo.dart, main.dart — protected).
//
// Remediasi keamanan (hasil security review lib/mt5_auth.dart):
//  - M1: token plaintext lama di SharedPreferences dihapus HANYA setelah
//    migrasi ke secure storage berhasil dan terverifikasi terbaca balik.
//  - M2: HTTP 401 pada panggilan ber-token membersihkan sesi lokal;
//    timeout, kegagalan jaringan, dan 5xx TIDAK menyentuh sesi.
//  - M3: URL HTTP ditolak untuk konfigurasi produksi; HTTP hanya untuk
//    localhost/loopback atau development eksplisit (allowInsecureHttp).
//    Kebijakan M3 kini SATU SUMBER di Mt5UrlPolicy (mt5_api.dart) — dipakai
//    bersama klien auth DAN klien data (remediasi M-A, audit PR #4).
import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'mt5_api.dart';

/// Kontrak penyimpanan token (di-mock di test).
abstract class SessionStoreContract {
  Future<String?> readToken();
  Future<void> saveToken(String token);
  Future<void> clear();
}

/// Penyimpanan token sesi — secure storage; tanpa secure storage token hanya
/// di memori (R4). SharedPreferences hanya dibaca untuk migrasi token lama.
class SessionStore implements SessionStoreContract {
  static const _kToken = 'mt5_session_token';
  static const _kLegacyToken = 'mt5_api_token'; // key lama di SharedPreferences
  final FlutterSecureStorage _sec;

  SessionStore({FlutterSecureStorage? secure})
      : _sec = secure ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  @override
  Future<String?> readToken() async {
    String? t;
    try {
      t = await _sec.read(key: _kToken);
    } catch (_) {
      t = null; // secure storage tidak tersedia -> cek fallback di bawah
    }
    if ((t == null || t.isEmpty) && _memoryToken != null) {
      return _memoryToken; // R4: sesi memori (secure storage tak tersedia)
    }
    if (t != null && t.isNotEmpty) {
      // M1: secure storage sudah memegang sesi — sisa token lama di
      // SharedPreferences (plaintext) tidak diperlukan lagi; bersihkan
      // bila ada. Pembersihan bersifat best-effort.
      try {
        final sp = await SharedPreferences.getInstance();
        if (sp.getString(_kLegacyToken) != null) {
          await sp.remove(_kLegacyToken);
        }
      } catch (_) {
        // gagal membersihkan sisa -> sesi tetap aman di secure storage
      }
      return t;
    }

    final sp = await SharedPreferences.getInstance();
    final legacy = sp.getString(_kLegacyToken);
    if (legacy == null || legacy.isEmpty) return null;

    // M1: migrasi sekali ke secure storage. Token lama (plaintext di
    // SharedPreferences) dihapus HANYA setelah penulisan secure storage
    // berhasil DAN terverifikasi terbaca kembali. Bila penulisan gagal,
    // token lama dipertahankan agar sesi tidak hilang — migrasi dicoba
    // ulang pada readToken() berikutnya.
    try {
      await _sec.write(key: _kToken, value: legacy);
      final verify = await _sec.read(key: _kToken);
      if (verify != legacy) return legacy; // tidak terverifikasi -> simpan
    } catch (_) {
      return legacy; // penulisan gagal -> token lama TIDAK dihapus
    }
    await sp.remove(_kLegacyToken); // migrasi sukses -> hapus plaintext
    return legacy;
  }

  /// Remediasi R4: bila secure storage TIDAK tersedia, token sesi hanya
  /// dipegang di memori proses (hilang saat aplikasi ditutup) — TIDAK
  /// PERNAH ditulis plaintext ke SharedPreferences.
  static String? _memoryToken;

  @override
  Future<void> saveToken(String token) async {
    try {
      await _sec.write(key: _kToken, value: token);
      _memoryToken = null;
    } catch (_) {
      _memoryToken = token.isEmpty ? null : token;
    }
    // sisa token plaintext lama (bila ada) tidak boleh hidup berdampingan
    try {
      final sp = await SharedPreferences.getInstance();
      await sp.remove(_kLegacyToken);
    } catch (_) {
      // best-effort
    }
  }

  @override
  Future<void> clear() async {
    _memoryToken = null;
    try {
      await _sec.delete(key: _kToken);
    } catch (_) {
      // tetap bersihkan fallback
    }
    final sp = await SharedPreferences.getInstance();
    await sp.remove(_kLegacyToken);
  }
}

/// Klien endpoint auth & manajemen akun (/api/v1/auth/*, /api/v1/accounts).
class Mt5AuthApi {
  final String baseUrl;

  /// M3: HTTP (tanpa TLS) hanya untuk localhost/loopback, atau untuk
  /// konfigurasi development yang eksplisit lewat flag ini.
  /// Konfigurasi produksi WAJIB HTTPS. Validasi memakai [Mt5UrlPolicy] —
  /// kebijakan yang sama dengan klien data Mt5Api.
  final bool allowInsecureHttp;

  final http.Client _http;
  final SessionStoreContract store;

  Mt5AuthApi({required String baseUrl, http.Client? client,
              SessionStoreContract? store,
              this.allowInsecureHttp = false})
      : baseUrl = Mt5UrlPolicy.validatedBase(baseUrl,
            allowInsecureHttp: allowInsecureHttp),
        _http = client ?? http.Client(),
        store = store ?? SessionStore();

  Uri _uri(String path) {
    final base =
        baseUrl.startsWith('http') ? baseUrl : 'https://$baseUrl';
    return Uri.parse(
        '${base.trim().replaceAll(RegExp(r'/+$'), '')}/api/v1$path');
  }

  Map<String, String> _headers([String? token]) => {
        'Content-Type': 'application/json',
        if (token != null && token.isNotEmpty) 'X-API-Token': token,
      };

  /// [clearSessionOn401] = true untuk panggilan yang memakai token sesi:
  /// bila server menjawab 401 (token tidak valid/dicabut), sesi lokal
  /// dibersihkan (M2). Panggilan kredensial (login/register) TIDAK
  /// membersihkan sesi — 401 di sana berarti email/password salah,
  /// bukan sesi yang basi.
  Future<dynamic> _send(Future<http.Response> Function() fn,
      {bool clearSessionOn401 = false}) async {
    http.Response r;
    try {
      r = await fn().timeout(const Duration(seconds: 25));
    } catch (_) {
      // M2: timeout / kegagalan jaringan — sesi lokal TIDAK disentuh.
      throw Mt5ApiError(Mt5ApiErrorKind.network, '');
    }
    if (r.statusCode == 401) {
      // M2: token ditolak server -> bersihkan token/sesi lokal supaya
      // token basi tidak terus dipakai ulang (HANYA bila pemanggil
      // memintanya — panggilan kredensial tidak membersihkan).
      if (clearSessionOn401) await store.clear();
      // Redesign D1: 401 pada /mt5/connect berarti verifikasi kredensial
      // gagal — pesan seragam dari server jauh lebih bermanfaat daripada
      // teks generik "Token API ditolak". Bila server tidak mengirim
      // pesan, fallback teks lama tetap dipakai.
      String msg = '';
      try {
        final jj = jsonDecode(utf8.decode(r.bodyBytes));
        if (jj is Map && jj['error'] is Map && jj['error']['message'] is String) {
          msg = jj['error']['message'] as String;
        }
      } catch (_) {// body bukan JSON — pakai fallback
      }
      throw Mt5ApiError(Mt5ApiErrorKind.auth, msg);
    }
    // M2: 5xx / respons rusak di bawah — sesi lokal TIDAK disentuh.
    dynamic j;
    try {
      j = jsonDecode(utf8.decode(r.bodyBytes));
    } catch (_) {
      throw Mt5ApiError(Mt5ApiErrorKind.badResponse,
          'Balasan bukan JSON (status ${r.statusCode})');
    }
    if (r.statusCode >= 400) {
      String msg = 'HTTP ${r.statusCode}';
      if (j is Map && j['error'] is Map && j['error']['message'] is String) {
        msg = j['error']['message'] as String;
      }
      throw Mt5ApiError(Mt5ApiErrorKind.badResponse, msg);
    }
    return j;
  }

  /// Login -> token disimpan aman. Return token.
  Future<String> login(String email, String password) async {
    final j = await _send(() => _http.post(_uri('/auth/login'),
        headers: _headers(),
        body: jsonEncode({'email': email, 'password': password})));
    final token = (j as Map)['token'] as String;
    await store.saveToken(token);
    return token;
  }

  /// HUBUNGKAN AKUN MT5 (D1) — self-link via POST /mt5/connect.
  ///
  /// Identitas = [login] (nomor akun MT5) + [server] + [password]
  /// (password INVESTOR, sesi read-only). Password TIDAK PERNAH
  /// disimpan lokal — hanya dikirim sekali ke server via HTTPS lalu
  /// dibiarkan keluar dari memori (pemanggil mebersihkan controller).
  /// Token sesi hasil server disimpan aman dan di-return.
  ///
  /// Server menjawab:
  ///  - 201 mode=self_link : akun baru (customer dibuat otomatis,
  ///    dibatasi kapasitas max_customers + guard anti-balapan).
  ///  - 200 mode=login     : akun sudah dikenal, sesi diterbitkan lagi.
  ///  - 401/403/409/429    : Mt5ApiError dengan pesan seragam dari server.
  Future<Map<String, dynamic>> connectAccount(
      {required int login,
      required String server,
      required String password}) async {
    final j = await _send(
        () => _http.post(_uri('/mt5/connect'),
            headers: _headers(),
            body: jsonEncode(
                {'login': login, 'server': server, 'password': password})),
        clearSessionOn401: false); // 401 di sini = password salah, bukan
    // sesi basi — jangan bersihkan store (tak ada sesi lama yang sah).
    final map = j as Map<String, dynamic>;
    final token = map['token'] as String;
    await store.saveToken(token);
    return map;
  }

  /// PUTUSKAN KONEKSI (D3) — POST /mt5/disconnect.
  ///
  /// HANYA mencabut token sesi di server + menghapus token lokal.
  /// Riwayat jurnal, akun ter-bind, dan kredensial TIDAK disentuh
  /// (tidak ada pemanggilan unbind di jalur ini).
  Future<void> disconnect(String token) async {
    try {
      await _send(
          () => _http.post(_uri('/mt5/disconnect'),
              headers: _headers(token), body: '{}'),
          clearSessionOn401: true);
    } finally {
      await store.clear();
    }
  }

  Future<void> register(String email, String password,
      {String? displayName}) async {
    await _send(() => _http.post(_uri('/auth/register'),
        headers: _headers(),
        body: jsonEncode({
          'email': email,
          'password': password,
          'display_name': ?displayName,
        })));
  }

  /// Logout: revoke token di server + hapus dari perangkat.
  Future<void> logout(String token) async {
    try {
      await _send(
          () => _http.post(_uri('/auth/logout'),
              headers: _headers(token), body: '{}'),
          clearSessionOn401: true);
    } finally {
      await store.clear();
    }
  }

  /// Bind akun MT5 (MVP: 1 akun per customer). Password MT5 TIDAK
  /// pernah disimpan lokal — langsung dikirim ke server utk dienkripsi.
  Future<Map<String, dynamic>> bindAccount(String token,
      {required int login, required String server, required String password})
      async {
    // _send mengembalikan Future<dynamic> — cast nilainya SETELAH await,
    // bukan cast objek Future-nya (yang selalu melempar TypeError).
    final j = await _send(
        () => _http.post(_uri('/accounts/bind'),
            headers: _headers(token),
            body: jsonEncode(
                {'login': login, 'server': server, 'password': password})),
        clearSessionOn401: true);
    return j as Map<String, dynamic>;
  }

  Future<void> unbindAccount(String token) async {
    await _send(
        () => _http.post(_uri('/accounts/unbind'),
            headers: _headers(token), body: '{}'),
        clearSessionOn401: true);
  }

  Future<Map<String, dynamic>> me(String token) async {
    final j = await _send(
        () => _http.get(_uri('/auth/me'), headers: _headers(token)),
        clearSessionOn401: true);
    return j as Map<String, dynamic>;
  }
}

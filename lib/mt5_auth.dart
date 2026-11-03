// Transcendence — mt5_auth.dart
// Autentikasi multi-customer (brief §9): register/login/logout/me,
// bind/unbind akun MT5, dan penyimpanan token sesi.
//
// Token sesi disimpan di flutter_secure_storage (encrypted keystore).
// Fallback SharedPreferences hanya untuk perangkat sangat lama —
// dilakukan migrasi sekali ke secure storage begitu tersedia.
//
// TIDAK ADA bagian dari file ini yang menyentuh kalkulator lot
// (lib/engine.dart, lib/montecarlo.dart, main.dart — protected).
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

/// Penyimpanan token sesi — secure storage dulu, fallback SharedPreferences.
class SessionStore implements SessionStoreContract {
  static const _kToken = 'mt5_session_token';
  static const _kLegacyToken = 'mt5_api_token'; // key lama di SharedPreferences
  final FlutterSecureStorage _sec;

  SessionStore({FlutterSecureStorage? secure})
      : _sec = secure ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  Future<String?> readToken() async {
    try {
      final t = await _sec.read(key: _kToken);
      if (t != null && t.isNotEmpty) return t;
    } catch (_) {
      // secure storage tidak tersedia -> fallback di bawah
    }
    final sp = await SharedPreferences.getInstance();
    final legacy = sp.getString(_kLegacyToken);
    if (legacy != null && legacy.isNotEmpty) {
      // migrasi sekali ke secure storage
      await saveToken(legacy);
      return legacy;
    }
    return null;
  }

  Future<void> saveToken(String token) async {
    try {
      await _sec.write(key: _kToken, value: token);
    } catch (_) {
      final sp = await SharedPreferences.getInstance();
      await sp.setString(_kLegacyToken, token);
    }
  }

  Future<void> clear() async {
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
  final http.Client _http;
  final SessionStoreContract store;

  Mt5AuthApi({required this.baseUrl, http.Client? client,
              SessionStoreContract? store})
      : _http = client ?? http.Client(),
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

  Future<dynamic> _send(Future<http.Response> Function() fn) async {
    http.Response r;
    try {
      r = await fn().timeout(const Duration(seconds: 25));
    } catch (_) {
      throw Mt5ApiError(Mt5ApiErrorKind.network, '');
    }
    if (r.statusCode == 401) {
      throw Mt5ApiError(Mt5ApiErrorKind.auth, '');
    }
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

  Future<void> register(String email, String password,
      {String? displayName}) async {
    await _send(() => _http.post(_uri('/auth/register'),
        headers: _headers(),
        body: jsonEncode({
          'email': email,
          'password': password,
          if (displayName != null) 'display_name': displayName,
        })));
  }

  /// Logout: revoke token di server + hapus dari perangkat.
  Future<void> logout(String token) async {
    try {
      await _send(() => _http.post(_uri('/auth/logout'),
          headers: _headers(token), body: '{}'));
    } finally {
      await store.clear();
    }
  }

  /// Bind akun MT5 (MVP: 1 akun per customer). Password MT5 TIDAK
  /// pernah disimpan lokal — langsung dikirim ke server utk dienkripsi.
  Future<Map<String, dynamic>> bindAccount(String token,
      {required int login, required String server, required String password}) {
    return _send(() => _http.post(_uri('/accounts/bind'),
        headers: _headers(token),
        body: jsonEncode(
            {'login': login, 'server': server, 'password': password}))) as Future<Map<String, dynamic>>;
  }

  Future<void> unbindAccount(String token) async {
    await _send(() => _http.post(_uri('/accounts/unbind'),
        headers: _headers(token), body: '{}'));
  }

  Future<Map<String, dynamic>> me(String token) {
    return _send(() => _http.get(_uri('/auth/me'), headers: _headers(token)))
        as Future<Map<String, dynamic>>;
  }
}

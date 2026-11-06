// Transcendence — mt5_auth_test.dart
// Test endpoint auth multi-customer di sisi klien (brief §11 #13):
// login menyimpan token, logout menghapus, bind mengirim kredensial
// ke endpoint yang benar, password MT5 TIDAK pernah disimpan lokal.
//
// CATATAN JUJUR: Flutter SDK tidak tersedia di lingkungan implementasi
// (sandbox tanpa flutter/dart) — test ini ditulis untuk CI (GitHub
// Actions) dan WAJIB dijalankan via `flutter test` di sana.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:transcendence/mt5_api.dart';
import 'package:transcendence/mt5_auth.dart';

class _FakeStore implements SessionStoreContract {
  String? token;
  @override
  Future<void> clear() async => token = null;
  @override
  Future<String?> readToken() async => token;
  @override
  Future<void> saveToken(String t) async => token = t;
}

void main() {
  test('login memanggil /auth/login dan menyimpan token', () async {
    final store = _FakeStore();
    final seen = <String>[];
    final client = MockClient((req) async {
      seen.add(req.url.toString());
      expect(req.url.path, endsWith('/api/v1/auth/login'));
      final body = jsonDecode(req.body) as Map<String, dynamic>;
      expect(body['email'], 'budi@test.id');
      return http.Response(
          jsonEncode({'ok': true, 'token': 'SESI123', 'token_type': 'bearer'}),
          200,
          headers: {'content-type': 'application/json'});
    });
    final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.domain.com',
        client: client,
        store: store);
    final tok = await api.login('budi@test.id', 'rahasia123');
    expect(tok, 'SESI123');
    expect(store.token, 'SESI123');
    expect(seen.single, 'https://jurnal.domain.com/api/v1/auth/login');
  });

  test('login gagal 401 -> Mt5ApiError auth', () async {
    final store = _FakeStore();
    final client = MockClient((req) async =>
        http.Response(jsonEncode({
          'error': {'code': 401, 'message': 'Email atau password salah'}
        }), 401));
    final api = Mt5AuthApi(
        baseUrl: 'https://x.id', client: client, store: store);
    await expectLater(api.login('a@b.id', 'salah'),
        throwsA(isA<Mt5ApiError>()));
    expect(store.token, isNull); // tidak ada token tersimpan
  });

  test('bindAccount mengirim login/server/password ke /accounts/bind',
      () async {
    final store = _FakeStore();
    String? sentBody;
    final client = MockClient((req) async {
      sentBody = req.body;
      return http.Response(
          jsonEncode({
            'id': 1, 'login': 11111111, 'server': 'Exness-MT5Real',
          }), 201);
    });
    final api = Mt5AuthApi(
        baseUrl: 'https://x.id', client: client, store: store);
    final r = await api.bindAccount('TOK',
        login: 11111111, server: 'Exness-MT5Real', password: 'MT5PW');
    expect(r['login'], 11111111);
    final body = jsonDecode(sentBody!) as Map<String, dynamic>;
    expect(body['password'], 'MT5PW'); // ke server, bukan ke storage
    expect(store.token, isNull);       // password MT5 tidak menyentuh store
  });

  test('logout menghapus token lokal walau server gagal', () async {
    final store = _FakeStore();
    store.token = 'OLD';
    final client = MockClient((req) async => http.Response('x', 500));
    final api = Mt5AuthApi(
        baseUrl: 'https://x.id', client: client, store: store);
    // Kontrak implementasi: error server DITERUSKAN ke pemanggil, tapi
    // finally menjamin token lokal tetap dihapus walau revoke gagal.
    await expectLater(
        api.logout('OLD'), throwsA(isA<Mt5ApiError>()));
    expect(store.token, isNull); // clear di finally tetap jalan
  });
}

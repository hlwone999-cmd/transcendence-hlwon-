// Transcendence — mt5_auth_test.dart
// Test endpoint auth multi-customer di sisi klien (brief §11 #13):
// login menyimpan token, logout menghapus, bind mengirim kredensial
// ke endpoint yang benar, password MT5 TIDAK pernah disimpan lokal.
//
// Sekaligus test remediasi security review (M1/M2/M3):
//  - M1: migrasi token plaintext -> secure storage (sukses & gagal).
//  - M2: 401 membersihkan sesi lokal; timeout/jaringan/5xx tidak.
//  - M3: URL HTTP produksi ditolak; localhost/dev & HTTPS diizinkan.
//
// Dijalankan dengan `flutter test` (Flutter 3.47.1) — lokal & CI.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

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

/// Fake FlutterSecureStorage — backing in-memory; tidak mewarisi
/// implementasi apa pun (noSuchMethod) supaya tahan beda signature antar
/// versi paket. [failWrite] mensimulasikan kegagalan penulisan (M1);
/// [bacaBed] membuat read mengembalikan nilai lain (verifikasi migrasi
/// gagal — M1).
class _FakeSecure implements FlutterSecureStorage {
  final Map<String, String> backing;
  final bool failWrite;
  final bool bacaBed;
  int _reads = 0;

  _FakeSecure({Map<String, String>? backing,
               this.failWrite = false,
               this.bacaBed = false})
      : backing = backing ?? {};

  @override
  dynamic noSuchMethod(Invocation invocation) {
    final named = invocation.namedArguments;
    final key = named[const Symbol('key')] as String?;
    switch (invocation.memberName) {
      case const Symbol('read'):
        _reads++;
        // bacaBed hanya berlaku untuk bacaan KEDUA dst (verifikasi baca
        // balik setelah migrasi) — bacaan pertama tetap jujur.
        if (bacaBed && _reads > 1) return Future<String?>.value('NILAI-LAIN');
        return Future<String?>.value(backing[key ?? '']);
      case const Symbol('write'):
        if (failWrite) {
          return Future<void>.error(Exception('tulis secure storage gagal'));
        }
        final value = named[const Symbol('value')] as String?;
        if (value == null) {
          backing.remove(key);
        } else {
          backing[key ?? ''] = value;
        }
        return Future<void>.value();
      case const Symbol('delete'):
        backing.remove(key);
        return Future<void>.value();
      default:
        return super.noSuchMethod(invocation);
    }
  }
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

  // ------------------------------------------------ M1: migrasi token
  group('M1 — migrasi token plaintext ke secure storage', () {
    test('migrasi BERHASIL: token lama dihapus dari SharedPreferences',
        () async {
      SharedPreferences.setMockInitialValues(
          {'mt5_api_token': 'PLAINTEXT-LAMA'});
      final secure = _FakeSecure();
      final store = SessionStore(secure: secure);

      final t = await store.readToken();

      expect(t, 'PLAINTEXT-LAMA'); // sesi tetap terbaca
      expect(secure.backing['mt5_session_token'],
          'PLAINTEXT-LAMA'); // pindah ke secure storage
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_api_token'),
          isNull); // plaintext lama dihapus
    });

    test('migrasi GAGAL (secure write error): token lama DIPERTAHANKAN',
        () async {
      SharedPreferences.setMockInitialValues(
          {'mt5_api_token': 'PLAINTEXT-LAMA'});
      final secure = _FakeSecure(failWrite: true);
      final store = SessionStore(secure: secure);

      final t = await store.readToken();

      expect(t, 'PLAINTEXT-LAMA'); // sesi tidak hilang
      expect(secure.backing.containsKey('mt5_session_token'),
          isFalse); // tidak tertulis di secure storage
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_api_token'),
          'PLAINTEXT-LAMA'); // plaintext DIPERTAHANKAN
    });

    test('verifikasi baca balik tidak cocok: token lama DIPERTAHANKAN',
        () async {
      SharedPreferences.setMockInitialValues(
          {'mt5_api_token': 'PLAINTEXT-LAMA'});
      final secure = _FakeSecure(bacaBed: true);
      final store = SessionStore(secure: secure);

      final t = await store.readToken();

      expect(t, 'PLAINTEXT-LAMA');
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_api_token'),
          'PLAINTEXT-LAMA'); // belum divalidasi -> jangan dihapus
    });

    test('secure storage sudah berisi token -> sisa plaintext ikut dibersihkan',
        () async {
      SharedPreferences.setMockInitialValues({'mt5_api_token': 'SISA-LAMA'});
      final secure = _FakeSecure(
          backing: {'mt5_session_token': 'SESI-BARU'});
      final store = SessionStore(secure: secure);

      final t = await store.readToken();

      expect(t, 'SESI-BARU'); // token secure storage yang menang
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_api_token'), isNull);
    });
  });

  // ------------------------------------ M2: 401 vs timeout/jaringan/5xx
  group('M2 — pembersihan sesi pada HTTP 401', () {
    test('me() dapat 401 -> sesi lokal dibersihkan + error auth', () async {
      final store = _FakeStore();
      store.token = 'TOK-BASI';
      final client = MockClient((req) async {
        expect(req.url.path, endsWith('/api/v1/auth/me'));
        return http.Response(
            jsonEncode({
              'error': {'code': 401, 'message': 'Token tidak valid'}
            }),
            401);
      });
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);

      Object? err;
      try {
        await api.me('TOK-BASI');
      } catch (e) {
        err = e;
      }

      expect(err, isA<Mt5ApiError>());
      expect((err as Mt5ApiError).kind, Mt5ApiErrorKind.auth);
      expect(store.token, isNull); // M2: sesi dibersihkan
    });

    test('login salah password (401) TIDAK menghapus sesi lama yang valid',
        () async {
      final store = _FakeStore();
      store.token = 'SESI-MASIH-VALID';
      final client = MockClient((req) async =>
          http.Response(jsonEncode({
            'error': {'code': 401, 'message': 'Email atau password salah'}
          }), 401));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);

      await expectLater(
          api.login('a@b.id', 'salah'), throwsA(isA<Mt5ApiError>()));
      expect(store.token, 'SESI-MASIH-VALID'); // 401 kredensial != sesi basi
    });

    test('timeout TIDAK menghapus sesi', () async {
      final store = _FakeStore();
      store.token = 'TOK';
      final client = MockClient((req) async =>
          throw TimeoutException('simulasi timeout', const Duration(seconds: 25)));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);

      await expectLater(
          api.me('TOK'), throwsA(isA<Mt5ApiError>()));
      expect(store.token, 'TOK'); // M2: sesi tetap ada
    });

    test('kegagalan jaringan TIDAK menghapus sesi', () async {
      final store = _FakeStore();
      store.token = 'TOK';
      final client = MockClient(
          (req) async => throw SocketException('simulasi offline'));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);

      await expectLater(
          api.me('TOK'), throwsA(isA<Mt5ApiError>()));
      expect(store.token, 'TOK'); // M2: sesi tetap ada
    });

    test('HTTP 5xx TIDAK menghapus sesi', () async {
      final store = _FakeStore();
      store.token = 'TOK';
      final client = MockClient((req) async =>
          http.Response(jsonEncode({
            'error': {'code': 500, 'message': 'Kesalahan internal'}
          }), 500));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);

      await expectLater(
          api.me('TOK'), throwsA(isA<Mt5ApiError>()));
      expect(store.token, 'TOK'); // M2: sesi tetap ada
    });
  });

  // ------------------------------------- M3: skema URL produksi vs dev
  group('M3 — penolakan URL HTTP produksi', () {
    test('HTTP ke host produksi DITOLAK', () async {
      final store = _FakeStore();
      Mt5ApiError? caught;
      try {
        Mt5AuthApi(
            baseUrl: 'http://jurnal.domain.com',
            client: MockClient((req) async => http.Response('{}', 200)),
            store: store);
      } on Mt5ApiError catch (e) {
        caught = e;
      }
      expect(caught, isNotNull);
      expect(caught!.kind, Mt5ApiErrorKind.badResponse);
      expect(caught.message, contains('HTTPS'));
    });

    test('HTTP ke localhost dan loopback DIIZINKAN dan berfungsi', () async {
      final store = _FakeStore();
      final seen = <String>[];
      final client = MockClient((req) async {
        seen.add(req.url.toString());
        return http.Response(jsonEncode({'token': 'DEV-TOK'}), 200);
      });
      final api = Mt5AuthApi(
          baseUrl: 'http://localhost:8000',
          client: client,
          store: store);
      final tok = await api.login('dev@x.id', 'pw');
      expect(tok, 'DEV-TOK');
      expect(seen.single,
          'http://localhost:8000/api/v1/auth/login'); // skema http utuh

      // varian loopback lain diterima konstruktor
      expect(
          Mt5AuthApi(
              baseUrl: 'http://127.0.0.1:5000',
              client: client,
              store: store),
          isA<Mt5AuthApi>());
      expect(
          Mt5AuthApi(
              baseUrl: 'http://[::1]:9000', client: client, store: store),
          isA<Mt5AuthApi>());
    });

    test('HTTPS tetap berfungsi — eksplisit maupun host tanpa scheme',
        () async {
      final store = _FakeStore();
      final seen = <String>[];
      final client = MockClient((req) async {
        seen.add(req.url.toString());
        return http.Response(jsonEncode({'token': 'TLS-TOK'}), 200);
      });
      final api = Mt5AuthApi(
          baseUrl: 'https://jurnal.domain.com',
          client: client,
          store: store);
      final tok = await api.login('budi@x.id', 'pw');
      expect(tok, 'TLS-TOK');
      expect(seen.single, 'https://jurnal.domain.com/api/v1/auth/login');

      // host tanpa scheme tetap dianggap https (perilaku lama)
      final api2 = Mt5AuthApi(
          baseUrl: 'jurnal.domain.com', client: client, store: store);
      await api2.login('budi@x.id', 'pw');
      expect(seen.last, startsWith('https://'));
    });

    test('HTTP non-loopback diizinkan hanya dengan flag dev eksplisit',
        () async {
      final store = _FakeStore();
      final seen = <String>[];
      final client = MockClient((req) async {
        seen.add(req.url.toString());
        return http.Response(jsonEncode({'token': 'DEV-EXPLISIT'}), 200);
      });
      final api = Mt5AuthApi(
          baseUrl: 'http://192.168.1.10:8000',
          client: client,
          store: store,
          allowInsecureHttp: true); // development eksplisit
      final tok = await api.login('dev@x.id', 'pw');
      expect(tok, 'DEV-EXPLISIT');
      expect(seen.single, 'http://192.168.1.10:8000/api/v1/auth/login');
    });
  });

  // --------------------------------------------------- redesign D1/D3
  // connectAccount (self-link /mt5/connect) & disconnect (/mt5/disconnect)
  group('connectAccount & disconnect (redesign Jurnal MT5)', () {
    test('connectAccount kirim login/server/password ke /mt5/connect dan '
        'simpan token sesi', () async {
      final store = _FakeStore();
      String? path;
      String? body;
      final client = MockClient((req) async {
        path = req.url.path;
        body = req.body;
        return http.Response(
            jsonEncode({
              'ok': true,
              'mode': 'self_link',
              'token': 'SESI-CONNECT-1',
            }),
            201,
            headers: {'content-type': 'application/json'});
      });
      final api = Mt5AuthApi(
          baseUrl: 'https://jurnal.domain.com', client: client, store: store);
      final res = await api.connectAccount(
          login: 12345678,
          server: 'Exness-MT5Real',
          password: 'investor-pw');
      expect(res['mode'], 'self_link');
      expect(path, '/api/v1/mt5/connect'); // prefix /api/v1 konsisten
      final j = jsonDecode(body!) as Map<String, dynamic>;
      expect(j['login'], 12345678);
      expect(j['server'], 'Exness-MT5Real');
      expect(j['password'], 'investor-pw'); // ke server via HTTPS
      expect(store.token, 'SESI-CONNECT-1'); // token disimpan aman
    });

    test('connectAccount 401 (password salah) -> error, store TIDAK '
        'tertimpa/terhapus', () async {
      final store = _FakeStore();
      store.token = 'SESI-LAMA';
      final client = MockClient((req) async => http.Response(
          jsonEncode({
            'error': {
              'code': 401,
              'message': 'Verifikasi gagal — periksa nomor akun, server, '
                  'dan password investor'
            }
          }), 401));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);
      await expectLater(
          api.connectAccount(
              login: 1, server: 'S', password: 'p'),
          throwsA(isA<Mt5ApiError>()));
      expect(store.token, 'SESI-LAMA'); // tidak tersentuh (bukan sesi basi)
    });

    test('401 tanpa pesan server -> friendly fallback teks generik',
        () async {
      final store = _FakeStore();
      final client = MockClient(
          (req) async => http.Response(jsonEncode({'error': {}}), 401));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);
      try {
        await api.connectAccount(login: 1, server: 'S', password: 'p');
        fail('harusnya lempar');
      } on Mt5ApiError catch (e) {
        expect(e.friendly, contains('Token API ditolak'));
      }
    });

    test('disconnect memanggil /mt5/disconnect; token lokal dihapus',
        () async {
      final store = _FakeStore();
      store.token = 'SESI-AKTIF';
      final seen = <String>[];
      final client = MockClient((req) async {
        seen.add(req.url.toString());
        return http.Response(jsonEncode({'ok': true}), 200,
            headers: {'content-type': 'application/json'});
      });
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);
      await api.disconnect('SESI-AKTIF');
      expect(seen.single, 'https://x.id/api/v1/mt5/disconnect');
      expect(store.token, isNull); // sesi lokal dibersihkan
    });

    test('disconnect: token tetap dihapus walau revoke server gagal',
        () async {
      final store = _FakeStore();
      store.token = 'SESI-AKTIF';
      final client = MockClient((req) async => http.Response('x', 500));
      final api = Mt5AuthApi(
          baseUrl: 'https://x.id', client: client, store: store);
      await expectLater(
          api.disconnect('SESI-AKTIF'), throwsA(isA<Mt5ApiError>()));
      expect(store.token, isNull); // finally tetap membersihkan
    });
  });
}

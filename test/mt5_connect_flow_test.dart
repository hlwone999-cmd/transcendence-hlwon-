// Transcendence — mt5_connect_flow_test.dart
// REGRESI REDESIGN JURNAL MT5 (D1-D4) — menggantikan l2_token_overwrite_test
// (vektor L2 — penimpaan token manual — DIHAPUS BY DESIGN: field token
// manual tidak ada lagi di UI, jadi jalur penimpaan itu tertutup).
//
// Cakupan dua lapis (batasan platform: HTTP dart:io selalu ditunda di
// widget test sehingga connect sukses end-to-end tidak dapat digerakkan
// tanpa injeksi; jalur sukses diuji lewat INJEKSI authApi/api mock):
//
//   LAPISAN WIDGET (halaman asli):
//     W1  boot tanpa sesi -> FORM KONEKSI: hanya 3 input (D1), tanpa
//         ATUR SERVER / URL / email / token manual
//     W2  tombol lihat/sembunyi password investor
//     W3  submit kosong -> validasi lokal, TANPA panggilan jaringan
//     W4  connect sukses (authApi+api injeksi) -> token tersimpan,
//         halaman jurnal terbuka (TERHUBUNG)
//     W5  connect gagal 401 -> pesan seragam, token TIDAK tersimpan,
//         password field dikosongkan (password tidak bertahan di memori)
//     W6  dropdown server: pilih Lainnya -> field manual muncul, nilai
//         manual yang terkirim
//     W7  PUTUSKAN KONEKSI (D3): konfirmasi -> /mt5/disconnect dipanggil,
//         kembali ke form koneksi, store bersih
//     W8  regresi D1: label input lama (URL/email/token) TIDAK ADA
//   LAPISAN UNIT (Mt5AuthApi + SessionStore nyata):
//     U1  connectAccount sukses -> token BARU tersimpan (rotasi sesi)
//     U2  connectAccount 401 -> store tidak tersentuh, error dilempar
//     U3  connectAccount mengirim login/server/password ke /mt5/connect;
//         password TIDAK masuk store
//     U4  disconnect memanggil /mt5/disconnect; store dibersihkan walau
//         server gagal (kontrak finally)
//     U5  token hasil connect bertahan dibaca balik instance SessionStore
//         baru (semantik restart aplikasi)
//
// Lingkungan (flutter_test_config.dart): flutter_secure_storage -> mock
// channel in-memory; SharedPreferences -> mock in-memory.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:transcendence/mt5_api.dart';
import 'package:transcendence/mt5_auth.dart';
import 'package:transcendence/mt5_page.dart';
import 'package:transcendence/mt5_servers.dart';

const _tokenBaru = 'TOKEN-CONNECT-BARU-0123456789abcdef';
const _tokenLama = 'TOKEN-LAMA-BASI-yet-another-stale-token';
const _pwInvestor = 'inv-password-9';

// ---------------------------------------------------------------------------
// Fixture jurnal (dipakai setelah connect sukses) — bentuk backend Flask.
// ---------------------------------------------------------------------------
const _fixtureStatus = {
  'provider': 'mt5linux',
  'enabled': true,
  'connected': true,
  'reason': null,
  'last_sync': {'last_sync_finished_at': '2026-10-20T10:00:00Z'},
  'stored': {'trades': 1, 'deals': 2},
};

const _fixtureAccount = {
  'id': 1,
  'login': 12345678,
  'server': 'Exness-MT5Real',
  'company': 'Exness',
  'currency': 'USD',
  'leverage': 500,
  'balance': 750.0,
  'equity': 750.1,
  'margin_free': 750.1,
};

const _fixtureTrades = {
  'items': [
    {
      'position_id': 101,
      'symbol': 'XAUUSD',
      'direction': 'buy',
      'status': 'closed',
      'volume_in': 0.1,
      'volume_out': 0.1,
      'net_profit': 95.0,
    },
  ],
  'page': 1,
  'page_size': 50,
  'total': 1,
  'pages': 1,
};

const _fixtureStats = {
  'total_closed': 1, 'wins': 1, 'losses': 0, 'breakeven': 0,
  'win_rate': 100.0, 'gross_profit': 95.0, 'gross_loss': 0.0,
  'net_profit': 95.0, 'total_volume': 0.1, 'open_trades': 0,
};

const _fixtureEquity = {
  'points': [],
  'final_net_cum': 95.0,
  'balance_ops_cum': 0.0,
  'max_drawdown': 0.0,
  'max_drawdown_pct': null,
};

http.Response _json(Object j, [int code = 200]) => http.Response(
      jsonEncode(j),
      code,
      headers: {'content-type': 'application/json'},
    );

/// API data mock dengan fixture per-endpoint (refreshAll sukses).
Mt5Api mockDataApi() => Mt5Api(
      baseUrl: 'https://srv.test',
      token: _tokenBaru,
      client: MockClient((r) async {
        final p = r.url.path;
        if (p.endsWith('/mt5/status')) return _json(_fixtureStatus);
        if (p.endsWith('/mt5/account')) return _json(_fixtureAccount);
        if (p.endsWith('/journal/stats')) return _json(_fixtureStats);
        if (p.endsWith('/journal/equity-curve')) return _json(_fixtureEquity);
        if (p.contains('/journal/trades')) return _json(_fixtureTrades);
        if (p.endsWith('/symbols')) {
          return _json({
            'items': [
              {
                'symbol': 'XAUUSD',
                'point': 0.01,
                'digits': 2,
                'pip_size': 0.1
              },
            ],
          });
        }
        return _json({'error': {'message': 'tak dikenal: $p'}}, 404);
      }),
    );

/// Recorder: merekam path + body request dari mock auth client.
class _Recorder {
  final List<String> paths = [];
  Map<String, dynamic>? connectBody;
  Map<String, dynamic>? disconnectHeaders;
}

Mt5AuthApi _recordingAuth(
  _Recorder rec, {
  int connectStatus = 201,
  Map<String, dynamic> connectBody = const {
    'ok': true,
    'mode': 'self_link',
    'token': _tokenBaru,
  },
}) {
  return Mt5AuthApi(
    baseUrl: 'https://srv.test',
    client: MockClient((req) async {
      rec.paths.add(req.url.path);
      if (req.url.path.endsWith('/mt5/connect')) {
        rec.connectBody = jsonDecode(req.body) as Map<String, dynamic>;
        return _json(connectBody, connectStatus);
      }
      if (req.url.path.endsWith('/mt5/disconnect')) {
        return _json({'ok': true});
      }
      return _json({'error': {'message': 'tak dikenal'}}, 404);
    }),
  );
}

Future<void> _pumpForm(WidgetTester tester,
    {Mt5Api? api, Mt5AuthApi? authApi}) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Mt5JournalPage(api: api, authApi: authApi),
    ),
  ));
  await tester.pumpAndSettle();
}

Future<String?> _storedToken() async {
  final sp = await SharedPreferences.getInstance();
  final legacy = sp.getString('mt5_api_token');
  // mock channel secure storage dibaca lewat SessionStore
  final tok = await SessionStore().readToken();
  return tok ?? legacy;
}

Future<void> _resetStorage() async {
  await SessionStore().clear();
  final sp = await SharedPreferences.getInstance();
  await sp.remove('mt5_api_token');
}

/// Isi form koneksi: field urutan TextField yang tampil.
/// [lainnya]=true memilih "Lainnya (isi manual)" di dropdown.
Future<void> _isiForm(WidgetTester tester,
    {String login = '12345678',
    bool lainnya = false,
    String serverManual = 'Exness-MT5Real5',
    String password = _pwInvestor}) async {
  await tester.enterText(find.byType(TextField).at(0), login);
  if (lainnya) {
    await tester.tap(find.text('Exness-MT5Real').first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Lainnya (isi manual)').last);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField).at(1), serverManual);
  }
  // field password = TextField terakhir pada form
  final nFields = tester.widgetList<TextField>(find.byType(TextField)).length;
  await tester.enterText(find.byType(TextField).at(nFields - 1), password);
  await tester.pump();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    await _resetStorage();
  });

  // ======================================================== LAPISAN WIDGET
  group('form koneksi (D1) — widget', () {
    testWidgets('W1: boot tanpa sesi -> hanya 3 input + HUBUNGKAN AKUN MT5, '
        'tanpa ATUR SERVER', (tester) async {
      await _pumpForm(tester);
      expect(find.text('HUBUNGKAN AKUN MT5'), findsOneWidget);
      expect(find.text('NOMOR AKUN MT5'), findsOneWidget);
      expect(find.text('SERVER BROKER'), findsOneWidget);
      expect(find.text('PASSWORD INVESTOR'), findsOneWidget);
      // D1: tidak ada input lama
      expect(find.text('ATUR SERVER'), findsNothing);
      expect(find.text('URL SERVER'), findsNothing);
      expect(find.text('EMAIL CUSTOMER'), findsNothing);
      expect(find.text('TOKEN SESI'), findsNothing);
      // dropdown default berisi server Exness pertama
      expect(find.text('Exness-MT5Real'), findsOneWidget);
    });

    testWidgets('W2: tombol lihat/sembunyi password investor',
        (tester) async {
      await _pumpForm(tester);
      final nFields = tester.widgetList<TextField>(
          find.byType(TextField)).length;
      final passField = tester.widget<TextField>(
          find.byType(TextField).at(nFields - 1));
      expect(passField.obscureText, isTrue); // default tersembunyi

      await tester.tap(find.byIcon(Icons.visibility_outlined));
      await tester.pump();
      final passField2 = tester.widget<TextField>(
          find.byType(TextField).at(nFields - 1));
      expect(passField2.obscureText, isFalse); // terlihat

      await tester.tap(find.byIcon(Icons.visibility_off_outlined));
      await tester.pump();
      final passField3 = tester.widget<TextField>(
          find.byType(TextField).at(nFields - 1));
      expect(passField3.obscureText, isTrue); // tersembunyi lagi
    });

    testWidgets('W3: submit kosong -> validasi lokal, tanpa jaringan',
        (tester) async {
      final rec = _Recorder();
      await _pumpForm(
        tester,
        api: mockDataApi(),
        authApi: _recordingAuth(rec),
      );
      await tester.tap(find.text('HUBUNGKAN AKUN MT5'));
      await tester.pump();
      expect(find.textContaining('wajib'), findsWidgets); // pesan galat
      expect(rec.paths, isEmpty); // TIDAK ada panggilan jaringan
    });

    testWidgets('W4: connect sukses -> token tersimpan, jurnal terbuka',
        (tester) async {
      final rec = _Recorder();
      await _pumpForm(
        tester,
        api: mockDataApi(),
        authApi: _recordingAuth(rec),
      );
      await _isiForm(tester);
      await tester.tap(find.text('HUBUNGKAN AKUN MT5'));
      await tester.pumpAndSettle();

      expect(rec.paths.single, endsWith('/api/v1/mt5/connect'));
      expect(rec.connectBody!['login'], 12345678);
      expect(rec.connectBody!['server'], 'Exness-MT5Real');
      expect(rec.connectBody!['password'], _pwInvestor);
      expect(await _storedToken(), _tokenBaru); // token sesi tersimpan aman

      // halaman jurnal terbuka
      expect(find.text('TERHUBUNG'), findsOneWidget);
      expect(find.text('#12345678'), findsOneWidget);
      expect(find.text('STATISTIK'), findsOneWidget);
      // password TIDAK bertahan di field (dibersihkan setelah sukses)
      final nFields = tester.widgetList<TextField>(
          find.byType(TextField)).length;
      // pada fase ready tidak ada field password lagi (3 field form hilang)
      expect(nFields, 0);
    });

    testWidgets('W5: connect gagal 401 -> pesan seragam, token tidak '
        'tersimpan, password dibersihkan', (tester) async {
      final rec = _Recorder();
      await _pumpForm(
        tester,
        api: mockDataApi(),
        authApi: _recordingAuth(rec,
            connectStatus: 401,
            connectBody: {
              'error': {
                'code': 401,
                'message': 'Verifikasi gagal — periksa nomor akun, server, '
                    'dan password investor',
              }
            }),
      );
      await _isiForm(tester);
      await tester.tap(find.text('HUBUNGKAN AKUN MT5'));
      await tester.pumpAndSettle();

      expect(rec.paths.single, endsWith('/api/v1/mt5/connect'));
      expect(await _storedToken(), isNull); // TIDAK ada token tersimpan
      expect(find.textContaining('Verifikasi gagal'), findsOneWidget);
      // form tetap tampil (bukan layar jurnal)
      expect(find.text('HUBUNGKAN AKUN MT5'), findsOneWidget);
      // password dikosongkan — tidak bertahan di memori controller
      final nFields = tester.widgetList<TextField>(
          find.byType(TextField)).length;
      final passCtrl = tester.widget<TextField>(
          find.byType(TextField).at(nFields - 1)).controller!;
      expect(passCtrl.text, isEmpty);
    });

    testWidgets('W6: pilih Lainnya -> field manual muncul, nilai manual '
        'terkirim', (tester) async {
      final rec = _Recorder();
      await _pumpForm(
        tester,
        api: mockDataApi(),
        authApi: _recordingAuth(rec),
      );
      await _isiForm(tester, lainnya: true);
      // field manual server tampil
      expect(find.text('NAMA SERVER (MANUAL)'), findsOneWidget);
      await tester.tap(find.text('HUBUNGKAN AKUN MT5'));
      await tester.pumpAndSettle();

      expect(rec.connectBody!['server'], 'Exness-MT5Real5');
    });

    testWidgets('W7: PUTUSKAN KONEKSI -> /mt5/disconnect dipanggil, '
        'kembali ke form, store bersih', (tester) async {
      final rec = _Recorder();
      // seed sesi: token tersimpan sebelum boot
      await SessionStore().saveToken(_tokenLama);
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Mt5JournalPage(api: mockDataApi(), authApi:
              _recordingAuth(rec)),
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('TERHUBUNG'), findsOneWidget); // boot restore jalan

      await tester.tap(find.text('PUTUSKAN KONEKSI'));
      await tester.pumpAndSettle();
      // dialog konfirmasi
      expect(find.text('PUTUSKAN KONEKSI?'), findsOneWidget);
      expect(find.textContaining('Riwayat jurnal TETAP tersimpan'),
          findsOneWidget);
      await tester.tap(find.widgetWithText(TextButton, 'PUTUSKAN'));
      // toast snackbar 3 dtk + animasi: bakar timer manual
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pump(const Duration(seconds: 4));
      await tester.pump(const Duration(milliseconds: 100));

      expect(rec.paths, [endsWith('/api/v1/mt5/disconnect')]);
      expect(await _storedToken(), isNull); // token dicabut & dibuang
      // kembali ke form koneksi
      expect(find.text('HUBUNGKAN AKUN MT5'), findsOneWidget);
      expect(find.text('TERHUBUNG'), findsNothing);
    });

    testWidgets('W8: regresi D1 — tidak ada jalur input URL/email/token '
        'manual di seluruh halaman', (tester) async {
      await _pumpForm(tester, api: mockDataApi());
      // label lama yang DIHAPUS dari desain:
      for (final label in ['URL SERVER', 'EMAIL CUSTOMER', 'PASSWORD',
          'TOKEN MANUAL (LANJUTAN)', 'TOKEN SESI', 'ATUR SERVER', 'SERVER']) {
        expect(find.text(label), findsNothing, reason: label);
      }
      // hanya TextField form koneksi (3: login, [manual], password)
      final n = tester.widgetList<TextField>(find.byType(TextField)).length;
      expect(n, 2); // login + password (dropdown bukan TextField)
    });
  });

  // ========================================================== LAPISAN UNIT
  group('connect/disconnect unit (Mt5AuthApi + SessionStore nyata)', () {
    test('U1: connectAccount sukses -> token BARU tersimpan', () async {
      final store = SessionStore();
      await store.saveToken(_tokenLama); // sesi lama pra-ada
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => _json({
              'ok': true,
              'mode': 'login',
              'token': _tokenBaru,
            })),
        store: store,
      );
      final res = await api.connectAccount(
          login: 12345678, server: 'Exness-MT5Real', password: _pwInvestor);
      expect(res['mode'], 'login');
      expect(await store.readToken(), _tokenBaru); // token BARU menang
    });

    test('U2: connectAccount 401 -> error dilempar, store tidak tersentuh',
        () async {
      final store = SessionStore();
      await store.saveToken(_tokenLama);
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => _json({
              'error': {
                'code': 401,
                'message': 'Verifikasi gagal',
              }
            }, 401)),
        store: store,
      );
      await expectLater(
        api.connectAccount(
            login: 1, server: 'S', password: 'p'),
        throwsA(isA<Mt5ApiError>()),
      );
      expect(await store.readToken(), _tokenLama); // utuh
    });

    test('U3: connectAccount mengirim payload ke /mt5/connect; password '
        'tidak masuk store', () async {
      final store = SessionStore();
      String? body;
      String? path;
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async {
          path = req.url.path;
          body = req.body;
          return _json({'ok': true, 'token': _tokenBaru});
        }),
        store: store,
      );
      await api.connectAccount(
          login: 55500011, server: 'Exness-MT5Trial7', password: _pwInvestor);
      expect(path, '/api/v1/mt5/connect'); // prefix /api/v1 konsisten
      final j = jsonDecode(body!) as Map<String, dynamic>;
      expect(j['login'], 55500011);
      expect(j['server'], 'Exness-MT5Trial7');
      expect(j['password'], _pwInvestor); // ke server via HTTPS
      expect(await store.readToken(), _tokenBaru); // token saja yang disimpan
    });

    test('U4: disconnect memanggil /mt5/disconnect; store dibersihkan '
        'walau server gagal', () async {
      final store = SessionStore();
      await store.saveToken(_tokenLama);
      final paths = <String>[];
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async {
          paths.add(req.url.path);
          return http.Response('x', 500); // revoke gagal
        }),
        store: store,
      );
      await expectLater(api.disconnect(_tokenLama), throwsA(isA<Mt5ApiError>()));
      expect(paths.single, '/api/v1/mt5/disconnect');
      expect(await store.readToken(), isNull); // finally tetap bersih
    });

    test('U5: token hasil connect bertahan dibaca balik instance baru '
        '(semantik restart)', () async {
      final store = SessionStore();
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => _json(
            {'ok': true, 'token': _tokenBaru})),
        store: store,
      );
      await api.connectAccount(
          login: 12345678, server: 'Exness-MT5Real', password: _pwInvestor);
      for (var i = 0; i < 3; i++) {
        expect(await SessionStore().readToken(), _tokenBaru);
      }
    });
  });

  // ============================================================== D4 data
  group('mt5_servers (D4)', () {
    test('daftar Exness + Lainnya — Lainnya selalu tersedia di akhir', () {
      expect(kServerOptions.isNotEmpty, isTrue);
      expect(kServerOptions.last.value, kServerLainnya);
      expect(
        kServerOptions.map((o) => o.value).toSet().length,
        kServerOptions.length,
      );
    });

    test('validasi server manual', () {
      expect(validateManualServer(''), isNotNull);
      expect(validateManualServer('   '), isNotNull);
      expect(validateManualServer('Exness-MT5Real5'), isNull);
      expect(validateManualServer('x' * 129), isNotNull);
      expect(validateManualServer('x' * 128), isNull);
    });

    test('labelForServerValue: nilai tak dikenal -> Lainnya', () {
      expect(labelForServerValue('Exness-MT5Real'), 'Exness-MT5Real');
      expect(labelForServerValue('Server-Aneh'), 'Lainnya (isi manual)');
    });
  });
}

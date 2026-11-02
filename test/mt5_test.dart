// Transcendence — mt5_test.dart
// Test Jurnal MT5: parsing model, klien API (MockClient), dan widget halaman
// (tanpa jaringan nyata, tanpa backend).
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/mt5_api.dart';
import 'package:transcendence/mt5_models.dart';
import 'package:transcendence/mt5_page.dart';

// ---------------------------------------------------------------------------
// Fixture JSON — bentuk persis seperti balasan backend Flask.
// ---------------------------------------------------------------------------

const fixtureStatus = {
  'provider': 'mt5linux',
  'enabled': true,
  'connected': true,
  'reason': null,
  'last_sync': {'last_sync_finished_at': '2026-10-20T10:00:00Z'},
  'stored': {'trades': 3, 'deals': 10},
};

const fixtureAccount = {
  'id': 1,
  'login': 12345678,
  'server': 'Exness-MT5Real',
  'company': 'Exness',
  'currency': 'USD',
  'leverage': 500,
  'balance': 750.0,
  'equity': 750.1,
  'margin_free': 750.1,
  'last_sync_at': '2026-10-20T10:00:00Z',
};

const fixtureTrade101 = {
  'position_id': 101,
  'symbol': 'XAUUSD',
  'direction': 'buy',
  'status': 'closed',
  'volume_in': 0.2,
  'volume_out': 0.2,
  'entry_price': 2002.5,
  'exit_price': 2013.75,
  'open_time_utc': '2026-10-01T08:10:00Z',
  'close_time_utc': '2026-10-01T08:40:00Z',
  'profit': 200.0,
  'commission': -4.0,
  'swap': -1.0,
  'fee': 0.0,
  'net_profit': 195.0,
  'sl_initial': 1995.0,
  'tp_initial': 2050.0,
  'exit_reason': 'tp',
  'magic': 777,
  'comment': '',
  'pips': 112.5,
  'pip_distance': 112.5,
  'rr': 1.5,
};

const fixtureTrade105 = {
  'position_id': 105,
  'symbol': 'XAUUSD',
  'direction': 'sell',
  'status': 'open',
  'volume_in': 0.02,
  'volume_out': 0.0,
  'entry_price': 2020.0,
  'exit_price': null,
  'open_time_utc': '2026-10-01T08:50:00Z',
  'close_time_utc': null,
  'profit': 0.0,
  'commission': 0.0,
  'swap': 0.0,
  'fee': 0.0,
  'net_profit': 0.0,
  'sl_initial': 2030.0,
  'tp_initial': 2000.0,
  'exit_reason': null,
  'magic': 777,
  'comment': '',
  'pips': null,
  'pip_distance': null,
  'rr': null, // open + tanpa exit -> RR N/A
};

const fixtureTrades = {
  'items': [fixtureTrade101, fixtureTrade105],
  'page': 1,
  'page_size': 50,
  'total': 2,
  'pages': 1,
};

const fixtureStats = {
  'total_closed': 2,
  'wins': 1,
  'losses': 1,
  'breakeven': 0,
  'win_rate': 50.0,
  'gross_profit': 195.0,
  'gross_loss': 23.0,
  'net_profit': 172.0,
  'profit_factor': 8.48,
  'average_win': 195.0,
  'average_loss': 23.0,
  'expectancy': 86.0,
  'total_volume': 0.7,
  'open_trades': 1,
};

const fixtureEquity = {
  'points': [
    {
      't': '2026-10-01T08:00:00Z',
      'type': 'deposit',
      'label': 'Deposit',
      'net_cum': 0.0,
      'balance_ops_cum': 1000.0,
    },
    {
      't': '2026-10-01T08:40:00Z',
      'type': 'trade',
      'label': 'XAUUSD #101',
      'net_cum': 195.0,
      'balance_ops_cum': 1000.0,
    },
    {
      't': '2026-10-01T09:30:00Z',
      'type': 'trade',
      'label': 'EURUSD #110',
      'net_cum': 172.0,
      'balance_ops_cum': 1000.0,
    },
  ],
  'final_net_cum': 172.0,
  'balance_ops_cum': 1000.0,
  'max_drawdown': 23.0,
  'max_drawdown_pct': 1.92,
  'max_drawdown_at': '2026-10-01T09:30:00Z',
};

http.Response _json(Object j, [int code = 200]) => http.Response(
  jsonEncode(j),
  code,
  headers: {'content-type': 'application/json'},
);

Mt5Api _mockApi(Future<http.Response> Function(Uri, http.Request) handler) {
  return Mt5Api(
    baseUrl: 'https://srv.test',
    token: 'TESTTOKEN',
    client: MockClient((r) async => handler(r.url, r)),
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('model parsing', () {
    test('Mt5Status', () {
      final s = Mt5Status.fromJson(fixtureStatus);
      expect(s.connected, isTrue);
      expect(s.enabled, isTrue);
      expect(s.storedTrades, 3);
      expect(s.provider, 'mt5linux');
    });

    test('Mt5Account', () {
      final a = Mt5Account.fromJson(fixtureAccount);
      expect(a.login, 12345678);
      expect(a.server, 'Exness-MT5Real');
      expect(a.balance, 750.0);
      expect(a.currency, 'USD');
    });

    test('Mt5Trade: closed dengan RR, open tanpa RR (N/A)', () {
      final t = Mt5Trade.fromJson(fixtureTrade101);
      expect(t.positionId, 101);
      expect(t.netProfit, 195.0);
      expect(t.rr, 1.5);
      expect(t.exitReason, 'tp');
      expect(t.isOpen, isFalse);
      final o = Mt5Trade.fromJson(fixtureTrade105);
      expect(o.isOpen, isTrue);
      expect(o.rr, isNull); // RR N/A — tidak mengarang SL
      expect(o.exitPrice, isNull);
    });

    test('Mt5Stats & Mt5Equity', () {
      final s = Mt5Stats.fromJson(fixtureStats);
      expect(s.winRate, 50.0);
      expect(s.profitFactor, 8.48);
      expect(s.netProfit, 172.0);
      final e = Mt5Equity.fromJson(fixtureEquity);
      expect(e.points.length, 3);
      expect(e.finalNetCum, 172.0);
      expect(e.maxDrawdown, 23.0);
    });

    test('parse tahan field null/tipe aneh', () {
      final t = Mt5Trade.fromJson({
        ...fixtureTrade101,
        'pips': '112.5', // string tetap terbaca
        'magic': null,
      });
      expect(t.pips, 112.5);
      expect(t.magic, isNull);
    });
  });

  group('Mt5Api (MockClient)', () {
    test('status ok', () async {
      final api = _mockApi((u, r) async {
        if (u.path.endsWith('/mt5/status')) return _json(fixtureStatus);
        fail('path tak dikenal: ${u.path}');
      });
      final s = await api.status();
      expect(s.connected, isTrue);
    });

    test('header X-API-Token terkirim', () async {
      String? got;
      final api = _mockApi((u, r) async {
        got = r.headers['X-API-Token'];
        return _json(fixtureStatus);
      });
      await api.status();
      expect(got, 'TESTTOKEN');
    });

    test('401 -> auth error', () async {
      final api = _mockApi((u, r) async => _json({'error': {}}, 401));
      await expectLater(api.account(), throwsA(isA<Mt5ApiError>()));
      try {
        await api.account();
      } on Mt5ApiError catch (e) {
        expect(e.kind, Mt5ApiErrorKind.auth);
      }
    });

    test('jaringan gagal -> network error', () async {
      final api = Mt5Api(
        baseUrl: 'https://srv.test',
        token: 't',
        client: MockClient((r) async => throw Exception('down')),
      );
      try {
        await api.status();
        fail('harusnya lempar');
      } on Mt5ApiError catch (e) {
        expect(e.kind, Mt5ApiErrorKind.network);
      }
    });

    test('trades mengirim filter simbol & halaman', () async {
      Uri? captured;
      final api = _mockApi((u, r) async {
        captured = u;
        return _json(fixtureTrades);
      });
      final tl = await api.trades(
        page: 2,
        pageSize: 10,
        symbol: 'XAUUSD',
        from: '2026-09-01',
      );
      expect(captured!.queryParameters['page'], '2');
      expect(captured!.queryParameters['symbol'], 'XAUUSD');
      expect(captured!.queryParameters['from'], '2026-09-01');
      expect(tl.total, 2);
      expect(tl.hasMore, isFalse);
    });

    test('baseUrl trailing slash dirapikan', () {
      final api = Mt5Api(baseUrl: 'https://srv.test///', token: 't');
      expect(api.baseUrl, 'https://srv.test');
    });
  });

  group('Mt5JournalPage (widget)', () {
    testWidgets('belum dikonfigurasi -> tampil ATUR SERVER, tanpa crash', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({});
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: Mt5JournalPage())),
      );
      await tester.pumpAndSettle();
      expect(find.text('BELUM TERHUBUNG'), findsOneWidget);
      expect(find.text('ATUR SERVER'), findsOneWidget);
    });

    testWidgets('terhubung -> akun, statistik, dan daftar trade tampil', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({
        'mt5_base_url': 'https://srv.test',
        'mt5_api_token': 'TESTTOKEN',
      });
      final api = _mockApi((u, r) async {
        final p = u.path;
        if (p.endsWith('/mt5/status')) return _json(fixtureStatus);
        if (p.endsWith('/mt5/account')) return _json(fixtureAccount);
        if (p.endsWith('/journal/stats')) return _json(fixtureStats);
        if (p.endsWith('/journal/equity-curve')) return _json(fixtureEquity);
        if (p.contains('/journal/trades')) return _json(fixtureTrades);
        if (p.endsWith('/symbols')) {
          return _json({
            'items': [
              {'symbol': 'XAUUSD', 'point': 0.01, 'digits': 2, 'pip_size': 0.1},
            ],
          });
        }
        return _json({
          'error': {'message': 'tak dikenal: $p'},
        }, 404);
      });
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: Mt5JournalPage(api: api)),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Jurnal MT5 Otomatis'), findsOneWidget);
      expect(find.text('TERHUBUNG'), findsOneWidget);
      expect(find.text('#12345678'), findsOneWidget);
      expect(find.text('Exness-MT5Real'), findsOneWidget);
      expect(find.text('STATISTIK'), findsOneWidget);
      expect(find.text('KURVA P/L TRADING (NET)'), findsOneWidget);
      // trade list tampil: simbol + arah + RR badge
      expect(find.text('XAUUSD'), findsWidgets);
      expect(find.text('BUY'), findsOneWidget);
      expect(find.text('SELL'), findsOneWidget);
      expect(find.text('OPEN'), findsOneWidget);
      expect(find.text('RR 1.50'), findsOneWidget);
      expect(find.text('MUAT LAGI'), findsNothing); // 1 halaman saja
    });

    testWidgets('server 401 -> pesan ramah, bukan crash', (tester) async {
      SharedPreferences.setMockInitialValues({
        'mt5_base_url': 'https://srv.test',
        'mt5_api_token': 'SALAH',
      });
      final api = _mockApi((u, r) async => _json({'error': {}}, 401));
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: Mt5JournalPage(api: api)),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('GAGAL TERHUBUNG'), findsOneWidget);
      expect(find.textContaining('Token API ditolak'), findsOneWidget);
      expect(find.text('COBA LAGI'), findsOneWidget);
    });
  });
}

// Transcendence — phase11_test.dart
// Test Phase 1.1 (UI/UX & statistik): formatter uang dinamis, volume lot,
// Net P/L penuh, DD% N/A tanpa penyebut valid, panduan rentang invalid,
// anti-race epoch, dan binding label interval.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/engine.dart';
import 'package:transcendence/mt5_api.dart';
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

Map<String, dynamic> accountCur(String cur) => {
  'id': 1,
  'login': 12345678,
  'server': 'Exness-MT5Real',
  'company': 'Exness',
  'currency': cur,
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
  'rr': null,
};

const fixtureTrades = {
  'items': [fixtureTrade101, fixtureTrade105],
  'page': 1,
  'page_size': 50,
  'total': 2,
  'pages': 1,
};

const fixtureDetail = {
  'trade': fixtureTrade101,
  'deals': [
    {
      'ticket': 9001,
      'type_name': 'buy',
      'entry_name': 'in',
      'volume': 0.2,
      'price': 2002.5,
      'profit': 0.0,
      'time_utc': '2026-10-01T08:10:00Z',
    },
  ],
  'orders': [],
};

Map<String, dynamic> statsNet(double net, {double volume = 0.7}) => {
  'total_closed': 2,
  'wins': 1,
  'losses': 1,
  'breakeven': 0,
  'win_rate': 50.0,
  'gross_profit': 195.0,
  'gross_loss': 23.0,
  'net_profit': net,
  'profit_factor': 8.48,
  'average_win': 195.0,
  'average_loss': 23.0,
  'expectancy': 86.0,
  'total_volume': volume,
  'open_trades': 1,
};

Map<String, dynamic> equityPct(double? pct, {double dd = 23.0}) => {
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
  'max_drawdown': dd,
  'max_drawdown_pct': pct,
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

/// Handler standar: parameter mata uang & nilai stats/equity bisa diatur.
Mt5Api _stdApi({
  String currency = 'USD',
  double net = 172.0,
  double volume = 0.7,
  double? ddPct = 1.92,
  double dd = 23.0,
}) {
  return _mockApi((u, r) async {
    final p = u.path;
    if (p.endsWith('/mt5/status')) return _json(fixtureStatus);
    if (p.endsWith('/mt5/account')) return _json(accountCur(currency));
    if (p.endsWith('/journal/stats')) {
      return _json(statsNet(net, volume: volume));
    }
    if (p.endsWith('/journal/equity-curve')) {
      return _json(equityPct(ddPct, dd: dd));
    }
    if (p.contains('/journal/trades/')) return _json(fixtureDetail);
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
}

Future<void> _pumpPage(WidgetTester tester, Mt5Api api) async {
  SharedPreferences.setMockInitialValues({
    'mt5_base_url': 'https://srv.test',
    'mt5_api_token': 'TESTTOKEN',
  });
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(body: Mt5JournalPage(api: api)),
    ),
  );
  await tester.pumpAndSettle();
}

dynamic _stateOf(WidgetTester tester) =>
    tester.state<State<Mt5JournalPage>>(find.byType(Mt5JournalPage));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  // ---------------------------------------------------------------- unit
  group('fmtMoney — uang dinamis per mata uang akun (Temuan 1 & 2)', () {
    test('IDR: nilai penuh, tidak dipotong ke rb/jt, tanda jelas', () {
      expect(fmtMoney(22036.79, 'IDR', signed: true), '+Rp22.036,79');
      expect(
        fmtMoney(-1564837.89, 'IDR', signed: true),
        '\u2212Rp1.564.837,89',
      );
      expect(fmtMoney(22036.79, 'IDR'), 'Rp22.036,79');
    });

    test('USD: tanpa awalan Rp ganda (dahulu "Rp346rb USD")', () {
      expect(fmtMoney(172.0, 'USD', signed: true), '+\$172,00');
      expect(fmtMoney(-23.0, 'USD'), '\u2212\$23,00');
      expect(fmtMoney(750.0, 'USD'), '\$750,00');
      expect(fmtMoney(172.0, 'USD', signed: true).contains('Rp'), isFalse);
    });

    test('EUR & mata uang lain memakai simbol sendiri', () {
      expect(fmtMoney(12.5, 'EUR', signed: true), '+\u20ac12,50');
      expect(fmtMoney(9.99, 'MYR'), 'RM9,99');
    });

    test('zero-decimal (JPY) tanpa pecahan sen', () {
      expect(fmtMoney(1234.0, 'JPY', signed: true), '+\u00a51.234');
    });

    test('kode tak dikenal: prefiks kode (jelas, tidak menyesatkan)', () {
      expect(fmtMoney(12.5, 'XYZ', signed: true), '+XYZ 12,50');
      expect(fmtMoney(12.5, 'xyz'), 'XYZ 12,50');
    });

    test('null/NaN/kosong -> en dash', () {
      expect(fmtMoney(null, 'USD'), '\u2013');
      expect(fmtMoney(double.nan, 'USD'), '\u2013');
      expect(fmtMoney(1.0, ''), '\u2013');
    });

    test('lowercase tetap terbaca (dinamis dari backend)', () {
      expect(fmtMoney(5.0, 'idr'), 'Rp5,00');
      expect(fmtMoney(5.0, 'usd', signed: true), '+\$5,00');
    });
  });

  group('fmtLots — volume lot, bukan uang (Temuan 3)', () {
    test('contoh pengguna: 2,17 lot; tanpa pembulatan merugikan', () {
      expect(fmtLots(2.17), '2,17');
      expect(fmtLots(0.7), '0,7'); // dahulu shortIdr -> "Rp1"
      expect(fmtLots(0.02), '0,02'); // dahulu shortIdr -> "Rp0"
    });

    test('nol akhir dipangkas, presisi 3 desimal dipertahankan', () {
      expect(fmtLots(2.0), '2');
      expect(fmtLots(0.0), '0');
      expect(fmtLots(2.175), '2,175'); // broker micro-step 0.001 tidak hilang
      expect(fmtLots(12.3456), '12,346');
    });

    test('negatif & null', () {
      expect(fmtLots(-1.5), '-1,5');
      expect(fmtLots(null), '\u2013');
      expect(fmtLots(double.infinity), '\u2013');
    });
  });

  group('two — 2 desimal koma konsisten (Temuan 7)', () {
    test('RR & profit factor gaya id-ID', () {
      expect(two(8.48), '8,48');
      expect(two(1.5), '1,50');
      expect(two(null), '\u2013');
    });
  });

  // -------------------------------------------------------------- widget
  group('Phase 1.1 — Mt5JournalPage (widget)', () {
    testWidgets('mata uang dinamis USD: NET P/L & balance tanpa Rp', (
      tester,
    ) async {
      await _pumpPage(tester, _stdApi(currency: 'USD'));
      // NET P/L (grid) + Net kumulatif (footer kurva) — sama-sama penuh
      expect(find.text('+\$172,00'), findsNWidgets(2));
      expect(find.text('\$750,00'), findsOneWidget); // BALANCE penuh
      expect(find.textContaining('Rp'), findsNothing);
    });

    testWidgets('regresi Oktober: +Rp22.036,79 tampil PENUH (IDR akun)', (
      tester,
    ) async {
      await _pumpPage(
        tester,
        _stdApi(currency: 'IDR', net: 22036.79, dd: 22036.79),
      );
      expect(find.text('+Rp22.036,79'), findsOneWidget);
      expect(find.textContaining('Rp22rb'), findsNothing);
      expect(find.textContaining('Rp22,0jt'), findsNothing);
    });

    testWidgets('regresi September: −Rp1.564.837,89 tampil PENUH tanpa '
        'terpotong (sel FittedBox, bukan ellipsis)', (tester) async {
      tester.view.physicalSize = const Size(412, 915);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      await _pumpPage(
        tester,
        _stdApi(currency: 'IDR', net: -1564837.89, dd: 1564837.89),
      );
      expect(find.text('\u2212Rp1.564.837,89'), findsOneWidget);
      // sel statistik memakai FittedBox (skala turun), BUKAN ellipsis
      expect(
        find.byWidgetPredicate(
          (w) => w is FittedBox && w.fit == BoxFit.scaleDown,
        ),
        findsWidgets,
      );
    });

    testWidgets('volume tampil sebagai LOT di grid, tile, dan detail', (
      tester,
    ) async {
      await _pumpPage(tester, _stdApi(volume: 0.7));
      expect(find.text('0,7 lot'), findsOneWidget); // grid VOLUME
      expect(find.textContaining('0,2/0,2 lot'), findsOneWidget); // tile
      expect(
        find.text('Volume = total lot keluar (sisi close).'),
        findsOneWidget,
      ); // definisi eksplisit
      expect(find.textContaining('Rp0'), findsNothing); // dahulu "Rp0"
      expect(find.textContaining('Rp1'), findsNothing); // dahulu 0.7 -> "Rp1"

      // detail sheet: in / out (tile di bawah lipatan — scroll dulu)
      await tester.ensureVisible(find.text('XAUUSD').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('XAUUSD').first);
      await tester.pumpAndSettle();
      expect(find.text('0,2 / 0,2 lot'), findsOneWidget);
      expect(find.textContaining('Rp'), findsNothing);
    });

    testWidgets('DD% dengan penyebut valid: nominal + persen berdampingan', (
      tester,
    ) async {
      await _pumpPage(tester, _stdApi(ddPct: 1.92, dd: 23.0));
      expect(find.text('DD CLOSED-P/L 1,9%'), findsOneWidget);
      expect(find.textContaining('(1,9%)'), findsOneWidget);
      expect(find.text('MAX DD \u00b7 CLOSED P/L'), findsOneWidget);
    });

    testWidgets('DD% tanpa penyebut valid (puncak <= 0): N/A, BUKAN 0%', (
      tester,
    ) async {
      await _pumpPage(tester, _stdApi(ddPct: null, dd: 500.0));
      expect(find.text('DD CLOSED-P/L N/A'), findsOneWidget);
      expect(find.textContaining('(N/A)'), findsOneWidget);
      expect(find.text('DD CLOSED-P/L 0,0%'), findsNothing);
      expect(find.textContaining('(0,0%)'), findsNothing);
    });

    testWidgets('rentang custom invalid: PANDUAN tampil, statistik lama '
        'TIDAK ditampilkan (Temuan 5)', (tester) async {
      await _pumpPage(tester, _stdApi());
      expect(find.text('STATISTIK'), findsOneWidget);

      final state = _stateOf(tester);
      await (state as dynamic).applyCustomRange(null, null);
      await tester.pumpAndSettle();

      expect(find.text('RENTANG TANGGAL BELUM LENGKAP'), findsOneWidget);
      expect(find.textContaining('[mulai, akhir + 1 hari)'), findsOneWidget);
      // data lama tidak boleh tampil apa adanya
      expect(find.text('STATISTIK'), findsNothing);
      expect(find.text('+\$172,00'), findsNothing);

      // pemulihan: pilih preset lain -> data dimuat lagi
      await tester.tap(find.text('7 HARI'));
      await tester.pumpAndSettle();
      expect(find.text('STATISTIK'), findsOneWidget);
      expect(find.text('+\$172,00'), findsWidgets);
    });

    testWidgets('label interval terikat pada fetch terakhir (bukan '
        'dihitung ulang saat build)', (tester) async {
      await _pumpPage(tester, _stdApi());
      expect(find.textContaining('30 hari terakhir'), findsOneWidget);
      expect(find.textContaining('(batas akhir eksklusif)'), findsOneWidget);
    });

    testWidgets('anti-race: respons refresh lama yang selesai belakangan '
        'DIBUANG (epoch)', (tester) async {
      final gate = Completer<void>();
      var statsCalls = 0;
      final api = _mockApi((u, r) async {
        final p = u.path;
        if (p.endsWith('/mt5/status')) return _json(fixtureStatus);
        if (p.endsWith('/mt5/account')) return _json(accountCur('USD'));
        if (p.endsWith('/journal/stats')) {
          statsCalls++;
          if (statsCalls == 1) {
            await gate.future; // refresh pertama macet di sini
            return _json(statsNet(111.0));
          }
          return _json(statsNet(777.0));
        }
        if (p.endsWith('/journal/equity-curve')) {
          return _json(equityPct(1.92));
        }
        if (p.contains('/journal/trades')) return _json(fixtureTrades);
        if (p.endsWith('/symbols')) {
          return _json({'items': []});
        }
        return _json({
          'error': {'message': 'tak dikenal: $p'},
        }, 404);
      });
      await _pumpPage(tester, api);

      final state = _stateOf(tester);
      // refresh #1 (akan macet di stats), lalu refresh #2 selesai duluan
      final f1 = (state as dynamic).refreshAll() as Future<void>;
      final f2 = (state as dynamic).refreshAll() as Future<void>;
      await tester.pumpAndSettle();
      expect(find.text('+\$777,00'), findsOneWidget);

      gate.complete(); // respons #1 datang TERAKHIR
      await f1;
      await f2;
      await tester.pumpAndSettle();
      // yang tampil tetap milik refresh terakhir — 111 tidak bocor
      expect(find.text('+\$111,00'), findsNothing);
      expect(find.text('+\$777,00'), findsOneWidget);
    });

    testWidgets('anti-race loadMore: halaman-2 rentang lama tidak menyusul '
        'setelah refresh rentang baru', (tester) async {
      final gate = Completer<void>();
      final page2 = {
        'items': [
          {
            ...fixtureTrade101,
            'position_id': 999,
            'symbol': 'GBPJPY',
            'net_profit': 5.0,
          },
        ],
        'page': 2,
        'page_size': 50,
        'total': 3,
        'pages': 2,
      };
      final trades2pages = {...fixtureTrades, 'pages': 2, 'total': 3};
      final api = _mockApi((u, r) async {
        final p = u.path;
        if (p.contains('/journal/trades/') && p.endsWith('/999')) {
          return _json(fixtureDetail);
        }
        if (p.contains('/journal/trades') && u.queryParameters['page'] == '2') {
          await gate.future; // halaman-2 sengaja ditunda
          return _json(page2);
        }
        if (p.endsWith('/mt5/status')) return _json(fixtureStatus);
        if (p.endsWith('/mt5/account')) return _json(accountCur('USD'));
        if (p.endsWith('/journal/stats')) return _json(statsNet(172.0));
        if (p.endsWith('/journal/equity-curve')) return _json(equityPct(1.92));
        if (p.contains('/journal/trades')) return _json(trades2pages);
        if (p.endsWith('/symbols')) return _json({'items': []});
        return _json({
          'error': {'message': 'tak dikenal: $p'},
        }, 404);
      });
      await _pumpPage(tester, api);
      expect(find.text('MUAT LAGI'), findsOneWidget);

      // klik muat-lagi (macet di gate), lalu refresh baru selesai duluan
      await tester.tap(find.text('MUAT LAGI'));
      await tester.pump(); // jangan settle — gate masih ditutup
      final state = _stateOf(tester);
      final f2 = (state as dynamic).refreshAll() as Future<void>;
      await tester.pumpAndSettle();
      await f2;
      expect(find.text('GBPJPY'), findsNothing);

      gate.complete(); // halaman-2 lama datang belakangan
      await tester.pumpAndSettle();
      // epoch lama dibuang: tidak ada append dari rentang lama
      expect(find.text('GBPJPY'), findsNothing);
      expect(find.text('XAUUSD'), findsWidgets);
    });
  });
}

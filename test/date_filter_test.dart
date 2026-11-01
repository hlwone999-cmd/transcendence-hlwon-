// Transcendence — date_filter_test.dart
// Test Phase 1B: preset tanggal rolling/kalender/custom + zona waktu
// (UTC & Asia/Jakarta) + batas interval [from, to) + propagasi ke API.
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:transcendence/mt5_api.dart';
import 'package:transcendence/mt5_filters.dart';

DateTime _utc(int y, int m, int d, [int h = 0, int min = 0, int s = 0]) =>
    DateTime.utc(y, m, d, h, min, s);

void main() {
  // "sekarang" tetap: 2 Nov 2026 03:00 UTC = 2 Nov 10:00 WIB
  final now = _utc(2026, 11, 2, 3, 0, 0);
  const wib = AppTimeZone.jakarta;
  const utc = AppTimeZone.utc;

  group('rolling periods', () {
    test('7 hari: [now-7d, now) eksklusif', () {
      final r = rangeFor(DatePreset.d7, utc, now)!;
      expect(r.fromUtc, '2026-10-26T03:00:00Z');
      expect(r.toUtc, '2026-11-02T03:00:00Z');
    });

    test('30 hari & 90 hari konsisten durasi', () {
      final r30 = rangeFor(DatePreset.d30, utc, now)!;
      final r90 = rangeFor(DatePreset.d90, utc, now)!;
      expect(r30.fromUtc, '2026-10-03T03:00:00Z');
      expect(r90.fromUtc, '2026-08-04T03:00:00Z');
      expect(r30.toUtc, r90.toUtc);
    });
  });

  group('calendar months', () {
    test('Bulan ini (UTC): 1 Nov 00:00 – 1 Des 00:00', () {
      final r = rangeFor(DatePreset.thisMonth, utc, now)!;
      expect(r.fromUtc, '2026-11-01T00:00:00Z');
      expect(r.toUtc, '2026-12-01T00:00:00Z');
    });

    test('Bulan ini (WIB): 31 Okt 17:00Z – 30 Nov 17:00Z', () {
      final r = rangeFor(DatePreset.thisMonth, wib, now)!;
      expect(r.fromUtc, '2026-10-31T17:00:00Z');
      expect(r.toUtc, '2026-11-30T17:00:00Z');
    });

    test(
      'Bulan lalu (WIB): 30 Sep 17:00Z – 31 Okt 17:00Z (bulan Okt penuh)',
      () {
        final r = rangeFor(DatePreset.lastMonth, wib, now)!;
        expect(r.fromUtc, '2026-09-30T17:00:00Z');
        expect(r.toUtc, '2026-10-31T17:00:00Z');
      },
    );

    test('bulan lalu & bulan ini tidak tumpang tindih (WIB)', () {
      final prev = rangeFor(DatePreset.lastMonth, wib, now)!;
      final cur = rangeFor(DatePreset.thisMonth, wib, now)!;
      expect(prev.toUtc, cur.fromUtc); // batas sama, to eksklusif
    });

    test('Desember -> Januari tahun berikutnya', () {
      final r = rangeFor(DatePreset.thisMonth, utc, _utc(2026, 12, 15, 10))!;
      expect(r.fromUtc, '2026-12-01T00:00:00Z');
      expect(r.toUtc, '2027-01-01T00:00:00Z');
    });

    test('Januari -> bulan lalu = Desember tahun sebelumnya', () {
      final r = rangeFor(DatePreset.lastMonth, utc, _utc(2026, 1, 15, 10))!;
      expect(r.fromUtc, '2025-12-01T00:00:00Z');
      expect(r.toUtc, '2026-01-01T00:00:00Z');
    });
  });

  group('custom range', () {
    test('1-31 Okt 2026 (WIB) = [30 Sep 17:00Z, 31 Okt 17:00Z)', () {
      final r = rangeFor(
        DatePreset.custom,
        wib,
        now,
        customStart: DateTime(2026, 10, 1),
        customEnd: DateTime(2026, 10, 31),
      )!;
      expect(r.fromUtc, '2026-09-30T17:00:00Z');
      expect(r.toUtc, '2026-10-31T17:00:00Z');
    });

    test('belum lengkap -> null', () {
      expect(rangeFor(DatePreset.custom, wib, now), isNull);
      expect(
        rangeFor(
          DatePreset.custom,
          wib,
          now,
          customStart: DateTime(2026, 10, 1),
        ),
        isNull,
      );
    });

    test('mulai > akhir -> null (invalid)', () {
      expect(
        rangeFor(
          DatePreset.custom,
          wib,
          now,
          customStart: DateTime(2026, 10, 10),
          customEnd: DateTime(2026, 10, 1),
        ),
        isNull,
      );
    });
  });

  group('timezone conversion', () {
    test('1 Nov 00:00 WIB == 31 Okt 17:00 UTC', () {
      final u = wib.localToUtc(DateTime(2026, 11, 1, 0, 0)).toUtc();
      expect(isoUtc(u), '2026-10-31T17:00:00Z');
    });

    test('utcToLocal membalik konversi', () {
      final wall = wib.utcToLocal(_utc(2026, 11, 2, 3, 0, 0));
      expect(wall.day, 2);
      expect(wall.hour, 10);
    });

    test('isoUtc memformat dengan padding', () {
      expect(isoUtc(_utc(2026, 3, 5, 7, 8, 9)), '2026-03-05T07:08:09Z');
    });

    test('semua zona didukung tanpa DST (offset tetap)', () {
      for (final z in AppTimeZone.supported) {
        expect(z.offsetMinutes % 60, 0);
      }
    });
  });

  group('propagasi filter ke API', () {
    late Uri? captured;
    late Map<String, String> capturedHeaders;

    setUp(() {
      captured = null;
      capturedHeaders = {};
    });

    Mt5Api apiWith(http.Client client) =>
        Mt5Api(baseUrl: 'https://x.test', token: 'TOK', client: client);

    test('trades mengirim from/to/to_exclusive', () async {
      final api = apiWith(
        MockClient((req) async {
          captured = req.url;
          capturedHeaders = req.headers;
          return http.Response(
            '{"items":[],"page":1,"page_size":50,'
            '"total":0,"pages":0}',
            200,
          );
        }),
      );
      await api.trades(
        page: 1,
        from: '2026-10-01T00:00:00Z',
        to: '2026-11-01T00:00:00Z',
        toExclusive: true,
      );
      expect(captured!.queryParameters['from'], '2026-10-01T00:00:00Z');
      expect(captured!.queryParameters['to'], '2026-11-01T00:00:00Z');
      expect(captured!.queryParameters['to_exclusive'], '1');
      expect(capturedHeaders['X-API-Token'], 'TOK');
      expect(captured!.path, '/api/v1/journal/trades');
    });

    test('stats & equityCurve mengirim filter sama', () async {
      final api = apiWith(
        MockClient((req) async {
          captured = req.url;
          return http.Response('{}', 200);
        }),
      );
      await api.stats(
        from: '2026-09-30T17:00:00Z',
        to: '2026-10-31T17:00:00Z',
        toExclusive: true,
      );
      expect(captured!.path, '/api/v1/journal/stats');
      expect(captured!.queryParameters['to_exclusive'], '1');
      await api.equityCurve(from: 'a', to: 'b');
      expect(captured!.path, '/api/v1/journal/equity-curve');
      expect(captured!.queryParameters['to_exclusive'], isNull);
    });

    test('reconciliation endpoint terpanggil dengan filter', () async {
      final api = apiWith(
        MockClient((req) async {
          captured = req.url;
          return http.Response('{"from_deals":{}}', 200);
        }),
      );
      await api.reconciliation(from: 'a', to: 'b', toExclusive: true);
      expect(captured!.path, '/api/v1/journal/reconciliation');
      expect(captured!.queryParameters['to_exclusive'], '1');
    });
  });
}

// Unit test paritas: memastikan port Dart menghasilkan angka yang PERSIS sama
// dengan versi web (script.js). Nilai ekspektasi diambil dari perilaku web.
import 'package:flutter_test/flutter_test.dart';
import 'package:transcendence/engine.dart';
import 'package:transcendence/montecarlo.dart';

void main() {
  group('parseAmt — parser angka fleksibel (sama seperti web)', () {
    test('format Indonesia & singkatan', () {
      expect(parseAmt('10.000.000', isMoney: true), 10000000);
      expect(parseAmt('10jt'), 10000000);
      expect(parseAmt('1,5jt'), 1500000);
      expect(parseAmt('150rb'), 150000);
      expect(parseAmt('10,000,000', isMoney: true), 10000000);
      expect(parseAmt('2'), 2);
      expect(parseAmt('17.934', isMoney: true), 17934);
      expect(parseAmt('25'), 25);
      expect(parseAmt('25,5'), 25.5);
      expect(parseAmt('abc'), isNull);
      expect(parseAmt(''), isNull);
    });
  });

  group('computeCalc — algoritma lot (rumus web: SL = RiskUSD/(Lot×10))', () {
    test('kasus default web: 10jt, kurs 17.934, risk 2%, rasio 2, SL 25', () {
      final c = computeCalc(10000000, 17934, 2, 2, 25);
      expect(c.ok, isTrue);
      expect(c.riskIdr, 200000);
      expect(c.riskUsd, closeTo(200000 / 17934, 1e-12));
      // pick = lot terbesar dengan SL >= 25 pips → 0.04 lot (27,9 pips)
      expect(kLots[c.pick], 0.04);
      final row = c.rows[c.pick];
      expect(row.sl, 27.9);
      expect(row.tp, 55.8); // 27.9 × 2
      expect(row.r, (27.9 * 0.04 * 10 * 17934).round()); // ≈ 200.199
    });

    test('lot 2.19 muncul untuk Rules 35 + SL 25 (fitur perluasan 10.00)', () {
      // Rules 35: balance Rp 98.444.705, risk 10% → riskUsd ≈ 549
      final c = computeCalc(98444705, 17934, 10, 2, 25);
      final exact = c.riskUsd / (25 * 10);
      final lot = (exact * 100 + 1e-9).floorToDouble() / 100;
      expect(lot, 2.19); // sebelumnya mentok tabel 1.50
      expect(kLots.length, 1000);
      expect(kLots.last, 10.0);
    });

    test('baris tabel: lot 0.01 → SL paling longgar', () {
      final c = computeCalc(10000000, 17934, 2, 2, 25);
      expect(c.rows.first.lot, 0.01);
      expect(c.rows.first.sl, round1dec(c.riskUsd / 0.1));
    });
  });

  group('computeAuto — kotak lot otomatis + peringatan 10.00', () {
    test('normal', () {
      final c = computeCalc(10000000, 17934, 10, 2, 25);
      final a = computeAuto(25, 10000000, 17934, 10, 2, c.riskUsd);
      expect(a.big, '0.22');
      expect(a.bad, isFalse);
      expect(a.sub, contains('lot presisi'));
    });

    test('peringatan bila kebutuhan > 10.00 lot', () {
      final c = computeCalc(98444705, 17934, 10, 2, 5);
      final a = computeAuto(5, 98444705, 17934, 10, 2, c.riskUsd);
      expect(a.big, '10.00+');
      expect(a.bad, isTrue);
      expect(a.sub, contains('melebihi batas 10.00 lot'));
      expect(a.sub, contains('SL minimal 5,5 pips')); // riskUsd/100 dibulatkan atas
    });

    test('belum cukup untuk 0.01 lot', () {
      final a = computeAuto(25, 50000, 17934, 1, 2, 50000 * 0.01 / 17934);
      expect(a.big, '0.00');
      expect(a.sub, startsWith('Belum cukup untuk 0.01 lot'));
    });

    test('plan kosong → empty state', () {
      final a = computeAuto(null, 10000000, 17934, 2, 2, 11.15);
      expect(a.empty, isTrue);
      expect(a.big, '–');
    });
  });

  group('Rules Recovery — data tabel PDF (sama seperti web)', () {
    test('rules 1: X recovery 11,11% balik ke balance awal', () {
      final d = ruleData(1);
      expect(d.b, 200000);
      expect(d.after, 180000); // 90% balance
      expect(d.diff, 20000);
      expect(d.x, closeTo(11.1111, 0.001));
      expect(d.prev, isNull);
    });

    test('rules 2: X recovery 7,41%', () {
      final d = ruleData(2);
      expect(d.b, 240000);
      expect(d.after, 216000);
      expect(d.prev, 200000);
      expect(d.diff, 16000);
      expect((d.x.toStringAsFixed(2)), '7.41');
    });

    test('rules 35 sesuai PDF: Rp 98.444.705', () {
      final d = ruleData(35);
      expect(d.b, 98444705);
      // rDiv10 integer: (98444705×9+5) ~/ 10 = 88.600.235 (round-half-up eksak)
      expect(d.after, 88600235);
      expect(d.sl, 98444705 - 88600235);
      expect(d.next, 118133646); // (98444705×12+5) ~/ 10
    });

    test('rDiv10 round-half-up eksak', () {
      expect(rDiv10(240000, 9), 216000);
      expect(rDiv10(200000, 12), 240000);
      expect(rDiv10(5, 9), 5); // (45+5)~/10 = 5 → round(4.5) = 5 ✓
    });
  });

  group('format angka id-ID (sama seperti toLocaleString web)', () {
    test('idr', () {
      expect(idr(10000000), 'Rp10.000.000');
      expect(idr(200000), 'Rp200.000');
      expect(idr(double.nan), '–');
    });
    test('usd', () {
      expect(usd(11.151668), '\$11,15');
      expect(usd(55.76), '\$55,76');
    });
    test('one (1 desimal, koma)', () {
      expect(one(25), '25,0');
      expect(one(5.5), '5,5');
      expect(one(27.9), '27,9');
    });
    test('lotTxt (2 desimal, titik)', () {
      expect(lotTxt(0.22), '0.22');
      expect(lotTxt(10), '10.00');
    });
    test('shortIdr untuk sumbu chart', () {
      expect(shortIdr(1500000), 'Rp1,5jt');
      expect(shortIdr(25000), 'Rp25rb');
      expect(shortIdr(1234567890), 'Rp1,2M');
    });
  });

  group('Monte Carlo — port runMC()', () {
    test('berjalan & statistik dalam batas wajar', () {
      final p = const McParams(
          10000000, 0.02, 2, 25, 0.5, 3, 0.30, 100, 2000, true);
      final r = runMc(p);
      expect(r.ends.length, 2000);
      expect(r.bands.length, 101);
      for (final b in r.bands) {
        expect(b.length, 5);
        expect(b[0] <= b[2] && b[2] <= b[4], isTrue);
      }
      expect(r.pProfit, greaterThanOrEqualTo(0));
      expect(r.pProfit, lessThanOrEqualTo(1));
      // win rate impas: loss/(win+loss) = 1.12/3
      expect(r.beWr, closeTo(1.12 / 3.0, 1e-12));
    });

    test('biaya dipotong: cR = cost/SL', () {
      final p = const McParams(1000000, 0.02, 2, 25, 0.5, 3, 0.30, 10, 200, false);
      final r = runMc(p);
      expect(r.cR, closeTo(0.12, 1e-12));
      expect(r.exp, closeTo(0.5 * (2 - 0.12) - 0.5 * (1 + 0.12), 1e-12));
    });
  });

  group('computeMatrix — matriks SL pips × risk %', () {
    test('kolom = PCTS + risk terisi, sel = 1 desimal', () {
      final m = computeMatrix(10000000, 17934, 2, 25)!;
      expect(m.pcts.contains(2), isTrue);
      expect(m.pcts.length, kPcts.length); // 2 sudah ada di PCTS
      // 1% balance di 0.01 lot: riskUsd=55.76/… → SL = bal*1%/kurs/(0.01*10)
      final expectPips = round1dec(10000000 * 1 / 100 / 17934 / (0.01 * 10));
      expect(m.pips[0][0], expectPips);
    });
  });
}

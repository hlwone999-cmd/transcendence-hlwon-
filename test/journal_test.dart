// Test fitur Jurnal Trade (lib/journal.dart):
// - model & JSON roundtrip
// - statistik: total/TP/SL/winrate/buangan/balance
// - drawdown harian/mingguan/bulanan/all
// - smoke test UI: tab Jurnal merender + simpan trade + validasi
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/engine.dart';
import 'package:transcendence/journal.dart';
import 'package:transcendence/main.dart';

DateTime d(int y, int mo, int da, [int h = 10, int mi = 0]) =>
    DateTime(y, mo, da, h, mi);

void main() {
  group('JournalEntry model', () {
    test('outcome / pnl / buangan sesuai tangga rules', () {
      final tp = JournalEntry(
          at: d(2026, 1, 5), pair: 'XAUUSD', price: 4000, ruleA: 2, ruleB: 3);
      final sl = JournalEntry(
          at: d(2026, 1, 5), pair: 'XAUUSD', price: 4000, ruleA: 2, ruleB: 1);
      final be = JournalEntry(
          at: d(2026, 1, 5), pair: 'XAUUSD', price: 4000, ruleA: 2, ruleB: 2);
      expect(tp.outcome, 1);
      expect(tp.pnl, ruleBal(3) - ruleBal(2)); // +20% gap
      expect(sl.outcome, -1);
      // buangan murni: balance setelah SL (90%) − balance rule 1
      expect(sl.buangan, rDiv10(ruleBal(2), 9) - ruleBal(1)); // 216rb − 200rb
      expect(sl.pnl, -(ruleBal(2) - ruleBal(1))); // pergeseran balance = gap penuh
      expect(be.outcome, 0);
      expect(be.pnl, 0);
      expect(be.buangan, 0);
    });

    test('tanda manual TP/SL & miss', () {
      // tanda manual menang atas arah rules
      final tpSama = JournalEntry(
          at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 2, ruleB: 2, mark: 'TP');
      expect(tpSama.outcome, 1);
      expect(tpSama.pnl, 0);
      // SL di rule yang sama (SL kecil, tidak pindah rule): SL terhitung,
      // tapi buangan 0 karena tidak turun rules — terpisah dari buangan.
      final slSama = JournalEntry(
          at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 2, ruleB: 2, mark: 'SL');
      expect(slSama.outcome, -1);
      expect(slSama.buangan, 0);
      // miss: bukan TP/SL walau rules turun & tanda SL
      final miss = JournalEntry(
          at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 3, ruleB: 1,
          mark: 'SL', taken: false);
      expect(miss.outcome, 2);
      expect(miss.pnl, 0);
      expect(miss.buangan, 0);
    });

    test('JSON roundtrip (dengan tanda & eksekusi)', () {
      final e = JournalEntry(
          at: DateTime(2026, 3, 14, 21, 5),
          pair: 'xauusd',
          price: 4021.5,
          ruleA: 7,
          ruleB: 8,
          mark: 'SL',
          taken: false);
      final back = JournalEntry.fromJson(e.toJson());
      expect(back.mark, 'SL');
      expect(back.taken, false);
      expect(back.outcome, 2);
      // entry lama tanpa mark/taken → kompatibel: diturunkan dari rules
      final legacy = JournalEntry.fromJson(
          {'at': '2026-03-14T21:05:00.000', 'pair': 'XAUUSD', 'price': 4000, 'a': 5, 'b': 4});
      expect(legacy.mark, '');
      expect(legacy.taken, true);
      expect(legacy.outcome, -1);
    });
  });

  group('computeJournalStats', () {
    // 2026-01-05 = Senin. Trade 5 & 6 Jan = minggu sama; "now" = 6 Jan.
    final entries = [
      JournalEntry(
          at: d(2026, 1, 5, 9), pair: 'XAUUSD', price: 4000, ruleA: 1, ruleB: 2),
      JournalEntry(
          at: d(2026, 1, 5, 14), pair: 'XAUUSD', price: 4010, ruleA: 2, ruleB: 1),
      JournalEntry(
          at: d(2026, 1, 6, 11), pair: 'XAUUSD', price: 3990, ruleA: 1, ruleB: 2),
    ];
    final now = d(2026, 1, 6, 12);
    final s = computeJournalStats(entries, 1, now);

    test('total / tp / sl / winrate', () {
      expect(s.total, 3);
      expect(s.tp, 2);
      expect(s.sl, 1);
      expect(s.winrate, closeTo(66.6667, 0.01));
    });

    test('total buangan = buangan murni, bukan gabungan SL + buangan', () {
      expect(s.buangan, rDiv10(ruleBal(2), 9) - ruleBal(1)); // 216rb − 200rb
    });

    test('balance sekarang dari rules akhir trade terakhir', () {
      expect(s.startBal, ruleBal(1));
      expect(s.balanceNow, ruleBal(2)); // trade terakhir 1 → 2
    });

    test('drawdown harian/mingguan/bulanan/all', () {
      // Kurva: (5/1 09.00, 200rb) → (5/1 09.00, 240rb) → (5/1 14.00, 200rb)
      //        → (6/1 11.00, 240rb)
      // DD 5 Jan: puncak 240rb → lembah 200rb = 40rb (16,67% dari 240rb).
      expect(s.daily.rp, 0); // hari "ini" (6/1) cuma naik, tak ada penurunan
      expect(s.weekly.rp, 40000); // minggu sama mencakup 5 & 6 Jan
      expect(s.monthly.rp, 40000);
      expect(s.all.rp, 40000);
      expect(s.all.pct, closeTo(40000 / 240000 * 100, 0.01));
    });

    test('jurnal kosong → semua nol, balance = start rule', () {
      final e = computeJournalStats(const [], 3, now);
      expect(e.total, 0);
      expect(e.winrate, 0);
      expect(e.buangan, 0);
      expect(e.balanceNow, ruleBal(3));
      expect(e.all.rp, 0);
    });

    test('BE tidak dihitung TP/SL tapi masuk total', () {
      final s2 = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 4, ruleB: 4),
        JournalEntry(
            at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 4, ruleB: 5),
      ], 4, d(2026, 1, 5, 23));
      expect(s2.total, 2);
      expect(s2.tp, 1);
      expect(s2.sl, 0);
      expect(s2.miss, 0);
      // winrate = TP / (TP + SL) — BE di luar penyebut
      expect(s2.winrate, closeTo(100, 0.01));
    });

    test('trade miss: masuk total, bukan TP/SL, balance tidak berubah', () {
      final s3 = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 4, ruleB: 3,
            mark: 'SL', taken: false),
        JournalEntry(
            at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 4, ruleB: 5,
            mark: 'TP'),
      ], 4, d(2026, 1, 5, 23));
      expect(s3.total, 2);
      expect(s3.miss, 1);
      expect(s3.tp, 1);
      expect(s3.sl, 0);
      expect(s3.buangan, 0);
      expect(s3.winrate, 100); // hanya trade yang diambil yang dihitung
      expect(s3.balanceNow, ruleBal(5)); // miss tidak menggerakkan balance
      expect(s3.all.rp, 0); // kurva balance hanya dari trade yang diambil
    });

    test('SL tanpa buangan (rule sama) terpisah dari total buangan', () {
      final s4 = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 4, ruleB: 4,
            mark: 'SL'),
        JournalEntry(
            at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 4, ruleB: 3,
            mark: 'SL'),
      ], 4, d(2026, 1, 5, 23));
      expect(s4.sl, 2); // dua-duanya SL
      expect(s4.buangan,
          rDiv10(ruleBal(4), 9) - ruleBal(3)); // hanya yang turun rules
      expect(s4.winrate, 0);
    });

    test('buangan murni = selisih antar rule − nominal SL (10%)', () {
      // Trade rule 5 → 4: selisih penuh 69.120 terbagi dua —
      //   nominal SL 10% = 41.472, buangan murni = 27.648.
      final e = JournalEntry(
          at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 5, ruleB: 4,
          mark: 'SL');
      final gap = ruleBal(5) - ruleBal(4); // 69.120 (gabungan)
      final slNom = ruleBal(5) - rDiv10(ruleBal(5), 9); // 41.472 (SL 10%)
      expect(gap, 69120); // sanity angka user di screenshot
      expect(slNom, 41472);
      expect(e.buangan, 27648); // PURE buangan — bukan 69.120
      expect(e.buangan, rDiv10(ruleBal(5), 9) - ruleBal(4));
      expect(e.buangan + slNom, gap); // SL + buangan = selisih penuh
    });
  });

  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('tab Jurnal: render, simpan trade, validasi', (tester) async {
    // kursmode 'fixed' supaya tidak ada fetchLive HTTP (sama seperti test lain)
    SharedPreferences.setMockInitialValues({'lotxau_kursmode': 'fixed'});
    tester.view.physicalSize = const Size(412, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    // Tanpa override FlutterError.onError: exception apapun = test gagal.
    // (GhostButton kini overflow-safe & label jurnal ringkas — tidak ada
    // artefak Ahem tersisa di halaman ini.)
    await tester.pumpWidget(const TranscendenceApp());
    await tester.pump();
    // 200ms: debounce tabel (120ms) ter-render, timer MC (250ms) SENGAJA
    // tidak dieksekusi — Isolate.run tidak selesai di FakeAsync (pola
    // layout_mobile_test.dart).
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pumpAndSettle();

    // buka tab Jurnal
    await tester.tap(find.text('Jurnal').last);
    await tester.pumpAndSettle();

    expect(find.text('Trading Journal'), findsOneWidget);
    expect(find.text('TOTAL BUANGAN'), findsOneWidget);
    expect(find.text('KEAMBIL'), findsOneWidget);
    expect(find.text('MISS'), findsOneWidget);
    expect(find.text('Belum ada trade. Catat trade pertamamu di form atas — '
        'tandai hasilnya TP/SL dan apakah keambil atau miss.'), findsOneWidget);

    // validasi: simpan tanpa isi rules → toast error, tidak ada entry
    await tester.enterText(
        find.descendant(
            of: find.byKey(const Key('jurnal_price')),
            matching: find.byType(TextField)),
        '4021,5');
    await tester.tap(find.text('Simpan Trade'));
    await tester.pump();
    expect(find.text('Pilih rules awal & rules akhir dulu.'), findsOneWidget);
    // biarkan snackbar hilang dulu supaya tidak menghalangi tap berikutnya
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();

    // isi rules awal = Rule 1, rules akhir = Rule 2 (TP)
    await tester.tap(find.byKey(const Key('jurnal_ruleA')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Rule 1 · Rp200rb').last);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('jurnal_ruleB')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Rule 2 · Rp240rb').last);
    await tester.pumpAndSettle();

    await tester.tap(find.text('Simpan Trade'));
    await tester.pump();
    // hanguskan timer snackbar (4s) supaya tidak ada timer pending saat test
    // berakhir (timer yang menyala di FakeAsync bisa memicu error liar).
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();

    // trade tersimpan → riwayat muncul, badge TP & tombol hapus ada
    expect(find.text('Rule 1'), findsWidgets);
    expect(find.text('TP'), findsWidgets);
    expect(find.byIcon(Icons.delete_outline), findsOneWidget);
    final s = computeJournalStats(const [], 1, DateTime.now());
    expect(s.startBal, 200000); // sanity engine
  });
}

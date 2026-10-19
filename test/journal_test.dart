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
      // SL murni = 10% balance rule awal — berdiri sendiri tanpa buangan
      expect(sl.slMurni, ruleBal(2) - rDiv10(ruleBal(2), 9)); // 240rb − 216rb
      expect(sl.slMurni + sl.buangan, ruleBal(2) - ruleBal(1)); // = gap penuh
      expect(tp.slMurni, 0);
      expect(be.slMurni, 0);
      // bias default kosong (entry lama) — tidak dipakai dalam hitungan
      expect(tp.bias, '');
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
      expect(slSama.slMurni, 0); // tidak turun rules → tidak masuk drawdown
      // miss: bukan TP/SL walau rules turun & tanda SL
      final miss = JournalEntry(
          at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 3, ruleB: 1,
          mark: 'SL', taken: false);
      expect(miss.outcome, 2);
      expect(miss.pnl, 0);
      expect(miss.buangan, 0);
      expect(miss.slMurni, 0);
      // bias arah trade tersimpan apa adanya, tidak mengubah outcome
      final sell = JournalEntry(
          at: d(2026, 1, 5), pair: 'X', price: 1, ruleA: 2, ruleB: 1,
          mark: 'SL', bias: 'SELL');
      expect(sell.bias, 'SELL');
      expect(sell.outcome, -1);
    });

    test('JSON roundtrip (dengan tanda & eksekusi)', () {
      final e = JournalEntry(
          at: DateTime(2026, 3, 14, 21, 5),
          pair: 'xauusd',
          price: 4021.5,
          ruleA: 7,
          ruleB: 8,
          mark: 'SL',
          taken: false,
          bias: 'SELL');
      final back = JournalEntry.fromJson(e.toJson());
      expect(back.mark, 'SL');
      expect(back.taken, false);
      expect(back.bias, 'SELL');
      expect(back.outcome, 2);
      // entry lama tanpa mark/taken → kompatibel: diturunkan dari rules
      final legacy = JournalEntry.fromJson(
          {'at': '2026-03-14T21:05:00.000', 'pair': 'XAUUSD', 'price': 4000, 'a': 5, 'b': 4});
      expect(legacy.mark, '');
      expect(legacy.taken, true);
      expect(legacy.bias, ''); // entry lama tanpa bias → kosong (tanpa pil)
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

    test('drawdown = akumulasi nominal SL murni per periode (tanpa buangan)',
        () {
      // Satu-satunya SL: 5/1 14.00 rule 2 → 1. SL murni = 10% dari balance
      // rule 2 = 240rb − 216rb = 24rb — BUKAN selisih penuh 40rb (SL+buangan).
      final slNom = ruleBal(2) - rDiv10(ruleBal(2), 9);
      expect(slNom, 24000);
      expect(s.daily.rp, 0); // hari "ini" (6/1) tidak ada SL
      expect(s.weekly.rp, slNom); // minggu sama mencakup 5 & 6 Jan
      expect(s.monthly.rp, slNom);
      expect(s.all.rp, slNom);
      // % = SL murni terhadap balance awal periode (awal jurnal = rule 1)
      expect(s.all.pct, closeTo(slNom / ruleBal(1) * 100, 0.01)); // 12%
    });

    test('SL hari ini → drawdown harian & mingguan langsung terisi', () {
      // "now" digeser ke 5/1 — tanggal yang sama dengan trade SL-nya.
      final sNow = computeJournalStats(entries, 1, d(2026, 1, 5, 15));
      final slNom = ruleBal(2) - rDiv10(ruleBal(2), 9);
      expect(sNow.daily.rp, slNom);
      expect(sNow.weekly.rp, slNom);
      expect(sNow.monthly.rp, slNom);
      expect(sNow.all.rp, slNom);
      // % harian: balance awal 5/1 = balance awal jurnal (belum ada trade)
      expect(sNow.daily.pct, closeTo(slNom / ruleBal(1) * 100, 0.01));
    });

    test('SL di hari/pekan berbeda terpisah per periode', () {
      final sMix = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5, 9), pair: 'X', price: 1, ruleA: 3, ruleB: 2,
            mark: 'SL'), // Senin 5/1 — pekan lalu
        JournalEntry(
            at: d(2026, 1, 12, 9), pair: 'X', price: 1, ruleA: 2, ruleB: 1,
            mark: 'SL'), // Senin 12/1 — pekan ini ("now")
      ], 3, d(2026, 1, 12, 10));
      final sl3 = ruleBal(3) - rDiv10(ruleBal(3), 9); // 28.800
      final sl2 = ruleBal(2) - rDiv10(ruleBal(2), 9); // 24.000
      expect(sl3, 28800);
      expect(sl2, 24000);
      expect(sMix.daily.rp, sl2); // hanya SL hari ini
      expect(sMix.weekly.rp, sl2); // minggu 12/1 tidak memuat SL 5/1
      expect(sMix.monthly.rp, sl3 + sl2); // dua-duanya di bulan Januari
      expect(sMix.all.rp, sl3 + sl2);
      // % harian: balance awal 12/1 = ruleBal(2) (hasil trade 5/1)
      expect(sMix.daily.pct, closeTo(sl2 / ruleBal(2) * 100, 0.01)); // 10%
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
      expect(s3.all.rp, 0); // miss & TP tidak menyumbang SL murni
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
      // Drawdown hanya dari SL yang turun rules — SL murni, tanpa buangan
      expect(s4.all.rp, ruleBal(4) - rDiv10(ruleBal(4), 9)); // 34.560
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
    // segmen bias BUY/SELL ada di form
    expect(find.text('BIAS'), findsOneWidget);
    expect(find.text('BUY'), findsOneWidget);
    expect(find.text('SELL'), findsOneWidget);
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

    // pilih bias SELL → preview ikut menampilkan pil SELL
    await tester.tap(find.text('SELL'));
    await tester.pump();
    expect(find.text('SELL'), findsNWidgets(2)); // opsi form + pil preview

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
    // pil bias SELL di riwayat + opsi SELL di form (preview hilang: ruleB null)
    expect(find.text('SELL'), findsNWidgets(2));
    final s = computeJournalStats(const [], 1, DateTime.now());
    expect(s.startBal, 200000); // sanity engine
  });
}

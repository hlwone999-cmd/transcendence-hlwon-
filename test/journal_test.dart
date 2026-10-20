// Test fitur Jurnal Trade (lib/journal.dart):
// - model & JSON roundtrip
// - statistik: total/TP/SL/winrate/buangan/balance
// - drawdown harian/mingguan/bulanan/all (periode terburuk)
// - ekspor/impor data jurnal (JSON roundtrip) + kartu KEAMBIL/MISS
// - smoke test UI: tab Jurnal merender + simpan trade + validasi + dialog
//   ekspor/impor
import 'dart:convert';

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

    test('drawdown = SL murni pada periode TERBURUK (tanpa buangan)', () {
      // Satu-satunya SL: 5/1 14.00 rule 2 → 1. SL murni = 10% dari balance
      // rule 2 = 240rb − 216rb = 24rb — BUKAN selisih penuh 40rb (SL+buangan).
      final slNom = ruleBal(2) - rDiv10(ruleBal(2), 9);
      expect(slNom, 24000);
      // "now" = 6/1, SL-nya 5/1 — tetap terlihat karena yang diambil adalah
      // hari/minggu/bulan TERBURUK, bukan "hari ini" menurut jam.
      expect(s.daily.rp, slNom); // hari terburuk = 5/1
      expect(s.weekly.rp, slNom); // pekan terburuk memuat 5/1
      expect(s.monthly.rp, slNom);
      expect(s.all.rp, slNom);
      // % = SL murni terhadap balance rule awal trade (rule 2) = 10%
      expect(s.all.pct, closeTo(slNom / ruleBal(2) * 100, 0.01)); // 10%
      expect(s.daily.pct, closeTo(10.0, 0.01));
    });

    test('trade SL bertanggal lampau → harian & mingguan langsung terisi '
        '(regresi keluhan user)', () {
      // "now" digeser jauh (17/2) sementara SL-nya 5/1 — implementasi lama
      // menunjukkan Rp0 karena hanya melihat hari/minggu/bulan "ini".
      final sPast = computeJournalStats(entries, 1, d(2026, 2, 17, 15));
      final slNom = ruleBal(2) - rDiv10(ruleBal(2), 9);
      expect(sPast.daily.rp, slNom);
      expect(sPast.weekly.rp, slNom);
      expect(sPast.monthly.rp, slNom);
      expect(sPast.all.rp, slNom);
      expect(sPast.daily.pct, closeTo(slNom / ruleBal(2) * 100, 0.01));
    });

    test('periode terburuk terpilih: SL terbesar menang di tiap tingkat', () {
      final sMix = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5, 9), pair: 'X', price: 1, ruleA: 3, ruleB: 2,
            mark: 'SL'), // Senin 5/1 — SL 28.800 (hari/pekan/bulan Jan)
        JournalEntry(
            at: d(2026, 2, 12, 9), pair: 'X', price: 1, ruleA: 2, ruleB: 1,
            mark: 'SL'), // Kamis 12/2 — SL 24.000 (bulan Feb)
      ], 3, d(2026, 2, 12, 10));
      final sl3 = ruleBal(3) - rDiv10(ruleBal(3), 9); // 28.800
      final sl2 = ruleBal(2) - rDiv10(ruleBal(2), 9); // 24.000
      expect(sl3, 28800);
      expect(sl2, 24000);
      expect(sMix.daily.rp, sl3); // hari terburuk = 5/1 (28.800 > 24.000)
      expect(sMix.weekly.rp, sl3); // pekan 5/1 terburuk (pekan 12/2 cuma 24rb)
      expect(sMix.monthly.rp, sl3); // bulan terburuk = Jan (Feb cuma 24rb)
      expect(sMix.all.rp, sl3 + sl2); // TOTAL tetap seluruh SL
      expect(sMix.daily.pct, closeTo(sl3 / ruleBal(3) * 100, 0.01)); // 10%
      expect(sMix.all.pct, closeTo(10.0, 0.01));
    });

    test('kasus user: start rule 1, SL 5→4 → persen = 10% (bukan 20,7%)', () {
      // SL murni 41.472 = 10% dari balance rule 5 (414.720) — SAAT trade.
      // Persen TIDAK memakai balance awal jurnal (rule 1 = 200rb) — kalau
      // dipakai, 41.472/200rb = 20,7% (angka salah yang pernah dikeluhkan).
      final sU = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5, 9), pair: 'XAUUSD', price: 4000,
            ruleA: 5, ruleB: 4, mark: 'SL'),
      ], 1, d(2026, 1, 5, 10));
      expect(sU.daily.rp, 41472);
      expect(sU.daily.pct, closeTo(10.0, 0.01));
      expect(sU.weekly.rp, 41472);
      expect(sU.monthly.rp, 41472);
      expect(sU.monthly.pct, closeTo(10.0, 0.01));
      expect(sU.all.pct, closeTo(10.0, 0.01));
      // dua SL bertingkat (5→4 lalu 4→3), 5/1 & 6/1 satu pekan satu bulan:
      // hari terburuk = 5/1 (41.472); pekan/bulan terburuk = gabungan 76.032
      final sW = computeJournalStats([
        JournalEntry(
            at: d(2026, 1, 5, 9), pair: 'X', price: 1,
            ruleA: 5, ruleB: 4, mark: 'SL'),
        JournalEntry(
            at: d(2026, 1, 6, 9), pair: 'X', price: 1,
            ruleA: 4, ruleB: 3, mark: 'SL'),
      ], 5, d(2026, 1, 6, 10));
      expect(sW.daily.rp, 41472);
      expect(sW.daily.pct, closeTo(10.0, 0.01));
      expect(sW.weekly.rp, 41472 + 34560);
      expect(sW.weekly.pct, closeTo(10.0, 0.01));
      expect(sW.monthly.rp, 41472 + 34560);
      expect(sW.all.rp, 41472 + 34560);
      expect(sW.all.pct, closeTo(10.0, 0.01));
    });

    test('jurnal kosong → semua nol, balance = start rule', () {
      final e = computeJournalStats(const [], 3, now);
      expect(e.total, 0);
      expect(e.winrate, 0);
      expect(e.buangan, 0);
      expect(e.balanceNow, ruleBal(3));
      expect(e.all.rp, 0);
      expect(e.daily.rp, 0);
      expect(e.weekly.rp, 0);
      expect(e.monthly.rp, 0);
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

  group('ekspor / impor data jurnal', () {
    final entries = [
      JournalEntry(
          at: d(2026, 1, 5, 9, 30), pair: 'XAUUSD', price: 4021.5,
          ruleA: 5, ruleB: 4, mark: 'SL', taken: true, bias: 'SELL'),
      JournalEntry(
          at: d(2026, 1, 6, 10), pair: 'XAUUSD', price: 4000,
          ruleA: 4, ruleB: 5, mark: 'TP', taken: false, bias: 'BUY'),
    ];

    test('ekspor → impor = data identik (roundtrip semua field)', () {
      final json =
          journalExportJson(entries, 3, exportedAt: d(2026, 1, 7, 8));
      final imp = journalParseImport(json);
      expect(imp.startRule, 3);
      expect(imp.entries.length, 2);
      final a = imp.entries[0];
      expect(a.at, d(2026, 1, 5, 9, 30));
      expect(a.pair, 'XAUUSD');
      expect(a.price, 4021.5);
      expect(a.ruleA, 5);
      expect(a.ruleB, 4);
      expect(a.mark, 'SL');
      expect(a.taken, true);
      expect(a.bias, 'SELL');
      final b = imp.entries[1];
      expect(b.taken, false);
      expect(b.mark, 'TP');
      expect(b.bias, 'BUY');
      // ekspor ulang hasil impor = teks JSON yang sama persis
      expect(
          journalExportJson(imp.entries, imp.startRule,
              exportedAt: d(2026, 1, 7, 8)),
          json);
    });

    test('list polosan diterima; start default 1 & di-clamp ke rule terakhir',
        () {
      final imp = journalParseImport(
          jsonEncode(entries.map((e) => e.toJson()).toList()));
      expect(imp.entries.length, 2);
      expect(imp.startRule, 1);
      final bad =
          journalParseImport(jsonEncode({'start': 999, 'entries': []}));
      expect(bad.startRule, kRuleBalances.length); // clamp, bukan crash
    });

    test('data sampah → FormatException (ditangkap UI, bukan crash)', () {
      expect(() => journalParseImport('bukan json'), throwsFormatException);
      expect(() => journalParseImport('{"app":"x"}'), throwsFormatException);
      expect(() => journalParseImport('42'), throwsFormatException);
      expect(() => journalParseImport(''), throwsFormatException);
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
    // segmen EKSEKUSI + kartu ringkasan eksekusi = masing-masing 2 kemunculan
    expect(find.text('KEAMBIL'), findsNWidgets(2));
    expect(find.text('MISS'), findsNWidgets(2));
    // tombol ekspor/impor data ada di bagian riwayat
    expect(find.text('EKSPOR'), findsOneWidget);
    expect(find.text('IMPOR'), findsOneWidget);
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
    await tester.tap(find.text('Rule 1 · Rp200.000').last);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('jurnal_ruleB')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Rule 2 · Rp240.000').last);
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

    // --- EKSPOR: dialog terbuka, JSON terlihat, tutup lagi ---
    // (Scrollable halaman disebut eksplisit — TextField punya Scrollable
    // internal sendiri, jadi finder .first di dalam SingleChildScrollView)
    await tester.scrollUntilVisible(find.text('EKSPOR'), 300,
        scrollable: find
            .descendant(
                of: find.byType(SingleChildScrollView),
                matching: find.byType(Scrollable))
            .first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('EKSPOR'));
    await tester.pumpAndSettle();
    expect(find.text('Ekspor Data Jurnal'), findsOneWidget);
    expect(find.text('Salin'), findsOneWidget);
    expect(find.text('Bagikan'), findsOneWidget);
    await tester.tap(find.text('Tutup'));
    await tester.pumpAndSettle();

    // --- IMPOR: format sampah → toast error, data lama tetap utuh ---
    await tester.tap(find.text('IMPOR'));
    await tester.pumpAndSettle();
    expect(find.text('Impor Data Jurnal'), findsOneWidget);
    await tester.enterText(
        find.byKey(const Key('jurnal_import_text')), 'sampah');
    await tester.tap(find.text('Impor'), warnIfMissed: true);
    // toast error impor masuk ANTREAN ScaffoldMessenger di belakang toast
    // 'Trade TP tersimpan' — majukan jam supaya antrean sampai ke toast
    // error impor, lalu hanguskan timer-nya.
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
    expect(find.text('Format tidak dikenali — tempel JSON hasil ekspor jurnal.'),
        findsOneWidget);
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
    expect(find.text('Rule 1'), findsWidgets);

    final s = computeJournalStats(const [], 1, DateTime.now());
    expect(s.startBal, 200000); // sanity engine
  });
}

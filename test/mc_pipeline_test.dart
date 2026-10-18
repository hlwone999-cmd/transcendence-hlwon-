// Regresi bug "Monte Carlo simulasi nggak bisa dijalanin":
// mem-pump aplikasi penuh, membiarkan debounce MC (250ms) berjalan, lalu
// menunggu Isolate.run selesai secara NYATA (runAsync). Hasil harus muncul:
// verdict "Peluang profit …", statistik, dan dua chart (fan + histogram).
import 'dart:ui' show Size;
import 'package:flutter/widgets.dart' show FlutterError, FlutterErrorDetails;
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/charts.dart';
import 'package:transcendence/main.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('Monte Carlo otomatis berjalan & menampilkan hasil + chart',
      (tester) async {
    // kurs mode fixed supaya test tidak memanggil API live (tanpa jaringan).
    SharedPreferences.setMockInitialValues({'lotxau_kursmode': 'fixed'});
    tester.view.physicalSize = const Size(412, 915);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    // Overflow RenderFlex akibat font test Ahem (glyph selebar fontSize)
    // dianggap artefak — pola sama dengan layout_mobile_test.dart.
    final errs = <FlutterErrorDetails>[];
    FlutterError.onError = (d) {
      if (d.exception.toString().contains('RenderFlex overflowed')) return;
      if (errs.length < 10) errs.add(d);
    };

    await tester.pumpWidget(const TranscendenceApp());
    await tester.pump(); // biarkan _restore() selesai

    // Sebelum MC selesai: hero menampilkan placeholder sesuai desain baru.
    // Kartu MC ada di tab "Simulasi" yang masih offstage (IndexedStack) —
    // pakai skipOffstage:false; JANGAN pumpAndSettle dulu karena timer MC
    // (250ms) bisa terpicu dan fallback isolate sinkron menyelesaikan MC.
    expect(
        find.text('Lengkapi parameter di bawah untuk menjalankan simulasi.',
            skipOffstage: false),
        findsOneWidget);

    // UI baru: aktifkan tab "Simulasi" agar kartu terlihat & hit-testable.
    await tester.tap(find.text('Simulasi'));
    await tester.pump();

    // Jalankan timer debounce MC (250ms) — timer fake async.
    await tester.pump(const Duration(milliseconds: 300));

    // Tunggu Isolate.run benar-benar selesai di event loop nyata.
    await tester.runAsync(() async {
      await Future<void>.delayed(const Duration(milliseconds: 2500));
    });
    await tester.pump();

    // Verdict hasil harus muncul (bukan lagi placeholder)
    expect(
        find.text('Lengkapi parameter di bawah untuk menjalankan simulasi.'),
        findsNothing,
        reason: 'MC dengan input default valid harus menghasilkan verdict');
    expect(find.textContaining('Peluang Profit ('), findsOneWidget);

    // Statistik MC harus terisi (label tile gaya Obsidian Precision;
    // "Median Saldo" muncul 2x: tile + legenda fan chart)
    expect(find.text('Median Saldo'), findsWidgets);
    expect(find.text('Skenario Buruk (5%)'), findsOneWidget);
    expect(find.text('Skenario Bagus (95%)'), findsOneWidget);

    // Dua chart harus dirender
    expect(find.byType(FanChart), findsOneWidget);
    expect(find.byType(HistChart), findsOneWidget);

    // Tidak boleh ada exception selain artefak overflow font test
    expect(errs, isEmpty, reason: 'Tidak boleh ada exception non-artefak');
  });
}

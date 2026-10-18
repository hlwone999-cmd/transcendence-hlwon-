// Reproduksi bug: layar blank di lebar < 880 (mobile).
// Test ini mem-pump aplikasi penuh pada viewport ponsel dan menangkap exception.
// Catatan: hanya pump sampai debounce tabel (120ms) — timer MC (250ms)
// sengaja tidak dieksekusi karena Isolate.run tidak selesai di FakeAsync.
import 'dart:ui' show Size;
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/main.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('layout mobile (<880px) harus merender konten', (tester) async {
    // kurs mode fixed supaya test deterministik (tidak ada fetchLive async
    // yang selesai setelah body test — penyebab error zona tak tertangkap).
    SharedPreferences.setMockInitialValues({'lotxau_kursmode': 'fixed'});
    tester.view.physicalSize = const Size(412, 915);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final errs = <FlutterErrorDetails>[];
    FlutterError.onError = (d) {
      if (errs.length < 10) errs.add(d);
    };

    await tester.pumpWidget(const TranscendenceApp());
    await tester.pump();
    // biarkan debounce 120ms tabel dirender (MC di 250ms sengaja dilewati)
    await tester.pump(const Duration(milliseconds: 200));

    // Exception struktural (mis. constraint infinite yang bikin layar blank)
    // = test GAGAL. Overflow RenderFlex akibat font test Ahem (glyph kotak
    // selebar fontSize) dianggap artefak dan diabaikan.
    final structural = errs
        .where((e) => !e.exception.toString().contains('RenderFlex overflowed'))
        .toList();
    for (final e in structural) {
      debugPrint('=== STRUCTURAL EXCEPTION ===');
      debugPrint(e.exception.toString());
      debugPrint(e.informationCollector?.call().join('\n') ?? '(tanpa info)');
    }

    // konten kunci harus ada di tree (UI Obsidian Precision)
    expect(find.text('Transcendence'), findsOneWidget,
        reason: 'Top bar harus terlihat di layout mobile');
    expect(find.text('TRADING PLAN STAGE'), findsOneWidget);
    expect(find.text('RECOMMENDED LOT SIZE'), findsOneWidget);
    // Tab non-aktif di IndexedStack dianggap offstage oleh finder default —
    // pakai skipOffstage:false untuk memverifikasi seluruh tab ter-render.
    expect(find.text('Matrix Setup & SL Pips', skipOffstage: false),
        findsOneWidget,
        reason: 'Tab Matrix ada di IndexedStack walaupun tidak aktif');
    expect(structural, isEmpty,
        reason: 'Layout tidak boleh melempar exception struktural');
  });
}

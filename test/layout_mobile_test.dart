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
    SharedPreferences.setMockInitialValues({});
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

    // konten kunci harus ada di tree
    expect(find.text('Kalkulator Lot XAUUSD'), findsOneWidget,
        reason: 'Header harus terlihat di layout mobile');
    expect(find.text('Rules Recovery'), findsOneWidget);
    expect(find.text('Input'), findsOneWidget);
    expect(structural, isEmpty,
        reason: 'Layout tidak boleh melempar exception struktural');
  });
}

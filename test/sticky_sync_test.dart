// Regresi bug "saat aku geser tabel nya itu atas nya ngga ikut ke geser":
// geser HORIZONTAL pada badan tabel harus ikut menggeser header (dan
// sebaliknya). Keduanya kini disinkronkan lewat pasangan controller mirror.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/main.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  // Overflow RenderFlex akibat font test Ahem (glyph selebar fontSize)
  // dianggap artefak — pola sama dengan layout_mobile_test.dart.
  void ignoreAhemOverflow() {
    FlutterError.onError = (d) {
      if (d.exception.toString().contains('RenderFlex overflowed')) return;
      FlutterError.presentError(d);
    };
  }

  testWidgets('geser horizontal badan tabel ikut menggeser header',
      (tester) async {
    SharedPreferences.setMockInitialValues({'lotxau_kursmode': 'fixed'});
    tester.view.physicalSize = const Size(412, 915);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    ignoreAhemOverflow();

    await tester.pumpWidget(const TranscendenceApp());
    await tester.pump();
    // debounce tabel 120ms
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pumpAndSettle();

    // UI baru: tabel utama ada di tab "Matrix Risiko" — aktifkan dulu.
    await tester.tap(find.text('Matrix Risiko').first);
    await tester.pumpAndSettle();

    final tableFinder = find.byKey(const Key('mainTable'));
    await tester.ensureVisible(tableFinder);
    await tester.pumpAndSettle();

    // Header "SL (PIPS)" dan sel body baris pertama (SL 0.01 lot = "111,5"
    // dengan input default: balance 10jt, risk 2%, kurs 17.934).
    final headerFinder = find.text('SL (PIPS)');
    final bodyCellFinder = find.descendant(
        of: tableFinder, matching: find.text('111,5'));
    expect(headerFinder, findsOneWidget);
    expect(bodyCellFinder, findsWidgets);

    final headerX0 = tester.getTopLeft(headerFinder).dx;
    final bodyX0 = tester.getTopLeft(bodyCellFinder.first).dx;

    // Geser horizontal pada BADAN tabel (titik tengah sel body) ke kiri.
    final start = tester.getCenter(bodyCellFinder.first);
    await tester.dragFrom(start, const Offset(-120, 0));
    await tester.pumpAndSettle();

    final headerX1 = tester.getTopLeft(headerFinder).dx;
    final bodyX1 = tester.getTopLeft(bodyCellFinder.first).dx;

    final headerDelta = headerX0 - headerX1;
    final bodyDelta = bodyX0 - bodyX1;

    expect(bodyDelta, greaterThan(50),
        reason: 'Badan tabel harus bergeser setelah drag horizontal');
    expect(headerDelta, greaterThan(50),
        reason: 'Header harus ikut bergeser (bug: dulu tidak ikut)');
    expect((headerDelta - bodyDelta).abs(), lessThan(1.5),
        reason: 'Header & body harus bergeser selaras (sinkron sempurna)');
  });

  testWidgets('geser horizontal HEADER juga menggeser badan tabel',
      (tester) async {
    SharedPreferences.setMockInitialValues({'lotxau_kursmode': 'fixed'});
    tester.view.physicalSize = const Size(412, 915);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    ignoreAhemOverflow();

    await tester.pumpWidget(const TranscendenceApp());
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pumpAndSettle();

    // UI baru: aktifkan tab "Matrix Risiko" dulu agar tabel bisa di-hit-test.
    await tester.tap(find.text('Matrix Risiko').first);
    await tester.pumpAndSettle();

    final tableFinder = find.byKey(const Key('mainTable'));
    await tester.ensureVisible(tableFinder);
    await tester.pumpAndSettle();

    final headerFinder = find.text('SL (PIPS)');
    final bodyCellFinder = find.descendant(
        of: tableFinder, matching: find.text('111,5'));

    final headerX0 = tester.getTopLeft(headerFinder).dx;
    final bodyX0 = tester.getTopLeft(bodyCellFinder.first).dx;

    // Geser horizontal mulai dari titik HEADER.
    final start = tester.getCenter(headerFinder);
    await tester.dragFrom(start, const Offset(-100, 0));
    await tester.pumpAndSettle();

    final headerX1 = tester.getTopLeft(headerFinder).dx;
    final bodyX1 = tester.getTopLeft(bodyCellFinder.first).dx;

    final headerDelta = headerX0 - headerX1;
    final bodyDelta = bodyX0 - bodyX1;
    expect(bodyDelta, greaterThan(50),
        reason: 'Badan harus mengikuti header saat header digeser');
    expect((headerDelta - bodyDelta).abs(), lessThan(1.5),
        reason: 'Kedua arah harus tetap sinkron');
  });
}

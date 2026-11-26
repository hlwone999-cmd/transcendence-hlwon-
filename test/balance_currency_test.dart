// Modal bersih bisa diketik dalam IDR atau USD (toggle di field modal).
// Kalkulator tetap membaca modal dalam IDR: di mode USD nilainya = USD × kurs.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/main.dart';
import 'package:transcendence/ui_kit.dart';

Future<void> _pumpApp(WidgetTester tester, Map<String, Object> prefs) async {
  // kurs mode fixed: test tidak memanggil API kurs live (tanpa jaringan).
  SharedPreferences.setMockInitialValues({
    'lotxau_kursmode': 'fixed',
    ...prefs,
  });
  tester.view.physicalSize = const Size(412, 915);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  final prev = FlutterError.onError;
  FlutterError.onError = (d) {
    // Overflow akibat font test Ahem = artefak (pola sama layout_mobile_test).
    if (d.exception.toString().contains('RenderFlex overflowed')) return;
    prev?.call(d);
  };
  addTearDown(() => FlutterError.onError = prev);
  await tester.pumpWidget(const TranscendenceApp());
  await tester.pump(); // _restore()
  await tester.pump();
}

Future<void> _openKursPanel(WidgetTester tester) async {
  final head = find.text('Parameter Kurs & Saldo');
  await tester.ensureVisible(head);
  await tester.tap(head);
  await tester.pumpAndSettle(const Duration(milliseconds: 50));
}

Finder _modalField(String cur) => find.descendant(
  of: find.byKey(ValueKey('modal-$cur')),
  matching: find.byType(TextField),
);

Finder _kursField() => find.descendant(
  of: find.ancestor(
    of: find.text('KURS ACUAN USD / IDR'),
    matching: find.byType(LabeledField),
  ),
  matching: find.byType(TextField),
);

String _text(WidgetTester tester, Finder f) =>
    tester.widget<TextField>(f).controller!.text;

Future<void> _tapCur(WidgetTester tester, String cur) async {
  final seg = find.descendant(
    of: find.byType(CurrencyToggle),
    matching: find.text(cur),
  );
  await tester.ensureVisible(seg);
  await tester.tap(seg);
  await tester.pump();
}

void main() {
  testWidgets('toggle IDR/USD: konversi, ganti kurs, kembali IDR, tersimpan', (
    tester,
  ) async {
    await _pumpApp(tester, {
      'lotxau2': jsonEncode({'balance': '10.000.000', 'kurs': '18.000'}),
    });
    await _openKursPanel(tester);

    // Default IDR: label & toggle tampil, hint memakai Rupiah.
    expect(find.text('MODAL BERSIH AKUN (IDR)'), findsOneWidget);
    expect(find.byType(CurrencyToggle), findsOneWidget);
    expect(find.text('Terbaca: Rp10.000.000'), findsOneWidget);

    // Ganti ke USD: modal dibawa memakai kurs (10.000.000 / 18.000).
    await _tapCur(tester, 'USD');
    expect(find.text('MODAL BERSIH AKUN (USD)'), findsOneWidget);
    expect(_text(tester, _modalField('USD')), '555.56');
    // Konversi memakai nilai USD persis: tetap Rp10.000.000 (tanpa geser).
    expect(
      find.textContaining('Terbaca: \$555,56 ≈ Rp10.000.000'),
      findsOneWidget,
    );

    // Bolak-balik tanpa mengetik: modal IDR tidak berubah sama sekali.
    await _tapCur(tester, 'IDR');
    expect(_text(tester, _modalField('IDR')), '10.000.000');
    await _tapCur(tester, 'USD');
    expect(_text(tester, _modalField('USD')), '555.56');

    // Ketik modal USD manual -> IDR yang dipakai kalkulator = 500 × 18.000.
    await tester.enterText(_modalField('USD'), '500');
    await tester.pump();
    expect(
      find.text('Terbaca: \$500,00 ≈ Rp9.000.000 (kurs Rp18.000)'),
      findsOneWidget,
    );

    // Format USD lain yang umum diketik.
    await tester.enterText(_modalField('USD'), '1,250.50');
    await tester.pump();
    expect(
      find.textContaining('Terbaca: \$1.250,50 ≈ Rp22.509.000'),
      findsOneWidget,
    );
    await tester.enterText(_modalField('USD'), '500');
    await tester.pump();

    // Kurs berubah -> konversi ikut berubah (modal USD tetap 500).
    await tester.enterText(_kursField(), '20.000');
    await tester.pump();
    expect(
      find.text('Terbaca: \$500,00 ≈ Rp10.000.000 (kurs Rp20.000)'),
      findsOneWidget,
    );

    // Kurs kosong -> tidak ada konversi, diberi peringatan.
    await tester.enterText(_kursField(), '');
    await tester.pump();
    expect(find.textContaining('isi kurs acuan USD/IDR'), findsOneWidget);
    await tester.enterText(_kursField(), '20.000');
    await tester.pump();

    // Kembali ke IDR: field Rupiah berisi hasil konversi terakhir.
    await _tapCur(tester, 'IDR');
    expect(find.text('MODAL BERSIH AKUN (IDR)'), findsOneWidget);
    expect(_text(tester, _modalField('IDR')), '10.000.000');
    expect(find.text('Terbaca: Rp10.000.000'), findsOneWidget);

    // Pilihan mata uang & modal USD ikut tersimpan.
    await _tapCur(tester, 'USD');
    await tester.pump(const Duration(milliseconds: 50));
    final sp = await SharedPreferences.getInstance();
    final saved = jsonDecode(sp.getString('lotxau2')!) as Map<String, dynamic>;
    expect(saved['balCur'], 'USD');
    expect(saved['balanceUsd'], '500.00');
    expect(saved['balance'], '10.000.000');
    await tester.pump(const Duration(milliseconds: 500));
  });

  testWidgets('mode USD dipulihkan dari penyimpanan & langsung dikonversi', (
    tester,
  ) async {
    await _pumpApp(tester, {
      'lotxau2': jsonEncode({
        'balance': '1',
        'balanceUsd': '1000',
        'balCur': 'USD',
        'kurs': '18.000',
      }),
    });
    await _openKursPanel(tester);
    expect(find.text('MODAL BERSIH AKUN (USD)'), findsOneWidget);
    expect(_text(tester, _modalField('USD')), '1000');
    // balance lama ('1') diganti hasil konversi 1000 × 18.000.
    expect(
      find.text('Terbaca: \$1.000,00 ≈ Rp18.000.000 (kurs Rp18.000)'),
      findsOneWidget,
    );
    await tester.pump(const Duration(milliseconds: 500));
  });

  testWidgets('data lama tanpa balCur tetap IDR (kompatibel mundur)', (
    tester,
  ) async {
    await _pumpApp(tester, {
      'lotxau2': jsonEncode({'balance': '5.324.667', 'kurs': '18.227'}),
    });
    await _openKursPanel(tester);
    expect(find.text('MODAL BERSIH AKUN (IDR)'), findsOneWidget);
    expect(_text(tester, _modalField('IDR')), '5.324.667');
    expect(find.text('Terbaca: Rp5.324.667'), findsOneWidget);
    await tester.pump(const Duration(milliseconds: 500));
  });

  testWidgets('memilih Rules (tabel Rupiah) mengembalikan input ke IDR', (
    tester,
  ) async {
    await _pumpApp(tester, {
      'lotxau2': jsonEncode({
        'balanceUsd': '1000',
        'balCur': 'USD',
        'kurs': '18.000',
      }),
      'lotxau_rules': jsonEncode({'sel': 1, 'mode': 'normal'}),
    });
    await _openKursPanel(tester);
    expect(find.text('MODAL BERSIH AKUN (IDR)'), findsOneWidget);
    expect(find.text('RULE 1'), findsOneWidget);
    expect(find.textContaining('Terbaca: Rp'), findsOneWidget);
    await tester.pump(const Duration(milliseconds: 500));
  });
}

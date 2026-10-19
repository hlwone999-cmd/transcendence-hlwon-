// Regresi bug "saat tabelnya di geser ada ruang kosong":
// StickyTable ada DI LUAR SafeArea (SafeArea hanya membungkus top bar),
// sehingga MediaQuery.padding.top (tinggi status bar) masih terlihat oleh
// kedua ListView vertikal. ListView tanpa `padding` eksplisit menyuntiknya
// sebagai SliverPadding → muncul slot kosong ~1 tinggi baris di antara
// header dan baris pertama, dan slot itu ikut kegeser saat tabel di-drag.
// Fix: padding: EdgeInsets.zero pada kedua ListView.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:transcendence/sticky_table.dart';

void main() {
  Future<void> pumpTable(WidgetTester tester,
      {EdgeInsets padding = const EdgeInsets.only(top: 42)}) async {
    await tester.pumpWidget(
      MediaQuery(
        data: MediaQueryData(padding: padding),
        child: Directionality(
          textDirection: TextDirection.ltr,
          child: StickyTable(
            cols: const [
              TblCol(72, Text('H0')),
              TblCol(86, Text('H1')),
              TblCol(86, Text('H2')),
            ],
            rowCount: 100,
            pickRow: -1,
            maxHeight: 500, // total 500px < permukaan test 600px (anti-overflow)
            borderColor: Colors.grey,
            cellBuilder: (ctx, r, c) => Text('r$r c$c'),
          ),
        ),
      ),
    );
    await tester.pump();
  }

  testWidgets(
      'tidak ada slot kosong antara header tabel dan baris pertama '
      '(MediaQuery.padding TIDAK disuntik ke ListView)', (tester) async {
    // Simulasi status bar 42px — sebelum fix, gap antara header & baris
    // pertama = 42px dan ikut kegeser saat tabel di-drag.
    await pumpTable(tester);

    final firstRowTop = tester.getRect(find.text('r0 c0')).top;

    // Sebelum fix: ListView menyuntik MediaQuery.padding.top (42px di sini)
    // → baris pertama turun 42px di bawah header dan slotnya ikut kegeser
    // saat drag. Setelah fix: baris pertama mulai tepat di y=40 (tinggi
    // header), artinya jarak TEKS header → TEKS baris-0 ≈ 40px.
    final headerTop = tester.getRect(find.text('H0')).top;
    final offset = firstRowTop - headerTop;
    expect(offset, moreOrLessEquals(40, epsilon: 1.0),
        reason:
            'teks baris-0 harus mulai tepat 40px (tinggi header) di bawah '
            'teks header; ditemukan $offset (injeksi MediaQuery.padding)');
  });

  testWidgets('baris pertama & seluruh tabel mulai tepat di bawah header',
      (tester) async {
    await pumpTable(tester);

    // Tinggi header default = 40 → TEKS baris 0 harus mulai di y = 40.
    final firstRowTop = tester.getRect(find.text('r0 c0')).top;
    expect(firstRowTop, moreOrLessEquals(40, epsilon: 1.0));

    // Baris kedua tepat 1 tinggi baris (42) di bawah baris pertama —
    // memastikan itemExtent tetap rapat tanpa jeda.
    final secondRowTop = tester.getRect(find.text('r1 c0')).top;
    expect(secondRowTop - firstRowTop, moreOrLessEquals(42, epsilon: 0.5));
  });
}

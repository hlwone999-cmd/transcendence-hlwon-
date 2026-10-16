// Kalkulator Lot XAUUSD (IDR) — tables_build.dart
// Builder sel untuk tabel rekomendasi & matriks — replika markup/klas CSS web
// (pick = baris emas "◀", tight = pudar, me = kolom risk terisi, fit = kotak
// bergaris emas, dead = pudar).
//
// PERBAIKAN PERFORMA: string & flag sel dihitung SEKALI per perubahan data
// (specs), sedangkan widget Text/Container hanya dibuat untuk sel yang
// terlihat (dipanggil lazy oleh StickyTable). Tabel 1000 baris jadi ringan.
import 'package:flutter/material.dart';
import 'engine.dart';
import 'sticky_table.dart';
import 'ui_kit.dart';

const double _cellPad = 10.0;

Widget _cell(Widget child,
    {required bool right, Color? bg, EdgeInsets? padding}) {
  return Container(
    alignment: right ? Alignment.centerRight : Alignment.centerLeft,
    padding: padding ?? const EdgeInsets.symmetric(horizontal: _cellPad),
    color: bg,
    child: child,
  );
}

TextStyle _t(double size, FontWeight w, Color c, {bool tab = true}) =>
    TextStyle(
        fontFamily: kBodyFont,
        fontSize: size,
        fontWeight: w,
        color: c,
        fontFeatures: tab ? tabular : null);

// ---------------- tabel rekomendasi (0.01 – 10.00) ----------------

List<TblCol> mainCols(Palette p) => [
      TblCol(64, _cell(Text('Lot', style: _t(12.5, FontWeight.w600, p.muted)),
          right: false, bg: p.paper)),
      TblCol(84,
          _cell(Text('SL pips', style: _t(12.5, FontWeight.w600, p.muted)),
              right: true, bg: p.paper)),
      TblCol(84,
          _cell(Text('TP pips', style: _t(12.5, FontWeight.w600, p.muted)),
              right: true, bg: p.paper)),
      TblCol(132,
          _cell(Text('Risk (Rp)', style: _t(12.5, FontWeight.w600, p.muted)),
              right: true, bg: p.paper)),
      TblCol(132,
          _cell(Text('Target (Rp)', style: _t(12.5, FontWeight.w600, p.muted)),
              right: true, bg: p.paper)),
    ];

/// Data ringan per baris tabel rekomendasi (tanpa widget — murah dibuat).
class MainRowSpec {
  final String lot;
  final bool pick, tight;
  final String sl, tp, r, t;
  const MainRowSpec(
      this.lot, this.pick, this.tight, this.sl, this.tp, this.r, this.t);
}

/// Hitung specs sekali per perubahan data (dipanggil via cache identitas).
List<MainRowSpec> mainRowSpecs(CalcResult c) {
  final out = <MainRowSpec>[];
  for (final r in c.rows) {
    final pick = c.pick >= 0 && identical(r, c.rows[c.pick]);
    final tight = r.sl < 10;
    out.add(MainRowSpec(lotTxt(r.lot), pick, tight, one(r.sl), one(r.tp),
        groupInt(r.r), groupInt(r.t)));
  }
  return out;
}

/// Builder sel lazy untuk tabel rekomendasi — visual sama persis dengan
/// mainCells() lama (rich text ◀, pudar bila tight, bg emas bila pick).
CellBuilder mainCellBuilder(List<MainRowSpec> specs, Palette p) {
  return (BuildContext context, int row, int col) {
    final s = specs[row];
    final bg = s.pick ? p.goldsoft : null;
    switch (col) {
      case 0:
        final lotStyle = _t(14, FontWeight.w700, s.tight ? p.muted : p.ink);
        return _cell(
          RichText(
            maxLines: 1,
            text: TextSpan(children: [
              TextSpan(text: s.lot, style: lotStyle),
              if (s.pick)
                TextSpan(
                    text: ' ◀', style: _t(14, FontWeight.w700, p.gold, tab: false)),
            ]),
          ),
          right: false,
          bg: bg,
        );
      case 1:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.sl, style: _t(14, FontWeight.w600, p.red))),
          right: true,
          bg: bg,
        );
      case 2:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.tp, style: _t(14, FontWeight.w600, p.good))),
          right: true,
          bg: bg,
        );
      case 3:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.r, style: _t(14, FontWeight.w400, p.ink))),
          right: true,
          bg: bg,
        );
      default:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.t, style: _t(14, FontWeight.w400, p.ink))),
          right: true,
          bg: bg,
        );
    }
  };
}

// ---------------- matriks SL pips per lot × % risiko ----------------

List<TblCol> matrixCols(MatrixResult m, double bal, Palette p) {
  final cols = <TblCol>[
    TblCol(64,
        _cell(Text('Lot', style: _t(12.5, FontWeight.w600, p.muted)),
            right: false, bg: p.paper)),
  ];
  for (final pc in m.pcts) {
    final me = pc == m.rp;
    cols.add(TblCol(
        92,
        _cell(
            Column(
              crossAxisAlignment: CrossAxisAlignment.center,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('${jsNum(pc).replaceFirst('.', ',')}%',
                    style: _t(12.5, FontWeight.w600, p.muted)),
                Text(idr(bal * pc / 100),
                    style: _t(11, FontWeight.w500, p.muted)),
              ],
            ),
            right: true,
            bg: me ? p.goldsoft : p.paper)));
  }
  return cols;
}

/// Data ringan per sel matriks.
class MatrixCellSpec {
  final String text;
  final bool fit, dead, me;
  const MatrixCellSpec(this.text, this.fit, this.dead, this.me);
}

class MatrixRowSpec {
  final String lot;
  final List<MatrixCellSpec> cells;
  const MatrixRowSpec(this.lot, this.cells);
}

/// Hitung specs matriks sekali per perubahan data.
List<MatrixRowSpec> matrixRowSpecs(MatrixResult m) {
  final out = <MatrixRowSpec>[];
  for (int i = 0; i < kLots.length; i++) {
    final cells = <MatrixCellSpec>[];
    for (int j = 0; j < m.pcts.length; j++) {
      cells.add(MatrixCellSpec(one(m.pips[i][j]), m.fitIdx[j] == i,
          m.pips[i][j] < 10, m.pcts[j] == m.rp));
    }
    out.add(MatrixRowSpec(lotTxt(kLots[i]), cells));
  }
  return out;
}

/// Builder sel lazy matriks — visual sama persis dengan matrixCells() lama.
CellBuilder matrixCellBuilder(List<MatrixRowSpec> specs, Palette p) {
  return (BuildContext context, int row, int col) {
    final s = specs[row];
    if (col == 0) {
      return _cell(Text(s.lot, style: _t(14, FontWeight.w700, p.ink)),
          right: false);
    }
    final c = s.cells[col - 1];
    final me = c.me;
    final bg = me ? p.goldsoft : null;
    final txt = Text(c.text,
        style: _t(14, c.fit ? FontWeight.w700 : FontWeight.w400,
            c.fit ? p.gold : (c.dead ? p.muted : p.ink)));
    return _cell(
      c.fit
          ? Container(
              margin: const EdgeInsets.all(2),
              alignment: Alignment.centerRight,
              padding: const EdgeInsets.only(right: 8),
              decoration: BoxDecoration(
                  border: Border.all(width: 2, color: p.gold)),
              child: txt)
          : Opacity(opacity: c.dead ? 0.45 : 1, child: txt),
      right: true,
      bg: bg,
    );
  };
}

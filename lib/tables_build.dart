// Transcendence — tables_build.dart
// Builder sel untuk tabel rekomendasi & matriks — gaya "Obsidian Precision":
// angka monospace rata kanan, hairline divider, SL = salmon, TP = mint,
// baris pick = tint emas + badge BEST, baris tight = redup.
//
// PERFORMA (tetap seperti sebelumnya): string & flag sel dihitung SEKALI per
// perubahan data (specs), widget hanya dibuat untuk baris yang terlihat
// (dipanggil lazy oleh StickyTable).
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

TextStyle _mono(double size, FontWeight w, Color c, {bool tab = true}) =>
    TextStyle(
        fontFamily: kMonoFont,
        fontSize: size,
        fontWeight: w,
        height: 1.2,
        letterSpacing: 0.2,
        color: c,
        fontFeatures: tab ? tabular : null);

TextStyle _lbl(double size, FontWeight w, Color c) => TextStyle(
    fontFamily: kBodyFont,
    fontSize: size,
    fontWeight: w,
    letterSpacing: 0.8,
    color: c);

// ---------------- tabel rekomendasi (0.01 – 10.00) ----------------

List<TblCol> mainCols(Palette p) => [
      TblCol(72,
          _cell(Text('LOT SIZE', style: _lbl(10.5, FontWeight.w500, p.muted)),
              right: false, bg: p.surface1)),
      TblCol(86,
          _cell(Text('SL (PIPS)', style: _lbl(10.5, FontWeight.w500, p.muted)),
              right: true, bg: p.surface1)),
      TblCol(86,
          _cell(Text('TP (PIPS)', style: _lbl(10.5, FontWeight.w500, p.muted)),
              right: true, bg: p.surface1)),
      TblCol(126,
          _cell(Text('RISIKO (RP)', style: _lbl(10.5, FontWeight.w500, p.muted)),
              right: true, bg: p.surface1)),
      TblCol(126,
          _cell(Text('TARGET (RP)', style: _lbl(10.5, FontWeight.w500, p.muted)),
              right: true, bg: p.surface1)),
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

/// Builder sel lazy untuk tabel rekomendasi.
CellBuilder mainCellBuilder(List<MainRowSpec> specs, Palette p) {
  return (BuildContext context, int row, int col) {
    final s = specs[row];
    final bg = s.pick ? p.goldsoft : null;
    switch (col) {
      case 0:
        return _cell(
          Row(children: [
            if (s.pick)
              Container(
                width: 6,
                height: 6,
                margin: const EdgeInsets.only(right: 6),
                decoration:
                    BoxDecoration(shape: BoxShape.circle, color: p.gold),
              )
            else
              const SizedBox(width: 12),
            Flexible(
              child: Text(s.lot,
                  maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: _mono(13, FontWeight.w600,
                      s.tight ? p.muted : (s.pick ? p.gold : p.ink))),
            ),
            if (s.pick) ...[
              const SizedBox(width: 6),
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 5, vertical: 1.5),
                decoration: BoxDecoration(
                  color: p.goldSolid,
                  borderRadius: BorderRadius.circular(4),
                ),
                child: Text('BEST',
                    style: TextStyle(
                        fontFamily: kBodyFont,
                        fontSize: 8.5,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.6,
                        color: p.onGold)),
              ),
            ],
          ]),
          right: false,
          bg: bg,
        );
      case 1:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.sl,
                  style: _mono(13, FontWeight.w500, p.red))),
          right: true,
          bg: bg,
        );
      case 2:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.tp,
                  style: _mono(13, FontWeight.w500, p.mint))),
          right: true,
          bg: bg,
        );
      case 3:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.r, style: _mono(13, FontWeight.w400, p.ink))),
          right: true,
          bg: bg,
        );
      default:
        return _cell(
          Opacity(
              opacity: s.tight ? 0.45 : 1,
              child: Text(s.t, style: _mono(13, FontWeight.w400, p.muted))),
          right: true,
          bg: bg,
        );
    }
  };
}

// ---------------- matriks SL pips per lot × % risiko ----------------

List<TblCol> matrixCols(MatrixResult m, double bal, Palette p) {
  final cols = <TblCol>[
    TblCol(72,
        _cell(Text('LOT SIZE', style: _lbl(10.5, FontWeight.w500, p.muted)),
            right: false, bg: p.surface1)),
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
                    style: _mono(11, FontWeight.w600,
                        me ? p.gold : p.muted)),
                Text(idr(bal * pc / 100),
                    style: _lbl(9, FontWeight.w400, p.dim)),
              ],
            ),
            right: true,
            bg: me ? p.goldsoft : p.surface1)));
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

/// Builder sel lazy matriks.
CellBuilder matrixCellBuilder(List<MatrixRowSpec> specs, Palette p) {
  return (BuildContext context, int row, int col) {
    final s = specs[row];
    if (col == 0) {
      return _cell(Text(s.lot, style: _mono(13, FontWeight.w600, p.ink)),
          right: false);
    }
    final c = s.cells[col - 1];
    final me = c.me;
    final bg = me ? p.goldsoft : null;
    final txt = Text(c.text,
        style: _mono(
            13,
            c.fit ? FontWeight.w700 : FontWeight.w400,
            c.fit ? p.gold : (c.dead ? p.dim : p.ink)));
    return _cell(
      c.fit
          ? Container(
              margin: const EdgeInsets.all(2),
              alignment: Alignment.centerRight,
              padding: const EdgeInsets.only(right: 8),
              decoration: BoxDecoration(
                  border: Border.all(width: 2, color: p.goldSolid),
                  borderRadius: BorderRadius.circular(4)),
              child: txt)
          : Opacity(opacity: c.dead ? 0.45 : 1, child: txt),
      right: true,
      bg: bg,
    );
  };
}

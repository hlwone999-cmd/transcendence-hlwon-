// Kalkulator Lot XAUUSD (IDR) — tables_build.dart
// Builder sel untuk tabel rekomendasi & matriks — replika markup/klas CSS web
// (pick = baris emas "◀", tight = pudar, me = kolom risk terisi, fit = kotak
// bergaris emas, dead = pudar).
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

List<List<Widget>> mainCells(CalcResult c, Palette p) {
  final out = <List<Widget>>[];
  for (final r in c.rows) {
    final pick = c.pick >= 0 && identical(r, c.rows[c.pick]);
    final tight = r.sl < 10;
    final bg = pick ? p.goldsoft : null;
    final lotStyle = _t(14, FontWeight.w700, tight ? p.muted : p.ink);
    final lot = RichText(
      maxLines: 1,
      text: TextSpan(children: [
        TextSpan(text: lotTxt(r.lot), style: lotStyle),
        if (pick)
          TextSpan(
              text: ' ◀', style: _t(14, FontWeight.w700, p.gold, tab: false)),
      ]),
    );
    out.add([
      _cell(lot, right: false, bg: bg),
      _cell(
          Opacity(
              opacity: tight ? 0.45 : 1,
              child: Text(one(r.sl), style: _t(14, FontWeight.w600, p.red))),
          right: true,
          bg: bg),
      _cell(
          Opacity(
              opacity: tight ? 0.45 : 1,
              child: Text(one(r.tp), style: _t(14, FontWeight.w600, p.good))),
          right: true,
          bg: bg),
      _cell(
          Opacity(
              opacity: tight ? 0.45 : 1,
              child:
                  Text(groupInt(r.r), style: _t(14, FontWeight.w400, p.ink))),
          right: true,
          bg: bg),
      _cell(
          Opacity(
              opacity: tight ? 0.45 : 1,
              child:
                  Text(groupInt(r.t), style: _t(14, FontWeight.w400, p.ink))),
          right: true,
          bg: bg),
    ]);
  }
  return out;
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

List<List<Widget>> matrixCells(MatrixResult m, Palette p) {
  final out = <List<Widget>>[];
  for (int i = 0; i < kLots.length; i++) {
    final row = <Widget>[
      _cell(Text(lotTxt(kLots[i]), style: _t(14, FontWeight.w700, p.ink)),
          right: false),
    ];
    for (int j = 0; j < m.pcts.length; j++) {
      final me = m.pcts[j] == m.rp;
      final fit = m.fitIdx[j] == i;
      final dead = m.pips[i][j] < 10;
      final txt = Text(one(m.pips[i][j]),
          style: _t(14, fit ? FontWeight.w700 : FontWeight.w400,
              fit ? p.gold : (dead ? p.muted : p.ink)));
      row.add(_cell(
        fit
            ? Container(
                margin: const EdgeInsets.all(2),
                alignment: Alignment.centerRight,
                padding: const EdgeInsets.only(right: 8),
                decoration: BoxDecoration(
                    border: Border.all(width: 2, color: p.gold)),
                child: txt)
            : Opacity(opacity: dead ? 0.45 : 1, child: txt),
        right: true,
        bg: me ? p.goldsoft : null,
      ));
    }
    out.add(row);
  }
  return out;
}
